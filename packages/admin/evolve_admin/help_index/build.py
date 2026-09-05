"""Scan in-tree markdown and produce a help index JSON.

Spec: internal/spec-primary-bot-interface-2026-05-14.md §4.1.

In-scope (per spec):
  - docs/help/*.md              — primary user-pitched corpus
  - docs/operator-runbook.md, getting-started.md, overview.md,
    configuration.md — operator-facing top-level docs
  - docs/applications.md, applications-vs-skills.md, manifest-spec.md
    — application-framework primer

Out of scope (per spec):
  - internal/spec-*.md, docs/archive/*, internal/research/*, internal/design/*
  - internal/postmortem-*.md, CLAUDE.md

The scope list is explicit, not glob-based, so adding a new help doc
requires a one-line edit. That's the right friction: the help corpus
is what the bot will ground answers from, and we want each addition
to be a conscious choice (cost/quality, not "every spec leaks here").
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .schema import Doc, Index


# ─────────────────────────────────────────────────────────────────────────────
# Scope
# ─────────────────────────────────────────────────────────────────────────────

# Explicit list of operator-facing docs outside docs/help/.
# Order is preserved in the index for stable iteration.
_OPERATOR_DOCS: tuple[tuple[str, str], ...] = (
    # (relative path under docs_root, category)
    ("operator-runbook.md", "operator"),
    ("getting-started.md", "operator"),
    ("overview.md", "operator"),
    ("configuration.md", "operator"),
    ("applications.md", "applications"),
    ("applications-vs-skills.md", "applications"),
    ("manifest-spec.md", "applications"),
)


SUMMARY_MAX_CHARS = 200


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────


def parse_doc(text: str, *, doc_id: str, path: str, category: str) -> Doc:
    """Build a :class:`Doc` from a raw markdown body.

    Title = first H1 line stripped of leading ``#`` and whitespace.
    Summary = first non-empty, non-heading, non-fence paragraph after
    the H1, condensed to one line and capped at ``SUMMARY_MAX_CHARS``.
    Body = the markdown as-is (search hits over the body return real
    snippets, not synthesized excerpts).
    """
    title = _first_h1(text) or doc_id
    summary = _first_paragraph(text)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Doc(
        doc_id=doc_id,
        title=title,
        summary=summary,
        body=text,
        path=path,
        sha=sha,
        size=len(text),
        category=category,
    )


_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```")
_HEADING = re.compile(r"^#{1,6}\s")


def _first_h1(text: str) -> str | None:
    m = _H1.search(text)
    if not m:
        return None
    return m.group(1).strip()


def _first_paragraph(text: str) -> str:
    """Return the first prose paragraph after the H1, capped.

    Skips fenced code blocks and subsequent headings. Internal newlines
    are collapsed so the summary renders as a single line in tool
    output.
    """
    in_fence = False
    found_h1 = False
    para: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if _FENCE.match(line):
            in_fence = not in_fence
            if para:
                break
            continue
        if in_fence:
            continue
        if not found_h1:
            if _H1.match(line):
                found_h1 = True
            continue
        if not line.strip():
            if para:
                break
            continue
        if _HEADING.match(line):
            if para:
                break
            continue
        para.append(line.strip())

    joined = " ".join(para).strip()
    if len(joined) > SUMMARY_MAX_CHARS:
        joined = joined[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return joined


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────


def build_index(docs_root: Path) -> Index:
    """Walk the in-scope set under ``docs_root`` and return an :class:`Index`.

    Sort order: ``docs/help/*`` first (alpha), then the operator/
    applications curated list in spec order. Stable.

    Missing files are skipped silently — partial trees (e.g. during
    cleanup work) shouldn't crash the build, just produce a smaller
    index.
    """
    docs_root = Path(docs_root)
    docs: list[Doc] = []

    # 1. docs/help/*.md (alphabetical)
    help_dir = docs_root / "help"
    if help_dir.exists():
        for p in sorted(help_dir.glob("*.md")):
            doc = _load_doc(p, docs_root, category="help")
            if doc is not None:
                docs.append(doc)

    # 2. Operator + applications curated list (spec order)
    for rel, category in _OPERATOR_DOCS:
        p = docs_root / rel
        if not p.exists():
            continue
        doc = _load_doc(p, docs_root, category=category)
        if doc is not None:
            docs.append(doc)

    return Index(docs=docs, source_root=str(docs_root))


def _load_doc(p: Path, docs_root: Path, *, category: str) -> Doc | None:
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    doc_id = _doc_id_for(p, category=category)
    rel_path = str(p.relative_to(docs_root.parent)) if docs_root.parent in p.parents else str(p)
    return parse_doc(text, doc_id=doc_id, path=rel_path, category=category)


def _doc_id_for(p: Path, *, category: str) -> str:
    """Build a stable, collision-free doc id.

    ``help/cost.md``         → ``help/cost``
    ``operator-runbook.md``  → ``operator/operator-runbook``
    ``manifest-spec.md``     → ``applications/manifest-spec``

    Always category-prefixed because the live tree has real
    collisions: ``docs/help/overview.md`` and ``docs/overview.md`` both
    exist, both stem to ``overview``. Without the prefix, ``Index.by_id``
    silently returns whichever sorts first — different docs depending
    on iteration order, with no error to the caller.

    Bots get the doc id back from ``evolve_help_search`` and pass it
    straight to ``evolve_help_read``, so the prefix never has to be
    constructed from scratch.
    """
    return f"{category}/{p.stem}"


# ─────────────────────────────────────────────────────────────────────────────
# IO
# ─────────────────────────────────────────────────────────────────────────────


def index_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "help_index.json"


def write_index(index: Index, shared_dir: Path) -> Path:
    """Atomically write the index JSON to ``{shared_dir}/help_index.json``.

    Same temp-file + ``os.replace`` pattern as :mod:`signals.store` —
    mode 0o644 so other-user readers (admin UI, plugin tool calls
    proxied via the admin server) aren't broken.
    """
    path = index_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(index.to_dict(), fh, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_index(shared_dir: Path) -> Index | None:
    """Read the on-disk index, or ``None`` if absent/unreadable.

    Callers should treat ``None`` as "no help available" — the spec
    explicitly allows the bot to say "I don't have a doc on that" in
    this case, rather than failing the conversation.
    """
    path = index_path(shared_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Index.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return None
