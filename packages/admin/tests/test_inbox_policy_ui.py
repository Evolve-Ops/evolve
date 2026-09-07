"""Markup + JS contract tests for the Phase 5 auto-response UI.

Pins the load-bearing markup of:
  - The policy panel (collapsible, lives inside the Triage card)
  - The auto-action status pill on the detail card
  - The Apply / Undo buttons + their wiring to the new endpoints
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
_INBOX_JS = _WEB / "static" / "js" / "pages" / "inbox.js"


def _read_html() -> str:
    # Inbox JS now lives in pages/inbox.js (Phase 3b of the source split).
    # Concat so the existing regex-shape assertions stay valid.
    return _INDEX_HTML.read_text() + "\n" + _INBOX_JS.read_text()


# ── Policy panel markup ──────────────────────────────────────────────────────


def test_policy_panel_lives_inside_triage_card():
    """The policy belongs to the Triage card — it controls the
    auto-responder, which only operates on triage items. Putting it
    elsewhere disconnects it from the context it modifies."""
    html = _read_html()
    card_start = html.find('id="inbox-triage-card"')
    card_end = html.find('id="inbox-triage-detail-card"')
    assert card_start > 0 and card_end > card_start
    card_block = html[card_start:card_end]
    assert 'id="inbox-triage-policy-card"' in card_block


def test_policy_panel_starts_collapsed():
    """The panel's body must start hidden so the operator isn't
    confronted with config on first load."""
    html = _read_html()
    m = re.search(
        r'<div[^>]*id="inbox-triage-policy-body"[^>]*>',
        html,
    )
    assert m is not None
    assert "display:none" in m.group(0)


def test_policy_panel_has_enable_checkboxes_for_each_kind():
    """All three action kinds must be toggleable independently — the
    global flag is a kill switch; the per-kind flags pick what fires."""
    html = _read_html()
    for elem_id in (
        "inbox-triage-policy-enabled",
        "inbox-triage-policy-close-dup",
        "inbox-triage-policy-reply",
        "inbox-triage-policy-label",
    ):
        assert f'id="{elem_id}"' in html


def test_policy_panel_has_confidence_inputs():
    """Each kind needs its own confidence floor input — defaults are
    high (0.9+) but the operator can lower them per-kind."""
    html = _read_html()
    for elem_id in (
        "inbox-triage-policy-close-dup-conf",
        "inbox-triage-policy-reply-conf",
        "inbox-triage-policy-label-conf",
    ):
        assert f'id="{elem_id}"' in html


def test_policy_panel_save_button_present():
    html = _read_html()
    assert "_inboxPolicySave()" in html


# ── Detail card: auto-action status + Apply / Undo controls ─────────────────


def test_detail_card_has_auto_status_pill():
    """The status pill renders the AutoActionRecord (acted_at, kind,
    undo deadline). Starts hidden — only shown when auto_action exists."""
    html = _read_html()
    assert 'id="inbox-triage-detail-auto-status"' in html
    m = re.search(
        r'<div[^>]*id="inbox-triage-detail-auto-status"[^>]*>',
        html,
    )
    assert m is not None
    assert "display:none" in m.group(0)


def test_detail_card_has_apply_button():
    html = _read_html()
    assert 'id="inbox-triage-detail-apply-btn"' in html
    assert "_inboxTriageApply()" in html


def test_detail_card_has_apply_help_text():
    """The Apply control needs an inline help blurb so the operator
    knows what's about to happen + that undo is available."""
    html = _read_html()
    assert 'id="inbox-triage-detail-apply-help"' in html


# ── JS function shape ──────────────────────────────────────────────────────


def test_apply_function_defined():
    html = _read_html()
    assert "async function _inboxTriageApply()" in html


def test_undo_function_defined():
    html = _read_html()
    assert "async function _inboxTriageUndo()" in html


def test_policy_save_function_defined():
    html = _read_html()
    assert "async function _inboxPolicySave()" in html


def test_load_policy_function_defined():
    html = _read_html()
    assert "async function loadInboxPolicy()" in html


def test_render_auto_section_function_defined():
    html = _read_html()
    assert "function _inboxRenderTriageAutoSection(" in html


# ── API wiring ─────────────────────────────────────────────────────────────


def test_apply_calls_correct_endpoint():
    """POST /api/inbox/triage/<id>/apply — pin the URL shape so the
    backend route can't drift away from the UI."""
    html = _read_html()
    assert re.search(
        r"/api/inbox/triage/\$\{[^}]+\}/apply",
        html,
    )


def test_apply_uses_post():
    html = _read_html()
    start = html.find("async function _inboxTriageApply()")
    body = html[start:start + 2000]
    assert "method: 'POST'" in body or 'method: "POST"' in body


def test_undo_calls_correct_endpoint():
    html = _read_html()
    assert re.search(
        r"/api/inbox/triage/\$\{[^}]+\}/undo",
        html,
    )


def test_undo_uses_post():
    html = _read_html()
    start = html.find("async function _inboxTriageUndo()")
    body = html[start:start + 1500]
    assert "method: 'POST'" in body or 'method: "POST"' in body


def test_undo_uses_confirm_prompt():
    """Undo reverses a public GitHub action — operator must confirm."""
    html = _read_html()
    start = html.find("async function _inboxTriageUndo()")
    body = html[start:start + 1500]
    assert "confirmModal(" in body


def test_policy_save_calls_correct_endpoint():
    html = _read_html()
    start = html.find("async function _inboxPolicySave()")
    body = html[start:start + 2000]
    assert "/api/inbox/triage/policy" in body
    assert "method: 'POST'" in body or 'method: "POST"' in body


def test_apply_escapes_intake_id_in_url():
    """The intake id flows into the URL — must be encoded to defend
    against any future id formats with special characters."""
    html = _read_html()
    start = html.find("async function _inboxTriageApply()")
    body = html[start:start + 2000]
    assert "encodeURIComponent" in body


def test_undo_escapes_intake_id_in_url():
    html = _read_html()
    start = html.find("async function _inboxTriageUndo()")
    body = html[start:start + 1500]
    assert "encodeURIComponent" in body
