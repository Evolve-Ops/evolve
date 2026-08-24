"""Unit tests for the wizard verification gauntlet.

Spec: internal/spec-wizard-verification-gauntlet-2026-05-30.md.

These tests cover each check's pure logic — ownership walking,
credential-shape scanning, channel handshake response parsing, e2e
contract block management. Subprocess-bound code paths (sudo /bin/cat,
sudo /usr/sbin/chown, urllib.request.urlopen) are patched. Anything
that requires a real bot user, real macOS ACL, or real upstream API
is out of scope for unit tests (covered separately by the
integration test).
"""

from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path
from unittest import mock

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import wizard_verify  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_bot_user():
    """The macOS user the tests pretend the bot runs as.

    We pick the current user (whatever pytest is running as) — that uid
    is guaranteed to resolve through pwd, and any file we create under
    tmp_path is owned by that uid by construction.
    """
    return pwd.getpwuid(os.getuid()).pw_name


@pytest.fixture
def fake_network(fake_bot_user):
    return {
        "bots": {
            "testbot": {
                "user": fake_bot_user,
                "port": 19099,
                "role": "member",
                "display_name": "TestBot",
            },
        },
    }


@pytest.fixture
def fake_oc_root(tmp_path, fake_bot_user, monkeypatch):
    """Build a fake /Users/<bot>/.openclaw/ tree under tmp_path.

    Patches ``Path(f"/Users/{bot_user}/.openclaw")`` references inside
    wizard_verify by monkey-patching the ``check_ownership`` walker's
    base path. Returns the tmp .openclaw root the test can populate.
    """
    home = tmp_path / "Users" / fake_bot_user
    oc = home / ".openclaw"
    oc.mkdir(parents=True)
    (oc / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"model": {"primary": "claude-sonnet-4-6"}}},
    }))
    (oc / "agents" / "main" / "agent").mkdir(parents=True)
    (oc / "agents" / "main" / "agent" / "auth-profiles.json").write_text(json.dumps({
        "profiles": {"anthropic:api_key": {"value": "sk-ant-fake"}},
    }))
    (oc / "logs").mkdir()
    (oc / "logs" / "gateway.err.log").write_text("nothing here\n")
    (oc / "workspace").mkdir()
    return oc


# Helper: monkey-patch Path("/Users/...") references inside wizard_verify
# by patching the module-level Path construction sites to redirect under
# a tmp root. The pattern in the module is `Path(f"/Users/{bot_user}/...")`
# scattered through individual helpers — easiest to just monkey-patch the
# helpers themselves to read from our tmp root.


@pytest.fixture
def redirect_oc_paths(monkeypatch, fake_oc_root):
    """Patch wizard_verify's path-construction helpers to read tmp."""
    def _patched_walk_root(bot_user):
        return fake_oc_root

    # Patch check_ownership's hardcoded Path construction by reaching into
    # the function via mock.patch on the relevant module-level helpers.
    # Simplest hook: monkey-patch the four file readers and the walker
    # root resolver to point at fake_oc_root.

    def patched_read_oc_json(bot_user):
        path = fake_oc_root / "openclaw.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def patched_read_auth_profiles(bot_user):
        path = fake_oc_root / "agents" / "main" / "agent" / "auth-profiles.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def patched_err_tail(bot_user, lines=60):
        path = fake_oc_root / "logs" / "gateway.err.log"
        if not path.exists():
            return None
        return "\n".join(path.read_text().splitlines()[-lines:])

    monkeypatch.setattr(wizard_verify, "_read_openclaw_json", patched_read_oc_json)
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", patched_read_auth_profiles)
    monkeypatch.setattr(wizard_verify, "_read_gateway_err_tail", patched_err_tail)

    return fake_oc_root


# ── Check 1: ownership audit ─────────────────────────────────────────────────


