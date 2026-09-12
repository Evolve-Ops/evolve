"""tests/test_reports_context_snapshot.py — guard that the Reports
page's context-pack ('reports' entry in _EVO_CONTEXT_PACKS) surfaces
the firing-alerts signal data, and that its snapshot writer
preserves siblings.

Closes the gap from before this PR: the operator on Reports → Alerts
asks evo about a visible firing signal, and evo had no structured
awareness of what was on screen — only the raw
``pod_state.signals.firing`` tool, no notion of which alerts the
operator was actually looking at. The prior 'reports' pack entry
was tool-pointers-only; this test pins the richer pack and its
writer.

Pattern lifted from test_security_context_snapshot.py (PR #1366) —
regex on the HTML source. Tests SHAPE, not behavior. The runtime
is exercised live on the mini.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_INDEX_HTML = _ADMIN_PKG / "evolve_admin" / "web" / "index.html"


_EVO_DRAWER_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "evo-drawer.js"
_ALERTS_JS = _ADMIN_PKG / "evolve_admin" / "web" / "static" / "js" / "pages" / "alerts.js"
@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8") + "\n" + _ALERTS_JS.read_text(encoding="utf-8") + "\n" + _EVO_DRAWER_JS.read_text(encoding="utf-8")


def test_reports_snapshot_writer_preserves_siblings(html: str):
    """Every assignment to ``window._evoContextSnapshots.reports``
    must spread the prior value, so concurrent writers (alerts
    poll, subscription poll, watchlist load, …) don't clobber each
    other's fields.

    Without this guarantee, the alerts-list writer in ``_alLoadLane``
    can blow away sibling fields written by other producers on the
    Reports page — exactly the 2026-05-20 snapshot-clobber bug
    shape that the security-snapshot test guards against.
    """
    pattern = re.compile(
        r"window\._evoContextSnapshots\.reports\s*=\s*\{([\s\S]*?)\n\s*\};",
        re.MULTILINE,
    )
    matches = pattern.findall(html)
    assert matches, (
        "no assignments to window._evoContextSnapshots.reports found. "
        "If the snapshot mechanism was refactored, update this test "
        "to match the new shape."
    )
    for i, body in enumerate(matches):
        # Spread can be ``..._prev``, ``...prev``, ``..._prevReports``
        # (suffixed for disambiguation between concurrent writers in
        # the same scope), etc. We accept any identifier whose stem is
        # ``prev`` / ``_prev``.
        has_spread = bool(re.search(r"\.\.\.\s*_?prev[A-Za-z0-9_]*", body))
        assert has_spread, (
            f"_evoContextSnapshots.reports assignment #{i+1} doesn't "
            f"spread the prior value. Without the spread, sibling "
            f"fields written by other producers on the Reports page "
            f"get clobbered every time the alerts poll runs. Body was:\n"
            f"{body[:300]}"
        )


def _locate_reports_pack(html: str) -> str:
    """Return a window of HTML containing the 'reports' entry of
    _EVO_CONTEXT_PACKS. Returns ~6000 chars from the entry start —
    big enough to cover any plausible builder body."""
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    if packs_start < 0:
        packs_start = html.find("_EVO_CONTEXT_PACKS = {")
    assert packs_start > 0, (
        "could not locate the _EVO_CONTEXT_PACKS declaration. If the "
        "registry was renamed, update this test."
    )
    # The 'reports' key may appear earlier in commentary; anchor at the
    # actual builder declaration after the registry's opening brace.
    start = html.find("'reports':", packs_start)
    if start < 0:
        start = html.find('"reports":', packs_start)
    assert start > 0, (
        "could not locate the 'reports' entry inside _EVO_CONTEXT_PACKS. "
        "If the key shape was refactored, update this test."
    )
    return html[start:start + 6000]


def test_reports_pack_entry_exists(html: str):
    """The 'reports' page-context-pack entry must exist in
    _EVO_CONTEXT_PACKS — without it, evo on Reports → Alerts has no
    page-context block beyond page_id+page_label."""
    window = _locate_reports_pack(html)
    # The builder should reference the snapshot it reads.
    assert (
        "_evoContextSnapshots" in window
        and "reports" in window
    ), (
        "the 'reports' context-pack builder no longer reads from "
        "``window._evoContextSnapshots.reports``. The pack is then "
        "static and can't reflect what's on screen."
    )


def test_reports_pack_surfaces_firing_signals(html: str):
    """The pack must surface the firing-alerts list to evo — items
    array sourced from the snapshot's firing_top. Without this,
    the operator's "what's the worst alert?" / "snooze that one"
    question has no on-screen context."""
    window = _locate_reports_pack(html)
    assert "firing_top" in window, (
        "the 'reports' context-pack builder no longer reads "
        "``snap.firing_top`` — the firing-alerts list isn't reaching "
        "evo. The Reports → Alerts gap returns."
    )
    assert "firing_count" in window, (
        "the 'reports' context-pack builder no longer surfaces "
        "firing_count — evo can't tell the operator the totals "
        "without scanning the items list."
    )


def test_reports_pack_points_at_signal_tools(html: str):
    """The pack's tool_pointers must include the firing-signal
    primary tool + the snooze/dismiss action tools. Without these,
    evo can read the pack but doesn't know which tool to call when
    the operator says "snooze it" — the action that closes the loop.
    """
    window = _locate_reports_pack(html)
    # Post-B7-Phase-2: the pack teaches the consolidated facade forms.
    for tool_name in (
        'pod_state(query="signals.firing"',
        'pod_state(query="signals.history"',
        'signal_action(action="snooze"',
        'signal_action(action="dismiss"',
    ):
        assert tool_name in window, (
            f"the 'reports' context-pack no longer points at "
            f"``{tool_name}``. Evo will read the pack but have no "
            f"guidance on which tool maps to the on-screen actions — "
            f"and may either fabricate a tool name or refuse to act."
        )


def test_reports_pack_tracks_active_subtab_and_inner(html: str):
    """The Reports page has three outer subtabs (Subscriptions /
    Alerts / Watchlist) and the Alerts subtab has three inner
    subtabs (Firing / History / Configure). The pack must surface
    BOTH so headline framing matches what the operator's looking at —
    otherwise evo on the Configure subtab gets a "firing alerts"
    framing that's wrong for the actual UI state."""
    window = _locate_reports_pack(html)
    assert "active_subtab" in window, (
        "the 'reports' context-pack no longer tracks active_subtab. "
        "Evo can't tell whether the operator is on Subscriptions, "
        "Alerts, or Watchlist — and may answer Watchlist questions "
        "as if they were Alerts questions."
    )
    assert "active_inner" in window, (
        "the 'reports' context-pack no longer tracks active_inner. "
        "Evo can't tell whether the operator is on Alerts → Firing "
        "vs Alerts → History vs Alerts → Configure — and may "
        "fabricate a firing-list framing when the operator is on a "
        "configuration page."
    )
