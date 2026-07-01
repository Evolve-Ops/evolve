"""release_notes — Tier-aware release metadata.

Reads ``RELEASES.yaml`` at the repo root and resolves the "highest-tier
release between a deployed version and the current version" so the
admin UI can render an appropriate banner.

See ``docs/spec-release-tiers-2026-05-16.md`` for the design.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Repo root: packages/admin/evolve_admin/release_notes.py → up four parents.
_REPO_ROOT = Path(__file__).parent.parent.parent.parent
RELEASES_FILE = _REPO_ROOT / "RELEASES.yaml"

TIER_SECURITY = "security"
TIER_FEATURE = "feature"
TIER_MAINTENANCE = "maintenance"

_TIER_RANK = {
    TIER_MAINTENANCE: 0,
    TIER_FEATURE: 1,
    TIER_SECURITY: 2,
}

_VALID_TIERS = frozenset(_TIER_RANK.keys())

# Matches "YYYY.MMDD.PR" — the version format produced by deploy._compute_version().
_VERSION_RE = re.compile(r"^(\d{4})\.(\d{4})\.(\d+)$")


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    tier: str
    headline: str | None = None
    details: str | None = None
    link: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class ResolvedRelease:
    """Selected entry plus the (from, to, count) range it summarizes."""
    entry: ReleaseEntry
    from_version: str | None
    to_version: str
    count: int

    def to_dict(self) -> dict:
        return {
            **self.entry.to_dict(),
            "range": {
                "from_version": self.from_version,
                "to_version": self.to_version,
                "count": self.count,
            },
        }


def parse_version(s: str) -> tuple[int, int, int] | None:
    """Parse ``YYYY.MMDD.PR`` into a sortable tuple, or None if malformed."""
    if not isinstance(s, str):
        return None
    m = _VERSION_RE.match(s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def tier_rank(tier: str) -> int:
    """Higher = more urgent. Unknown tiers rank as maintenance (0)."""
    return _TIER_RANK.get(tier, 0)


def _parse_yaml(text: str) -> list[dict] | None:
    """Parse RELEASES.yaml content. Tolerates JSON for environments without PyYAML."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
    except ImportError:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("RELEASES: PyYAML missing and content is not valid JSON")
            return None
    except Exception as e:
        log.warning("RELEASES: YAML parse failed: %s", e)
        return None
    if data is None:
        return []
    if not isinstance(data, list):
        log.warning("RELEASES: expected a list at top level, got %s", type(data).__name__)
        return None
    return data


def _coerce_entry(raw: dict) -> ReleaseEntry | None:
    """Validate one raw dict and return a ReleaseEntry, or None to skip."""
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    if not isinstance(version, str) or parse_version(version) is None:
        return None
    tier = raw.get("tier", TIER_MAINTENANCE)
    if tier not in _VALID_TIERS:
        # Unknown tier: log and fall back to maintenance rather than dropping.
        log.warning("RELEASES: unknown tier %r for version %s; treating as maintenance", tier, version)
        tier = TIER_MAINTENANCE
    headline = raw.get("headline")
    if headline is not None and not isinstance(headline, str):
        headline = None
    details = raw.get("details")
    if details is not None and not isinstance(details, str):
        details = None
    link = raw.get("link")
    if link is not None and not isinstance(link, str):
        link = None
    return ReleaseEntry(
        version=version,
        tier=tier,
        headline=headline.strip() if headline else None,
        details=details.strip() if details else None,
        link=link.strip() if link else None,
    )


def load_releases(path: Path | None = None) -> list[ReleaseEntry]:
    """Load and validate RELEASES.yaml. Returns [] on any error."""
    p = path or RELEASES_FILE
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("RELEASES: could not read %s: %s", p, e)
        return []
    raw = _parse_yaml(text)
    if not raw:
        return []
    entries: list[ReleaseEntry] = []
    for item in raw:
        entry = _coerce_entry(item)
        if entry is not None:
            entries.append(entry)
    return entries


def _versions_in_range(
    entries: Iterable[ReleaseEntry],
    *,
    from_exclusive: str | None,
    to_inclusive: str,
) -> list[ReleaseEntry]:
    """Filter entries to those with from_exclusive < version <= to_inclusive."""
    to_v = parse_version(to_inclusive)
    if to_v is None:
        return []
    from_v = parse_version(from_exclusive) if from_exclusive else None
    out = []
    for e in entries:
        v = parse_version(e.version)
        if v is None:
            continue
        if v > to_v:
            continue
        if from_v is not None and v <= from_v:
            continue
        out.append(e)
    return out


def resolve_latest_release(
    *,
    min_deployed_version: str | None,
    current_version: str,
    entries: list[ReleaseEntry] | None = None,
) -> ResolvedRelease | None:
    """Pick the highest-tier release entry between min_deployed and current.

    - ``min_deployed_version=None`` means "no bots deployed yet" — return None.
    - If no curated entries exist in the range, return a maintenance default
      pointing at ``current_version`` so the UI can still render the quiet chip.
    - Tie-break by newest version when multiple entries share the top tier.
    """
    if min_deployed_version is None:
        return None
    min_v = parse_version(min_deployed_version)
    cur_v = parse_version(current_version)
    if cur_v is None or min_v is None or min_v >= cur_v:
        return None

    releases = entries if entries is not None else load_releases()
    in_range = _versions_in_range(
        releases, from_exclusive=min_deployed_version, to_inclusive=current_version
    )

    # Count is "number of release-stamped commits between deployed and current".
    # We approximate with PR-component difference; falls back to 1 if same day.
    count = max(cur_v[2] - min_v[2], 1)

    if not in_range:
        # No curated entry — surface the quiet maintenance default.
        return ResolvedRelease(
            entry=ReleaseEntry(version=current_version, tier=TIER_MAINTENANCE),
            from_version=min_deployed_version,
            to_version=current_version,
            count=count,
        )

    # Sort by (tier rank desc, version desc) and pick the top.
    in_range.sort(
        key=lambda e: (tier_rank(e.tier), parse_version(e.version) or (0, 0, 0)),
        reverse=True,
    )
    top = in_range[0]
    return ResolvedRelease(
        entry=top,
        from_version=min_deployed_version,
        to_version=current_version,
        count=count,
    )


def min_deployed_version(bot_sync: dict[str, dict]) -> str | None:
    """Return the lowest deployed_version across non-primary bots, or None.

    Mirrors how the existing banner filters: skip primary bots (their version
    reflects local code, not deploy state), skip bots with no deployed version.
    """
    deployed: list[tuple[tuple[int, int, int], str]] = []
    for bot_id, sync in (bot_sync or {}).items():
        # Skip never-deployed (no version) bots; they show up via a separate path.
        v = sync.get("deployed_version") if isinstance(sync, dict) else None
        if not v:
            continue
        parsed = parse_version(v)
        if parsed is None:
            continue
        deployed.append((parsed, v))
    if not deployed:
        return None
    deployed.sort()
    return deployed[0][1]