def test_ownership_clean_returns_ok(monkeypatch, tmp_path, fake_bot_user, fake_network):
    """All-bot-owned tree → STATUS_OK."""
    oc = tmp_path / "Users" / fake_bot_user / ".openclaw"
    oc.mkdir(parents=True)
    (oc / "openclaw.json").write_text("{}")

    # Patch Path("/Users/<bot>/.openclaw") in check_ownership by reaching
    # into the closure. Easiest: redirect via monkey-patch on Path.exists
    # / Path constructor. Simpler: monkey-patch check_ownership's locals
    # by replacing the function's `oc_root` lookup — we just rewrite the
    # function to receive an explicit root for the test.
    #
    # Implementation choice: patch the module-level pwd.getpwnam call to
    # return the current uid AND patch the hardcoded path. Cleanest with
    # a small helper wrapper.
    with mock.patch.object(wizard_verify, "Path") as mp:
        def path_factory(arg):
            # Hijack only the .openclaw root construction; pass everything
            # else through to real Path
            s = str(arg)
            if s == f"/Users/{fake_bot_user}/.openclaw":
                return Path(str(oc))
            return Path(s)
        mp.side_effect = path_factory
        result = wizard_verify.check_ownership("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK, result.summary
    assert result.issues == []


def test_ownership_finds_root_owned_path(tmp_path, fake_bot_user, fake_network):
    """Root-owned file under .openclaw/ → STATUS_FAIL + path in issues."""
    oc = tmp_path / "Users" / fake_bot_user / ".openclaw"
    oc.mkdir(parents=True)
    bad = oc / "auth-profiles.json"
    bad.write_text("{}")

    # Inject a root-owned uid for this one file
    expected_uid = pwd.getpwnam(fake_bot_user).pw_uid
    real_lstat = Path.lstat

    def fake_lstat(self):
        st = real_lstat(self)
        if str(self) == str(bad):
            # Return a mock stat with st_uid=0 (root)
            class _FakeStat:
                st_uid = 0
            return _FakeStat()
        return st

    with mock.patch.object(wizard_verify, "Path") as mp:
        def path_factory(arg):
            s = str(arg)
            if s == f"/Users/{fake_bot_user}/.openclaw":
                return Path(str(oc))
            return Path(s)
        mp.side_effect = path_factory
        with mock.patch.object(Path, "lstat", fake_lstat):
            result = wizard_verify.check_ownership("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL, result.summary
    assert result.fix_available is True
    assert any(str(bad) == i["path"] for i in result.issues)
    assert all(i["expected_owner"] == fake_bot_user for i in result.issues)


def test_ownership_respects_exemption(tmp_path, fake_bot_user, fake_network):
    """Root-owned plugin-dist file → still STATUS_OK because exempt."""
    oc = tmp_path / "Users" / fake_bot_user / ".openclaw"
    plugin = oc / "plugins" / "evolve-plugin" / "dist"
    plugin.mkdir(parents=True)
    bad = plugin / "index.js"
    bad.write_text("// plugin code\n")

    real_lstat = Path.lstat

    def fake_lstat(self):
        st = real_lstat(self)
        if str(self) == str(bad):
            class _FakeStat:
                st_uid = 0  # root
            return _FakeStat()
        return st

    with mock.patch.object(wizard_verify, "Path") as mp:
        def path_factory(arg):
            s = str(arg)
            if s == f"/Users/{fake_bot_user}/.openclaw":
                return Path(str(oc))
            return Path(s)
        mp.side_effect = path_factory
        with mock.patch.object(Path, "lstat", fake_lstat):
            result = wizard_verify.check_ownership("testbot", network=fake_network)
    # No findings → STATUS_OK
    assert result.status == wizard_verify.STATUS_OK, result.issues


def test_ownership_respects_workspace_root_carveout(tmp_path, fake_bot_user, fake_network):
    """workspace/INSTALLED_APPS.md owner=evolve → STATUS_OK (atlas 2026-05-30 regression).

    Before the carve-out, every healthy bot post-2026-05-28 showed
    workspace/INSTALLED_APPS.md as a finding because the file is
    evolve-written. The added carve-out exempts the four well-known
    top-of-workspace admin-written files.
    """
    oc = tmp_path / "Users" / fake_bot_user / ".openclaw"
    ws = oc / "workspace"
    ws.mkdir(parents=True)
    bad = ws / "INSTALLED_APPS.md"
    bad.write_text("# Installed apps\n")

    real_lstat = Path.lstat

    def fake_lstat(self):
        st = real_lstat(self)
        if str(self) == str(bad):
            class _FakeStat:
                st_uid = 1234  # not the bot user — pretend evolve
            return _FakeStat()
        return st

    with mock.patch.object(wizard_verify, "Path") as mp:
        def path_factory(arg):
            s = str(arg)
            if s == f"/Users/{fake_bot_user}/.openclaw":
                return Path(str(oc))
            return Path(s)
        mp.side_effect = path_factory
        with mock.patch.object(Path, "lstat", fake_lstat):
            result = wizard_verify.check_ownership("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK, result.issues


def test_ownership_respects_backup_file_pattern(tmp_path, fake_bot_user, fake_network):
    """Root-owned `openclaw.json.bak-*` → STATUS_OK (team_bot_a/team_bot_b 2026-05-30 false-positives)."""
    oc = tmp_path / "Users" / fake_bot_user / ".openclaw"
    oc.mkdir(parents=True)
    bad = oc / "openclaw.json.bak-20260402-220623"
    bad.write_text("{}")

    real_lstat = Path.lstat

    def fake_lstat(self):
        st = real_lstat(self)
        if str(self) == str(bad):
            class _FakeStat:
                st_uid = 0  # root
            return _FakeStat()
        return st

    with mock.patch.object(wizard_verify, "Path") as mp:
        def path_factory(arg):
            s = str(arg)
            if s == f"/Users/{fake_bot_user}/.openclaw":
                return Path(str(oc))
            return Path(s)
        mp.side_effect = path_factory
        with mock.patch.object(Path, "lstat", fake_lstat):
            result = wizard_verify.check_ownership("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK, result.issues


def test_ownership_unknown_user_returns_fail(fake_network):
    network = {"bots": {"ghost": {"user": "nonexistent_user_xyz", "port": 1, "role": "member"}}}
    result = wizard_verify.check_ownership("ghost", network=network)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "not found" in result.summary


# ── Carve-out helper ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel,expected", [
    # Directory subtree carve-outs
    ("workspace/evolve", True),
    ("workspace/evolve/foo.json", True),
    ("workspace/evolve-backup/x", True),
    ("workspace/manifests", True),
    ("workspace/manifests/foo.json", True),
    ("plugins/evolve-plugin/dist/index.js", True),
    ("plugins/evolve-plugin", True),
    # Top-of-workspace admin-written files (added 2026-05-30 post-atlas)
    ("workspace/INSTALLED_APPS.md", True),
    ("workspace/POD_CONDUCT.md", True),
    ("workspace/AGENTS.md", True),
    ("workspace/RUNTIME_NOTES.md", True),
    # workspace/.git subtree — write-by-many; exempted 2026-05-30 after
    # the live-mini preview showed 99 of atlas's 100 findings were in
    # .git/objects/. Plus a representative leaf.
    ("workspace/.git", True),
    ("workspace/.git/config", True),
    ("workspace/.git/objects/6a/527a05ad3cbe9f85232d8d17ce68c9c8fb1433", True),
    # Migration-backup basename pattern (team_bot_a/team_bot_b hit this 2026-05-30)
    ("openclaw.json.bak-20260402-220623", True),
    ("openclaw.json.bak.original", True),
    ("openclaw.json.pre-cleanup-2026-05-13", True),
    ("openclaw.json.pre-streaming-off", True),
    # Same backup pattern but nested deeper — basename matcher still hits
    ("agents/main/agent/openclaw.json.bak-20260101", True),
    # Negative cases
    ("auth-profiles.json", False),
    ("agents/main/agent/auth-profiles.json", False),
    ("workspace/manifests-other", False),  # exact prefix match required
    ("workspace/INSTALLED_APPS.md.tmp", False),  # not the exact name
    ("openclaw.json", False),                    # the real config, not a backup
    ("openclaw.json.bak", True),                 # naked .bak (no suffix) — still leftover
    ("workspace/SOME_OTHER_FILE.md", False),
    ("", False),
])
def test_is_exempt(rel, expected):
    assert wizard_verify._is_exempt(rel) is expected


# ── Check 2: agent dry-run ──────────────────────────────────────────────────


def test_dry_run_no_port_configured_fails(fake_network):
    fake_network["bots"]["testbot"]["port"] = None
    result = wizard_verify.check_agent_dry_run("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "port" in result.summary.lower()


def test_dry_run_gateway_down_fails(monkeypatch, fake_network):
    monkeypatch.setattr(
        wizard_verify, "_probe_gateway",
        lambda port: (False, "probe failed: Connection refused"),
    )
    monkeypatch.setattr(
        wizard_verify, "_read_gateway_err_tail",
        lambda bot_user, lines=60: "ENOENT: oc plugin not loaded",
    )
    result = wizard_verify.check_agent_dry_run("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "19099" in result.summary
    assert "ENOENT" in (result.detail or "")


def test_dry_run_config_invalid_fails(monkeypatch, fake_network):
    monkeypatch.setattr(wizard_verify, "_probe_gateway", lambda port: (True, "ok"))
    monkeypatch.setattr(
        wizard_verify, "_run_config_validate",
        lambda bot, net: (False, [{"path": "plugins.entries.evolve", "message": "unknown property"}], ""),
    )
    result = wizard_verify.check_agent_dry_run("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "rejected" in result.summary
    assert "unknown property" in (result.detail or "")


def test_dry_run_missing_primary_model_fails(monkeypatch, fake_network):
    monkeypatch.setattr(wizard_verify, "_probe_gateway", lambda port: (True, "ok"))
    monkeypatch.setattr(wizard_verify, "_run_config_validate", lambda bot, net: (True, [], ""))
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"agents": {"defaults": {"model": {}}}},
    )
    result = wizard_verify.check_agent_dry_run("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "primary" in result.summary


def test_dry_run_legacy_key_shape_fails(monkeypatch, fake_network):
    """The [#1752] atlas regression — `anthropic_api_key` instead of `anthropic:api_key`."""
    monkeypatch.setattr(wizard_verify, "_probe_gateway", lambda port: (True, "ok"))
    monkeypatch.setattr(wizard_verify, "_run_config_validate", lambda bot, net: (True, [], ""))
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"agents": {"defaults": {"model": {"primary": "claude-sonnet-4-6"}}}},
    )
    monkeypatch.setattr(
        wizard_verify, "_read_auth_profiles",
        lambda user: {"profiles": {"anthropic_api_key": {"value": "sk-ant-fake"}}},
    )
    result = wizard_verify.check_agent_dry_run("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "Legacy key shape" in result.summary or "anthropic_api_key" in result.summary
    assert any(
        i.get("canonical") == "anthropic:api_key" for i in result.issues
    )


def test_dry_run_anthropic_credential_missing(monkeypatch, fake_network):
    """primary=claude-* but no anthropic:* key in auth-profiles."""
    monkeypatch.setattr(wizard_verify, "_probe_gateway", lambda port: (True, "ok"))
    monkeypatch.setattr(wizard_verify, "_run_config_validate", lambda bot, net: (True, [], ""))
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"agents": {"defaults": {"model": {"primary": "claude-sonnet-4-6"}}}},
    )
    monkeypatch.setattr(
        wizard_verify, "_read_auth_profiles",
        lambda user: {"profiles": {"openai:api_key": {"value": "sk-fake"}}},
    )
    result = wizard_verify.check_agent_dry_run("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "anthropic" in result.summary.lower()


def test_dry_run_all_green(monkeypatch, fake_network):
    monkeypatch.setattr(wizard_verify, "_probe_gateway", lambda port: (True, "ok"))
    monkeypatch.setattr(wizard_verify, "_run_config_validate", lambda bot, net: (True, [], ""))
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"agents": {"defaults": {"model": {"primary": "claude-sonnet-4-6"}}}},
    )
    monkeypatch.setattr(
        wizard_verify, "_read_auth_profiles",
        lambda user: {"profiles": {"anthropic:api_key": {
            # Real profile shape — the credential-shape scanner now
            # matches by profile.provider + profile.type (not by
            # profile key name) so the test fixture must include
            # those fields. Bare {"value": "..."} bodies were
            # ignored under the old name-only check and passed
            # vacuously; under the new check they correctly fail.
            "type": "api_key", "provider": "anthropic", "key": "sk-ant-fake",
        }}},
    )
    result = wizard_verify.check_agent_dry_run("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK
    assert "claude-sonnet-4-6" in result.summary


# ── Credential-shape scanner unit tests ──────────────────────────────────────


def test_scan_credential_shape_missing_file():
    issues = wizard_verify._scan_credential_shape(None, "claude-sonnet-4-6", {})
    assert len(issues) == 1
    assert "missing" in issues[0]["summary"].lower()


def test_scan_credential_shape_malformed_profiles():
    issues = wizard_verify._scan_credential_shape({"profiles": "not-an-object"}, "claude", {})
    assert any("malformed" in i["summary"].lower() for i in issues)


def test_scan_credential_shape_legacy_anthropic():
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {"anthropic_api_key": {"value": "x"}}},
        "claude-sonnet-4-6", {},
    )
    assert len(issues) == 1
    assert issues[0]["canonical"] == "anthropic:api_key"


