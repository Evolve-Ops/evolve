"""tests/test_evo_pack_coverage.py — JS page packs in lock-step with HTML.

Per-page context packs (spec §3.4) live in
``web/index.html::_EVO_CONTEXT_PACKS``. The admin UI's nav declares
each page via ``data-page="X"`` attributes. These tests detect the
two silent drift modes:

  * **Page added without a pack** — operator chats from the new page,
    pack lookup misses, no `<page-context>` block, evo falls back to
    fetch-on-demand. Bounded but slower; AGENTS.md page-tool map
    handles it.

  * **Pack added that doesn't map to a real page** — typo in the
    pack key. Pack never fires; silent waste of work.

When you intentionally add a page WITHOUT a pack, add its page-id to
``_PACK_OPT_OUT_PAGES`` below with a comment explaining why. Don't
weaken these tests; the opt-out list IS the documentation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_INDEX_HTML = (
    _ADMIN_PKG / "evolve_admin" / "web" / "index.html"
)
_EVO_DRAWER_JS = (
    _ADMIN_PKG / "evolve_admin" / "web" / "static" / "js" / "pages" / "evo-drawer.js"
)


# Pages that intentionally have no page-context pack. Adding to this
# list is the documented escape hatch — pair every entry with a
# one-line rationale.
#
# ``home`` (the Chat page) was originally in this list with the
# rationale "no context to pack about". That turned out to be wrong:
# the Evo's-report banner at the top of the Chat page IS context the
# operator just read, and a 2026-05-20 transcript exposed evo asking
# "which baseline?" right after the report named which one. The home
# pack now exists — see ``test_home_pack_carries_report_content``
# below.
_PACK_OPT_OUT_PAGES = {
    "getting-started",       # operator-onboarding wizard; static content
    "settings",              # config form; operator's intent is clear from the page
    "users",                 # config-shape page (pod admins + per-bot paired users);
                             # same shape as settings — operator's intent is the page itself
    "errors",                # log dump; tool path (pod_state.signals.firing) is enough
    "feedback",              # contact form; no actionable state
    "help",                  # docs; AGENTS.md page-tool map is the right surface
    "integrations-keys",     # plugin manager; deferred (mechanical follow-on)
    "skills",                # OC skills; deferred
    "ai-optimization",       # niche tuning page; deferred
    "cost-measures",         # cost-optimization — paired with the `cost` pack
    # The five pages below get packs in PR #1301 — once that merges
    # and this branch rebases, these entries become harmless (packs
    # exist; the test still passes because opt-out is a "no pack ok"
    # ack, not a "must not have pack" requirement). Drop them then.
    "overview",              # Dashboard pack — lever #3 (PR #1301)
    "security",              # Security pack — lever #3 (PR #1301)
    "cost",                  # Usage pack — lever #3 (PR #1301)
    "apps",                  # Apps pack — lever #3 (PR #1301)
    "reports",               # Reports pack — lever #3 (PR #1301)
}


def _read_html() -> str:
    # _EVO_CONTEXT_PACKS + _EVO_PAGE_PROMPTS live in pages/evo-drawer.js
    # (Phase 3n). Concat so the page-coverage assertions still find them.
    return _INDEX_HTML.read_text() + "\n" + _EVO_DRAWER_JS.read_text()


def _discover_nav_pages(html: str) -> set[str]:
    """Pull every ``data-page="X"`` value from the nav. The HTML uses
    this attribute on every routed page; ad-hoc divs that aren't
    real pages don't have it."""
    return set(re.findall(r'class="nav-item[^"]*"\s+data-page="([^"]+)"', html))


def _discover_pack_keys(html: str) -> set[str]:
    """Extract the keys of the ``_EVO_CONTEXT_PACKS`` registry.

    The registry is a JS object literal with quoted-string keys. We
    look for the registry start marker then parse top-level keys only
    (entries indented exactly two spaces from the registry body).
    Nested-object keys (eg ``counts: { ... }`` inside a pack body)
    are at deeper indentation and skipped.

    Regex-based — keeps test self-contained without a JS parser
    dependency.
    """
    match = re.search(
        r"const\s+_EVO_CONTEXT_PACKS\s*=\s*\{(.*?)\n\};",
        html, re.DOTALL,
    )
    if not match:
        return set()
    body = match.group(1)
    keys: set[str] = set()
    # Top-level entries inside ``const _EVO_CONTEXT_PACKS = { ... }``
    # sit at exactly 2-space indent and start with a quoted string OR
    # an identifier, then arrow / paren / function. Nested keys are at
    # deeper indent and don't match this anchored pattern.
    for m in re.finditer(
        r"^  (?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][\w-]*))\s*:\s*\(",
        body, re.MULTILINE,
    ):
        key = m.group(1) or m.group(2) or m.group(3)
        if key:
            keys.add(key)
    return keys


