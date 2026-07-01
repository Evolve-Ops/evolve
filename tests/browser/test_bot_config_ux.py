"""Browser smoke for the PR K Bot subtab UX overhaul (2026-06-01).

Each card under Settings → Pod Config → Bot must answer
"what is this, why do I care, what's set, how do I change it"
within 5 seconds without hover. This test pins the structural
invariants that make that bar enforceable in CI:

1. The Bot subtab renders without JS errors on bot select.
2. The Display Name input has a max-width (NOT full-card width).
3. The Daily Spend Cap input has a narrow max-width (~120px).
4. The Slack Policy card is hidden for a bot with no Slack integration.
5. The Slack Policy card is visible for a bot with Slack configured.
6. Primary Model and Compaction Settings cards carry a visible
   ``.card-subtitle`` (not just a hover tooltip).
7. Section headers (``.botcfg-section-header``) are present, so the
   13 cards are grouped instead of being one continuous scroll.

The full SPA bot-picker path requires bots in network.json + tab
priming. Tests here force-activate the Bot subtab (same pattern as
``test_customizations_picker.py``) and drive renderers directly via
``page.evaluate``. Hermetic, route-driven, no real pod state.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)


_TEST_BOT_ID = "team-bot-a"

# Pre-existing baseline JS errors during SPA boot (page-load redirect
# stubs calling nav() before it's defined). Same allow-list shape as
# ``test_smoke.py``. Anything outside this list is a regression
# introduced by this PR.
_BASELINE_PAGEERROR_SUBSTRINGS: tuple[str, ...] = (
    "nav is not defined",
    "Can't find variable: nav",
)


def _new_errors_only(errors: list[str]) -> list[str]:
    """Filter out known-baseline pageerror strings."""
    return [
        e for e in errors
        if not any(substr in e for substr in _BASELINE_PAGEERROR_SUBSTRINGS)
    ]


def _force_activate_bot_config_subtab(page) -> None:
    """Make ``#settings-bots`` visible without driving the SPA's nav()
    path.

    Post the 2026-06-01 Settings restructure, the per-bot cards live on
    the top-level Settings → Bots subtab — one level deep, no inner
    nesting.
    """
    page.evaluate(
        """
        () => {
          document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
          document.getElementById('page-settings').classList.add('active');
          document.querySelectorAll('#page-settings > .subtab-page').forEach(p => p.classList.remove('active'));
          document.getElementById('settings-bots').classList.add('active');
        }
        """
    )


def _stub_bot_endpoints(page, *, slack_enabled: bool) -> None:
    """Stub the GET endpoints loadConfigBot() fans out to.

    slack_enabled=True simulates a bot with a real Slack integration —
    the Slack Policy card MUST render visibly.
    slack_enabled=False simulates a bot with no slack block at all —
    the Slack Policy card mount-point MUST be hidden via display:none.
    """
    bot = _TEST_BOT_ID

    def _ok(payload):
        def _h(route, request):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload))
        return _h

    # Bot Setup + User Profile (used by the top two body sections).
    page.route(f"**/api/arbiter/bot-setup/{bot}", _ok({
        "ok": True, "archetype": "assistant", "surfacing_cadence": "morning",
        "monthly_cap_usd": 30, "daily_warn_usd": 1, "daily_hard_usd": 2,
        "timezone": "America/Los_Angeles",
    }))
    page.route(f"**/api/arbiter/profile/{bot}", _ok({
        "ok": True, "has_content": False, "frontmatter": {}, "body": "",
    }))
    page.route(
        f"**/api/bot/models?bot={bot}",
        _ok({
            "primary": "anthropic/claude-sonnet-4-5",
            "fallbacks": ["anthropic/claude-haiku-4-5"],
            "per_agent": {},
        }),
    )
    page.route(
        f"**/api/bot/compaction?bot={bot}",
        _ok({
            "mode": "auto",
            "reserveTokensFloor": 32000,
        }),
    )
    page.route(
        f"**/api/bot/config?bot={bot}",
        _ok({"agents": {"defaults": {"model": {"primary": "anthropic/claude-sonnet-4-5"}}}}),
    )
    page.route(
        f"**/api/classifier/keywords?bot={bot}",
        _ok({
            "scope": "pod-wide",
            "base": {"productive": ["draft"], "maintenance": ["status"], "correction": ["wait"]},
            "calibration": {},
            "network_hints": {},
            "source_file": "TierClassifier.ts",
            "calibration_writer_implemented": False,
        }),
    )
    page.route(
        f"**/api/customizations/{bot}",
        _ok({"ok": True, "entries": [], "needs_review_count": 0}),
    )
    # Slack policy — shape varies by whether the bot has the integration.
    if slack_enabled:
        slack_payload = {
            "bot_id": bot,
            "policy_exists": True,
            "policy_load_error": None,
            "policy": {"channels": []},
            "drift_state": "in_sync",
            "doctor": {
                "slack_enabled": True,
                "transport_mode": "socket",
                "group_policy": "mention_only",
                "dm_policy": "closed",
                "has_signing_secret": True,
                "has_app_token": True,
                "streaming_mode": "off",
                "streaming_native_transport": False,
                "bot_token_source": "openclaw_channels",
                "workspace_name": "Test Workspace",
                "workspace_team_id": "T12345",
                "listening_channels": [],
                "joined_not_listening": [],
                "allow_from_user_ids": [],
                "allow_from_count": 0,
                "other_provider_keys": [],
                "feature_bundles": [],
                "elevated_scopes_granted": [],
                "oauth_scopes": [],
                "oauth_scopes_count": 0,
                "findings": [],
                "has_fail": False,
            },
        }
    else:
        # Telegram-only bot: no slack block at all.
        slack_payload = {
            "bot_id": bot,
            "policy_exists": False,
            "policy_load_error": None,
            "policy": None,
            "drift_state": "unknown",
            "doctor": {
                "slack_enabled": None,
                "transport_mode": None,
                "group_policy": None,
                "dm_policy": None,
                "has_signing_secret": False,
                "has_app_token": False,
                "streaming_mode": None,
                "streaming_native_transport": None,
                "bot_token_source": None,
                "workspace_name": "",
                "workspace_team_id": "",
                "listening_channels": [],
                "joined_not_listening": [],
                "allow_from_user_ids": [],
                "allow_from_count": 0,
                "other_provider_keys": [],
                "feature_bundles": [],
                "elevated_scopes_granted": [],
                "oauth_scopes": [],
                "oauth_scopes_count": 0,
                "findings": [],
                "has_fail": False,
            },
        }
    page.route(f"**/api/bot/slack-policy?bot={bot}", _ok(slack_payload))


def _bring_up_bot_subtab(page, admin_server: str, *, slack_enabled: bool) -> None:
    """Boot the SPA, stub everything, activate the Bot subtab,
    set _configBot, and drive loadConfigBot()."""
    page.add_init_script(
        "Object.defineProperty(navigator, 'serviceWorker', {value: undefined});"
    )
    _stub_bot_endpoints(page, slack_enabled=slack_enabled)
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Give the SPA a chance to attach its event handlers + finish the
    # home-page fetch fan-out before we force-activate. Both `fetch`
    # and `api` paths must be settled before we drive the next render.
    page.wait_for_function(
        "() => typeof loadConfigBot === 'function' && document.getElementById('botcfg-models') !== null",
        timeout=15_000,
    )
    _force_activate_bot_config_subtab(page)
    # Drive the renderer via switchConfigBot — it's the only documented
    # writer of the closure-scoped _configBot module variable. Setting
    # window._configBot doesn't reach the closure (let-binding, not a
    # window property), which would silently make loadConfigBot()
    # short-circuit on the !botId guard.
    page.evaluate(f"switchConfigBot({json.dumps(_TEST_BOT_ID)});")
    # Wait for the models card to populate from our stubbed fetch.
    page.wait_for_function(
        """() => {
          const el = document.getElementById('botcfg-models');
          return el && el.textContent && el.textContent.includes('Primary model');
        }""",
        timeout=15_000,
    )


# ── 1. Page loads cleanly ────────────────────────────────────────────────────


def test_bot_subtab_renders_without_jserror(page, admin_server: str) -> None:
    """Activating the Bot subtab + driving loadConfigBot must not throw.

    Filters known baseline page-load errors (the nav-redirect-stubs
    issue tracked separately) so the test catches NEW regressions
    introduced by this PR's changes.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    _bring_up_bot_subtab(page, admin_server, slack_enabled=True)
    new = _new_errors_only(errors)
    assert not new, f"loadConfigBot raised new errors not in baseline: {new}"


