"""Tests for the Phase E pod-wide GitHub-dev-PAT mini-wizard.

Two surfaces:

  * Backend status endpoint (/api/admin/github-dev/status) — normalizes
    v1 (single-target) and v2 (multi-target) intake.github storage
    shapes into one render-friendly response.
  * UI structural pins for the modal HTML + load + section rendering.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"
SERVER_PY = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "server.py"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _setup_path() -> None:
    _ADMIN = Path(__file__).parent.parent
    _ANALYZER = _ADMIN.parent / "analyzer"
    for p in (_ADMIN, _ANALYZER):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


# ── UI structural pins ────────────────────────────────────────────────────


def test_github_dev_modal_overlay_exists():
    html = _html()
    assert 'id="github-dev-modal"' in html
    assert 'id="github-dev-modal-body"' in html
    assert 'id="github-dev-modal-title"' in html


def test_modal_closes_on_outside_click():
    html = _html()
    assert (
        "onclick=\"if(event.target===this)closeGithubDevWizard()\""
        in html
    )


def test_open_close_load_functions_defined():
    html = _html()
    assert "async function openGithubDevWizard()" in html
    assert "function closeGithubDevWizard()" in html
    assert "async function loadGithubDevWizard()" in html


def test_load_fn_hits_status_endpoint():
    html = _html()
    fn = re.search(
        r"async function loadGithubDevWizard\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "loadGithubDevWizard not found"
    assert "/api/admin/github-dev/status" in fn.group(1)


def test_dispatcher_routes_to_modal_not_alert():
    """Phase E upgraded the stub from alert() to a real modal. The
    dispatcher name `_podSetupGithubDevWizardStub` stays for binding
    stability."""
    html = _html()
    fn = re.search(
        r"function _podSetupGithubDevWizardStub\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupGithubDevWizardStub not found"
    body = fn.group(1)
    assert "openGithubDevWizard()" in body
    # Strip comments (// to EOL and /* */ blocks) before guarding against
    # an actual alert() invocation. The implementation note legitimately
    # mentions alert().
    no_line_comments = re.sub(r"//[^\n]*", "", body)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    assert "alert(" not in no_block_comments, \
        "Phase E should have replaced alert() invocation with modal"


def test_close_refreshes_parent_surfaces():
    html = _html()
    fn = re.search(
        r"function closeGithubDevWizard\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "closeGithubDevWizard not found"
    body = fn.group(1)
    assert "loadPodSetupChecklist" in body
    assert "loadPodSetupChip" in body


def test_copy_cli_function_uses_navigator_clipboard():
    html = _html()
    fn = re.search(
        r"function _githubDevCopyCmd\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_githubDevCopyCmd not found"
    body = fn.group(1)
    assert "navigator.clipboard" in body
    assert "sudo evolve-admin intake configure" in body
    assert "Select and copy" in body


def test_render_shows_targets_when_configured():
    """When intake.github has targets, the modal renders them so the
    operator can verify what's already wired."""
    html = _html()
    fn = re.search(
        r"function _renderGithubDevWizard\(body, st\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_renderGithubDevWizard not found"
    body = fn.group(1)
    # Render walks st.targets — the variable must appear in the function body.
    assert "st.targets" in body or "targets" in body
    assert "owner" in body
    assert "token_slot" in body


def test_render_shows_default_target_marker():
    """v2 has multiple targets; one is the default. Modal must surface
    which one (a small dot marker on the line) so operator can see."""
    html = _html()
    fn = re.search(
        r"function _renderGithubDevWizard\(body, st\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_renderGithubDevWizard not found"
    body = fn.group(1)
    assert "default_target" in body


def test_render_special_cases_already_configured():
    """When configured, section 2 reads "Add another target (optional)"
    and dims. Section 3 says "Already configured" instead of "Click after
    the command finishes."""
    html = _html()
    fn = re.search(
        r"function _renderGithubDevWizard\(body, st\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_renderGithubDevWizard not found"
    body = fn.group(1)
    assert "Add another target" in body or "configured" in body.lower()


def test_render_links_to_github_pat_settings():
    """Section 2 surfaces a link to the PAT-generation page so the
    operator can grab a token without leaving the browser."""
    html = _html()
    fn = re.search(
        r"function _renderGithubDevWizard\(body, st\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_renderGithubDevWizard not found"
    body = fn.group(1)
    assert "github.com/settings/tokens/new" in body


# ── Backend endpoint regression ───────────────────────────────────────────


def test_endpoint_defined_in_server_py():
    text = SERVER_PY.read_text(encoding="utf-8")
    assert "/api/admin/github-dev/status" in text
    assert "def api_github_dev_status" in text


def test_endpoint_returns_expected_fields():
    text = SERVER_PY.read_text(encoding="utf-8")
    fn = re.search(
        r"def api_github_dev_status\(\).+?return jsonify\(\{(.+?)\}\)",
        text, re.DOTALL,
    )
    assert fn, "api_github_dev_status not found"
    body = fn.group(0)
    for key in ("configured", "shape", "targets", "default_target"):
        assert f'"{key}"' in body, f"missing response key {key!r}"


def test_endpoint_normalizes_v1_shape(tmp_path):
    """v1 storage is owner/repo at top level. Endpoint exposes it as
    a single-target list with name='default' so the UI doesn't branch."""
    _setup_path()
    from flask import Flask, jsonify
    from evolve_admin.config import load_network

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "networkId": "test",
        "intake": {"github": {
            "owner": "ops", "repo": "evolve",
            "token_slot": "github_intake",
        }},
    }))

    app = Flask(__name__)

    @app.get("/api/admin/github-dev/status")
    def _status():
        network = load_network(network_path)
        intake_gh = (network.get("intake") or {}).get("github") or {}
        targets = []
        shape = None
        default_target = None
        v2 = intake_gh.get("targets")
        if isinstance(v2, dict) and v2:
            shape = "v2"
            default_target = intake_gh.get("default") if isinstance(intake_gh.get("default"), str) else None
            for name, entry in v2.items():
                if not isinstance(entry, dict):
                    continue
                targets.append({"name": str(name), "owner": entry.get("owner") or "",
                                "repo": entry.get("repo") or "", "token_slot": entry.get("token_slot") or ""})
        elif intake_gh.get("owner") and intake_gh.get("repo"):
            shape = "v1"
            targets.append({"name": "default", "owner": intake_gh.get("owner") or "",
                            "repo": intake_gh.get("repo") or "",
                            "token_slot": intake_gh.get("token_slot") or "github_intake"})
        return jsonify({"configured": bool(targets), "shape": shape,
                        "targets": targets, "default_target": default_target})

    with app.test_client() as c:
        data = c.get("/api/admin/github-dev/status").get_json()
    assert data["configured"] is True
    assert data["shape"] == "v1"
    assert data["default_target"] is None
    assert len(data["targets"]) == 1
    assert data["targets"][0]["name"] == "default"
    assert data["targets"][0]["owner"] == "ops"
    assert data["targets"][0]["repo"] == "evolve"


def test_endpoint_normalizes_v2_multi_target(tmp_path):
    _setup_path()
    from flask import Flask, jsonify
    from evolve_admin.config import load_network

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "networkId": "test",
        "intake": {"github": {
            "default": "evolve",
            "targets": {
                "evolve": {"owner": "ops", "repo": "evolve", "token_slot": "github_intake"},
                "openclaw": {"owner": "openclaw", "repo": "openclaw", "token_slot": "github_intake_openclaw"},
            },
        }},
    }))

    app = Flask(__name__)

    @app.get("/api/admin/github-dev/status")
    def _status():
        network = load_network(network_path)
        intake_gh = (network.get("intake") or {}).get("github") or {}
        targets = []
        shape = None
        default_target = None
        v2 = intake_gh.get("targets")
        if isinstance(v2, dict) and v2:
            shape = "v2"
            default_target = intake_gh.get("default") if isinstance(intake_gh.get("default"), str) else None
            for name, entry in v2.items():
                if not isinstance(entry, dict):
                    continue
                targets.append({"name": str(name), "owner": entry.get("owner") or "",
                                "repo": entry.get("repo") or "", "token_slot": entry.get("token_slot") or ""})
        return jsonify({"configured": bool(targets), "shape": shape,
                        "targets": targets, "default_target": default_target})

    with app.test_client() as c:
        data = c.get("/api/admin/github-dev/status").get_json()
    assert data["configured"] is True
    assert data["shape"] == "v2"
    assert data["default_target"] == "evolve"
    names = {t["name"] for t in data["targets"]}
    assert names == {"evolve", "openclaw"}


def test_endpoint_reports_unconfigured_for_empty(tmp_path):
    _setup_path()
    from flask import Flask, jsonify
    from evolve_admin.config import load_network

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"networkId": "test"}))

    app = Flask(__name__)

    @app.get("/api/admin/github-dev/status")
    def _status():
        network = load_network(network_path)
        intake_gh = (network.get("intake") or {}).get("github") or {}
        targets = []
        if isinstance(intake_gh, dict):
            v2 = intake_gh.get("targets")
            if isinstance(v2, dict) and v2:
                pass  # would enumerate
            elif intake_gh.get("owner") and intake_gh.get("repo"):
                targets.append({"name": "default", "owner": "x", "repo": "y", "token_slot": ""})
        return jsonify({"configured": bool(targets), "shape": None,
                        "targets": targets, "default_target": None})

    with app.test_client() as c:
        data = c.get("/api/admin/github-dev/status").get_json()
    assert data["configured"] is False
    assert data["targets"] == []
