"""Tests for orphaned_bot_account_monitor.

Two things carry most of the weight here.

**The false-positive guards.** The naive test ("account name not in members")
is wrong three ways on a real pod, and each way would fire a loud security
Signal about a healthy bot or the operator's own account. Those three shapes
get dedicated tests, because getting them wrong is worse than not shipping the
monitor.

**The fail-safe.** A sweep-style monitor that cannot read must never look
clean. In particular a failed account enumeration must SKIP the sweep — with
no account list, `kept_signatures` is empty and `sweep_resolve` would
auto-resolve every live orphan Signal, which is the exact catastrophe the
unreadable type exists to prevent.

Bot ids and account names here are placeholders (docs/PLACEHOLDER_NAMING.md) —
never real accounts.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import orphaned_bot_account_monitor as monitor  # noqa: E402
from runtime.isolation import (  # noqa: E402
    Account,
    IsolationError,
    set_isolation,
)

# Placeholder identities modelling the reference pod's real shapes:
#   ACTIVE_BOT       — an ordinary bot, account name == bot id
#   ALIAS_BOT        — a roster member running under a DIFFERENT account name
#   ALIAS_ACCOUNT    — ...that account
#   RETIRED_ACCOUNT  — the orphan: Evolve install, no roster entry
#   OPERATOR_ACCOUNT — a human's own OpenClaw install, never Evolve-provisioned
ACTIVE_BOT = "team_bot_a"
ALIAS_BOT = "team_bot_b"
ALIAS_ACCOUNT = "shared_account_b"
RETIRED_ACCOUNT = "team_bot_retired"
OPERATOR_ACCOUNT = "pod_operator"

FAKE_TOKEN = "PLACEHOLDER-not-a-real-token-0000000000"


class _FakeAccounts:
    """Isolation adapter stub returning a fixed account list (or raising)."""

    def __init__(self, accounts, error: "str | None" = None) -> None:
        self._accounts = tuple(accounts)
        self._error = error

    def list_accounts(self):
        if self._error:
            raise IsolationError(self._error)
        return self._accounts


def _make_home(root: Path, account: str, *, evolve: bool, token: bool = False) -> Path:
    """Create an account home. ``evolve`` plants the Evolve-provisioned marker."""
    home = root / account
    oc = home / ".openclaw"
    oc.mkdir(parents=True)
    if evolve:
        (oc / "workspace" / "evolve").mkdir(parents=True)
    else:
        # A personal OpenClaw install: config present, no Evolve markers.
        (oc / "workspace").mkdir(parents=True)
    config: dict = {"agents": {"defaults": {"maxTokens": 4096}}}
    if token:
        config["channels"] = {"telegram": {"botToken": FAKE_TOKEN}}
    (oc / "openclaw.json").write_text(json.dumps(config))
    return home


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A synthetic pod: network.json + signal store + swappable account list."""
    shared = tmp_path / "shared"
    for sub in ("firing", "snoozed", "archived", "log"):
        (shared / "signals" / sub).mkdir(parents=True)
    homes = tmp_path / "homes"
    homes.mkdir()

    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "members": [ACTIVE_BOT, ALIAS_BOT],
        # The alias shape: a roster member whose account name differs.
        "bots": {ACTIVE_BOT: {}, ALIAS_BOT: {"user": ALIAS_ACCOUNT}},
    }))

    config_mod = types.ModuleType("evolve_admin.config")
    config_mod.load_network = lambda p: json.loads(Path(p).read_text())
    config_mod.resolve_pod_context = lambda n: {"ssh_prefix": ""}
    ea = types.ModuleType("evolve_admin")
    ea.config = config_mod
    monkeypatch.setitem(sys.modules, "evolve_admin", ea)
    monkeypatch.setitem(sys.modules, "evolve_admin.config", config_mod)

    yield types.SimpleNamespace(
        net=net_path, shared=shared, homes=homes,
        set_accounts=lambda accts, error=None: set_isolation(
            _FakeAccounts(accts, error)
        ),
    )
    set_isolation(None)


def _firing(shared):
    from signals import store as signals_store
    return list(signals_store.iter_signals(shared, subdirs=("firing",)))


# ── The condition fires ───────────────────────────────────────────────────


