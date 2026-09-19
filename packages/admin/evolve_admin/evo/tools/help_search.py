"""Help-corpus retrieval tools — ``evolve_help_search`` + ``evolve_help_read``.

These two read-only tools give evo (the admin/tray bot) a way to GROUND
"how does X work / what does this page do / where do I find Y / what is
<term>" answers in the in-tree help corpus (``docs/help/*.md`` +
curated operator/applications docs), instead of answering from generic
model knowledge — the confabulation risk the Help-page bias in
AGENTS.md fights.

The BM25 search + doc-read logic already exists and is shared with the
admin-UI HTTP routes ``POST /api/evo/help/search`` /
``POST /api/evo/help/read`` (``evolve_admin.web.evo_routes``). This
module is a thin tool-registry adapter over the SAME code:

  * ``evolve_admin.help_index.build.load_index`` reads the on-disk
    ``{shared_dir}/help_index.json`` (built at deploy time by
    ``deploy_shared_dir`` / the ``evolve-admin help-index build`` CLI).
  * ``evolve_admin.help_search.search`` runs BM25 over the loaded index.
  * ``Index.by_id`` resolves a ``help/<slug>`` doc id back to the full
    doc for ``evolve_help_read``.

No BM25 is reimplemented here — we import the same functions the routes
call so the tray and the HTTP surface can never diverge.

Spec: ``internal/spec-primary-bot-interface-2026-05-14.md`` §4 (help index,
BM25 search, the spec explicitly names ``evolve_help_search`` /
``evolve_help_read`` as the bot-facing surface). The HTTP routes landed
first; this closes the registry gap so the tray actually has the tool.

Index-absent handling: ``load_index`` returns ``None`` when the index
file hasn't been built (fresh pod, docs/ missing at deploy time). Both
tools return a clean ``available=False`` envelope in that case — never
a raised exception — so evo's model can say "I don't have a help doc on
that" rather than dropping a stack trace.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from evolve_admin import help_search as _help_search
from evolve_admin.help_index import build as _help_index_build

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Default / max number of hits returned by evolve_help_search. Mirrors
# the ``/api/evo/help/search`` route's ``k`` defaulting (default 3,
# capped at 10) so the tray and the HTTP surface behave identically.
_DEFAULT_LIMIT = 3
_MAX_LIMIT = 10

_NO_INDEX_NOTE = (
    "No help index is available on this pod yet. The index is built at "
    "deploy time (deploy_shared_dir) or via `evolve-admin help-index "
    "build`; if the daemon is fresh or docs/ was missing at deploy, the "
    "index hasn't been written. Answer from what you know and say you "
    "couldn't consult the help corpus — do not fabricate a citation."
)


def _coerce_limit(limit: Any) -> int:
    """Clamp the model-supplied ``limit`` to ``[1, _MAX_LIMIT]``.

    Non-int / missing → ``_DEFAULT_LIMIT``. Matches the route's
    defensive ``int(... )`` + clamp so a stray ``"3"`` or out-of-range
    value can't break the call.
    """
    try:
        k = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(k, _MAX_LIMIT))


def _search_handler(
    shared_dir: Path,
    query: str | None = None,
    limit: Any = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Run BM25 search over the help corpus and return ranked hits.

    Returns ``{"ok": True, "available": True, "hits": [...]}`` where each
    hit is ``{doc_id, title, summary, snippet}`` — ``summary`` is the
    doc's first-paragraph summary (good for a one-line description) and
    ``snippet`` is the windowed extract around the query match (good for
    grounding the actual answer). Pass a ``doc_id`` straight to
    ``evolve_help_read`` to pull the full body.

    Graceful degradation:
      * index not built → ``available=False`` + note (NOT an error).
      * empty/blank query → ``ok=False`` with a clear message.
    """
    index = _help_index_build.load_index(shared_dir)
    if index is None:
        return {"ok": True, "available": False, "hits": [], "note": _NO_INDEX_NOTE}

    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "query is required"}

    k = _coerce_limit(limit)
    hits = _help_search.search(index, query, k=k)

    out: list[dict[str, Any]] = []
    for h in hits:
        # ``Hit`` carries doc_id/title/snippet/score/path but not the
        # doc summary — look it up so callers get a stable one-line
        # description alongside the match snippet.
        doc = index.by_id(h.doc_id)
        out.append({
            "doc_id": h.doc_id,
            "title": h.title,
            "summary": doc.summary if doc is not None else "",
            "snippet": h.snippet,
        })
    return {"ok": True, "available": True, "hits": out}


