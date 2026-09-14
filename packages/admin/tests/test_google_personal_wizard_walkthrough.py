"""Pin the Path-A Google wizard's GCP setup walkthrough UX fixes.

Two operator-reported gaps (2026-06-21) in the "Set up Google for <bot>"
wizard's GCP-walkthrough screen (``gws-screen-gcp`` in index.html, driven
by ``openGoogleWorkspaceWizard`` in skill-install.js):

1. The walkthrough never gave a clear, dedicated "add a Test user" step.
   The only mention was a buried sub-bullet that said "add your own Gmail
   address" — wrong account for a bot, and easy to miss. Without the bot's
   @gmail on the consent screen's Test users list (in the SAME project as
   the OAuth client), consent dies with "Access blocked: app is being
   tested / Error 403: access_denied". An operator hit this twice,
   including a case where the test user landed in the wrong project.

2. The collapsible steps rendered with no expand affordance (the inline
   ``display:flex`` on each <summary> suppresses the native disclosure
   marker), so it wasn't discoverable they expand. The fix adds the
   canonical ``.expand-icon`` chevron (style-guide §9.13) to each summary,
   which auto-rotates via ``details[open] > summary .expand-icon``.

These tests source-pin both fixes so a future refactor can't silently
revert them.
"""
from __future__ import annotations

from pathlib import Path

_INDEX_HTML = (
    Path(__file__).resolve().parent.parent
    / "evolve_admin" / "web" / "index.html"
)
_TEXT = _INDEX_HTML.read_text()


def _gcp_screen() -> str:
    """Slice the GCP-walkthrough screen markup (Screen 1 of the wizard).

    Bounded by the screen's own id and the next screen's id so the
    assertions can't accidentally match unrelated wizard markup.
    """
    start = _TEXT.find('id="gws-screen-gcp"')
    assert start > 0, "gws-screen-gcp not found in index.html"
    end = _TEXT.find('id="gws-screen-services"', start)
    assert end > start, "gws-screen-services (end marker) not found"
    return _TEXT[start:end]


# ── Gap 1: Test-user step + same-project guard ──────────────────────────────


def test_walkthrough_has_dedicated_test_user_step():
    """A discoverable, dedicated Test-user step must exist — not just a
    buried sub-bullet. Pin the step title."""
    gcp = _gcp_screen()
    assert "Add a Test user" in gcp, (
        "the GCP walkthrough must name a dedicated 'Add a Test user' step "
        "— without it, consent dies with 'app is being tested / 403'"
    )


def test_test_user_step_names_the_signin_account_not_the_admin():
    """The step must tell the operator to add the account they'll CONSENT
    as (the bot's @gmail), not 'your own Gmail address'. That ambiguity was
    the operator's confusion."""
    gcp = _gcp_screen()
    # The new copy ties the test user to the sign-in/consent account.
    assert "Sign in with Google" in gcp or "sign in as" in gcp, (
        "the Test-user step must tie the account to the one used for "
        "'Sign in with Google', so the operator adds the bot's @gmail"
    )
    assert "@gmail" in gcp, (
        "the Test-user guidance must reference the bot's @gmail account"
    )
    # The pre-fix misleading phrasing must be gone.
    assert "add your own Gmail address" not in gcp, (
        "regression: the buried 'add your own Gmail address' bullet is "
        "misleading for a bot — it should name the sign-in account"
    )


def test_walkthrough_has_same_project_guard():
    """The 'OAuth client and Test user must be in the SAME project' guard
    must be present — that mismatch was the operator's actual dead-end."""
    gcp = _gcp_screen()
    lowered = gcp.lower()
    assert "same gcp project" in lowered or "same project" in lowered, (
        "the walkthrough must warn that the OAuth client and the Test user "
        "must live in the SAME GCP project (the operator's real dead-end)"
    )
    # Surfaced near the client-ID input too, per the chip's request.
    assert "Same-project check" in gcp, (
        "the same-project guard must also surface near the Client ID input"
    )


def test_walkthrough_keeps_7day_publish_note():
    """The accurate 7-day testing-mode / publish-for-durability note must
    survive the rework."""
    gcp = _gcp_screen()
    assert "7 days" in gcp and "Publish" in gcp, (
        "the 7-day re-consent / publish note must be retained"
    )


# ── Gap 2: expand affordance on every collapsible step ──────────────────────


def test_every_walkthrough_step_has_expand_icon():
    """Each <details class="gws-step"> summary must carry the canonical
    .expand-icon chevron (§9.13). The inline display:flex on the summary
    kills the native marker, so without the chevron the steps look
    non-expandable."""
    gcp = _gcp_screen()
    n_steps = gcp.count('class="gws-step"')
    assert n_steps >= 5, (
        f"expected the multi-step GCP walkthrough; found {n_steps} gws-step "
        "blocks"
    )
    n_chevrons = gcp.count('class="expand-icon"')
    assert n_chevrons >= n_steps, (
        f"every gws-step <summary> needs an .expand-icon chevron: "
        f"{n_steps} steps but only {n_chevrons} chevrons"
    )


def test_walkthrough_uses_svg_chevron_not_unicode_glyph():
    """The affordance must be the SVG chevron, never a Unicode triangle
    (style-guide §9.13 / CLAUDE.md 'five highest-violation rules')."""
    gcp = _gcp_screen()
    for glyph in ("▸", "▾", "▼", "▶", "⟩"):  # ▸▾▼▶⟩
        assert glyph not in gcp, (
            f"Unicode expand glyph {glyph!r} found — use the .expand-icon "
            "SVG chevron instead (§9.13)"
        )
    # The canonical chevron polyline must be present.
    assert "<polyline points=\"9 18 15 12 9 6\"/>" in gcp, (
        "the .expand-icon must use the canonical chevron SVG polyline"
    )
