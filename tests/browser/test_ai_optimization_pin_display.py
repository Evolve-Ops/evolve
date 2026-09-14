"""Browser regression: with auto-latest ON, the tier editor must say which
pinned version each consolidated row stands on — and the picker must say where
an already-added id lives.

Operator report 2026-08-24 (screenshot): the Standard tier read
``claude-sonnet · latest`` while the "Add a model" picker greyed
``claude-sonnet-4-6 (already added)``. Both statements were true — the saved
chain concretely contains 4-6, and ``_aiFamilyGroups`` consolidates the pinned
versions of one model line into a single row — but nothing on screen connected
them, so the page read as contradicting itself: the operator wanted 4-6, was
told it was already added, and could not find a row bearing its name.

These tests drive the real SPA against a stubbed API and pin the two halves of
the fix, in BOTH themes (there is no CI gate for theme parity):

  1. the consolidated row's own label names the standing version —
     ``claude-sonnet · latest (now 4-6)`` — not only a tooltip;
  2. the picker's greyed entry reads
     ``★ claude-sonnet-4-6 (in Standard as claude-sonnet · latest)``.

Auto-latest OFF is covered too: the rows are plain pinned ids there, and the
note degrades to "already in Standard" rather than claiming a consolidation
that is not on screen.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)


_BOTS = {
    "evo": {"role": "primary", "user": "evo", "port": 8790, "display_name": "Evo"},
    "team-bot-a": {
        "role": "member", "user": "team-bot-a", "port": 8791,
        "display_name": "Ada",
    },
}

# Two pinned versions of ONE model line, newest first — the shape that
# consolidates to a single row. `claude-opus-4-8` is a second family so the
# grouping is exercised, not just a single-group passthrough.
_SONNET_NEW = "anthropic/claude-sonnet-4-6"
_SONNET_OLD = "anthropic/claude-sonnet-4-5"
_OPUS = "anthropic/claude-opus-4-8"

_BOT_CONFIG = {
    "customTiers": True,
    "catalog": [_SONNET_NEW, _SONNET_OLD, _OPUS],
    "tiers": {
        "tier2": {"models": [_SONNET_NEW, _SONNET_OLD]},   # Standard
        "tier1": {"models": [_OPUS]},                       # Power
        "tier3": {"models": [_SONNET_OLD]},                 # Fast
        "tier0": {"models": []},                            # Judge
        "max": {"models": []},
    },
    # `defaultModels` is the picker's RECOMMENDED set. 4-6 being recommended
    # AND already present is exactly the combination that renders the greyed
    # "(already added)" option the operator hit.
    "roles": {
        "standard": {
            "available": True,
            "models": [_SONNET_NEW, _SONNET_OLD],
            "defaultModels": [_SONNET_NEW],
        },
        "power": {"available": True, "models": [_OPUS], "defaultModels": [_OPUS]},
        "fast": {"available": True, "models": [_SONNET_OLD], "defaultModels": []},
        "max": {"available": True, "models": [], "defaultModels": []},
        "judge": {"available": True, "models": [], "defaultModels": []},
    },
    "fallbackMode": "static",
    "tierCascade": ["tier2", "tier3", "tier1"],
    "keyStatus": {"anthropic": True},
}

# The server-computed qualified-id → family-stem map the SPA groups by
# (model_discovery._family_of). No client-side version parsing.
_FAMILIES = {
    _SONNET_NEW: "claude-sonnet",
    _SONNET_OLD: "claude-sonnet",
    _OPUS: "claude-opus",
}

# Pre-existing baseline JS errors during SPA boot (page-load redirect stubs
# calling nav() before it's defined) — same allow-list as the sibling suites.
_BASELINE_PAGEERROR_SUBSTRINGS: tuple[str, ...] = (
    "nav is not defined",
    "Can't find variable: nav",
)


def _seed_bots(network_path) -> None:
    data = json.loads(network_path.read_text())
    data["bots"] = _BOTS
    data["members"] = list(_BOTS)
    data["primary"] = "evo"
    network_path.write_text(json.dumps(data, indent=2))


def _stub_api(page, *, auto_enabled: bool) -> None:
    """Answer the two endpoints the tier editor reads, from an init script.

    `page.route` does NOT intercept service-worker-originated requests and the
    SPA registers one, so the stub shims `window.fetch` instead (see
    test_ai_optimization_tabs.py for the full reasoning). That is also the
    truer seam: what these tests pin is how the page renders what `api()`
    hands back.
    """
    page.add_init_script(
        r"""
        (() => {
          const real = window.fetch.bind(window);
          const CONFIG = %s;
          const AUTO = %s;
          const json = (body) => Promise.resolve(new Response(
            JSON.stringify(body),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ));
          window.fetch = function (input, init) {
            const url = (typeof input === 'string')
              ? input : ((input && input.url) || '');
            if (url.indexOf('/api/models/auto-upgrade') !== -1) return json(AUTO);
            // Single trailing segment only — /api/admin/config/pod/models is a
            // different route and must keep reaching the real server.
            if (/\/api\/admin\/config\/[^/?]+(\?|$)/.test(url)) return json(CONFIG);
            return real(input, init);
          };
        })();
        """
        % (
            json.dumps(_BOT_CONFIG),
            json.dumps({
                "auto_upgrade": {"enabled": auto_enabled},
                "custom_tiers": True,
                "families": _FAMILIES,
                "auto_upgrade_governed": [],
                "auto_upgrade_excluded": [],
            }),
        )
    )


def _open_bot_tier_editor(page, admin_server) -> None:
    page.add_init_script(
        "localStorage.setItem('evolve_active_page','ai-optimization')"
    )
    page.goto(admin_server, wait_until="load")
    page.wait_for_selector("#page-ai-optimization.active", timeout=15000)
    page.wait_for_selector(
        '#ai-bot-tabs .subtab[data-bot="team-bot-a"]', timeout=15000
    )
    # Drive the tab's own handler — nav clicks race the sidebar overlay on the
    # default test viewport (same reason test_smoke.py calls nav()).
    page.evaluate("() => window.aiSwitchBot('team-bot-a')")
    page.wait_for_selector("#ai-tiers-save-btn", timeout=15000)


def _new_errors_only(errors: list[str]) -> list[str]:
    return [
        e for e in errors
        if not any(sub in e for sub in _BASELINE_PAGEERROR_SUBSTRINGS)
    ]


def _theme(page) -> str:
    return page.evaluate(
        "() => document.documentElement.getAttribute('data-theme')"
    )


def _flip_theme(page) -> str:
    before = _theme(page)
    page.evaluate("() => document.querySelector('.theme-toggle').click()")
    page.wait_for_function(
        "prev => document.documentElement.getAttribute('data-theme') !== prev",
        arg=before,
        timeout=5000,
    )
    return _theme(page)


def _tier_panel_text(page) -> str:
    return page.locator("#ai-tiers-panel").inner_text()


def _option_labels(page) -> list[str]:
    return page.eval_on_selector_all(
        "#ai-tiers-panel option", "els => els.map(e => e.textContent)"
    )


def _standard_chip(page):
    """The consolidated Standard-tier chip for the claude-sonnet line.

    Scoped by the row's own × handler (``…,'tier2',…``) — the model line also
    appears in Fast, standing on a different pin, so "first chip mentioning
    claude-sonnet" would grab the wrong row.
    """
    return page.locator(
        '#ai-tiers-panel .ai-tier-model-row:has(.ai-chip-x[onclick*="tier2"])'
        " .ai-model-provider-chip"
    ).first


def _family_rows(page) -> list[dict]:
    """Every consolidated row, tagged with the tier store key it belongs to.

    The key comes from the row's own × handler
    (``_aiTierFamilyRemove('bot','tier2',0)``) — the only thing in the DOM that
    ties a row to its tier, and the same string the mutation rides.
    """
    return page.evaluate(
        r"""() => Array.from(
            document.querySelectorAll('#ai-tiers-panel .ai-tier-model-row')
          ).map(row => {
            const x = row.querySelector('.ai-chip-x');
            const oc = x ? (x.getAttribute('onclick') || '') : '';
            const m = /_aiTierFamilyRemove\('[^']*','([^']*)'/.exec(oc);
            return { tier: m ? m[1] : null, text: row.innerText.trim() };
          })"""
    )


def _rows_in(page, tier: str) -> list[str]:
    return [r["text"] for r in _family_rows(page) if r["tier"] == tier]


def test_family_row_names_the_version_it_stands_on(
    page, admin_server, network_path
):
    """THE regression, half one: the row says "(now 4-6)".

    Before the fix this row read "claude-sonnet · latest" with the pinned ids
    reachable only by hovering the few pixels around the chip — and the chip's
    own title ("Anthropic") shadowed even that.
    """
    _seed_bots(network_path)
    _stub_api(page, auto_enabled=True)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    _open_bot_tier_editor(page, admin_server)

    # Standard: two pinned sonnet versions collapse to ONE row, and that row
    # names the head — the pin the resolver actually routes to.
    standard = _rows_in(page, "tier2")
    assert len(standard) == 1, f"Standard should consolidate to one row; got {standard}"
    assert "claude-sonnet · latest (now 4-6)" in standard[0], (
        "the consolidated row must name the pinned version it stands on; got:\n"
        + standard[0]
    )

    # The SAME model line in a different tier stands on ITS OWN pin. A label
    # that read "latest" for both would hide a real difference in what routes.
    assert any("claude-sonnet · latest (now 4-5)" in r for r in _rows_in(page, "tier3")), (
        "Fast pins 4-5 and must say so; got " + repr(_rows_in(page, "tier3"))
    )
    assert any("claude-opus · latest (now 4-8)" in r for r in _rows_in(page, "tier1"))

    # The full disclosure is reachable ON the chip, not only on its wrapper.
    title = _standard_chip(page).get_attribute("title")
    assert "Right now this line is claude-sonnet-4-6" in (title or "")
    assert "Kept behind it as fallback: claude-sonnet-4-5" in (title or "")

    assert _new_errors_only(errors) == []


def test_picker_says_where_the_already_added_id_lives(
    page, admin_server, network_path
):
    """THE regression, half two: the greyed entry names its row.

    "(already added)" against a row that never shows the id is what the
    operator read as a contradiction.
    """
    _seed_bots(network_path)
    _stub_api(page, auto_enabled=True)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    _open_bot_tier_editor(page, admin_server)

    labels = _option_labels(page)
    assert "★ claude-sonnet-4-6 (in Standard as claude-sonnet · latest)" in labels, (
        "picker must name the tier AND the consolidated row; got:\n"
        + "\n".join(labels)
    )
    assert not any("(already added)" in lbl for lbl in labels), (
        "the bare '(already added)' must not survive under auto-latest"
    )
    assert _new_errors_only(errors) == []


def test_auto_off_keeps_pinned_rows_and_a_plain_note(
    page, admin_server, network_path
):
    """With the policy OFF nothing consolidates, so the note must not claim it.

    The honest-display copy is derived from the live policy, not hardcoded: a
    note that said "as claude-sonnet · latest" here would point at a row that
    does not exist.
    """
    _seed_bots(network_path)
    _stub_api(page, auto_enabled=False)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    _open_bot_tier_editor(page, admin_server)

    text = _tier_panel_text(page)
    assert "· latest" not in text, "no consolidation when the policy is off"
    assert "claude-sonnet-4-6" in text and "claude-sonnet-4-5" in text

    labels = _option_labels(page)
    assert "★ claude-sonnet-4-6 (already in Standard)" in labels, (
        "got:\n" + "\n".join(labels)
    )
    assert _new_errors_only(errors) == []


def test_honest_display_renders_in_both_themes(page, admin_server, network_path):
    """Theme parity — there is no CI gate for it (CLAUDE.md admin-UI rules).

    Both the row label and the picker note must survive the toggle, and the
    chip must be painted from a theme-paired token rather than one hardcoded
    color that happens to be legible in dark.
    """
    _seed_bots(network_path)
    _stub_api(page, auto_enabled=True)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    _open_bot_tier_editor(page, admin_server)

    seen: dict[str, str] = {}
    for _ in range(2):
        theme = _theme(page)
        assert theme in ("dark", "light"), f"unexpected data-theme {theme!r}"
        assert "claude-sonnet · latest (now 4-6)" in _tier_panel_text(page), (
            f"row label missing in {theme} theme"
        )
        assert any(
            "(in Standard as claude-sonnet · latest)" in lbl
            for lbl in _option_labels(page)
        ), f"picker note missing in {theme} theme"
        chip = _standard_chip(page)
        assert chip.is_visible(), f"consolidated chip not visible in {theme} theme"
        seen[theme] = chip.evaluate(
            "el => getComputedStyle(el).color"
        )
        _flip_theme(page)

    assert set(seen) == {"dark", "light"}, f"only saw themes {sorted(seen)}"
    assert seen["dark"] != seen["light"], (
        "the consolidated chip renders the SAME color in both themes "
        f"({seen['dark']}) — it is not reading a theme-paired token"
    )
    assert _new_errors_only(errors) == []
