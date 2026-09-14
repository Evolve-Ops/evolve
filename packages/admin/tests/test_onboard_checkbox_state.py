"""Pin the wizard's per-bot checkbox state-persistence fix.

The 2026-06-07 GitHub setup wizard rendered every per-bot row with
a hardcoded `<input type="checkbox" checked>` and re-rendered on
every onchange. The user's uncheck triggered a re-render which
immediately re-hardcoded `checked`, so checkboxes felt frozen.

Fix: persist selection state in _onboardState.perBot[botId].selected
and render the `checked` attribute from it. This test source-pins
that structure so a future refactor doesn't silently revert.
"""
from __future__ import annotations

from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "index.html"
_ONBOARD_MODAL_JS = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "static" / "js" / "pages" / "onboard-modal.js"
# _onboardRenderBotsList + onboardSetSelected moved to
# pages/onboard-modal.js (Phase 3ae).
_TEXT = _INDEX_HTML.read_text() + "\n" + _ONBOARD_MODAL_JS.read_text()


def test_onboard_checkbox_renders_from_state_not_hardcoded():
    """The wizard's per-bot input must render `checked` from state.
    Look for a `selectedAttr` derivation in _onboardRenderBotsList.
    The pre-fix hardcoded 'checked' attribute is what re-engaged on
    every render.
    """
    # Slice _onboardRenderBotsList.
    start = _TEXT.find("function _onboardRenderBotsList(")
    assert start > 0
    end = _TEXT.find("\n}\n", start)
    body = _TEXT[start:end]
    # The fix introduces this exact variable / pattern.
    assert "selectedAttr" in body, (
        "_onboardRenderBotsList must compute selectedAttr from per-bot "
        "state — a hardcoded `checked` attribute makes the wizard's "
        "checkboxes un-uncheckable (2026-06-07 user-reported bug)."
    )
    # Hardcoded `checked` on the per-bot data-bot input is the smell.
    # The new code interpolates ${selectedAttr}. Catch a regression
    # that drops the interpolation back to a literal `checked`.
    assert 'data-bot="${escHtml(b)}" checked onchange' not in body, (
        "regression: per-bot checkbox is hardcoded `checked` — state "
        "won't persist across re-renders, user can't uncheck bots"
    )


def test_onboard_checkbox_has_setSelected_callback():
    """The onchange handler must save the new state, not just trigger
    a re-render. The pre-fix wired onchange→_onboardRenderBotsList,
    which re-rendered before the new state was captured.
    """
    assert "function onboardSetSelected(" in _TEXT, (
        "onboardSetSelected helper must exist to capture user toggles"
    )
    assert "onboardSetSelected(" in _TEXT, (
        "the per-bot checkbox onchange must call onboardSetSelected to "
        "persist the new state across re-renders"
    )


def test_onboardSetSelected_sets_state_and_updates_submit():
    """The helper must set perBot[bot].selected AND call
    _onboardUpdateSubmitState so the submit button enable/disable
    stays consistent with the new selection set.
    """
    start = _TEXT.find("function onboardSetSelected(")
    assert start > 0
    end = _TEXT.find("\n}\n", start)
    body = _TEXT[start:end]
    assert ".selected = " in body
    assert "_onboardUpdateSubmitState" in body
