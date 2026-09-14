"""tests/test_alerts_per_row_actions.py — UI shape checks for the
Reports → Alerts per-row actions, bulk-action bar, and filter chips.

Pins the structural markup added to surface signal Snooze / Dismiss /
Resolve at the row level + a sticky bulk-action bar + filter chips.

The 2026-05-21 audit-noise transcript landed the operator on 87 firing
signals with no in-UI path to dismiss them in bulk; this PR closes that
gap and these tests pin the affordances so they don't silently
regress out of the source.

Tests are regex-on-source checks against index.html — no browser is
spun up. Each test name maps to one structural invariant.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_ALERTS_EXTENDED_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "alerts-extended.js"
_ALERTS_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "static" / "js" / "pages" / "alerts.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _ALERTS_JS.read_text(encoding="utf-8") + "\n" + _ALERTS_EXTENDED_JS.read_text(encoding="utf-8")


# ── Part 1: per-row inline action buttons (Snooze ▾ / Dismiss ▾ / Resolve)

def test_signal_row_has_per_row_snooze_button():
    """Every signal row renders a "Snooze ▾" split button in the row
    header — visible without expanding the <details> disclosure."""
    html = _html()
    # The renderer is _alSignalRow; the button class is al-row-snooze.
    assert "al-row-snooze" in html, "per-row Snooze button class missing"
    assert "Snooze ▾" in html, "per-row Snooze ▾ split-button label missing"


def test_signal_row_has_per_row_dismiss_button_with_verdict_options():
    """Per-row Dismiss ▾ split button exposes the three verdicts inline
    (false_positive / bad_inference / not_actionable) so the operator
    can pick one without opening the full modal."""
    html = _html()
    # Find the _alSignalRow function body and verify all three verdicts
    # appear as inline options.
    fn = re.search(r"function _alSignalRow\(sig\)\s*\{(.+?)\nfunction ",
                   html, re.DOTALL)
    assert fn, "_alSignalRow function not found"
    body = fn.group(1)
    assert "al-row-dismiss" in body, "Dismiss ▾ class missing on row button"
    assert "_alSingleDismiss" in body, (
        "Per-row dismiss should route through _alSingleDismiss for fast-path "
        "verdict picking (skips the modal)"
    )
    # Each verdict must be wired as a menu option.
    for verdict in ("false_positive", "bad_inference", "not_actionable"):
        assert verdict in body, (
            f"verdict {verdict!r} missing from per-row dismiss menu"
        )


def test_signal_row_has_per_row_resolve_button():
    """Per-row Resolve button calls _alResolve directly."""
    html = _html()
    fn = re.search(r"function _alSignalRow\(sig\)\s*\{(.+?)\nfunction ",
                   html, re.DOTALL)
    assert fn
    body = fn.group(1)
    assert "al-row-resolve" in body, "per-row Resolve button class missing"
    # The button label appears as plain "Resolve" (not "Mark resolved"
    # which is the older in-body button).
    assert ">Resolve<" in body, "per-row Resolve button label missing"


def test_signal_row_snooze_menu_has_three_durations():
    """Per-row Snooze popover surfaces 1 hour / 1 day / 1 week options
    — these map to the 1h/1d/1w shorthand the signal store parses."""
    html = _html()
    fn = re.search(r"function _alSignalRow\(sig\)\s*\{(.+?)\nfunction ",
                   html, re.DOTALL)
    assert fn
    body = fn.group(1)
    # The three duration shorthands all appear in the snooze menu.
    for dur in ("'1h'", "'1d'", "'1w'"):
        assert f"_alSnooze('${{sigIdJs}}', {dur})" in body, (
            f"per-row snooze menu missing duration {dur}"
        )


def test_signal_row_action_buttons_stop_event_propagation():
    """Per-row buttons must call event.stopPropagation() / preventDefault
    so clicking an action button doesn't toggle the <details> disclosure
    or surface a phantom click on the surrounding summary."""
    html = _html()
    fn = re.search(r"function _alSignalRow\(sig\)\s*\{(.+?)\nfunction ",
                   html, re.DOTALL)
    assert fn
    body = fn.group(1)
    assert "stopPropagation" in body, (
        "per-row action buttons must stopPropagation so clicking them "
        "doesn't toggle the <details> disclosure"
    )
    assert "preventDefault" in body, (
        "per-row action buttons must preventDefault for the same reason"
    )


# ── Part 2: multi-select + bulk-action bar

def test_signal_row_has_multiselect_checkbox():
    """Each row exposes a checkbox.al-row-select for bulk-action capture."""
    html = _html()
    fn = re.search(r"function _alSignalRow\(sig\)\s*\{(.+?)\nfunction ",
                   html, re.DOTALL)
    assert fn
    body = fn.group(1)
    assert 'class="al-row-select"' in body, (
        "per-row multi-select checkbox class missing"
    )
    assert "_alSelectionChanged" in body, (
        "checkbox must invoke _alSelectionChanged on change to refresh the bar"
    )


def test_bulk_action_bar_markup_present():
    """Sticky bottom bar shows up by id, with bulk Snooze / Dismiss /
    Resolve / Clear controls."""
    html = _html()
    assert 'id="al-bulk-bar"' in html, "sticky bulk-action bar missing"
    assert "Bulk Snooze ▾" in html, "Bulk Snooze split button missing"
    assert "Bulk Dismiss ▾" in html, "Bulk Dismiss split button missing"
    assert "Bulk Resolve" in html, "Bulk Resolve button missing"
    assert 'id="al-bulk-count"' in html, "bulk-bar selection count slot missing"


def test_bulk_action_bar_wires_to_bulk_endpoint():
    """The bulk handlers must POST to /api/signals/bulk-action with the
    signal_ids + action contract."""
    html = _html()
    assert "/api/signals/bulk-action" in html, (
        "bulk handlers must POST to /api/signals/bulk-action"
    )
    fn = re.search(r"async function _alBulkPost\([^)]*\)\s*\{(.+?)\n\}",
                   html, re.DOTALL)
    assert fn, "_alBulkPost helper not found"
    body = fn.group(1)
    assert "/api/signals/bulk-action" in body


def test_bulk_dismiss_routes_through_confirm_modal():
    """Bulk dismiss is destructive; it MUST gate through the confirm
    modal (Part 4) rather than firing on the menu click directly."""
    html = _html()
    # The menu option calls _alBulkDismissConfirm, NOT a direct submit.
    assert "_alBulkDismissConfirm" in html, "bulk-dismiss confirm flow missing"
    # And the confirm modal is in the DOM.
    assert 'id="al-bulk-confirm-modal"' in html, (
        "bulk-dismiss confirmation modal markup missing"
    )
    # Modal has a verdict-label slot so the operator sees what they're confirming.
    assert 'id="al-bulk-confirm-verdict-label"' in html


def test_bulk_dismiss_submit_only_fires_after_confirm():
    """_alBulkDismissSubmit is the only path that actually POSTs the
    bulk dismiss — it must be guarded by the pending state set in
    _alBulkDismissConfirm so a stray click can't fire it."""
    html = _html()
    fn = re.search(r"async function _alBulkDismissSubmit\([^)]*\)\s*\{(.+?)\n\}",
                   html, re.DOTALL)
    assert fn, "_alBulkDismissSubmit not found"
    body = fn.group(1)
    assert "_alBulkDismissPendingVerdict" in body
    assert "_alBulkDismissPendingIds" in body