def test_scan_credential_shape_legacy_openai():
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {"openai_api_key": {"value": "x"}}},
        "gpt-4", {},
    )
    assert len(issues) == 1
    assert issues[0]["canonical"] == "openai:api_key"


def test_scan_credential_shape_brave_underscore_not_flagged():
    """`brave_api_key` is intentionally tolerated — brave is an MCP plugin
    whose key is read by provider+type lookup, not OC's `agent.profile`
    resolver, so the [#1752] regression does not apply. Flagging it fans
    out one alert per bot with no operator-actionable fix (rotate just
    re-writes the same name)."""
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {
            "anthropic:api": {
                "type": "api_key", "provider": "anthropic", "key": "sk-ant-x",
            },
            "brave_api_key": {
                "type": "api_key", "provider": "brave", "key": "BSA-x",
            },
        }},
        "claude-sonnet-4-6", {},
    )
    assert issues == [], (
        f"brave_api_key must NOT be flagged as legacy shape. Issues: {issues}"
    )


def test_scan_credential_shape_anthropic_api_key_type_ok():
    """A profile with provider=anthropic AND type=api_key passes. The
    profile NAME is unconstrained — `anthropic:api_key` is one valid
    spelling but production bots use other slot names (see below)."""
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {"anthropic:api_key": {
            "type": "api_key", "provider": "anthropic", "key": "sk-ant-x",
        }}},
        "claude-sonnet-4-6", {},
    )
    assert issues == []


