"""tests/test_heal_drift_admin_ui_aware.py — drift detector credits admin-UI writes.

Before #1734, every admin-UI config write (Reconcile catalog, Seed
defaults, AI Optimization edits, Routing/Fallback editors) tripped
"Unexplained config drift" alerts the moment the next heal cycle ran —
because the drift detector only knew about the proposal pipeline as a
legitimate writer. Operators saw a wave of 5 false-positive alerts after
a single Reconcile-all sweep.

#1734 added a translation map (audit action string → top-level openclaw
keys) inside heal.py. The follow-up structural refactor replaced that
with a writer-self-declared `oc_keys` field on the audit log entry, so
adding a new write endpoint no longer requires updating a remote
table — and writers truly intend what they record.

Now `detect_backup_drift_keys` reads `audit-log.jsonl` and credits any
entry whose bot_id matches, whose timestamp is within the TTL window,
AND whose `oc_keys` field declares the openclaw keys it touched.

Coverage:
- Recent entry with oc_keys → keys credited
- Recent entry WITHOUT oc_keys (legacy / non-openclaw event) → no credit
- Old entry (past TTL) → not credited even if oc_keys present
- Different bot_id → not credited
- Missing audit log → empty set (silent no-op, doesn't crash)
- Corrupt JSON lines → skipped without crashing
- Both Z and +00:00 timestamp suffixes parsed
- End-to-end: drift detector returns [] when audit log explains it
- End-to-end: drift detector still returns the key for true off-path
  drift (no audit entry, no apply-result)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402
from heal import (  # noqa: E402
    _ADMIN_UI_DRIFT_TTL_HOURS,
    _get_recent_admin_ui_writes,
    detect_backup_drift_keys,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_audit_log(shared_dir: Path, entries: list[dict]) -> None:
    """Append entries to shared_dir/audit-log.jsonl in the canonical shape."""
    shared_dir.mkdir(parents=True, exist_ok=True)
    audit_path = shared_dir / "audit-log.jsonl"
    with open(audit_path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _audit_entry(
    *, action: str, bot_id: str, hours_ago: float = 0,
    oc_keys: list[str] | None = None,
    details: dict | None = None,
) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    entry: dict = {
        "timestamp": ts.isoformat(),
        "action": action,
        "bot_id": bot_id,
        "details": details or {},
    }
    if oc_keys is not None:
        entry["oc_keys"] = oc_keys
    return entry


# ── _get_recent_admin_ui_writes ─────────────────────────────────────────────


def test_credits_recent_entry_with_oc_keys(tmp_path):
    """The exact scenario Pod_admin hit: operator clicked Reconcile on team_bot_a,
    audit log shows config.tiers.set 30s ago with oc_keys=['agents'] →
    drift detector should credit 'agents' as explained."""
    _write_audit_log(tmp_path, [
        _audit_entry(
            action="config.tiers.set", bot_id="team_bot_a", hours_ago=0.01,
            oc_keys=["agents"],
        ),
    ])
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert keys == {"agents"}


def test_does_not_credit_entry_without_oc_keys(tmp_path):
    """Entries without `oc_keys` (legacy entries from before the
    structural refactor, or non-openclaw events like 'login.success'
    or 'routing.update' which edits network.json) must NOT credit
    drift — they declare no openclaw-touching intent."""
    _write_audit_log(tmp_path, [
        # No oc_keys field at all
        _audit_entry(action="login.success", bot_id="team_bot_a", hours_ago=0.01),
        # Explicit empty list
        _audit_entry(
            action="routing.update", bot_id="team_bot_a", hours_ago=0.01,
            oc_keys=[],
        ),
    ])
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert keys == set(), (
        "entries lacking oc_keys must not credit drift — that's the whole "
        "point of writer self-declaration vs the old action-map approach"
    )


def test_credits_arbitrary_oc_keys_no_action_translation(tmp_path):
    """STRUCTURAL: a writer can declare ANY top-level key — there's no
    fixed allowlist of action names. If a future endpoint named
    'foo.bar.baz' writes to openclaw.json and declares oc_keys=['plugins'],
    the drift detector credits 'plugins' without any change to heal.py.
    That's the whole point of the refactor."""
    _write_audit_log(tmp_path, [
        _audit_entry(
            action="some.brand.new.action", bot_id="team_bot_a", hours_ago=0.01,
            oc_keys=["plugins"],
        ),
    ])
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert keys == {"plugins"}


