"""tests/test_retire_primary_ui_choice.py — pin the primary-UI retire.

Every Evolve install ships with a dedicated ``evo`` primary bot
(2026-05-20 direction). Three operator-facing surfaces for changing
the primary bot were retired in that PR:

  1. The setup wizard's "dedicated vs. existing" prompt
     (``setup_wizard.py`` around line 2556-2607)
  2. The "Primary Bot" card on Settings → Pod Config
     (``index.html`` around line 3412)
  3. The ``evolve-admin config set-primary`` CLI subcommand
     (``cli.py:config_set_primary``) — kept as a no-op + deprecation
     warning for one release, then removed.

These tests pin those retirements so a future contributor doesn't
re-introduce the operator-facing knob. ``network.primary`` (the
JSON field) + ``role: "primary"`` (the bot record flag) are still
there in the codebase — only the user-facing chooser is gone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_SETUP_WIZARD = _ADMIN_PKG / "evolve_admin" / "setup_wizard.py"
_INDEX_HTML = _ADMIN_PKG / "evolve_admin" / "web" / "index.html"
_SETTINGS_JS = _ADMIN_PKG / "evolve_admin" / "web" / "static" / "js" / "pages" / "settings.js"
_CLI = _ADMIN_PKG / "evolve_admin" / "cli.py"


def _read_index_and_settings(encoding: str = "utf-8") -> str:
    # populate_config + save_primary moved to pages/settings.js (Phase 3u).
    return _INDEX_HTML.read_text(encoding=encoding) + "\n" + _SETTINGS_JS.read_text(encoding=encoding)


# ── Wizard prompt removal ────────────────────────────────────────────────────


def test_wizard_no_longer_prompts_primary_dedicated_vs_existing():
    """The setup wizard must NOT ask the operator to choose between
    'dedicated' and 'existing' primary bot. That prompt was the
    operator-confusion surface — pre-this-PR it dispatched on a
    string like 'd' vs 'e'."""
    src = _SETUP_WIZARD.read_text(encoding="utf-8")
    # The old prompt string had unique phrasing — match it
    # specifically so a similar-sounding prompt elsewhere doesn't
    # false-positive.
    assert "Primary bot setup [d]edicated/[e]xisting" not in src, (
        "the dedicated-vs-existing prompt is back. Every Evolve "
        "install should now provision a dedicated 'evo' primary; "
        "asking the operator to choose was the source of confusion."
    )
    # And the secondary prompt that fell out of the 'e' branch:
    assert "Which existing bot is primary?" not in src, (
        "the 'pick an existing bot to flip to primary' prompt is "
        "back. Same reasoning — the choice is no longer offered."
    )


def test_wizard_still_sets_primary_mode_to_dedicated_for_downstream():
    """The downstream provisioning code at lines ~2800 branches on
    ``primary_mode``. The wizard must continue to set both
    ``primary_bot_id_choice`` AND ``primary_mode`` so existing-mode
    deploys (if any survive in the wild) keep working until cleanup
    in a future PR."""
    src = _SETUP_WIZARD.read_text(encoding="utf-8")
    assert 'primary_bot_id_choice: str = "evo"' in src, (
        "the wizard no longer initializes primary_bot_id_choice — "
        "downstream code that consumes this variable will break."
    )
    assert 'primary_mode: str = "dedicated"' in src, (
        "the wizard no longer initializes primary_mode — the "
        "downstream branch at lines ~2800 expects this string."
    )


# ── Settings card removal ────────────────────────────────────────────────────


def test_settings_card_for_primary_bot_removed():
    """The 'Primary Bot' card on Settings → Pod Config must be gone.
    The operator's day-to-day surface should not expose this knob."""
    src = _read_index_and_settings()
    # The card had a unique title cell — check that's gone.
    assert '<div class="card-title">Primary Bot<' not in src, (
        "the Primary Bot card was added back to Settings → Pod "
        "Config. Every install now uses dedicated 'evo'; the "
        "operator-facing chooser is intentionally hidden."
    )
    # The DOM id on the dropdown is also unique — pin it too.
    assert 'id="cfg-primary"' not in src, (
        "the cfg-primary <select> dropdown is back in the HTML. "
        "It was retired along with the Primary Bot card."
    )