def test_scan_credential_shape_auth_token_accepted():
    """type=auth_token (OAuth flow) is a valid alternative to api_key."""
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {"anthropic:oauth": {
            "type": "auth_token", "provider": "anthropic", "key": "oat-x",
        }}},
        "claude-sonnet-4-6", {},
    )
    assert issues == []


# ── Production-shape profile names must pass ─────────────────────────────────
#
# Pre-2026-06-01 the check hardcoded profile NAMES `anthropic:api_key` and
# `anthropic:auth_token`. Every actual bot on the pod uses a different
# slot name (`anthropic:api`, `anthropic:default`, etc.) so the check
# false-positived on every working bot — it just wasn't hit because no
# one ran Screen 5 against an existing bot. Each test below pins a real
# profile name shape that must NOT trigger the missing-credential issue.


def test_scan_credential_shape_anthropic_api_slot_ok():
    """Production bots use the slot name `anthropic:api`."""
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {"anthropic:api": {
            "type": "api_key", "provider": "anthropic", "key": "sk-ant-x",
        }}},
        "claude-sonnet-4-6", {},
    )
    assert issues == [], (
        f"anthropic:api slot must pass — production bots use this name. "
        f"Issues: {issues}"
    )


def test_scan_credential_shape_anthropic_default_slot_ok():
    """Wizard paste-new path writes to `anthropic:default`."""
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {"anthropic:default": {
            "type": "api_key", "provider": "anthropic", "key": "sk-ant-x",
        }}},
        "claude-sonnet-4-6", {},
    )
    assert issues == [], (
        f"anthropic:default slot must pass — wizard paste-new path "
        f"writes here. Issues: {issues}"
    )


def test_scan_credential_shape_missing_anthropic_credential():
    """No anthropic profile at all → flag. Names don't matter; what
    matters is whether any profile has provider=anthropic + type=api_key."""
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {"openai:default": {
            "type": "api_key", "provider": "openai", "key": "sk-oai-x",
        }}},
        "claude-sonnet-4-6", {},
    )
    assert len(issues) == 1
    assert "anthropic" in issues[0]["summary"].lower()


def test_scan_credential_shape_wrong_provider_field_does_not_match():
    """A profile with the right NAME but wrong provider field must NOT
    pass — provider is the authoritative field, not the slot suffix."""
    issues = wizard_verify._scan_credential_shape(
        {"profiles": {"anthropic:api_key": {
            "type": "api_key", "provider": "openai", "key": "sk-x",
        }}},
        "claude-sonnet-4-6", {},
    )
    assert len(issues) == 1, (
        "Profile with provider=openai must not satisfy an anthropic primary "
        "model just because its name happens to start with 'anthropic:'."
    )


def test_provider_for_model():
    assert wizard_verify._provider_for_model("claude-sonnet-4-6") == "anthropic"
    assert wizard_verify._provider_for_model("gpt-4-turbo") == "openai"
    assert wizard_verify._provider_for_model("haiku-4-5") == "anthropic"
    assert wizard_verify._provider_for_model("unknown-model") is None
    assert wizard_verify._provider_for_model("") is None


# ── store-format/schema-drift distinction in the None branch ──────────────────


def test_scan_credential_shape_store_present_but_unreadable_is_schema_drift():
    """auth is None but a store artifact EXISTS on disk → a format/schema-drift
    advisory (the #3248 'Evolve can't read OpenClaw's credential store'
    vocabulary), NOT a false 'credential missing'."""
    issues = wizard_verify._scan_credential_shape(
        None, "claude-sonnet-4-6", {}, store_present=True,
    )
    assert len(issues) == 1
    summary = issues[0]["summary"].lower()
    assert "can't read" in summary or "cannot read" in summary
    assert "missing" not in summary
    # Routing-unaffected reassurance carries through to the detail.
    assert "routing is unaffected" in issues[0]["detail"].lower()


def test_scan_credential_shape_store_absent_is_unconfigured():
    """auth is None and NO artifact on disk → a genuinely un-configured bot."""
    issues = wizard_verify._scan_credential_shape(
        None, "claude-sonnet-4-6", {}, store_present=False,
    )
    assert len(issues) == 1
    assert "no credential store" in issues[0]["summary"].lower()


# ── _read_auth_profiles: the sqlite-migration source ladder ───────────────────


def _seed_agent_dir(home: Path, agent_id: str = "main") -> Path:
    d = home.joinpath(".openclaw", "agents", agent_id, "agent")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_sqlite_store(agent_dir: Path, profiles: dict) -> None:
    """Write an openclaw-agent.sqlite carrying the migrated profiles blob."""
    import sqlite3
    db = agent_dir / "openclaw-agent.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE auth_profile_store ("
            "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auth_profile_store VALUES ('primary', ?, 0)",
            (json.dumps({"version": 1, "profiles": profiles}),),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def _point_home_at(monkeypatch):
    """Point oc_store's account→home resolver at a temp tree so
    _read_auth_profiles reads the fixture store, not a real /Users home."""
    import evolve_config

    def _set(home: Path):
        monkeypatch.setattr(evolve_config, "user_home", lambda user: home)
    return _set


def test_read_auth_profiles_from_sqlite(tmp_path, _point_home_at):
    """A migrated pod (sqlite store, JSON deleted) resolves the profiles —
    the bug #3248 fixed in oc_keys, now fixed for the wizard reader too."""
    home = tmp_path / "bot"
    _seed_sqlite_store(_seed_agent_dir(home), {
        "anthropic:api_key": {"provider": "anthropic", "type": "api_key"},
    })
    _point_home_at(home)

    auth = wizard_verify._read_auth_profiles("bot")
    assert auth is not None
    assert "anthropic:api_key" in auth["profiles"]


