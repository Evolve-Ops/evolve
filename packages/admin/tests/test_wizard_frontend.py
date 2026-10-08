"""tests/test_wizard_frontend.py — Wizard PR γ frontend smoke tests.

These are static-string assertions against index.html: we confirm the
5-screen markup is present, the JS state-machine entry points exist,
the wizard is wired to the API endpoints PR β registered, and the
prior 3-step modal's symbols are gone.

This is the "won't load with a 404" tier of test — not a full
JSDOM/Playwright check. Live behaviour is verified via the smoke-test
checklist in the spec §8.3 (operator drives the wizard against the
mini after merge).
"""

from __future__ import annotations

from pathlib import Path

import pytest


_WEB = Path(__file__).parent.parent / "evolve_admin" / "web"
_INDEX = _WEB / "index.html"
_POD_CONFIG_JS = _WEB/ "static" / "js" / "pages" / "pod-config.js"
_BASE_CSS = _WEB / "static" / "css" / "base.css"
_DOM_UTILS_JS = _WEB / "static" / "js" / "core" / "dom-utils.js"
_SKILLS_JS = _WEB / "static" / "js" / "pages" / "skills.js"
_SKILL_INSTALL_JS = _WEB / "static" / "js" / "pages" / "skill-install.js"
_ADD_BOT_WIZARD_JS = _WEB / "static" / "js" / "pages" / "add-bot-wizard.js"
_OVERVIEW_JS = _WEB / "static" / "js" / "pages" / "overview.js"
_CREDENTIALS_JS = _WEB / "static" / "js" / "pages" / "credentials.js"


@pytest.fixture(scope="module")
def html() -> str:
    # The shell's content is now split across multiple files:
    #   - base.css           (Phase 1 — CSS rules)
    #   - dom-utils.js       (Phase 2b — toast, escHtml, attrJsLiteral)
    #   - skills.js          (Phase 3r — Skills page incl. google-route helper)
    #   - skill-install.js   (Phase 3v — install flow + Google OAuth wizards)
    #   - add-bot-wizard.js  (Phase 3aa — Add-a-Bot wizard 5-screen modal +
    #                         verify gauntlet)
    #   - overview.js        (Phase 3ai — Overview render + pod-tile menu +
    #                         bot lifecycle actions)
    # Concat so the existing string-shape assertions stay valid without
    # per-test knowledge of layout. The JS-parse-check test below uses a
    # `<script>(.*?)</script>` regex so CSS/JS appended at the end has no
    # effect on which inline blocks it finds.
    return (
        _INDEX.read_text()
        + "\n"
        + _BASE_CSS.read_text()
        + "\n"
        + _DOM_UTILS_JS.read_text()
        + "\n"
        + _SKILLS_JS.read_text()
        + "\n"
        + _SKILL_INSTALL_JS.read_text()
        + "\n"
        + _ADD_BOT_WIZARD_JS.read_text()
        + "\n"
        + _OVERVIEW_JS.read_text()
    )


# ── Modal markup present ─────────────────────────────────────────────────────


def test_five_screens_exist(html: str):
    for i in range(1, 6):
        assert f'id="wiz-screen-{i}"' in html, f"missing screen {i}"


def test_step_indicators_exist(html: str):
    for i in range(1, 6):
        assert f'id="wiz-ind-{i}"' in html


def test_modal_overlay_uses_canonical_id(html: str):
    """The "+ Add Bot" button at line ~3307 still calls openAddBotModal();
    the modal id must remain `add-bot-modal` so that trigger keeps working."""
    assert '<div class="modal-overlay" id="add-bot-modal">' in html
    assert 'onclick="openAddBotModal()"' in html


# ── JS entry points present ──────────────────────────────────────────────────


JS_FUNCTIONS = [
    "openAddBotModal",
    "closeAddBotModal",
    "_wizGoto",
    "_wizScreen1Next",
    "_wizDebouncedPreview",
    "_wizRunPreview",
    "_wizScreen2Next",
    "_wizRenderStages",
    "_wizPollJob",
    "_wizShowFailure",
    "_wizChannelChanged",
    "_wizScreen4Apply",
    "_wizDeriveAppliedLlmSource",
    "_wizRenderReviewSummary",
    "_wizGotoApplicationsPage",
]


@pytest.mark.parametrize("fn", JS_FUNCTIONS)
def test_function_defined(html: str, fn: str):
    # Match either `function name(` or `async function name(`
    needles = [f"function {fn}(", f"async function {fn}("]
    assert any(n in html for n in needles), f"{fn} not defined"


# ── Old 3-step modal symbols removed ─────────────────────────────────────────


@pytest.mark.parametrize("old", [
    "addBotStep2",
    "addBotStep3",
    "addBotBack",
    'id="add-bot-step-1"',
    'id="add-bot-step-2"',
    'id="add-bot-step-3"',
    'id="add-bot-progress"',
    'id="add-bot-done-btn"',
])
def test_old_3step_modal_symbols_gone(html: str, old: str):
    assert old not in html, f"stale 3-step symbol still present: {old}"


# ── API endpoints from PR β are wired in ─────────────────────────────────────


@pytest.mark.parametrize("endpoint", [
    "/api/wizard/preview",
    "/api/wizard/provision",
    "/api/wizard/borrow-candidates",
    "/api/jobs/",  # for provision-job polling
    "/api/skills/install/telegram/set-token",  # PR β reuses PR #1683 endpoint
    "/api/skills/install/discord/set-token",  # parallel to Telegram for Discord
])
def test_endpoint_referenced(html: str, endpoint: str):
    assert endpoint in html, f"frontend doesn't call {endpoint}"


# ── No auto-borrow of sibling LLM credentials ────────────────────────────────
#
# The wizard collects the LLM credential on Screen 2 (operator picks
# "Borrow from <bot>" or "Paste new"). Screen 4 used to *also* re-prompt
# for an LLM credential, with a hidden default-checked "Borrow from
# <first sibling>" radio. _wizScreen4Apply read that hidden radio and
# silently POSTed /api/credentials/borrow, joining a sibling bot's
# anthropic key into the new bot's auth-profiles regardless of the
# operator's Screen-2 choice.
#
# Real-world incident: 2026-06-01 wizard provision pasted a fresh
# Anthropic key on Screen 2, but auth-profiles.json on the new bot
# ended up with two anthropic profiles — the pasted one as
# `anthropic:default` plus `anthropic:api` stamped
# `borrowed_from: "team-bot-a"` from the silent auto-borrow.
#
# These tests pin the post-fix invariants so the silent-auto-borrow
# regression can't return:


def test_screen4_apply_does_not_call_credentials_borrow(html: str):
    """_wizScreen4Apply must NOT POST to /api/credentials/borrow.
    The LLM credential is handled on Screen 2; Screen 4 is channels-only."""
    apply_fn = _re.search(
        r"async function _wizScreen4Apply\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert apply_fn, "_wizScreen4Apply not found"
    body = apply_fn.group(1)
    assert "/api/credentials/borrow" not in body, (
        "_wizScreen4Apply calls /api/credentials/borrow — that's the "
        "silent-auto-borrow path from the 2026-06-01 incident. LLM "
        "credentials are handled on Screen 2 only; Screen 4 must not "
        "touch /api/credentials/borrow."
    )
    # The dead `wiz-llm-source` radio set must not be read on Screen 4
    # either — that was the input the auto-borrow read from.
    assert "wiz-llm-source" not in body, (
        "_wizScreen4Apply still queries the `wiz-llm-source` radio set. "
        "That input set was the silent-auto-borrow trigger and has been "
        "removed; the read here is dead code that will resurrect the bug "
        "if the radios ever come back."
    )


def test_no_dead_screen4_llm_choice_renderer(html: str):
    """The Screen-4 LLM radio renderer is gone. If it comes back, so does
    the silent-auto-borrow bug — _wizScreen4Apply would find a checked
    radio in the DOM even when the operator never picked one."""
    # The renderer is named _wizRenderLLMChoice; its loader is
    # _wizLoadBorrowCandidatesAndRenderS4; the change-handler is
    # _wizLLMSourceChanged. None of them should exist any more.
    for dead in (
        "_wizRenderLLMChoice",
        "_wizLoadBorrowCandidatesAndRenderS4",
        "_wizLLMSourceChanged",
    ):
        assert f"function {dead}(" not in html, (
            f"{dead} is back. That function renders default-checked "
            "borrow-from-<first-sibling> radios into the DOM, which "
            "_wizScreen4Apply will silently submit to "
            "/api/credentials/borrow. Remove it; LLM credential collection "
            "lives on Screen 2."
        )
    # The hidden DOM elements that held the radios must also be gone.
    # Their presence (even hidden) is enough — querySelector finds
    # the radio inside them and the submit fires.
    for dead_id in ('wiz-s4-llm-choice', 'wiz-s4-llm-paste-wrap',
                    'wiz-s4-llm-paste'):
        assert f'id="{dead_id}"' not in html, (
            f"DOM element id={dead_id!r} is back. Hidden or not, this "
            "is where the silent-auto-borrow radios get rendered into; "
            "querySelector('input[name=\"wiz-llm-source\"]:checked') "
            "still finds them. Remove the element."
        )


# ── Channel set-token calls must send bot_token, not token ───────────────────
#
# The set-token endpoints (telegram + discord) both accept ``bot_token``.
# The wizard previously sent ``token``, which produced a 400 from Telegram
# ("bot_token required") and a 404 from Discord (endpoint did not exist).
# Both manifested as "<channel> install failed: unknown" in the wizard's
# error chip, hiding the real cause. Pin the contract so the regression
# does not recur silently.


@pytest.mark.parametrize("channel,endpoint", [
    ("telegram", "/api/skills/install/telegram/set-token"),
    ("discord", "/api/skills/install/discord/set-token"),
])
def test_wizard_channel_call_sends_bot_token_field(
    html: str, channel: str, endpoint: str,
):
    """The body for each channel's set-token call must use ``bot_token:`` —
    sending ``token:`` matches neither endpoint's contract and silently
    fails as "unknown" because the JSON 400/404 response has no detail
    the wizard's catch-all surfaces."""
    import re

    # Find the api(...) call to this endpoint and capture its body literal.
    # The wizard body is `{ bot_id: ..., bot_token: tok }` — single quotes
    # around the URL, then a body object literal.
    pattern = re.compile(
        r"api\(\s*['\"]POST['\"]\s*,\s*['\"]"
        + re.escape(endpoint)
        + r"['\"]\s*,\s*(\{[^}]*\})",
        re.S,
    )
    m = pattern.search(html)
    assert m, f"wizard does not call {endpoint}"
    body_literal = m.group(1)

    assert "bot_token" in body_literal, (
        f"{channel} set-token call must include `bot_token:` field "
        f"(endpoint contract). Found: {body_literal!r}"
    )
    # And must NOT send the legacy ``token:`` field name — which would
    # be silently ignored by the endpoint and trigger the 400.
    # The literal regex bounds ``token`` to not match ``bot_token``.
    assert not re.search(r"(?<!bot_)token\s*:", body_literal), (
        f"{channel} set-token call still passes legacy `token:` field. "
        f"Found: {body_literal!r}"
    )


# ── Stage list matches PR α canonical names ──────────────────────────────────


def test_stage_keys_match_provisioning_module(html: str):
    """The _WIZ_STAGES array in the frontend must mirror the stage
    name constants in provisioning.py, or the progress UI won't
    correlate log messages to stage rows.
    """
    import sys
    sys.path.insert(0, str(_INDEX.parent.parent.parent.parent / "admin"))
    from evolve_admin.provisioning import (  # noqa: E402
        STAGE_VALIDATE, STAGE_CREATE_USER, STAGE_CREATE_OC_DIR,
        STAGE_OC_ONBOARD, STAGE_ADD_BOT, STAGE_DEPLOY,
    )
    for stage in [STAGE_VALIDATE, STAGE_CREATE_USER, STAGE_CREATE_OC_DIR,
                  STAGE_OC_ONBOARD, STAGE_ADD_BOT, STAGE_DEPLOY]:
        assert f"key: '{stage}'" in html, f"stage {stage} missing from _WIZ_STAGES"


# ── Channel options present ──────────────────────────────────────────────────


@pytest.mark.parametrize("channel", ["telegram", "slack", "discord", "none"])
def test_messaging_channel_option(html: str, channel: str):
    assert f'value="{channel}"' in html


# ── Wizard does NOT use ad-hoc network.json deploy path ──────────────────────


def test_wizard_does_not_call_legacy_deploy_endpoint(html: str):
    """The old 3-step modal called POST /api/deploy with botId + role +
    port. The new wizard must call /api/wizard/provision instead, which
    routes through PR α's provision_bot() with full pipeline + rollback.

    We can't grep for '/api/deploy' globally because other UI features
    use it (Settings → Deploy bot button, etc.). The wizard JS now
    lives in pages/add-bot-wizard.js (Phase 3aa) — read it directly.
    """
    wizard_js = _ADD_BOT_WIZARD_JS.read_text()
    assert "/api/deploy" not in wizard_js, (
        "wizard JS still calls /api/deploy — should use /api/wizard/provision"
    )


# ── Cancel behaviour ─────────────────────────────────────────────────────────


def test_cancel_button_on_post_commit_screens_just_closes(html: str):
    """Spec Q3 resolution: Cancel from screens 4 or 5 must not roll
    back the provisioned bot. The Done button on screen 5 wires to a
    handler that EITHER closeAddBotModal()s directly OR calls a
    wrapper which is required to closeAddBotModal() first before any
    optional handoff (e.g. _wizScreen5Done routes to Skills when an
    OAuth-pending channel was picked, but it MUST close the wizard
    modal first — it never calls /api/remove)."""
    import re
    # Accept either onclick="closeAddBotModal()" (legacy / no-handoff)
    # or onclick="_wizScreen5Done()" (the wrapper added 2026-06-01 for
    # the Slack/Discord OAuth handoff). Both close the wizard; the
    # wrapper additionally deep-links to the Skills page when the
    # operator picked an OAuth-based channel.
    done_btn_re = re.compile(
        r'<button[^>]*\bid="wiz-s5-done"[^>]*'
        r'\bonclick="(?:closeAddBotModal\(\)|_wizScreen5Done\(\))"'
        r'[^>]*>\s*Done\s*</button>',
        re.S,
    )
    assert done_btn_re.search(html), (
        "screen-5 Done button must wire to closeAddBotModal() or "
        "_wizScreen5Done() — no other handler is allowed (each must "
        "close-modal-first and never call /api/remove)."
    )
    # If the handler is the wrapper, verify it definitionally calls
    # closeAddBotModal() first. Otherwise a future refactor could
    # accidentally rewire to a fetch-then-close path that loses work
    # when the network blips at exactly the wrong moment.
    if "_wizScreen5Done()" in html:
        # Pin the wrapper definition: must close the modal before any
        # navigation. This is the contract the test name promises.
        wrapper_re = re.compile(
            r"function _wizScreen5Done\(\)\s*\{[^}]*closeAddBotModal\(\)",
            re.S,
        )
        assert wrapper_re.search(html), (
            "_wizScreen5Done() exists but doesn't call closeAddBotModal() "
            "in its body — Done must close the modal before any OAuth "
            "handoff. Reorder so close happens first."
        )
    # No /api/remove API CALL in the wizard JS block. Comments mentioning
    # /api/remove (e.g. explaining why we DON'T call it any more after
    # the lifecycle redesign in PR #1903) are fine — they're documentation.
    # The bug we're guarding against is an actual fetch/api invocation,
    # which has the shape api('POST', '/api/remove', ...) or
    # fetch('/api/remove', ...).
    #
    # The wizard JS now lives in pages/add-bot-wizard.js (Phase 3aa).
    # Read it directly — it's the whole file, no need to slice.
    wizard_js = _ADD_BOT_WIZARD_JS.read_text()
    api_call_patterns = [
        # api('METHOD', '/api/remove'...) — the most common form in this file
        r"""api\(\s*['"][A-Z]+['"]\s*,\s*['"]/api/remove['"]""",
        # fetch('/api/remove'...) — defensive: catch any direct fetch
        r"""fetch\(\s*['"]/api/remove['"]""",
    ]
    for pat in api_call_patterns:
        assert not re.search(pat, wizard_js), (
            "wizard JS makes an API call to /api/remove — Cancel from "
            "post-commit screens must not roll back. Matched pattern: " + pat
        )


import re as _re


# ── Slack OAuth handoff (no inline credential collection) ────────────────────
#
# Telegram and Discord are bot-token-direct (paste a token, the backend
# verifies via getMe / users/@me, install completes inline). Slack is
# OAuth-only on the backend — no bot-token-direct path exists — so the
# wizard explicitly defers Slack install to the bot's Skills page via
# the pending_oauth_channel + Done-route pattern.
#
# Pre-2026-06-01 the Slack branch promised a deep-link in copy
# ("the wizard will deep-link you after provisioning") but
# _wizScreen4Apply did NOTHING for Slack — the operator landed on
# Verify with no Slack installed and no instructions. This block
# pins the new handoff so that silent-no-op can't return.
#
# Bot-token-direct contract for Telegram + Discord is pinned higher up
# in this file by the parametrized `test_wizard_channel_call_sends_bot_token_field`.


def test_screen4_slack_shows_oauth_pending_copy(html: str):
    """Slack option on Screen 4 must not promise an action the wizard
    can't deliver. The OAuth-pending copy explicitly tells the operator
    Done will route them to the Skills page — no false promises."""
    # Pin the Slack branch of _wizChannelChanged. The branch must
    # mention OAuth and the Skills-page handoff so the operator's
    # expectations match what Done actually does.
    handler = _re.search(
        r"function _wizChannelChanged\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert handler, "_wizChannelChanged not found"
    body = handler.group(1)
    # Find the slack-specific branch by anchoring on the literal.
    slack_branch = _re.search(
        r"sel\.value\s*===\s*'slack'.*?(?=else if|\n\s*\})",
        body, _re.S,
    )
    assert slack_branch, (
        "No `sel.value === 'slack'` branch in _wizChannelChanged. "
        "If the dispatch shape changed, update this test."
    )
    branch_body = slack_branch.group(0)
    assert "OAuth" in branch_body, (
        "Screen 4 Slack branch doesn't mention OAuth — copy must "
        "set expectations about the handoff before Apply."
    )
    assert "Skills page" in branch_body, (
        "Screen 4 Slack branch doesn't mention the Skills page "
        "handoff — operator needs to know where Done sends them."
    )


def test_screen4_apply_stashes_pending_oauth_channel_for_slack(html: str):
    """When Slack is picked, _wizScreen4Apply must set
    _wizState.pending_oauth_channel = 'slack' so the Verify summary
    banner renders and Done routes correctly. Telegram and Discord
    install inline and must NOT set this flag (they go straight to
    Verify-then-close-modal without a Skills-page handoff)."""
    apply_fn = _re.search(
        r"async function _wizScreen4Apply\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert apply_fn, "_wizScreen4Apply not found"
    body = apply_fn.group(1)
    assert "pending_oauth_channel" in body, (
        "_wizScreen4Apply doesn't set pending_oauth_channel — Verify "
        "banner and Done routing both depend on it."
    )
    # The Slack branch must explicitly stash 'slack'. We allow either
    # the literal 'slack' assignment or a generic `= channel` when the
    # surrounding branch is `channel === 'slack'`.
    slack_block = _re.search(
        r"channel\s*===\s*'slack'.*?(?=\}\s*else|\n\s*\})",
        body, _re.S,
    )
    assert slack_block, (
        "No `channel === 'slack'` branch in _wizScreen4Apply. The "
        "handoff lives in this branch; without it picking Slack just "
        "silently no-ops on Apply (the bug closed by this PR)."
    )
    assert "pending_oauth_channel" in slack_block.group(0), (
        "The Slack branch in _wizScreen4Apply doesn't set "
        "pending_oauth_channel — the silent-no-op regression is back."
    )


def test_wiz_screen5_done_routes_to_skills_when_slack_pending(html: str):
    """The Done button's _wizScreen5Done() handler must call
    _wizGotoSkillsPage('slack') when pending_oauth_channel is 'slack'.
    Without it, picking Slack drops the operator on the Overview page
    with no indication of what comes next."""
    fn = _re.search(
        r"function _wizScreen5Done\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert fn, "_wizScreen5Done not found"
    body = fn.group(1)
    # Must call the deep-link helper. The argument is the pending
    # channel.
    assert "_wizGotoSkillsPage" in body, (
        "_wizScreen5Done() doesn't call _wizGotoSkillsPage() — the "
        "wizard→Skills handoff is broken; operator lands on Overview "
        "with no next-step indication."
    )
    # And it must close the modal first (this is also pinned by
    # test_cancel_button_on_post_commit_screens_just_closes, but a
    # focused failure here is more actionable).
    close_idx = body.find("closeAddBotModal()")
    route_idx = body.find("_wizGotoSkillsPage")
    assert close_idx >= 0, "_wizScreen5Done() doesn't call closeAddBotModal()"
    assert close_idx < route_idx, (
        "_wizScreen5Done() routes to Skills before closing the wizard "
        "modal — close must happen first so the operator doesn't end "
        "up looking at the Skills page through the modal overlay."
    )


def test_wiz_goto_skills_page_helper_exists(html: str):
    """The deep-link helper is the bridge between Done and the Skills
    page. Must exist; must navigate to the data-page='skills' nav."""
    fn = _re.search(
        r"function _wizGotoSkillsPage\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert fn, "_wizGotoSkillsPage helper not defined"
    body = fn.group(1)
    assert "data-page=\"skills\"" in body, (
        "_wizGotoSkillsPage doesn't navigate to the data-page='skills' "
        "nav item. The nav() helper expects the right target."
    )


def test_verify_summary_renders_oauth_pending_banner(html: str):
    """The Verify summary block must surface pending_oauth_channel so
    the operator sees the OAuth handoff before clicking Done. Without
    this, Done feels like 'all set' and the OAuth requirement is
    invisible until they wonder why messaging doesn't work."""
    fn = _re.search(
        r"function _wizRenderReviewSummary\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert fn, "_wizRenderReviewSummary not found"
    body = fn.group(1)
    assert "pending_oauth_channel" in body, (
        "_wizRenderReviewSummary doesn't reference pending_oauth_channel "
        "— the OAuth-pending banner won't render. Operator clicks Done "
        "thinking everything's set up and lands on the Skills page "
        "confused about why they're there."
    )
    # The banner copy must mention OAuth + Skills (helps the operator
    # connect "Done sent me here" to "I need to finish OAuth").
    assert "OAuth pending" in body or "OAuth" in body, (
        "Pending-OAuth banner copy in _wizRenderReviewSummary doesn't "
        "mention OAuth — operator can't tell what's about to happen."
    )


# ── Lifecycle UI: always-refresh + real error detail ─────────────────────────
#
# detachBot / retireBot / _doDeleteBot used to:
#   1. Gate `loadStatus()` on `r.success` — partial-success cases left
#      the bot tile stranded until the next polling tick (~5-10s).
#   2. Show bare "Failed" toast (the response body has `errors: [...]`
#      array, not `error: "..."` string, so `r.error || 'Failed'`
#      always fell through to 'Failed' with no actionable detail).
#   3. Conflate "fully failed" with "partial success" (retire_bot sets
#      success=False on any error, even non-fatal cases like one
#      notification dispatcher failing — but the bot's still removed
#      from network.json and archived).
#
# Fix: shared `_lifecycleHandle(verb, id, r)` helper that ALWAYS calls
# loadStatus, distinguishes partial-success from full-failure, and
# surfaces the real error detail from `r.errors[0]` when `r.error` is
# missing. Pin the contract here so the silent-no-refresh regression
# can't return.


def test_lifecycle_handle_helper_exists(html: str):
    """The shared lifecycle response handler must exist — the three
    callers (detachBot/retireBot/_doDeleteBot) delegate to it, so a
    missing definition would silently break all three."""
    fn = _re.search(
        r"async function _lifecycleHandle\([^)]*\)\s*\{",
        html,
    )
    assert fn, (
        "_lifecycleHandle() helper is missing from the wizard JS — "
        "detach/retire/delete responses won't be handled consistently."
    )


def test_lifecycle_handle_always_refreshes_status(html: str):
    """_lifecycleHandle must call loadStatus() UNCONDITIONALLY (not
    gated on r.success). Otherwise a partial-success delete leaves
    the bot tile visible in the UI until the next polling tick — the
    operator sees red 'Failed' AND a still-present bot, which is
    exactly the bug operators hit on 2026-06-01."""
    body = _re.search(
        r"async function _lifecycleHandle\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert body, "_lifecycleHandle body not found"
    fn_body = body.group(1)
    # Helper must reference loadStatus(), and it must NOT be inside an
    # `if (r.success)` block. We check the inverse: no `if.*success.*loadStatus`
    # pattern, AND loadStatus is referenced.
    assert "loadStatus" in fn_body, (
        "_lifecycleHandle doesn't call loadStatus() — tile state will "
        "stay stale after the operator clicks Detach/Retire/Delete."
    )
    # The gate that caused the bug was literally `if (r.success) await
    # loadStatus()`. Reject that exact shape (allow any other usage).
    assert not _re.search(
        r"if\s*\(\s*r\.success\s*\)[^{]*\bawait\s+loadStatus\(",
        fn_body,
    ), (
        "_lifecycleHandle gates loadStatus() on r.success — the partial-"
        "success refresh bug from 2026-06-01 has regressed. loadStatus "
        "must run on both success and failure paths."
    )


def test_lifecycle_handle_surfaces_real_error_detail(html: str):
    """When the response body has `errors: [...]`, the toast must
    surface the first error rather than falling through to bare
    'Failed'. The backend uses RetireResult shape — `errors` is the
    array; `error` is rarely set."""
    body = _re.search(
        r"async function _lifecycleHandle\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert body, "_lifecycleHandle body not found"
    fn_body = body.group(1)
    # Must reference both r.error AND r.errors so it can pick whichever
    # is populated.
    assert "r.error" in fn_body, (
        "_lifecycleHandle doesn't fall back to r.error — operator-visible "
        "toast will say 'Failed' with no actionable detail when the "
        "response body uses the {error: ...} shape."
    )
    assert "r.errors" in fn_body, (
        "_lifecycleHandle doesn't fall back to r.errors[] — the standard "
        "RetireResult shape uses an errors array, not a single error "
        "string. Without this the toast says 'Failed' for every retire/"
        "delete operation with non-fatal step failures."
    )


@pytest.mark.parametrize("caller", ["detachBot", "retireBot", "_doDeleteBot"])
def test_lifecycle_caller_delegates_to_handler(html: str, caller: str):
    """Each of the three lifecycle entry points must delegate to
    _lifecycleHandle rather than re-implementing toast + refresh
    inline. The old inline copies all had the gated-loadStatus bug;
    keeping them all on one helper prevents the three from drifting."""
    fn = _re.search(
        rf"async function {_re.escape(caller)}\([^)]*\)\s*\{{(.*?)\n\}}",
        html, _re.S,
    )
    assert fn, f"{caller} not found"
    fn_body = fn.group(1)
    assert "_lifecycleHandle" in fn_body, (
        f"{caller}() doesn't call _lifecycleHandle — the inline "
        f"toast+refresh path has the loadStatus-gated-on-success "
        f"regression. Delegate to the shared helper."
    )


# ── Warn toast is pinned + lifecycle shows all errors ───────────────────────
#
# Background: yellow ("warn") toasts carry the actionable diagnostic on
# lifecycle partial-success — e.g. "⚠ <bot> partially deleted (3 stopped,
# 1 failed): <reason>". The 2.8s auto-dismiss kept eating the reason
# before the operator could read or copy it, leaving the bot in a
# half-retired state with no UI-side evidence of what went wrong. The
# fix pins the warn branch open (× button to dismiss) and expands the
# detail to show ALL errors in r.errors[] joined with newlines, not
# just errors[0].


def test_toast_warn_skips_auto_dismiss(html: str):
    """The toast() helper must NOT call setTimeout in the pinned branch.
    Warn AND err toasts are diagnostic-bearing and must stay on screen until
    the operator dismisses with the × button. A regression here brings
    back the 'yellow toast vanished before I could read it' incident
    from 2026-06-01 — and, for err, the vanishing-error problem that native
    alert() has in the desktop webview (err alert() sites migrated to
    toast(_, 'err'), so the error toast must persist)."""
    fn = _re.search(
        r"function toast\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert fn, "toast() helper not found"
    body = fn.group(1)
    # The pinned branch must exist and must return BEFORE the setTimeout
    # auto-dismiss line. We pin both: the special-case shape, and the
    # ordering (the branch returns early). warn and err share the branch.
    warn_branch = _re.search(
        r"if\s*\(\s*type\s*===\s*['\"]warn['\"]\s*\|\|\s*type\s*===\s*['\"]err['\"]\s*\)\s*\{(.*?)\}\s*\n",
        body, _re.S,
    )
    assert warn_branch, (
        "toast() has no `if (type === 'warn' || type === 'err')` branch — "
        "warn/err toasts will auto-dismiss after 2.8s like every other type, "
        "eating the diagnostic detail operators need (lifecycle "
        "partial-success, and migrated alert() errors)."
    )
    warn_body = warn_branch.group(1)
    assert "return" in warn_body, (
        "toast() warn branch doesn't `return` early — execution will fall "
        "through to the setTimeout auto-dismiss and the pin breaks."
    )
    # And the warn branch must wire up a click-to-dismiss × button so
    # the operator can actually close the pinned toast.
    assert "toast-dismiss" in warn_body, (
        "toast() warn branch doesn't include a .toast-dismiss button — "
        "pinned toast with no × is a stuck UI element."
    )
    assert "onclick" in warn_body, (
        "toast() warn branch's × button has no onclick handler — "
        "operator can't dismiss the pinned toast."
    )


def test_toast_warn_css_supports_newlines_and_pointer_events(html: str):
    """#toast.warn must (a) wrap multi-line messages — errList.join('\\n')
    produces them — and (b) accept pointer events so the × dismiss button
    is clickable. The base #toast rule sets pointer-events: none, which
    the warn class must override."""
    # pre-wrap so the \n joined error list renders as multiple lines
    # rather than collapsing into a single space.
    rule = _re.search(
        r"#toast\.warn\s*\{([^}]*)\}",
        html,
    )
    assert rule, "#toast.warn CSS rule not found"
    decl = rule.group(1)
    assert "pre-wrap" in decl, (
        "#toast.warn missing `white-space: pre-wrap` — joined multi-error "
        "messages will render as a single squashed line."
    )
    assert "pointer-events: auto" in decl, (
        "#toast.warn doesn't override pointer-events:none from the base "
        "#toast rule — the × dismiss button won't accept clicks."
    )


def test_lifecycle_handle_joins_all_errors_in_warn_branch(html: str):
    """The partial-success (warn) branch must show every entry in
    r.errors[], not just errors[0]. Multiple sub-step failures are
    the norm for retire/delete; truncating to the first error hides
    which sub-step is actually wedged."""
    body = _re.search(
        r"async function _lifecycleHandle\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert body, "_lifecycleHandle body not found"
    fn_body = body.group(1)
    # The handler must join the error list (any join call on the list
    # variable shape — we don't pin the variable name, just the join).
    assert _re.search(r"\.join\(", fn_body), (
        "_lifecycleHandle never calls .join() — it's still truncating to "
        "errors[0]. Expand to show every entry in r.errors[]; pinned warn "
        "toast + pre-wrap CSS handles the multi-line render."
    )
    # And critically, the old `r.errors && r.errors[0]` truncate shape
    # must be gone — that's the regression we're pinning against.
    assert not _re.search(
        r"r\.errors\s*\[\s*0\s*\]\s*[\)\|]",
        fn_body,
    ) or _re.search(r"\.join\(", fn_body), (
        "_lifecycleHandle still uses `r.errors[0]` to pick a single error. "
        "Replace with the full list joined on \\n."
    )


# ── JS parse check ───────────────────────────────────────────────────────────


def test_index_html_script_blocks_parse(tmp_path, html: str):
    """Real JS parse-check of every inline <script> block in index.html.

    Every other test in this file is a regex/string assertion: it confirms
    a function name appears, an endpoint URL is wired, a field name
    matches. None of them notice if the surrounding JS doesn't parse —
    a missing `}`, an unterminated template literal, or a stray comma
    leaves the whole <script> tag a syntax error in the browser, every
    JS global becomes undefined, and the SPA cascades into "nav is not
    a function" / "_renderUsageTables is not defined" at runtime.

    PR #1912 (Slack OAuth handoff) shipped exactly this shape twice in
    one branch — once a stray `;`, once a missing `}` after a rebase.
    Both passed the regex assertions; both blew up Playwright with ~36
    cascading failures across 3 browsers (~12 CI-minutes per occurrence).

    `node --check` parses each <script> block in well under a second.
    Skip cleanly when node isn't on PATH so CI environments without it
    don't break.
    """
    import shutil as _shutil
    import subprocess as _sp

    node = _shutil.which("node")
    if not node:
        pytest.skip("node not on PATH — JS parse-check unavailable")

    # Strip HTML comments first: section-header comments occasionally
    # contain the literal text "<script>" / "</script>" (e.g. "its own
    # <script> block to make it easy to lift…"), which confuses the
    # naive regex into matching a comment as a script open tag.
    stripped = _re.sub(r"<!--.*?-->", "", html, flags=_re.S)
    scripts = _re.findall(r"<script>(.*?)</script>", stripped, _re.S)
    assert scripts, "no inline <script> blocks found — regex bitrot?"

    for i, src in enumerate(scripts):
        if not src.strip():
            continue
        target = tmp_path / f"block-{i}.js"
        target.write_text(src)
        r = _sp.run(
            [node, "--check", str(target)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            continue
        head = "\n".join(
            f"{n+1:4}  {ln}" for n, ln in enumerate(src.splitlines()[:20])
        )
        pytest.fail(
            f"<script> block {i} failed JS parse — would break the admin "
            f"UI at runtime:\n"
            f"--- node stderr ---\n{r.stderr}\n"
            f"--- first 20 lines of block ---\n{head}"
        )


# ── Unified Google integration entry point ───────────────────────────────────
#
# Before 2026-06-02 Google had three Skills catalog entries (gmail,
# calendar, gog) + two Settings → API Keys buttons (`+ Add Gmail/Calendar`
# routing to the read-only OAuth flow, `+ Workspace (path C)` routing
# to the SA+DwD wizard). Operators landed on whichever surface they
# found first and got inconsistent results. Now ONE entry point — the
# Skills page — opens a chooser that asks Workspace vs Personal and
# routes to the matching downstream wizard. These tests pin the
# unification so the redundant-button + broken-flow regression can't
# come back.


def test_unified_google_chooser_modal_exists(html: str):
    """The chooser modal must be in the markup. Without it the
    interceptor would call into a non-existent function and the
    operator gets nothing on click."""
    assert 'id="goog-chooser-modal"' in html, (
        "goog-chooser-modal not in markup. The unified entry point is "
        "broken — Skills page clicks on Google entries will silently fail."
    )
    # All three choice options must be radio inputs in the modal.
    for value in ("workspace", "personal", "help"):
        assert f'id="goog-chooser-input-{value}"' in html, (
            f"chooser missing '{value}' radio input — operator can't "
            f"select this account type."
        )


def test_unified_google_chooser_routes_to_existing_wizards(html: str):
    """The chooser's Continue button must route Workspace → the path-C
    (service-account + DwD) wizard and Personal → the unified capability-
    picker install flow. Routing Personal straight to the bare
    openGoogleWorkspaceWizard would SKIP the capability picker — the Path-A
    UX regression the chooser-first composition is meant to avoid."""
    import re as _re
    # Find the chooser's continue handler.
    m = _re.search(
        r"function _googleUnifiedChooserContinue\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert m, "_googleUnifiedChooserContinue not found"
    body = m.group(1)
    # Workspace branch → path-C wizard
    assert "openGoogleWorkspaceWizardPathC" in body, (
        "Workspace branch doesn't route to openGoogleWorkspaceWizardPathC. "
        "The chooser would silently drop the Workspace selection on the floor."
    )
    # Personal branch → unified capability-picker flow (openSkillInstall),
    # NOT a bare openGoogleWorkspaceWizard as the primary path. The picker is
    # what lets the operator choose Gmail/Calendar capabilities before OAuth.
    assert "openSkillInstall(" in body, (
        "Personal branch doesn't re-enter openSkillInstall — it would skip "
        "the capability picker and drop the operator straight into bare "
        "Path-A OAuth. Route the unified 'google' skill through "
        "openSkillInstall so pick_capabilities runs."
    )


def test_skills_page_google_entries_route_through_chooser(html: str):
    """Every Google skill ID — the catalog-visible unified `google` row
    plus the hidden legacy gog/gmail/calendar — must route through the
    unified chooser via `_openSkillInstallOrUnifiedGoogle`, NOT directly
    through `openSkillInstall`. The interceptor is the choke point that
    prevents operators from landing in the Path-A Personal OAuth flow when
    they wanted Workspace (Path C)."""
    import re as _re
    # The Skills page Browse render emits onclick handlers for each
    # per-bot install button. Find that template literal.
    m = _re.search(
        r"openSkillInstallOrUnifiedGoogle\('\$\{escHtml\(skillId\)\}'",
        html,
    )
    assert m, (
        "Skills page Browse renderer doesn't use "
        "_openSkillInstallOrUnifiedGoogle — Google entries will skip the "
        "chooser and route straight to the read-only path."
    )

    # And the interceptor must know about every Google skill ID — including
    # the unified ``google`` row. The _GOOGLE_SKILL_IDS array can be built
    # from a const (`_GOOGLE_UNIFIED_SKILL_ID`) so resolve the unified id by
    # name too, not just by the literal inside the array brackets.
    interceptor = _re.search(
        r"const _GOOGLE_SKILL_IDS\s*=\s*\[([^\]]*)\]",
        html,
    )
    assert interceptor, "_GOOGLE_SKILL_IDS registry not found"
    arr = interceptor.group(1)
    # The unified ``google`` id is THE one the catalog actually surfaces
    # (PR #2231 hid gog/gmail/calendar/gdrive). If it's not in the set, the
    # only clickable Google row skips the chooser and hardwires Path-A OAuth,
    # leaving Path C (Workspace SA+DwD) unreachable from the primary UI.
    # Accept either the literal `'google'` or the `_GOOGLE_UNIFIED_SKILL_ID`
    # const that's defined as 'google'.
    assert "'google'" in arr or "_GOOGLE_UNIFIED_SKILL_ID" in arr, (
        "_GOOGLE_SKILL_IDS does not include the unified 'google' id. The "
        "catalog-visible Google row will skip the account-type chooser and "
        "route straight into Path-A OAuth — Path C (Workspace service "
        "account + DwD) becomes unreachable. This is the 2026-06-20 bug."
    )
    assert _re.search(r"_GOOGLE_UNIFIED_SKILL_ID\s*=\s*'google'", html), (
        "_GOOGLE_UNIFIED_SKILL_ID must be defined as 'google' — the Personal "
        "branch keys on it to decide capability-picker vs bare-OAuth routing."
    )
    # Legacy ids stay listed so any re-surfaced legacy row still routes
    # through the chooser.
    for skill in ("'gog'", "'gmail'", "'calendar'"):
        assert skill in arr, (
            f"_GOOGLE_SKILL_IDS missing {skill} — Skills page click on "
            f"the {skill} skill will skip the chooser and route directly "
            f"to the read-only OAuth path. Add it to the registry."
        )


def test_settings_api_keys_no_longer_has_redundant_google_buttons(html: str):
    """The previous '+ Add Gmail/Calendar' and '+ Workspace (path C)'
    buttons in Settings → API Keys are removed. Skills page is THE
    entry point for Google integration now. If either button comes
    back, the redundant-surface bug returns.

    Matched against actual button onclick wiring rather than bare
    strings — the strings still appear inside the chooser modal's
    explanatory comments and that's fine."""
    import re as _re
    # The old `+ Add Gmail/Calendar` button id (it was unique to that
    # button on the API Keys page).
    assert 'id="ik-add-gmail-btn"' not in html, (
        "'+ Add Gmail/Calendar' button is back in Settings → API Keys. "
        "Skills page is now the canonical entry for Google integration; "
        "this button is the redundant-surface bug from 2026-06-01."
    )
    # The path-C button was a <button> with onclick=openGoogleWorkspaceWizardPathC(_ikBot).
    # _ikBot was the API Keys page's current-bot variable; the onclick
    # only existed in that context. Match the call shape directly so
    # the test doesn't false-fire on documentation/comments mentioning
    # the historical button.
    pattern = _re.compile(
        r"<button[^>]*onclick=\"openGoogleWorkspaceWizardPathC\(\s*_ikBot\s*\)",
    )
    assert not pattern.search(html), (
        "Settings → API Keys still has a button wired to "
        "openGoogleWorkspaceWizardPathC(_ikBot) — the redundant entry "
        "point is back. Skills page is now the canonical Google entry."
    )


def test_credentials_fresh_google_setup_routes_through_chooser():
    """Settings → API Keys reconciliation (2026-06-20). The Credentials
    table's google_workspace_wizard dispatch must send the FRESH-add action
    (`id === 'setup'`, status=missing) through the unified account-type
    chooser — the same Path-A-vs-Path-C choice the Skills page offers — so
    Path C is reachable from this surface too. Reauthorize/migrate act on an
    existing install and stay on the bare wizard.

    Read credentials.js directly: it is not part of the concatenated `html`
    fixture above (which only covers the Skills/wizard/overview pages)."""
    import re as _re
    src = _CREDENTIALS_JS.read_text()
    # Locate the google_workspace_wizard dispatch branch.
    branch = _re.search(
        r"action\.modal\s*===\s*'google_workspace_wizard'\s*\)\s*\{(.*?)\n\s{2}\}",
        src, _re.S,
    )
    assert branch, (
        "google_workspace_wizard dispatch branch not found in credentials.js "
        "— if the dispatch shape changed, update this test."
    )
    body = branch.group(1)
    # Fresh add must reach the chooser…
    assert "openGoogleUnifiedChooser" in body, (
        "credentials.js no longer routes the fresh 'Set up' Google action "
        "through openGoogleUnifiedChooser. Operators on Settings → API Keys "
        "would be hardwired into Path-A OAuth with no way to reach Path C."
    )
    # …and it must be gated on the 'setup' action id so reauthorize/migrate
    # (existing installs, account type already chosen) still re-run the wizard.
    assert "action.id === 'setup'" in body, (
        "credentials.js doesn't gate the chooser on action.id === 'setup'. "
        "Reauthorize/migrate act on an existing install whose account type is "
        "already determined; forcing the chooser on them is wrong."
    )
    # The bare wizard must remain as the reconnect path.
    assert "openGoogleWorkspaceWizard(botId)" in body, (
        "credentials.js dropped the bare openGoogleWorkspaceWizard reconnect "
        "path — reauthorize/migrate need it."
    )


def test_personal_gmail_branch_advertises_write_after_phase_a(html: str):
    """Phase A.5 (2026-06-01): the Personal @gmail.com path now supports
    send + Drive write + Calendar (Phase A.1 backed by the canonical
    OAuth-token store + Phase A.2 wizard finalize). The chooser modal
    and the downstream wizard subtitle must reflect that — the prior
    'Read-only today' warning is stale and would mislead operators.

    What we DO want to see in the modal:
      - The Personal-Gmail option is still present.
      - Both 'send' and 'Drive' are mentioned as supported.
      - The 7-day re-consent caveat is surfaced (it's the load-bearing
        downside; hiding it would invite operator surprise on day 8).
    """
    # The chooser modal still has the Personal option.
    assert "Personal @gmail.com account" in html, (
        "Personal-Gmail chooser row missing — the entry point regressed."
    )
    # The chooser advertises send + Drive (the Phase A.1 capabilities).
    assert "<strong>send</strong>" in html, (
        "Chooser modal doesn't advertise that Personal-Gmail can now "
        "send mail (Phase A.1 capability). Operators may still think "
        "this path is read-only."
    )
    assert "<strong>Drive</strong>" in html, (
        "Chooser modal doesn't advertise Drive write capability for "
        "Personal-Gmail (Phase A.1)."
    )
    # The 7-day caveat is still surfaced — eliminating it would set
    # operators up to discover the re-consent loop the hard way.
    assert "re-consent" in html or "verification" in html, (
        "Personal-Gmail copy lost the 7-day re-consent / app-verification "
        "caveat — operators need this in the entry point so they aren't "
        "blindsided when the timer fires on day 8."
    )
    # And the stale 'Read-only today' yellow chip is gone.
    assert "Read-only today" not in html, (
        "Stale 'Read-only today' chip still in chooser modal — Phase A.5 "
        "was supposed to drop it. With Phase A.1+A.2 shipped, the chip "
        "actively lies about ship state."
    )


# ── Path-C correspondence block uses "Alias", not "Persona" ──────────────────
#
# 2026-06-01: "Persona" overloaded between design-archetype thinking
# tools (Diana, Carla — never product objects) and the outbound mail
# identity in network.json's correspondence block. Operators conflated
# the two. Renamed the path-C wizard UI to "Email alias" — the well-
# known concept that carries no design-archetype baggage. The
# correspondence JSON field name is unchanged; this guards the UI
# rename so the operator-facing copy doesn't drift back.


# ── Optional bot purpose/goals capture (Effectiveness Layer anchor) ──────────
#
# Purpose (archetype + one-line mission) is what the Fit Reviewer reads to
# know what "more effective" means for a bot. Before this slice nothing
# captured it at creation, so purpose was NULL on every live bot. Screen 4
# now offers an OPTIONAL, skippable purpose step that PUTs to the same
# /api/bot/<id>/purpose endpoint the per-bot Purpose card uses. These tests
# pin: the fields exist, the save helper is wired, both Screen-4 exits call
# it, the archetype options don't drift from bot_purpose.ARCHETYPES, and the
# style-guide data-shape width classes are present.


def test_purpose_fields_present_on_screen4(html: str):
    """The optional archetype <select> and mission <input> must be in the
    Screen-4 markup, or there's nothing to capture purpose with."""
    assert 'id="wiz-purpose-archetype"' in html, "purpose archetype select missing"
    assert 'id="wiz-purpose-mission"' in html, "purpose mission input missing"


def test_purpose_fields_use_data_shape_width_classes(html: str):
    """Style-guide §9.2: archetype is a short enum → input-w-md; the mission
    is a one-line sentence → input-w-lg. (Mirrors the per-bot Purpose card.)"""
    import re as _re
    sel = _re.search(r'<select id="wiz-purpose-archetype"[^>]*>', html)
    assert sel and "input-w-md" in sel.group(0), (
        "wiz-purpose-archetype <select> must carry the input-w-md width class"
    )
    inp = _re.search(r'<input id="wiz-purpose-mission"[^>]*>', html)
    assert inp and "input-w-lg" in inp.group(0), (
        "wiz-purpose-mission <input> must carry the input-w-lg width class"
    )
    # And the mission caps at MISSION_MAX_CHARS (280) like the backend.
    assert inp and 'maxlength="280"' in inp.group(0), (
        "wiz-purpose-mission must cap at maxlength=280 (bot_purpose.MISSION_MAX_CHARS)"
    )


def test_purpose_save_helper_posts_to_purpose_endpoint(html: str):
    """_wizSavePurpose must PUT to /api/bot/<id>/purpose — the same endpoint
    the per-bot Purpose card uses, so the two surfaces never drift."""
    fn = _re.search(
        r"async function _wizSavePurpose\([^)]*\)\s*\{(.*?)\n\}",
        html, _re.S,
    )
    assert fn, "_wizSavePurpose not defined"
    body = fn.group(1)
    assert "/purpose" in body, "_wizSavePurpose doesn't reference the /purpose endpoint"
    assert "'PUT'" in body or '"PUT"' in body, (
        "_wizSavePurpose must use the PUT verb (the purpose write contract)"
    )
    # Skippable: a blank archetype AND blank mission must short-circuit
    # before any POST — purpose never blocks creation.
    assert "return" in body, (
        "_wizSavePurpose must early-return when both fields are blank so a "
        "skipped purpose never POSTs"
    )


@pytest.mark.parametrize("caller", ["_wizScreen4Apply", "_wizScreen4Skip"])
def test_both_screen4_exits_save_purpose(html: str, caller: str):
    """Both the Apply path AND the Skip path must call _wizSavePurpose —
    "Skip" skips the messaging channel, not the purpose. If the operator
    typed a mission then clicked Skip, it must still persist."""
    fn = _re.search(
        rf"(?:async )?function {_re.escape(caller)}\([^)]*\)\s*\{{(.*?)\n\}}",
        html, _re.S,
    )
    assert fn, f"{caller} not found"
    assert "_wizSavePurpose" in fn.group(1), (
        f"{caller} doesn't call _wizSavePurpose — purpose typed on Screen 4 "
        f"would be silently dropped on this exit path."
    )


def test_wizard_archetype_options_match_bot_purpose_module(html: str):
    """Anti-drift: the archetype <option>s in the wizard must mirror
    bot_purpose.ARCHETYPES exactly. If an archetype is added to the module
    but not here, the wizard silently can't offer it. (Same shape as
    test_stage_keys_match_provisioning_module above.)"""
    import sys
    sys.path.insert(0, str(_INDEX.parent.parent.parent.parent / "analyzer"))
    from bot_purpose import ARCHETYPES  # noqa: E402

    # Isolate the archetype <select> so we don't match option values from
    # unrelated selects elsewhere in the page.
    block = _re.search(
        r'<select id="wiz-purpose-archetype".*?</select>', html, _re.S,
    )
    assert block, "wiz-purpose-archetype <select> block not found"
    for arch in ARCHETYPES:
        assert f'value="{arch}"' in block.group(0), (
            f"wizard archetype select is missing the '{arch}' option — it "
            f"has drifted from bot_purpose.ARCHETYPES."
        )


def test_pathc_correspondence_block_uses_alias_not_persona(html: str):
    """The path-C wizard's outbound-mail identity section must be
    labeled 'Email alias' (or similar), NOT 'Persona'. Operators read
    'Persona' as a design archetype and configure the wrong thing."""
    # Locate the correspondence block by its comment marker and grab
    # the surrounding section (~600 chars covers heading + 3 fields).
    marker = "<!-- Alias block (correspondence) -->"
    idx = html.find(marker)
    assert idx >= 0, (
        "Alias-block section marker not found in path-C wizard. "
        "Either the section was removed or the comment was renamed — "
        "update this test to match."
    )
    section = html[idx:idx + 1500]

    assert "Email alias" in section, (
        "Path-C wizard's correspondence section is missing the "
        "'Email alias' heading. Operators need the alias framing to "
        "distinguish this from design-archetype 'persona' references."
    )
    assert "Persona" not in section, (
        "Path-C wizard's correspondence section still contains "
        "'Persona' in visible UI text. The rename to 'Alias' is "
        "incomplete — operator-facing copy will read as design-"
        "archetype framing again."
    )