def test_does_not_credit_entry_past_ttl(tmp_path):
    """TTL bound — an audit entry from 10 days ago shouldn't credit
    drift forever. Otherwise an operator who edited models a month ago
    would mask actual off-path drift today."""
    _write_audit_log(tmp_path, [
        _audit_entry(
            action="config.tiers.set", bot_id="team_bot_a",
            hours_ago=_ADMIN_UI_DRIFT_TTL_HOURS + 1,
            oc_keys=["agents"],
        ),
    ])
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert keys == set()


def test_does_not_credit_entry_for_other_bot(tmp_path):
    """team_bot_a's audit entry doesn't excuse drift on admin_bot."""
    _write_audit_log(tmp_path, [
        _audit_entry(
            action="config.tiers.set", bot_id="team_bot_a",
            oc_keys=["agents"],
        ),
    ])
    keys = _get_recent_admin_ui_writes("admin_bot", tmp_path)
    assert keys == set()


def test_unions_keys_across_multiple_entries(tmp_path):
    """Operator made several edits in sequence — credit each one's keys."""
    _write_audit_log(tmp_path, [
        _audit_entry(
            action="config.tiers.set", bot_id="team_bot_a",
            oc_keys=["agents"],
        ),
        _audit_entry(
            action="manifest.remediate", bot_id="team_bot_a",
            oc_keys=["agents", "plugins", "channels"],
        ),
    ])
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert "agents" in keys
    assert "plugins" in keys
    assert "channels" in keys


def test_missing_audit_log_returns_empty_set(tmp_path):
    """Fresh install / pre-audit-log shared_dir → silent empty result.
    Must NOT crash heal's drift check (which runs every 5 min)."""
    # tmp_path exists but has no audit-log.jsonl
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert keys == set()


def test_corrupt_audit_log_lines_are_skipped(tmp_path):
    """Malformed JSON lines in the middle shouldn't kill the read."""
    shared_dir = tmp_path
    shared_dir.mkdir(parents=True, exist_ok=True)
    audit_path = shared_dir / "audit-log.jsonl"
    valid = _audit_entry(
        action="config.tiers.set", bot_id="team_bot_a", oc_keys=["agents"],
    )
    with open(audit_path, "w") as f:
        f.write("not json at all\n")
        f.write(json.dumps(valid) + "\n")
        f.write("{partial json\n")
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert keys == {"agents"}


def test_tolerates_z_suffix_timestamps(tmp_path):
    """Some loggers emit '...Z' instead of '+00:00' — both must parse."""
    shared_dir = tmp_path
    shared_dir.mkdir(parents=True, exist_ok=True)
    audit_path = shared_dir / "audit-log.jsonl"
    now_z = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry = {
        "timestamp": now_z,
        "action": "config.tiers.set",
        "bot_id": "team_bot_a",
        "details": {},
        "oc_keys": ["agents"],
    }
    with open(audit_path, "w") as f:
        f.write(json.dumps(entry) + "\n")
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert keys == {"agents"}


def test_malformed_oc_keys_field_is_silently_ignored(tmp_path):
    """Defensive: if oc_keys is somehow not a list (e.g. a string from
    a buggy writer or hand-edited log line), don't crash — just ignore."""
    _write_audit_log(tmp_path, [
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "config.tiers.set",
            "bot_id": "team_bot_a",
            "oc_keys": "agents",  # malformed: should be a list
        },
        # ...followed by a well-formed entry that should still be credited
        _audit_entry(
            action="config.routing.set", bot_id="team_bot_a",
            oc_keys=["agents"],
        ),
    ])
    keys = _get_recent_admin_ui_writes("team_bot_a", tmp_path)
    assert keys == {"agents"}


# ── End-to-end: detect_backup_drift_keys uses the credit ───────────────────


def _stub_subprocess_and_file_reads(monkeypatch, *, committed: dict, live: dict):
    """Stub both committed-config (git subprocess) AND live-config (file read).

    `detect_backup_drift_keys`:
      - reads committed_cfg via `subprocess.run(["git", ..., "show",
        "HEAD:evolve-backup/openclaw.json"])`
      - reads live_cfg via `Path(live_path).read_text()` (fallback: sudo cat)

    We monkeypatch `subprocess.run` and `Path.read_text` so the test
    can drive the function without a real bot home directory or git
    repo. Mocking both lets us exercise the whole drift-credit path.
    """
    real_run = heal.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        # The committed-config read: git show HEAD:evolve-backup/openclaw.json
        if (
            isinstance(cmd, list)
            and "git" in cmd
            and any("HEAD:evolve-backup/openclaw.json" in c for c in cmd if isinstance(c, str))
        ):
            return _FakeProc(returncode=0, stdout=json.dumps(committed), stderr="")
        # Other subprocess calls (sudo cat fallback, etc.) — let live read
        # land via Path.read_text first; if that's monkeypatched it won't
        # reach sudo. Defer to real run only if absolutely needed.
        return real_run(cmd, *args, **kwargs)

    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if str(self).endswith("/.openclaw/openclaw.json"):
            return json.dumps(live)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(heal.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "read_text", fake_read_text)