def test_orphan_with_live_credentials_fires(pod):
    """ACCEPTANCE: the 2026-08-02 shape — an account with an Evolve install,
    no roster entry, and a live channel token still on disk."""
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True, token=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])

    summary = monitor.run(pod.net)

    assert summary["orphans"] == 1
    fired = _firing(pod.shared)
    assert len(fired) == 1
    sig = fired[0]
    assert sig.type == monitor.ORPHAN_TYPE
    assert sig.scope == "pod"
    assert sig.bot_id is None
    assert sig.details["account"] == RETIRED_ACCOUNT
    assert sig.details["live_credential_count"] == 1
    assert sig.signature == (
        f"{monitor.PRODUCER}:{monitor.ORPHAN_TYPE}:{RETIRED_ACCOUNT}"
    )


def test_signal_never_carries_a_credential_value(pod):
    """The value-free contract, end to end through the Signal payload — this
    body is mirrored into operator chat and the Alerts UI."""
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True, token=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])

    monitor.run(pod.net)

    sig = _firing(pod.shared)[0]
    assert FAKE_TOKEN not in sig.body
    assert FAKE_TOKEN not in json.dumps(sig.details)
    assert FAKE_TOKEN not in sig.title
    # ...but the operator can still tell it is populated, and where to go.
    assert "chars" in json.dumps(sig.details)
    assert "BotFather" in json.dumps(sig.details)


# ── The three false-positive guards ───────────────────────────────────────


def test_roster_member_under_a_different_account_is_not_an_orphan(pod):
    """The `team_bot_b` runs on `shared_account_b` shape. Resolving membership by account
    name instead of get_bot_user would fire a security Signal on a live bot."""
    home = _make_home(pod.homes, ALIAS_ACCOUNT, evolve=True, token=True)
    pod.set_accounts([Account(ALIAS_ACCOUNT, 503, home)])

    summary = monitor.run(pod.net)

    assert summary["orphans"] == 0
    assert _firing(pod.shared) == []


def test_operator_personal_openclaw_install_is_not_a_bot(pod):
    """The `pod_admin_user` shape: a real human with their own OpenClaw. `.openclaw/`
    alone is not evidence of an Evolve-provisioned bot."""
    home = _make_home(pod.homes, OPERATOR_ACCOUNT, evolve=False, token=True)
    pod.set_accounts([Account(OPERATOR_ACCOUNT, 501, home)])

    summary = monitor.run(pod.net)

    assert summary["orphans"] == 0
    assert _firing(pod.shared) == []


def test_evolve_service_account_is_not_a_bot(pod):
    """The `evolve` service user carries stray OpenClaw state on the reference
    pod but is infrastructure, never a roster member."""
    home = _make_home(pod.homes, "evolve", evolve=True)
    pod.set_accounts([Account("evolve", 507, home)])

    assert monitor.run(pod.net)["orphans"] == 0


def test_evo_is_not_hardcoded_as_infra(pod):
    """`evo` is the PRIMARY BOT's account post-account-separation, so it must
    be excluded by the roster join, not by a name list — otherwise retiring the
    primary would leave a genuine orphan permanently invisible."""
    assert "evo" not in monitor._INFRA_ACCOUNTS


# ── Retirement record ─────────────────────────────────────────────────────


def test_retirement_archive_is_reported_when_present(pod):
    (pod.shared / "archived-bots" / f"{RETIRED_ACCOUNT}-2026-06-15").mkdir(parents=True)
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])

    monitor.run(pod.net)

    sig = _firing(pod.shared)[0]
    assert sig.details["retired_via_retire_bot"] is True
    assert sig.details["retirement_archive"] == f"{RETIRED_ACCOUNT}-2026-06-15"
    assert "retired through `retire-bot`" in sig.body


def test_missing_retirement_archive_says_so(pod):
    """The materially different case: left the roster with no retirement
    record at all — worth an operator going to look at how."""
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])

    monitor.run(pod.net)

    sig = _firing(pod.shared)[0]
    assert sig.details["retired_via_retire_bot"] is False
    assert "NOT retired through `retire-bot`" in sig.body


def test_find_retirement_record_picks_the_newest(tmp_path):
    root = tmp_path / "archived-bots"
    root.mkdir()
    (root / f"{RETIRED_ACCOUNT}-2026-06-15").mkdir()
    (root / f"{RETIRED_ACCOUNT}-2026-07-01").mkdir()
    (root / "other_bot-2026-06-15").mkdir()
    assert monitor.find_retirement_record(tmp_path, RETIRED_ACCOUNT) == (
        f"{RETIRED_ACCOUNT}-2026-07-01"
    )


def test_find_retirement_record_absent_archive_dir(tmp_path):
    assert monitor.find_retirement_record(tmp_path, RETIRED_ACCOUNT) is None


# ── Dedup + sweep ─────────────────────────────────────────────────────────


