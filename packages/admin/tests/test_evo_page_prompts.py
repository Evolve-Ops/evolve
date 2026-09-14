"""tests/test_evo_page_prompts.py — chat-widget per-page coaching prompts.

The Evo drawer's empty-state placeholder shows page-specific (and
subtab-specific) example prompts pulled from the ``_EVO_PAGE_PROMPTS``
registry inside ``packages/admin/evolve_admin/web/index.html``. People
don't read manuals — they read what's right in front of them. That
registry is one of the load-bearing places where we teach operators
how to use evo, so it pays to guard its shape.

This module runs structural invariants over the registry without
spinning up a browser:

  * Every key matches a real nav-item ``data-page="..."`` value in
    the same file, so we can't accidentally register a prompt for a
    page that doesn't exist.
  * Every prompt is a non-empty short string (the placeholder gets
    crowded past ~80 chars).
  * Every subtab override matches a real ``.subtab`` declaration on
    the corresponding page.
  * Every page-with-subtabs registry entry has a ``default`` key, so
    operators viewing the page before clicking any subtab still see
    coaching.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_WEB = _ADMIN_PKG / "evolve_admin" / "web"
_INDEX_HTML = _WEB / "index.html"
_EVO_DRAWER_JS = _WEB / "static" / "js" / "pages" / "evo-drawer.js"


# ── Extract registry + nav items via lightweight text scans ──────────────────


def _read_html() -> str:
    # _EVO_PAGE_PROMPTS lives in pages/evo-drawer.js (Phase 3n). The
    # registry parser below reads the same blob the nav-pages walker
    # does, so concat — nav markup stays in index.html, prompts come
    # from evo-drawer.js.
    return _INDEX_HTML.read_text(encoding="utf-8") + "\n" + _EVO_DRAWER_JS.read_text(encoding="utf-8")


def _extract_prompts_literal(html: str) -> str:
    """Return the source-text slice between ``const _EVO_PAGE_PROMPTS = {``
    and the matching closing ``};``. We don't parse JS; we just isolate
    the literal so a JSON-like brace walker can pull out the structure.
    """
    start = html.find("const _EVO_PAGE_PROMPTS = {")
    assert start >= 0, "_EVO_PAGE_PROMPTS literal not found in index.html"
    # Walk braces from the `{` after the `=` to find the matching `}`.
    brace_open = html.index("{", start)
    depth = 0
    i = brace_open
    while i < len(html):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[brace_open:i + 1]
        i += 1
    raise AssertionError("unbalanced braces in _EVO_PAGE_PROMPTS")


def _strip_block_comments(src: str) -> str:
    """Remove /* ... */ comment spans. Single-line // comments are kept
    because none appear inside the registry literal in practice; if they
    do later, add stripping here."""
    return re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)


# Match top-level page keys — anchored at exactly 2-space indent so we
# don't accidentally match the 4-space-indented subtab keys nested
# inside a page's object literal.
_PAGE_RE = re.compile(r"^  '([a-z0-9_-]+)'\s*:\s*(\[|\{)", re.MULTILINE)

# Subtab keys: anchored at exactly 4-space indent inside a page's
# object literal. (Pages with flat lists skip this.)
_SUBTAB_RE = re.compile(r"^    '([a-z0-9_-]+)'\s*:\s*\[", re.MULTILINE)

# String entries inside any [...]: ``"foo"`` or ``'foo'`` (we use double
# quotes in the registry but support both).
_PROMPT_RE = re.compile(r"""(?<![A-Za-z0-9_])(?:"([^"]*)"|'([^']*)')""")


