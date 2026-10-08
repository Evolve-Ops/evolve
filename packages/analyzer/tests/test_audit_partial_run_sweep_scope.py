"""tests/test_audit_partial_run_sweep_scope.py — partial audit runs sweep only their own coverage.

Follow-up to the independent review of #3919 (page-on-transition critical
delivery), observation 4. Design context:
internal/design-security-alert-fatigue-2026-08-31.md.

A partial run (``--bot <id>`` / ``--category <cat>``) computes
``kept_signatures`` from a subset of the checks. Before this fix the
auto-resolve sweep ran unscoped, so every finding the partial run never
looked for was archived as "cleared". That was cosmetic while every
standing critical re-paged on every run; under page-on-transition (R-1)
it is an alert bomb — the wrongly-resolved Signals REOPEN on the next
full run, and a reopen is a firing transition, so every standing critical
pages again.

Covers:
  - ``--bot`` run leaves other bots' firing Signals alone
  - ``--category`` run leaves other categories' firing Signals alone
  - a sub-monitor category (mcp/plugins/…) sweeps no audit Signals at all
  - a FULL run still sweep-resolves cleared conditions (no regression)
  - the reopen-re-page interaction is prevented end to end, with the
    pre-fix behaviour pinned as the explicit counterfactual
  - main() actually wires the computed scope into dispatch_findings
  - an empty ``--bot ""`` scopes like no ``--bot`` at all, while an empty
    ``--category ""`` still sweeps nothing (the two flags carry different
    truthiness contracts in main(), so _sweep_scope must mirror each)
  - the flap-dwell clear-sweep is narrowed to the run's coverage too,
    by parsing the ledger signature (so a partial run resets its OWN
    cleared conditions but leaves out-of-coverage dwell state alone)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
from audit import (  # noqa: E402
    Finding,
    _audit_signature,
    _sweep_scope,
    dispatch_findings,
)
from signals import flap_gate  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def pages(monkeypatch) -> list[str]:
    """Capture (and suppress) the Telegram critical page.

    The stub returns True: ``_send_security_alert``'s return value is the
    delivery outcome ``dispatch_findings`` records on the Signal, and a stub
    that reported nothing would model a page that never reached the operator
    — leaving each episode eligible to re-page on the next run.
    """
    sent: list[str] = []

    def _send(msg, *a, **kw):
        sent.append(msg)
        return True

    monkeypatch.setattr(audit, "_send_security_alert", _send)
    monkeypatch.setattr(audit, "_send_telegram_direct", lambda *a, **kw: True)
    return sent


class _FakeClock:
    """A settable stand-in for the Signal store's wall clock.

    The store stamps state transitions and deliveries with
    ``evolve_util.now_iso_offset`` — SECONDS precision. Page-on-transition
    asks ``entered_firing > last_paged``, strictly. Three dispatch_findings
    calls in one test land in the same second, so that comparison is False
    no matter what the states did, and a re-page test would pass for the
    wrong reason. Advancing this clock by the real 15-minute audit cadence
    between runs is what makes the transition observable.
    """

    def __init__(self) -> None:
        self._t = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)

    def now_iso(self) -> str:
        return self._t.isoformat(timespec="seconds")

    def advance(self, seconds: int) -> None:
        self._t += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> _FakeClock:
    from signals import state_machine as _state_machine

    c = _FakeClock()
    monkeypatch.setattr(signals_store, "_utc_now_iso", c.now_iso)
    monkeypatch.setattr(_state_machine, "_utc_now_iso", c.now_iso)
    return c


def _critical(category: str, bot_id: str | None, message: str) -> Finding:
    return Finding(level="critical", finding_kind="event", category=category,
                   bot_id=bot_id, message=message, detail="")


def _active_bodies(shared_dir: Path) -> set[str]:
    return {
        s.body for s in signals_store.iter_active(shared_dir, producer="audit")
    }


def _state_of(shared_dir: Path, finding: Finding) -> str | None:
    """Current state of the Signal mirroring ``finding``, or None if absent."""
    signature = _audit_signature(finding)
    for subdir in ("firing", "snoozed", "archived"):
        for sig in signals_store.iter_signals(shared_dir, subdirs=[subdir]):
            if sig.signature == signature:
                return sig.state
    return None


# ─────────────────────────────────────────────────────────────────────────────
# _sweep_scope — the coverage map itself
# ─────────────────────────────────────────────────────────────────────────────


def test_full_run_scope_is_unrestricted():
    """No flags = full coverage = sweep everything, exactly as before."""
    assert _sweep_scope(category=None, bot=None) == (None, None)


def test_bot_run_scopes_sweep_to_that_bot():
    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    assert types is None          # every category ran, for that bot
    assert bot_ids == {"admin_bot"}


def test_category_run_scopes_sweep_to_that_category_type():
    types, bot_ids = _sweep_scope(category="identity", bot=None)
    assert types == {"audit_identity"}
    assert bot_ids is None        # every bot's identity checks ran


def test_machine_and_process_categories_share_machine_coverage():
    """main() runs audit_machine + audit_process for EITHER flag, and both
    emit category="machine" findings — so both sweep audit_machine."""
    assert _sweep_scope(category="machine", bot=None)[0] == {"audit_machine"}
    assert _sweep_scope(category="process", bot=None)[0] == {"audit_machine"}


def test_submonitor_categories_sweep_no_audit_types():
    """mcp / plugins / hooks / content_scan / permissions / app_permissions
    produce no audit-producer findings — each sub-monitor owns its own
    producer and runs its own scoped sweep. The audit sweep must be empty."""
    for cat in ("mcp", "plugins", "hooks", "content_scan",
                "permissions", "app_permissions"):
        types, _bots = _sweep_scope(category=cat, bot=None)
        assert types == set(), f"{cat} must sweep no audit types, got {types!r}"


def test_unknown_category_fails_toward_not_sweeping():
    """argparse choices should make this unreachable, but the direction of
    the failure matters: leaving a cleared Signal firing self-heals on the
    next full run; resolving a still-true Signal is the re-page bomb."""
    types, _bots = _sweep_scope(category="not_a_real_category", bot=None)
    assert types == set()


def test_both_flags_narrow_both_dimensions():
    types, bot_ids = _sweep_scope(category="config", bot="team_bot_a")
    assert types == {"audit_config"}
    assert bot_ids == {"team_bot_a"}


def test_every_cli_category_choice_has_a_coverage_entry():
    """The coverage map must stay in step with the --category choices: a new
    choice with no entry would silently fall into the empty-set default and
    stop sweeping its own category forever."""
    captured: dict = {}

    def fake_parse(self, *a, **kw):
        for action in self._actions:
            if action.dest == "category":
                captured["choices"] = list(action.choices or [])
        raise SystemExit(0)

    orig = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = fake_parse  # type: ignore[assignment]
    try:
        with pytest.raises(SystemExit):
            audit.main()
    finally:
        argparse.ArgumentParser.parse_args = orig  # type: ignore[assignment]

    missing = set(captured["choices"]) - set(audit._CATEGORY_FINDING_COVERAGE)
    assert not missing, (
        f"--category choices with no _CATEGORY_FINDING_COVERAGE entry: {missing}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Partial runs leave out-of-coverage Signals alone
# ─────────────────────────────────────────────────────────────────────────────


def test_partial_bot_run_leaves_other_bots_signals_firing(tmp_path, pages):
    """`--bot admin_bot` never looked at team_bot_a — it must not resolve
    team_bot_a's still-true finding."""
    f_a = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_b = _critical("identity", "team_bot_a", "authorized_keys world-writable")

    dispatch_findings([f_a, f_b], tmp_path, config={}, dry_run=False)
    assert len(list(signals_store.iter_active(tmp_path, producer="audit"))) == 2

    # Partial run: only admin_bot scanned, so only admin_bot's finding is kept.
    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    dispatch_findings([f_a], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _state_of(tmp_path, f_b) == "firing", (
        "a --bot run must not resolve another bot's finding"
    )
    assert _state_of(tmp_path, f_a) == "firing"


def test_partial_bot_run_still_resolves_that_bots_cleared_finding(tmp_path, pages):
    """The narrowing must not make --bot a no-op: within its own bot the
    sweep still auto-clears what got fixed."""
    f_a1 = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_a2 = _critical("identity", "admin_bot", "shell history world-readable")
    f_b = _critical("identity", "team_bot_a", "authorized_keys world-writable")

    dispatch_findings([f_a1, f_a2, f_b], tmp_path, config={}, dry_run=False)

    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    dispatch_findings([f_a1], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _state_of(tmp_path, f_a2) == "resolved"
    assert _state_of(tmp_path, f_a1) == "firing"
    assert _state_of(tmp_path, f_b) == "firing"


def test_partial_category_run_leaves_other_categories_firing(tmp_path, pages):
    """`--category identity` never ran the config checks — their Signals
    must survive untouched."""
    f_ident = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_config = _critical("config", "team_bot_a", "openclaw.json drift unexplained")
    f_machine = _critical("machine", None, "FileVault disabled")

    dispatch_findings([f_ident, f_config, f_machine], tmp_path, config={},
                      dry_run=False)
    assert len(list(signals_store.iter_active(tmp_path, producer="audit"))) == 3

    types, bot_ids = _sweep_scope(category="identity", bot=None)
    dispatch_findings([f_ident], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _state_of(tmp_path, f_config) == "firing"
    assert _state_of(tmp_path, f_machine) == "firing"
    assert _state_of(tmp_path, f_ident) == "firing"


def test_partial_category_run_still_resolves_within_its_category(tmp_path, pages):
    f_i1 = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_i2 = _critical("identity", "admin_bot", "shell history world-readable")
    f_config = _critical("config", "team_bot_a", "openclaw.json drift unexplained")

    dispatch_findings([f_i1, f_i2, f_config], tmp_path, config={}, dry_run=False)

    types, bot_ids = _sweep_scope(category="identity", bot=None)
    dispatch_findings([f_i1], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _state_of(tmp_path, f_i2) == "resolved"
    assert _state_of(tmp_path, f_config) == "firing"


def test_submonitor_category_run_resolves_nothing(tmp_path, pages):
    """`--category mcp` derives no audit-producer findings, so the audit
    sweep must not run at all — an empty keep-set with an unscoped sweep
    would archive the entire audit surface."""
    f_ident = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_config = _critical("config", "team_bot_a", "openclaw.json drift unexplained")
    dispatch_findings([f_ident, f_config], tmp_path, config={}, dry_run=False)

    types, bot_ids = _sweep_scope(category="mcp", bot=None)
    dispatch_findings([], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _state_of(tmp_path, f_ident) == "firing"
    assert _state_of(tmp_path, f_config) == "firing"


def test_full_run_still_sweep_resolves_cleared_conditions(tmp_path, pages):
    """No-regression guard on the default path: an unflagged run has full
    coverage and must keep auto-clearing fixed findings."""
    f_a = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_b = _critical("config", "team_bot_a", "openclaw.json drift unexplained")
    f_c = _critical("machine", None, "FileVault disabled")

    dispatch_findings([f_a, f_b, f_c], tmp_path, config={}, dry_run=False)
    assert len(list(signals_store.iter_active(tmp_path, producer="audit"))) == 3

    # Everything fixed.
    dispatch_findings([], tmp_path, config={}, dry_run=False)

    assert list(signals_store.iter_active(tmp_path, producer="audit")) == []
    for f in (f_a, f_b, f_c):
        assert _state_of(tmp_path, f) == "resolved"


# ─────────────────────────────────────────────────────────────────────────────
# The interaction that made this urgent: wrongly-resolved → reopened → re-paged
# ─────────────────────────────────────────────────────────────────────────────


_AUDIT_CADENCE_SECONDS = 15 * 60


def test_partial_run_does_not_cause_a_reopen_repage(tmp_path, pages, clock):
    """End-to-end: a partial run between two full runs must not re-page a
    standing critical it never examined."""
    f_a = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_b = _critical("config", "team_bot_a", "openclaw.json drift unexplained")

    # Run 1 (full) — both newly firing, one page carrying both.
    dispatch_findings([f_a, f_b], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1

    # Run 2 (partial, --category identity) — config was never checked.
    clock.advance(_AUDIT_CADENCE_SECONDS)
    types, bot_ids = _sweep_scope(category="identity", bot=None)
    dispatch_findings([f_a], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)
    assert len(pages) == 1, "a partial run must not page a standing critical"
    assert _state_of(tmp_path, f_b) == "firing"

    # Run 3 (full) — both still true. Both Signals stayed firing, so nothing
    # transitions and nothing pages.
    clock.advance(_AUDIT_CADENCE_SECONDS)
    dispatch_findings([f_a, f_b], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1, (
        f"standing criticals re-paged after a partial run: {pages[1:]}"
    )


def test_unscoped_sweep_would_have_repaged(tmp_path, pages, clock):
    """The counterfactual, pinned so the mechanism can't be forgotten: with
    the sweep left unscoped (the pre-fix call shape), the partial run
    resolves the config critical, the next full run REOPENS it, and a reopen
    is a firing transition — so it pages again.

    Both reopen and re-page need the runs separated in time: the reopen
    window is an hour (so 15 minutes still reopens rather than minting a
    fresh Signal) and the page predicate is a strict timestamp comparison
    at seconds precision.
    """
    f_a = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_b = _critical("config", "team_bot_a", "openclaw.json drift unexplained")

    dispatch_findings([f_a, f_b], tmp_path, config={}, dry_run=False)
    assert len(pages) == 1

    # Pre-fix shape: partial findings, unscoped sweep.
    clock.advance(_AUDIT_CADENCE_SECONDS)
    dispatch_findings([f_a], tmp_path, config={}, dry_run=False)
    assert _state_of(tmp_path, f_b) == "resolved"

    clock.advance(_AUDIT_CADENCE_SECONDS)
    dispatch_findings([f_a, f_b], tmp_path, config={}, dry_run=False)
    assert _state_of(tmp_path, f_b) == "firing"
    assert len(pages) == 2, (
        "expected the reopen to re-page — this is the defect the scoped "
        "sweep prevents"
    )
    # And the re-page is specifically the finding the partial run never
    # examined — not incidental noise from the identity critical.
    assert "openclaw.json drift" in pages[1]
    assert "ssh key" not in pages[1]


# ─────────────────────────────────────────────────────────────────────────────
# main() wiring — the scope must actually reach dispatch_findings
# ─────────────────────────────────────────────────────────────────────────────


_STUBBED_CHECKS = (
    "audit_shell_config", "audit_identity", "audit_script_inventory",
    "audit_workspace_secrets", "audit_policy_file_permissions",
    "audit_config", "audit_cron_health", "audit_oc_security",
    "audit_evolve_sudoers", "audit_machine", "audit_process",
    "audit_proposals",
)


def _run_main(monkeypatch, tmp_path, *, bot, category) -> dict:
    captured: dict = {}
    ns = argparse.Namespace(network=None, dry_run=False, bot=bot,
                            category=category, reset_baselines=False)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self, *a, **kw: ns)
    monkeypatch.setattr(audit, "load_config", lambda *a, **kw: {})
    monkeypatch.setattr(audit, "get_shared_dir", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(audit, "get_members",
                        lambda *a, **kw: ["admin_bot", "team_bot_a"])
    for name in _STUBBED_CHECKS:
        monkeypatch.setattr(audit, name, lambda *a, **kw: [])
    monkeypatch.setattr(audit, "dispatch_findings",
                        lambda *a, **kw: captured.update(kw))
    audit.main()
    return captured


def test_main_passes_bot_scope_to_dispatch(monkeypatch, tmp_path):
    """`--category identity` keeps the sub-monitors from running, so this
    exercises both scope dimensions without touching MCP/plugin probes."""
    captured = _run_main(monkeypatch, tmp_path,
                         bot="admin_bot", category="identity")
    assert captured["sweep_bot_ids"] == {"admin_bot"}
    assert captured["sweep_types"] == {"audit_identity"}


def test_main_passes_no_scope_for_a_category_only_run(monkeypatch, tmp_path):
    captured = _run_main(monkeypatch, tmp_path, bot=None, category="identity")
    assert captured["sweep_bot_ids"] is None
    assert captured["sweep_types"] == {"audit_identity"}


# ─────────────────────────────────────────────────────────────────────────────
# Empty-string flags — the two flags need DIFFERENT truthiness tests
#
# main() narrows the roster under ``if args.bot:`` but gates the check blocks
# on ``run_all = args.category is None``. So ``--bot ""`` is a FULL-coverage
# run (full roster) while ``--category ""`` is a ZERO-coverage run (no block
# executes). _sweep_scope has to mirror each flag's own test; a single shared
# convention would be wrong for one of them either way.
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_bot_flag_scopes_like_no_bot_flag():
    """`--bot ""` leaves main()'s roster at every bot, so the run has full
    per-bot coverage and must sweep unrestricted. Testing ``is not None``
    here yielded ``{""}`` — a bot_id no Signal carries — which silently
    disabled auto-clear for the whole invocation."""
    assert _sweep_scope(category=None, bot="") == _sweep_scope(category=None, bot=None)
    assert _sweep_scope(category=None, bot="") == (None, None)


def test_empty_bot_flag_scopes_like_no_bot_flag_alongside_a_category():
    """The bot dimension normalises independently of the category one."""
    assert _sweep_scope(category="identity", bot="") == ({"audit_identity"}, None)


def test_empty_category_flag_still_sweeps_nothing():
    """The other half of the asymmetry, pinned so nobody "fixes" category to
    match bot. ``run_all`` is ``args.category is None``, so ``--category ""``
    runs NO check block — its coverage really is empty, and a truthiness test
    here would hand a zero-coverage run an unrestricted sweep."""
    types, bot_ids = _sweep_scope(category="", bot=None)
    assert types == set()
    assert bot_ids is None


def test_empty_bot_flag_still_resolves_cleared_findings(tmp_path, pages):
    """The behavioural consequence: a `--bot ""` run has full coverage, so
    auto-clear must still work. Pre-fix this run swept nothing at all."""
    f_a = _critical("identity", "admin_bot", "ssh key 0644 — must be 0600")
    f_b = _critical("config", "team_bot_a", "openclaw.json drift unexplained")
    dispatch_findings([f_a, f_b], tmp_path, config={}, dry_run=False)

    types, bot_ids = _sweep_scope(category=None, bot="")
    dispatch_findings([f_a], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _state_of(tmp_path, f_b) == "resolved"
    assert _state_of(tmp_path, f_a) == "firing"


def test_main_passes_no_bot_scope_for_an_empty_bot_flag(monkeypatch, tmp_path):
    """End to end through main(), where the two truthiness tests have to
    agree: the roster stays full, so the sweep scope must stay unrestricted."""
    captured = _run_main(monkeypatch, tmp_path, bot="", category="identity")
    assert captured["sweep_bot_ids"] is None
    assert captured["sweep_types"] == {"audit_identity"}


# ─────────────────────────────────────────────────────────────────────────────
# The flap-dwell clear-sweep — the same bug class, one layer down
#
# ``flap_gate.note_cleared_absent`` resets the dwell counter for every TRACKED
# signature absent from this run, filtered only by flap-family type. That is
# an absent-branch sweep too, so it has the same precondition: the run must
# actually have looked for what it is about to clear. A partial run has not.
#
# It cannot filter on the store's type/bot fields — a ledger entry records the
# flap-family type and a count, and carries no bot — but it needs no new ledger
# field either: the entry keeps the signature, and audit's signatures encode
# both dimensions as ``audit:<category>:<bot_id|pod>:<digest>``. So the sweep
# is NARROWED, not skipped, and a partial run still clears what it examined.
# ─────────────────────────────────────────────────────────────────────────────


def _flap_warn(bot_id: str) -> Finding:
    """A benign group-readable perm warn — the flap-prone family that dwells
    N≥2 consecutive runs before paging (the Linux-VPS OC re-clamp shape)."""
    return Finding(
        level="warn", category="config", bot_id=bot_id,
        message=f"{bot_id} (fs.auth_profiles.perms_readable): "
                f"auth-profiles.json is group-readable",
        detail="mode 0640 (group-class bit); OC re-clamp flap",
    )


def _dwell_entry_exists(shared_dir: Path, finding: Finding) -> bool:
    return flap_gate._pending_path(shared_dir, _audit_signature(finding)).exists()


def test_flap_warn_dwells_rather_than_firing_on_cycle_one(tmp_path, pages):
    """Precondition for the tests below: the fixture finding really is
    flap-gated, so a pending dwell entry (not a Signal) is what run 1 leaves."""
    warn = _flap_warn("admin_bot")
    dispatch_findings([warn], tmp_path, config={}, dry_run=False)

    assert _dwell_entry_exists(tmp_path, warn)
    assert _state_of(tmp_path, warn) is None, "must still be dwelling, not firing"


def test_full_run_still_clears_dwell_for_an_absent_signature(tmp_path, pages):
    """No-regression guard on the default path: a full run looked everywhere,
    so an absent condition genuinely cleared and its counter must reset."""
    w_a = _flap_warn("admin_bot")
    w_b = _flap_warn("team_bot_a")
    dispatch_findings([w_a, w_b], tmp_path, config={}, dry_run=False)
    assert _dwell_entry_exists(tmp_path, w_b)

    dispatch_findings([w_a], tmp_path, config={}, dry_run=False)

    assert not _dwell_entry_exists(tmp_path, w_b), (
        "a full run must still reset the dwell for a cleared condition"
    )


def test_partial_bot_run_leaves_out_of_coverage_dwell_intact(tmp_path, pages):
    """`--bot admin_bot` never examined team_bot_a, so team_bot_a's absence
    from the keep-set is ignorance, not evidence that its condition cleared."""
    w_a = _flap_warn("admin_bot")
    w_b = _flap_warn("team_bot_a")
    dispatch_findings([w_a, w_b], tmp_path, config={}, dry_run=False)

    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    dispatch_findings([w_a], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _dwell_entry_exists(tmp_path, w_b), (
        "a --bot run must not reset another bot's dwell counter"
    )


def test_partial_category_run_leaves_out_of_coverage_dwell_intact(tmp_path, pages):
    """Same for the other scope dimension — narrowing either one is enough to
    make the run partial, and the narrowing applies on either."""
    w_a = _flap_warn("admin_bot")
    w_b = _flap_warn("team_bot_a")
    dispatch_findings([w_a, w_b], tmp_path, config={}, dry_run=False)

    types, bot_ids = _sweep_scope(category="identity", bot=None)
    dispatch_findings([], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _dwell_entry_exists(tmp_path, w_a)
    assert _dwell_entry_exists(tmp_path, w_b)


def test_partial_run_does_not_restart_an_out_of_coverage_dwell(tmp_path, pages):
    """The consequence that makes this worth fixing, end to end.

    A condition one cycle into its dwell, an operator ad-hoc `--bot` run in
    between, then the next full run. The dwell must complete on that run —
    pre-fix the partial run reset the counter, so the condition restarted at
    1 and its page slipped a whole cadence.
    """
    w_a = _flap_warn("admin_bot")
    w_b = _flap_warn("team_bot_a")

    # Run 1 (full): both conditions observed once — dwelling, nothing fires.
    dispatch_findings([w_a, w_b], tmp_path, config={}, dry_run=False)
    assert _state_of(tmp_path, w_b) is None

    # Run 2 (operator, --bot admin_bot): team_bot_a never examined.
    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    dispatch_findings([w_a], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    # Run 3 (full): this is team_bot_a's SECOND consecutive observation, so
    # the dwell is met and the Signal fires.
    dispatch_findings([w_a, w_b], tmp_path, config={}, dry_run=False)

    assert _state_of(tmp_path, w_b) == "firing", (
        "the partial run clobbered the dwell counter, delaying a real page"
    )


def test_partial_run_still_dwells_conditions_inside_its_coverage(tmp_path, pages):
    """Narrowing must not turn a partial run into a no-op for the dwell: it
    still OBSERVES what it examined, so a condition inside its coverage keeps
    accumulating consecutive cycles and promotes on schedule."""
    w_a = _flap_warn("admin_bot")

    dispatch_findings([w_a], tmp_path, config={}, dry_run=False)
    assert _state_of(tmp_path, w_a) is None  # cycle 1 — dwelling

    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    dispatch_findings([w_a], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _state_of(tmp_path, w_a) == "firing"  # cycle 2 — dwell met


def test_audit_signature_format_is_the_narrowing_contract(tmp_path, pages):
    """The dwell clear-sweep narrows by parsing the ledger's ``signature``, so
    ``_audit_signature``'s FORMAT is load-bearing, not an internal detail.

    Pinned here because a reformat would otherwise break the narrowing
    silently: _sweep_covers_signature fails toward not clearing, so every
    dwell reset would just stop happening with nothing going red.
    """
    w = _flap_warn("admin_bot")
    dispatch_findings([w], tmp_path, config={}, dry_run=False)

    stored = json.loads(
        flap_gate._pending_path(tmp_path, _audit_signature(w)).read_text()
    )["signature"]
    # Deliberately an unlimited split where the predicate uses split(":", 3):
    # the predicate must tolerate a digest field, this must prove there ISN'T
    # one beyond it. Do not "fix" the mismatch — it blinds the test.
    parts = stored.split(":")
    assert len(parts) == 4, f"expected producer:category:scope:digest, got {stored!r}"
    assert parts[:3] == ["audit", "config", "admin_bot"]

    # The message is hashed INTO the digest, so no message content can add a
    # field — this is what makes a plain split safe. Note the guarantee covers
    # the MESSAGE only: bot_id is interpolated verbatim, and _sweep_covers_
    # signature documents that (fail-safe) divergence.
    colon_msg = _flap_warn("admin_bot")
    colon_msg.message = "a: b: c: group-readable"
    assert len(_audit_signature(colon_msg).split(":")) == 4

    # Pod-scope findings use the literal "pod" in the bot position.
    pod = Finding(level="warn", category="machine", bot_id=None,
                  message="shared dir is group-readable", detail="mode 0750")
    assert _audit_signature(pod).split(":")[2] == "pod"


def test_sweep_covers_signature_mirrors_the_store_filters():
    """Unit coverage of the predicate, including the two fail-safe cases."""
    ident = "audit:identity:admin_bot:0123456789abcdef"
    config_b = "audit:config:team_bot_a:0123456789abcdef"
    pod = "audit:machine:pod:0123456789abcdef"

    # Full run: everything covered.
    assert audit._sweep_covers_signature(ident, None, None)
    assert audit._sweep_covers_signature(pod, None, None)

    # --bot admin_bot: that bot yes, another bot no, pod scope no (a --bot
    # run's pod-wide coverage is partial too) — matching sweep_resolve, which
    # drops a Signal whose bot_id is None once bot_ids is set.
    assert audit._sweep_covers_signature(ident, None, {"admin_bot"})
    assert not audit._sweep_covers_signature(config_b, None, {"admin_bot"})
    assert not audit._sweep_covers_signature(pod, None, {"admin_bot"})

    # --category identity.
    assert audit._sweep_covers_signature(ident, {"audit_identity"}, None)
    assert not audit._sweep_covers_signature(config_b, {"audit_identity"}, None)

    # Fail toward NOT clearing on anything unrecognised.
    assert not audit._sweep_covers_signature("pod_perms_drift:x:y", None, {"admin_bot"})
    assert not audit._sweep_covers_signature("garbage", None, None)


def test_partial_bot_run_still_clears_dwell_inside_its_own_coverage(tmp_path, pages):
    """The narrowing must not become a blanket skip: a --bot run DID examine
    that bot, so its own cleared condition still resets, exactly as before.

    This is the case an all-or-nothing skip would regress — and for the
    identity family, whose CRITICAL shape dwells by design (R-2), regressing
    it re-pages the single-cycle edit-vs-backup blip that R-2 exists to
    silence."""
    w_a1 = _flap_warn("admin_bot")
    w_a2 = _flap_warn("admin_bot")
    w_a2.message = "admin_bot (fs.config.perms_readable): openclaw.json is group-readable"
    dispatch_findings([w_a1, w_a2], tmp_path, config={}, dry_run=False)
    assert _dwell_entry_exists(tmp_path, w_a2)

    # --bot admin_bot: w_a2 is genuinely gone, and this run looked for it.
    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    dispatch_findings([w_a1], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert not _dwell_entry_exists(tmp_path, w_a2), (
        "a --bot run must still reset the dwell for its OWN cleared condition"
    )


def test_partial_bot_run_leaves_pod_scope_dwell_intact(tmp_path, pages):
    """A --bot run's pod-wide coverage is partial too (audit_process sees one
    bot's processes), so pod-scope dwell state is not its to clear — the same
    line sweep_resolve draws for pod-scope Signals."""
    w_bot = _flap_warn("admin_bot")
    w_pod = Finding(level="warn", category="machine", bot_id=None,
                    message="shared dir is group-readable",
                    detail="mode 0750 (group-class bit)")
    dispatch_findings([w_bot, w_pod], tmp_path, config={}, dry_run=False)
    assert _dwell_entry_exists(tmp_path, w_pod)

    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    dispatch_findings([w_bot], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert _dwell_entry_exists(tmp_path, w_pod)


def test_partial_run_leaves_other_producers_dwell_entries_alone(tmp_path, pages):
    """The pending dir is shared. type_filter already keeps audit off
    pod_perms_drift / acl_drift counters; the signature predicate must not
    quietly widen that — a foreign signature fails the parse and is skipped."""
    w = _flap_warn("admin_bot")
    dispatch_findings([w], tmp_path, config={}, dry_run=False)

    foreign_sig = "pod_perms_drift:admin_bot:abc"
    flap_gate.note_observed(tmp_path, signature=foreign_sig, transient=True,
                            type="pod_perms_drift")
    assert flap_gate._pending_path(tmp_path, foreign_sig).exists()

    types, bot_ids = _sweep_scope(category=None, bot="admin_bot")
    dispatch_findings([], tmp_path, config={}, dry_run=False,
                      sweep_types=types, sweep_bot_ids=bot_ids)

    assert flap_gate._pending_path(tmp_path, foreign_sig).exists()


def test_signature_parsing_divergences_fail_toward_retaining():
    """Two inputs are read out of the signature rather than off a field, so
    they can diverge from what store.sweep_resolve would decide. Both must
    diverge toward RETAINING a dwell entry — a wrongly cleared one delays a
    real page, a wrongly retained one cannot cause a missed one.

    Documented on _sweep_covers_signature; pinned here so the claim is checked
    rather than asserted.
    """
    # A bot literally named "pod" is indistinguishable from the pod-scope
    # sentinel. sweep_resolve would cover it (bot_id == "pod" is in the set);
    # the predicate reads it as pod-scope and declines.
    assert not audit._sweep_covers_signature(
        "audit:config:pod:0123456789abcdef", None, {"pod"}
    )

    # bot_id is interpolated verbatim (only the MESSAGE is hashed), so a colon
    # in it shifts the split. macOS usernames admit no colon, so unreachable —
    # but it declines rather than mismatching.
    assert not audit._sweep_covers_signature(
        "audit:config:a:b:0123456789abcdef", None, {"a:b"}
    )

    # The upstream collision this sits downstream of: three distinct bot_id
    # values mint one signature. Pre-existing in _audit_signature, recorded
    # here so the next reader meets it.
    def sig(bot_id):
        return _audit_signature(Finding(level="warn", category="config",
                                        bot_id=bot_id, message="m", detail=""))
    assert sig(None) == sig("") == sig("pod")