def test_settings_save_button_for_primary_removed():
    """The Set Primary button (the action affordance on the card)
    must be gone. Catches the case where someone deletes the dropdown
    but leaves a now-broken button."""
    src = _read_index_and_settings()
    assert 'onclick="savePrimary()"' not in src, (
        "the 'Set Primary' button is back in the HTML, wired to "
        "savePrimary(). The card was retired; the button should be "
        "too."
    )


def test_save_primary_function_is_a_noop_with_warning():
    """``savePrimary()`` is retained as a no-op stub (in case a
    third-party automation or a stale browser tab still calls it),
    but it must not actually do anything except warn. Catches a
    regression where the function body grows back to a real network
    POST."""
    src = _read_index_and_settings()
    m = re.search(
        r"async\s+function\s+savePrimary\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        src,
    )
    assert m, "savePrimary() definition missing entirely"
    body = m.group(1)
    # Must contain the deprecation warning marker
    assert "retired" in body.lower() or "deprecated" in body.lower(), (
        "savePrimary's body no longer mentions retirement/deprecation. "
        "Either it grew back to a real handler (regression) or the "
        "warning was removed (also regression — silent breakage)."
    )
    # Must NOT POST to the config endpoint
    assert "/api/config" not in body, (
        "savePrimary is POSTing to /api/config again. The function "
        "is supposed to be a no-op stub now."
    )
    assert "api(" not in body, (
        "savePrimary is making an api() call. It should be a no-op."
    )


def test_populate_config_no_longer_fills_primary_dropdown():
    """``populateConfig()`` previously walked the bots list to fill
    the #cfg-primary dropdown. With the card gone, that loop should
    be removed too. Forgetting to clean this leaves a JS reference
    to a missing DOM element."""
    src = _read_index_and_settings()
    m = re.search(
        r"function\s+populateConfig\s*\(\s*\)\s*\{([\s\S]*?)\n\}",
        src,
    )
    assert m, "populateConfig() definition missing"
    body = m.group(1)
    # The primarySel local + the option-building loop should be gone.
    assert "cfg-primary" not in body, (
        "populateConfig still references cfg-primary — but the DOM "
        "element is gone. This is dead code at best, a reference "
        "to a missing element at worst."
    )


# ── CLI deprecation ──────────────────────────────────────────────────────────


def test_cli_set_primary_command_still_exists_for_backcompat():
    """The CLI subcommand stays callable so existing scripts don't
    break — it just warns. Verifies the @click.command registration
    is still there."""
    src = _CLI.read_text(encoding="utf-8")
    assert '@config.command("set-primary")' in src, (
        "the set-primary CLI command was removed entirely. The plan "
        "was deprecate-with-warning for one release first, then "
        "remove. Removing it now breaks existing scripts."
    )


def test_cli_set_primary_emits_deprecation_warning():
    """When the command runs, it must surface a deprecation notice
    to the operator. Without that, scripts using it silently keep
    working and the operator doesn't know they need to migrate."""
    src = _CLI.read_text(encoding="utf-8")
    m = re.search(
        r"def\s+config_set_primary\s*\([^)]*\)\s*->\s*None\s*:\s*\"\"\"([\s\S]*?)\"\"\"\s*\n([\s\S]*?)(?=\n\n@|\nif\s+__name__|\Z)",
        src,
    )
    assert m, "could not locate config_set_primary body for deprecation check"
    docstring = m.group(1)
    body = m.group(2)
    # The docstring should mark it deprecated.
    assert "DEPRECATED" in docstring or "deprecated" in docstring.lower(), (
        "config_set_primary's docstring no longer marks it as "
        "deprecated. ``--help`` won't surface the deprecation that way."
    )
    # And the body must print a warning before doing the work.
    assert (
        "deprecated" in body.lower() or "retired" in body.lower()
    ), (
        "config_set_primary's body no longer warns the operator. "
        "Anyone scripting against it silently keeps using a path "
        "we're retiring."
    )


def test_cli_help_line_marks_set_primary_deprecated():
    """The CLI's top-level help block lists subcommands. The
    set-primary entry should flag itself as deprecated so operators
    looking at ``evolve-admin --help`` see the status."""
    src = _CLI.read_text(encoding="utf-8")
    # Find the help-block line about set-primary.
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("config set-primary"):
            assert "DEPRECATED" in s or "deprecated" in s.lower(), (
                f"the help-block line for set-primary doesn't flag "
                f"deprecation. Line was: {line!r}"
            )
            return
    pytest.fail("could not find a help-block line mentioning set-primary")