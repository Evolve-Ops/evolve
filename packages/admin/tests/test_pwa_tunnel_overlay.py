"""tests/test_pwa_tunnel_overlay.py — PWA tunnel-down overlay contract.

The overlay appears when `/api/health` fails enough times in a row that
the operator needs to intervene. On the canonical setup the *cause* of
the failure depends on whether the operator is on desktop (laptop with
an SSH tunnel installed by `evolve-admin connect`) or mobile (phone with
Tailscale). Each platform needs a different remediation list:

  - Desktop's likely cause is the SSH tunnel agent or the admin daemon;
    the fix is `evolve-admin connect --status` / `--host <host>`. The
    `tailscale://` deep-link is meaningless here (the macOS app doesn't
    handle it reliably), so the Reconnect button is hidden on desktop.
  - Mobile's likely cause is the Tailscale connection itself; the fix
    is opening Tailscale, which the `tailscale://` deep-link provides.

These tests pin the markup + JS shape so a casual edit can't drift one
branch away from the spec without tripping a clear failure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_WEB = _ADMIN_PKG / "evolve_admin" / "web"
_INDEX_HTML = _WEB / "index.html"
_TUNNEL_JS = _WEB / "static" / "js" / "widgets" / "pwa-tunnel.js"
_BASE_CSS = _WEB / "static" / "css" / "base.css"


def _read_html() -> str:
    # The overlay markup still lives in index.html. The JS that wires it
    # now lives in pwa-tunnel.js and the CSS that hides the wrong-platform
    # branch lives in base.css (Phase 1 of the index.html source split).
    # Concat the three so the existing string-shape assertions stay valid
    # without per-assertion knowledge of where each substring landed.
    return "\n".join(p.read_text() for p in (_INDEX_HTML, _TUNNEL_JS, _BASE_CSS))


# ── Markup ────────────────────────────────────────────────────────────────────


def test_overlay_root_exists():
    html = _read_html()
    assert 'id="pwa-tunnel-overlay"' in html


def test_desktop_sub_copy_present():
    """Desktop sub-line names the real failure: tunnel or admin daemon."""
    html = _read_html()
    assert (
        'data-platform="desktop">The connection from your laptop to the mini'
        in html
    ), "desktop sub-line should describe the tunnel/daemon failure mode"
    assert "admin daemon" in html


def test_mobile_sub_copy_present():
    """Mobile sub-line names the Tailscale failure mode honestly — no 'tunnel'."""
    html = _read_html()
    assert (
        'data-platform="mobile">The pod may be offline, or your phone may have lost its Tailscale connection.'
        in html
    )


def test_reconnect_button_is_mobile_only():
    """Reconnect button must carry data-platform=mobile so desktop CSS hides it.

    On desktop the `tailscale://` scheme doesn't lead anywhere useful;
    the actual remediation is a shell command. Showing the button on
    desktop would be deceptive UI.
    """
    html = _read_html()
    # The Reconnect button has data-platform="mobile" attribute.
    m = re.search(
        r'<button[^>]*id="pto-reconnect-btn"[^>]*>',
        html,
    )
    assert m, "expected #pto-reconnect-btn element"
    assert 'data-platform="mobile"' in m.group(0), (
        "Reconnect button must be data-platform=mobile so the desktop "
        "branch hides it via CSS"
    )


def test_evolve_admin_connect_command_is_copy_pasteable():
    """Desktop help block must surface the actual remediation command."""
    html = _read_html()
    assert "evolve-admin connect --status" in html
    # The --host segment is rendered by JS from network data, but the
    # markup defaults to `--host mini` (canonical-install fallback).
    assert "evolve-admin connect --host mini" in html


def test_tailscale_deeplink_still_wired_for_mobile():
    """Mobile branch keeps the existing tailscale:// behaviour."""
    html = _read_html()
    assert "tailscale://" in html


def test_pod_url_row_preserved():
    """Pod URL diagnostic row should still be present (cross-platform)."""
    html = _read_html()
    assert 'id="pto-pod-url"' in html


def test_no_stale_iphone_mac_combined_row():
    """The pre-fix "On iPhone / Mac:" row conflated platforms — it must go.

    Mac (desktop) needs the SSH-tunnel commands, not the Tailscale
    advice. If this string reappears in markup, someone reverted the
    platform-branching.
    """
    html = _read_html()
    assert "On iPhone / Mac:" not in html


def test_desktop_help_rows_present():
    """Desktop help rows must carry data-platform=desktop."""
    html = _read_html()
    assert re.search(
        r'data-platform="desktop">\s*<dt>Check the connection:',
        html,
    ), "expected desktop 'Check the connection' help row"
    assert re.search(
        r'data-platform="desktop">\s*<dt>Reinstall it:',
        html,
    ), "expected desktop 'Reinstall it' help row"


# ── JS shape ──────────────────────────────────────────────────────────────────


def test_platform_detection_function_exists():
    """JS must define a platform-detection function the overlay uses."""
    html = _read_html()
    assert "_pwaTunnelDetectPlatform" in html, (
        "expected _pwaTunnelDetectPlatform() in the overlay JS — without "
        "it the data-platform attribute can't be set and CSS branches "
        "both stay visible"
    )


def test_platform_detection_handles_iphone_android_mac():
    """Detection regex must cover the canonical user agents."""
    html = _read_html()
    # Sanity: the implementation references the three canonical UA tokens.
    assert "iphone" in html
    assert "android" in html
    assert "macintosh" in html


def test_data_platform_attribute_is_set_at_init():
    """The overlay element gets data-platform set at module init, not
    lazily — otherwise the wrong copy can flash on first paint."""
    html = _read_html()
    assert "$overlay.setAttribute('data-platform'" in html


def test_reconnect_handler_guards_on_desktop():
    """Programmatic invocation of _pwaTunnelReconnect on desktop must
    not fire the tailscale:// deep-link (the button is CSS-hidden, but
    test harnesses can still call the function directly)."""
    html = _read_html()
    # The handler checks _platform before navigating.
    handler_re = re.search(
        r"window\._pwaTunnelReconnect\s*=\s*\(\)\s*=>\s*\{(.*?)\};",
        html,
        re.DOTALL,
    )
    assert handler_re, "expected window._pwaTunnelReconnect handler"
    body = handler_re.group(1)
    assert "_platform" in body, (
        "Reconnect handler must early-return on desktop (no tailscale:// fire)"
    )


def test_css_hides_wrong_platform_branch():
    """CSS contract: data-platform on the overlay hides the other branch."""
    html = _read_html()
    assert (
        '#pwa-tunnel-overlay[data-platform="desktop"] [data-platform="mobile"]'
        in html
    )
    assert (
        '#pwa-tunnel-overlay[data-platform="mobile"] [data-platform="desktop"]'
        in html
    )


def test_install_command_id_for_dynamic_host_rewrite():
    """The install command has an id so JS can rewrite the host
    placeholder from network data."""
    html = _read_html()
    assert 'id="pto-cmd-install"' in html


def test_derive_host_function_exists():
    html = _read_html()
    assert "_pwaTunnelDeriveHost" in html
