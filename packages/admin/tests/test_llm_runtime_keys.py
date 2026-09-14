"""LLM keys in OC's per-agent runtime store — probe, visibility, rotation.

Brief: ``internal/dispatch/done/llm-keys-visible-and-rotatable-from-runtime-
store.md``. Operator finding 2026-09-04: the PoC personal bot's Plugins tab
showed four LLM providers "Enabled", the Anthropic console showed $36
month-to-date, and the Credentials tab had no LLM Providers section at all,
because every probe looked in ``auth-profiles.json`` / the workspace
``.env`` / ``openclaw.json`` and the keys were in none of those.

The fixture tree in this file is the shape scanned off the live pod: TWO
agents (``main`` and ``email-reader``) each carrying a copy of the same key,
which is precisely why "rotate" has to write more than one file.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import oc_agent_keys as ak  # noqa: E402
from evolve_admin.web import oc_agent_keys_io as akio  # noqa: E402

ANTHROPIC_KEY = "sk-ant-api03-FIXTUREKEYplaceholder-not-a-real-credential-AAAA"
XAI_KEY = "xai-FIXTUREKEYplaceholder-not-a-real-credential-BBBB"
OPENAI_KEY = "sk-proj-FIXTUREKEYplaceholder-not-a-real-credential-CCCC"
NEW_KEY = "sk-ant-api03-ROTATEDplaceholder-not-a-real-credential-DDDD"

_OC_CONFIG = {
    "plugins": {"entries": {
        "anthropic": {"enabled": True},
        "xai": {"enabled": True},
        "openai": {"enabled": True},
    }},
    "agents": {"list": [
        {"id": "main", "agentDir": "/ignored/by/design"},
        {"id": "email-reader"},
    ]},
}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


@pytest.fixture
def agent_tree(tmp_path: Path) -> Path:
    """A two-agent ``.openclaw`` tree in all three runtime-store shapes."""
    oc = tmp_path / ".openclaw"
    _write(oc / "openclaw.json", _OC_CONFIG)
    for agent in ("main", "email-reader"):
        base = oc / "agents" / agent / "agent"
        _write(base / "plugins" / "anthropic" / "catalog.json", {
            "generatedBy": "openclaw-plugin-model-catalog-v1",
            "providers": {"anthropic": {
                "api": "anthropic-messages",
                "baseUrl": "https://api.anthropic.com",
                "models": [{"id": "claude-haiku-4-5", "name": "claude-haiku-4-5",
                            "maxTokens": 64000}],
                "apiKey": ANTHROPIC_KEY,
            }},
        })
        # models.json spells xAI `x-ai`; the scan must normalise it to `xai`.
        _write(base / "models.json", {"providers": {"x-ai": {
            "api": "openai-responses",
            "baseUrl": "https://api.x.ai/v1",
            "models": [{"id": "grok-4-1-fast"}],
            "apiKey": XAI_KEY,
        }}})
    _write(oc / "agents" / "main" / "agent" / "codex-home" / "auth.json", {
        "auth_mode": "apikey", "OPENAI_API_KEY": OPENAI_KEY,
    })
    return oc


def _scan(oc: Path):
    return akio.scan_oc_dir_runtime_llm_keys(
        oc, _OC_CONFIG, read_text=lambda p: p.read_text() if p.exists() else None,
        list_dir=lambda p: sorted(os.listdir(p)) if p.is_dir() else [],
    )


# ── 1. The probe finds the keys, across both agents, and masks them ──────────

def test_scan_finds_every_store_across_both_agents(agent_tree: Path):
    located = _scan(agent_tree)
    by_provider = {}
    for hit in located:
        by_provider.setdefault(hit.provider, []).append(hit)

    assert sorted(by_provider) == ["anthropic", "openai", "xai"]
    assert ak.agents_for_provider(located, "anthropic") == ["main", "email-reader"]
    assert ak.agents_for_provider(located, "xai") == ["main", "email-reader"]
    # codex-home exists only under `main` on the live bot — one agent, and the
    # scan must not invent the other.
    assert ak.agents_for_provider(located, "openai") == ["main"]

    stores = {hit.store for hit in located}
    assert stores == {
        ak.STORE_PLUGIN_CATALOG, ak.STORE_AGENT_MODELS, ak.STORE_CODEX_AUTH,
    }


def test_scan_masks_and_never_puts_the_raw_key_on_the_row(agent_tree: Path):
    located = _scan(agent_tree)
    hit = next(h for h in located if h.provider == "anthropic")
    assert hit.masked == ANTHROPIC_KEY[:8] + "..." + ANTHROPIC_KEY[-4:]
    assert ANTHROPIC_KEY not in hit.masked
    # The full value stays reachable in-process (the rotate path needs it to
    # stash the outgoing key) but is never what the API renders.
    assert hit.value == ANTHROPIC_KEY


def test_scan_reports_file_mtime_when_it_can_stat(agent_tree: Path):
    hit = next(h for h in _scan(agent_tree) if h.provider == "anthropic")
    expected = (
        agent_tree / "agents/main/agent/plugins/anthropic/catalog.json"
    ).stat().st_mtime
    assert hit.mtime == pytest.approx(expected)


def test_models_json_provider_alias_normalises_to_the_evolve_id(agent_tree: Path):
    """``x-ai`` in models.json is ``xai`` on the row.

    Two different ids for one provider is how a rotation writes a block
    nobody reads. Both directions are pinned.
    """
    hit = next(h for h in _scan(agent_tree) if h.provider == "xai")
    assert hit.slot.json_path == ("providers", "x-ai", "apiKey")
    assert ak.store_provider_id(ak.STORE_AGENT_MODELS, "xai") == "x-ai"
    assert ak.evolve_provider_id(ak.STORE_AGENT_MODELS, "x-ai") == "xai"


def test_agent_enumeration_ignores_the_bot_writable_agentdir(agent_tree: Path):
    """``agents.list[].agentDir`` is bot-writable and every read here is a
    root ``sudo /bin/cat`` — honouring it would aim a privileged read
    anywhere on the box. Ids only, and only ones that pass the shape check.
    """
    assert ak.agent_ids_from_oc_config(_OC_CONFIG) == ["main", "email-reader"]
    hostile = {"agents": {"list": [
        {"id": "../../../etc", "agentDir": "/etc"},
        {"id": "ok-agent"},
    ]}}
    assert ak.agent_ids_from_oc_config(hostile) == ["main", "ok-agent"]


def test_missing_files_are_silent_and_malformed_ones_warn(tmp_path: Path):
    oc = tmp_path / ".openclaw"
    base = oc / "agents" / "main" / "agent"
    base.mkdir(parents=True)
    (base / "models.json").write_text("{not json")
    errors: list[str] = []
    located = akio.scan_oc_dir_runtime_llm_keys(
        oc, {}, read_text=lambda p: p.read_text() if p.exists() else None,
        list_dir=lambda p: sorted(os.listdir(p)) if p.is_dir() else [],
        errors_out=errors,
    )
    assert located == []
    assert any("models.json" in e for e in errors), errors


def test_probe_match_carries_agents_and_storage_locations(agent_tree: Path):
    from evolve_admin.web.probes import ProbeContext, ProbeHelpers, ProbeOutcome
    from evolve_admin.web.probes.oc_agent_catalog import OcAgentCatalogKeyProbe

    located = _scan(agent_tree)
    ctx = ProbeContext(
        bot_id="testbot", profiles={}, oc_cfg=_OC_CONFIG, network={},
        by_provider={}, by_key={}, auth_order={},
        helpers=ProbeHelpers(
            discover_github_remote=lambda b: None,
            detect_legacy_gws=lambda b: {},
            detect_dropbox_desktop=lambda b: {},
            read_google_oauth_client=lambda: None,
            ensure_fresh_google_access_token=lambda b: (None, None),
            scopes_to_services=lambda s: [],
            google_oauth_profile_id=lambda b: "",
            mask_key=lambda v: v[:8] + "..." + v[-4:],
            scan_agent_llm_keys=lambda bot_id, errors_out=None: located,
        ),
    )
    outcome, result = OcAgentCatalogKeyProbe(provider="anthropic").probe(ctx)
    assert outcome is ProbeOutcome.MATCH
    assert result.extras["agents"] == ["main", "email-reader"]
    assert result.extras["masked"].endswith(ANTHROPIC_KEY[-4:])
    assert result.storage_locations == (
        "~/.openclaw/agents/main/agent/plugins/anthropic/catalog.json",
        "~/.openclaw/agents/email-reader/agent/plugins/anthropic/catalog.json",
    )
    assert result.affordances == ("rotate",)

    outcome, _ = OcAgentCatalogKeyProbe(provider="cohere").probe(ctx)
    assert outcome is ProbeOutcome.NO_EVIDENCE


# ── 2. Runtime evidence: billing without a locatable key ─────────────────────

def test_provider_with_turns_but_no_key_lists_as_active_unlocated():
    from evolve_admin.web.credentials_visibility import annotate_should_list
    rows = [{
        "provider": "anthropic", "category": "llm", "status": "missing",
        "plugin_enabled": False,
        "actions": [{"id": "setup", "label": "Set up"}],
    }]
    annotate_should_list(rows, ["github"], lambda: {"anthropic"})
    row = rows[0]
    assert row["status"] == "active_unlocated", (
        "a provider that is billing must never read 'missing' — the page "
        "would be stating something false about where the money goes"
    )
    assert row["should_list"] is True
    assert "rotate is unavailable" in row["unlocated_reason"]
    assert row["actions"] == [], (
        "no affordance may be offered for a key we cannot locate: 'Set up' "
        "would write to a store the runtime is not reading from"
    )


def test_runtime_evidence_is_not_consulted_when_the_key_was_located():
    """The scan is lazy — on a healthy bot the turn log is never read."""
    from evolve_admin.web.credentials_visibility import annotate_should_list
    calls: list[int] = []

    def _evidence():
        calls.append(1)
        return {"anthropic"}

    rows = [{"provider": "anthropic", "category": "llm", "status": "active"}]
    annotate_should_list(rows, ["github"], _evidence)
    assert calls == []
    assert rows[0]["status"] == "active"


def test_runtime_evidence_does_not_upgrade_non_llm_rows():
    from evolve_admin.web.credentials_visibility import annotate_should_list
    rows = [{"provider": "brave", "category": "search", "status": "missing"}]
    annotate_should_list(rows, ["github"], lambda: {"brave"})
    assert rows[0]["status"] == "missing"


def test_runtime_evidence_failure_never_breaks_the_page():
    from evolve_admin.web.credentials_visibility import annotate_should_list

    def _boom():
        raise RuntimeError("turn log unreadable")

    rows = [{"provider": "anthropic", "category": "llm", "status": "missing"}]
    annotate_should_list(rows, ["github"], _boom)
    assert rows[0]["status"] == "missing"
    assert rows[0]["should_list"] is False


def test_turn_log_scan_walks_utc_days_and_stops_early(tmp_path: Path):
    """Turn files are UTC-named; reading them on local time drops a day."""
    from datetime import datetime, timezone
    from evolve_admin.web import credentials_runtime_evidence as cre

    turns = tmp_path / "shared" / "team-bot-a" / "turns"
    turns.mkdir(parents=True)
    (turns / "turns-2026-09-04.jsonl").write_text(
        json.dumps({"provider": "anthropic", "ts": "2026-09-04T23:59:00Z"}) + "\n"
    )
    (turns / "turns-2026-08-01.jsonl").write_text(
        json.dumps({"provider": "google", "ts": "2026-08-01T00:00:00Z"}) + "\n"
    )
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(tmp_path / "shared")}))

    cre.clear_cache()
    found = cre.providers_with_recent_turns(
        "team-bot-a", network_path=net, use_cache=False,
        now=datetime(2026, 9, 6, 0, 30, tzinfo=timezone.utc),
    )
    assert "anthropic" in found
    # 2026-08-01 is 36 days back — outside the 30-day window.
    assert "google" not in found


# ── 3. Rotation writes every agent's file ────────────────────────────────────

@pytest.fixture
def rotatable(agent_tree: Path, monkeypatch):
    """Redirect the privileged write path at a real tmp tree.

    ``sudo cp`` / ``sudo chown`` are emulated (a test cannot become root);
    ``chmod_secret_config`` is replaced with a real ``os.chmod`` that RECORDS
    its calls, because on the live box that helper is the single owner of the
    "0600 with the evolve read ACL preserved on macOS / re-granted on Linux"
    contract — asserting the write routes through it is what pins the ACL
    behaviour here, and ``test_secret_config_perms`` pins the helper itself.
    """
    chmod_calls: list[str] = []

    def _fake_sudo(*cmd: str):
        if cmd[:2] == ("sudo", "/bin/cp"):
            Path(cmd[3]).write_bytes(Path(cmd[2]).read_bytes())
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    def _fake_chmod(path):
        chmod_calls.append(str(path))
        os.chmod(path, 0o600)
        return True

    monkeypatch.setattr(akio, "_oc_dir", lambda b, network_path: agent_tree)
    monkeypatch.setattr(akio, "_bot_account", lambda b, network_path: "testbot")
    monkeypatch.setattr(
        akio, "_sudo_read",
        lambda bot, path: Path(path).read_text() if Path(path).exists() else None,
    )
    monkeypatch.setattr(akio, "_sudo", _fake_sudo)
    monkeypatch.setattr(akio, "chmod_secret_config", _fake_chmod)
    monkeypatch.setattr(
        akio, "_read_oc_json", lambda bot, network_path: _OC_CONFIG,
    )
    return agent_tree, chmod_calls


def _rotate(tmp_path: Path, monkeypatch, *, verify=None, restart=None):
    from evolve_admin.web import llm_key_rotate
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(tmp_path / "shared")}))
    audited: list[tuple] = []

    def _audit(event, bot_id, details, **kw):
        audited.append((event, bot_id, details))

    app = _flask_app()
    with app.test_request_context():
        resp = llm_key_rotate.rotate_oc_agent_store(
            "testbot", "anthropic", NEW_KEY,
            network_path=net, audit=_audit,
            restart_gateway=restart or (lambda b: {"ok": True, "service": "s"}),
            verify=verify or (lambda p, k: {
                "ok": True, "skipped": False, "status": 200,
                "detail": "anthropic accepted the key",
            }),
        )
    return resp, audited, net


def _flask_app():
    from flask import Flask
    return Flask(__name__)


def test_rotate_updates_every_agent_and_changes_only_the_key(
    rotatable, tmp_path, monkeypatch,
):
    oc, chmod_calls = rotatable
    before = {
        agent: json.loads(
            (oc / f"agents/{agent}/agent/plugins/anthropic/catalog.json").read_text()
        )
        for agent in ("main", "email-reader")
    }

    resp, audited, _ = _rotate(tmp_path, monkeypatch)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["agents"] == ["email-reader", "main"]
    assert len(body["written"]) == 2

    for agent in ("main", "email-reader"):
        path = oc / f"agents/{agent}/agent/plugins/anthropic/catalog.json"
        after = json.loads(path.read_text())
        assert after["providers"]["anthropic"]["apiKey"] == NEW_KEY
        # Byte-for-byte identical once the key is put back: transport fields,
        # the model list and the generatedBy marker all survive.
        after["providers"]["anthropic"]["apiKey"] = ANTHROPIC_KEY
        assert after == before[agent]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert str(path) in chmod_calls, (
            "the 0600 clamp must go through chmod_secret_config — a bare "
            "chmod would zero the POSIX-ACL mask on Linux and strand the "
            "evolve read ACE"
        )


def test_rotate_does_not_touch_an_unrelated_provider(rotatable, tmp_path, monkeypatch):
    oc, _ = rotatable
    before = (oc / "agents/main/agent/models.json").read_text()
    _rotate(tmp_path, monkeypatch)
    assert (oc / "agents/main/agent/models.json").read_text() == before


def test_rotate_records_the_verification_result(rotatable, tmp_path, monkeypatch):
    calls: list[tuple] = []

    def _verify(provider, key):
        calls.append((provider, key))
        return {"ok": True, "skipped": False, "status": 200, "detail": "ok"}

    resp, audited, _ = _rotate(tmp_path, monkeypatch, verify=_verify)
    assert calls == [("anthropic", NEW_KEY)]
    assert resp.get_json()["verify"]["ok"] is True
    event, bot, details = audited[-1]
    assert event == "keys.rotate"
    assert details["verified"] is True
    assert details["gateway_restarted"] is True
    assert details["storage"] == ak.RUNTIME_STORAGE_ID
    assert sorted(details["agents"]) == ["email-reader", "main"]
    # The audit entry names files and agents; it must never name the key.
    assert NEW_KEY not in json.dumps(details)


def test_rotate_survives_a_rejecting_provider_without_rolling_back(
    rotatable, tmp_path, monkeypatch,
):
    """A 401 does not undo the write.

    The new key is already where the runtime reads; putting the old one back
    on a provider-side answer would silently restore a credential the
    operator just replaced. The row shows the failure and offers Undo.
    """
    oc, _ = rotatable
    resp, audited, _ = _rotate(tmp_path, monkeypatch, verify=lambda p, k: {
        "ok": False, "skipped": False, "status": 401,
        "detail": "anthropic rejected the key (HTTP 401)",
    })
    body = resp.get_json()
    assert body["ok"] is True
    assert body["verify"]["ok"] is False
    after = json.loads(
        (oc / "agents/main/agent/plugins/anthropic/catalog.json").read_text()
    )
    assert after["providers"]["anthropic"]["apiKey"] == NEW_KEY


def test_undo_restores_the_previous_key_in_every_agent(
    rotatable, tmp_path, monkeypatch,
):
    from evolve_admin.web import llm_key_rotate
    oc, _ = rotatable
    resp, _, net = _rotate(tmp_path, monkeypatch)
    assert resp.get_json()["can_undo"] is True

    app = _flask_app()
    with app.test_request_context():
        undo = llm_key_rotate.undo_oc_agent_store(
            "testbot", "anthropic", network_path=net, audit=lambda *a, **k: None,
            restart_gateway=lambda b: {"ok": True},
            verify=lambda p, k: {"ok": True, "skipped": False, "status": 200,
                                 "detail": "ok"},
        )
    assert undo.get_json()["ok"] is True
    for agent in ("main", "email-reader"):
        after = json.loads(
            (oc / f"agents/{agent}/agent/plugins/anthropic/catalog.json").read_text()
        )
        assert after["providers"]["anthropic"]["apiKey"] == ANTHROPIC_KEY


def test_rollback_stash_is_0600_and_holds_one_rotation_only(
    rotatable, tmp_path, monkeypatch,
):
    _rotate(tmp_path, monkeypatch)
    path = tmp_path / "shared" / akio.ROLLBACK_SUBDIR / "testbot.json"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    entry = json.loads(path.read_text())["anthropic"]
    assert entry["previous"] == ANTHROPIC_KEY
    assert entry["rotated_at"].endswith("Z")


def test_verification_verdict_survives_the_modal_closing(
    rotatable, tmp_path, monkeypatch,
):
    """The row shows "verified <time>" or the provider's error afterwards.

    A verdict that only ever appears in a modal is a verdict the operator
    loses the moment they click away — and the bad-paste case is exactly the
    one they need to come back to.
    """
    _rotate(tmp_path, monkeypatch, verify=lambda p, k: {
        "ok": False, "skipped": False, "status": 401,
        "detail": "anthropic rejected the key (HTTP 401)",
    })
    net = tmp_path / "network.json"
    verdict = akio.last_verification("testbot", "anthropic", network_path=net)
    assert verdict["ok"] is False
    assert verdict["status"] == 401
    assert verdict["at"].endswith("Z")
    assert NEW_KEY not in json.dumps(verdict)


def test_rotate_refuses_when_the_provider_has_no_runtime_key(
    rotatable, tmp_path, monkeypatch,
):
    from evolve_admin.web import llm_key_rotate
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(tmp_path / "shared")}))
    app = _flask_app()
    with app.test_request_context():
        resp, status = llm_key_rotate.rotate_oc_agent_store(
            "testbot", "cohere", NEW_KEY, network_path=net,
            audit=lambda *a, **k: None,
            restart_gateway=lambda b: {"ok": True},
            verify=lambda p, k: {"ok": True},
        )
    assert status == 404
    assert "nothing to rotate" in resp.get_json()["error"]


def test_partial_write_is_reported_as_a_failure_naming_the_stale_file(
    rotatable, tmp_path, monkeypatch,
):
    """A half-written rotation puts a two-agent bot on two different keys.

    Never "ok" — the operator has to be told which file still bills on the
    old credential.
    """
    oc, _ = rotatable
    real_write = akio.write_runtime_key

    def _flaky(bot_id, slot, key_value, *, network_path):
        if slot.agent == "email-reader":
            return False, "cp refused"
        return real_write(bot_id, slot, key_value, network_path=network_path)

    monkeypatch.setattr(
        "evolve_admin.web.llm_key_rotate.write_runtime_key", _flaky,
    )
    resp, status, *_ = (*_rotate_expect_tuple(tmp_path, monkeypatch),)
    assert status == 500
    body = resp.get_json()
    assert "INCOMPLETE" in body["error"]
    assert body["failures"][0]["agent"] == "email-reader"


def _rotate_expect_tuple(tmp_path: Path, monkeypatch):
    from evolve_admin.web import llm_key_rotate
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(tmp_path / "shared")}))
    app = _flask_app()
    with app.test_request_context():
        return llm_key_rotate.rotate_oc_agent_store(
            "testbot", "anthropic", NEW_KEY, network_path=net,
            audit=lambda *a, **k: None,
            restart_gateway=lambda b: {"ok": True},
            verify=lambda p, k: {"ok": True},
        )


# ── 4. Sudoers: the grants exist and the render still parses ─────────────────

_GRANT_SHAPES = (
    "/bin/cat /Users/*/.openclaw/agents/*/agent/plugins/*/catalog.json",
    "/bin/cat /Users/*/.openclaw/agents/*/agent/models.json",
    "/bin/cat /Users/*/.openclaw/agents/*/agent/codex-home/auth.json",
    "/bin/ls /Users/*/.openclaw/agents/*/agent/plugins",
    "/bin/cp /tmp/evolve-llmkey-* /Users/*/.openclaw/agents/*/agent/plugins/*/catalog.json",
    "/bin/cp /tmp/evolve-llmkey-* /Users/*/.openclaw/agents/*/agent/models.json",
    "/bin/cp /tmp/evolve-llmkey-* /Users/*/.openclaw/agents/*/agent/codex-home/auth.json",
    "/bin/chmod 600 /Users/*/.openclaw/agents/*/agent/plugins/*/catalog.json",
    "/bin/chmod 600 /Users/*/.openclaw/agents/*/agent/models.json",
    "/bin/chmod 600 /Users/*/.openclaw/agents/*/agent/codex-home/auth.json",
)

#: Grants this surface must NOT carry. `cp` rewrites an existing inode in
#: place, so ownership survives without a chown — and a `chown *` line is an
#: argv-spanning root grant (sudo matches arguments as one space-joined
#: string with FNM_PATHNAME off) bought for nothing.
_FORBIDDEN_GRANT_SHAPES = (
    "/usr/sbin/chown * /Users/*/.openclaw/agents/*/agent/plugins/*/catalog.json",
    "/usr/sbin/chown * /Users/*/.openclaw/agents/*/agent/models.json",
    "/usr/sbin/chown * /Users/*/.openclaw/agents/*/agent/codex-home/auth.json",
)


def _macos_sudoers() -> str:
    return (
        _ADMIN_DIR / "tests" / "fixtures" / "sudoers_golden" / "evolve_macos.sudoers"
    ).read_text()


@pytest.mark.parametrize("shape", _GRANT_SHAPES)
def test_runtime_store_grants_are_rendered(shape: str):
    """Every privileged argv the rotate path issues has a NOPASSWD grant.

    sudo matches EXACT argv, and a drifted byte is a grant that silently
    demands a password on a daemon with no TTY.
    """
    assert f"evolve ALL=(root) NOPASSWD: {shape}\n" in _macos_sudoers()


@pytest.mark.parametrize("shape", _FORBIDDEN_GRANT_SHAPES)
def test_runtime_store_asks_for_no_chown_grant(shape: str):
    """The rotate path never chowns, so no chown grant may be shipped for it.

    Every destination already exists and is bot-owned (the scan found it),
    and `sudo /bin/cp` over an existing inode leaves ownership alone. A
    `chown *` grant would hand root a pattern whose wildcard spans spaces
    and '/' — `chown evolve /etc/sudoers /Users/b/.openclaw/agents/main/
    agent/models.json` matches it — in exchange for nothing.
    """
    assert shape not in _macos_sudoers()


def test_write_runtime_key_issues_cp_and_chmod_but_never_chown(
    agent_tree: Path, tmp_path: Path, monkeypatch,
):
    """The privileged argv sequence for one slot write, pinned.

    Pins the fix as behaviour, not just as an absent sudoers line: a chown
    reintroduced here would be a grant-less sudo call that dies asking for a
    password on a daemon with no TTY.
    """
    calls: list[list[str]] = []

    def _fake_sudo(*cmd: str):
        calls.append(list(cmd))
        if cmd[:2] == ("sudo", "/bin/cp"):
            Path(cmd[3]).write_text(Path(cmd[2]).read_text())
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(akio, "_sudo", _fake_sudo)
    monkeypatch.setattr(akio, "chmod_secret_config", lambda p: True)
    monkeypatch.setattr(akio, "_oc_dir", lambda b, network_path: agent_tree)
    monkeypatch.setattr(akio, "_sudo_read",
                        lambda b, p: Path(p).read_text() if Path(p).exists() else None)
    slot = ak.RuntimeKeySlot(
        provider="anthropic", agent="main", store=ak.STORE_PLUGIN_CATALOG,
        relpath="agents/main/agent/plugins/anthropic/catalog.json",
        json_path=("providers", "anthropic", "apiKey"),
    )
    ok, err = akio.write_runtime_key(
        "testbot", slot, NEW_KEY, network_path=tmp_path / "network.json",
    )
    assert (ok, err) == (True, None)
    assert [c[1] for c in calls] == ["/bin/cp"], calls
    assert not any("chown" in part for c in calls for part in c), calls


def test_rotate_over_a_bot_owned_target_leaves_ownership_and_mode_alone(
    agent_tree: Path, tmp_path: Path, monkeypatch,
):
    """`cp` over an existing inode preserves its owner; chmod pins 0600.

    The fixture stands in for the live shape: a bot-owned file the evolve
    daemon rewrites as root. The test cannot change uids, so it asserts what
    it can observe — the inode is the SAME one (so whatever owned it still
    does) and the mode is 0600 afterwards.
    """
    dest = agent_tree / "agents/main/agent/plugins/anthropic/catalog.json"
    dest.chmod(0o644)
    before = dest.stat()

    def _fake_sudo(*cmd: str):
        if cmd[:2] == ("sudo", "/bin/cp"):
            # `cp` (no -p) truncates and rewrites in place — same inode.
            with open(cmd[3], "w") as out:
                out.write(Path(cmd[2]).read_text())
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(akio, "_sudo", _fake_sudo)
    monkeypatch.setattr(akio, "chmod_secret_config",
                        lambda p: (Path(p).chmod(0o600), True)[1])
    monkeypatch.setattr(akio, "_oc_dir", lambda b, network_path: agent_tree)
    monkeypatch.setattr(akio, "_sudo_read",
                        lambda b, p: Path(p).read_text() if Path(p).exists() else None)
    slot = ak.RuntimeKeySlot(
        provider="anthropic", agent="main", store=ak.STORE_PLUGIN_CATALOG,
        relpath="agents/main/agent/plugins/anthropic/catalog.json",
        json_path=("providers", "anthropic", "apiKey"),
    )
    ok, _ = akio.write_runtime_key(
        "testbot", slot, NEW_KEY, network_path=tmp_path / "network.json",
    )
    after = dest.stat()
    assert ok
    assert after.st_ino == before.st_ino
    assert after.st_uid == before.st_uid
    assert stat.S_IMODE(after.st_mode) == 0o600
    assert json.loads(dest.read_text())["providers"]["anthropic"]["apiKey"] == NEW_KEY


def test_runtime_store_grants_pass_visudo(tmp_path: Path):
    visudo = "/usr/sbin/visudo"
    if not Path(visudo).exists():
        pytest.skip("visudo not available on this host")
    fixture = tmp_path / "evolve.sudoers"
    fixture.write_text(_macos_sudoers())
    proc = subprocess.run(
        [visudo, "-c", "-f", str(fixture)],
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_agent_store_relpaths_are_covered_by_the_0600_self_heal(agent_tree: Path):
    from evolve_admin.secret_config_perms import discovered_agent_store_relpaths
    found = set(discovered_agent_store_relpaths(agent_tree))
    assert "agents/main/agent/plugins/anthropic/catalog.json" in found
    assert "agents/email-reader/agent/models.json" in found
    assert "agents/main/agent/codex-home/auth.json" in found


def test_rollback_store_is_a_registered_shared_secret():
    from evolve_admin.secret_config_perms import SHARED_SECRET_SUBDIRS
    assert akio.ROLLBACK_SUBDIR in SHARED_SECRET_SUBDIRS, (
        "the stashed previous key is a live credential at rest — it must be "
        "in the table check_shared_secret_modes enforces 0600 over"
    )


# ── 5. Verification is a cheap, auth-only call ───────────────────────────────

def test_verify_uses_a_header_not_a_query_string_for_google():
    """A credential in a query string lands in proxy and access logs."""
    from evolve_admin.web import llm_key_verify
    seen: dict = {}

    class _Resp:
        status = 200

        def read(self, n=None):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        return _Resp()

    result = llm_key_verify.verify_key("google", "AIza-fixture", opener=_opener)
    assert result.ok is True
    assert "AIza-fixture" not in seen["url"]
    assert any(v == "AIza-fixture" for v in seen["headers"].values())


def test_verify_distinguishes_rejected_from_unreachable():
    import urllib.error
    from evolve_admin.web import llm_key_verify

    def _401(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    def _down(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    rejected = llm_key_verify.verify_key("anthropic", "k", opener=_401)
    assert rejected.ok is False and rejected.status == 401
    assert "rejected" in rejected.detail

    unreachable = llm_key_verify.verify_key("anthropic", "k", opener=_down)
    assert unreachable.ok is False and unreachable.status is None
    assert "unproven" in unreachable.detail, (
        "an unreachable provider must not be reported as a bad key — that is "
        "the broken-vs-unavailable conflation the fallback work fixed"
    )


def test_verify_skips_a_provider_it_cannot_reach_rather_than_failing():
    from evolve_admin.web import llm_key_verify
    result = llm_key_verify.verify_key("some-new-provider", "k")
    assert result.skipped is True and result.ok is False
    assert llm_key_verify.verifiable("anthropic") is True


# ── 6. A provider Evolve has no registry entry for still LISTS ───────────────
# The operator's rule, stated 2026-09-04: "if it has a key or a credential
# loaded, I don't care if it has never been used, it needs to be viewable in
# the UI." The `ls` of plugins/ is what makes an unknown vendor findable; if
# the row minting then drops it, the scan's whole reason for listing rather
# than guessing is wasted.

NEWVENDOR_KEY = "nv-FIXTUREKEYplaceholder-not-a-real-credential-EEEE"


@pytest.fixture
def agent_tree_with_unknown_vendor(agent_tree: Path) -> Path:
    """The two-agent tree plus a provider dir Evolve has never heard of."""
    for agent in ("main", "email-reader"):
        _write(
            agent_tree / "agents" / agent / "agent" / "plugins" / "newvendor"
            / "catalog.json",
            {
                "generatedBy": "openclaw-plugin-model-catalog-v1",
                "providers": {"newvendor": {
                    "api": "openai-responses",
                    "baseUrl": "https://api.newvendor.example/v1",
                    "apiKey": NEWVENDOR_KEY,
                }},
            },
        )
    return agent_tree


def _runtime_only_rows(oc: Path, seen: set) -> list[dict]:
    from evolve_admin.web.credentials_api_key_rows import runtime_only_rows
    return runtime_only_rows(lambda _bot: _scan(oc), "testbot", seen)


def test_unknown_provider_in_the_runtime_store_gets_a_row(
    agent_tree_with_unknown_vendor: Path,
):
    seen = {"anthropic", "openai", "xai"}  # stands in for _KEY_REGISTRY
    rows = _runtime_only_rows(agent_tree_with_unknown_vendor, seen)
    assert [r["provider"] for r in rows] == ["newvendor"], (
        "a provider outside _KEY_REGISTRY must produce exactly one row, and "
        "registry providers must not be duplicated by this pass"
    )
    row = rows[0]
    assert row["status"] == "active"
    assert row["oc_only"] is True
    assert row["storage"] == ak.RUNTIME_STORAGE_ID
    assert row["category"] == "llm"
    # Agent chips: one key mirrored to both agents, same as a known provider.
    assert row["agents"] == ["main", "email-reader"]
    assert row["runtime_location_count"] == 2
    assert all(
        loc.startswith("~/.openclaw/agents/") for loc in row["storage_locations"]
    )
    assert "newvendor" in row["display"].lower()


def test_unknown_provider_row_offers_no_rotate_and_no_raw_key(
    agent_tree_with_unknown_vendor: Path,
):
    """No rotate affordance, because there is no verify call to prove it.

    Writing the key the gateway is live on and being unable to check the
    write is decision B's forbidden move; the row is read-only until the
    provider gains a verify call (llm_key_verify.verifiable).
    """
    from evolve_admin.web import llm_key_verify
    rows = _runtime_only_rows(agent_tree_with_unknown_vendor, {"anthropic"})
    row = next(r for r in rows if r["provider"] == "newvendor")
    assert row["rotatable"] is False
    assert llm_key_verify.verifiable("newvendor") is False
    assert NEWVENDOR_KEY not in json.dumps(row)
    assert row["masked"].endswith(NEWVENDOR_KEY[-4:])


def test_unknown_provider_row_survives_a_failing_scan(agent_tree: Path):
    """Discovery is additive — it never 500s the Credentials page."""
    from evolve_admin.web.credentials_api_key_rows import runtime_only_rows

    def _boom(_bot):
        raise RuntimeError("sudoers not refreshed")

    assert runtime_only_rows(_boom, "testbot", set()) == []


def test_a_located_key_lists_under_the_visibility_rule(
    agent_tree_with_unknown_vendor: Path,
):
    """Clause (a): status "active" ⇒ should_list, for the unknown vendor too."""
    from evolve_admin.web.credentials_visibility import annotate_should_list
    rows = _runtime_only_rows(agent_tree_with_unknown_vendor, {"anthropic"})
    annotate_should_list(rows, [], None)
    assert all(r["should_list"] for r in rows)


# ── 7. The undo route is behind the same gate as the rotate sibling ──────────

def test_undo_rotation_refuses_an_unauthenticated_post(tmp_path: Path, monkeypatch):
    """Undo rewrites every agent's key file and bounces the gateway.

    Neither it nor its rotate sibling carries a per-route gate — gating is
    the app-wide `_enforce_device_auth` before_request hook — so the thing
    worth pinning is that the hook actually covers this path, and covers it
    the same way it covers rotate.
    """
    from evolve_admin.web import admin_auth
    from evolve_admin.web.server import create_app

    monkeypatch.delenv(admin_auth._AUTH_DISABLED_ENV, raising=False)
    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"bots": {}, "sharedDir": str(shared)}))
    admin_auth.ensure_key(shared)  # operator paired → enforcement on

    app = create_app(network_path=net)
    app.config["TESTING"] = True
    with app.test_client() as client:
        undo = client.post("/api/admin/keys/testbot/anthropic/undo-rotation", json={})
        rotate = client.post(
            "/api/admin/keys/testbot/anthropic/rotate",
            json={"key_value": "should-never-be-written",
                  "storage": ak.RUNTIME_STORAGE_ID},
        )
    assert undo.status_code == 401, undo.get_data(as_text=True)
    assert undo.status_code == rotate.status_code, (
        "undo must be refused exactly as rotate is — it rewrites the same "
        "files and restarts the same gateway"
    )