# ── 2. Display Name input has a max-width ────────────────────────────────────


def test_display_name_input_is_not_full_width(page, admin_server: str) -> None:
    """Display Name input must cap at ~320px, not span the card.

    A bot display name is at most 40 chars; a full-card-width input
    reads as "edit a paragraph", not "edit a short label".
    """
    _bring_up_bot_subtab(page, admin_server, slack_enabled=True)
    # The rename input lives inside a wrapper with max-width:320px.
    wrapper_max_width = page.evaluate(
        """
        () => {
          const inp = document.getElementById('botcfg-rename-input');
          if (!inp) return null;
          const wrapper = inp.parentElement;
          return wrapper ? wrapper.style.maxWidth : null;
        }
        """
    )
    assert wrapper_max_width == "320px", (
        f"Display Name input wrapper should have max-width:320px (got {wrapper_max_width!r}). "
        f"Operator quote: 'Several fields have hugely wide drop downs or entry windows "
        f"beyond what is needed (eg display name).' Fix is a hard max, not width:100%."
    )


# test_daily_spend_cap_input_is_narrow removed in Phase 4 of the 2026-06
# cost-cap normalization. The standalone "Daily Spend Cap" card was
# deleted; the daily hard cap field lives in the canonical "Cost & caps"
# card above (rendered by renderBotSetup), which has its own width rules.


