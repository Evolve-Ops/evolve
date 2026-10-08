"""tests/test_apps_create_recent_drafts_clickable.py — Apps → Create "Recent
drafts" rows must be clickable so the operator can resume an in-flight spec
session or re-approve a draft.

Pre-fix: appsLoadCreate built each row as a static <div> with no onclick;
clicking did nothing. Operator after a Forge-job failure had no way back
into the wizard for the existing draft session without crafting a URL.

Same tier as test_create_app_wizard_step3_ui — static-string assertions
against index.html. Catches "the affordance is silently missing or
mis-wired in the rendered HTML."
"""

from __future__ import annotations

from pathlib import Path

import pytest


_INDEX = Path(__file__).parent.parent / "evolve_admin" / "web" / "index.html"


_CREATE_APP_WIZARD_JS = Path(__file__).parent.parent / "evolve_admin" / "web"/ "static" / "js" / "pages" / "create-app-wizard.js"
@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX.read_text() + "\n" + _CREATE_APP_WIZARD_JS.read_text()


def test_draft_row_has_click_handler(html: str):
    """The recent-drafts row template must wire onclick → appsOpenDraft."""
    # The row template lives inside appsLoadCreate; the click handler
    # reads the session id from a data-sid attribute.
    assert 'class="apps-draft-row"' in html
    assert 'onclick="appsOpenDraft(this.dataset.sid)"' in html
    assert 'data-sid="${escHtml(s.session_id || \'\')}"' in html


def test_draft_row_has_hover_state(html: str):
    """Operators need a visual signal the row is clickable. The row
    template must change background/border on hover."""
    # Inline mouseover/mouseout handlers swap the background + border —
    # matches the rest of the admin UI's hover style (no CSS class hooks
    # available for this specific section).
    assert 'onmouseover="this.style.background=' in html
    assert 'onmouseout="this.style.background=' in html
    # The row's cursor must signal clickability up-front.
    assert 'cursor:pointer' in html


def test_apps_open_draft_function_defined(html: str):
    assert "async function appsOpenDraft(sessionId)" in html


def test_apps_open_draft_fetches_session_detail(html: str):
    """Handler must call GET /api/specs/<id> to load the full session
    (the list endpoint doesn't return drafts/generation/forge_jobs)."""
    idx = html.find("async function appsOpenDraft(sessionId)")
    assert idx >= 0
    body = html[idx:idx + 4000]
    assert "/api/specs/${encodeURIComponent(sessionId)}" in body


def test_apps_open_draft_routes_by_status(html: str):
    """The handler must dispatch to the right wizard step based on the
    session's status. Both forms of in-flight (gathering/iterating)
    resume polling; approved/queued land on Step 3; draft lands on
    Step 2."""
    idx = html.find("async function appsOpenDraft(sessionId)")
    assert idx >= 0
    body = html[idx:idx + 4000]
    # Streaming resume for in-flight generations.
    assert "'gathering'" in body
    assert "'iterating'" in body
    assert "_wizardResumeStreamPoll(session.session_id)" in body
    # Forge-jobs view for approved/queued.
    assert "'approved'" in body
    assert "'queued'" in body
    # All paths invoke _wizardRender after setting _wizardStep.
    assert "_wizardRender()" in body


def test_apps_open_draft_opens_modal(html: str):
    """Without making the modal visible, no UI change would be perceptible."""
    idx = html.find("async function appsOpenDraft(sessionId)")
    assert idx >= 0
    body = html[idx:idx + 4000]
    assert "document.getElementById('create-app-wizard')" in body
    assert "el.style.display = 'flex'" in body


def test_apps_open_draft_sets_wizard_session(html: str):
    """Step 2 / Step 3 rendering reads from _wizardSession — the handler
    must populate it from the loaded session before _wizardRender runs."""
    idx = html.find("async function appsOpenDraft(sessionId)")
    assert idx >= 0
    body = html[idx:idx + 4000]
    assert "_wizardSession = {" in body
    assert "session_id: session.session_id" in body
    assert "draft: latestDraft" in body


def test_apps_open_draft_loads_forge_jobs(html: str):
    """Step 3 renders from _wizardForgeJobs. For an approved/queued
    session reopened from the drafts list, the handler must seed this
    from the loaded session — otherwise Step 3 shows 'No forge jobs
    returned.'"""
    idx = html.find("async function appsOpenDraft(sessionId)")
    assert idx >= 0
    body = html[idx:idx + 4000]
    assert "_wizardForgeJobs = session.forge_jobs || []" in body


def test_resume_stream_poll_function_defined(html: str):
    assert "async function _wizardResumeStreamPoll(sessionId)" in html


def test_resume_stream_poll_lands_on_step2_when_done(html: str):
    """When the worker finishes mid-resume, the wizard should transition
    to Step 2 with the new draft — same as wizardGenerate/wizardIterate
    onDone."""
    idx = html.find("async function _wizardResumeStreamPoll(sessionId)")
    assert idx >= 0
    body = html[idx:idx + 4000]
    # onDone path sets step 2.
    assert "_wizardStep = 2" in body
    # Pulls the newest draft off the polled session.
    assert "sess.drafts[sess.drafts.length - 1]" in body


def test_resume_stream_poll_handles_terminal_states(html: str):
    """failed / cancelled paths must surface an error and return — not
    spin forever."""
    idx = html.find("async function _wizardResumeStreamPoll(sessionId)")
    assert idx >= 0
    body = html[idx:idx + 4000]
    assert "genStatus === 'failed'" in body
    assert "genStatus === 'cancelled'" in body
