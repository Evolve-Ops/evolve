"""ALPHA-2 reader side — the Discovered empty states, EXECUTED not grepped.

Audit findings U5 (the never-scanned empty state lies), B2a (the degradation is
invisible) and U2 (a "ready" draft that cannot be offered gives no reason) all
land in ``web/static/js/pages/apps-discovered.js``. A source-text assertion
would prove the new copy exists somewhere in the file; it would not prove the
right branch is reached. So this runs the real renderer in Node against a
minimal DOM and reads the HTML it produced.

The shim supplies exactly the globals the page expects from its siblings
(``escHtml``, ``_appsEmpty``, ``botLabel``, ``ago``) and nothing else — if the
renderer starts depending on something new, this goes red rather than silently
rendering a hole.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PAGE = (_REPO / "packages/admin/evolve_admin/web/static/js/pages/apps-discovered.js")

# Node is a declared toolchain dependency of this repo (eslint, the plugin
# build) and is preinstalled on every CI runner, so this skip does not fire in
# CI. It exists so a Python-only checkout is not blocked.
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to execute the SPA renderer",
)

_HARNESS = textwrap.dedent("""
    const fs = require('fs');

    // ── The smallest DOM the page needs ──────────────────────────────────
    const nodes = {};
    function node(id) {
      if (!nodes[id]) nodes[id] = {id, innerHTML: '', textContent: '',
                                   value: '', classList: {add(){}, remove(){},
                                   contains(){ return false; }},
                                   setAttribute(){}, querySelector(){ return null; }};
      return nodes[id];
    }
    global.document = {
      getElementById: (id) => (id === 'apps-discovered-body'
        || id === 'apps-discovered-filter-bot') ? node(id) : null,
      querySelector: () => null,
      addEventListener: () => {},
    };
    global.window = {};

    // ── The sibling globals apps-discovered.js reads ─────────────────────
    global.escHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
    global._appsEmpty = (message, hint) =>
      `<div class="empty">${escHtml(message)}<div class="hint">${escHtml(hint || '')}</div></div>`;
    global._appsErrorBox = (where) => `<div class="empty">error ${escHtml(where)}</div>`;
    global.botLabel = (b) => b;
    global.ago = (t) => String(t);
    global.fmtPodTime = (t) => String(t);
    global.api = async () => ({});
    global.toast = () => {};
    global.confirmModal = async () => false;

    // The page declares its state with `let`, and a `let` inside a direct eval
    // lives in the eval's OWN scope — assigning from out here would silently
    // create a second variable the renderer never reads. So the driver is
    // appended to the source and evaluated with it, in one scope.
    const driver = `
      _appsDiscoveredData = JSON.parse(process.argv[3]);
      _appsDiscoveredFilterBot = process.argv[4] || '';
      _appsRenderDiscovered();
    `;
    eval(fs.readFileSync(process.argv[2], 'utf8') + driver);
    process.stdout.write(node('apps-discovered-body').innerHTML);
""")


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("alpha2-js") / "render.js"
    path.write_text(_HARNESS)
    return path


def render(harness: Path, payload: dict, *, bot: str = "") -> str:
    result = subprocess.run(
        ["node", str(harness), str(_PAGE), json.dumps(payload), bot],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


# ── Fixtures come from the REAL producer, not a Python re-implementation ─────
#
# An earlier draft of this file hand-rolled the ``scan_summary`` shape here. That
# left nothing crossing the Python→JS boundary: renaming ``degraded_reasons`` or
# ``remedy`` in ``scan_provenance.summarize`` would have kept both sides green
# and blanked the banner in production. Building the payload by CALLING the real
# ``classify`` / ``summarize`` / ``to_dict`` is what makes these tests a contract
# test rather than two independent restatements of one guess.

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scan_provenance as sp  # noqa: E402


#: Status-file contents that produce each state, so the fixture goes through the
#: same classifier production does.
_STATUS_FOR_STATE = {
    "ok": ({"status": "done", "updated_at": "2026-08-23T09:00:00Z"}, sp.READ_OK),
    "degraded": ({"status": "done", "updated_at": "2026-08-23T09:00:00Z",
                  "llm_degraded": True,
                  "llm_degraded_reason": sp.REASON_NO_LLM_KEY}, sp.READ_OK),
    "never_scanned": (None, sp.READ_MISSING),
    "unreadable": (None, sp.READ_DENIED),
}


def _prov(bot_id: str, state: str):
    status, read_state = _STATUS_FOR_STATE[state]
    prov = sp.classify(bot_id, status, read_state)
    assert prov.state == state, f"fixture for {state!r} classified as {prov.state!r}"
    return prov


def _payload(states: list[tuple[str, str]], drafts: list[dict] | None = None) -> dict:
    provs = [_prov(bot, state) for bot, state in states]
    return {"ok": True, "drafts": drafts or [], "count": len(drafts or []),
            "bots": [b for b, _ in states], "offers_readable": True,
            "scans": [p.to_dict() for p in provs],
            "scan_summary": sp.summarize(provs)}


# ── U5: the never-scanned empty state ────────────────────────────────────────


def test_never_scanned_pod_does_not_claim_everything_was_reviewed(harness):
    """The audit's exact screen: first open, no drafts, nothing ever scanned."""
    html = render(harness, _payload([(b, "never_scanned")
                                     for b in ("bot-a", "bot-b", "bot-c")]))
    assert "has been vouched for or set aside" not in html, (
        "U5: this tells a brand-new operator the scanner already looked"
    )
    assert "Nothing has looked here yet." in html
    assert "Sync all bots" in html, "an empty state must say what will change it"


