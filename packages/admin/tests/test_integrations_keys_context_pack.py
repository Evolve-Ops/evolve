"""tests/test_integrations_keys_context_pack.py — guard that the
Plugins page's context-pack (``integrations-keys`` entry in
_EVO_CONTEXT_PACKS) surfaces every subtab's data to evo, AND that
each subtab's snapshot writer goes through the shared
``_ikWriteContextSnapshot`` helper so siblings are preserved.

Closes the gap from before this PR: the page has six subtabs
(Plugins / Credentials / Embeddings / MCP / Hooks / Activity) with
suggested-prompt entries in ``_EVO_PAGE_PROMPTS``, but no
context-pack entry — operators chatting from this page got
suggestions that routed nowhere.

Pattern lifted from test_security_context_snapshot.py (PR #1366) and
the parallel test_reports_context_snapshot.py (PR #1369) — regex on
the HTML source. Tests SHAPE, not behavior. The runtime is exercised
live on the mini.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_INDEX_HTML = _ADMIN_PKG / "evolve_admin" / "web" / "index.html"
_PLUGINS_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "plugins.js"
_EVO_DRAWER_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "evo-drawer.js"
_AGENTS_MD = (
    _ADMIN_PKG.parent / "analyzer" / "evolve_bot" / "AGENTS.md"
)


@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8") + "\n" + _EVO_DRAWER_JS.read_text(encoding="utf-8") + "\n" + _PLUGINS_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def agents_md() -> str:
    return _AGENTS_MD.read_text(encoding="utf-8") + "\n" + _EVO_DRAWER_JS.read_text(encoding="utf-8") + "\n" + _PLUGINS_JS.read_text(encoding="utf-8")


def test_ik_snapshot_writer_helper_exists(html: str):
    """The ``_ikWriteContextSnapshot`` helper must exist — it's the
    single funnel every subtab loader goes through to fold its slice
    of data into ``_evoContextSnapshots['integrations-keys']`` while
    preserving sibling fields. Without it, each subtab's writer
    would have to remember the ``..._prev`` spread itself — and the
    moment one forgets, the 2026-05-20 'drifted_bots=0' clobber bug
    returns for this page.
    """
    assert "function _ikWriteContextSnapshot" in html, (
        "the _ikWriteContextSnapshot helper is gone. If snapshot writes "
        "moved to inline ..._prev spreads, that's fine — but every "
        "ikLoad*/ikRender* function below now needs its own clobber-"
        "prevention check, and this test should be updated to scan "
        "for that pattern directly."
    )
    # The helper must spread the prior value (that's the whole point).
    # Locate the helper body and check the spread is present.
    start = html.find("function _ikWriteContextSnapshot")
    body = html[start:start + 600]
    assert re.search(r"\.\.\.\s*_?prev", body), (
        "_ikWriteContextSnapshot no longer spreads the prior value. "
        "Without the spread, switching subtabs clobbers the previously-"
        "written subtab's data — every subtab loses its slice the "
        "moment another subtab loads. Restore the ``...prev`` spread."
    )


def test_ik_subtab_loaders_use_helper(html: str):
    """Each of the six subtabs (Plugins / Credentials / Embeddings /
    MCP / Hooks / Activity) must call ``_ikWriteContextSnapshot`` so
    its slice reaches evo. Without coverage on a subtab, evo on that
    subtab gets an ``active_subtab`` flag but no data — and answers
    a "what do I see here?" question with a no-op."""
    # We don't pin which function does the write (loader vs renderer
    # is implementation-detail); we just count the writes. One per
    # subtab data slice + one for the active-subtab tracker in
    # onSubTabActivate + one for the bot/subtab seed in
    # loadIntegrationsKeys = 8+ total. Accept 6+ as the floor —
    # below that, some subtab has lost coverage.
    count = len(re.findall(r"\b_ikWriteContextSnapshot\s*\(", html))
    assert count >= 6, (
        f"only {count} call(s) to _ikWriteContextSnapshot found; "
        f"expected at least 6 (one per subtab data slice). Some "
        f"subtab's coverage is gone — evo will be told it's on that "
        f"subtab but get no data about what's on screen."
    )


def _locate_ik_pack(html: str) -> str:
    """Return a window of HTML containing the 'integrations-keys' entry
    of _EVO_CONTEXT_PACKS. Returns ~12000 chars from the entry start —
    big enough to cover the multi-subtab builder body."""
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    if packs_start < 0:
        packs_start = html.find("_EVO_CONTEXT_PACKS = {")
    assert packs_start > 0, (
        "could not locate the _EVO_CONTEXT_PACKS declaration. If the "
        "registry was renamed, update this test."
    )
    # The 'integrations-keys' key appears in _EVO_PAGE_PROMPTS first;
    # anchor at the actual builder declaration after the registry's
    # opening brace.
    start = html.find("'integrations-keys':", packs_start)
    if start < 0:
        start = html.find('"integrations-keys":', packs_start)
    assert start > 0, (
        "could not locate the 'integrations-keys' entry inside "
        "_EVO_CONTEXT_PACKS. Without it, the Plugins page has no "
        "context-pack — operator chats reach evo with only page_id "
        "and page_label. Restore the pack entry."
    )
    return html[start:start + 12000]


def test_integrations_keys_pack_entry_exists(html: str):
    """The 'integrations-keys' page-context-pack entry must exist in
    _EVO_CONTEXT_PACKS. The page has six subtabs with prompts
    already shipped; without the pack entry, those prompts route to
    a model with no awareness of what's on screen."""
    window = _locate_ik_pack(html)
    # The builder must read from the snapshot bucket; without that,
    # the pack can't reflect what's on screen.
    assert "_evoContextSnapshots" in window, (
        "the 'integrations-keys' context-pack builder no longer reads "
        "from ``window._evoContextSnapshots['integrations-keys']``. The "
        "pack is then static and can't reflect what's on screen."
    )