def _parse_registry(html: str) -> dict[str, dict]:
    """Lightweight parse of the registry literal into:

      ``{ page_id: {"flat": list[str] | None, "subtabs": {sub: list[str]}} }``

    A page with a flat list shows ``flat`` set; a page with an object
    literal shows ``subtabs`` populated (and may include a ``default``
    key inside that dict).
    """
    lit = _strip_block_comments(_extract_prompts_literal(html))
    pages: dict[str, dict] = {}

    for m in _PAGE_RE.finditer(lit):
        page_id = m.group(1)
        opener = m.group(2)
        # Find the matching closer for this page's value.
        depth = 1
        i = m.end()
        opener_ch = opener
        closer_ch = "]" if opener == "[" else "}"
        while i < len(lit) and depth > 0:
            ch = lit[i]
            if ch in '"\'':
                # Skip over string literals — they can contain braces.
                quote = ch
                i += 1
                while i < len(lit) and lit[i] != quote:
                    if lit[i] == "\\":
                        i += 2
                        continue
                    i += 1
                i += 1
                continue
            if ch == opener_ch:
                depth += 1
            elif ch == closer_ch:
                depth -= 1
            i += 1
        body = lit[m.end():i - 1]
        if opener == "[":
            # Flat list — extract string entries
            prompts = [g1 or g2 for g1, g2 in _PROMPT_RE.findall(body)]
            pages[page_id] = {"flat": prompts, "subtabs": {}}
        else:
            # Subtab object — find each subtab's list
            pages[page_id] = {"flat": None, "subtabs": {}}
            for sm in _SUBTAB_RE.finditer(body):
                subtab = sm.group(1)
                # Walk to matching ]
                sdepth = 1
                si = sm.end()
                while si < len(body) and sdepth > 0:
                    sch = body[si]
                    if sch in '"\'':
                        quote = sch
                        si += 1
                        while si < len(body) and body[si] != quote:
                            if body[si] == "\\":
                                si += 2
                                continue
                            si += 1
                        si += 1
                        continue
                    if sch == "[":
                        sdepth += 1
                    elif sch == "]":
                        sdepth -= 1
                    si += 1
                sub_body = body[sm.end():si - 1]
                sub_prompts = [g1 or g2 for g1, g2 in _PROMPT_RE.findall(sub_body)]
                pages[page_id]["subtabs"][subtab] = sub_prompts
    return pages


def _nav_pages(html: str) -> set[str]:
    """Every value of ``data-page="..."`` on a ``.nav-item``. Used to
    verify that the registry doesn't list a page that isn't in the
    sidebar."""
    return set(re.findall(r'class="nav-item[^"]*"\s+data-page="([^"]+)"', html))


def _subtabs_for_page(html: str, page_id: str) -> set[str]:
    """Every subtab name registered on the named page. Reads
    ``subTab(this,'page-id','subtab-name')`` declarations. Matches the
    runtime resolution path in ``_evoDrawerCurrentSubtab``."""
    pattern = re.compile(
        r"subTab\(this\s*,\s*'%s'\s*,\s*'([^']+)'\)" % re.escape(page_id),
    )
    return set(pattern.findall(html))


# ── Test fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def html() -> str:
    return _read_html()


@pytest.fixture(scope="module")
def registry(html: str) -> dict[str, dict]:
    return _parse_registry(html)


@pytest.fixture(scope="module")
def nav_pages(html: str) -> set[str]:
    return _nav_pages(html)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_registry_parses_and_has_entries(registry: dict):
    assert len(registry) > 0, "registry parse returned empty — parser broken?"


def test_every_registered_page_id_matches_a_nav_item(registry: dict, nav_pages: set[str]):
    """Catches typos: e.g. registering 'plugins' when the nav-item is
    'integrations-keys'."""
    bogus = set(registry.keys()) - nav_pages
    assert not bogus, (
        f"_EVO_PAGE_PROMPTS has page id(s) with no matching nav-item: "
        f"{sorted(bogus)}. Either fix the spelling or remove the entry."
    )


def test_every_subtab_override_matches_a_real_subtab(html: str, registry: dict):
    """A subtab override on a page that doesn't have that subtab in its
    .subtab declarations is dead code — and a typo waiting to confuse a
    future maintainer. (e.g. registering 'creds' instead of 'credentials')."""
    for page_id, entry in registry.items():
        subtabs = entry.get("subtabs") or {}
        if not subtabs:
            continue
        page_subtabs = _subtabs_for_page(html, page_id)
        if not page_subtabs:
            # Page has no subtab system at all — only "default" is
            # allowed in that case (it shouldn't be used either, but
            # benign).
            extras = {k for k in subtabs if k != "default"}
            assert not extras, (
                f"page '{page_id}' has no subtabs in HTML but registry "
                f"declares overrides for: {sorted(extras)}"
            )
            continue
        extras = {k for k in subtabs if k != "default"} - page_subtabs
        assert not extras, (
            f"page '{page_id}': subtab override(s) {sorted(extras)} don't "
            f"match any .subtab on that page (real: {sorted(page_subtabs)})"
        )


