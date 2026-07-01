"""Pinning tests for subprocess invocations from audit.py.

These pin two fixes from 2026-05-07:

  1. audit_script_inventory previously called `sudo find <workspace>` but
     /etc/sudoers.d/evolve has no /usr/bin/find grant — the call failed
     with "sudo: a password is required" on every audit run, producing a
     spurious WARN per bot. The deploy-time evolve ACL on .openclaw/
     gives the audit user direct read access; sudo is unnecessary.

  2. _send_security_alert's openclaw fallback path (used when the
     dedicated security-alert keystore is absent) used to run subprocess
     in audit.py with cwd=/tmp to dodge a Node-EACCES startup abort. As
     of Phase 3 of the alert-notifier consolidation, that fallback now
     routes through alerts.dispatcher.send instead of an inline
     subprocess; the cwd=/tmp protection lives in the dispatcher and is
     pinned in test_alerts_dispatcher.py::test_subprocess_call_pins_cwd_tmp.
     The audit-side test below now pins the dispatcher hand-off rather
     than the subprocess kwargs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


# ── audit_script_inventory: no sudo on find ──


def test_script_inventory_uses_direct_find_not_sudo(tmp_path: Path, monkeypatch):
    """Pin the contract: the find subprocess call must NOT start with `sudo`."""
    home = tmp_path / "bot"
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "thing.py").write_text("print(1)")

    captured: list = []

    def fake_run(args, **kw):
        captured.append(list(args))
        # Mimic real find output for the assertion downstream
        return type("R", (), {
            "returncode": 0,
            "stdout": str(workspace / "thing.py") + "\n",
            "stderr": "",
        })()

    monkeypatch.setattr(audit, "_bot_home", lambda *_a, **_k: home)
    # NOTE: monkeypatching audit.subprocess.run patches the SHARED subprocess
    # module, so on a Linux runner the pre-find reassert_mask's getfacl probe is
    # captured here too (it's a no-op on macOS). Locate the find among all
    # captured calls rather than assuming it is first.
    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    audit.audit_script_inventory("bot", tmp_path)

    assert captured, "subprocess.run was never invoked"
    find_cmds = [c for c in captured if c and c[0] == "find"]
    assert find_cmds, f"no direct `find` invocation; calls={captured}"
    cmd = find_cmds[0]
    assert "sudo" not in cmd, f"sudo should not appear in: {cmd}"


# ── _send_security_alert fallback now routes through alerts.dispatcher ──


def test_send_security_alert_fallback_routes_through_dispatcher(
    tmp_path: Path, monkeypatch
):
    """When the dedicated security-alert keystore is absent, the fallback
    path goes through alerts.dispatcher.send (Phase 3 of alert-notifier
    spec). Audit no longer calls subprocess directly — the dispatcher
    owns the openclaw subprocess (and the cwd=/tmp protection)."""
    # Make evolve_admin importable from this analyzer-side test.
    admin_dir = _ANALYZER_DIR.parent / "admin"
    if str(admin_dir) not in sys.path:
        sys.path.insert(0, str(admin_dir))

    from evolve_admin.alerts import dispatcher

    captured: list = []

    def fake_send(*, shared_dir, network, source, message, severity,
                  dedup_key=None, catalog_event=None, **_kw):
        captured.append({
            "source": source, "message": message,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        from evolve_admin.alerts.dispatcher import DispatchOutcome, DispatchResult
        return DispatchOutcome(
            result=DispatchResult.SENT, source=source, severity=severity,
            dedup_key=dedup_key, channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    # Audit must NOT call subprocess directly anymore.
    def fail_run(*a, **kw):
        raise AssertionError(
            f"audit must not call subprocess.run directly — route through "
            f"dispatcher. args={a}, kw={kw}"
        )

    monkeypatch.setattr(audit.subprocess, "run", fail_run)

    config = {"alerts": {"channel": "telegram", "chatId": "12345"}}
    audit._send_security_alert("test message", tmp_path, config)

    assert len(captured) == 1, f"expected one dispatcher.send call, got {captured}"
    call = captured[0]
    assert call["source"] == "audit"
    assert call["catalog_event"] == "security.audit_finding", (
        f"Phase D: audit must annotate dispatcher.send with the catalog_event "
        f"so operator subscription preferences from Alerts → Subscriptions "
        f"take effect. got: {call['catalog_event']}"
    )
    assert call["message"] == "test message"
    assert call["severity"].value == "critical"
    assert call["dedup_key"] is not None and call["dedup_key"].startswith("audit/batch/")


def test_send_security_alert_dedup_key_changes_when_findings_change(
    tmp_path: Path, monkeypatch
):
    """The dedup_key is a stable hash of the message content. Identical
    findings → identical key (cooldown applies). Any change → fresh key
    (operator sees the new finding immediately)."""
    admin_dir = _ANALYZER_DIR.parent / "admin"
    if str(admin_dir) not in sys.path:
        sys.path.insert(0, str(admin_dir))

    from evolve_admin.alerts import dispatcher

    keys: list = []

    def fake_send(*, dedup_key=None, **_kw):
        keys.append(dedup_key)
        from evolve_admin.alerts.dispatcher import DispatchOutcome, DispatchResult, Severity
        return DispatchOutcome(
            result=DispatchResult.SENT, source="audit", severity=Severity.CRITICAL,
            dedup_key=dedup_key,
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    audit._send_security_alert("findings A", tmp_path, {})
    audit._send_security_alert("findings A", tmp_path, {})
    audit._send_security_alert("findings B (different)", tmp_path, {})

    assert len(keys) == 3
    assert keys[0] == keys[1], "identical messages must produce identical dedup_keys"
    assert keys[0] != keys[2], "different messages must produce different dedup_keys"


def test_send_security_alert_dispatcher_success_skips_direct(
    tmp_path: Path, monkeypatch,
):
    """When dispatcher.send succeeds, the direct-Telegram fallback (even
    with the dedicated security-alert keystore present) must NOT fire.
    Dispatcher-first is the post-Phase-D semantics — direct is reserved
    for "dispatcher is wedged" defense-in-depth, not a preferred path."""
    keystore = tmp_path / "keystore"
    keystore.mkdir()
    (keystore / "security-alert-token").write_text("fake-token")
    (keystore / "security-alert-chat-id").write_text("99999")

    direct_calls: list = []
    monkeypatch.setattr(audit, "_send_telegram_direct",
                        lambda token, chat_id, msg: direct_calls.append((token, chat_id, msg)))

    # Force dispatcher success so we're testing the "skip direct" path.
    monkeypatch.setattr(audit, "_send_via_dispatcher",
                        lambda msg, shared_dir, config: True)

    audit._send_security_alert("test", tmp_path, config={})
    assert direct_calls == [], (
        f"direct-Telegram must not fire when dispatcher succeeded; got: {direct_calls}"
    )


def test_send_security_alert_falls_back_to_keystore_when_dispatcher_fails(
    tmp_path: Path, monkeypatch,
):
    """When dispatcher.send fails (e.g. admin package wedged), the fallback
    uses the dedicated security-alert keystore files to POST directly via
    urllib — bypassing the openclaw gateway entirely."""
    keystore = tmp_path / "keystore"
    keystore.mkdir()
    (keystore / "security-alert-token").write_text("fake-token")
    (keystore / "security-alert-chat-id").write_text("99999")

    direct_calls: list = []
    monkeypatch.setattr(audit, "_send_telegram_direct",
                        lambda token, chat_id, msg: direct_calls.append((token, chat_id, msg)))

    # Force dispatcher failure so the fallback path runs.
    monkeypatch.setattr(audit, "_send_via_dispatcher",
                        lambda msg, shared_dir, config: False)

    def fail_run(*a, **kw):
        raise AssertionError(f"subprocess.run should not be invoked: args={a}")

    monkeypatch.setattr(audit.subprocess, "run", fail_run)

    audit._send_security_alert("test", tmp_path, config={})
    assert direct_calls and direct_calls[0][1] == "99999"


# ── audit_script_inventory: runtime ACL-clamp resilience ──
#
# On Linux the OC gateway re-hardens .openclaw to 0700 on its runtime ops,
# clamping the POSIX-ACL mask so evolve loses traverse — and this hourly audit
# is one of the readers that trips over it. The find must (a) couple a
# mask-reassert before it runs, and (b) degrade gracefully (no scary signal)
# on a transient permission-denied rather than pinning a warn finding.


class _RecordingPerms:
    def __init__(self):
        self.reasserted = []

    def reassert_mask(self, path, *, recursive=False):
        self.reasserted.append(str(path))
        return True


def _patch_perms(monkeypatch):
    import runtime.perms as rp
    fake = _RecordingPerms()
    monkeypatch.setattr(rp, "get_perms", lambda: fake)
    return fake


def test_script_inventory_reasserts_mask_before_find(tmp_path: Path, monkeypatch):
    """The find couples a `reassert_mask` on the bot's .openclaw before it
    runs — undoing a clamp the gateway may have just applied."""
    home = tmp_path / "bot"
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)

    order: list = []
    fake_perms = _patch_perms(monkeypatch)
    monkeypatch.setattr(fake_perms, "reassert_mask",
                        lambda p, **k: order.append("reassert") or True)

    def fake_run(args, **kw):
        order.append("find")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(audit, "_bot_home", lambda *_a, **_k: home)
    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    audit.audit_script_inventory("bot", tmp_path)
    assert order and order[0] == "reassert" and "find" in order


def test_script_inventory_permission_denied_degrades_without_warn(tmp_path, monkeypatch):
    """A pure `Permission denied` find failure (the ACL-clamp shape) must NOT
    emit a warn finding — that became a scary, repeating Signal. It self-heals
    via the periodic reassert; surface an ok-level note instead."""
    home = tmp_path / "bot"
    (home / ".openclaw" / "workspace").mkdir(parents=True)
    _patch_perms(monkeypatch)

    def fake_run(args, **kw):
        return type("R", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": f"find: '{home}/.openclaw/workspace': Permission denied",
        })()

    monkeypatch.setattr(audit, "_bot_home", lambda *_a, **_k: home)
    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    findings = audit.audit_script_inventory("bot", tmp_path)
    assert all(f.level != "warn" for f in findings), \
        "transient EACCES must not page"
    assert any("transiently clamped" in f.message for f in findings)


def test_script_inventory_persistent_eacces_escalates_to_warn(tmp_path, monkeypatch):
    """A clamp that PERSISTS past the self-heal grace window is not transient
    (e.g. a bot 0700-ing a workspace subdir to hide scripts) — it must page."""
    home = tmp_path / "bot"
    (home / ".openclaw" / "workspace").mkdir(parents=True)
    _patch_perms(monkeypatch)

    def fake_run(args, **kw):
        return type("R", (), {
            "returncode": 1, "stdout": "",
            "stderr": f"find: '{home}/.openclaw/workspace/x': Permission denied",
        })()

    monkeypatch.setattr(audit, "_bot_home", lambda *_a, **_k: home)
    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    # Within the grace window: quiet (no warn).
    for _ in range(audit._SCRIPT_INVENTORY_EACCES_GRACE - 1):
        findings = audit.audit_script_inventory("bot", tmp_path)
        assert all(f.level != "warn" for f in findings)
    # The cycle that reaches the threshold escalates to warn.
    findings = audit.audit_script_inventory("bot", tmp_path)
    assert any(f.level == "warn" for f in findings)


def test_script_inventory_success_resets_eacces_counter(tmp_path, monkeypatch):
    """A successful find clears the consecutive-skip counter so a later
    transient clamp starts the grace window over (doesn't insta-page)."""
    home = tmp_path / "bot"
    (home / ".openclaw" / "workspace").mkdir(parents=True)
    _patch_perms(monkeypatch)
    monkeypatch.setattr(audit, "_bot_home", lambda *_a, **_k: home)

    denied = type("R", (), {
        "returncode": 1, "stdout": "",
        "stderr": f"find: '{home}/.openclaw/workspace': Permission denied"})()
    ok = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: denied)
    audit.audit_script_inventory("bot", tmp_path)
    audit.audit_script_inventory("bot", tmp_path)
    assert audit._load_eacces_counts(tmp_path).get("bot") == 2
    monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: ok)
    audit.audit_script_inventory("bot", tmp_path)
    assert "bot" not in audit._load_eacces_counts(tmp_path)


def test_script_inventory_other_find_error_still_warns(tmp_path, monkeypatch):
    """A NON-permission find failure is a real problem and must still warn —
    the degradation is scoped strictly to the ACL-clamp shape."""
    home = tmp_path / "bot"
    (home / ".openclaw" / "workspace").mkdir(parents=True)
    _patch_perms(monkeypatch)

    def fake_run(args, **kw):
        return type("R", (), {
            "returncode": 1, "stdout": "",
            "stderr": "find: invalid predicate `-bogus'",
        })()

    monkeypatch.setattr(audit, "_bot_home", lambda *_a, **_k: home)
    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    findings = audit.audit_script_inventory("bot", tmp_path)
    assert any(f.level == "warn" for f in findings)


def test_find_is_permission_denied_predicate():
    assert audit._find_is_permission_denied(
        "find: '/home/darwin/.openclaw/workspace': Permission denied")
    # mixed output (a real error alongside) is NOT suppressed
    assert not audit._find_is_permission_denied(
        "find: '/x': Permission denied\nfind: bad predicate")
    assert not audit._find_is_permission_denied("")
    assert not audit._find_is_permission_denied(None)
