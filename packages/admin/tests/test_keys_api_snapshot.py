"""tests/test_keys_api_snapshot.py — JSON-shape lock for /api/admin/keys/<bot>.

Asserts that the keys API returns byte-identical JSON to the committed
fixtures for synthetic bots covering the credential classes the probes
refactor touches:
  - api_key (anthropic, brave with oc_drift)
  - token_pair (telegram, slack with present + missing fields)
  - oauth (google_workspace wizard-managed + dropbox missing)
  - integration_token (github via .git/config)
  - discovery (extra profile not in seeded registry)

A second scenario (`team_bot_both_google`) covers the Phase 1.5 split: a
bot with BOTH a Gemini api_key (`google:default`) and a Workspace OAuth
profile (`google_workspace_<bot>`) — verifying the two render as separate,
independently-active rows.

Phase 2 scenarios (suffix `-v2`) flip on `integrations.discovery.v2` in
network.json and add the workspace-side artifacts the new probes look at:
  - team_bot_a-v2: same bot, flag on. Output should match the v1 fixture
    aside from the changes the new probes induce on this bot's data
    (none — team_bot_a has no workspace/credentials/, no .env).
  - team_bot_c_synth-v2: simulated-Team_bot_c shape (workspace/credentials/ with
    a service account + token cache + client secret + matching manifest)
    that exercises WorkspaceCredentialsProbe and verifies the Google
    Workspace row renders with VIEW_CONFIG-only affordances (Phase 3
    routes that affordance into the row's `actions` array — only
    "View config" appears, no Reauthorize/Disconnect).
  - team_bot_a_synth-v2: simulated-Team_bot_a shape — Slack token only in
    workspace/.env (no auth-profiles entry). Verifies dotenv match
    drives the Slack row to active without exposing the secret value
    and that the row's `actions` array carries only View-config.

Regenerate the fixture with `UPDATE_SNAPSHOTS=1`; review the diff before
committing. The intent is that any *behavioral* change to the keys API
shows up here as a fixture diff.
"""

from __future__ import annotations

import json
import os
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


_FIXTURES = Path(__file__).parent / "fixtures"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario builders
# ─────────────────────────────────────────────────────────────────────────────


def _scenario_team_bot_a(bot_id: str) -> dict:
    """Comprehensive bot covering all credential classes.

    Adds, vs the bare minimum:
      - google_workspace_<bot> wizard OAuth profile (Workspace active)
      - github remote in .git/config (https_pat form)
      - an extra "made_up_provider" profile to exercise discovery loop
      - a brave key in openclaw.json that drifts from auth-profiles

    Note: NO `google:default` profile, so the Google Gemini row stays
    "missing" — verifying the split renders Gemini and Workspace
    independently (Workspace active, Gemini missing).
    """
    return {
        "openclaw_json": {
            "channels": {"telegram": {"enabled": True, "botToken": "old-tg-token"}},
            "plugins": {
                "entries": {
                    "brave": {
                        "config": {
                            "webSearch": {"apiKey": "oc-drift-brave-key-1234"}
                        }
                    }
                }
            },
            "auth": {"order": {"anthropic": ["anthropic_api_key"]}},
        },
        "auth_profiles": {
            "anthropic_api_key": {
                "provider": "anthropic",
                "type": "api_key",
                "key": "sk-ant-api-test-key-1234567890abcdef",
            },
            "telegram_token_pair": {
                "provider": "telegram",
                "type": "token_pair",
                "bot_token": "tg-bot-token-active-12345678",
                "chat_id": "123456",
            },
            "slack_token_pair": {
                "provider": "slack",
                "type": "token_pair",
                "bot_token": "xoxb-slack-active-12345678",
                # app_token deliberately missing → field "missing"
            },
            "brave_api_key": {
                "provider": "brave",
                "type": "api_key",
                "key": "auth-profiles-brave-key-9999",
            },
            f"google_workspace_{bot_id}": {
                "provider": "google_workspace",
                "type": "oauth",
                "refresh_token": "1//refresh-token-fake",
                "access_token": "ya29.fake-access-token",
                "access_token_expires_at": 9999999999,  # never expires for test
                "google_account": "ops@example.com",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/userinfo.email",
                ],
            },
            "made_up_provider:default": {
                "provider": "made_up_provider",
                "type": "api_key",
                "key": "mu-fake-key-discovered-12345",
            },
        },
        "git_config": (
            '[remote "evolve-backup"]\n'
            f'\turl = https://ghp_FAKE_TOKEN_FOR_TEST_1234@github.com/example/{bot_id.replace("_", "-")}-workspace.git\n'
            '\tfetch = +refs/heads/*:refs/remotes/evolve-backup/*\n'
        ),
    }


