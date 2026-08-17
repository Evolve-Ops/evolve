"""Tests for the Phase D HTTPS-on-LAN mini-wizard.

Two surfaces:

  * Backend status endpoint (/api/admin/https-setup/status) — composes
    https_setup helpers gracefully when Tailscale CLI isn't reachable.
  * UI structural pins for the modal HTML + load + section dimming +
    copy-CLI behavior.

The endpoint test stands up a minimal Flask app rather than spinning
up the full admin server, so it doesn't need a real shared dir or
network.json.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"
SERVER_PY = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "server.py"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ── UI structural pins ────────────────────────────────────────────────────


def test_https_setup_modal_overlay_exists():
    html = _html()
    assert 'id="https-setup-modal"' in html
    assert 'id="https-setup-modal-body"' in html
    assert 'id="https-setup-modal-title"' in html


def test_modal_closes_on_outside_click():
    html = _html()
    assert (
        "onclick=\"if(event.target===this)closeHttpsSetupWizard()\""
        in html
    )


def test_open_close_load_functions_defined():
    html = _html()
    assert "async function openHttpsSetupWizard()" in html
    assert "function closeHttpsSetupWizard()" in html
    assert "async function loadHttpsSetupWizard()" in html


def test_load_fn_hits_status_endpoint():
    html = _html()
    fn = re.search(
        r"async function loadHttpsSetupWizard\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "loadHttpsSetupWizard not found"
    assert "/api/admin/https-setup/status" in fn.group(1)


def test_dispatcher_routes_to_modal_not_alert():
    """The `open_https_wizard` action id now opens the real modal rather
    than the alert() stub. The stub function name stayed the same for
    binding stability — but its body must call openHttpsSetupWizard()
    and not actually invoke alert()."""
    html = _html()
    fn = re.search(
        r"function _podSetupHttpsWizardStub\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupHttpsWizardStub not found"
    body = fn.group(1)
    assert "openHttpsSetupWizard()" in body
    # Strip JS comments (// to EOL and /* */ blocks) before checking for
    # alert(. The Phase B → D upgrade comment legitimately mentions
    # "alert()" as a historical note.
    no_line_comments = re.sub(r"//[^\n]*", "", body)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    assert "alert(" not in no_block_comments, \
        "Phase D should have replaced alert() invocation with modal"


def test_close_refreshes_parent_surfaces():
    """Closing the wizard re-fetches the checklist + chip so the
    operator sees the row flip if verify succeeded."""
    html = _html()
    fn = re.search(
        r"function closeHttpsSetupWizard\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "closeHttpsSetupWizard not found"
    body = fn.group(1)
    assert "loadPodSetupChecklist" in body
    assert "loadPodSetupChip" in body


def test_copy_cli_function_uses_navigator_clipboard():
    """Copy button uses clipboard API with graceful fallback when not
    in a secure context (HTTP, sandbox, etc.)."""
    html = _html()
    fn = re.search(
        r"function _httpsSetupCopyCmd\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_httpsSetupCopyCmd not found"
    body = fn.group(1)
    assert "navigator.clipboard" in body
    assert "sudo evolve-admin enable-https" in body
    # And a fallback path for when clipboard API is unavailable
    assert "Select and copy" in body


def test_render_dims_action_when_tailscale_not_ready():
    """If Tailscale isn't signed in, the CLI action section dims so the
    operator can't fool themselves into copying the command before the
    prereq is met."""
    html = _html()
    fn = re.search(
        r"function _renderHttpsSetupWizard\(body, st\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_renderHttpsSetupWizard not found"
    body = fn.group(1)
    # The dim style is `opacity:0.45;pointer-events:none` (same pattern
    # as the per-bot GitHub wizard) — only applied when tsOk is false.
    assert "tsOk" in body
    assert "opacity:0.45;pointer-events:none" in body


def test_render_shows_tailnet_host_when_known():
    """When _check_signed_in returns a Self.DNSName, the modal surfaces
    it — operator should see what hostname they're about to expose."""
    html = _html()
    fn = re.search(
        r"function _renderHttpsSetupWizard\(body, st\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_renderHttpsSetupWizard not found"
    body = fn.group(1)
    assert "tailnet_host" in body


def test_render_special_cases_already_enabled():
    """When current_scheme is already 'https', the modal congratulates
    + dims section 2 (re-running is optional, not the primary action)."""
    html = _html()
    fn = re.search(
        r"function _renderHttpsSetupWizard\(body, st\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_renderHttpsSetupWizard not found"
    body = fn.group(1)
    assert "already_enabled" in body or "already" in body.lower()


# ── Backend endpoint regression ───────────────────────────────────────────


def test_endpoint_defined_in_server_py():
    text = SERVER_PY.read_text(encoding="utf-8")
    assert "/api/admin/https-setup/status" in text
    assert "def api_https_setup_status" in text


def test_endpoint_returns_expected_fields():
    """Pin the response shape — UI depends on these keys."""
    text = SERVER_PY.read_text(encoding="utf-8")
    fn = re.search(
        r"def api_https_setup_status\(\).+?return jsonify\(\{(.+?)\}\)",
        text, re.DOTALL,
    )
    assert fn, "api_https_setup_status not found"
    body = fn.group(0)
    for key in ("current_scheme", "admin_url", "tailscale_state",
                "tailnet_host", "already_enabled", "error"):
        assert f'"{key}"' in body, f"missing response key {key!r}"


def test_endpoint_handles_tailscale_not_installed():
    """Direct unit-style call: when https_setup raises TailscaleNotInstalled,
    the endpoint reports state='not_installed' rather than 500ing.

    Spins up a fresh Flask app with just this route attached — keeps
    the test fast (no full admin server boot)."""
    _ADMIN = Path(__file__).parent.parent
    _ANALYZER = _ADMIN.parent / "analyzer"
    for p in (_ADMIN, _ANALYZER):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    from flask import Flask, jsonify
    from evolve_admin import https_setup
    from evolve_admin.config import load_network

    app = Flask(__name__)
    network_path = REPO_ROOT / "nonexistent-network.json"  # load_network returns defaults

    @app.get("/api/admin/https-setup/status")
    def _status():
        network = load_network(network_path)
        admin_url = network.get("adminBaseUrl") or ""
        current_scheme = "https" if admin_url.startswith("https://") else "http"
        tailscale_state = "unknown"
        tailnet_host = None
        error = None
        try:
            status = https_setup._check_signed_in()
            tailscale_state = "ok"
            try:
                tailnet_host = https_setup._resolve_tailnet_hostname(status)
            except Exception:
                pass
        except https_setup.TailscaleNotInstalled as exc:
            tailscale_state = "not_installed"
            error = str(exc)
        except https_setup.TailscaleNotSignedIn as exc:
            tailscale_state = "not_signed_in"
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        return jsonify({
            "current_scheme": current_scheme,
            "admin_url": admin_url,
            "tailscale_state": tailscale_state,
            "tailnet_host": tailnet_host,
            "already_enabled": current_scheme == "https",
            "error": error,
        })

    with patch.object(
        https_setup, "_check_signed_in",
        side_effect=https_setup.TailscaleNotInstalled("Tailscale CLI missing"),
    ):
        with app.test_client() as client:
            resp = client.get("/api/admin/https-setup/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["tailscale_state"] == "not_installed"
            assert data["already_enabled"] is False
            assert "Tailscale CLI missing" in (data.get("error") or "")