def test_read_auth_profiles_falls_back_to_legacy_json(tmp_path, _point_home_at):
    """Un-migrated pod (no sqlite, legacy JSON present) → JSON path."""
    home = tmp_path / "bot"
    agent = _seed_agent_dir(home)
    (agent / "auth-profiles.json").write_text(
        json.dumps({"profiles": {"anthropic:api": {
            "provider": "anthropic", "type": "api_key", "key": "sk-ant-legacy",
        }}})
    )
    _point_home_at(home)

    auth = wizard_verify._read_auth_profiles("bot")
    assert auth is not None
    assert auth["profiles"]["anthropic:api"]["key"] == "sk-ant-legacy"


def test_read_auth_profiles_none_when_no_store(tmp_path, _point_home_at):
    """No sqlite, no legacy JSON, no bak → None (the loud-fail marker), never a
    silent empty dict that masquerades as 'configured with zero keys'."""
    home = tmp_path / "bot"
    _seed_agent_dir(home)  # empty agent dir
    _point_home_at(home)

    assert wizard_verify._read_auth_profiles("bot") is None


def test_credential_shape_passes_over_sqlite_store(tmp_path, _point_home_at):
    """End-to-end: the credential-shape check (the consumer) is satisfied by a
    sqlite-backed anthropic profile — no false 'auth-profiles missing'."""
    home = tmp_path / "bot"
    _seed_sqlite_store(_seed_agent_dir(home), {
        "anthropic:api_key": {"provider": "anthropic", "type": "api_key"},
    })
    _point_home_at(home)

    auth = wizard_verify._read_auth_profiles("bot")
    issues = wizard_verify._scan_credential_shape(auth, "claude-sonnet-4-6", {})
    assert issues == []


# ── Check 3: channel handshake ──────────────────────────────────────────────


def test_channels_none_configured_skips(monkeypatch, fake_network):
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})
    fake_network["bots"]["testbot"]["channels"] = {}
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_SKIP


def test_channels_telegram_ok(monkeypatch, fake_network):
    monkeypatch.setattr(
        wizard_verify, "_read_auth_profiles",
        lambda user: {"profiles": {"telegram:atlas:bot_token": {"bot_token": "123:ABC"}}},
    )
    monkeypatch.setattr(
        wizard_verify, "_check_telegram_token",
        lambda tok: (True, "Telegram: @atlas_bot", {"username": "atlas_bot"}),
    )
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK
    assert "@atlas_bot" in (result.detail or "")


def test_channels_telegram_bad_token(monkeypatch, fake_network):
    monkeypatch.setattr(
        wizard_verify, "_read_auth_profiles",
        lambda user: {"profiles": {"telegram:atlas:bot_token": {"bot_token": "bad"}}},
    )
    monkeypatch.setattr(
        wizard_verify, "_check_telegram_token",
        lambda tok: (False, "Telegram: Unauthorized", {"status": 401}),
    )
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "Unauthorized" in (result.detail or "")
    assert result.fix_hint is not None


def test_channels_mixed_some_pass(monkeypatch, fake_network):
    """One channel ok + one bad → overall FAIL (only the bad one in issues)."""
    monkeypatch.setattr(
        wizard_verify, "_read_auth_profiles",
        lambda user: {"profiles": {
            "telegram:atlas:bot_token": {"bot_token": "ok"},
            "slack:bot_token": {"bot_token": "bad"},
        }},
    )
    monkeypatch.setattr(
        wizard_verify, "_check_telegram_token",
        lambda tok: (True, "Telegram: @atlas_bot", {}),
    )
    monkeypatch.setattr(
        wizard_verify, "_check_slack_token",
        lambda tok: (False, "Slack: invalid_auth", {}),
    )
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_FAIL
    # Exactly one issue (slack) — telegram succeeded
    assert len(result.issues) == 1
    assert result.issues[0]["channel"] == "slack"


# ── Token finders ────────────────────────────────────────────────────────────


def test_find_telegram_token_namespaced():
    profiles = {"telegram:atlas:bot_token": {"bot_token": "123:ABC"}}
    assert wizard_verify._find_telegram_token(profiles) == "123:ABC"


def test_find_telegram_token_legacy_shape():
    profiles = {"telegram": {"token": "123:ABC"}}
    assert wizard_verify._find_telegram_token(profiles) == "123:ABC"


def test_find_slack_token_botToken_field():
    profiles = {"slack:atlas": {"botToken": "xoxb-fake"}}
    assert wizard_verify._find_slack_token(profiles) == "xoxb-fake"


def test_find_discord_token():
    profiles = {"discord:atlas:bot_token": {"bot_token": "tok"}}
    assert wizard_verify._find_discord_token(profiles) == "tok"


# ── Check 4 — REMOVED ──────────────────────────────────────────────────────
# The "End-to-end echo" check (arm_e2e_contract / poll_e2e_status /
# disarm_e2e_contract / _strip_e2e_block / _extract_output_text) and
# its tests were removed 2026-06-01. Pairing happens through the
# dedicated wizard modal — see tests/test_routes_pairing.py.

# Pin the removal: anyone re-adding `e2e_echo` to the gauntlet's
# check order would break the install wizard's verify Done logic
# (which now assumes all three checks must pass — no optional row).
def test_check_order_has_no_e2e_echo():
    assert "e2e_echo" not in wizard_verify.CHECK_ORDER
    assert len(wizard_verify.CHECK_ORDER) == 3


def test_run_gauntlet_signature_has_no_e2e_kwargs():
    import inspect
    sig = inspect.signature(wizard_verify.run_gauntlet)
    assert "include_e2e" not in sig.parameters
    assert "e2e_session_id" not in sig.parameters


# ── Status reducer ───────────────────────────────────────────────────────────


def test_overall_status_all_ok():
    checks = [
        wizard_verify.CheckResult(name="a", status=wizard_verify.STATUS_OK, summary=""),
        wizard_verify.CheckResult(name="b", status=wizard_verify.STATUS_OK, summary=""),
    ]
    assert wizard_verify._overall_status(checks) == wizard_verify.STATUS_OK