def test_select_all_visible_button_present():
    """The bulk-bar contract surfaces a "Select all visible" affordance
    so the operator can act on a chip-filtered slice in two clicks."""
    html = _html()
    assert "Select all visible" in html, (
        '"Select all visible" button missing — needed to combine '
        "filter chips with bulk action in <5 clicks"
    )
    assert "_alSelectAllVisible" in html


# ── Part 3: filter chips

def test_filter_chip_section_present():
    """Filter-chip container is in the DOM (hidden until populated)."""
    html = _html()
    assert 'id="al-filter-chips"' in html, "filter-chip container missing"


def test_filter_chip_dimensions_include_producer_bot_severity():
    """_alRenderFilterChips builds chips for producer / bot / severity —
    the three dimensions the operator wants to slice the 87-row noise
    pile by."""
    html = _html()
    fn = re.search(r"function _alRenderFilterChips\([^)]*\)\s*\{(.+?)\n\}",
                   html, re.DOTALL)
    assert fn, "_alRenderFilterChips not found"
    body = fn.group(1)
    # All three dimensions get a dim() call rendering chips.
    assert "producer" in body
    assert "bot" in body
    assert "severity" in body


def test_filter_chip_toggle_and_clear_handlers_present():
    """Chips support per-chip toggle + a single Clear filters action.
    AND-across-dimensions + OR-within-dimension semantics live in
    _alApplyChipFilters."""
    html = _html()
    assert "function _alToggleChip" in html, "chip toggle handler missing"
    assert "function _alClearChips" in html, "chip clear handler missing"
    assert "function _alApplyChipFilters" in html, (
        "chip filter-apply (AND across, OR within) missing"
    )


def test_signal_row_carries_filter_dimensions_as_data_attrs():
    """_alSignalRow must stamp data-producer / data-bot-id /
    data-severity on the row so _alApplyChipFilters can read them
    back without re-fetching."""
    html = _html()
    fn = re.search(r"function _alSignalRow\(sig\)\s*\{(.+?)\nfunction ",
                   html, re.DOTALL)
    assert fn
    body = fn.group(1)
    assert "data-producer" in body, "row missing data-producer attr"
    assert "data-bot-id" in body, "row missing data-bot-id attr"
    assert "data-severity" in body, "row missing data-severity attr"
    assert 'class="al-signal-row"' in body or "al-signal-row" in body, (
        "row missing al-signal-row class for chip selector"
    )


