"""tests/test_forge_jobs_ui.py — Forge Jobs page UX gaps.

Static-string assertions against index.html for the four affordances
added in this PR:

  1. Retry button on failed / cancelled / rejected rows
  2. "View manifest" button on every expanded row
  3. "This job did not run" banner when status=failed and current_step is 0/1
  4. Failure-step detail block for failed jobs that DID start

Same tier as test_wizard_frontend — won't catch every UI bug, but
catches "the affordance is silently missing from the rendered HTML."
"""

from __future__ import annotations

from pathlib import Path

import pytest


_WEB = Path(__file__).parent.parent / "evolve_admin" / "web"
_INDEX = _WEB / "index.html"
_FORGE_JS = _WEB / "static" / "js" / "pages" / "forge.js"


@pytest.fixture(scope="module")
def html() -> str:
    # Forge page JS now lives in pages/forge.js (Phase 3c of the source
    # split). Concat so the existing regex-shape assertions stay valid.
    return _INDEX.read_text() + "\n" + _FORGE_JS.read_text()


# ── Action cell: Retry button ────────────────────────────────────────────────


def test_failed_row_has_retry_button(html: str):
    """Failed jobs must render a Retry button in the actions cell —
    previously they showed only the dead grey "failed" text."""
    assert 'data-action="retry"' in html
    assert "forgeRetry(this.dataset.jid)" in html


def test_rejected_row_has_retry_button(html: str):
    """Rejected jobs also get retry — operator may have rejected for
    a fixable reason and want another go without re-creating the job."""
    # Both failed/cancelled and rejected branches reference forgeRetry
    count = html.count("forgeRetry(this.dataset.jid)")
    assert count >= 2, "forgeRetry should be wired in both failed and rejected action cells"


def test_forge_retry_function_defined(html: str):
    assert "async function forgeRetry(jobId)" in html


def test_forge_retry_calls_retry_endpoint(html: str):
    """The JS must hit POST /api/forge/jobs/<id>/retry (the new endpoint
    added in this PR), not some legacy reset path."""
    assert "/api/forge/jobs/${jobId}/retry" in html


# ── Manifest link ────────────────────────────────────────────────────────────


def test_expanded_row_has_view_manifest_button(html: str):
    """Every expanded forge job row must include a View manifest button
    so the operator can inspect what the forge built / tried to build
    without leaving the Forge Jobs page."""
    assert "📄 View manifest" in html


def test_view_manifest_uses_smart_dispatcher(html: str):
    """The manifest button calls viewForgeManifest(jobId), which tries
    the installed-manifest endpoint first and falls back to the gallery
    package spec for jobs that never wrote a manifest (atlas pattern)."""
    assert 'viewForgeManifest(this.dataset.jid)' in html
    assert 'async function viewForgeManifest(jobId)' in html


def test_view_forge_manifest_tries_installed_first(html: str):
    """viewForgeManifest must hit /api/applications/<bot>/<app> first —
    that's the canonical lifecycle target."""
    assert '/api/applications/${encodeURIComponent(job.bot_id)}/${encodeURIComponent(job.app_id)}' in html


def test_view_forge_manifest_falls_back_to_gallery_package(html: str):
    """When the installed manifest 404s, viewForgeManifest must try
    /api/gallery/<pkg_id> next — that's what jobs reference before
    step 1 writes anything."""
    assert '/api/gallery/${encodeURIComponent(job.pkg_id)}' in html
    assert '_showGalleryPackagePreview(job, pkg)' in html


def test_gallery_package_preview_explains_state(html: str):
    """The gallery preview modal must explain that this is the package
    target, not an installed manifest — without that header the
    operator can't tell whether they're looking at live state or a
    pre-install spec."""
    assert 'Gallery package preview' in html
    assert "didn't write an installed manifest yet" in html


# ── "Did not run" banner ─────────────────────────────────────────────────────


def test_failed_without_running_banner_present(html: str):
    """When status=failed but current_step is 0/1 AND no step has a
    terminal state, the expanded view must surface a clear "did not
    run" banner. This was Pod_admin's bug: a job marked failed with step 9
    showing the hourglass seed icon looked like it was waiting for
    operator approval."""
    assert "This job did not run." in html
    assert "_failedWithoutRunning" in html


def test_failed_without_running_recommends_retry(html: str):
    """The banner must point operators at the Retry path so they know
    how to get unstuck. Without this the banner just states the
    problem without offering an escape."""
    # The banner text mentions "Retry" so operators see the connection
    # to the new button.
    assert "Retry" in html
    # The banner block specifically wires Retry guidance — check both
    # the banner and the button share the same glyph for visual link.
    assert "↻" in html


# ── Failure-step detail block ────────────────────────────────────────────────


def test_failed_step_detail_block_present(html: str):
    """Jobs that DID start and then failed at a specific step must
    surface that step's number, label, and failure detail prominently —
    operators shouldn't have to scan the full step list to find it."""
    assert "Step ${escHtml(String(failedStep.num))} failed:" in html


def test_failure_detail_falls_back_to_log_pointer(html: str):
    """When the failed step has no .detail, the UI must point operators
    at the admin server logs rather than showing an empty box. The
    earlier behaviour silently rendered nothing — operators couldn't
    tell whether the detail field was empty or just not shown."""
    assert "check admin server logs for job" in html


# ── Error pattern recognizer (Likely cause hint) ─────────────────────────────


def test_error_recognizer_function_present(html: str):
    """_forgeRecognizeError converts noisy openclaw stderr into a
    one-line operator-actionable hint above the collapsed raw output."""
    assert '_FORGE_ERROR_PATTERNS' in html
    assert 'function _forgeRecognizeError(detail, job)' in html


def test_error_recognizer_matches_missing_api_key(html: str):
    """The most common atlas-style failure: 'No API key found for
    provider X'. The hint must name the provider and tell the operator
    where to add the key."""
    assert "no api key found for provider" in html.lower()
    assert "auth-profiles.json doesn't have one" in html


def test_error_recognizer_matches_agent_died(html: str):
    """'agent exited rc=N without writing outbox file' — surface a
    generic 'check auth + logs' hint since this can be auth, rate
    limit, or upstream failure."""
    assert "agent exited rc=" in html
    assert "auth-profiles.json" in html


def test_likely_cause_block_renders_above_raw_output(html: str):
    """The Likely cause hint must appear ABOVE the collapsed raw error
    output — the whole point is to spare the operator from reading
    the openclaw spew first."""
    assert "<b style=\"color:var(--yellow)\">Likely cause:</b>" in html
    # Raw output is wrapped in <details> so it's collapsed by default
    assert "Raw error output</summary>" in html


# ── No regression of existing affordances ────────────────────────────────────


@pytest.mark.parametrize("preserved", [
    'data-action="dispatch"',           # queued rows
    'data-action="cancel"',             # queued/running rows
    'forgeDispatch(this.dataset.jid)',  # function still wired
    'forgeCancel(this.dataset.jid)',    # function still wired
    'openForgePanel(this.dataset.jid)', # awaiting_approval Review →
])
def test_existing_action_buttons_preserved(html: str, preserved: str):
    assert preserved in html, f"existing affordance removed: {preserved}"