def test_repeat_run_dedups(pod):
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])

    monitor.run(pod.net)
    monitor.run(pod.net)

    fired = _firing(pod.shared)
    assert len(fired) == 1
    assert fired[0].observation_count == 2


def test_sweep_resolves_once_the_install_is_gone(pod):
    """`delete-bot` removes the account entirely; the Signal must clear.

    The account list still holds the live bot — modelling the real post-delete
    host rather than an empty one (a genuinely empty enumeration raises, and is
    covered by the enumeration-failure test below)."""
    active_home = _make_home(pod.homes, ACTIVE_BOT, evolve=True)
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    pod.set_accounts([
        Account(ACTIVE_BOT, 502, active_home),
        Account(RETIRED_ACCOUNT, 512, home),
    ])
    monitor.run(pod.net)
    assert len(_firing(pod.shared)) == 1

    pod.set_accounts([Account(ACTIVE_BOT, 502, active_home)])
    summary = monitor.run(pod.net)

    assert summary["signals_resolved"] == 1
    assert _firing(pod.shared) == []


# ── Fail-safe: a blind tick must never read as clean ──────────────────────


def test_enumeration_failure_fires_unreadable_and_skips_the_sweep(pod):
    """THE critical fail-safe. With no account list, kept_signatures is empty —
    sweeping would auto-resolve every live orphan Signal."""
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])
    monitor.run(pod.net)
    assert len(_firing(pod.shared)) == 1

    pod.set_accounts([], error="directory service not answering")
    summary = monitor.run(pod.net)

    assert summary["swept"] is False
    assert summary["signals_resolved"] == 0
    types_now = {s.type for s in _firing(pod.shared)}
    # The orphan Signal SURVIVED, and the blind spot is now marked.
    assert types_now == {monitor.ORPHAN_TYPE, monitor.UNREADABLE_TYPE}


def test_unreadable_install_keeps_the_existing_orphan_signal(pod, monkeypatch):
    """A per-account blind probe must not sweep-resolve that account's Signal."""
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])
    monitor.run(pod.net)

    monkeypatch.setattr(monitor, "evolve_install_status", lambda home: "unreadable")
    summary = monitor.run(pod.net)

    assert summary["unreadable"] == 1
    assert summary["signals_resolved"] == 0
    types_now = {s.type for s in _firing(pod.shared)}
    assert monitor.ORPHAN_TYPE in types_now
    assert monitor.UNREADABLE_TYPE in types_now


def test_unreadable_clears_when_the_read_recovers(pod, monkeypatch):
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])
    monkeypatch.setattr(monitor, "evolve_install_status", lambda home: "unreadable")
    monitor.run(pod.net)
    assert any(s.type == monitor.UNREADABLE_TYPE for s in _firing(pod.shared))

    monkeypatch.undo()
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])
    monitor.run(pod.net)

    types_now = {s.type for s in _firing(pod.shared)}
    assert monitor.UNREADABLE_TYPE not in types_now
    assert monitor.ORPHAN_TYPE in types_now


def test_install_status_blind_only_when_a_probe_hits_eacces(tmp_path):
    """An account with no markers and clean absent-probes is definitively not
    an Evolve install — only an EACCES makes us unable to say."""
    home = tmp_path / "acct"
    (home / ".openclaw").mkdir(parents=True)
    assert monitor.evolve_install_status(home) == "not-evolve"
    (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
    assert monitor.evolve_install_status(home) == "evolve"


# ── Roster resolution ─────────────────────────────────────────────────────


def test_roster_accounts_resolves_aliases():
    network = {
        "members": [ACTIVE_BOT, ALIAS_BOT],
        "bots": {ACTIVE_BOT: {}, ALIAS_BOT: {"user": ALIAS_ACCOUNT}},
    }
    assert monitor.roster_accounts(network) == {ACTIVE_BOT, ALIAS_ACCOUNT}


def test_roster_accounts_falls_back_to_bot_id_on_a_malformed_block():
    """A malformed bot block must never SHRINK the known-account set — that
    would manufacture an orphan out of a live bot."""
    network = {"members": [ACTIVE_BOT], "bots": {ACTIVE_BOT: "not-a-dict"}}
    assert ACTIVE_BOT in monitor.roster_accounts(network)


def test_roster_accounts_falls_back_to_bots_keys_without_members():
    network = {"bots": {ACTIVE_BOT: {}}}
    assert monitor.roster_accounts(network) == {ACTIVE_BOT}


# ── Dry run ───────────────────────────────────────────────────────────────


def test_dry_run_writes_nothing(pod):
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])

    summary = monitor.run(pod.net, dry_run=True)

    assert summary["orphans"] == 1
    assert summary["signals_fired"] == 0
    assert _firing(pod.shared) == []


