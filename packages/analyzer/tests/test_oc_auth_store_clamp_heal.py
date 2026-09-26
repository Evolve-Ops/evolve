"""Inline ACL-clamp heal at the credential read (#3477).

The live shape being pinned (reference VPS pod, 2026-07-29): the OC gateway
re-hardened ``~/.openclaw/agents/main/agent`` to 0700 on an auth write, which on
Linux recalculated the POSIX-ACL mask to ``---`` and capped evolve's inherited
``user:evolve:r-x`` ACE. Every rung of the credential ladder then hit EACCES and
reported the store as ABSENT — ``resolve_infra_llm()`` returned ``None`` and
every engine LLM feature said "no provider credentialed" — for ~52 minutes,
until the next hourly ``pod_perms_drift_monitor`` tick re-widened the mask. No
Signal, no trace.

The fixtures here reproduce the *observable* half of that with a real
``chmod 000`` on the agent dir: an unprivileged process gets EACCES on exactly
the same syscalls (``lstat`` of the db, ``read_text`` of the legacy json,
``iterdir`` for the ``.bak`` snapshots), which is what the reader keys on. The
POSIX-ACL mask itself is Linux-only, so the mask mechanics are covered by
``secret_config_perms``' own tests; what needs pinning HERE is the reader's
reaction: heal once, retry, and tell "self-healed" apart from "still dark".

The four contracts:
  1. clamped → healed → key found: reader returns the key, heal called EXACTLY
     once, and NO Signal (a self-healed clamp must stay silent — that silence is
     the anti-flap design the hourly monitor deliberately adopted).
  2. clamped → heal fails → reader returns ``None`` AND a ``credential_access``
     Signal fires, which the hourly sweeper later clears.
  3. throttle: repeated EACCES in one process does not spawn repeated heals.
  4. regression pin: an unclamped pod behaves byte-identically — no heal call,
     no Signal, no extra syscalls the old code did not make.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import credential_access_signal as cred_signal  # noqa: E402
import oc_auth_store  # noqa: E402
from signals import store as signals_store  # noqa: E402

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses mode bits, so a chmod-000 dir cannot produce EACCES",
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_process_globals():
    """Tear down the module's process-global seams.

    ``set_heal_fn`` / ``_HEAL_STATE`` are process-wide; a leak of exactly this
    kind (a seam left installed by one test) has red-ed CI in this arc before.
    Restore unconditionally, before AND after, so ordering can't matter.
    """
    oc_auth_store.set_heal_fn(None)
    oc_auth_store.reset_heal_throttle()
    yield
    oc_auth_store.set_heal_fn(None)
    oc_auth_store.reset_heal_throttle()


@pytest.fixture(autouse=True)
def _unclamp_on_teardown():
    """Restore modes on any dir a test clamped, so tmp_path cleanup can run."""
    clamped: list[Path] = []
    yield clamped
    for p in clamped:
        with contextlib.suppress(OSError):
            p.chmod(0o700)


@pytest.fixture(autouse=True)
def _no_sudo(monkeypatch):
    """Neutralize the reader's root fallbacks.

    ``_read_text_file`` shells out to ``sudo /bin/cat`` on EACCES and
    ``_read_sqlite_store`` to ``sudo sqlite3 -readonly``. Neither may run in a
    test (no TTY, and on a dev box it would prompt), and for these fixtures both
    would mask the very EACCES we are pinning.
    """
    def _refuse(*_a, **_kw):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "sudo disabled in tests"
        return _R()

    monkeypatch.setattr(oc_auth_store.subprocess, "run", _refuse)


PROFILES = json.dumps(
    {
        "version": 1,
        "profiles": {
            "anthropic:api_key": {
                "type": "api_key",
                "provider": "anthropic",
                "key": "sk-ant-engine",
            }
        },
    }
)


def _make_bot(home: Path) -> Path:
    """A bot home with a populated per-agent sqlite auth store."""
    agent = home / ".openclaw" / "agents" / "main" / "agent"
    agent.mkdir(parents=True)
    db = agent / "openclaw-agent.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE auth_profile_store ("
            "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auth_profile_store VALUES (?, ?, ?)",
            ("primary", PROFILES, 1_700_000_000),
        )
        conn.commit()
    finally:
        conn.close()
    return agent


def _shared_dir(tmp_path: Path) -> Path:
    shared = tmp_path / "shared"
    for sub in ("firing", "snoozed", "archived", "log"):
        (shared / "signals" / sub).mkdir(parents=True)
    return shared


def _firing(shared: Path) -> list:
    return [
        s
        for s in signals_store.iter_active(shared, producer=cred_signal.PRODUCER)
        if s.type == cred_signal.SIGNAL_TYPE
    ]


# ── 1. clamped → healed → key found (success, silent) ─────────────────────────


def test_clamp_heals_inline_and_returns_the_key(tmp_path, _unclamp_on_teardown, caplog):
    home = tmp_path / "evo"
    agent = _make_bot(home)
    shared = _shared_dir(tmp_path)
    network = {"sharedDir": str(shared)}

    agent.chmod(0o000)
    _unclamp_on_teardown.append(agent)

    heals: list[tuple[str, str]] = []

    def _heal(bot_id, bot_user):
        heals.append((bot_id, bot_user))
        agent.chmod(0o700)          # what setfacl -m m::rwX achieves on Linux
        return True

    oc_auth_store.set_heal_fn(_heal)

    with caplog.at_level("WARNING", logger="evolve.oc_store"):
        raw = oc_auth_store.read_auth_store("evo", home=home, network=network)

    # The key is found — NOT reported absent.
    assert raw is not None
    assert json.loads(raw)["profiles"]["anthropic:api_key"]["key"] == "sk-ant-engine"
    # Exactly one heal, targeting (bot_id, bot_user).
    assert heals == [("evo", "evo")]
    # Self-healed → NO Signal (the anti-flap contract).
    assert _firing(shared) == []
    # …but a durable trace exists, naming the clamp and the heal outcome.
    trace = "\n".join(r.getMessage() for r in caplog.records)
    assert "ACL-clamped" in trace
    assert "re-read SUCCEEDED" in trace
    assert str(agent) in trace or "openclaw-agent.sqlite" in trace


def test_clamp_heal_also_covers_the_iterator_entry_point(
    tmp_path, _unclamp_on_teardown
):
    """``iter_auth_store_payloads`` is the engine (primary_bot) entry point —
    it must heal too, not just ``read_auth_store``."""
    home = tmp_path / "evo"
    agent = _make_bot(home)
    agent.chmod(0o000)
    _unclamp_on_teardown.append(agent)

    calls = []

    def _heal(bot_id, bot_user):
        calls.append(bot_id)
        agent.chmod(0o700)
        return True

    oc_auth_store.set_heal_fn(_heal)
    payloads = list(
        oc_auth_store.iter_auth_store_payloads(
            "evo", home=home, network={"sharedDir": str(tmp_path / "nope")}
        )
    )
    assert len(payloads) == 1
    assert json.loads(payloads[0])["profiles"]["anthropic:api_key"]["key"] == (
        "sk-ant-engine"
    )
    assert calls == ["evo"]


# ── 2. clamped → heal fails → None + Signal, clearing later ───────────────────


def test_heal_failure_fires_a_signal_and_returns_none(
    tmp_path, _unclamp_on_teardown
):
    home = tmp_path / "evo"
    agent = _make_bot(home)
    shared = _shared_dir(tmp_path)
    network = {"sharedDir": str(shared)}

    agent.chmod(0o000)
    _unclamp_on_teardown.append(agent)

    oc_auth_store.set_heal_fn(lambda b, u: False)   # heal ran, did not restore

    raw = oc_auth_store.read_auth_store("evo", home=home, network=network)
    assert raw is None

    firing = _firing(shared)
    assert len(firing) == 1
    sig = firing[0]
    assert sig.producer == "credential_access"
    assert sig.type == "credential_store_unreadable"
    assert sig.bot_id == "evo"
    assert sig.severity == "alert"        # PRODUCER_SEVERITY default
    assert sig.category == "platform"     # PRODUCER_CATEGORY_DEFAULT
    assert sig.signature == cred_signal.signature_for("evo")
    # The operator gets the one-command fix, and the clamped path.
    assert "ensure-pod-perms" in sig.body
    assert any("agent" in p for p in sig.details["clamped_paths"])


def test_signal_clears_once_access_is_restored(tmp_path, _unclamp_on_teardown):
    """The alert must be able to auto-clear — the hourly sweeper's job."""
    home = tmp_path / "evo"
    agent = _make_bot(home)
    shared = _shared_dir(tmp_path)
    network = {"sharedDir": str(shared)}

    agent.chmod(0o000)
    _unclamp_on_teardown.append(agent)
    oc_auth_store.set_heal_fn(lambda b, u: False)
    assert oc_auth_store.read_auth_store("evo", home=home, network=network) is None
    assert len(_firing(shared)) == 1

    # A run that evaluated NO bots must resolve nothing — an empty keep-set with
    # no scope would otherwise mass-clear the pod's alerts.
    assert cred_signal.sweep_resolve_healthy(
        shared, checked_bots=set(), still_unreadable_bots=set()
    ) == 0
    assert len(_firing(shared)) == 1

    # A later hourly tick finds the contract holding for every bot it checked.
    n = cred_signal.sweep_resolve_healthy(
        shared, checked_bots={"evo"}, still_unreadable_bots=set()
    )
    assert n == 1
    assert _firing(shared) == []

    # And a bot STILL locked out keeps its alert.
    oc_auth_store.reset_heal_throttle()
    assert oc_auth_store.read_auth_store("evo", home=home, network=network) is None
    assert len(_firing(shared)) == 1
    assert cred_signal.sweep_resolve_healthy(
        shared, checked_bots={"evo"}, still_unreadable_bots={"evo"}
    ) == 0
    assert len(_firing(shared)) == 1
    # A sweep that checked a DIFFERENT bot must not touch this one either.
    assert cred_signal.sweep_resolve_healthy(
        shared, checked_bots={"darwin"}, still_unreadable_bots=set()
    ) == 0
    assert len(_firing(shared)) == 1