def _scenario_team_bot_both_google(bot_id: str) -> dict:
    """Phase 1.5 coverage: bot with both Gemini and Google Workspace active.

    Models the post-split shape from the production survey (e.g., admin_bot in
    docs/design/integration-discovery-survey-2026-05-04.json), where the
    bot has its own Gemini api_key for LLM use *and* an authorized
    Workspace OAuth profile for Drive/Calendar/Gmail. The two must render
    as independent rows: rotating one must not touch the other.
    """
    return {
        "openclaw_json": {
            "auth": {"order": {}},
        },
        "auth_profiles": {
            "google:default": {
                "provider": "google",
                "type": "api_key",
                "key": "AIzaSy-fake-gemini-api-key-1234567890",
            },
            f"google_workspace_{bot_id}": {
                "provider": "google_workspace",
                "type": "oauth",
                "refresh_token": "1//both-bot-refresh-token-fake",
                "access_token": "ya29.both-bot-access-token-fake",
                "access_token_expires_at": 9999999999,
                "google_account": "admin_bot-shape@example.com",
                "scopes": [
                    "https://www.googleapis.com/auth/drive.readonly",
                    "https://www.googleapis.com/auth/calendar.events",
                ],
            },
        },
        "git_config": None,  # no github remote
    }


def _scenario_team_bot_c_synth(bot_id: str) -> dict:
    """Phase 2 simulated-Team_bot_c shape — exercises WorkspaceCredentialsProbe.

    The real team_bot_c bot has plugin-managed Google Workspace credentials at
    `~/.openclaw/workspace/credentials/` (storage shape S3 from the design
    doc). This scenario seeds:
      - one OAuth client secret JSON
      - one OAuth token cache JSON (with refresh_token)
      - one service-account JSON
      - a matching workspace manifest (`google_integration.json`)

    Verifies the Google Workspace row surfaces as active+plugin-managed
    with the VIEW_CONFIG-only affordance routed into `actions[]` (no
    Reauthorize/Disconnect — those would write tokens the running
    plugin doesn't read, per decision B) plus the manifest evidence chip.
    """
    return {
        "openclaw_json": {
            "plugins": {
                "entries": {
                    "google": {"enabled": True},
                },
            },
            "auth": {"order": {}},
        },
        "auth_profiles": {
            # Bot has its own Gemini key (LLM use), unrelated to Workspace.
            "google:default": {
                "provider": "google",
                "type": "api_key",
                "key": "AIzaSy-fake-team_bot_c-gemini-1234567890",
            },
        },
        "git_config": None,
        # Workspace artifacts the new probes consume.
        "workspace_credentials": {
            "client_secret.json": {
                "installed": {
                    "client_id": "111-fake.apps.googleusercontent.com",
                    "project_id": "fake-project",
                    "client_secret": "GOCSPX-fake-secret",
                },
            },
            "token.json": {
                "refresh_token": "1//fake-refresh-token",
                "access_token": "ya29.fake-access-token",
                "expiry": "2030-01-01T00:00:00Z",
                "account": "ranch-ops@example.com",
            },
            "service-account.json": {
                "type": "service_account",
                "project_id": "fake-project",
                "client_email": "ranch-svc@fake-project.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            },
        },
        "workspace_manifests": ["google_integration.json"],
        "network_extras": {"integrations": {"discovery": {"v2": True}}},
    }


def _scenario_team_bot_a_synth(bot_id: str) -> dict:
    """Phase 2 simulated-Team_bot_a shape — exercises DotenvProbe.

    Team_bot_a's Slack token lives in `~/.openclaw/workspace/.env` (no
    auth-profiles entry). Verifies the Slack row renders as active
    without exposing the secret value, and surfaces the env-var name
    presence as evidence with the targeted button-suppression flag.
    """
    return {
        "openclaw_json": {
            "plugins": {
                "entries": {
                    "slack": {"enabled": True},
                },
            },
            "auth": {"order": {}},
        },
        "auth_profiles": {},
        "git_config": None,
        "workspace_dotenv": (
            "# Mixed secrets in the .env — the probe must extract presence\n"
            "# of provider-specific keys without exposing values or unrelated\n"
            "# secrets like database passwords.\n"
            "DATABASE_PASSWORD=should-never-leak\n"
            'SLACK_BOT_TOKEN="xoxb-FAKE-DO-NOT-LEAK-1234"\n'
            "TELEGRAM_BOT_TOKEN=\n"  # empty value — must NOT match
            "OTHER_SECRET=irrelevant\n"
        ),
        "network_extras": {"integrations": {"discovery": {"v2": True}}},
    }


def _scenario_manifest_only(bot_id: str) -> dict:
    """Q2 coverage — bot declares Google Workspace via manifest but has
    zero credentials anywhere.

    The bot's workspace has `google_integration.json` (proving the
    runtime expects Google Workspace to work) but no auth-profiles
    entry, no `~/.config/gws/`, and no plugin-managed credentials.
    Verifies the row renders as "missing" with a
    `manifest_without_credentials` warning so the drift is visible —
    the failure mode this PR was built to surface.
    """
    return {
        "openclaw_json": {
            "plugins": {
                "entries": {
                    "google": {"enabled": True},
                },
            },
            "auth": {"order": {}},
        },
        "auth_profiles": {},
        "git_config": None,
        "workspace_manifests": ["google_integration.json", "gmail_fetcher.json"],
        "network_extras": {"integrations": {"discovery": {"v2": True}}},
    }


_SCENARIOS = {
    "team_bot_a": _scenario_team_bot_a,
    "team_bot_both_google": _scenario_team_bot_both_google,
    "team_bot_c_synth": _scenario_team_bot_c_synth,
    "team_bot_a_synth": _scenario_team_bot_a_synth,
    "manifest_only": _scenario_manifest_only,
}


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _build_bot_tree(tmp_path: Path, bot_id: str, cfg: dict) -> dict:
    """Materialize a synthetic bot tree from a scenario config dict."""
    bot_home = tmp_path / "Users" / bot_id
    oc_dir = bot_home / ".openclaw"
    agent_dir = oc_dir / "agents" / "main" / "agent"
    workspace = oc_dir / "workspace"
    git_dir = workspace / ".git"
    agent_dir.mkdir(parents=True)
    workspace.mkdir(parents=True)
    git_dir.mkdir(parents=True)

    oc_path = oc_dir / "openclaw.json"
    oc_doc = {"agents": {"defaults": {"workspace": str(workspace)}}}
    oc_doc.update(cfg.get("openclaw_json") or {})
    oc_path.write_text(json.dumps(oc_doc, indent=2))

    auth_path = agent_dir / "auth-profiles.json"
    auth_path.write_text(json.dumps(
        {"profiles": cfg.get("auth_profiles") or {}}, indent=2,
    ))

    if cfg.get("git_config"):
        (git_dir / "config").write_text(cfg["git_config"])

    # Phase 2 artifacts: workspace credentials, dotenv, manifests. Built
    # under the bot's user home (under tmp_path) so the redirected sudo
    # probe helpers can find them via the per-bot path indirection below.
    creds = cfg.get("workspace_credentials") or {}
    if creds:
        cred_dir = workspace / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in creds.items():
            (cred_dir / fname).write_text(json.dumps(content, indent=2))
    if cfg.get("workspace_dotenv") is not None:
        (workspace / ".env").write_text(cfg["workspace_dotenv"])
    manifests = cfg.get("workspace_manifests") or []
    if manifests:
        man_dir = workspace / "manifests"
        man_dir.mkdir(parents=True, exist_ok=True)
        for name in manifests:
            (man_dir / name).write_text("{}\n")

    return {
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


@pytest.fixture(params=list(_SCENARIOS.keys()))
def snapshot_bot(request, tmp_path, monkeypatch):
    bot_id = request.param
    cfg = _SCENARIOS[bot_id](bot_id)
    paths_dict = _build_bot_tree(tmp_path, bot_id, cfg)
    workspace_root = Path(paths_dict["workspace"])

    from evolve_admin.web import server as srv

    monkeypatch.setattr(srv, "resolve_bot_paths", lambda b, user=None: paths_dict)

    # Patch the module-level legacy probes so this test doesn't depend on
    # /Users/<bot>/.config/gws or ~/.dropbox/info.json being absent on disk.
    monkeypatch.setattr(srv, "_detect_legacy_gws", lambda b: {
        "present": False, "google_account": None,
        "token_age_days": None, "scopes": [],
    })
    monkeypatch.setattr(srv, "_detect_dropbox_desktop", lambda b: {
        "present": False, "sync_path": None, "subscription_type": None,
        "is_team": False, "account_kind": None, "host_id": None,
    })

    # Phase 2 helpers: redirect to the synthetic workspace under tmp_path.
    # Production reads /Users/<bot>/.openclaw/workspace/...; in tests we
    # bypass that path and run the same classification against the
    # synthetic tree.
    def _list_workspace_credentials_synth(b: str) -> list[dict]:
        cred_dir = workspace_root / "credentials"
        if not cred_dir.is_dir():
            return []
        files: list[dict] = []
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
            files.append({"path": str(p), "kind": kind, "account": account})
        return files

    def _detect_dotenv_synth(b: str, names: tuple[str, ...]) -> list[str]:
        env_path = workspace_root / ".env"
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

    def _list_manifests_synth(b: str) -> list[str]:
        man_dir = workspace_root / "manifests"
        if not man_dir.is_dir():
            return []
        return sorted(p.name for p in man_dir.iterdir() if p.is_file())

    def _list_ssh_synth(b: str) -> list[str]:
        return []

    def _read_gh_hosts_synth(b: str) -> list[str] | None:
        return None

    monkeypatch.setattr(srv, "_list_workspace_credentials", _list_workspace_credentials_synth)
    monkeypatch.setattr(srv, "_detect_workspace_dotenv_keys", _detect_dotenv_synth)
    monkeypatch.setattr(srv, "_list_workspace_manifest_files", _list_manifests_synth)
    monkeypatch.setattr(srv, "_list_user_ssh_private_keys", _list_ssh_synth)
    monkeypatch.setattr(srv, "_read_gh_cli_hosts", _read_gh_hosts_synth)

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
                    src, dst = rest[0], rest[1]
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, dst)
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

    return {
        "bot_id": bot_id,
        "paths": paths_dict,
        "tmp_path": tmp_path,
        "network_extras": cfg.get("network_extras") or {},
    }


@pytest.fixture
def app(tmp_path, snapshot_bot):
    from evolve_admin.web.server import create_app

    network = {
        "bots": {snapshot_bot["bot_id"]: {"user": snapshot_bot["bot_id"]}},
        "podInvariantIntegrations": ["github", "brave"],
    }
    # Phase 2 scenarios merge `integrations.discovery.v2: true` into
    # network.json via `network_extras`. v1 scenarios leave the flag off.
    for k, v in (snapshot_bot.get("network_extras") or {}).items():
        if isinstance(v, dict) and isinstance(network.get(k), dict):
            network[k].update(v)
        else:
            network[k] = v
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


def _normalize_volatile(payload: dict) -> dict:
    """Strip / overwrite fields that legitimately vary across hosts (paths to
    the synthetic tmpdir, pod-default cascade results, refresh-on-render
    timing) so the fixture is portable.
    """
    out = json.loads(json.dumps(payload))  # deep copy
    if "source_path" in out:
        # Fixture replaces the synthetic tmpdir prefix with a marker.
        sp = out["source_path"]
        if "/Users/" in sp and "/.openclaw/" in sp:
            out["source_path"] = "<tmpdir>" + sp[sp.index("/.openclaw/"):]
    # Phase 2: storage_locations on plugin-managed rows include the
    # tmpdir-prefixed credentials paths. Rewrite them to the same
    # `<tmpdir>` marker so the fixture stays portable.
    for row in out.get("keys") or []:
        locs = row.get("storage_locations")
        if isinstance(locs, list):
            row["storage_locations"] = [
                ("<tmpdir>" + p[p.index("/.openclaw/"):])
                if isinstance(p, str) and "/Users/" in p and "/.openclaw/" in p
                else p
                for p in locs
            ]
    # Pod-default github discovery walks the synthetic bot's git config,
    # which is portable, so leave that alone.
    return out


def test_keys_api_snapshot_matches_fixture(app, snapshot_bot):
    bot_id = snapshot_bot["bot_id"]
    snapshot_path = _FIXTURES / f"keys-api-snapshot-{bot_id}.json"

    with app.test_client() as c:
        resp = c.get(f"/api/admin/keys/{bot_id}")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        actual = _normalize_volatile(resp.get_json())

    update = os.environ.get("UPDATE_SNAPSHOTS") == "1"
    if update or not snapshot_path.exists():
        _FIXTURES.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        if not update:
            pytest.skip(f"wrote new snapshot at {snapshot_path}; rerun to assert")
        return

    expected = json.loads(snapshot_path.read_text())
    assert json.dumps(actual, indent=2, sort_keys=True) == json.dumps(
        expected, indent=2, sort_keys=True
    ), (
        f"Keys API JSON shape changed for {bot_id}.\n\n"
        "If this is intentional, regenerate with:\n"
        f"  UPDATE_SNAPSHOTS=1 pytest {Path(__file__).name}::test_keys_api_snapshot_matches_fixture\n"
        "and review the diff before committing."
    )