def test_overall_status_any_fail_wins():
    checks = [
        wizard_verify.CheckResult(name="a", status=wizard_verify.STATUS_OK, summary=""),
        wizard_verify.CheckResult(name="b", status=wizard_verify.STATUS_FAIL, summary=""),
        wizard_verify.CheckResult(name="c", status=wizard_verify.STATUS_PENDING, summary=""),
    ]
    assert wizard_verify._overall_status(checks) == wizard_verify.STATUS_FAIL


def test_overall_status_pending_when_no_fail_warn():
    checks = [
        wizard_verify.CheckResult(name="a", status=wizard_verify.STATUS_OK, summary=""),
        wizard_verify.CheckResult(name="b", status=wizard_verify.STATUS_PENDING, summary=""),
    ]
    assert wizard_verify._overall_status(checks) == wizard_verify.STATUS_PENDING


def test_overall_status_skip_does_not_affect():
    checks = [
        wizard_verify.CheckResult(name="a", status=wizard_verify.STATUS_OK, summary=""),
        wizard_verify.CheckResult(name="b", status=wizard_verify.STATUS_SKIP, summary=""),
    ]
    assert wizard_verify._overall_status(checks) == wizard_verify.STATUS_OK


# ── Safe-run wrapper ─────────────────────────────────────────────────────────


def test_safe_run_check_catches_exceptions():
    def boom():
        raise RuntimeError("kaboom")
    result = wizard_verify._safe_run_check("explosive", boom)
    assert result.status == wizard_verify.STATUS_FAIL
    assert "kaboom" in result.summary


# ── Repair ownership ─────────────────────────────────────────────────────────


def test_repair_ownership_rejects_out_of_tree_path(tmp_path, fake_bot_user, fake_network):
    """Refuse to chown paths not under /Users/<bot>/.openclaw/."""
    bad = tmp_path / "etc" / "passwd"
    bad.parent.mkdir(parents=True)
    bad.write_text("evil")
    result = wizard_verify.repair_ownership("testbot", str(bad), network=fake_network)
    assert result.ok is False
    assert "refusing" in (result.error or "")


def test_repair_ownership_calls_chown(monkeypatch, fake_bot_user, fake_network):
    """Happy path: chown is invoked with -R bot:staff path."""
    # Set up a path inside the expected tree
    target_path = f"/Users/{fake_bot_user}/.openclaw/auth-profiles.json"
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(wizard_verify.subprocess, "run", fake_run)
    # Patch Path.resolve and lstat to not fail on the synthetic path
    monkeypatch.setattr(Path, "resolve", lambda self, **k: self)
    monkeypatch.setattr(Path, "lstat", lambda self: type("_S", (), {"st_uid": 0})())

    result = wizard_verify.repair_ownership("testbot", target_path, network=fake_network)
    assert result.ok is True
    # First call should be chown -R <bot>:staff <path>
    assert calls[0][:3] == ["sudo", "/usr/sbin/chown", "-R"]
    assert calls[0][3] == f"{fake_bot_user}:staff"
    assert calls[0][4] == target_path


# ── _probe_gateway retry loop ───────────────────────────────────────────────
#
# Pre-2026-06-01 the gateway probe was single-shot. The post-provision
# Verify gauntlet hit the gateway during its 5-10s warm-up window when
# /evolve/status hadn't been mounted yet, cached the "not responding"
# failure, and forced the operator to reload the page to retry. The
# retry loop turns that flake into a wait.


def test_probe_gateway_retries_until_success(monkeypatch):
    """First probe fails (gateway still starting); subsequent attempt
    succeeds. _probe_gateway must return the success result, not bail
    on the first failure."""
    monkeypatch.setattr(wizard_verify, "_GATEWAY_PROBE_RETRIES", 3)
    monkeypatch.setattr(wizard_verify, "_GATEWAY_PROBE_RETRY_DELAY_S", 0.0)
    calls = {"n": 0}

    def fake_once(port):
        calls["n"] += 1
        if calls["n"] < 2:
            return False, "probe failed: connection refused"
        return True, "plugin loaded (keys: bot_id,plugin_version,status)"

    monkeypatch.setattr(wizard_verify, "_probe_gateway_once", fake_once)
    loaded, detail = wizard_verify._probe_gateway(19050)
    assert loaded is True
    assert "plugin loaded" in detail
    assert calls["n"] == 2, (
        f"_probe_gateway should retry on transient failure; got "
        f"{calls['n']} probe attempts (expected 2: first fail, then success)"
    )


def test_probe_gateway_returns_last_detail_on_persistent_failure(monkeypatch):
    """All probe attempts fail. _probe_gateway must return the most
    recent failure detail (so the operator sees the freshest error),
    NOT the first failure detail."""
    monkeypatch.setattr(wizard_verify, "_GATEWAY_PROBE_RETRIES", 3)
    monkeypatch.setattr(wizard_verify, "_GATEWAY_PROBE_RETRY_DELAY_S", 0.0)
    calls = {"n": 0}

    def fake_once(port):
        calls["n"] += 1
        return False, f"probe failed (attempt {calls['n']})"

    monkeypatch.setattr(wizard_verify, "_probe_gateway_once", fake_once)
    loaded, detail = wizard_verify._probe_gateway(19050)
    assert loaded is False
    assert "attempt 4" in detail, (
        f"Expected the detail from the final (4th) attempt; got: {detail!r}. "
        f"Returning a stale detail from an earlier attempt would mislead "
        f"the operator about the gateway's current state."
    )
    # 1 initial + 3 retries = 4 attempts.
    assert calls["n"] == 4


def test_probe_gateway_succeeds_first_try_no_extra_calls(monkeypatch):
    """Happy path: gateway is up on the first attempt. _probe_gateway
    must NOT make extra probe calls — that would add a baseline latency
    of _GATEWAY_PROBE_RETRY_DELAY_S × _GATEWAY_PROBE_RETRIES to every
    verify run."""
    monkeypatch.setattr(wizard_verify, "_GATEWAY_PROBE_RETRIES", 3)
    monkeypatch.setattr(wizard_verify, "_GATEWAY_PROBE_RETRY_DELAY_S", 0.0)
    calls = {"n": 0}

    def fake_once(port):
        calls["n"] += 1
        return True, "plugin loaded"

    monkeypatch.setattr(wizard_verify, "_probe_gateway_once", fake_once)
    loaded, _detail = wizard_verify._probe_gateway(19050)
    assert loaded is True
    assert calls["n"] == 1, (
        f"_probe_gateway must short-circuit on first success; got "
        f"{calls['n']} probe attempts"
    )