# ── 3. throttle ───────────────────────────────────────────────────────────────


def test_repeated_eacces_heals_once_per_process(tmp_path, _unclamp_on_teardown):
    home = tmp_path / "evo"
    agent = _make_bot(home)
    shared = _shared_dir(tmp_path)
    network = {"sharedDir": str(shared)}

    agent.chmod(0o000)
    _unclamp_on_teardown.append(agent)

    heals: list[str] = []
    oc_auth_store.set_heal_fn(lambda b, u: heals.append(b) or False)

    for _ in range(5):
        assert oc_auth_store.read_auth_store("evo", home=home, network=network) is None

    assert heals == ["evo"], "the throttle must permit exactly one heal per bot"
    # Deduped into a single Signal, not five.
    assert len(_firing(shared)) == 1


def test_throttle_is_per_account_not_global(tmp_path, _unclamp_on_teardown):
    """A pod-wide sweep must still heal bot 2 after healing bot 1."""
    shared = _shared_dir(tmp_path)
    network = {"sharedDir": str(shared)}
    heals: list[str] = []
    oc_auth_store.set_heal_fn(lambda b, u: heals.append(b) or False)

    for bot in ("alpha", "beta"):
        home = tmp_path / bot
        agent = _make_bot(home)
        agent.chmod(0o000)
        _unclamp_on_teardown.append(agent)
        assert oc_auth_store.read_auth_store(bot, home=home, network=network) is None

    assert heals == ["alpha", "beta"]