class _FakeProc:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_drift_detector_credits_admin_ui_write_end_to_end(tmp_path, monkeypatch):
    """End-to-end: live config differs from committed config in the
    'agents' key, audit log shows a recent config.tiers.set on this
    bot → drift detector returns no unexplained keys."""
    committed = {
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-sonnet-4-6"}}},
        "plugins": {},
        "meta": {"lastTouchedAt": "old"},
    }
    live = {
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-opus-4-7"}}},
        "plugins": {},
        "meta": {"lastTouchedAt": "new"},
    }
    _stub_subprocess_and_file_reads(monkeypatch, committed=committed, live=live)
    _write_audit_log(tmp_path, [
        _audit_entry(
            action="config.tiers.set", bot_id="team_bot_a", hours_ago=0.01,
            oc_keys=["agents"],
        ),
    ])

    network_config = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    keys = detect_backup_drift_keys("team_bot_a", tmp_path, network_config)
    assert keys == [], (
        f"recent admin-UI write should explain the agents-key drift; "
        f"got unexplained keys: {keys}"
    )


def test_drift_detector_returns_key_when_no_audit_entry(tmp_path, monkeypatch):
    """Negative case: actual off-path drift (no audit entry, no apply-result)
    must still surface as unexplained — the detector's safety-net job."""
    committed = {
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-sonnet-4-6"}}},
    }
    live = {
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-opus-4-7"}}},
    }
    _stub_subprocess_and_file_reads(monkeypatch, committed=committed, live=live)
    # No audit log written → no credit

    network_config = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    keys = detect_backup_drift_keys("team_bot_a", tmp_path, network_config)
    assert "agents" in keys, (
        "off-path drift (no audit, no apply-result) must still alert — "
        "that's the whole point of the safety net"
    )


# ── Writer-side contract sentinels ──────────────────────────────────────────
#
# These tests pin the WRITER side of the contract — that every config-writing
# endpoint passes oc_keys to its audit-log call. Because the structural fix
# moved the intent declaration from a remote translation table to the call
# site itself, a writer that forgets to pass oc_keys silently fails to
# suppress drift. These sentinels catch that regression at unit-test time
# instead of letting it surface as an operator alert on the mini.


def test_web_server_audit_log_entry_accepts_oc_keys_kwarg():
    """STRUCTURAL: the boundary function `_audit_log_entry` in web/server.py
    must accept `oc_keys` as a keyword argument. If a refactor removes
    or renames this, every config-writing endpoint silently loses its
    drift-credit and the false-positive alerts return."""
    import sys as _sys
    admin_path = str(Path(__file__).resolve().parent.parent.parent / "admin")
    if admin_path not in _sys.path:
        _sys.path.insert(0, admin_path)
    from evolve_admin.web import server as _srv
    import inspect
    sig = inspect.signature(_srv._audit_log_entry)
    assert "oc_keys" in sig.parameters, (
        "web/server.py::_audit_log_entry must accept oc_keys kwarg — "
        "the structural fix replaces heal.py's translation map with "
        "writer-self-declared oc_keys. Removing this kwarg silently "
        "reintroduces false-positive drift alerts."
    )


def test_provisioning_record_audit_accepts_oc_keys_kwarg():
    """STRUCTURAL: the same contract applies to provisioning._record_audit,
    used by the CLI seed path and auto-seed inside provision_bot()."""
    import sys as _sys
    admin_path = str(Path(__file__).resolve().parent.parent.parent / "admin")
    if admin_path not in _sys.path:
        _sys.path.insert(0, admin_path)
    from evolve_admin import provisioning as _prov
    import inspect
    sig = inspect.signature(_prov._record_audit)
    assert "oc_keys" in sig.parameters, (
        "provisioning._record_audit must accept oc_keys kwarg — "
        "CLI / auto-seed paths bypass the web layer's _audit_log_entry "
        "and would otherwise silently fail to credit drift."
    )