# ── check_channels reads openclaw.json::channels ────────────────────────────
#
# Pre-2026-06-01 check_channels only checked network.json::bots.<bot>.channels
# and auth-profiles.json for the configured-channel + token. The wizard's
# /api/skills/install/<channel>/set-token endpoints write to
# openclaw.json::channels.<channel> instead, so wizard-installed channels
# showed up as "no channels configured" in the verify gauntlet despite
# being correctly installed and running. Surfaced when a Telegram
# install via the wizard appeared in the All-Set summary but the
# channel-handshake check reported "no messaging channels configured".


def test_check_channels_detects_telegram_via_openclaw_json(monkeypatch, fake_network):
    """Telegram channel configured in openclaw.json::channels — must be
    detected as configured even when network.json + auth-profiles have
    nothing. This is exactly what the wizard's set-token endpoint
    produces."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"telegram": {"botToken": "123:ABC"}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})
    monkeypatch.setattr(
        wizard_verify, "_check_telegram_token",
        lambda tok: (True, "Telegram: @testbot", {"username": "testbot"}),
    )
    fake_network["bots"]["testbot"]["channels"] = {}
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK, (
        f"Channel configured in openclaw.json::channels must register as "
        f"configured. Result: status={result.status}, summary={result.summary!r}"
    )


def test_check_channels_uses_token_from_openclaw_json(monkeypatch, fake_network):
    """The token in openclaw.json::channels.telegram.botToken must be
    passed to _check_telegram_token. Without this, the channel registers
    as configured but the API call uses no token (or the wrong token from
    a stale auth-profiles entry)."""
    captured = {"tok": None}

    def capture_tok(tok):
        captured["tok"] = tok
        return True, "ok", {}

    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"telegram": {"botToken": "from-oc-json:XYZ"}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})
    monkeypatch.setattr(wizard_verify, "_check_telegram_token", capture_tok)
    fake_network["bots"]["testbot"]["channels"] = {}
    wizard_verify.check_channels("testbot", network=fake_network)
    assert captured["tok"] == "from-oc-json:XYZ", (
        f"Token should be sourced from openclaw.json::channels.telegram.botToken; "
        f"got {captured['tok']!r}. The set-token endpoint writes the token here; "
        f"verify must read from the same location."
    )


def test_check_channels_openclaw_json_takes_priority_over_auth_profiles(
    monkeypatch, fake_network,
):
    """When both openclaw.json::channels and auth-profiles have tokens,
    openclaw.json wins. That's the canonical write location post-2026-
    06-01; auth-profiles is the legacy fallback for older wizard versions."""
    captured = {"tok": None}

    def capture_tok(tok):
        captured["tok"] = tok
        return True, "ok", {}

    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"telegram": {"botToken": "winner-from-oc-json"}}},
    )
    monkeypatch.setattr(
        wizard_verify, "_read_auth_profiles",
        lambda user: {"profiles": {"telegram:bot_token": {"bot_token": "loser-from-auth"}}},
    )
    monkeypatch.setattr(wizard_verify, "_check_telegram_token", capture_tok)
    wizard_verify.check_channels("testbot", network=fake_network)
    assert captured["tok"] == "winner-from-oc-json", (
        f"openclaw.json::channels must be the canonical source; got "
        f"{captured['tok']!r}. If auth-profiles wins, stale tokens from "
        f"earlier wizard versions can shadow correct new ones."
    )


# ── WhatsApp channel check (2026-06-04 Phase 1.2) ─────────────────────────────


def test_check_channels_detects_whatsapp_when_account_paired(monkeypatch, fake_network):
    """A WhatsApp account is configured when channels.whatsapp.accounts.<id>
    has a populated authDir. The credential isn't a token; it's the Baileys
    device-link files in authDir."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"whatsapp": {"enabled": True, "accounts": {
            "primary": {"enabled": True, "authDir": "/x/whatsapp/auth"},
        }}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})

    from evolve_admin.skills import whatsapp_install as _wa
    monkeypatch.setattr(
        _wa, "resolve_status",
        lambda bot_id: _wa.InstallStatus(
            bot_id=bot_id, status="active",
            account_id="primary", auth_dir="/x/whatsapp/auth",
            paired_phone="+15551234567",
            oc_probe_ok=True,
        ),
    )

    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK, (
        f"WhatsApp configured with active probe must register OK. "
        f"Got: status={result.status}, summary={result.summary!r}"
    )


def test_check_channels_whatsapp_probe_failure_fails_gauntlet(
    monkeypatch, fake_network,
):
    """If the WhatsApp probe says not-connected, the gauntlet must fail.
    Same load-bearing rule as iMessage — never claim active from config
    presence alone."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"whatsapp": {"enabled": True, "accounts": {
            "primary": {"enabled": True, "authDir": "/x/whatsapp/auth"},
        }}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})

    from evolve_admin.skills import whatsapp_install as _wa
    monkeypatch.setattr(
        _wa, "resolve_status",
        lambda bot_id: _wa.InstallStatus(
            bot_id=bot_id, status="oc_probe_failed",
            account_id="primary", auth_dir="/x/whatsapp/auth",
            oc_probe_ok=False,
            oc_probe_detail="Linked-device session expired on phone",
        ),
    )

    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status != wizard_verify.STATUS_OK
    summary_or_detail = (result.summary or "") + " " + (result.detail or "")
    assert "expired" in summary_or_detail.lower()


def test_check_channels_whatsapp_empty_accounts_skips(monkeypatch, fake_network):
    """A channels.whatsapp block with no accounts (or all without authDir)
    should not count as configured — skip without crashing."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"whatsapp": {"enabled": True, "accounts": {}}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})
    fake_network["bots"]["testbot"]["channels"] = {}
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_SKIP