# ── 4. regression pin: an unclamped pod is untouched ──────────────────────────


def test_unclamped_pod_never_heals_and_never_signals(tmp_path):
    home = tmp_path / "evo"
    _make_bot(home)
    shared = _shared_dir(tmp_path)
    network = {"sharedDir": str(shared)}

    heals: list[str] = []
    oc_auth_store.set_heal_fn(lambda b, u: heals.append(b) or True)

    raw = oc_auth_store.read_auth_store("evo", home=home, network=network)
    assert raw is not None
    assert oc_auth_store.read_anthropic_key(
        "evo", home=home, network=network
    ) == "sk-ant-engine"
    assert heals == []
    assert _firing(shared) == []


def test_absent_store_is_not_a_clamp(tmp_path):
    """A keyless bot (no auth store at all) records no EACCES, so it must not
    trigger a privileged repair — "absent" and "locked out" stay distinct."""
    home = tmp_path / "keyless"
    (home / ".openclaw" / "agents" / "main" / "agent").mkdir(parents=True)
    shared = _shared_dir(tmp_path)
    network = {"sharedDir": str(shared)}

    heals: list[str] = []
    oc_auth_store.set_heal_fn(lambda b, u: heals.append(b) or True)

    assert oc_auth_store.read_auth_store(
        "keyless", home=home, network=network
    ) is None
    assert heals == []
    assert _firing(shared) == []


def test_enoent_is_not_recorded_as_a_clamp(tmp_path):
    """``_note_eacces`` must ignore every errno except EACCES/EPERM — an ENOENT
    is a real "not here" answer, not a lockout."""
    clamp: dict = {}
    oc_auth_store._note_eacces(clamp, tmp_path / "gone", FileNotFoundError(2, "nope"))
    assert oc_auth_store._clamped_paths(clamp) == []
    oc_auth_store._note_eacces(clamp, tmp_path / "denied", PermissionError(13, "nope"))
    assert oc_auth_store._clamped_paths(clamp) == [str(tmp_path / "denied")]
    # Deduped — one path recorded once even if several rungs hit it.
    oc_auth_store._note_eacces(clamp, tmp_path / "denied", PermissionError(13, "nope"))
    assert len(oc_auth_store._clamped_paths(clamp)) == 1


def test_clamp_with_no_resolvable_account_does_not_heal(tmp_path, _unclamp_on_teardown):
    """No bot_id and no user → nothing to hand ``heal_evolve_access``; the read
    must degrade exactly as it did before rather than guess an account."""
    home = tmp_path / "evo"
    agent = _make_bot(home)
    agent.chmod(0o000)
    _unclamp_on_teardown.append(agent)

    heals: list[str] = []
    oc_auth_store.set_heal_fn(lambda b, u: heals.append(b) or True)

    assert oc_auth_store.read_auth_store(None, home=home, network={}) is None
    assert heals == []


def test_clamped_dir_mode_precondition(tmp_path, _unclamp_on_teardown):
    """Guard the fixture itself: if a future platform/CI image stopped denying
    these syscalls, every clamp test above would silently pass for the wrong
    reason. Assert the EACCES is real."""
    home = tmp_path / "evo"
    agent = _make_bot(home)
    agent.chmod(0o000)
    _unclamp_on_teardown.append(agent)

    assert stat.S_IMODE(agent.stat().st_mode) == 0o000
    with pytest.raises(PermissionError):
        os.lstat(agent / "openclaw-agent.sqlite")
    clamp: dict = {}
    assert list(oc_auth_store._iter_payloads_for_home(home, clamp)) == []
    assert oc_auth_store._clamped_paths(clamp), "no EACCES recorded by the ladder"
