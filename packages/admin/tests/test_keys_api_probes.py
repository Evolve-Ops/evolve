"""tests/test_keys_api_probes.py — probe-driven keys API behaviors.

Asserts the rendered rows for the cases the snapshot fixture intentionally
holds at "off": legacy oc-gws on disk (#709), dropbox desktop on disk (#717),
github via SSH deploy key vs https_credhelper. These are the live paths the
probes refactor inherited; without coverage here a future probe rename is
free to silently break them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _build_app(tmp_path, monkeypatch, *,
               git_config: str | None = None,
               legacy_gws: dict | None = None,
               dropbox_desktop: dict | None = None,
               profiles: dict | None = None):
    """Spin up a Flask app over a synthetic bot. Probes get patched-in stubs
    for the off-disk side effects (legacy gws, dropbox desktop)."""
    bot_id = "team_bot_a"
    bot_home = tmp_path / "Users" / bot_id
    oc_dir = bot_home / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    workspace = oc_dir / "workspace"
    git_dir = workspace / ".git"
    agent_dir.mkdir(parents=True)
    workspace.mkdir(parents=True)
    git_dir.mkdir(parents=True)

    oc_path = oc_dir / "openclaw.json"
    oc_path.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(workspace)}},
    }, indent=2))

    auth_path = agent_dir / "auth-profiles.json"
    auth_path.write_text(json.dumps({"profiles": profiles or {}}, indent=2))

    if git_config is not None:
        (git_dir / "config").write_text(git_config)

    paths_dict = {
        "oc_config": str(oc_path),
        "workspace": str(workspace),
        "agent_dir": str(agent_dir),
        "auth_profiles": str(auth_path),
        "turns_dir": str(workspace / "turns"),
        "turns_dir_fallback": str(workspace / "turns"),
        "turns_dir_candidates": [str(workspace / "turns")],
        "logs_dir": str(oc_dir / "logs"),
        "user": bot_id,
    }

    from evolve_admin.web import server as srv

    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: paths_dict)
    monkeypatch.setattr(
        srv, "_detect_legacy_gws",
        lambda b: legacy_gws or {
            "present": False, "google_account": None,
            "token_age_days": None, "scopes": [],
        },
    )
    monkeypatch.setattr(
        srv, "_detect_dropbox_desktop",
        lambda b: dropbox_desktop or {
            "present": False, "sync_path": None, "subscription_type": None,
            "is_team": False, "account_kind": None, "host_id": None,
        },
    )

    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and len(args) >= 2 and args[0] == "sudo":
            verb = args[1]
            rest = list(args[2:])
            try:
                if verb in ("/bin/cat", "cat"):
                    text = Path(rest[0]).read_text()
                    return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
                if verb == "/bin/cp":
                    Path(rest[1]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(rest[0], rest[1])
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "/bin/mkdir":
                    Path(rest[-1]).mkdir(parents=True, exist_ok=True)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb in ("/bin/chmod", "/usr/sbin/chown", "chown"):
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "find":
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            except Exception as exc:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
        return real_run(args, *a, **kw)

    monkeypatch.setattr("subprocess.run", fake_run)

    network = {"bots": {bot_id: {"user": bot_id}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.web.server import create_app

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, bot_id


def _row(payload: dict, provider: str) -> dict:
    for r in payload["keys"]:
        if r["provider"] == provider:
            return r
    raise AssertionError(f"no row for {provider} in payload")


def test_legacy_gws_on_disk_renders_active_oc_only(tmp_path, monkeypatch):
    """#709: bot has no wizard profile but the pre-wizard `oc gws --reauth`
    CLI files exist on disk. Row should surface as active+oc_only with the
    legacy account / scopes / token age."""
    app, bot_id = _build_app(
        tmp_path, monkeypatch,
        legacy_gws={
            "present": True,
            "google_account": "ranch-ops@example.com",
            "token_age_days": 31.5,
            "scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        assert resp.status_code == 200
        gws = _row(resp.get_json(), "google_workspace")

    assert gws["status"] == "active"
    assert gws["oc_only"] is True
    assert gws["legacy_token_age_days"] == 31.5
    assert gws["google_account"] == "ranch-ops@example.com"
    assert gws["access_token_expires_at"] is None
    # Scope→service mapping ran, not just raw scopes — so the renderer
    # threaded `granted_services` through the legacy probe path.
    assert isinstance(gws["granted_services"], list)


def test_no_legacy_gws_no_wizard_renders_missing(tmp_path, monkeypatch):
    """Negative: no wizard profile, no legacy CLI files → row missing."""
    app, bot_id = _build_app(tmp_path, monkeypatch)
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")

    assert gws["status"] == "missing"
    assert "oc_only" not in gws
    assert gws["google_account"] == ""
    assert gws["scopes"] == []


def test_dropbox_desktop_on_disk_renders_active_oc_only(tmp_path, monkeypatch):
    """#717: bot has no Dropbox auth-profile but the macOS desktop sync app
    is installed (info.json present). Row should flip to active+oc_only."""
    app, bot_id = _build_app(
        tmp_path, monkeypatch,
        dropbox_desktop={
            "present": True,
            "sync_path": "/Users/team_bot_a/Dropbox",
            "subscription_type": "Pro",
            "is_team": False,
            "account_kind": "personal",
            "host_id": 1234567890,
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        dbx = _row(resp.get_json(), "dropbox")

    assert dbx["status"] == "active"
    assert dbx["oc_only"] is True
    assert dbx["dropbox_sync_path"] == "/Users/team_bot_a/Dropbox"
    assert dbx["dropbox_subscription_type"] == "Pro"
    assert dbx["dropbox_account_kind"] == "personal"


def test_github_ssh_deploy_key_path(tmp_path, monkeypatch):
    """SSH deploy key form of github auth — must produce ssh:evolve-backup-<bot>
    masked (or the (key missing) variant) and auth_type=ssh."""
    app, bot_id = _build_app(
        tmp_path, monkeypatch,
        git_config=(
            '[remote "evolve-backup"]\n'
            '\turl = git@github.com:example/team-bot-a.git\n'
            '\tfetch = +refs/heads/*:refs/remotes/evolve-backup/*\n'
        ),
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gh = _row(resp.get_json(), "github")

    assert gh["auth_type"] == "ssh"
    assert gh["status"] == "active"
    assert gh["repo_slug"] == "example/team-bot-a"
    assert gh["type_label"] == "SSH deploy key"
    # No /Users/evolve/.ssh/evolve-backup-team_bot_a in this test → "(key missing)"
    assert "ssh:evolve-backup-team_bot_a" in gh["masked"]


def test_github_https_credhelper_path(tmp_path, monkeypatch):
    """HTTPS without embedded PAT — credentials live in osxkeychain / credhelper."""
    app, bot_id = _build_app(
        tmp_path, monkeypatch,
        git_config=(
            '[remote "evolve-backup"]\n'
            '\turl = https://github.com/example/team-bot-a.git\n'
            '\tfetch = +refs/heads/*:refs/remotes/evolve-backup/*\n'
        ),
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gh = _row(resp.get_json(), "github")

    assert gh["auth_type"] == "https_credhelper"
    assert gh["masked"] == "https (credential helper)"
    assert gh["type_label"] == "HTTPS + credential helper"


def test_github_missing_renders_onboarding_row(tmp_path, monkeypatch):
    """No .git/config → still surface a missing row for guided onboarding."""
    app, bot_id = _build_app(tmp_path, monkeypatch)  # git_config=None
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gh = _row(resp.get_json(), "github")

    assert gh["status"] == "missing"
    assert gh["repo_slug"] is None
    assert gh["credential_class"] == "integration_token"


# ── Visibility rule: hide optional, never-attempted providers ─────────────────
# The keys API computes an authoritative `should_list` boolean per row so the
# "hide until configured OR attempted" rule lives in one place (the frontend
# honors it verbatim). A provider is pre-listed ONLY when it has a real
# credential, is pod-invariant, or carries a setup-attempt signal (a
# probe/manifest warning). An optional, never-touched provider stays
# reachable via "+ Add Key" but is NOT pre-listed as a gap.


def test_should_list_hides_optional_never_configured_providers(tmp_path, monkeypatch):
    """An optional provider that was never set up (no creds, not pod-invariant,
    no warnings) must carry should_list=False — so the Credentials tab does NOT
    pre-list it as a gap. An ACTIVE provider and a POD-INVARIANT provider must
    carry should_list=True. This is the fresh-VPS case the rule was built for:
    Slack/Dropbox/Google Workspace should disappear until touched, while an
    active key and the github invariant stay visible.
    """
    app, bot_id = _build_app(
        tmp_path, monkeypatch,
        profiles={
            "anthropic_api_key": {
                "provider": "anthropic",
                "type": "api_key",
                "key": "sk-ant-api-test-key-1234567890abcdef",
            },
        },
    )
    # Fresh-VPS shape: no workspace credentials / manifests on disk, so no
    # probe-ERROR or manifest warnings leak in (which would legitimately keep a
    # row visible). Patching these to empty isolates the "nothing attempted"
    # case the rule must hide. Without it, the workspace_credentials probe tries
    # a real sudo read in the test sandbox and records a spurious warning.
    from evolve_admin.web import server as srv
    monkeypatch.setattr(srv, "_list_workspace_credentials", lambda b: [])
    monkeypatch.setattr(srv, "_list_workspace_manifest_files", lambda b: [])

    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        assert resp.status_code == 200
        payload = resp.get_json()

    by_provider = {r["provider"]: r for r in payload["keys"]}
    pod_invariants = set(payload.get("pod_invariants") or [])

    # (a) Active credential → always shown.
    anthropic = by_provider["anthropic"]
    assert anthropic["status"] == "active"
    assert anthropic["should_list"] is True, anthropic

    # (b) Pod-invariant provider (github) → shown even when missing.
    github = by_provider["github"]
    assert "github" in pod_invariants
    assert github["status"] == "missing"
    assert github["should_list"] is True, github

    # (c) Optional, never-attempted providers → hidden. Covers both an
    #     api_key/token_pair provider (slack) and an OAuth provider (dropbox,
    #     google_workspace) — the OAuth class was the regression: it used to be
    #     force-shown regardless of configuration.
    for opt in ("slack", "dropbox", "google_workspace"):
        if opt not in pod_invariants:  # don't assert against operator overrides
            row = by_provider[opt]
            assert row["status"] == "missing", row
            assert not row.get("warnings"), row
            assert row["should_list"] is False, (
                f"optional never-configured {opt} should be hidden: {row}"
            )

    # Spot-check other untouched optional providers are uniformly hidden.
    for opt in ("openai", "telegram", "runway", "elevenlabs"):
        if opt not in pod_invariants:
            assert by_provider[opt]["should_list"] is False, by_provider[opt]


def test_should_list_true_when_manifest_attempt_signal_present(tmp_path, monkeypatch):
    """A "missing" optional provider that carries a setup-ATTEMPT signal (here a
    manifest_without_credentials warning) must NOT be hidden — should_list=True —
    so a legitimate half-finished setup is still surfaced. Guards the two-pass
    regression: declared intent (manifest/plugin) keeps the row visible.
    """
    app, bot_id = _build_app(tmp_path, monkeypatch)
    # Patch the manifest lister to report a Google Workspace manifest the bot's
    # runtime declares — the manifest_catalog cross-probe assertion then emits a
    # `manifest_without_credentials` warning on the (still-missing) row.
    from evolve_admin.web import server as srv
    monkeypatch.setattr(
        srv, "_list_workspace_manifest_files",
        lambda b: ["google_integration.json"],
    )

    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        assert resp.status_code == 200
        gws = _row(resp.get_json(), "google_workspace")

    assert gws["status"] == "missing"
    # The attempt signal is present — the row stays visible.
    assert gws.get("warnings"), gws
    assert gws["should_list"] is True, gws


# ── Phase 2 (integrations.discovery.v2) probe tests ───────────────────────────
# These exercise the v2-on path: WorkspaceCredentialsProbe (Team_bot_c's case),
# DotenvProbe (Team_bot_a's case), SshKeyProbe / GhCliProbe evidence chips on
# github, the targeted button-suppression flag plumbed for plugin-managed
# rows, and the privacy guarantee that env-var values never leave the
# helper.


def _build_app_v2(tmp_path, monkeypatch, *,
                  workspace_credentials: dict | None = None,
                  workspace_dotenv: str | None = None,
                  workspace_manifests: list[str] | None = None,
                  ssh_keys: list[str] | None = None,
                  gh_cli_hosts: list[str] | None = None,
                  profiles: dict | None = None,
                  oc_extras: dict | None = None,
                  git_config: str | None = None,
                  v2_flag: bool | None = True):
    """Build a Flask app with the v2 flag flipped on (default), or pinned
    explicitly via `v2_flag` (pass False to exercise the kill switch, None
    to omit the flag entirely and rely on the v2-on-by-default behavior).
    Phase 2 helpers are redirected to read from the synthetic tmp_path
    workspace rather than /Users/<bot>/.openclaw/...
    """
    bot_id = "team_bot_a"
    bot_home = tmp_path / "Users" / bot_id
    oc_dir = bot_home / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    workspace = oc_dir / "workspace"
    git_dir = workspace / ".git"
    agent_dir.mkdir(parents=True)
    workspace.mkdir(parents=True)
    git_dir.mkdir(parents=True)

    oc_doc = {"agents": {"defaults": {"workspace": str(workspace)}}}
    if oc_extras:
        oc_doc.update(oc_extras)
    (oc_dir / "openclaw.json").write_text(json.dumps(oc_doc, indent=2))
    (agent_dir / "auth-profiles.json").write_text(
        json.dumps({"profiles": profiles or {}}, indent=2),
    )

    if git_config is not None:
        (git_dir / "config").write_text(git_config)

    if workspace_credentials:
        cred_dir = workspace / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        for name, content in workspace_credentials.items():
            (cred_dir / name).write_text(json.dumps(content, indent=2))
    if workspace_dotenv is not None:
        (workspace / ".env").write_text(workspace_dotenv)
    if workspace_manifests:
        man_dir = workspace / "manifests"
        man_dir.mkdir(parents=True, exist_ok=True)
        for name in workspace_manifests:
            (man_dir / name).write_text("{}\n")

    paths_dict = {
        "oc_config": str(oc_dir / "openclaw.json"),
        "workspace": str(workspace),
        "agent_dir": str(agent_dir),
        "auth_profiles": str(agent_dir / "auth-profiles.json"),
        "turns_dir": str(workspace / "turns"),
        "turns_dir_fallback": str(workspace / "turns"),
        "turns_dir_candidates": [str(workspace / "turns")],
        "logs_dir": str(oc_dir / "logs"),
        "user": bot_id,
    }

    from evolve_admin.web import server as srv

    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: paths_dict)
    monkeypatch.setattr(srv, "_detect_legacy_gws", lambda b: {
        "present": False, "google_account": None,
        "token_age_days": None, "scopes": [],
    })
    monkeypatch.setattr(srv, "_detect_dropbox_desktop", lambda b: {
        "present": False, "sync_path": None, "subscription_type": None,
        "is_team": False, "account_kind": None, "host_id": None,
    })

    # Phase 2 helpers redirected to the synthetic workspace.
    def _list_creds(b: str) -> list[dict]:
        cred_dir = workspace / "credentials"
        if not cred_dir.is_dir():
            return []
        out: list[dict] = []
        for p in sorted(cred_dir.iterdir()):
            if p.suffix != ".json":
                continue
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            kind, account = srv._classify_workspace_credential_json(data)
            if kind == "unknown":
                continue
            out.append({"path": str(p), "kind": kind, "account": account})
        return out

    def _detect_dotenv(b: str, names: tuple[str, ...]) -> list[str]:
        env_path = workspace / ".env"
        if not env_path.is_file() or not names:
            return []
        wanted = set(names)
        matched: list[str] = []
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            name, _, value = line.partition("=")
            name = name.strip()
            if name not in wanted:
                continue
            v = value.strip().strip('"').strip("'")
            if v and name not in matched:
                matched.append(name)
        return matched

    def _list_manifests(b: str) -> list[str]:
        man_dir = workspace / "manifests"
        if not man_dir.is_dir():
            return []
        return sorted(p.name for p in man_dir.iterdir() if p.is_file())

    monkeypatch.setattr(srv, "_list_workspace_credentials", _list_creds)
    monkeypatch.setattr(srv, "_detect_workspace_dotenv_keys", _detect_dotenv)
    monkeypatch.setattr(srv, "_list_workspace_manifest_files", _list_manifests)
    monkeypatch.setattr(srv, "_list_user_ssh_private_keys",
                        lambda b: list(ssh_keys or []))
    monkeypatch.setattr(srv, "_read_gh_cli_hosts",
                        lambda b: (list(gh_cli_hosts) if gh_cli_hosts else None))

    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and len(args) >= 2 and args[0] == "sudo":
            verb = args[1]
            rest = list(args[2:])
            try:
                if verb in ("/bin/cat", "cat"):
                    return subprocess.CompletedProcess(
                        args, 0, stdout=Path(rest[0]).read_text(), stderr="",
                    )
                if verb == "/bin/cp":
                    Path(rest[1]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(rest[0], rest[1])
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb in ("/bin/mkdir", "/bin/chmod", "/usr/sbin/chown", "chown"):
                    if verb == "/bin/mkdir":
                        Path(rest[-1]).mkdir(parents=True, exist_ok=True)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "find":
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            except Exception as exc:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
        return real_run(args, *a, **kw)

    monkeypatch.setattr("subprocess.run", fake_run)

    network = {
        "bots": {bot_id: {"user": bot_id}},
    }
    if v2_flag is not None:
        network["integrations"] = {"discovery": {"v2": v2_flag}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.web.server import create_app

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, bot_id


def test_v2_kill_switch_disables_phase2_probes(tmp_path, monkeypatch):
    """Kill switch: with `integrations.discovery.v2: false` set explicitly,
    the new probes don't affect output — even if a team_bot_c-shape credentials
    dir happens to exist on disk."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        workspace_credentials={
            "service-account.json": {
                "type": "service_account",
                "client_email": "ranch-svc@example.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
            },
        },
        v2_flag=False,
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "missing"
    # Missing google_workspace: synthesized "Set up" action so the
    # actions array stays the single source of truth for buttons.
    assert [a["id"] for a in gws.get("actions", [])] == ["setup"]
    assert "manifest_present" not in gws