def test_check_channels_whatsapp_account_without_authdir_skips(
    monkeypatch, fake_network,
):
    """An account block with no authDir field is half-set-up state; it
    must NOT register as configured."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"whatsapp": {"accounts": {
            "primary": {"enabled": True},  # no authDir
        }}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})
    fake_network["bots"]["testbot"]["channels"] = {}
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_SKIP


# ── Signal channel check (2026-06-04 Phase 1.3) ───────────────────────────────


def test_check_channels_detects_signal_when_account_paired(monkeypatch, fake_network):
    """A Signal account is configured when channels.signal.accounts.<number>
    has a populated configDir (the signal-cli linked-device state dir)."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"signal": {"enabled": True, "accounts": {
            "+15551234567": {"enabled": True,
                              "number": "+15551234567",
                              "configDir": "/x/signal/config/15551234567"},
        }}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})

    from evolve_admin.skills import signal_install as _sg
    monkeypatch.setattr(
        _sg, "resolve_status",
        lambda bot_id: _sg.InstallStatus(
            bot_id=bot_id, status="active",
            paired_number="+15551234567",
            config_dir="/x/signal/config/15551234567",
            device_name="evolve-testbot",
            oc_probe_ok=True,
        ),
    )

    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK


def test_check_channels_signal_probe_failure_fails_gauntlet(
    monkeypatch, fake_network,
):
    """If the Signal probe says not-connected, the gauntlet must fail.
    Load-bearing F3 rule: never claim active from config presence alone."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"signal": {"enabled": True, "accounts": {
            "+15551234567": {"enabled": True,
                              "number": "+15551234567",
                              "configDir": "/x/signal/config/15551234567"},
        }}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})

    from evolve_admin.skills import signal_install as _sg
    monkeypatch.setattr(
        _sg, "resolve_status",
        lambda bot_id: _sg.InstallStatus(
            bot_id=bot_id, status="oc_probe_failed",
            paired_number="+15551234567",
            config_dir="/x/signal/config/15551234567",
            oc_probe_ok=False,
            oc_probe_detail="Linked-device session expired on phone",
        ),
    )

    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status != wizard_verify.STATUS_OK
    summary_or_detail = (result.summary or "") + " " + (result.detail or "")
    assert "expired" in summary_or_detail.lower()


def test_check_channels_signal_empty_accounts_skips(monkeypatch, fake_network):
    """A channels.signal block with no accounts should not count as
    configured — skip without crashing."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"signal": {"enabled": True, "accounts": {}}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})
    fake_network["bots"]["testbot"]["channels"] = {}
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_SKIP


def test_check_channels_signal_account_without_configdir_skips(
    monkeypatch, fake_network,
):
    """An account block with no configDir field is half-set-up state
    (number captured but pair not completed); must NOT register as configured."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"signal": {"accounts": {
            "+15551234567": {"enabled": False, "number": "+15551234567"},
        }}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})
    fake_network["bots"]["testbot"]["channels"] = {}
    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_SKIP


# ── iMessage channel check (2026-06-04 bundled-plugin rewire) ─────────────────


def test_check_channels_detects_imessage_when_handle_set(monkeypatch, fake_network):
    """An iMessage channel is considered configured when channels.imessage
    has a handle, regardless of network.json or auth-profiles state. Other
    channels source from a token; iMessage sources from the handle field
    because the credential is a TCC grant, not a token."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"imessage": {"enabled": True,
                                                  "handle": "me@icloud.com"}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})

    # Stub the install resolver to return ``active``
    from evolve_admin.skills import imessage_install as _ii
    monkeypatch.setattr(
        _ii, "resolve_status",
        lambda bot_id: _ii.InstallStatus(
            bot_id=bot_id, status="active",
            tcc_fda_granted=True, tcc_automation_granted=True,
            messages_app_running=True, signed_in=True,
            imessage_handle="me@icloud.com",
            oc_channel_wired=True, oc_plugin_enabled=True, oc_probe_ok=True,
        ),
    )

    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status == wizard_verify.STATUS_OK, (
        f"iMessage configured in openclaw.json::channels with active probe "
        f"must register OK. Got: status={result.status}, summary={result.summary!r}"
    )


def test_check_channels_imessage_probe_failure_fails_gauntlet(
    monkeypatch, fake_network,
):
    """If the iMessage probe says not-connected, the gauntlet must fail.
    The load-bearing rule from the May audit: never claim ``active`` from
    config presence alone — extends to the verification gauntlet too."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"imessage": {"enabled": True,
                                                  "handle": "me@icloud.com"}}},
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})

    from evolve_admin.skills import imessage_install as _ii
    monkeypatch.setattr(
        _ii, "resolve_status",
        lambda bot_id: _ii.InstallStatus(
            bot_id=bot_id, status="oc_probe_failed",
            tcc_fda_granted=True, tcc_automation_granted=True,
            messages_app_running=True, signed_in=True,
            imessage_handle="me@icloud.com",
            oc_channel_wired=True, oc_plugin_enabled=True, oc_probe_ok=False,
            oc_probe_detail="Messages signed out",
        ),
    )

    result = wizard_verify.check_channels("testbot", network=fake_network)
    assert result.status != wizard_verify.STATUS_OK, (
        "iMessage probe failure must fail the gauntlet; got OK"
    )
    # Operator-readable detail surfaces in the summary
    assert "signed out" in (result.summary or "").lower() or \
           "signed out" in (result.detail or "").lower()


def test_check_channels_imessage_unset_handle_not_configured(
    monkeypatch, fake_network,
):
    """channels.imessage block exists but no handle field → treat as
    unconfigured (the wizard will pick that up at install time). The
    gauntlet must not blow up on the empty block."""
    monkeypatch.setattr(
        wizard_verify, "_read_openclaw_json",
        lambda user: {"channels": {"imessage": {"enabled": True}}},  # no handle
    )
    monkeypatch.setattr(wizard_verify, "_read_auth_profiles", lambda user: {})
    fake_network["bots"]["testbot"]["channels"] = {}
    result = wizard_verify.check_channels("testbot", network=fake_network)
    # No iMessage check should have been queued — no exception either
    assert result.status == wizard_verify.STATUS_SKIP, (
        f"Empty channels.imessage block (no handle) must skip the gauntlet, "
        f"not crash. Got status={result.status} summary={result.summary!r}"
    )
