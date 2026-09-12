"""HTTP routes for the in-app Help corpus viewer.

Renders the public ``docs/help/*.md`` knowledge corpus inside the admin SPA so
operators can READ the help topics without leaving the pod. Works offline /
on loopback-only pods: the bundled local markdown is rendered server-side —
nothing iframes or links out to the external GitHub Pages site.

    GET /api/help/topics          → section-grouped list of public topics
    GET /api/help/topic/<slug>    → rendered HTML + metadata for one topic

Rendering reuses ``tools/build_help_site.py``'s pure-Python corpus helpers
(``SECTIONS`` ordering + frontmatter ``parse_doc``) so the in-app index matches
the GitHub Pages index and auto-picks-up new topics — and new ``SECTIONS``
entries a sibling PR adds — without a code change here.

The markdown→HTML step prefers ``build_help_site.render_body`` when the
``markdown`` package is importable (dev / CI), and falls back to the admin
server's own offline ``_markdown_to_html`` otherwise. ``markdown`` is a
*build-time* dependency, NOT part of the admin runtime's locked deps (see
``uv.lock`` / the package ``pyproject.toml``), so we never hard-require it at
request time: ``build_help_site`` hard-imports it at module top and
``sys.exit()``s when it's missing, which would kill the request — we seed a
stub so the import succeeds and reuse only its ``markdown``-free helpers on
that path.

Extracted as its own ``register_*`` module so ``server.py`` (under a no-growth
line cap) doesn't grow.
"""

from __future__ import annotations

import re
from dataclasses import replace as _dc_replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import Flask, Response, jsonify

from ..telemetry import get_logger

_log = get_logger("web.routes_help_docs")

# Resolved once per process: (build_help_site_module_or_None, has_real_markdown).
# The corpus dir + tools dir don't move under a running daemon, and importing
# build_help_site execs a module — cache to keep the routes cheap. The module
# slot is ``Any``: it's an importlib-loaded module whose duck-typed helpers
# (parse_doc / render_body / SECTIONS) we access dynamically.
_BUILDER_CACHE: tuple[Any, bool] | None = None


def _resolve_corpus_dir() -> Path:
    """``docs/help`` under whichever checkout the admin code is loaded from.

    Reuses the server's checkout-aware ``_resolve_docs_dir`` (prefers the
    deploy checkout, else walks up) so the corpus path tracks the running
    code regardless of dev-clone vs deploy-checkout layout. Imported lazily
    to avoid any cold cross-module import ordering with ``server``.
    """
    from .server import _resolve_docs_dir

    return _resolve_docs_dir() / "help"


def _load_help_builder() -> tuple[Any, bool]:
    """Import ``tools/build_help_site`` for its pure-Python corpus helpers.

    Returns ``(module_or_None, has_real_markdown)``. ``build_help_site``
    hard-imports ``markdown`` at module top and ``sys.exit()``s if it's
    absent; ``markdown`` is not in the admin runtime's locked deps, so on a
    minimal pod that import would sink the request. We therefore seed a stub
    ``markdown`` module when the real one is missing (we only touch the
    module's ``markdown``-free helpers in that case) and report whether the
    real library was present so the caller can pick the renderer.
    """
    global _BUILDER_CACHE
    if _BUILDER_CACHE is not None:
        return _BUILDER_CACHE
    import importlib
    import importlib.util
    import sys
    import types

    try:
        has_md = importlib.util.find_spec("markdown") is not None
    except Exception:  # pragma: no cover — pathological import machinery
        has_md = False
    if not has_md and "markdown" not in sys.modules:
        # Minimal stand-in so build_help_site's top-level ``import markdown``
        # succeeds; ``render_body()`` is never called on this path.
        sys.modules["markdown"] = types.ModuleType("markdown")

    tools_dir = _resolve_corpus_dir().parent.parent / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    builder: Any
    try:
        builder = importlib.import_module("build_help_site")
    except (Exception, SystemExit):  # degrade to the offline path, never 500
        _log.warning("help corpus: build_help_site unavailable; offline render")
        builder = None
    _BUILDER_CACHE = (builder, has_md)
    return _BUILDER_CACHE


def _parse_doc_local(path: Path) -> SimpleNamespace:
    """Frontmatter + body parse for the rare case build_help_site won't import.

    Mirrors ``build_help_site.parse_doc`` (yaml is a locked dep; markdown is
    not). Returns a duck-typed stand-in for ``HelpDoc``.
    """
    import yaml

    raw = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, flags=re.DOTALL)
    if not m:
        raise ValueError(f"{path.name}: missing YAML frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    return SimpleNamespace(
        slug=fm["slug"],
        title=fm["title"],
        audience=fm.get("audience", "internal"),
        last_reviewed=str(fm.get("last_reviewed", "")),
        status=fm.get("status", "active"),
        concepts=list(fm.get("concepts") or []),
        body_md=m.group(2).lstrip("\n"),
    )


def _iter_public_docs(builder: Any) -> dict[str, Any]:
    """slug → parsed doc for every ``audience: public`` topic in the corpus.

    Auto-discovers via glob (no hardcoded list); skips the README and the
    underscore-prefixed registry/index files (``_index.yaml`` etc. aren't
    ``.md``, but guard defensively). A single malformed doc is skipped, not
    fatal to the whole list.
    """
    corpus = _resolve_corpus_dir()
    docs: dict[str, object] = {}
    if not corpus.is_dir():
        return docs
    parse = getattr(builder, "parse_doc", None) if builder is not None else None
    for path in sorted(corpus.glob("*.md")):
        if path.name == "README.md" or path.name.startswith("_"):
            continue
        try:
            doc = parse(path) if parse is not None else _parse_doc_local(path)
        except Exception as e:  # noqa: BLE001 — one bad doc must not 500 the list
            _log.warning("help corpus: skipping %s (%s)", path.name, e)
            continue
        if getattr(doc, "audience", "internal") != "public":
            continue
        docs[doc.slug] = doc
    return docs