def _read_handler(
    shared_dir: Path,
    doc_id: str | None = None,
) -> dict[str, Any]:
    """Read one help doc by id and return its title + full body.

    ``doc_id`` is exactly what ``evolve_help_search`` returns — the
    ``help/<slug>`` (or ``operator/<slug>`` / ``applications/<slug>``)
    form minted by ``help_index.build._doc_id_for``. Pass it through
    verbatim; do not reconstruct it.

    Graceful degradation:
      * index not built → ``available=False`` + note (NOT an error).
      * blank doc_id → ``ok=False`` with a clear message.
      * unknown doc_id → ``found=False`` (NOT an error) so the model can
        re-search rather than treating it as a failure.
    """
    index = _help_index_build.load_index(shared_dir)
    if index is None:
        return {
            "ok": True,
            "available": False,
            "found": False,
            "note": _NO_INDEX_NOTE,
        }

    if not isinstance(doc_id, str) or not doc_id.strip():
        return {"ok": False, "error": "doc_id is required"}

    doc = index.by_id(doc_id.strip())
    if doc is None:
        return {
            "ok": True,
            "found": False,
            "error": (
                f"no help doc with id {doc_id.strip()!r} — call "
                "evolve_help_search to get a valid doc_id"
            ),
        }
    return {
        "ok": True,
        "found": True,
        "doc_id": doc.doc_id,
        "title": doc.title,
        "body": doc.body,
        "path": doc.path,
        "category": doc.category,
    }


# ─── Tool definitions ────────────────────────────────────────────────────────


SEARCH_TOOL = Tool(
    name="evolve_help_search",
    description=(
        "Search the Evolve help corpus (the published docs behind the "
        "admin Help page) and return ranked matches. Call this FIRST for "
        "any 'how does X work / what does this page do / where do I find "
        "Y / what is <term>' question — on ANY page, especially the Help "
        "page — then read the top hit with evolve_help_read and ground "
        "your answer in that doc instead of generic knowledge. Each hit "
        "is {doc_id, title, summary, snippet}: pass doc_id to "
        "evolve_help_read for the full body. Returns available=false with "
        "a note (not an error) when the help index hasn't been built yet "
        "on this pod — then say you couldn't consult the corpus rather "
        "than fabricating a citation."
    ),
    wire_description=(
        "Search the Evolve help corpus; returns ranked hits {doc_id, "
        "title, summary, snippet}. Call FIRST for any 'how does X "
        "work / what is <term>' question, then read the top hit with "
        "evolve_help_read and ground the answer in that doc, not "
        "generic knowledge. If available=false (index not built), say "
        "you couldn't consult the corpus — never fabricate a citation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text search string — the operator's question or "
                    "its key terms (e.g. 'how do cost caps work', "
                    "'manifest spec', 'rollback')."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Optional — max hits to return (default 3, capped at "
                    "10)."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=_search_handler,
    risk_tier=RiskTier.READ,            # pure read; no validate, no confirm gate
    tags=("help", "search", "grounding"),
    # Conservative default — admin-only. The corpus is published help
    # content (no secrets/PII), so a future PR could open this to
    # cross-bot callers; until then it stays gated like every other
    # default tool. The primary caller is the admin tray (admin_ui).
    authorization_scope="admin",
)


READ_TOOL = Tool(
    name="evolve_help_read",
    description=(
        "Read one help doc by id and return its title + full markdown "
        "body. The doc_id is exactly what evolve_help_search returns "
        "(format help/<slug>, operator/<slug>, or applications/<slug>) — "
        "pass it through verbatim. Use this after evolve_help_search to "
        "ground a how-it-works / what-is answer in the actual doc text "
        "(cite the title). Returns found=false (not an error) for an "
        "unknown id so you can re-search; available=false with a note "
        "when the help index hasn't been built on this pod yet."
    ),
    wire_description=(
        "Read one help doc by id (title + full markdown body). Pass "
        "the doc_id from evolve_help_search through verbatim "
        "(help/<slug>, operator/<slug>, applications/<slug>). Use "
        "after evolve_help_search to ground the answer in the doc "
        "text and cite the title. found=false means unknown id — "
        "re-search rather than treating it as an error."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "doc_id": {
                "type": "string",
                "description": (
                    "The doc id from evolve_help_search, e.g. "
                    "'help/cost' or 'operator/operator-runbook'."
                ),
            },
        },
        "required": ["doc_id"],
        "additionalProperties": False,
    },
    handler=_read_handler,
    risk_tier=RiskTier.READ,            # pure read; no validate, no confirm gate
    tags=("help", "read", "grounding"),
    authorization_scope="admin",
)


register(SEARCH_TOOL)
register(READ_TOOL)