def test_integrations_keys_pack_tracks_active_subtab(html: str):
    """The page has six subtabs; the pack must surface
    ``active_subtab`` so evo can frame its reply correctly.
    Operator on Hooks isn't asking about Embeddings, and vice versa.
    Without subtab awareness, evo on every subtab gets a generic
    "Plugins page" headline that's almost never what the operator
    is actually pointing at."""
    window = _locate_ik_pack(html)
    assert "active_subtab" in window, (
        "the 'integrations-keys' context-pack no longer tracks "
        "active_subtab. Evo can't tell whether the operator is on "
        "Plugins, Credentials, Embeddings, MCP, Hooks, or Activity — "
        "every subtab gets the same framing, and headline phrasing "
        "becomes useless coaching."
    )


def test_integrations_keys_pack_covers_all_subtabs(html: str):
    """The pack should surface data slices for all six subtabs so
    evo can answer cross-subtab questions ("which subtab has the
    most issues?") without re-fetching. Each subtab name must appear
    somewhere in the builder body — either as a key in the data
    projection or as a headline branch."""
    window = _locate_ik_pack(html)
    for subtab in (
        "plugins",
        "credentials",
        "embeddings",
        "mcp",
        "hooks",
        "activity",
    ):
        assert subtab in window, (
            f"the 'integrations-keys' context-pack no longer references "
            f"the {subtab!r} subtab. Evo on that subtab will see only "
            f"the generic headline; questions about what's on screen "
            f"there have no anchor."
        )


def test_integrations_keys_pack_points_at_existing_tools(html: str):
    """The pack's tool_pointers must route evo at existing tools.
    The Plugins page lacks per-substrate action tools (rotate /
    mcp.install / hook.set_policy / etc.) — when the operator wants
    to mutate, evo points them at the row button. But for read +
    plugin enable/disable, the tools DO exist; the pack must point
    at them.
    """
    window = _locate_ik_pack(html)
    for tool_name in (
        # Reads — pod_state(query="config_bot") is the source-of-truth
        # for plugins / mcp / hooks / embeddings overrides. Without it
        # the pack tells evo nothing about WHERE the underlying data
        # lives.
        'pod_state(query="config_bot"',
        # Pod-wide invariants — needed for credentials.missing_pod_invariants
        # explanations.
        'pod_state(query="config_network")',
        # The two action tools that DO exist for this page — operator
        # says "enable discord on [bot]" / "disable the github plugin"
        # and evo must know to reach for these, not fabricate a UI
        # walkthrough.
        'plugin_action(action="enable"',
        'plugin_action(action="disable"',
    ):
        assert tool_name in window, (
            f"the 'integrations-keys' context-pack no longer points at "
            f"``{tool_name}``. Evo will read the pack but have no "
            f"guidance on which tool maps to the on-screen state. For "
            f"action tools specifically, evo may fabricate a tool name "
            f"or fall back to a shell snippet — both regressions we've "
            f"seen and explicitly guarded against."
        )


def test_integrations_keys_pack_surfaces_per_subtab_data(html: str):
    """Each subtab's snapshot slice must reach a model-facing key in
    the pack. We don't pin the exact key shape (the pack may evolve)
    but each subtab's data marker must appear in the builder body."""
    window = _locate_ik_pack(html)
    # These markers come from the snapshot patches each subtab's
    # writer hands to _ikWriteContextSnapshot. If one disappears,
    # that subtab's slice has been dropped.
    for marker in (
        "plugins_items",       # Plugins subtab projection
        "credentials_items",   # Credentials subtab projection
        "embeddings_items",    # Embeddings subtab projection
        "mcp_items",           # MCP subtab projection
        "hooks_items",         # Hooks subtab projection
        "activity_items",      # Activity subtab projection
    ):
        assert marker in window, (
            f"the 'integrations-keys' context-pack no longer surfaces "
            f"``{marker}``. The corresponding subtab's data has been "
            f"dropped from evo's view — questions about that subtab "
            f"have no on-screen anchor."
        )


def test_integrations_keys_page_tool_map_present(agents_md: str):
    """AGENTS.md's page-tool map must reference the Plugins page so
    evo knows which tools to reach for when the operator chats from
    this surface. The Page-tool map is the evergreen reference; the
    context-pack is the per-turn ground-truth."""
    # The map row mentions either "Plugins" or "Integrations-Keys"
    # explicitly. Either is fine; we just need SOME entry.
    assert (
        "Plugins (Plugins subtab)" in agents_md
        or "Plugins (Credentials" in agents_md
        or "Integrations" in agents_md
    ), (
        "AGENTS.md's page-tool map no longer has a row for the Plugins / "
        "Integrations page. Without it, evo on this page falls back to "
        "guessing which tool maps to which subtab — and the four "
        "subtabs without action tools (Credentials / MCP / Hooks / "
        "Embeddings) become an open invitation to fabricate."
    )