def test_a_scanned_pod_with_no_drafts_keeps_the_everything_reviewed_copy(harness):
    """The case where that sentence is actually true still says it."""
    html = render(harness, _payload([(b, "ok") for b in ("bot-a", "bot-b")]))
    assert "has been vouched for or set aside" in html


def test_a_partly_scanned_pod_names_the_bots_nobody_looked_at(harness):
    html = render(harness, _payload([("bot-a", "ok"), ("bot-b", "never_scanned")]))
    assert "has been vouched for or set aside" in html
    assert "bot-b has never been scanned" in html


def test_an_unreadable_pod_says_it_could_not_tell(harness):
    """Tri-state: "we could not check" is not "there is nothing here"."""
    html = render(harness, _payload([("bot-a", "unreadable"),
                                     ("bot-b", "unreadable")]))
    assert "could not check" in html
    assert "has been vouched for or set aside" not in html


def test_a_payload_with_no_provenance_asserts_nothing_about_scanning(harness):
    """An older server, or a read that could not be attempted."""
    html = render(harness, {"ok": True, "drafts": [], "count": 0,
                            "bots": ["bot-a"], "offers_readable": True,
                            "scans": None, "scan_summary": None})
    assert "Nothing here yet." in html
    assert "has been vouched for or set aside" not in html
    assert "never" not in html.lower()


def test_filtering_to_a_never_scanned_bot_says_so_for_that_bot(harness):
    draft = {"bot_id": "bot-a", "manifest_stem": "notes", "name": "Notes",
             "purpose": "", "evidence": ["files"], "readiness": None,
             "offer": {"state": "not_offered"}}
    html = render(harness, _payload([("bot-a", "ok"), ("bot-b", "never_scanned")],
                                    [draft]), bot="bot-b")
    assert "Nothing has looked at this bot yet." in html
    assert "1 draft elsewhere on the pod" in html


def test_an_all_degraded_pod_does_not_claim_everything_was_reviewed(harness):
    """The audit's exact pod: three bots, no provider key, no drafts.

    The first cut still fired the "everything vouched for" branch here, because
    it only checked never-scanned and unreadable — which directly contradicted
    the banner above it. Completeness needs a COMPLETED scan to stand on.
    """
    html = render(harness, _payload([(b, "degraded")
                                     for b in ("bot-a", "bot-b", "bot-c")]))
    assert "has been vouched for or set aside" not in html
    assert "Nothing to show yet." in html
    assert "does not mean there is nothing to find" in html
    # And the banner is still there to say why.
    assert "alert-warn" in html


def test_one_completed_scan_is_enough_to_claim_completeness_for_it(harness):
    html = render(harness, _payload([("bot-a", "ok"), ("bot-b", "degraded")]))
    assert "has been vouched for or set aside" in html


def test_the_unreadable_remedy_comes_from_the_payload(harness):
    """The server owns the advice, the same way it owns the thresholds."""
    html = render(harness, _payload([("bot-a", "unreadable")]))
    assert "Re-deploy the bot" in html, (
        "the remedy ScanProvenance ships is not reaching the screen"
    )


def test_last_scanned_is_shown_when_known_and_omitted_when_not(harness):
    draft = {"bot_id": "bot-a", "manifest_stem": "notes", "name": "Notes",
             "purpose": "", "evidence": ["files"], "readiness": None,
             "offer": {"state": "not_offered"}}
    shown = render(harness, _payload([("bot-a", "ok")], [draft]))
    assert "Last scanned" in shown
    never = render(harness, _payload([("bot-a", "never_scanned")], [draft]))
    assert "Last scanned" not in never, (
        "a pod with no recorded scan time must show no time, not a placeholder"
    )


# ── B2a: the degraded banner ─────────────────────────────────────────────────


