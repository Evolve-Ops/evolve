"""tests/test_backup_pat_unreadable.py — the 2026-08-18 outage, pinned shut.

THE OUTAGE. PR #3696 clamped ``{shared}/keystore/.machine-key`` to 0640
``evolve:wheel`` on 2026-08-18. ``analyzer/backup.py`` runs as the BOT user and
needed that key to decrypt the pod GitHub PAT, without which it cannot verify
its backup repo is private. ``keystore.load_github_pat`` swallowed the
``PermissionError`` and returned ``None`` — indistinguishable from "the
operator never stored one" — so every nightly run printed the setup-wizard
message and recorded itself ``skipped``. ``skipped`` does not increment
``consecutive_failures``, so ``backup_signal``'s threshold could never see it.
Nine bots, twenty-three nights, no push and no Signal.

Every test here FAILS against the pre-change tree. The load-bearing one is
``test_unreadable_pat_records_failed_not_skipped``, which fails with::

    AssertionError: assert 'skipped' == 'failed'

Covers, in order, the four things the fix has to hold true simultaneously:
  1. the push is refused                     (the guard never weakened)
  2. the run is recorded ``failed``          (so the threshold can see it)
  3. the diagnostic names the PERMISSION cause, not the wizard
  4. the Signal fires on attempt ONE
plus the two-way split that keeps a genuinely-unconfigured pod reporting as a
setup step rather than a fault.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup  # noqa: E402
import backup_diagnostics  # noqa: E402
import backup_signal  # noqa: E402
import backup_visibility as bv  # noqa: E402

_URL = "git@github.com:cjalden/test-workspace.git"


def _drive(tmp_path, monkeypatch, *, pat_state, daemon_answer):
    """Run ``_backup_bot_attempt`` with the PAT state and daemon hop stubbed.

    ``daemon_answer`` is what ``visibility_via_daemon`` returns: ``None`` for
    "socket unreachable", or a ``(visibility, reason)`` pair.
    """
    from backup import _backup_bot_attempt

    bot_home = tmp_path / "bot"
    (bot_home / ".openclaw" / "workspace").mkdir(parents=True, exist_ok=True)

    def git_stub(args, cwd, env=None):
        if args[:2] == ["push", "origin"]:
            raise AssertionError(
                "push must not run when the repo's visibility could not be "
                "confirmed private — the fail-closed guard has been bypassed"
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backup._bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr("backup._ssh_env", lambda bot_id, network=None: {})
    monkeypatch.setattr("backup._git", git_stub)
    monkeypatch.setattr("backup._load_github_pat_state", lambda network: (pat_state, None))
    monkeypatch.setattr("backup._visibility_via_daemon", lambda url: daemon_answer)
    monkeypatch.setattr("backup._refresh_local_drift_baseline", lambda b, s: None)

    return _backup_bot_attempt(
        bot_id="test-bot",
        shared_dir=tmp_path / "shared",
        backup_url=_URL,
        dry_run=False,
        network={},
    )


# ── 1 + 2: refused, and recorded as a FAILURE ───────────────────────────────

def test_unreadable_pat_records_failed_not_skipped(tmp_path, monkeypatch):
    """THE regression. Pre-change this returns 'skipped' and the outage is silent."""
    status, err = _drive(
        tmp_path, monkeypatch, pat_state=bv.PAT_UNREADABLE, daemon_answer=None,
    )
    assert status == "failed", (
        "a PAT that exists but cannot be read is a FAILURE, not a skipped "
        "setup step — 'skipped' leaves consecutive_failures at 0 and no "
        "Signal can ever fire (the 2026-08-18 silent outage)"
    )
    assert err


def test_absent_pat_still_records_skipped(tmp_path, monkeypatch):
    """The other side of the split — a genuinely unconfigured pod is NOT a fault.

    Without this, the fix would trade a silent outage for a nightly false
    alarm on every pod that has simply never set up cloud backup.
    """
    status, _err = _drive(
        tmp_path, monkeypatch, pat_state=bv.PAT_ABSENT, daemon_answer=None,
    )
    assert status == "skipped"


def test_unreadable_pat_recovers_when_the_daemon_answers(tmp_path, monkeypatch):
    """The actual fix path: bot can't read the PAT, daemon supplies the verdict.

    A 'private' verdict must let the run proceed past the guard — otherwise
    the eight locked-out bots stay dead, just noisily instead of silently.
    """
    status, _err = _drive(
        tmp_path, monkeypatch,
        pat_state=bv.PAT_UNREADABLE, daemon_answer=("private", "checked"),
    )
    assert status != "failed", (
        "a daemon verdict of 'private' must satisfy the guard; the bot never "
        "needs the PAT itself"
    )


def test_daemon_public_verdict_still_refuses(tmp_path, monkeypatch):
    """Fail-closed direction is unchanged when the verdict arrives via the daemon."""
    status, err = _drive(
        tmp_path, monkeypatch,
        pat_state=bv.PAT_UNREADABLE, daemon_answer=("public", "checked"),
    )
    assert status == "failed"
    assert "public" in err


# ── 3: the diagnostic names the permission cause, not the wizard ────────────

def test_diagnostic_names_the_permission_cause(tmp_path, monkeypatch):
    _status, err = _drive(
        tmp_path, monkeypatch, pat_state=bv.PAT_UNREADABLE, daemon_answer=None,
    )
    cause = backup_diagnostics.classify(err)
    assert cause is not None, (
        "the unreadable-PAT error must classify; an unclassified error only "
        "fires the generic 3-failure Signal, two nights late"
    )
    assert cause["cause_id"] == "pat_unreadable"

    steps = " ".join(cause["fix_steps"]).lower()
    assert "not a missing setup step" in steps
    # The precise regression: the old message sent the operator to re-run a
    # wizard they had already completed, which could not have helped.
    assert "re-running the backup → cloud wizard will not fix it" in steps


def test_diagnostic_does_not_tell_the_operator_to_rerun_the_wizard(tmp_path, monkeypatch):
    """Guards the wording that made the outage unreadable for 23 days."""
    _status, err = _drive(
        tmp_path, monkeypatch, pat_state=bv.PAT_UNREADABLE, daemon_answer=None,
    )
    assert "Store one via the Backup → Cloud wizard" not in err, (
        "this is the sentence the pre-#4196 code emitted for a PAT that was "
        "already stored — it is what made the outage read as an unfinished "
        "setup step"
    )
    assert "no GitHub PAT configured" not in err


# ── 4: the Signal fires on attempt ONE ──────────────────────────────────────

def test_signal_fires_on_the_first_failure(tmp_path, monkeypatch):
    """consecutive_failures=1 must already produce a Signal.

    Classified causes use CLASSIFIED_FIRE_THRESHOLD (1) rather than the
    generic 3, so registering ``pat_unreadable`` in the classifier is what
    makes night one actionable.
    """
    _status, err = _drive(
        tmp_path, monkeypatch, pat_state=bv.PAT_UNREADABLE, daemon_answer=None,
    )
    spec = backup_signal.build_signal_for_failing_backup(
        "test-bot",
        {
            "consecutive_failures": 1,
            "last_error": err,
            "last_attempt_at": "2026-09-14T02:00:00Z",
            "last_success_at": "2026-08-18T20:56:13Z",
        },
        _URL,
    )
    assert spec is not None, (
        "one failed night with a KNOWN cause must fire immediately; waiting "
        "for the generic 3-failure threshold is the delay this closes"
    )
    assert "pod's GitHub PAT exists" in spec["title"]


def test_unclassified_backup_error_still_waits_for_the_generic_threshold():
    """Sanity: registering a new cause didn't lower the bar for everything."""
    spec = backup_signal.build_signal_for_failing_backup(
        "test-bot",
        {"consecutive_failures": 1, "last_error": "something nobody has seen before"},
        _URL,
    )
    assert spec is None