def test_html_is_readable():
    """Sanity: the index.html exists and is non-trivial. Catches
    a refactor that moves index.html out from under us — better to
    fail loudly here than silently from a 0-byte parse."""
    assert _INDEX_HTML.exists(), f"{_INDEX_HTML} not found"
    text = _read_html()
    assert len(text) > 100_000, "index.html is suspiciously small"


def test_nav_pages_discovered():
    """At minimum we expect the original ~16 admin UI pages."""
    pages = _discover_nav_pages(_read_html())
    assert len(pages) >= 10, (
        f"only {len(pages)} nav pages discovered — the data-page regex "
        f"may have drifted from HTML. Found: {pages}"
    )


def test_pack_keys_discovered():
    """The pack registry parse picks up at least the original two
    packs we know exist on main."""
    keys = _discover_pack_keys(_read_html())
    assert keys, "_EVO_CONTEXT_PACKS parse returned no keys — regex stale?"
    # `maintenance` and `self-improvement` were the first packs
    # shipped (Phase 4.1). If those vanish, something deeper broke.
    assert "maintenance" in keys, f"core pack 'maintenance' missing: keys={keys}"
    assert "self-improvement" in keys, (
        f"core pack 'self-improvement' missing: keys={keys}"
    )


def test_every_pack_key_matches_a_real_nav_page():
    """A pack keyed on a page-id that doesn't exist in the nav is
    dead code — silent waste, never fires."""
    html = _read_html()
    nav_pages = _discover_nav_pages(html)
    pack_keys = _discover_pack_keys(html)
    phantom = pack_keys - nav_pages
    assert not phantom, (
        f"_EVO_CONTEXT_PACKS has keys that aren't in any nav data-page "
        f"attribute: {sorted(phantom)}. Either rename the pack to match "
        f"the actual page-id, or remove it. Nav pages found: "
        f"{sorted(nav_pages)}"
    )


def test_every_nav_page_has_a_pack_or_is_explicitly_opted_out():
    """A new page added to the nav without a pack triggers a
    deliberate ack: either register a pack (preferred) or add the
    page-id to ``_PACK_OPT_OUT_PAGES`` with rationale."""
    html = _read_html()
    nav_pages = _discover_nav_pages(html)
    pack_keys = _discover_pack_keys(html)
    uncovered = nav_pages - pack_keys - _PACK_OPT_OUT_PAGES
    assert not uncovered, (
        f"These nav pages have neither a context pack nor an "
        f"opt-out entry: {sorted(uncovered)}. Pick one:\n"
        f"  - Register a _EVO_CONTEXT_PACKS entry for the page (see "
        f"the maintenance / self-improvement / overview packs for "
        f"patterns).\n"
        f"  - Add the page-id to _PACK_OPT_OUT_PAGES in this test file "
        f"with a comment explaining why a pack isn't needed."
    )


def test_home_pack_carries_report_content():
    """The Chat page ('home') has a pack whose body references the
    DOM ids that hold the Evo's-report narrative + extras. Catches
    a refactor that renames the elements (#home-narr-prose,
    #home-narr-extras) without updating the pack — the operator-visible
    symptom would be evo reverting to "which baseline?" cluelessness
    we just fixed."""
    html = _read_html()
    pack_keys = _discover_pack_keys(html)
    assert "home" in pack_keys, (
        "_EVO_CONTEXT_PACKS no longer has a 'home' entry. The Chat "
        "page would fall back to a metadata-only pack, and evo would "
        "lose awareness of what the report banner says. See the "
        "2026-05-20 transcript for the operator-visible symptom."
    )
    # Pull the body of the home pack.
    m = re.search(
        r"^  'home'\s*:\s*\(\s*\)\s*=>\s*\{([\s\S]*?)\n  \},",
        html, re.MULTILINE,
    )
    assert m, "could not locate the home pack body for content check"
    body = m.group(1)
    for needle in ("home-narr-prose", "home-narr-extras"):
        assert needle in body, (
            f"home pack body no longer references '{needle}'. If you "
            f"renamed the report DOM element, also update the pack."
        )
    # Sanity: pack should mention report content + bots so the operator
    # can ask about either.
    assert "report_text" in body, (
        "home pack should expose report_text as a top-level field for "
        "evo's prompt template to render"
    )


def test_opt_out_pages_are_actually_nav_pages():
    """Defense against a stale opt-out: if a page was removed, its
    opt-out entry should be removed too."""
    nav_pages = _discover_nav_pages(_read_html())
    stale = _PACK_OPT_OUT_PAGES - nav_pages
    assert not stale, (
        f"_PACK_OPT_OUT_PAGES references pages that no longer exist in "
        f"the nav: {sorted(stale)}. Remove the stale entries."
    )
