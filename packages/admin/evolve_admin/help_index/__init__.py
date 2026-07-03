"""Help-doc index built from in-tree markdown for grounded Q&A.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §4.

The index is a list of small per-doc records (id, title, summary, body,
path, sha, size). Built once at deploy time by ``evolve-admin
help-index build`` and stored at ``{shared_dir}/help_index.json``.

Search is BM25 over title+summary+body — no embeddings, no LLM in the
loop. See :mod:`evolve_admin.help_search`.

Public entry points:

  - :func:`build.build_index`   — scan in-scope docs, return Index
  - :func:`build.write_index`   — atomically write Index to disk
  - :func:`build.load_index`    — read Index from disk
  - :func:`schema.Doc`          — per-doc record
  - :func:`schema.Index`        — top-level container
"""

from . import build as build  # re-export
from .schema import Doc, Index, INDEX_SCHEMA_VERSION

__all__ = [
    "build",
    "Doc",
    "Index",
    "INDEX_SCHEMA_VERSION",
]