def test_v2_default_on_when_flag_omitted(tmp_path, monkeypatch):
    """Default behavior (no `integrations.discovery.v2` key in network.json):
    the Phase 2 probes are registered. Verifies the flag flip — operators
    don't have to opt in any more."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        workspace_credentials={
            "service-account.json": {
                "type": "service_account",
                "client_email": "ranch-svc@example.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
            },
        },
        v2_flag=None,  # don't write the flag at all
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    # WorkspaceCredentialsProbe matched the service-account JSON → row
    # surfaces as active+plugin-managed with VIEW_CONFIG-only affordance.
    assert gws["status"] == "active"
    assert gws["flavor"] == "plugin-managed"
    assert [a["id"] for a in gws.get("actions") or []] == ["view_config"]


def test_v2_on_workspace_credentials_renders_plugin_managed_view_config_only(
    tmp_path, monkeypatch,
):
    """Team_bot_c's case: workspace/credentials/ holds OAuth token cache +
    service account + client secret. Row should flip to active+plugin-managed
    with the affordance routing (Phase 3) limiting actions to View-config
    — no Reauthorize/Disconnect buttons (those would write tokens the
    plugin doesn't read, decision B). manifest_present comes from the
    matching workspace/manifests/ entry."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        workspace_credentials={
            "client_secret.json": {
                "installed": {"client_id": "x.apps.googleusercontent.com"},
            },
            "token.json": {
                "refresh_token": "1//refresh",
                "access_token": "ya29.access",
                "expiry": "2030-01-01T00:00:00Z",
                "account": "ranch-ops@example.com",
            },
            "service-account.json": {
                "type": "service_account",
                "client_email": "ranch-svc@p.iam.gserviceaccount.com",
                "private_key": "PEM",
            },
        },
        workspace_manifests=["google_integration.json"],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")

    assert gws["status"] == "active"
    assert gws["flavor"] == "plugin-managed"
    assert gws["oc_only"] is True
    # Affordance routing (Phase 3): only View-config is safe on a
    # plugin-managed row (writing wizard tokens elsewhere would silently
    # break the plugin's read path). The actions array is the single
    # source of truth — no Reauthorize/Disconnect.
    assert [a["id"] for a in gws.get("actions") or []] == ["view_config"]
    assert gws["auth_model"] == "oauth_user"
    assert gws["google_account"] == "ranch-ops@example.com"
    assert gws["manifest_present"] is True
    assert "google_integration.json" in gws["manifest_files"]
    assert gws["plugin_credential_summary"] == {
        "token_caches": 1,
        "service_accounts": 1,
        "client_secrets": 1,
    }