# ── 3. Slack Policy card visibility ─────────────────────────────────────────


def test_slack_card_hidden_for_non_slack_bot(page, admin_server: str) -> None:
    """A bot with no slack block must hide the Slack Policy card entirely.

    Operator quote: 'There is a slack policy tile even for bots with no
    slack integration.' The fix is hiding the mount-point AND the
    section header when no channel cards are visible.
    """
    _bring_up_bot_subtab(page, admin_server, slack_enabled=False)
    card = page.locator("#botcfg-slack-card")
    assert card.evaluate("el => el.style.display") == "none", (
        "Slack Policy card must be display:none when the bot has no slack block."
    )
    # The channel section header also hides when no channel cards are visible.
    section = page.locator("#botcfg-section-channels")
    assert section.evaluate("el => el.style.display") == "none", (
        "Channels section header should hide when no channel cards are visible."
    )


def test_slack_card_visible_for_slack_bot(page, admin_server: str) -> None:
    """A bot with a real Slack integration shows the Slack Policy card."""
    _bring_up_bot_subtab(page, admin_server, slack_enabled=True)
    card = page.locator("#botcfg-slack-card")
    # display:'' (cleared) means "use the default" — i.e. visible.
    card_display = card.evaluate("el => el.style.display")
    assert card_display == "", (
        f"Slack Policy card should be visible for a Slack-enabled bot "
        f"(got display:{card_display!r})."
    )
    section_display = page.locator("#botcfg-section-channels").evaluate(
        "el => el.style.display"
    )
    assert section_display == "", (
        f"Channels section header should be visible when at least one "
        f"channel card is visible (got display:{section_display!r})."
    )


# ── 4. Visible card subtitles (not just hover tooltips) ─────────────────────


def test_primary_model_card_has_visible_subtitle(page, admin_server: str) -> None:
    """The Primary Model card must explain what/why visibly (no hover)."""
    _bring_up_bot_subtab(page, admin_server, slack_enabled=True)
    # The card's mount-point is #botcfg-models; the .card-subtitle lives
    # in the PARENT .card element, alongside the .card-title.
    subtitle_text = page.evaluate(
        """
        () => {
          const mount = document.getElementById('botcfg-models');
          if (!mount) return null;
          const card = mount.closest('.card');
          if (!card) return null;
          const sub = card.querySelector('.card-subtitle');
          return sub ? sub.textContent.trim() : null;
        }
        """
    )
    assert subtitle_text, "Primary Model card must have a visible .card-subtitle."
    assert "LLM" in subtitle_text or "model" in subtitle_text.lower(), subtitle_text


def test_compaction_card_has_visible_subtitle(page, admin_server: str) -> None:
    """The Compaction Settings card must explain what/why visibly (no hover)."""
    _bring_up_bot_subtab(page, admin_server, slack_enabled=True)
    subtitle_text = page.evaluate(
        """
        () => {
          const mount = document.getElementById('botcfg-compaction');
          if (!mount) return null;
          const card = mount.closest('.card');
          if (!card) return null;
          const sub = card.querySelector('.card-subtitle');
          return sub ? sub.textContent.trim() : null;
        }
        """
    )
    assert subtitle_text, "Compaction Settings card must have a visible .card-subtitle."
    assert "compaction" in subtitle_text.lower() or "summarize" in subtitle_text.lower(), subtitle_text


# ── 5. Section headers present (visual grouping) ─────────────────────────────


def test_section_headers_present(page, admin_server: str) -> None:
    """The Bot subtab must group cards under visible section headers.

    13 cards on a single scroll feed is overwhelming. The fix is
    grouping into Identity / Behavior & model / Channels / Reliability /
    Advanced sections — each with a header. (The Budget & limits section
    was retired in Phase 4 of the 2026-06 cost-cap normalization; cost
    knobs now live in the canonical "Cost & caps" card rendered by
    renderBotSetup above this section structure.)
    """
    _bring_up_bot_subtab(page, admin_server, slack_enabled=True)
    # Count visible section headers (Channels header may be hidden if
    # no channel cards are visible; here Slack is on, so it should be
    # visible too).
    headers = page.evaluate(
        """
        () => {
          const els = document.querySelectorAll('#settings-bots .botcfg-section-header');
          return Array.from(els).map(e => ({
            text: e.textContent.trim(),
            visible: getComputedStyle(e).display !== 'none',
          }));
        }
        """
    )
    visible_titles = [h["text"] for h in headers if h["visible"]]
    # We expect at least 4 visible groups (Identity, Behavior & model,
    # Channels [slack on], Reliability, Advanced).
    assert len(visible_titles) >= 4, (
        f"Expected at least 4 visible section headers, got {visible_titles}."
    )
    # Spot-check that the canonical group names exist.
    text_blob = " | ".join(visible_titles).lower()
    for expected in ("identity", "behavior", "reliability", "advanced"):
        assert expected in text_blob, (
            f"Missing section header containing {expected!r}; "
            f"visible headers: {visible_titles}"
        )