def _strip_help_prefix(title: str) -> str:
    return re.sub(r"^Help:\s*", "", title or "")


def _first_paragraph(md_text: str) -> str:
    """First non-heading paragraph, flattened to plain prose (for list cards)."""
    for block in (md_text or "").split("\n\n"):
        block = block.strip()
        if not block or block.startswith("#") or block.startswith("---"):
            continue
        text = re.sub(r"`([^`]+)`", r"\1", block)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        return (text[:237].rstrip() + "…") if len(text) > 240 else text
    return ""


def _topic_summary(doc: Any) -> dict:
    return {
        "slug": doc.slug,
        "title": _strip_help_prefix(doc.title),
        "summary": _first_paragraph(getattr(doc, "body_md", "")),
        "status": getattr(doc, "status", "active") or "active",
        "concepts": list(getattr(doc, "concepts", []) or [])[:6],
    }


# In-corpus cross-links: rewrite ``[text](slug.md|slug.html)`` to a sentinel
# href both renderers preserve as an anchor (``_markdown_to_html`` keeps
# ``/``-rooted hrefs; the markdown lib keeps any href), then mark them at the
# tag level for the client to rewire to an in-app topic open. Keeps the
# reading pane self-contained — no link-out, no dead ``.html`` href.
def _prerewrite_links(body_md: str, public_slugs: set[str]) -> str:
    def repl(m: re.Match) -> str:
        text, slug = m.group(1), m.group(2)
        return f"[{text}](/__help/{slug})" if slug in public_slugs else m.group(0)

    return re.sub(r"\[([^\]]+)\]\(([\w-]+)\.(?:md|html)(?:#[^)]*)?\)", repl, body_md)


def _postprocess_html(html_text: str) -> str:
    html_text = re.sub(
        r'<a href="/__help/([\w-]+)"(?:\s+target="_blank"\s+rel="noopener")?>',
        r'<a href="#" class="help-xref" data-help-slug="\1">',
        html_text,
    )
    # Drop a leading "Help: " inside the first <h1> — build_help_site's render
    # path already does this; the offline fallback doesn't.
    return re.sub(r"(<h1[^>]*>)\s*Help:\s*", r"\1", html_text, count=1)


def _render_body_html(
    builder: Any, has_md: bool, doc: Any, rewritten_body: str
) -> str:
    """Markdown → HTML for the reading pane.

    Prefers ``build_help_site.render_body`` when the real ``markdown`` lib is
    present; falls back to the server's offline ``_markdown_to_html`` (which
    needs no third-party dep) so a minimal pod still renders.
    """
    if builder is not None and has_md:
        try:
            tmp = _dc_replace(doc, body_md=rewritten_body)  # doc is a HelpDoc
            return builder.render_body(tmp)
        except Exception as e:  # noqa: BLE001 — fall through to offline render
            _log.warning("help corpus: render_body failed (%s); offline render", e)
    from .server import _markdown_to_html

    return _markdown_to_html(rewritten_body)


def register_help_docs_routes(app: Flask, network_path: Path) -> None:
    """Register the in-app Help corpus viewer endpoints."""

    @app.get("/api/help/topics")
    def api_help_topics() -> Response:
        """Public help topics, grouped + ordered to match the Pages index."""
        builder, _has_md = _load_help_builder()
        docs = _iter_public_docs(builder)
        sections_def = list(getattr(builder, "SECTIONS", []) or []) if builder else []

        out_sections: list[dict] = []
        used: set[str] = set()
        for key, title, slugs in sections_def:
            topics = []
            for slug in slugs:
                doc = docs.get(slug)
                if doc is None:
                    continue
                used.add(slug)
                topics.append(_topic_summary(doc))
            if topics:
                out_sections.append({"key": key, "title": title, "topics": topics})

        # Public docs not claimed by any section land in "Other" (mirrors the
        # Pages index's orphan handling) so a new topic is never invisible.
        orphans = sorted(s for s in docs if s not in used)
        if orphans:
            out_sections.append(
                {
                    "key": "other",
                    "title": "Other",
                    "topics": [_topic_summary(docs[s]) for s in orphans],
                }
            )
        return jsonify({"sections": out_sections, "count": len(docs)})

    @app.get("/api/help/topic/<slug>")
    def api_help_topic(slug: str) -> Response:
        """Rendered HTML + metadata for one public topic (404 otherwise)."""
        builder, has_md = _load_help_builder()
        docs = _iter_public_docs(builder)
        doc = docs.get(slug)
        if doc is None:
            resp = jsonify({"error": "unknown or non-public help topic", "slug": slug})
            resp.status_code = 404
            return resp
        rewritten = _prerewrite_links(getattr(doc, "body_md", ""), set(docs))
        html_out = _postprocess_html(
            _render_body_html(builder, has_md, doc, rewritten)
        )
        return jsonify(
            {
                "slug": doc.slug,
                "title": _strip_help_prefix(doc.title),
                "last_reviewed": getattr(doc, "last_reviewed", "") or "",
                "status": getattr(doc, "status", "active") or "active",
                "concepts": list(getattr(doc, "concepts", []) or [])[:8],
                "html": html_out,
            }
        )
