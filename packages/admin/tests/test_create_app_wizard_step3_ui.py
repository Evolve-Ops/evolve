"""tests/test_create_app_wizard_step3_ui.py — Step-3 "View in Forge Jobs →" button.

Static-string assertions against index.html for the wizard Step-3 success
modal. The button was previously wired to `nav(document.querySelector(
'[data-page=forge]'))` which threw silently because no element carries
`data-page="forge"` — the click looked dead.

Same tier as test_forge_jobs_ui — won't catch every UI bug, but catches
"the affordance is silently missing or mis-wired in the rendered HTML."
"""

from __future__ import annotations

from pathlib import Path

import pytest


_INDEX = Path(__file__).parent.parent / "evolve_admin" / "web" / "index.html"


_CREATE_APP_WIZARD_JS = Path(__file__).parent.parent / "evolve_admin" / "web"/ "static" / "js" / "pages" / "create-app-wizard.js"
@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX.read_text() + "\n" + _CREATE_APP_WIZARD_JS.read_text()


def test_step3_button_uses_helper_not_broken_selector(html: str):
    """The Step-3 button must NOT use the old `data-page=forge` selector —
    no element carries that attribute, so the old handler threw silently.
    The button must route through `_wizardOpenForgeJob` instead."""
    # Scan onclick handlers for the dead selector. Comments/docstrings
    # may reference it as the historical bug — we only care about live
    # wiring.
    import re
    onclicks = re.findall(r'onclick="([^"]*)"', html)
    bad = [h for h in onclicks if "data-page=forge]" in h]
    assert not bad, (
        f"onclick handler still uses dead `data-page=forge` selector: {bad}"
    )
    # New wiring present
    assert "_wizardOpenForgeJob(this.dataset.jid)" in html


def test_step3_button_passes_job_id(html: str):
    """The handler needs the job_id to highlight + scroll the right row.
    The button's `data-jid` must come from `j.job_id`."""
    # The button's data-jid attribute references j.job_id within the
    # Step-3 jobs.map() template — assert the literal substring.
    assert 'data-jid="${esc(j.job_id || \'\')}"' in html


def test_wizard_open_forge_job_function_defined(html: str):
    assert "async function _wizardOpenForgeJob(jobId)" in html


def test_handler_closes_wizard_before_navigating(html: str):
    """closeCreateWizard() must run first — leaving the wizard modal open
    over the Forge Jobs page would defeat the navigation."""
    # The helper's body must call closeCreateWizard before nav().
    idx = html.find("async function _wizardOpenForgeJob(jobId)")
    assert idx >= 0
    body = html[idx:idx + 1500]
    close_idx = body.find("closeCreateWizard()")
    nav_idx = body.find("nav(appsNavEl)")
    assert close_idx >= 0 and nav_idx >= 0
    assert close_idx < nav_idx, "closeCreateWizard must run before nav()"


def test_handler_activates_forge_jobs_subtab(html: str):
    """Apps page has Capabilities / Gallery / Forge Jobs subtabs — the
    handler must click the forge-jobs subtab explicitly, not just land
    on whatever subtab was last active."""
    assert '#page-apps .subtab[data-subtab="forge-jobs"]' in html


def test_handler_highlights_the_new_job(html: str):
    """The whole point of passing job_id through is the highlight: set
    `_forgeSelectedJobId` so the row expands, then scroll it into view."""
    idx = html.find("async function _wizardOpenForgeJob(jobId)")
    assert idx >= 0
    body = html[idx:idx + 1500]
    assert "_forgeSelectedJobId = jobId" in body
    assert "scrollIntoView" in body


def test_step3_button_is_a_real_button_element(html: str):
    """Not a styled div with click-through behaviour. The button must
    be a `<button>` so keyboard/screen-reader users can activate it."""
    # The Step-3 row template uses <button class="btn btn-ghost btn-sm" ...
    # for "View in Forge Jobs →" — assert that exact wiring is intact.
    assert (
        '<button class="btn btn-ghost btn-sm" style="margin-left:auto" '
        'data-jid="${esc(j.job_id || \'\')}" '
        'onclick="_wizardOpenForgeJob(this.dataset.jid)">View in Forge Jobs →</button>'
    ) in html
