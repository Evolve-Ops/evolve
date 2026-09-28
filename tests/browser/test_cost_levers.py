"""Browser smoke for the Usage page cost-levers chip painter + Dashboard
Settings deep-link.

Phase 3 of the 2026-06 cost-cap normalization removed the "Edit cost
levers" modal — per-bot cost knobs (cache TTL, per-session cap, daily
hard cap) now live in the canonical "Cost & caps" card on Settings ->
Bots, edited via POST /api/arbiter/bot-setup. The bot-tile chips on the
Cost Optimization page still paint state, but read from the same
/api/arbiter/bot-setup endpoint, and the "Configure ->" link jumps to
the Settings card instead of opening a modal.

What's tested here:

1. ``_cmPaintLevers`` — given a bot-setup payload, the chip DOM updates
   to show the canonical state. The two visible chips post-Phase-3 are
   ``cm-lever-budget-<bot>`` (per-session cap) and
   ``cm-lever-dailycap-<bot>`` (daily hard cap). TTL is no longer on
   the tile (set-and-forget knob).

2. Dashboard tile Actions menu -> "Settings" deep-link still activates
   the Settings page, the Bots subtab, AND calls
   ``switchConfigBot(<bot>)``.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)


_TEST_BOT_ID = "team-bot-a"


def test_cost_levers_chip_paints_current_value(page, admin_server: str) -> None:
    """Calling _cmPaintLevers directly updates the tile chip text.

    Phase 7 contract (2026-06-06): _cmPaintLevers paints four chips —
    tier_downgrade, L1 breaker (daily hard cap), L2 breaker, per-session.
    The TTL chip was retired in Phase 3.
    """
    page.add_init_script(
        "Object.defineProperty(navigator, 'serviceWorker', {value: undefined});"
    )

    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof _cmPaintLevers === 'function'",
        timeout=10_000,
    )
    page.evaluate(
        f"""
        () => {{
          const wrap = document.createElement('div');
          wrap.innerHTML = `
            <span id="cm-lever-tier-{_TEST_BOT_ID}">tier: —</span>
            <span id="cm-lever-dailycap-{_TEST_BOT_ID}">L1: —</span>
            <span id="cm-lever-l2-{_TEST_BOT_ID}">L2: —</span>
            <span id="cm-lever-budget-{_TEST_BOT_ID}">session: —</span>
          `;
          document.body.appendChild(wrap);
          _cmPaintLevers({json.dumps(_TEST_BOT_ID)}, {{
            ok: true,
            tier_downgrade_usd: 8,
            daily_hard_usd: 50,
            effective_daily_hard_usd: 50,
            l2_breaker_usd: 100,
            session_cost_cap_usd: 2.5,
          }});
        }}
        """
    )
    tier_text = page.locator(f"#cm-lever-tier-{_TEST_BOT_ID}").inner_text()
    daily_text = page.locator(f"#cm-lever-dailycap-{_TEST_BOT_ID}").inner_text()
    l2_text = page.locator(f"#cm-lever-l2-{_TEST_BOT_ID}").inner_text()
    budget_text = page.locator(f"#cm-lever-budget-{_TEST_BOT_ID}").inner_text()
    assert "$8" in tier_text, tier_text
    assert "$50" in daily_text, daily_text
    assert "$100" in l2_text, l2_text
    assert "$2.50" in budget_text, budget_text


def test_cost_levers_chip_falls_back_to_effective_daily(
    page, admin_server: str,
) -> None:
    """When ``daily_hard_usd`` is null but ``effective_daily_hard_usd``
    is set (derived from monthly_cap_usd), the L1 chip still shows
    the effective value — that's what'll actually trip the breaker."""
    page.add_init_script(
        "Object.defineProperty(navigator, 'serviceWorker', {value: undefined});"
    )
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof _cmPaintLevers === 'function'",
        timeout=10_000,
    )
    page.evaluate(
        f"""
        () => {{
          const wrap = document.createElement('div');
          wrap.innerHTML = `
            <span id="cm-lever-tier-{_TEST_BOT_ID}">tier: —</span>
            <span id="cm-lever-dailycap-{_TEST_BOT_ID}">L1: —</span>
            <span id="cm-lever-l2-{_TEST_BOT_ID}">L2: —</span>
            <span id="cm-lever-budget-{_TEST_BOT_ID}">session: —</span>
          `;
          document.body.appendChild(wrap);
          _cmPaintLevers({json.dumps(_TEST_BOT_ID)}, {{
            ok: true,
            tier_downgrade_usd: null,
            session_cost_cap_usd: null,
            daily_hard_usd: null,
            effective_daily_hard_usd: 8.33,
            l2_breaker_usd: null,
          }});
        }}
        """
    )
    tier_text = page.locator(f"#cm-lever-tier-{_TEST_BOT_ID}").inner_text()
    budget_text = page.locator(f"#cm-lever-budget-{_TEST_BOT_ID}").inner_text()
    daily_text = page.locator(f"#cm-lever-dailycap-{_TEST_BOT_ID}").inner_text()
    l2_text = page.locator(f"#cm-lever-l2-{_TEST_BOT_ID}").inner_text()
    assert "off" in tier_text.lower(), tier_text
    assert "none" in budget_text.lower(), budget_text
    assert "$8.33" in daily_text, daily_text
    assert "off" in l2_text.lower(), l2_text


def test_cm_go_to_cost_caps_navigates_to_cost_optimization(
    page, admin_server: str,
) -> None:
    """The bot-tile "Configure ->" button calls _cmGoToCostCaps, which
    activates the Cost Optimization page and selects the bot's tab on
    that page. PR D retargeted this deep link from Settings to Cost
    Optimization because the Settings Cost & caps card was reduced to
    a deep link itself in PR C — sending the operator to Settings just
    to bounce them right back here was an extra hop. _cmGoToCostCaps
    delegates to _jumpToCostOptForBot (the helper PR C introduced).
    """
    page.add_init_script(
        "Object.defineProperty(navigator, 'serviceWorker', {value: undefined});"
    )
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof _cmGoToCostCaps === 'function'",
        timeout=10_000,
    )

    # Stub _cmSelectBot so we can observe the bot-tab selection call
    # without racing against the network-data load that drives the real
    # tab rendering. The helper retries until #cm-bot-tabs has a subtab
    # for our bot id — in this test fixture the bot won't be in the
    # network's bots list, so we inject a stub subtab to break that
    # retry loop and exercise the success path.
    captured = page.evaluate(
        f"""
        async () => {{
          const calls = [];
          const origSel = window._cmSelectBot;
          window._cmSelectBot = (b) => {{ calls.push(b); }};
          // Make sure the cm-bot-tabs container + a matching subtab exist
          // so _jumpToCostOptForBot's retry loop finds the tab and calls
          // through to _cmSelectBot rather than timing out silently.
          const tabsEl = document.getElementById('cm-bot-tabs');
          if (tabsEl) {{
            const stub = document.createElement('div');
            stub.className = 'subtab';
            stub.setAttribute('data-bot', {json.dumps(_TEST_BOT_ID)});
            tabsEl.appendChild(stub);
          }}
          try {{
            _cmGoToCostCaps({json.dumps(_TEST_BOT_ID)});
            // The helper schedules _cmSelectBot via up to 10 retries at
            // 50ms intervals (~500ms worst case). 1500ms gives webkit's
            // slower event loop on CI plenty of headroom — 600ms was
            // marginal and produced flake on webkit specifically.
            await new Promise(r => setTimeout(r, 1500));
          }} finally {{
            window._cmSelectBot = origSel;
          }}
          return {{
            calls,
            page_cost_active: document.getElementById('page-cost-measures').classList.contains('active'),
            page_settings_active: document.getElementById('page-settings').classList.contains('active'),
          }};
        }}
        """
    )
    assert captured["page_cost_active"], (
        "Cost Optimization page didn't activate after _cmGoToCostCaps — the "
        "deep-link didn't follow nav([data-page=cost-measures])."
    )
    assert not captured["page_settings_active"], (
        "Settings page activated unexpectedly — PR D retargeted this deep "
        "link to Cost Optimization, not Settings."
    )
    assert _TEST_BOT_ID in captured["calls"], (
        f"_cmSelectBot was not called with the right bot id. "
        f"Calls: {captured['calls']!r}"
    )


def test_dashboard_actions_settings_deeplink_lands_on_bot_config(page, admin_server: str) -> None:
    """_podMenuOpenBotConfig activates the Pod Config / Bot subtab
    AND switches the bot selector to the requested bot.

    The full Dashboard path would require a bot tile to be rendered
    (which needs a populated bots dict on /api/status). We instead
    drive the helper directly — it's the load-bearing piece of the
    deep-link contract. If a future refactor moves the bot tab or
    renames the subtab, this assertion catches it.
    """
    page.add_init_script(
        "Object.defineProperty(navigator, 'serviceWorker', {value: undefined});"
    )

    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof _podMenuOpenBotConfig === 'function'",
        timeout=10_000,
    )

    # Stub switchConfigBot so we can observe the call without needing
    # a real bot wired through the full loadConfigBot fetch fan-out.
    # Also intercept scrollIntoView so we can assert the deep-link
    # lands at the top of the bot-specific settings (the bot tab strip),
    # not midway down at any one card.
    captured = page.evaluate(
        f"""
        async () => {{
          const calls = [];
          const orig = window.switchConfigBot;
          window.switchConfigBot = (b) => {{ calls.push(b); }};
          const scrollTargets = [];
          const origScroll = Element.prototype.scrollIntoView;
          Element.prototype.scrollIntoView = function(...args) {{
            scrollTargets.push(this.id || '<no-id>');
          }};
          try {{
            _podMenuOpenBotConfig({json.dumps(_TEST_BOT_ID)});
            // _podMenuOpenBotConfig schedules switchConfigBot in a 50ms
            // setTimeout so the bot tab strip has time to render. Wait
            // long enough to observe the call.
            await new Promise(r => setTimeout(r, 200));
          }} finally {{
            window.switchConfigBot = orig;
            Element.prototype.scrollIntoView = origScroll;
          }}
          return {{
            calls,
            scrollTargets,
            page_settings_active: document.getElementById('page-settings').classList.contains('active'),
            // Post 2026-06-01 restructure: Bots is a top-level Settings
            // subtab (sibling of Modules / Pod Config), not nested
            // under Pod Config. The deep-link must activate
            // #settings-bots; #settings-pod-config should NOT be the
            // active subtab.
            settings_bots_active: document.getElementById('settings-bots').classList.contains('active'),
          }};
        }}
        """
    )
    assert captured["page_settings_active"], (
        "Settings page didn't activate after _podMenuOpenBotConfig — "
        "the deep-link landed on the wrong page."
    )
    assert captured["settings_bots_active"], (
        "Settings → Bots subtab didn't activate — operator would land "
        "on Modules or Pod Config instead of the per-bot view."
    )
    assert _TEST_BOT_ID in captured["calls"], (
        f"switchConfigBot was not called with the right bot id. "
        f"Calls: {captured['calls']!r}"
    )
    # The deep-link must land at the top of the bot-specific settings —
    # the bot tab strip — not midway down at any individual card. If a
    # future refactor changes the anchor (e.g. scrolls to Customizations,
    # Continuity Engine, or any other card mount), this assertion fires
    # so the bug from the team-bot-a Settings deep-link feedback doesn't
    # silently regress.
    assert "config-bot-tabs" in captured["scrollTargets"], (
        f"Settings deep-link must scroll to #config-bot-tabs (the bot "
        f"picker menu — topmost stable anchor of bot-specific settings). "
        f"Got scroll targets: {captured['scrollTargets']!r}"
    )
    assert "botcfg-customizations" not in captured["scrollTargets"], (
        "Settings deep-link must NOT scroll to #botcfg-customizations — "
        "that lands the operator midway down the page, past Bot Setup, "
        "Model Config, Compaction, etc."
    )