def test_subtab_entries_have_default(registry: dict):
    """If a page registers subtab-specific prompts, it MUST register a
    'default' too — otherwise viewing the page before clicking any
    subtab (the freshly-loaded state) gets the generic placeholder
    instead of page-specific coaching."""
    for page_id, entry in registry.items():
        subtabs = entry.get("subtabs") or {}
        if not subtabs:
            continue
        assert "default" in subtabs, (
            f"page '{page_id}' has subtab-specific prompts but no "
            "'default' fallback. Operators who haven't clicked a subtab "
            "yet won't see coaching. Add a `default: [...]` entry."
        )


def test_every_prompt_is_non_empty(registry: dict):
    for page_id, entry in registry.items():
        flat = entry.get("flat")
        if flat is not None:
            for p in flat:
                assert isinstance(p, str) and p.strip(), (
                    f"page '{page_id}' has an empty prompt: {p!r}"
                )
        for subtab, prompts in (entry.get("subtabs") or {}).items():
            for p in prompts:
                assert isinstance(p, str) and p.strip(), (
                    f"page '{page_id}'.{subtab} has an empty prompt: {p!r}"
                )


def test_every_prompt_is_short_enough(registry: dict):
    """Placeholder gets crowded above ~80 chars. Hard cap at 100 so a
    well-meaning but verbose example doesn't break the layout."""
    issues: list[tuple[str, str, str]] = []
    for page_id, entry in registry.items():
        flat = entry.get("flat")
        if flat is not None:
            for p in flat:
                if len(p) > 100:
                    issues.append((page_id, "default", p))
        for subtab, prompts in (entry.get("subtabs") or {}).items():
            for p in prompts:
                if len(p) > 100:
                    issues.append((page_id, subtab, p))
    assert not issues, (
        "the following prompts exceed 100 chars (shorten or split): "
        + "\n".join(f"  {pg}.{st}: {p!r}" for pg, st, p in issues)
    )


def test_prompt_count_caps_at_four(registry: dict):
    """More than 4 prompts crowds the placeholder and reads as a list,
    not as inline coaching. Soft guard: cap at 4."""
    issues: list[tuple[str, str, int]] = []
    for page_id, entry in registry.items():
        flat = entry.get("flat")
        if flat is not None and len(flat) > 4:
            issues.append((page_id, "default", len(flat)))
        for subtab, prompts in (entry.get("subtabs") or {}).items():
            if len(prompts) > 4:
                issues.append((page_id, subtab, len(prompts)))
    assert not issues, (
        "prompt lists with more than 4 entries crowd the placeholder; "
        f"trim: {issues}"
    )


# Test-pod bot names that must never appear in inline-coaching examples.
# Each install has different bot names; showing another install's names
# looks broken to the operator. Use ``[bot]`` (or similar bracketed
# placeholder) instead. ``evo`` and ``evolve`` (the product / pod-meta
# names) are NOT included here — they're invariants across installs and
# are fine to use literally.
_FORBIDDEN_BOT_NAMES = {
    "team_bot_a", "admin_bot", "team_bot_b", "team_bot_c", "personal_bot", "security_bot",
}


def test_no_hardcoded_testpod_bot_names_in_prompts(registry: dict):
    """Inline-coaching examples must use generic placeholders for bot
    names, never the test-pod names. ``[bot]`` is the convention; future
    additions should follow it. (If a real, install-agnostic identifier
    enters use later — e.g. a built-in shipped bot — add it as an
    exception alongside ``evo`` / ``evolve`` rather than relaxing the
    rule wholesale.)"""
    bad: list[tuple[str, str, str, str]] = []  # (page, subtab, prompt, name)

    def _scan(page_id: str, subtab: str, prompts: list[str]) -> None:
        for p in prompts:
            # Tokenize on word boundaries — substring matches would
            # false-positive on e.g. "team_bot_a" inside "morning-brief team_bot_a-up"
            # if that ever existed; we want exact word matches.
            words = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", p.lower())
            for n in _FORBIDDEN_BOT_NAMES:
                if n in words:
                    bad.append((page_id, subtab, p, n))

    for page_id, entry in registry.items():
        flat = entry.get("flat")
        if flat is not None:
            _scan(page_id, "default", flat)
        for subtab, prompts in (entry.get("subtabs") or {}).items():
            _scan(page_id, subtab, prompts)

    assert not bad, (
        "inline-coaching prompts must not hardcode test-pod bot names — "
        "use `[bot]` as a placeholder so the examples don't look broken "
        "on installs with different bots. Offenders:\n"
        + "\n".join(
            f"  {pg}.{st}: {p!r}  (hit: {n})"
            for pg, st, p, n in bad
        )
    )
