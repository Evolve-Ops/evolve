"""tests/test_bot_detail_context_pack.py — structural guards for the
bot-detail page-context pack added so evo has structured awareness of
which bot is on screen when an operator is on Settings → Pod Config →
Bot.

The gap this closed: an operator on (say) team_bot_a's bot-detail page asks
evo "tell me about team_bot_a's setup" and evo has no idea which bot is
selected, what its archetype / caps / models / compaction / slack
policy are, or how to frame the reply. The pack reads from a
snapshot that `loadConfigBot` writes after rendering each card.

What this file pins:

  * The ``'settings'`` entry exists in ``_EVO_CONTEXT_PACKS``.
  * The snapshot writer in ``loadConfigBot`` uses ``..._prev`` spread
    so future writers onto ``_evoContextSnapshots.settings`` can't
    clobber bot-detail siblings (the PR #1366 hotfix pattern — every
    writer to a shared snapshot bucket must spread the prior value).
  * The pack reads ``snap.bot_detail`` AND surfaces it under a
    model-facing key — otherwise the data is captured but never
    reaches evo.
  * AGENTS.md's page-tool map still has a Bot-detail row with the
    matching tool pointers.

Pattern lifted from test_security_context_snapshot.py — regex on the
HTML source. Tests SHAPE, not behavior; the runtime is exercised live
on the mini.
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
_POD_CONFIG_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "pod-config.js"
_EVO_DRAWER_JS = _ADMIN_PKG / "evolve_admin" / "web"/ "static" / "js" / "pages" / "evo-drawer.js"
_AGENTS_MD = _ADMIN_PKG.parent / "analyzer" / "evolve_bot" / "AGENTS.md"
# B6 split the template into the ingested core + the seeded reference library;
# the page-tool map these tests pin now lives in reference/PAGE_CONTEXT.md, so
# the fixture reads the full taught corpus (same posture as
# test_evo_agents_md_guards.agents_md).
_REFERENCE_DIR = _AGENTS_MD.parent / "reference"


@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8") + "\n" + _EVO_DRAWER_JS.read_text(encoding="utf-8") + "\n" + _POD_CONFIG_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def agents_md() -> str:
    corpus = "\n".join(
        [_AGENTS_MD.read_text(encoding="utf-8")]
        + [p.read_text(encoding="utf-8") for p in sorted(_REFERENCE_DIR.glob("*.md"))]
    )
    return corpus + "\n" + _EVO_DRAWER_JS.read_text(encoding="utf-8") + "\n" + _POD_CONFIG_JS.read_text(encoding="utf-8")


def test_settings_pack_entry_exists(html: str):
    """The ``'settings'`` key must appear inside the
    ``_EVO_CONTEXT_PACKS`` registry. Without the entry, the proxy
    sends no <page-context> summary for Settings — evo has no
    awareness of which bot is selected on Pod Config → Bot."""
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    assert packs_start > 0, (
        "could not locate the _EVO_CONTEXT_PACKS declaration. If the "
        "registry was renamed, update this test."
    )
    # Find the closing ``};`` of the const declaration so we scope the
    # search to the registry body (the literal ``'settings'`` also
    # appears earlier in _EVO_PAGE_PROMPTS — anchor past _EVO_CONTEXT_PACKS).
    settings_key = html.find("'settings':", packs_start)
    assert settings_key > 0, (
        "no 'settings' entry inside _EVO_CONTEXT_PACKS. The bot-detail "
        "page-context pack hasn't been registered. Without it, the proxy "
        "sends no <page-context> when the operator is on Settings."
    )


def test_settings_snapshot_writers_preserve_siblings(html: str):
    """Every assignment to ``window._evoContextSnapshots.settings``
    must spread the prior value. Single-writer today, but the PR #1366
    hotfix pattern requires the spread upfront — a periodic poller or
    other render path added later must not be able to clobber
    bot-detail by writing without spread.
    """
    pattern = re.compile(
        r"window\._evoContextSnapshots\.settings\s*=\s*\{([\s\S]*?)\n\s*\};",
        re.MULTILINE,
    )
    matches = pattern.findall(html)
    assert matches, (
        "no assignments to window._evoContextSnapshots.settings found. "
        "Either the snapshot writer was removed, or the assignment "
        "shape was refactored away from a literal object — update "
        "this test to match the new shape."
    )
    for i, body in enumerate(matches):
        has_spread = bool(re.search(r"\.\.\.\s*_?prev\b", body))
        assert has_spread, (
            f"_evoContextSnapshots.settings assignment #{i+1} doesn't "
            f"spread the prior value. Without the spread, any future "
            f"writer onto this bucket (e.g. a Modules-tab poller) will "
            f"clobber bot_detail to undefined. Body was:\n{body[:400]}"
        )


def test_settings_pack_reads_bot_detail_snapshot(html: str):
    """The ``'settings'`` entry in _EVO_CONTEXT_PACKS must read
    ``bot_detail`` out of the snapshot AND surface it under a
    model-facing key — otherwise loading the cards into the snapshot
    is wasted work, no field reaches evo.

    We widen to a window after the 'settings' key because the builder
    body contains nested objects that break a simple ``\\{...\\}``
    balanced match.
    """
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    assert packs_start > 0, "registry declaration missing"
    start = html.find("'settings':", packs_start)
    assert start > 0, "no 'settings' entry in _EVO_CONTEXT_PACKS"
    # Builder bodies are ~50–100 lines. 5000 chars covers any plausible
    # rewrite while staying inside the same builder.
    window = html[start:start + 5000]
    assert "snap.bot_detail" in window, (
        "the 'settings' context-pack builder no longer reads "
        "``snap.bot_detail`` from the snapshot. Without it, the "
        "per-bot config cards loaded by loadConfigBot never reach "
        "evo's context — defeats the whole point of the pack."
    )
    # Some model-facing surfacing must remain. We accept either a
    # nested ``bot_detail: {...}`` field in the summary OR a flattened
    # ``bot_id`` + per-field keys; both let the model cite the data.
    assert (
        "summary.bot_detail" in window
        or "bot_detail:" in window
        or "bot_id" in window
    ), (
        "the 'settings' pack reads bot_detail from the snapshot but no "
        "longer exposes it on the summary returned to the proxy. "
        "Without a model-facing surfacing key, evo has nothing to cite."
    )


def test_settings_pack_points_at_config_bot_tool(html: str):
    """The pack's tool_pointers must signpost
    ``pod_state(query="config_bot", bot_id=...)`` — that's the
    fetch-on-demand path for any field the rendered cards elide (e.g.
    specific tool grants, hook config). Without the pointer the model
    has no fallback when an operator asks about a detail the snapshot
    didn't capture."""
    packs_start = html.find("const _EVO_CONTEXT_PACKS")
    assert packs_start > 0
    start = html.find("'settings':", packs_start)
    assert start > 0
    window = html[start:start + 5000]
    assert 'pod_state(query="config_bot"' in window, (
        "the 'settings' pack no longer references "
        'pod_state(query="config_bot") in its '
        "tool_pointers. Without that fallback, evo has nothing to call "
        "when the snapshot elides a field the operator is asking about."
    )