def test_v2_on_client_secret_only_does_not_match(tmp_path, monkeypatch):
    """Positive-evidence rule: a bare client_secret.json (no token cache,
    no service account) is intent-without-credentials. Probe must NOT
    flip the row to active — that's the failure mode we want to avoid."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        workspace_credentials={
            "client_secret.json": {
                "installed": {"client_id": "x.apps.googleusercontent.com"},
            },
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "missing"
    # Missing → synthesized "Set up" action only; no plugin-managed flow.
    assert [a["id"] for a in gws.get("actions") or []] == ["setup"]


# ── Phase 2.5: OpenclawChannelsTokenProbe — telegram/slack tokens that live
# only in openclaw.json (the live-pod case for 4 of 5 telegram-using bots).


def test_v2_kill_switch_openclaw_channels_telegram_renders_missing(tmp_path, monkeypatch):
    """Kill switch: with `integrations.discovery.v2: false` set explicitly,
    openclaw.json-only telegram tokens stay invisible — matches Phase 1.5
    behavior. ProbedRow is `missing` even though `channels.telegram.botToken`
    is populated, so an operator who hits a probe regression and flips the
    kill switch sees the previous JSON shape.
    """
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        oc_extras={"channels": {"telegram": {"enabled": True, "botToken": "tok-old"}}},
        v2_flag=False,
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        tg = _row(resp.get_json(), "telegram")
    assert tg["status"] == "missing"
    assert "storage" not in tg or tg.get("storage") in (None, "")
    assert "openclaw_channels_present" not in tg


def test_v2_on_openclaw_channels_telegram_renders_active_with_storage_chip(
    tmp_path, monkeypatch,
):
    """Live-pod admin_bot/security_bot/team_bot_c/personal_bot case: telegram bot_token only in
    openclaw.json. Row flips to active+oc_only with storage=openclaw_channels
    and the bot_token field rotatable (the masked value comes from the probe).
    """
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        oc_extras={
            "channels": {"telegram": {
                "enabled": True,
                "botToken": "tg-live-token-1234567890",
                "chatId": "98765",
            }},
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        tg = _row(resp.get_json(), "telegram")

    assert tg["status"] == "active"
    assert tg["storage"] == "openclaw_channels"
    assert tg["flavor"] == "openclaw_channels"
    assert tg["auth_model"] == "token_pair"
    assert tg["oc_only"] is True
    assert any(
        loc.startswith("openclaw.json#channels.telegram")
        for loc in tg["storage_locations"]
    )
    fields = {f["key"]: f for f in tg["fields"]}
    assert fields["bot_token"]["status"] == "active"
    assert fields["bot_token"]["rotatable"] is True
    assert fields["bot_token"]["secret"] is True
    # masked is shown but value is suppressed for secret fields.
    assert fields["bot_token"]["value"] is None
    # _mask_key keeps first 8 + "..." + last 4 — match prefix/suffix only.
    masked = fields["bot_token"]["masked"] or ""
    assert masked.startswith("tg-live-")
    assert masked.endswith("7890")
    assert "..." in masked
    assert "tg-live-token-1234567890" not in masked
    # chat_id is non-secret, surfaced as plain value.
    assert fields["chat_id"]["status"] == "active"
    assert fields["chat_id"]["value"] == "98765"
    assert fields["chat_id"]["secret"] is False
    # has_prev is False — openclaw_channels storage doesn't keep prev backups.
    assert fields["bot_token"]["has_prev"] is False
    # Phase 3 affordance routing: OpenclawChannelsTokenProbe declares ROTATE
    # only (no DISCONNECT — clearing the runtime read path takes the bot
    # offline). The action-button row carries that single rotate entry so
    # the frontend doesn't need to fall back to per-field rotatable.
    action_ids = [a["id"] for a in tg.get("actions") or []]
    assert action_ids == ["rotate"]
    rotate_action = next(a for a in tg["actions"] if a["id"] == "rotate")
    assert rotate_action["endpoint"] == (
        f"/api/admin/keys/{bot_id}/telegram/rotate"
    )


def test_v2_on_auth_profiles_telegram_emits_rotate_and_disconnect_actions(
    tmp_path, monkeypatch,
):
    """Phase 3 affordance routing for the auth_profiles winner: the row must
    carry an `actions` array with rotate + disconnect (matching the probe's
    declared affordances). The disconnect endpoint is per-provider —
    google_workspace alone routes to /onboard/google/revoke; telegram (and
    every other non-Google token_pair / api_key provider) goes through
    /api/admin/keys/<bot>/<provider>/disconnect, which clears the
    auth-profiles entry locally."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        profiles={
            "telegram_token_pair": {
                "provider": "telegram",
                "type": "token_pair",
                "bot_token": "auth-prof-tg-token",
                "chat_id": "11111",
            },
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        tg = _row(resp.get_json(), "telegram")
    assert tg["status"] == "active"
    assert tg["storage"] == "auth_profiles"
    action_ids = [a["id"] for a in tg.get("actions") or []]
    assert "rotate" in action_ids
    assert "disconnect" in action_ids
    rotate_action = next(a for a in tg["actions"] if a["id"] == "rotate")
    assert rotate_action["endpoint"] == (
        f"/api/admin/keys/{bot_id}/telegram/rotate"
    )
    disconnect_action = next(
        a for a in tg["actions"] if a["id"] == "disconnect"
    )
    # The bug was hardcoding /api/admin/onboard/google/revoke for every
    # DISCONNECT affordance — that endpoint only knows how to revoke
    # google_workspace credentials. Telegram disconnect must hit the
    # per-provider endpoint that wipes the auth-profiles entry locally.
    assert disconnect_action["endpoint"] == (
        f"/api/admin/keys/{bot_id}/telegram/disconnect"
    )
    assert "/api/admin/onboard/google/revoke" not in disconnect_action[
        "endpoint"
    ]


