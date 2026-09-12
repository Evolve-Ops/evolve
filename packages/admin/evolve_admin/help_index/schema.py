"""Help-index dataclasses.

Locked in internal/spec-primary-bot-interface-2026-05-14.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evolve_util import now_iso_offset as _utc_now_iso


INDEX_SCHEMA_VERSION = 1


@dataclass
class Doc:
    """One indexed help doc.

    ``doc_id`` is the filename stem (e.g. ``cost`` for ``docs/help/cost.md``;
    ``operator-runbook`` for the top-level operator-runbook). Stable across
    rebuilds so admin can reference it by name.

    ``summary`` is the first paragraph after the H1 title, capped at
    ~200 chars — enough for the table-of-contents sidebar and for
    keyword search to anchor on, short enough not to balloon the index.

    ``sha`` is a SHA-256 of the file body; used by retention / staleness
    checks. Not BM25 weighted.
    """

    doc_id: str
    title: str
    summary: str
    body: str
    path: str
    sha: str
    size: int
    category: str = "help"  # "help" | "operator" | "applications"

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "summary": self.summary,
            "body": self.body,
            "path": self.path,
            "sha": self.sha,
            "size": self.size,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Doc":
        return cls(
            doc_id=data["doc_id"],
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            body=data.get("body", ""),
            path=data.get("path", ""),
            sha=data.get("sha", ""),
            size=int(data.get("size", 0)),
            category=data.get("category", "help"),
        )


@dataclass
class Index:
    """Top-level index container."""

    docs: list[Doc] = field(default_factory=list)
    built_at: str = field(default_factory=_utc_now_iso)
    source_root: str = ""  # the docs/ dir the index was built from
    schema_version: int = INDEX_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "built_at": self.built_at,
            "source_root": self.source_root,
            "docs": [d.to_dict() for d in self.docs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Index":
        return cls(
            docs=[Doc.from_dict(d) for d in (data.get("docs") or [])],
            built_at=data.get("built_at", _utc_now_iso()),
            source_root=data.get("source_root", ""),
            schema_version=int(data.get("schema_version", INDEX_SCHEMA_VERSION)),
        )

    def by_id(self, doc_id: str) -> Doc | None:
        for d in self.docs:
            if d.doc_id == doc_id:
                return d
        return None