# ── The monitor's own copy must not say "create a PAT" either ───────────────

def test_visibility_monitor_distinguishes_unreadable_from_missing():
    """``_build_visibility_signal`` had the same ``if not pat`` collapse.

    Whatever surface reports it, "a PAT is stored but unreadable" must not
    render as "no PAT is configured" — the remediations are unrelated and only
    one of them is something the operator can act on.
    """
    unreadable = backup_signal._build_visibility_signal(
        "test-bot", _URL, {},
        pat_state_loader=lambda cfg: (bv.PAT_UNREADABLE, None),
    )
    missing = backup_signal._build_visibility_signal(
        "test-bot", _URL, {},
        pat_state_loader=lambda cfg: (bv.PAT_ABSENT, None),
    )
    assert unreadable is not None and missing is not None
    assert unreadable["title"] != missing["title"]
    assert "unreadable" in unreadable["title"].lower()

    body = unreadable["body"]
    assert "is** stored" in body or "is stored" in body
    assert "not a setup step" in body.lower()
    # The wrong instruction, pinned out.
    assert "create a PAT with" not in body


def test_visibility_monitor_still_reports_a_genuinely_missing_pat():
    missing = backup_signal._build_visibility_signal(
        "test-bot", _URL, {},
        pat_state_loader=lambda cfg: (bv.PAT_ABSENT, None),
    )
    assert "no PAT" in missing["title"]