# ── The pod admin account is excluded, not permanently "blind" ────────────


def test_pod_admin_account_is_excluded(pod, monkeypatch, tmp_path):
    """Found by the live dry-run: the admin home is 0700 with no evolve ACL —
    permanently — so probing it always returns EACCES and it would fire a
    blind-spot Signal that can NEVER clear. A standing alert with no reachable
    resolution is the cries-wolf pattern this producer exists to replace."""
    net = json.loads(pod.net.read_text())
    net["admin_user"] = OPERATOR_ACCOUNT
    pod.net.write_text(json.dumps(net))

    home = _make_home(pod.homes, OPERATOR_ACCOUNT, evolve=True)
    pod.set_accounts([Account(OPERATOR_ACCOUNT, 501, home)])
    # Even if every probe went blind, the account must not be classified.
    monkeypatch.setattr(monitor, "evolve_install_status", lambda home: "unreadable")

    summary = monitor.run(pod.net)

    assert summary["orphans"] == 0
    assert summary["unreadable"] == 0
    assert _firing(pod.shared) == []


def test_excluded_accounts_reads_admin_user_from_config():
    """Config-derived, not hardcoded — a pod that names its admin something
    else is covered with no edit here."""
    assert monitor.excluded_accounts({"admin_user": "someone_else"}) == {
        "evolve", "someone_else",
    }
    assert monitor.excluded_accounts({}) == {"evolve"}
    assert monitor.excluded_accounts({"admin_user": "  "}) == {"evolve"}


def test_title_reads_correctly_for_one_and_many_and_unchecked(pod):
    home = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True, token=True)
    pod.set_accounts([Account(RETIRED_ACCOUNT, 512, home)])
    monitor.run(pod.net)

    title = _firing(pod.shared)[0].title
    assert "1 live credential still" in title
    assert "1 live credentials" not in title
    assert len(title) <= monitor._TITLE_SOFT_LIMIT


# ── Title length: degrade to fit, never breach the store's soft limit ─────


def test_title_stays_under_the_soft_limit_with_an_unreadable_count():
    """The first live pod run emitted 83 chars — a 6-char account with 4
    credentials and 1 unreadable location — and logged a store warning on
    every tick. Both inputs are variable-width, so the title must degrade
    rather than be kept short by construction."""
    title = monitor._orphan_title("ledger", 4, 1)
    assert len(title) <= monitor._TITLE_SOFT_LIMIT, title
    assert "4 live credentials" in title


@pytest.mark.parametrize("account", ["a", "team_bot_retired", "x" * 40, "y" * 200])
@pytest.mark.parametrize("live,unread", [(0, 0), (1, 0), (1, 1), (4, 1), (99, 12)])
def test_title_never_breaches_the_limit_for_any_input(account, live, unread):
    title = monitor._orphan_title(account, live, unread)
    assert len(title) <= monitor._TITLE_SOFT_LIMIT, (len(title), title)
    assert title, "title must never be empty"


def test_title_drops_the_parenthetical_before_truncating():
    """Degradation order matters: the unreadable count is recoverable from
    details/body, so it goes first — a truncated account name is not."""
    account = "z" * 40
    title = monitor._orphan_title(account, 4, 1)
    assert account in title, "the account name must survive before the suffix"
    assert "unchecked" not in title


def test_one_failing_inventory_does_not_abort_the_pass(pod, monkeypatch):
    """Without a per-account guard, an unexpected error on the first account
    would skip every later account AND skip the sweep — the monitor goes silent
    instead of degrading."""
    bad = _make_home(pod.homes, RETIRED_ACCOUNT, evolve=True)
    good = _make_home(pod.homes, "other_retired_bot", evolve=True, token=True)
    pod.set_accounts([
        Account(RETIRED_ACCOUNT, 512, bad),
        Account("other_retired_bot", 513, good),
    ])

    real_collect = monitor.credentials.collect

    def _flaky(home):
        if home == bad:
            raise RuntimeError("unexpected probe failure")
        return real_collect(home)

    monkeypatch.setattr(monitor.credentials, "collect", _flaky)
    summary = monitor.run(pod.net)

    # The second account was still scanned, and the failure was reported.
    assert summary["orphans"] == 1
    assert summary["unreadable"] == 1
    assert summary["swept"] is True
    by_type = {s.type: s for s in _firing(pod.shared)}
    assert monitor.ORPHAN_TYPE in by_type and monitor.UNREADABLE_TYPE in by_type
    assert "credential inventory failed" in by_type[monitor.UNREADABLE_TYPE].body