def test_a_degraded_scan_banners_the_reason_and_the_fix(harness):
    html = render(harness, _payload([("bot-a", "degraded")]))
    assert "Discovered may not be showing everything." in html
    assert "no working provider key" in html
    assert "Plugins → Credentials" in html, (
        "principle-alerts: the banner must offer a next step, not just a fact"
    )


def test_the_banner_shows_on_an_empty_list_which_is_when_it_matters_most(harness):
    """A skipped model phase is the likeliest reason the list is empty."""
    html = render(harness, _payload([("bot-a", "degraded")]))
    assert "empty" in html and "alert-warn" in html


def test_the_banner_shows_above_a_populated_table_too(harness):
    draft = {"bot_id": "bot-a", "manifest_stem": "notes", "name": "Notes",
             "purpose": "", "evidence": ["files"], "readiness": None,
             "offer": {"state": "not_offered"}}
    html = render(harness, _payload([("bot-a", "degraded")], [draft]))
    assert html.index("alert-warn") < html.index("<table"), (
        "the explanation must precede the data it explains"
    )


def test_one_reason_shared_by_three_bots_is_one_banner_line(harness):
    html = render(harness, _payload([(b, "degraded")
                                     for b in ("bot-a", "bot-b", "bot-c")]))
    assert html.count("no working provider key") == 1
    assert "On 3 bots (bot-a, bot-b, bot-c)" in html


def test_a_healthy_pod_gets_no_banner(harness):
    html = render(harness, _payload([("bot-a", "ok")]))
    assert "alert-warn" not in html


def test_the_banner_never_prints_a_reason_key(harness):
    html = render(harness, _payload([("bot-a", "degraded")]))
    for field_name in ("llm_degraded", "no_llm_provider_key", "never_scanned",
                       "scan_did_not_finish", "workspace_not_found"):
        assert field_name not in html, f"{field_name} is a field name on screen"


# ── U2: the readiness blocker on the row ─────────────────────────────────────


def _draft_with_readiness(readiness: dict) -> dict:
    return {"bot_id": "bot-a", "manifest_stem": "brief", "name": "Morning Brief",
            "purpose": "Writes the brief.", "evidence": ["files"],
            "readiness": readiness, "offer": {"state": "not_offered"}}


def test_a_ready_draft_that_cannot_be_offered_says_why_on_the_row(harness):
    """The audit's 100 / ready / "not yet offered" row, explained."""
    draft = _draft_with_readiness({
        "score": 100, "band": "ready", "dimensions_measured": 1,
        "dimensions_total": 3, "eligible_to_offer": False,
        "offer_blockers": [{"key": "needs_more_measures",
                            "short": "needs another measure",
                            "text": "Measured on 1 of 3 signals."}],
    })
    html = render(harness, _payload([("bot-a", "ok")], [draft]))
    assert "needs another measure" in html
    # Both facts on ONE line rather than two stacked half-explanations.
    assert "from 1 of 3 measures · needs another measure" in html
    assert "not yet offered" in html


def test_an_offerable_draft_shows_no_blocker(harness):
    draft = _draft_with_readiness({
        "score": 90, "band": "ready", "dimensions_measured": 3,
        "dimensions_total": 3, "eligible_to_offer": True, "offer_blockers": [],
    })
    html = render(harness, _payload([("bot-a", "ok")], [draft]))
    assert "needs another measure" not in html
    assert "measures" not in html


def test_a_payload_with_no_blocker_list_under_explains_rather_than_inventing(harness):
    """An older server: show the measured-ness, invent no reason."""
    draft = _draft_with_readiness({
        "score": 100, "band": "ready", "dimensions_measured": 1,
        "dimensions_total": 3, "eligible_to_offer": False,
    })
    html = render(harness, _payload([("bot-a", "ok")], [draft]))
    assert "from 1 of 3 measures" in html
    assert "needs" not in html


def test_an_unscored_draft_still_reads_not_yet_scored(harness):
    draft = _draft_with_readiness(None)
    html = render(harness, _payload([("bot-a", "ok")], [draft]))
    assert "not yet scored" in html


# ── coverage footer ──────────────────────────────────────────────────────────


def test_a_populated_table_still_names_the_bots_it_cannot_speak_for(harness):
    """A queue of drafts says nothing about the bots nobody scanned."""
    draft = _draft_with_readiness({
        "score": 60, "band": "emerging", "dimensions_measured": 1,
        "dimensions_total": 3, "eligible_to_offer": False, "offer_blockers": [],
    })
    html = render(harness, _payload([("bot-a", "ok"), ("bot-c", "never_scanned")],
                                    [draft]))
    assert "<table" in html
    assert "bot-c has never been scanned" in html