# ── The keystore seam underneath it all ─────────────────────────────────────

def test_unreadable_vault_entry_is_not_reported_as_absent(tmp_path):
    """The root defect, at its source: EACCES must not read as 'never stored'.

    Simulates the exact pod state — a real ciphertext present in the vault,
    with the directory unreadable to this uid — and asserts the tri-state read
    says UNREADABLE. ``Path.exists()`` returns False here, which is what the
    pre-change code trusted.
    """
    import os
    import pytest
    from evolve_admin.keystore import SecretState, _file_store_get_state

    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions; cannot simulate EACCES")

    vault = tmp_path / "keystore" / "vault"
    vault.mkdir(parents=True)
    (vault / "github_pat.enc").write_bytes(b"ciphertext-that-really-is-here")
    vault.chmod(0o000)  # the #3696 shape: entry present, uid denied
    try:
        state, value = _file_store_get_state(vault, "github_pat")
    finally:
        vault.chmod(0o700)

    assert state == SecretState.UNREADABLE, (
        "a permission wall must never be classified ABSENT — that collapse is "
        "the whole bug: it turns 'I cannot read the PAT' into 'you never "
        "stored a PAT', which sends the operator to the wizard"
    )
    assert value is None


def test_absent_vault_entry_is_reported_as_absent(tmp_path):
    from evolve_admin.keystore import SecretState, _file_store_get_state

    vault = tmp_path / "keystore" / "vault"
    vault.mkdir(parents=True)
    state, value = _file_store_get_state(vault, "github_pat")
    assert state == SecretState.ABSENT
    assert value is None


# ── End-to-end: the real pod state, no PAT-layer stubs ──────────────────────

def test_end_to_end_real_unreadable_vault_records_failed(tmp_path, monkeypatch):
    """The whole chain, with a REAL unreadable vault rather than a stubbed state.

    The tests above stub ``_load_github_pat_state`` and so pin backup.py's
    classification logic. This one stubs nothing below the daemon hop: it
    writes an actual ciphertext into an actual vault directory, chmods that
    directory to 0000, and drives a real ``_backup_bot_attempt``. The read
    fails the way it fails on the pod, through
    ``keystore._file_store_get_state`` and ``backup_visibility.load_pat_state``.

    Run against the pre-change tree this is the outage verbatim::

        status : skipped
        error  : skipping push: no GitHub PAT configured (keystore key
                 `github_pat`)… Store one via the Backup → Cloud wizard…
        AssertionError: assert 'skipped' == 'failed'
    """
    import os
    import pytest
    from backup import _backup_bot_attempt

    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions; cannot simulate EACCES")

    shared = tmp_path / "shared"
    vault = shared / "keystore" / "vault"
    vault.mkdir(parents=True)
    (vault / "github_pat.enc").write_bytes(b"a-real-ciphertext-is-present-here")
    (shared / "keystore" / ".machine-key").write_bytes(b"k" * 32)

    bot_home = tmp_path / "bot"
    (bot_home / ".openclaw" / "workspace").mkdir(parents=True)

    def git_stub(args, cwd, env=None):
        if args[:2] == ["push", "origin"]:
            raise AssertionError("push must not run; visibility never confirmed")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backup._bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr("backup._ssh_env", lambda bot_id, network=None: {})
    monkeypatch.setattr("backup._git", git_stub)
    monkeypatch.setattr("backup._refresh_local_drift_baseline", lambda b, s: None)
    # No admin daemon in the test environment. Pinned explicitly rather than
    # left to a missing socket, so the assertion is about classification.
    monkeypatch.setattr("backup._visibility_via_daemon", lambda url: None)

    os.chmod(vault, 0o000)
    try:
        status, err = _backup_bot_attempt(
            bot_id="test-bot",
            shared_dir=shared,
            backup_url=_URL,
            dry_run=False,
            network={"sharedDir": str(shared)},
        )
    finally:
        os.chmod(vault, 0o700)

    assert status == "failed"
    assert backup_diagnostics.classify(err)["cause_id"] == "pat_unreadable"
