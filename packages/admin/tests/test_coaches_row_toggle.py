"""tests/test_coaches_row_toggle.py — pin the inline pause/resume
control on the Coaches tab row.

Background: server-side pause/resume already worked (verified in
``test_arbiter_endpoints.py::test_generator_pause_then_resume``), but
the UI only surfaced the toggle inside the detail modal. The
Coaches table now exposes a row-level button so the operator can
silence a noisy coach without opening the modal. These tests pin the
row's HTML shape so a future cleanup that drops the button fails
loudly.

Tests are source-pins on index.html (same pattern as
``test_jump_to_proposals_routing.py``). They don't run the JS.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_SELF_IMPROVEMENT_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "self-improvement.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _SELF_IMPROVEMENT_JS.read_text(encoding="utf-8")


def _render_generators_body() -> str:
    """Return the body of ``renderGenerators(data)`` so the tests
    can inspect the row template without grepping the whole file
    (which has multiple similarly-shaped tables)."""
    html = _html()
    m = re.search(
        r"function renderGenerators\(data\)\s*\{(.+?)\n\}\n",
        html, re.DOTALL,
    )
    assert m, "renderGenerators function body not found"
    return m.group(1)


def test_row_has_actions_column_header():
    """The table header must include an Actions column so the
    pause/resume button has a labeled place to live."""
    body = _render_generators_body()
    assert ">Actions<" in body, (
        "Coaches table header missing the Actions column. The "
        "inline pause/resume control needs a labeled column."
    )


def test_active_status_row_renders_pause_button():
    """When a coach is ``active`` the row gets a Pause button. The
    button must:
      - call ``pauseGenerator(id)`` (the existing handler, which
        already prompts for a reason + refreshes the table)
      - stop propagation so the click doesn't also fire the row's
        openGeneratorDetail handler (which would open the modal)."""
    body = _render_generators_body()
    # Find the branch for status === 'active'. Look for the literal
    # combination 'status === ' followed by 'active' since the
    # rendered branch builds a string containing ``pauseGenerator``.
    assert "status === 'active'" in body, (
        "active-status branch missing from row renderer"
    )
    # The Pause button onclick must include both stopPropagation and
    # the pauseGenerator call.
    pause_call = re.search(
        r"onclick=\"event\.stopPropagation\(\);pauseGenerator\(",
        body,
    )
    assert pause_call, (
        "Pause button onclick must call event.stopPropagation() "
        "and pauseGenerator(id). Without stopPropagation the click "
        "also opens the detail modal; without pauseGenerator the "
        "button does nothing."
    )


def test_paused_status_row_renders_resume_button():
    """When a coach is ``paused`` the row gets a Resume button. Same
    contract as Pause: stopPropagation + call the existing handler."""
    body = _render_generators_body()
    assert "status === 'paused'" in body, (
        "paused-status branch missing from row renderer"
    )
    resume_call = re.search(
        r"onclick=\"event\.stopPropagation\(\);resumeGenerator\(",
        body,
    )
    assert resume_call, (
        "Resume button onclick must call event.stopPropagation() "
        "and resumeGenerator(id)"
    )


def test_quarantined_status_does_not_render_toggle():
    """Quarantined coaches must NOT get a row-level toggle. The
    quarantine_reason and the unblock flow live in the detail modal;
    a row button here would let an operator paper over a real fault."""
    body = _render_generators_body()
    # The fallback branch (the else after active / paused checks)
    # must render a non-button placeholder. We check for the absence
    # of pauseGenerator / resumeGenerator wired to a quarantined-
    # status branch by confirming there are at most TWO mentions of
    # each — one in the explicit active branch, one in the explicit
    # paused branch — and not a third in the fallback.
    pause_count = body.count("pauseGenerator(")
    resume_count = body.count("resumeGenerator(")
    assert pause_count == 1, (
        f"Expected pauseGenerator wired exactly once (active branch); "
        f"got {pause_count}. A third call would mean the quarantined "
        f"fallback wires the toggle — that's the bug this test "
        f"guards against."
    )
    assert resume_count == 1, (
        f"Expected resumeGenerator wired exactly once (paused branch); "
        f"got {resume_count}."
    )
