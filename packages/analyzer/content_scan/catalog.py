"""content_scan.catalog — operator-curated pattern catalog.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md §3.1.

Catalog file lives at {shared_dir}/policy/content-scan-patterns.json.
Edits flow through UpdateContentScanCatalog proposals (one new
action kind) — operator can hand-edit but bypasses the audit trail.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def catalog_path(shared_dir: Path) -> Path:
    return shared_dir / "policy" / "content-scan-patterns.json"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Pattern:
    """One catalog pattern entry.

    ``kind`` selects the matcher: ``regex`` / ``html_comment`` /
    ``line_length`` / ``structural``. Other fields are kind-specific.
    """

    id: str
    kind: str  # "regex" | "html_comment" | "line_length" | "structural"
    description: str
    severity: str = "warn"  # info | warn | alert
    pattern: str = ""  # for kind=regex
    threshold: int = 0  # for kind=line_length (chars) or structural (min_size_bytes)
    min_size_bytes: int = 0  # for kind=structural
    applies_to: list[str] = field(default_factory=list)  # filename filter; empty = all scanned files
    flags: list[str] = field(default_factory=list)  # regex flags: ignorecase / multiline / dotall

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Scope:
    scanned_files_per_bot: list[str] = field(default_factory=list)
    scanned_pod_files: list[str] = field(default_factory=list)


@dataclass
class Catalog:
    version: int = 1
    deny_patterns: list[Pattern] = field(default_factory=list)
    evolve_markers_allowlist: list[str] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    bootstrapped_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "bootstrapped_at": self.bootstrapped_at,
            "deny_patterns": [p.to_dict() for p in self.deny_patterns],
            "evolve_markers_allowlist": list(self.evolve_markers_allowlist),
            "scope": asdict(self.scope),
        }

    def find_pattern(self, pattern_id: str) -> Pattern | None:
        for p in self.deny_patterns:
            if p.id == pattern_id:
                return p
        return None


# ── Read / write ──────────────────────────────────────────────────────────────

def load_with_status(shared_dir: Path) -> tuple[Catalog, str | None]:
    """Load the catalog, returning ``(catalog, error_str | None)``.

    ``error_str`` is non-None on read or parse failure so callers can
    distinguish a missing file (empty catalog, no error) from a corrupt
    one (empty catalog, error reported). Routes/UI use this to surface
    parse failures rather than silently rendering "no patterns" when the
    catalog file is broken.
    """
    path = catalog_path(shared_dir)
    if not path.exists():
        return Catalog(), None
    try:
        text = path.read_text()
    except OSError as exc:
        return Catalog(), f"os_error: {exc}"
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return Catalog(), f"json_decode: {exc.msg} (line {exc.lineno})"
    return _catalog_from_raw(raw), None


def load(shared_dir: Path) -> Catalog:
    """Convenience wrapper around ``load_with_status`` for callers that only
    need the catalog and treat any error as "empty catalog"."""
    cat, _ = load_with_status(shared_dir)
    return cat


def _catalog_from_raw(raw: dict) -> Catalog:
    """Parse a raw JSON dict into a Catalog. Caller has already done the
    file read + json.loads."""
    patterns = []
    for p in raw.get("deny_patterns") or []:
        if not isinstance(p, dict):
            continue
        patterns.append(Pattern(
            id=str(p.get("id") or ""),
            kind=str(p.get("kind") or ""),
            description=str(p.get("description") or ""),
            severity=str(p.get("severity") or "warn"),
            pattern=str(p.get("pattern") or ""),
            threshold=int(p.get("threshold") or 0),
            min_size_bytes=int(p.get("min_size_bytes") or 0),
            applies_to=list(p.get("applies_to") or []),
            flags=list(p.get("flags") or []),
        ))
    scope_raw = raw.get("scope") or {}
    return Catalog(
        version=int(raw.get("version") or 1),
        deny_patterns=[p for p in patterns if p.id and p.kind],
        evolve_markers_allowlist=list(raw.get("evolve_markers_allowlist") or []),
        scope=Scope(
            scanned_files_per_bot=list(scope_raw.get("scanned_files_per_bot") or []),
            scanned_pod_files=list(scope_raw.get("scanned_pod_files") or []),
        ),
        bootstrapped_at=str(raw.get("bootstrapped_at") or ""),
    )


def write(catalog: Catalog, shared_dir: Path) -> None:
    path = catalog_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = catalog.to_dict()
    payload["_comment"] = (
        "Content-scan pattern catalog. Phase A of "
        "docs/spec-prompt-injection-scanner-2026-05-10.md. Bootstrapped with "
        "starter patterns; edit via UpdateContentScanCatalog proposals or by "
        "hand (manual edits work but bypass the audit trail). The Evolve "
        "markers allowlist is load-bearing — without it the html_comment "
        "pattern fires on every bot because the evolve plugin uses "
        "<!-- evolve-handoff:* --> regions in AGENTS.md and POD_CONDUCT.md."
    )
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def write_default_if_missing(shared_dir: Path) -> bool:
    if catalog_path(shared_dir).exists():
        return False
    from .default_patterns import default_catalog
    write(default_catalog(), shared_dir)
    return True