# ── Part 4: confirmation gate for bulk-dismiss

def test_bulk_confirm_modal_explains_irreversibility():
    """The modal text must tell the operator dismiss is one-way
    (resolves can be undone via Resolve; dismisses cannot)."""
    html = _html()
    # Pull out the modal markup and check the explanatory copy.
    modal_match = re.search(
        r'id="al-bulk-confirm-modal".*?</div>\s*</div>\s*</div>',
        html, re.DOTALL,
    )
    assert modal_match, "al-bulk-confirm-modal markup not found"
    modal = modal_match.group(0)
    # Some signal that the destructive op is explained.
    assert "History" in modal, "modal should mention they go to History"
    assert "cannot" in modal.lower() or "irreversible" in modal.lower(), (
        "modal should signal dismiss is one-way"
    )


# ── Context-pack preservation (don't break PR #1369)

def test_reports_context_snapshot_still_spreads_prev():
    """PR #1366 + #1369 established that the reports context-pack
    snapshot writer must spread ``_prevReports`` so concurrent
    producers don't clobber each other's sibling fields. This PR
    is additive; the spread must still be there."""
    html = _html()
    # Find the reports snapshot-writer block.
    block = re.search(
        r"window\._evoContextSnapshots\.reports\s*=\s*\{(.+?)\};",
        html, re.DOTALL,
    )
    assert block, "reports context-pack writer not found"
    body = block.group(1)
    assert "_prevReports" in body and "..." in body, (
        "reports snapshot writer dropped the ..._prevReports spread "
        "from PR #1366 — concurrent producers will clobber each other"
    )


def test_firing_top_still_includes_signal_id():
    """PR #1369 added `id` to firing_top so evo can call
    action.signal.* without re-fetching. The new UI is additive — that
    field must still ship to the context pack."""
    html = _html()
    fn = re.search(r"const projectSig = \([^)]*\)\s*=>\s*\(\{(.+?)\}\);",
                   html, re.DOTALL)
    assert fn, "projectSig helper not found in _alLoadLane"
    body = fn.group(1)
    assert "id:" in body, "projectSig must surface signal id for evo"


# ── Part 5: truncation banner (closes the 2026-05-26 bulk-dismiss bug)

def test_signals_fetch_uses_bumped_limit():
    """_alLoadLane must request limit=1000, not the legacy 200. With
    ~250 active maintenance signals on a real pod, limit=200 silently
    truncated producers out of the client-side chip filter so
    bulk-dismissing "all visible" left hidden matches firing."""
    html = _html()
    assert "/api/signals?flavor=${apiFlavor}&limit=1000" in html, (
        "_alLoadLane must fetch with limit=1000 (server max), "
        "not the legacy 200 that silently truncates"
    )


def test_truncation_banner_element_present():
    """The banner element is in the DOM (hidden until the renderer
    populates it). Without this, the client-side chip filter can lie
    about how many signals match a producer."""
    html = _html()
    assert 'id="al-truncation-banner"' in html, (
        "truncation-disclosure banner element missing from "
        "reports-alerts-firing page"
    )


def test_truncation_banner_renderer_invoked_on_reload():
    """_alLoadLane must call _alRenderTruncationBanner with the
    response after each maintenance reload, so the banner stays in
    sync when alerts get dismissed and the total shrinks."""
    html = _html()
    assert "function _alRenderTruncationBanner" in html, (
        "_alRenderTruncationBanner helper missing"
    )
    fn = re.search(r"async function _alLoadLane\(flavor\)\s*\{(.+?)\n\}",
                   html, re.DOTALL)
    assert fn, "_alLoadLane not found"
    body = fn.group(1)
    assert "_alRenderTruncationBanner(" in body, (
        "_alLoadLane must invoke the banner renderer with the API "
        "response so total/count drift is reflected in the UI"
    )


def test_truncation_banner_hides_when_total_equals_count():
    """When the response fits under the cap, the banner must be
    hidden — otherwise the operator sees a "showing N of N" banner
    every time and learns to ignore it."""
    html = _html()
    fn = re.search(r"function _alRenderTruncationBanner\([^)]*\)\s*\{(.+?)\n\}",
                   html, re.DOTALL)
    assert fn, "_alRenderTruncationBanner not found"
    body = fn.group(1)
    # Banner reads total + count and hides itself when total <= count.
    assert "total" in body and "count" in body, (
        "renderer should read total + count from the response"
    )
    assert "total <= count" in body or "total<=count" in body, (
        "renderer must hide the banner when total <= count"
    )
