"""tests/test_keystore_auth_store_import.py — OC-SQLITE-AUTH-WRITE follow-up.

The same-class residual gap not closed by the admin-UI / restart-endpoint
wiring: the **CLI keystore path**. ``evolve-admin keys sync|rotate|rollback``
mutate a bot's ``auth-profiles.json`` directly (``KeystoreManager.
_write_to_auth_profiles``). OC 2026.6+ imports that JSON into the per-agent
SQLite store only on agent-CLI init, NOT on gateway start — so an operator who
syncs from the CLI and then restarts the gateway via a non-admin-endpoint path
(``deploy.restart_gateway`` / ``launchctl kickstart``) leaves the running agent
on a stale/empty key.

These tests pin the fix:
  * a SUCCESSFUL keystore write triggers ``ensure_agent_auth_store_imported``
    with the macOS account resolved via the seam (``bot_user_for``), NOT the
    bot_id literal, and with the home we wrote into;
  * a FAILED write (and a dry-run, which never reaches the writer) does NOT
    trigger it;
  * the trigger is best-effort — a helper blow-up never undoes the JSON write;
  * the provider key NEVER reaches the import subprocess on argv or stdin
    (it lives only in the 0600 JSON) — the ps-leak guard from
    ``test_oc_auth_store_import.py``;
  * the ``normalize_auth_profile_keys`` provisioning path re-triggers the
    import ONLY when it actually renames a profile key (the genuinely uncovered
    path: ``seed-model-config`` never restarts the gateway, and provision_bot's
    Stage-7 normalize runs after the Stage-6 deploy import).

Reuses the helper itself (no reimplementation); the helper's own command-shape
/ redaction contract is pinned in ``test_oc_auth_store_import.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Pinned absolute oc path so sudo command-matching is deterministic regardless
# of what (if anything) is installed in the test env.
_OC_BIN = "/opt/homebrew/bin/openclaw"

# The import-trigger lives on its home module and is imported lazily at each
# call site, so patching it there is what the lazy import resolves.
_HELPER = "evolve_admin.oc_auth_provision.ensure_agent_auth_store_imported"


def _make_agent_dir(bot_home: Path) -> Path:
    agent_dir = bot_home / ".openclaw" / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return agent_dir


# ── keystore _write_to_auth_profiles: success triggers the import ────────────


def test_keystore_write_triggers_import_with_resolved_bot_user(tmp_path, monkeypatch):
    """A successful auth-profiles write primes the sqlite store, addressing the
    bot by the macOS account resolved through the seam (NOT the bot_id literal)
    and the home we just wrote into."""
    from evolve_admin import keystore as ks_mod

    bot_home = tmp_path / "Users" / "macos_acct"
    _make_agent_dir(bot_home)
    # bot_id (logical) deliberately != macOS account, to prove seam resolution.
    monkeypatch.setattr(ks_mod, "_bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr(ks_mod, "_bot_user_for", lambda bot_id: "macos_acct")

    mgr = ks_mod.KeystoreManager(tmp_path / "shared")
    with patch(_HELPER) as helper:
        helper.return_value = (True, "ok")
        ok = mgr._write_to_auth_profiles(
            bot_id="logicalbot", provider="anthropic",
            key_name="anthropic_api_key", value="sk-ant-DUMMY",
        )

    assert ok is True
    helper.assert_called_once()
    args, kwargs = helper.call_args
    assert args[0] == "logicalbot"          # bot_id (log only)
    assert args[1] == "macos_acct"          # resolved via bot_user_for, not bot_id
    assert kwargs.get("bot_home") == bot_home

    # And the value actually landed on disk (this is what the import reads).
    written = json.loads(
        (bot_home / ".openclaw/agents/main/agent/auth-profiles.json").read_text()
    )
    assert written["profiles"]["anthropic"]["apiKey"] == "sk-ant-DUMMY"


def test_keystore_failed_write_does_not_trigger_import(tmp_path, monkeypatch):
    """No agent dir → _write_to_auth_profiles returns False before writing, and
    the import must NOT fire (priming a store we never wrote to is meaningless
    and the task contract is 'not on a failed write')."""
    from evolve_admin import keystore as ks_mod

    bot_home = tmp_path / "Users" / "macos_acct"  # parent dir intentionally absent
    monkeypatch.setattr(ks_mod, "_bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr(ks_mod, "_bot_user_for", lambda bot_id: "macos_acct")

    mgr = ks_mod.KeystoreManager(tmp_path / "shared")
    with patch(_HELPER) as helper:
        ok = mgr._write_to_auth_profiles(
            bot_id="logicalbot", provider="anthropic",
            key_name="anthropic_api_key", value="sk-ant-DUMMY",
        )

    assert ok is False
    helper.assert_not_called()


def test_keystore_import_failure_is_best_effort(tmp_path, monkeypatch):
    """A blow-up while priming the store (here: the lazy import / helper raising)
    must never undo a write that already succeeded — oc_store reads the JSON
    regardless. _write_to_auth_profiles still returns True."""
    from evolve_admin import keystore as ks_mod

    bot_home = tmp_path / "Users" / "macos_acct"
    _make_agent_dir(bot_home)
    monkeypatch.setattr(ks_mod, "_bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr(ks_mod, "_bot_user_for", lambda bot_id: "macos_acct")

    mgr = ks_mod.KeystoreManager(tmp_path / "shared")
    with patch(_HELPER, side_effect=RuntimeError("boom")):
        ok = mgr._write_to_auth_profiles(
            bot_id="logicalbot", provider="anthropic",
            key_name="anthropic_api_key", value="sk-ant-DUMMY",
        )

    assert ok is True


def test_keystore_write_never_leaks_key_to_import_subprocess(tmp_path, monkeypatch):
    """ps-leak guard (end-to-end, REAL helper): the provider key reaches disk via
    the JSON write, but must never appear on the import subprocess argv or be fed
    on its stdin. Pins the contract so a future refactor can't route it there."""
    from evolve_admin import deploy, keystore as ks_mod

    bot_home = tmp_path / "Users" / "macos_acct"
    _make_agent_dir(bot_home)
    monkeypatch.setattr(ks_mod, "_bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr(ks_mod, "_bot_user_for", lambda bot_id: "macos_acct")

    SENTINEL = "sk-ant-SECRET-LEAK-1234"
    calls: list[dict] = []

    def fake_run(argv, *a, **k):
        calls.append({"argv": list(argv), "input": k.get("input")})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    mgr = ks_mod.KeystoreManager(tmp_path / "shared")
    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run", side_effect=fake_run):
        ok = mgr._write_to_auth_profiles(
            bot_id="logicalbot", provider="anthropic",
            key_name="anthropic_api_key", value=SENTINEL,
        )

    assert ok is True
    # The verify-driven import ran end-to-end: it read the just-written JSON,
    # found the (faked) store empty, and pasted the key — so a `models auth list`
    # verify ran and a paste followed.
    assert any(c["argv"][-3:] == ["models", "auth", "list"] for c in calls), \
        [c["argv"] for c in calls]
    paste = [c for c in calls if "paste-api-key" in c["argv"]]
    assert paste, "expected a paste-api-key fallback when the store was empty"
    # The key is on NONE of the subprocess argvs, and on stdin ONLY for paste-*
    # (the verify reads carry no key). This is the corrected security invariant.
    for c in calls:
        assert all(SENTINEL not in tok for tok in c["argv"]), c["argv"]
        is_paste = "paste-api-key" in c["argv"] or "paste-token" in c["argv"]
        if is_paste:
            assert c["input"] == SENTINEL + "\n"
        else:
            assert c["input"] is None or SENTINEL not in str(c["input"]), c["argv"]


# ── keystore sync(): per-bot funnel + dry-run exclusion ──────────────────────


def _seed_shared_key(mgr) -> None:
    mgr.register(
        "test_shared", provider="anthropic", scope="shared",
        description="auth-store-import test", bots=None, value="sk-ant-DUMMY",
    )


def test_sync_triggers_import_but_dry_run_does_not(tmp_path, monkeypatch):
    """sync() funnels through _write_to_auth_profiles, so the import fires once a
    key actually lands on the bot. A dry-run never calls the writer, so it must
    NOT trigger the import (no real write happened)."""
    from evolve_admin import keystore as ks_mod

    # Force the deterministic file-vault path so value storage never touches a
    # developer's real macOS Keychain.
    monkeypatch.setattr(ks_mod, "_has_security_cmd", lambda: False)

    bot_home = tmp_path / "Users" / "macos_acct"
    _make_agent_dir(bot_home)
    monkeypatch.setattr(ks_mod, "_bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr(ks_mod, "_bot_user_for", lambda bot_id: "macos_acct")

    mgr = ks_mod.KeystoreManager(tmp_path / "shared")
    _seed_shared_key(mgr)

    with patch(_HELPER) as helper:
        helper.return_value = (True, "ok")
        results = mgr.sync(["logicalbot"])
    assert "test_shared" in results["logicalbot"]
    helper.assert_called_once()
    assert helper.call_args.args[1] == "macos_acct"

    with patch(_HELPER) as helper_dry:
        mgr.sync(["logicalbot"], dry_run=True)
    helper_dry.assert_not_called()


# ── provisioning normalize_auth_profile_keys: gated on actual rename ─────────


def _write_auth_profiles(agent_dir: Path, profiles: dict) -> Path:
    path = agent_dir / "auth-profiles.json"
    path.write_text(json.dumps({"version": 1, "profiles": profiles}, indent=2))
    return path


def _fake_cp_run_factory(calls: list[dict]):
    """A subprocess.run fake that makes `sudo /bin/cp` a real copy (so the
    normalize write succeeds), no-ops chown/chmod, and records every argv/stdin
    (so the import call and a ps-leak check ride the same capture)."""
    def fake_run(argv, *a, **k):
        calls.append({"argv": list(argv), "input": k.get("input")})
        if isinstance(argv, (list, tuple)) and len(argv) >= 2 and argv[0] == "sudo":
            verb = argv[1]
            if verb == "/bin/cp":
                shutil.copy(argv[2], argv[3])
            # cp / chown / chmod / the `models auth list` import all resolve 0.
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return fake_run


def test_normalize_triggers_import_on_actual_rename(tmp_path, monkeypatch):
    """A real rename rewrites which profile key the credential lives under on
    disk; the running agent's sqlite store still holds the un-normalized
    (runtime-invisible) entry, so normalize must re-trigger the import — as the
    macOS account it operates on."""
    from evolve_admin import provisioning as prov

    bot_home = tmp_path / "Users" / "atlas"
    agent_dir = _make_agent_dir(bot_home)
    _write_auth_profiles(agent_dir, {
        "anthropic_api_key": {  # legacy underscore shape → needs normalization
            "provider": "anthropic", "type": "api_key", "key": "sk-ant-DUMMY",
        },
    })
    monkeypatch.setattr(prov, "_user_home", lambda user: bot_home)

    calls: list[dict] = []
    with patch("subprocess.run", side_effect=_fake_cp_run_factory(calls)), \
         patch(_HELPER) as helper:
        helper.return_value = (True, "ok")
        result = prov.normalize_auth_profile_keys("atlas")

    assert result["ok"] is True
    assert result["renames"] == [{"from": "anthropic_api_key", "to": "anthropic:api_key"}]
    helper.assert_called_once()
    # normalize operates entirely on the macOS `user`; both args are that user.
    assert helper.call_args.args == ("atlas", "atlas")


def test_normalize_noop_does_not_trigger_import(tmp_path, monkeypatch):
    """All keys already canonical → no rewrite, so the import must NOT fire (the
    idempotent common case must stay subprocess-free)."""
    from evolve_admin import provisioning as prov

    bot_home = tmp_path / "Users" / "atlas"
    agent_dir = _make_agent_dir(bot_home)
    _write_auth_profiles(agent_dir, {
        "anthropic:api_key": {
            "provider": "anthropic", "type": "api_key", "key": "sk-ant-DUMMY",
        },
    })
    monkeypatch.setattr(prov, "_user_home", lambda user: bot_home)

    with patch(_HELPER) as helper:
        result = prov.normalize_auth_profile_keys("atlas")

    assert result["ok"] is True
    assert result["renames"] == []
    helper.assert_not_called()


def test_normalize_rename_never_leaks_key_to_import_subprocess(tmp_path, monkeypatch):
    """ps-leak guard for the normalize path: the key rides into the new JSON via
    the /tmp staging file + `sudo /bin/cp` (whose argv carries the tmp PATH), and
    must never appear on the cp / chown / chmod / import argvs or stdins."""
    from evolve_admin import deploy, provisioning as prov

    SENTINEL = "sk-ant-SECRET-LEAK-5678"
    bot_home = tmp_path / "Users" / "atlas"
    agent_dir = _make_agent_dir(bot_home)
    _write_auth_profiles(agent_dir, {
        "anthropic_api_key": {
            "provider": "anthropic", "type": "api_key", "key": SENTINEL,
        },
    })
    monkeypatch.setattr(prov, "_user_home", lambda user: bot_home)

    calls: list[dict] = []
    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run", side_effect=_fake_cp_run_factory(calls)):
        result = prov.normalize_auth_profile_keys("atlas")  # REAL helper runs

    assert result["renames"] == [{"from": "anthropic_api_key", "to": "anthropic:api_key"}]
    assert any(c["argv"][-3:] == ["models", "auth", "list"] for c in calls), \
        [c["argv"] for c in calls]
    # Key never on argv; on stdin ONLY for the paste-* fallback.
    for c in calls:
        assert all(SENTINEL not in tok for tok in c["argv"]), c["argv"]
        is_paste = "paste-api-key" in c["argv"] or "paste-token" in c["argv"]
        if is_paste:
            assert c["input"] == SENTINEL + "\n"
        else:
            assert c["input"] is None or SENTINEL not in str(c["input"]), c["argv"]