def test_agents_md_bot_detail_row_present(agents_md: str):
    """The AGENTS.md page-tool map must still have a Bot-detail row.
    Without it, evo has no signpost from "operator on Pod Config →
    Bot" to ``pod_state(query="config_bot", bot_id=...)`` and may
    default to vague "I'd need to look that up" replies."""
    # The row format is loose — different rows mix the page name and
    # tools across one or two cells. Anchor on the two load-bearing
    # tokens that must both appear in the same line.
    found = False
    for line in agents_md.splitlines():
        low = line.lower()
        if "bot detail" in low and 'pod_state(query="config_bot"' in low:
            found = True
            break
    assert found, (
        "AGENTS.md no longer has a page-tool-map row pairing 'Bot detail' "
        'with `pod_state(query="config_bot")`. Either the row was deleted '
        "or the wording drifted — restore the pairing so the model has "
        "the signpost."
    )


def test_agents_md_bot_detail_row_mentions_pod_state_bots(agents_md: str):
    """The Bot-detail row should also signpost
    ``pod_state(query="bots")`` — the runtime side (chips / status /
    activity) pairs with ``pod_state(query="config_bot")`` (the static
    openclaw.json). Operators often ask questions that need both ("why
    is team_bot_a showing a scan_needed chip while its archetype says
    ...")."""
    found = False
    for line in agents_md.splitlines():
        low = line.lower()
        if "bot detail" in low and 'pod_state(query="bots"' in low:
            found = True
            break
    assert found, (
        "AGENTS.md's Bot-detail row no longer signposts "
        'pod_state(query="bots"). pod_state(query="config_bot") is the '
        'static config; pod_state(query="bots") is the live tile. '
        "Operators routinely need both for bot-detail questions."
    )