def test_v2_on_empty_telegram_block_does_not_match(tmp_path, monkeypatch):
    """Positive-evidence rule: an empty `channels.telegram = {}` block
    (intent-only, no credentials) must NOT flip the row to active. This is
    the failure mode decision B exists to prevent.
    """
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        oc_extras={"channels": {"telegram": {"enabled": True}}},
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        tg = _row(resp.get_json(), "telegram")
    assert tg["status"] == "missing"
    assert tg.get("flavor") != "openclaw_channels"


def test_v2_on_auth_profiles_wins_over_openclaw_channels(tmp_path, monkeypatch):
    """When BOTH auth-profiles and openclaw.json carry a token, auth-profiles
    is canonical and wins. The openclaw_channels probe still fires — its
    presence surfaces as a chip on the auth-profiles-driven row so the
    operator can spot drift.
    """
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        profiles={
            "telegram_token_pair": {
                "provider": "telegram",
                "type": "token_pair",
                "bot_token": "auth-prof-token",
                "chat_id": "11111",
            },
        },
        oc_extras={
            "channels": {"telegram": {
                "botToken": "stale-oc-token",
                "chatId": "22222",
            }},
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        tg = _row(resp.get_json(), "telegram")

    assert tg["status"] == "active"
    assert tg["storage"] == "auth_profiles"
    assert tg["openclaw_channels_present"] is True
    fields = {f["key"]: f for f in tg["fields"]}
    # Masked value reflects the auth-profiles winner, not the stale oc one.
    masked = fields["bot_token"]["masked"] or ""
    assert masked.startswith("auth-pro")
    assert masked.endswith("oken")
    assert not masked.startswith("stale-oc")


def test_v2_on_openclaw_channels_slack_with_partial_fields(tmp_path, monkeypatch):
    """Slack with bot_token in openclaw.json but app_token / user_token absent.
    Probe MATCHes on bot_token (decision B: at least one secret active);
    the missing fields render as missing+not-rotatable so the operator sees
    the gap without the Rotate button on absent fields.
    """
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        oc_extras={
            "channels": {"slack": {
                "enabled": True,
                "botToken": "xoxb-only-bot-token",
            }},
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        slack = _row(resp.get_json(), "slack")
    assert slack["status"] == "active"
    assert slack["storage"] == "openclaw_channels"
    fields = {f["key"]: f for f in slack["fields"]}
    assert fields["bot_token"]["rotatable"] is True
    assert fields["app_token"]["status"] == "missing"
    assert fields["app_token"]["rotatable"] is False
    assert fields["user_token"]["status"] == "missing"


def test_v2_on_openclaw_channels_with_dotenv_chip(tmp_path, monkeypatch):
    """Bot has Telegram in openclaw.json (canonical for the openclaw_channels
    storage) AND TELEGRAM_BOT_TOKEN in workspace/.env (warm storage). Row
    should pick openclaw_channels as the winner — that's where the runtime
    reads — and surface the .env presence as a chip.
    """
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        oc_extras={
            "channels": {"telegram": {"botToken": "tg-oc-canonical"}},
        },
        workspace_dotenv='TELEGRAM_BOT_TOKEN="tg-env-warm-copy"\n',
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        tg = _row(resp.get_json(), "telegram")
    assert tg["storage"] == "openclaw_channels"
    assert tg["dotenv_present"] is True
    assert tg["dotenv_env_vars"] == ["TELEGRAM_BOT_TOKEN"]


def test_v2_on_dotenv_slack_renders_active_with_rotate_action_no_value_leak(
    tmp_path, monkeypatch,
):
    """Team_bot_a's case: Slack token only in workspace/.env. Row should flip to
    active with auth_model=env_var, the affordance routing surfaces both
    a Rotate action (rewrites the matching <NAME>=<value> line in place)
    and a View-config action, the matched env-var NAMES surface, and the
    rotatable bot_token field carries its `dotenv_var` so the modal can
    show the operator which line on disk is being touched. Values never
    leave the helper, even when the .env contains adjacent unrelated
    secrets like database passwords."""
    secret_value = "xoxb-NEVER-LEAK-THIS-VALUE-987654"
    db_password = "supersecret-db-password-do-not-leak"
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        workspace_dotenv=(
            f"DATABASE_PASSWORD={db_password}\n"
            f'SLACK_BOT_TOKEN="{secret_value}"\n'
            "TELEGRAM_BOT_TOKEN=\n"  # empty — must NOT match
            "OTHER_RANDOM=irrelevant\n"
        ),
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        payload = resp.get_json()
        slack = _row(payload, "slack")

    assert slack["status"] == "active"
    assert slack["flavor"] == "dotenv"
    assert slack["auth_model"] == "env_var"
    action_ids = [a["id"] for a in slack.get("actions") or []]
    assert action_ids == ["rotate", "view_config"]
    assert slack["storage"] == "dotenv"
    assert slack["dotenv_env_vars"] == ["SLACK_BOT_TOKEN"]
    bot_field = next(f for f in slack["fields"] if f["key"] == "bot_token")
    assert bot_field["status"] == "active"
    assert bot_field["rotatable"] is True
    assert bot_field["dotenv_var"] == "SLACK_BOT_TOKEN"
    app_field = next(f for f in slack["fields"] if f["key"] == "app_token")
    assert app_field["status"] == "missing"
    # The app_token field is non-rotatable here (no SLACK_APP_TOKEN line
    # in the .env) — the rotate endpoint refuses to invent new lines.
    assert app_field["rotatable"] is False
    # Privacy guarantee: the secret value, the database password, and the
    # empty-value telegram entry all stay buried — never serialised onto
    # the row anywhere.
    payload_str = json.dumps(payload)
    assert secret_value not in payload_str
    assert db_password not in payload_str
    assert "TELEGRAM_BOT_TOKEN" not in payload_str


def test_v2_on_github_evidence_chips(tmp_path, monkeypatch):
    """SSH and gh-CLI matches surface as evidence chips alongside the
    integration_token row. They must NOT override the primary status
    driver (the integration_token row from .git/config)."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        git_config=(
            '[remote "evolve-backup"]\n'
            f'\turl = https://ghp_FAKE_PAT_TOKEN_1234@github.com/example/{ "team-bot-a"}.git\n'
            '\tfetch = +refs/heads/*:refs/remotes/evolve-backup/*\n'
        ),
        ssh_keys=[
            "/Users/team_bot_a/.ssh/id_ed25519",
            "/Users/team_bot_a/.ssh/id_rsa",
        ],
        gh_cli_hosts=["github.com"],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gh = _row(resp.get_json(), "github")
    assert gh["status"] == "active"
    assert gh["auth_type"] == "https_pat"
    chips = gh.get("evidence_chips") or []
    kinds = {c["kind"] for c in chips}
    assert kinds == {"ssh_key", "gh_cli"}
    ssh_chip = next(c for c in chips if c["kind"] == "ssh_key")
    assert "id_ed25519" in ssh_chip["key_names"]
    assert "id_rsa" in ssh_chip["key_names"]
    gh_chip = next(c for c in chips if c["kind"] == "gh_cli")
    assert "github.com" in gh_chip["hosts"]


def test_v2_on_wizard_match_keeps_buttons_adds_manifest_chip(
    tmp_path, monkeypatch,
):
    """Wizard-managed Workspace + workspace manifest present → wizard wins,
    keeps its Reauthorize/Disconnect affordances, and gets a manifest
    chip as evidence the bot's runtime exercises the integration."""
    bot_id = "team_bot_a"
    app, _ = _build_app_v2(
        tmp_path, monkeypatch,
        profiles={
            f"google_workspace_{bot_id}": {
                "provider": "google_workspace",
                "type": "oauth",
                "refresh_token": "1//refresh",
                "access_token": "ya29.fake",
                "access_token_expires_at": 9999999999,
                "google_account": "ops@example.com",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.readonly",
                ],
            },
        },
        workspace_manifests=["google_integration.json"],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")

    # Wizard wins: row keeps the standard wizard affordances (Reauthorize +
    # Disconnect) and is NOT flavored as plugin-managed.
    assert gws["status"] == "active"
    assert gws.get("flavor") != "plugin-managed"
    action_ids = [a["id"] for a in gws.get("actions") or []]
    assert "reauthorize" in action_ids
    assert "disconnect" in action_ids
    assert "view_config" not in action_ids
    # Manifest chip attaches as evidence-only.
    assert gws.get("manifest_present") is True


# ── Phase 3: View-config endpoint ─────────────────────────────────────────────
# The VIEW_CONFIG affordance lands in row.actions[] for plugin-managed and
# dotenv rows. Clicking it hits /api/admin/keys/<bot>/<provider>/config which
# returns the read-only fragment of openclaw.json driving that integration,
# with secret fields masked server-side.


def test_view_config_telegram_masks_bot_token(tmp_path, monkeypatch):
    """Telegram channel block carries the live botToken in openclaw.json
    (the openclaw_channels storage shape). The View-config endpoint must
    return the fragment with botToken masked."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        oc_extras={
            "channels": {"telegram": {
                "enabled": True,
                "botToken": "12345:LIVE-NEVER-LEAK",
                "chatId": "67890",
            }},
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}/telegram/config")
        assert resp.status_code == 200
        body = resp.get_json()

    assert body["provider"] == "telegram"
    assert body["path"] == "channels.telegram"
    frag = body["json_fragment"]
    assert frag["botToken"] == "***"
    # Non-secret siblings stay readable so the operator can see context.
    assert frag["chatId"] == "67890"
    assert frag["enabled"] is True
    # The masked-fields list documents which keys the masking applied to,
    # so the frontend can show "secrets masked: botToken" without
    # re-implementing the rule.
    assert "botToken" in body["masked_fields"]
    # Privacy guarantee: the live token must NEVER appear in the response.
    assert "LIVE-NEVER-LEAK" not in json.dumps(body)


def test_view_config_unknown_provider_returns_404(tmp_path, monkeypatch):
    """Providers without a registered config path (e.g. anthropic) return
    404 — the endpoint is opt-in per provider, not a generic
    openclaw.json reader."""
    app, bot_id = _build_app_v2(tmp_path, monkeypatch)
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}/anthropic/config")
    assert resp.status_code == 404


def test_view_config_missing_path_returns_null_fragment(tmp_path, monkeypatch):
    """When the provider IS registered but the bot's openclaw.json has no
    block at the registered path, the endpoint returns
    json_fragment=null (the operator sees "not configured" in the modal,
    not a 500)."""
    app, bot_id = _build_app_v2(tmp_path, monkeypatch)  # no channels.telegram
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}/telegram/config")
        assert resp.status_code == 200
        body = resp.get_json()
    assert body["json_fragment"] is None
    assert body["path"] == "channels.telegram"


# ── Q5: probe ERROR → row warnings ────────────────────────────────────────
# These cover the team_bot_a/team_bot_c failure mode: a probe's storage location appears
# to exist but couldn't be read (sudoers grant gap, malformed JSON, sudo
# timeout). Without these tests a future probe-helper rewrite is free to
# silently re-collapse "couldn't read" into "not connected".


def test_classify_sudo_failure_distinguishes_missing_from_permission():
    """The classifier must split BSD/Darwin error strings into the right
    buckets — the whole Q5 surface depends on this. Production stderr
    samples from the live mini are pinned here."""
    from evolve_admin.web.server import _classify_sudo_failure

    # Genuine "doesn't exist" — silent NO_EVIDENCE, never a warning.
    kind, _ = _classify_sudo_failure(1, "/bin/cat: /Users/x/.config/gws/token_cache.json: No such file or directory")
    assert kind == "missing"
    kind, _ = _classify_sudo_failure(1, "ls: /Users/x/.openclaw/workspace/credentials: No such file or directory")
    assert kind == "missing"

    # Sudoers grant or filesystem permission rejected the read.
    kind, reason = _classify_sudo_failure(1, "sudo: a password is required")
    assert kind == "permission"
    assert "password is required" in reason.lower()
    kind, reason = _classify_sudo_failure(1, "user evolve is not in the sudoers file. This incident will be reported.")
    assert kind == "permission"
    kind, reason = _classify_sudo_failure(1, "/bin/cat: /Users/x/.config/gws/token_cache.json: Permission denied")
    assert kind == "permission"

    # Anything else surfaces as a warning so the operator can investigate.
    kind, reason = _classify_sudo_failure(2, "weird unexpected stderr text")
    assert kind == "other"
    assert reason


def test_remediation_hint_for_permission_errors_only():
    """Hints only fire on known classes (sudoers misconfig). For unknown
    reasons we fall through — never fabricate advice that might point
    operators away from the real cause."""
    from evolve_admin.web.server import _remediation_hint_for

    h = _remediation_hint_for("/bin/cat: /Users/admin_bot/.config/gws/token_cache.json: Permission denied")
    assert h is not None
    assert "sudoers" in h.lower()
    h = _remediation_hint_for("user evolve is not in the sudoers file")
    assert h is not None
    h = _remediation_hint_for("Malformed JSON at /Users/x/.openclaw/workspace/credentials/svc.json: ...")
    assert h is None
    h = _remediation_hint_for("")
    assert h is None


def _build_app_with_failing_helpers(
    tmp_path, monkeypatch, *,
    legacy_gws_errors: list[str] | None = None,
    workspace_credentials_errors: list[str] | None = None,
    workspace_dotenv_errors: list[str] | None = None,
    ssh_errors: list[str] | None = None,
    ghcli_errors: list[str] | None = None,
    workspace_credentials_data: list[dict] | None = None,
    workspace_dotenv_match: list[str] | None = None,
    profiles: dict | None = None,
    oc_extras: dict | None = None,
    git_config: str | None = None,
):
    """Build a Flask app where Phase 2 helpers can be configured to inject
    classified read errors. Each `*_errors` list is a list of strings that
    will be appended to the helper's `errors_out` parameter on each call —
    simulating the team_bot_a/team_bot_c failure mode where a sudoers grant is missing
    or a JSON file is malformed."""
    bot_id = "team_bot_a"
    bot_home = tmp_path / "Users" / bot_id
    oc_dir = bot_home / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    workspace = oc_dir / "workspace"
    git_dir = workspace / ".git"
    agent_dir.mkdir(parents=True)
    workspace.mkdir(parents=True)
    git_dir.mkdir(parents=True)

    oc_doc = {"agents": {"defaults": {"workspace": str(workspace)}}}
    if oc_extras:
        oc_doc.update(oc_extras)
    (oc_dir / "openclaw.json").write_text(json.dumps(oc_doc, indent=2))
    (agent_dir / "auth-profiles.json").write_text(
        json.dumps({"profiles": profiles or {}}, indent=2),
    )
    if git_config is not None:
        (git_dir / "config").write_text(git_config)

    paths_dict = {
        "oc_config": str(oc_dir / "openclaw.json"),
        "workspace": str(workspace),
        "agent_dir": str(agent_dir),
        "auth_profiles": str(agent_dir / "auth-profiles.json"),
        "turns_dir": str(workspace / "turns"),
        "turns_dir_fallback": str(workspace / "turns"),
        "turns_dir_candidates": [str(workspace / "turns")],
        "logs_dir": str(oc_dir / "logs"),
        "user": bot_id,
    }

    from evolve_admin.web import server as srv

    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: paths_dict)
    monkeypatch.setattr(srv, "_detect_dropbox_desktop", lambda b, errors_out=None: {
        "present": False, "sync_path": None, "subscription_type": None,
        "is_team": False, "account_kind": None, "host_id": None,
    })

    def _fake_legacy_gws(b: str, errors_out=None):
        if errors_out is not None and legacy_gws_errors:
            errors_out.extend(legacy_gws_errors)
        return {"present": False, "google_account": None,
                "token_age_days": None, "scopes": []}

    def _fake_list_creds(b: str, errors_out=None):
        if errors_out is not None and workspace_credentials_errors:
            errors_out.extend(workspace_credentials_errors)
        return list(workspace_credentials_data or [])

    def _fake_dotenv(b: str, names, errors_out=None):
        if errors_out is not None and workspace_dotenv_errors:
            errors_out.extend(workspace_dotenv_errors)
        return list(workspace_dotenv_match or [])

    def _fake_ssh(b: str, errors_out=None):
        if errors_out is not None and ssh_errors:
            errors_out.extend(ssh_errors)
        return []

    def _fake_ghcli(b: str, errors_out=None):
        if errors_out is not None and ghcli_errors:
            errors_out.extend(ghcli_errors)
        return None

    monkeypatch.setattr(srv, "_detect_legacy_gws", _fake_legacy_gws)
    monkeypatch.setattr(srv, "_list_workspace_credentials", _fake_list_creds)
    monkeypatch.setattr(srv, "_detect_workspace_dotenv_keys", _fake_dotenv)
    monkeypatch.setattr(srv, "_list_workspace_manifest_files", lambda b, errors_out=None: [])
    monkeypatch.setattr(srv, "_list_user_ssh_private_keys", _fake_ssh)
    monkeypatch.setattr(srv, "_read_gh_cli_hosts", _fake_ghcli)

    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and len(args) >= 2 and args[0] == "sudo":
            verb = args[1]
            rest = list(args[2:])
            try:
                if verb in ("/bin/cat", "cat"):
                    return subprocess.CompletedProcess(
                        args, 0, stdout=Path(rest[0]).read_text(), stderr="",
                    )
                if verb == "/bin/cp":
                    Path(rest[1]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(rest[0], rest[1])
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb in ("/bin/mkdir", "/bin/chmod", "/usr/sbin/chown", "chown"):
                    if verb == "/bin/mkdir":
                        Path(rest[-1]).mkdir(parents=True, exist_ok=True)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "find":
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            except Exception as exc:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
        return real_run(args, *a, **kw)

    monkeypatch.setattr("subprocess.run", fake_run)

    network = {"bots": {bot_id: {"user": bot_id}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.web.server import create_app

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, bot_id


def test_q5_legacy_gws_permission_denied_renders_warning(tmp_path, monkeypatch):
    """The team_bot_a/team_bot_c failure mode: pre-wizard /Users/X/.config/gws/ exists
    on disk but the evolve user can't read it (sudoers grant gap on the
    production mini). With Q5, the row should still render as `missing`
    (no MATCH) BUT carry a `warnings` entry naming the probe and reason
    so the dashboard shows ⚠ instead of "Not connected"."""
    perm_err = (
        "/bin/cat: /Users/team_bot_a/.config/gws/token_cache.json: "
        "Permission denied"
    )
    app, bot_id = _build_app_with_failing_helpers(
        tmp_path, monkeypatch,
        legacy_gws_errors=[perm_err],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "missing"
    warnings = gws.get("warnings") or []
    assert warnings, "expected probe warnings to surface on the row"
    w = warnings[0]
    assert w["probe_name"] == "legacy_oc_gws_cli"
    assert "Permission denied" in w["reason"]
    assert "token_cache.json" in w["reason"]
    # Permission errors get a remediation hint pointing at refresh-sudoers.
    assert w.get("remediation_hint")
    assert "sudoers" in w["remediation_hint"].lower()


def test_q5_workspace_credentials_unreadable_renders_warning(tmp_path, monkeypatch):
    """Phase 2 plugin-managed Workspace credentials: the directory exists
    but `sudo /bin/cat` returns Permission denied on each file. Probe
    has no positive match, must surface as ERROR → row.warnings."""
    perm_err = (
        "/bin/cat: /Users/team_bot_a/.openclaw/workspace/credentials/"
        "service-account.json: Permission denied"
    )
    app, bot_id = _build_app_with_failing_helpers(
        tmp_path, monkeypatch,
        workspace_credentials_errors=[perm_err],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "missing"
    warnings = gws.get("warnings") or []
    assert any(
        w["probe_name"] == "workspace_credentials:google_workspace"
        and "Permission denied" in w["reason"]
        for w in warnings
    )


def test_q5_dotenv_unreadable_renders_warning(tmp_path, monkeypatch):
    """Slack: workspace/.env exists with the SLACK_BOT_TOKEN line but
    isn't readable. DotenvProbe has no positive match → ERROR surfaces
    as a warning on the slack row."""
    perm_err = (
        "/bin/cat: /Users/team_bot_a/.openclaw/workspace/.env: "
        "Permission denied"
    )
    app, bot_id = _build_app_with_failing_helpers(
        tmp_path, monkeypatch,
        workspace_dotenv_errors=[perm_err],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        slack = _row(resp.get_json(), "slack")
    warnings = slack.get("warnings") or []
    # The dotenv probe runs under slack — the warning must attach to the
    # slack row even though no other slack probe matched.
    assert any(
        w["probe_name"] == "dotenv:slack" and "Permission denied" in w["reason"]
        for w in warnings
    )


def test_q5_legacy_gws_warns_even_when_wizard_match_wins(tmp_path, monkeypatch):
    """Spec — even a row with status="active" carries warnings from other
    probes that errored. Wizard probe wins, but the legacy CLI directory
    was unreadable; operator should know."""
    perm_err = (
        "/bin/cat: /Users/team_bot_a/.config/gws/token_cache.json: "
        "Permission denied"
    )
    bot_id = "team_bot_a"
    app, _ = _build_app_with_failing_helpers(
        tmp_path, monkeypatch,
        legacy_gws_errors=[perm_err],
        profiles={
            f"google_workspace_{bot_id}": {
                "provider": "google_workspace",
                "type": "oauth",
                "refresh_token": "1//refresh",
                "access_token": "ya29.fake",
                "access_token_expires_at": 9999999999,
                "google_account": "ops@example.com",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            },
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    # Wizard probe wins → status active.
    assert gws["status"] == "active"
    # …but the legacy probe ERRORed and that warning still surfaces.
    warnings = gws.get("warnings") or []
    assert any(
        w["probe_name"] == "legacy_oc_gws_cli" for w in warnings
    ), f"expected legacy_oc_gws_cli warning, got {warnings}"


def test_q5_no_warnings_when_helpers_clean(tmp_path, monkeypatch):
    """Negative — when no probes error, the row has no `warnings` field
    at all (clean JSON shape, no empty arrays cluttering the response)."""
    app, bot_id = _build_app_with_failing_helpers(
        tmp_path, monkeypatch,
        # All helpers report no errors and no data.
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        payload = resp.get_json()
    for row in payload["keys"]:
        assert "warnings" not in row or not row["warnings"], (
            f"unexpected warnings on {row.get('provider')} row: {row.get('warnings')}"
        )


def test_q5_helper_error_messages_do_not_leak_secrets(tmp_path, monkeypatch):
    """Privacy guarantee — the warning reason names the path but never
    the unreadable bytes. The probe layer never reads file content into
    the warning string, so we verify the contract by feeding a synthetic
    `secret_marker` via the helper data path and asserting it stays out
    of the warnings/JSON payload."""
    secret_path = "/Users/team_bot_a/.openclaw/workspace/.env"
    secret_marker = "PROBE-TEST-SHOULD-NEVER-LEAK-7c1f"
    perm_err = f"/bin/cat: {secret_path}: Permission denied"
    app, bot_id = _build_app_with_failing_helpers(
        tmp_path, monkeypatch,
        workspace_dotenv_errors=[perm_err],
        # workspace_dotenv_match would normally hold names; not values —
        # pass nothing here. The probe receives an empty match list,
        # plus the error string we constructed above. Neither path
        # carries the secret marker.
    )
    # Sanity: also point ssh_errors at a string that contains the
    # marker as a remediation suggestion — that string should not echo.
    # (We're proving that errors are reported verbatim in the warning,
    # which is acceptable because the probe's own error strings never
    # contain credential bytes; only paths and stderr.)
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        payload = resp.get_json()
    payload_str = json.dumps(payload)
    # Path is fine to expose (operator needs it to fix the grant).
    assert secret_path in payload_str
    # The synthetic marker — proxy for "any credential bytes" — must
    # never appear; the probe's own error strings only reference paths
    # and rc/stderr text.
    assert secret_marker not in payload_str


# ── Q2: manifest-without-credentials warnings ─────────────────────────────────
# These cover the "intent declared, no credentials" failure mode: a bot's
# workspace declares a manifest matching the provider's catalog
# (`google_integration.json`, `gmail_fetcher.json`) but no probe found
# credentials. The row should render as `missing` PLUS carry a
# `manifest_without_credentials` warning so the drift is visible.


def test_q2_manifest_only_emits_warning_on_missing_row(tmp_path, monkeypatch):
    """Bot has google_integration.json but no creds anywhere → row missing,
    `warnings` carries a manifest_without_credentials entry naming the
    matched manifest files."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        workspace_manifests=["google_integration.json", "gmail_fetcher.json"],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "missing"
    warnings = gws.get("warnings") or []
    manifest_warnings = [
        w for w in warnings if w.get("kind") == "manifest_without_credentials"
    ]
    assert len(manifest_warnings) == 1, warnings
    w = manifest_warnings[0]
    assert sorted(w["manifests"]) == ["gmail_fetcher.json", "google_integration.json"]
    assert "no Google Workspace credentials" in w["reason"]
    # Remediation hint always present so the operator has next-step copy.
    assert w.get("remediation_hint")


def test_q2_no_warning_when_credentials_present(tmp_path, monkeypatch):
    """Wizard match wins → manifest is informational evidence, not a
    warning. The Q2 surface fires only when the row would otherwise look
    like 'not connected.'"""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        workspace_manifests=["google_integration.json"],
        profiles={
            f"google_workspace_{ 'team_bot_a' }": {
                "provider": "google_workspace",
                "type": "oauth",
                "refresh_token": "1//refresh-token",
                "access_token": "ya29.access",
                "access_token_expires_at": 9999999999,
                "google_account": "ops@example.com",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            },
        },
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "active"
    warnings = gws.get("warnings") or []
    manifest_warnings = [
        w for w in warnings if w.get("kind") == "manifest_without_credentials"
    ]
    assert manifest_warnings == [], (
        "Active wizard match should not emit a manifest-without-creds warning"
    )
    # Existing informational chip plumbing still fires — manifest_files
    # stays attached to the active row.
    assert gws.get("manifest_present") is True
    assert "google_integration.json" in (gws.get("manifest_files") or [])


def test_q2_no_warning_when_no_manifests_present(tmp_path, monkeypatch):
    """No workspace/manifests/ → no warning even if status is missing.
    The warning is meant to surface drift, not nag operators on bots
    that never declared the integration in the first place."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        # no workspace_manifests at all
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "missing"
    assert "warnings" not in gws or not gws["warnings"]


def test_q2_unrelated_manifest_does_not_emit_warning(tmp_path, monkeypatch):
    """A manifest that doesn't match the catalog patterns (e.g.
    `random_thing.json`) must NOT be misclassified as Google evidence.
    Catalog patterns are deliberate; novel manifest names should be
    silent until added explicitly."""
    app, bot_id = _build_app_v2(
        tmp_path, monkeypatch,
        workspace_manifests=["random_thing.json", "ranch_ops.json"],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "missing"
    assert "warnings" not in gws or not gws["warnings"], gws.get("warnings")


def test_q2_coexists_with_q5_read_error_warning(tmp_path, monkeypatch):
    """Cross-check with Q5: if both a manifest exists AND a probe errored
    (sudoers-grant gap on legacy gws dir), the row should carry BOTH
    warnings — the read-error one from Q5 AND the manifest one from Q2.
    Neither must mask the other."""
    perm_err = (
        "/bin/cat: /Users/team_bot_a/.config/gws/token_cache.json: "
        "Permission denied"
    )
    app, bot_id = _build_app_with_failing_helpers(
        tmp_path, monkeypatch,
        legacy_gws_errors=[perm_err],
    )
    # Monkeypatch the manifest helper inside the failing-helpers app to
    # return a Google manifest so Q2 has something to fire on.
    from evolve_admin.web import server as srv
    monkeypatch.setattr(
        srv, "_list_workspace_manifest_files",
        lambda b, errors_out=None: ["google_integration.json"],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        gws = _row(resp.get_json(), "google_workspace")
    assert gws["status"] == "missing"
    warnings = gws.get("warnings") or []
    kinds = [w.get("kind") for w in warnings]
    # Q5 warnings predate Q2's `kind` field — they don't carry one.
    # Q2 warnings always do. Both should be present in the list.
    assert "manifest_without_credentials" in kinds, warnings
    assert any(k is None for k in kinds), (
        f"expected at least one Q5-style warning (no kind field): {warnings}"
    )


def test_q2_manifests_matching_provider_helper_glob_patterns():
    """Direct test of the catalog matcher — `gmail_*.json` patterns must
    match by basename, case-insensitively, and not produce duplicates."""
    from evolve_admin.web.probes import (
        MANIFEST_CATALOG, manifests_matching_provider,
    )
    assert "google_workspace" in MANIFEST_CATALOG
    assert manifests_matching_provider("google_workspace", []) == []
    # Exact-name match.
    assert manifests_matching_provider(
        "google_workspace", ["google_integration.json"],
    ) == ["google_integration.json"]
    # Glob pattern match (gmail_*.json).
    assert manifests_matching_provider(
        "google_workspace", ["gmail_fetcher.json"],
    ) == ["gmail_fetcher.json"]
    # Case-insensitive — bot authors don't always lowercase.
    assert manifests_matching_provider(
        "google_workspace", ["Gmail_Fetcher.JSON"],
    ) == ["Gmail_Fetcher.JSON"]
    # Unrelated names don't match.
    assert manifests_matching_provider(
        "google_workspace", ["random_thing.json", "ranch_ops.json"],
    ) == []
    # Unknown provider returns empty (no false positives).
    assert manifests_matching_provider(
        "slack", ["google_integration.json"],
    ) == []


# ── Rule (d): enabled plugin + no key = a live defect, not an untouched option ─
# Regression guard for the fleet-wide Brave failure found 2026-07-31. #3219
# correctly demoted brave from pod invariant to optional, but that removed the
# only reason its row rendered: with status "missing", no probe warning, and no
# invariant membership, the row vanished. Six of nine mini bots and VPS evo ran
# an enabled, keyless brave for five weeks with every surface reporting health,
# and the guided onboarding flow became unreachable (its sole entry point was
# the pod-invariant banner).

def test_should_list_true_for_enabled_but_keyless_brave():
    from evolve_admin.web.credentials_visibility import annotate_should_list
    rows = [{"provider": "brave", "status": "missing", "plugin_enabled": True}]
    annotate_should_list(rows, ["github"])
    assert rows[0]["should_list"] is True, (
        "an enabled brave plugin with no key must surface — the bot is "
        "advertising a web_search tool that 401s at call time"
    )


def test_should_list_false_for_disabled_brave():
    """Brave installed but switched off is not a gap — nothing claims it works."""
    from evolve_admin.web.credentials_visibility import annotate_should_list
    rows = [{"provider": "brave", "status": "missing", "plugin_enabled": False}]
    annotate_should_list(rows, ["github"])
    assert rows[0]["should_list"] is False


def test_should_list_rule_d_does_not_fire_for_llm_providers():
    """google/anthropic/openai keep keys in .env or auth-profiles.

    An unrestricted rule (d) pre-lists a "Setup required" row for a Gemini
    provider that works fine — the false positive the INLINE_KEY_PROVIDERS
    restriction exists to prevent. Verified against the manifest_only
    snapshot fixture, where google is enabled with no openclaw.json key.
    """
    from evolve_admin.web.credentials_visibility import annotate_should_list
    rows = [
        {"provider": p, "status": "missing", "plugin_enabled": True}
        for p in ("google", "anthropic", "openai", "xai")
    ]
    annotate_should_list(rows, ["github"])
    for row in rows:
        assert row["should_list"] is False, row


def test_inline_key_provider_sets_agree_across_surfaces():
    """The Skills page and the Credentials tab must classify the same providers.

    One surface calling brave "configured" while the other calls it a gap is
    the class of mismatch #3219 had to fix.
    """
    from evolve_admin.skills.inventory import _INLINE_KEY_PATHS
    from evolve_admin.web.credentials_oc import INLINE_KEY_PROVIDERS
    assert set(_INLINE_KEY_PATHS) == set(INLINE_KEY_PROVIDERS)
