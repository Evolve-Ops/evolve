"""Browser regression: AI Optimization must load its OWN bot list.

Operator regression report 2026-08-24 (evolve-stable-528): the AI
Optimization page rendered ONLY the POD tab — no per-bot tabs — so there
was no way to reach a bot's custom tier editor. The per-bot data was
fine; the page's tab bar simply had nothing to iterate.

Cause: ``loadAiOptimization()`` renders its tab bar from
``orderedBotIds(_networkData.bots)`` but was never a writer of that
global — it free-rode on the boot ``load() -> loadNetwork()`` fetch (and
on whichever sibling page the operator happened to visit first:
settings.js, pod-config.js, self-improvement.js are the only other
writers). Any boot where that ONE ``/api/network`` request fails leaves
``_networkData`` holding api()'s error object, and the page renders
POD-only with no retry — permanently, until some other page refills the
global. That is exactly what the operator hit, and why visiting
Settings first was the workaround.

The tests below pin the fix: the page ensures its own data, so the tab
bar does not depend on visit order or on a healthy boot fetch.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)


# Three bots, each with role/user/port so none is filtered out as a
# scaffold-only phantom by isScaffoldOnlyBot() (EVO-SEP-S4).
_BOTS = {
    "evo": {"role": "primary", "user": "evo", "port": 8790, "display_name": "Evo"},
    "team-bot-a": {
        "role": "member", "user": "team-bot-a", "port": 8791,
        "display_name": "Ada",
    },
    "team-bot-b": {
        "role": "member", "user": "team-bot-b", "port": 8792,
        "display_name": "Bex",
    },
}

# "" is the POD tab sentinel; primary first, then alpha by DISPLAY label.
_EXPECTED_TABS = ["", "evo", "team-bot-a", "team-bot-b"]

# Pre-existing baseline JS errors during SPA boot (page-load redirect
# stubs calling nav() before it's defined). Same allow-list shape as
# test_smoke.py / test_bot_config_ux.py.
_BASELINE_PAGEERROR_SUBSTRINGS: tuple[str, ...] = (
    "nav is not defined",
    "Can't find variable: nav",
)


def _seed_bots(network_path) -> None:
    """Put three real bots in the network.json the server reads."""
    data = json.loads(network_path.read_text())
    data["bots"] = _BOTS
    data["members"] = list(_BOTS)
    data["primary"] = "evo"
    network_path.write_text(json.dumps(data, indent=2))


# The SPA registers a service worker (`/sw.js`, network-first). Once it
# controls the page, WebKit routes fetches through it, and Playwright's
# `page.route` does NOT intercept service-worker-originated requests — so a
# route stub silently stops firing partway through boot (verified: the
# request reaches the server, the handler never runs; chromium/firefox
# intercept fine). Both stubs below therefore shim `window.fetch` from an
# init script instead. That is also the truer seam: what these tests pin is
# how the page reacts to what `api()` hands back.
_BOT_CONFIG = {
    "customTiers": True,
    "catalog": ["anthropic/model-a", "anthropic/model-b"],
    "tiers": {
        "tier1": {"models": ["anthropic/model-a"]},
        "tier2": {"models": ["anthropic/model-b"]},
        "tier3": {"models": ["anthropic/model-b"]},
    },
    "roles": {},
    "fallbackMode": "static",
    "tierCascade": ["tier2", "tier3", "tier1"],
}


def _stub_bot_config(page) -> None:
    """Answer ``/api/admin/config/<bot>`` with a CUSTOM-tier bot.

    ``customTiers: true`` is what routes _aiRenderTiers() to the editable
    custom editor (``#ai-tiers-save-btn``) rather than the read-only "use
    pod defaults" view — the surface the operator could not reach.

    The pattern deliberately excludes ``/api/admin/config/pod/models`` (a
    second segment), which the POD view fetches and which must keep
    reaching the real server.
    """
    page.add_init_script(
        r"""
        (() => {
          const real = window.fetch.bind(window);
          const PAYLOAD = %s;
          window.fetch = function (input, init) {
            const url = (typeof input === 'string')
              ? input : ((input && input.url) || '');
            if (/\/api\/admin\/config\/[^/?]+(\?|$)/.test(url)) {
              return Promise.resolve(new Response(JSON.stringify(PAYLOAD), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
              }));
            }
            return real(input, init);
          };
        })();
        """
        % json.dumps(_BOT_CONFIG)
    )


def _fail_first_network_fetch(page) -> None:
    """Make the FIRST ``/api/network`` fetch fail, as a dropped request does.

    A rejected `fetch` is exactly what `api()` turns into its
    ``{error, network_error: true}`` payload — the state the boot
    ``loadNetwork()`` leaves `_networkData` in when that one request
    doesn't land. Counts every attempt on `window` so the test can assert
    the page went back for its own copy.
    """
    page.add_init_script(
        """
        (() => {
          const real = window.fetch.bind(window);
          window.__evolveNetworkFetches = 0;
          window.fetch = function (input, init) {
            const url = (typeof input === 'string')
              ? input : ((input && input.url) || '');
            if (url.indexOf('/api/network') !== -1) {
              window.__evolveNetworkFetches += 1;
              if (window.__evolveNetworkFetches === 1) {
                return Promise.reject(new TypeError('Load failed'));
              }
            }
            return real(input, init);
          };
        })();
        """
    )


def _direct_nav_to_ai_optimization(page, admin_server) -> None:
    """Land on AI Optimization on a FRESH load, not via another page.

    localStorage restore is the SPA's own direct-navigation path
    (_restoreNav), so this is the operator's "open the admin UI on the
    page I was last on" flow — no sibling page runs first to populate
    the _networkData global.
    """
    page.add_init_script(
        "localStorage.setItem('evolve_active_page','ai-optimization')"
    )
    page.goto(admin_server, wait_until="load")
    page.wait_for_selector("#page-ai-optimization.active", timeout=15000)


def _new_errors_only(errors: list[str]) -> list[str]:
    return [
        e
        for e in errors
        if not any(sub in e for sub in _BASELINE_PAGEERROR_SUBSTRINGS)
    ]


def _tab_ids(page) -> list[str]:
    return page.eval_on_selector_all(
        "#ai-bot-tabs .subtab", "els => els.map(e => e.dataset.bot)"
    )


def test_bot_tabs_render_on_direct_nav(page, admin_server, network_path):
    """Healthy boot: POD + one tab per bot, in orderedBotIds order."""
    _seed_bots(network_path)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    _direct_nav_to_ai_optimization(page, admin_server)

    page.wait_for_function(
        "() => document.querySelectorAll('#ai-bot-tabs .subtab').length > 1",
        timeout=15000,
    )
    assert _tab_ids(page) == _EXPECTED_TABS
    assert _new_errors_only(errors) == []


def test_bot_tabs_survive_a_failed_boot_network_fetch(
    page, admin_server, network_path
):
    """THE regression: the boot ``/api/network`` fails, tabs still render.

    Before the fix this rendered ``['']`` — the POD tab alone — because
    the page read a ``_networkData`` global that the failed boot fetch
    had left as an error payload, and never retried.
    """
    _seed_bots(network_path)
    _fail_first_network_fetch(page)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    _direct_nav_to_ai_optimization(page, admin_server)

    page.wait_for_function(
        "() => document.querySelectorAll('#ai-bot-tabs .subtab').length > 1",
        timeout=15000,
    )
    assert _tab_ids(page) == _EXPECTED_TABS, (
        "AI Optimization rendered POD-only after a failed boot /api/network — "
        "the page is still free-riding on another page's fetch"
    )
    assert page.evaluate("() => window.__evolveNetworkFetches") >= 2, (
        "the page never re-fetched /api/network for itself"
    )
    assert _new_errors_only(errors) == []


def test_bot_tab_opens_the_custom_tier_editor(page, admin_server, network_path):
    """Clicking a bot tab reaches that bot's custom tier editor.

    The operator-visible point of the tab bar: per-bot model config must
    be reachable from this page alone.
    """
    _seed_bots(network_path)
    _stub_bot_config(page)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    _direct_nav_to_ai_optimization(page, admin_server)
    page.wait_for_selector(
        '#ai-bot-tabs .subtab[data-bot="team-bot-a"]', timeout=15000
    )

    # Drive the tab's own handler (nav clicks race the sidebar overlay on
    # the default test viewport — same reason test_smoke.py calls nav()).
    page.evaluate("() => window.aiSwitchBot('team-bot-a')")

    page.wait_for_selector("#ai-tiers-save-btn", timeout=15000)
    assert page.locator("#ai-bot-view").is_visible()
    # text_content, not inner_text — the label is CSS-uppercased.
    assert page.locator("#ai-tiers-bot-label").text_content() == "(Ada)"
    assert _new_errors_only(errors) == []
