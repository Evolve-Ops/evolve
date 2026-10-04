"""_merged_vocabulary — Static + dynamic domain vocabulary merge.

Phase 2 Layer 2 plumbing per the
2026-06-05 RSI design discussion.

The Phase 2 substrate (capability_gap_monitor, engagement_amplifier_
monitor, anti_domains parser) reads from a domain keyword vocabulary
to map observed conversation nouns to ``domain:*`` tags. v1 used a
hardcoded ``_DOMAIN_KEYWORDS`` dict in
``generators/app_suggester/observe.py``; v1.5 (#2194) widened that
dict to 15 domains. Both shipped as compile-time static vocab —
extending required a code change.

This module introduces the *dynamic* layer: a JSON file at
``{shared_dir}/vocabulary/dynamic.json`` that can hold additional
keyword → domain mappings (and seed catalog entries) added at runtime
by either:

  - The ``vocab_add`` operator CLI (manual, no LLM, available today)
  - The ``vocabulary_expander_monitor`` LLM-backed flow (opt-in,
    default off — wired by PR γ which hasn't shipped yet)

The merge result is **static ∪ dynamic** with dynamic taking
precedence on collision. The pattern monitors and the anti-domain
parser all call ``effective_keywords(shared_dir)`` instead of
importing ``_DOMAIN_KEYWORDS`` directly — so adding a runtime keyword
is enough to make the substrate recognize it, no daemon restart
required.

## File format

``{shared_dir}/vocabulary/dynamic.json``::

    {
      "schema_version": 1,
      "keywords": {
        "sourdough": {
          "domain": "domain:food",
          "added_at": "2026-06-05T12:00:00Z",
          "added_by": "manual",
          "rationale": "operator-supplied",
          "ttl_days": 90
        }
      },
      "catalog_seeds": [
        {
          "category": "...",
          "title": "...",
          "description": "...",
          "example_apps": [...],
          "tags": [...],
          "added_at": "...",
          "added_by": "..."
        }
      ]
    }

## TTL semantics

Dynamic keywords have a ``ttl_days`` field. ``effective_keywords()``
filters out entries whose ``added_at + ttl_days`` is in the past, so a
one-time conversational fad ("Wordle" in 2022) doesn't stay in the
vocabulary forever. Manual entries default to 365 days; LLM entries
default to 90.

## Fail-closed

A missing or malformed dynamic file → empty dynamic vocab → behavior
identical to pre-#2194 (static-only). No surprise regressions from
this module shipping.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json, now_iso_offset as _utc_now_iso

DYNAMIC_VOCAB_FILENAME = "dynamic.json"
PENDING_VOCAB_FILENAME = "pending.json"
SCHEMA_VERSION = 1

# Defaults when the dynamic entry doesn't specify
DEFAULT_TTL_DAYS_MANUAL = 365
DEFAULT_TTL_DAYS_LLM = 90


def _vocab_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "vocabulary"


def dynamic_path(shared_dir: Path) -> Path:
    return _vocab_dir(shared_dir) / DYNAMIC_VOCAB_FILENAME


def pending_path(shared_dir: Path) -> Path:
    return _vocab_dir(shared_dir) / PENDING_VOCAB_FILENAME


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────


def _empty_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "keywords": {},
        "catalog_seeds": [],
    }


def load_dynamic_state(shared_dir: Path) -> dict:
    """Read ``dynamic.json`` if present. Returns the empty state when
    the file is missing or malformed — fail-closed."""
    p = dynamic_path(shared_dir)
    if not p.exists():
        return _empty_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    # Defensive: ensure required shape
    if not isinstance(data.get("keywords"), dict):
        data["keywords"] = {}
    if not isinstance(data.get("catalog_seeds"), list):
        data["catalog_seeds"] = []
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


def _static_keywords() -> dict[str, str]:
    """Lazy import of app_suggester's static vocab so this module is
    importable in analyzer-only envs where the generators package
    hasn't loaded yet."""
    try:
        from generators.app_suggester.observe import _DOMAIN_KEYWORDS

        return dict(_DOMAIN_KEYWORDS)
    except ImportError:
        return {}


def _entry_is_live(entry: dict, *, now: datetime) -> bool:
    """True if the dynamic-vocab entry hasn't expired yet.

    Defensive parsing: malformed timestamp / ttl → treat as live,
    rather than silently dropping a real entry. The vocabulary file
    is operator-edited; we'd rather over-include than over-prune."""
    added_at = entry.get("added_at")
    ttl_days = entry.get("ttl_days")
    if not isinstance(added_at, str) or not isinstance(ttl_days, (int, float)):
        return True
    try:
        added_dt = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
        if added_dt.tzinfo is None:
            added_dt = added_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return added_dt + timedelta(days=int(ttl_days)) > now


def effective_keywords(
    shared_dir: Path | None,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return the merged keyword → domain map the pattern monitors
    should use. ``static ∪ dynamic`` with dynamic overriding on
    collision. When ``shared_dir`` is None, returns static only —
    useful for test harnesses + analyzer-only environments.

    TTL filter: dynamic entries past their ``added_at + ttl_days``
    are silently excluded.
    """
    merged = _static_keywords()
    if shared_dir is None:
        return merged
    now = now or datetime.now(timezone.utc)
    state = load_dynamic_state(Path(shared_dir))
    keywords = state.get("keywords") or {}
    if not isinstance(keywords, dict):
        return merged
    for kw, entry in keywords.items():
        if not isinstance(kw, str) or not isinstance(entry, dict):
            continue
        domain = entry.get("domain")
        if not isinstance(domain, str) or not domain.startswith("domain:"):
            continue
        if not _entry_is_live(entry, now=now):
            continue
        # Dynamic overrides static on collision — the operator (or
        # approved LLM proposal) wins over the compile-time default.
        merged[kw.lower()] = domain
    return merged


def effective_catalog_seeds(
    shared_dir: Path | None,
) -> list[dict]:
    """Return dynamic catalog seed entries. Pattern monitors merge
    these with the static ``app_suggester/catalog.json`` so a new
    dynamic domain (e.g. ``domain:gardening``) has at least one
    capability_gap_monitor-eligible catalog entry as soon as it's
    approved.

    Empty list when ``shared_dir`` is None or no dynamic file
    exists."""
    if shared_dir is None:
        return []
    state = load_dynamic_state(Path(shared_dir))
    seeds = state.get("catalog_seeds") or []
    if not isinstance(seeds, list):
        return []
    out: list[dict] = []
    for entry in seeds:
        if isinstance(entry, dict) and entry.get("category"):
            out.append(entry)
    return out


def effective_catalog(
    static_catalog_path: Path,
    shared_dir: Path | None,
) -> list[dict]:
    """Return ``static catalog ∪ dynamic catalog seeds`` — the
    single canonical loader both ``capability_gap_monitor`` and
    ``pod_capability_lift`` use so dynamic seeds added via the
    ``vocab_add`` CLI (or, once γ ships, the LLM expansion flow)
    are honored end-to-end.

    Empty list when both halves are unreadable. Dynamic seeds with
    the same ``category`` as a static entry win (operator override
    semantics — the dynamic add explicitly replaces the bundled
    default). Most dynamic seeds will be for brand-new categories
    that don't exist in the static file, so the collision case is
    rare in practice.
    """
    import json

    # Load static.
    try:
        data = json.loads(
            Path(static_catalog_path).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        data = []
    if not isinstance(data, list):
        data = []
    static_entries = [e for e in data if isinstance(e, dict)]

    # Pull dynamic seeds (or skip when no shared_dir).
    dynamic_entries = effective_catalog_seeds(shared_dir)
    if not dynamic_entries:
        return static_entries

    # Merge with dynamic overriding static on category collision.
    dynamic_categories = {
        e.get("category") for e in dynamic_entries if e.get("category")
    }
    return [
        e for e in static_entries
        if e.get("category") not in dynamic_categories
    ] + dynamic_entries


# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────


def _write_dynamic(path: Path, payload: dict) -> None:
    """Atomic JSON write plus the extras the vocab store needs: the
    ``vocabulary/`` parent may not exist on first write, and the file
    must stay readable by every monitor (0o644 — the writer may be the
    operator CLI, not the evolve user)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload, mode=0o644)


def add_keyword(
    shared_dir: Path,
    keyword: str,
    domain: str,
    *,
    added_by: str,
    rationale: str = "",
    ttl_days: int | None = None,
    added_at: datetime | None = None,
) -> None:
    """Add or refresh a dynamic keyword. ``added_by`` is "manual" /
    "llm-expansion" / "test-fixture" etc. — flows into the audit
    trail.

    ``added_at`` is an optional injectable clock. When omitted (the
    production default), the entry is stamped with the real UTC wall
    clock. Tests pass an explicit ``datetime`` so the ttl math is
    deterministic and immune to wall-clock drift — mirrors the
    ``now=`` injection that ``effective_keywords`` already supports.
    """
    keyword = keyword.strip().lower()
    if not keyword:
        raise ValueError("keyword must be non-empty")
    if not domain.startswith("domain:"):
        raise ValueError(f"domain must start with 'domain:'; got {domain!r}")
    if ttl_days is None:
        ttl_days = (
            DEFAULT_TTL_DAYS_LLM
            if added_by == "llm-expansion"
            else DEFAULT_TTL_DAYS_MANUAL
        )
    added_at_iso = (
        added_at.isoformat(timespec="seconds")
        if added_at is not None
        else _utc_now_iso()
    )
    state = load_dynamic_state(shared_dir)
    state["keywords"][keyword] = {
        "domain": domain,
        "added_at": added_at_iso,
        "added_by": added_by,
        "rationale": rationale,
        "ttl_days": int(ttl_days),
    }
    _write_dynamic(dynamic_path(shared_dir), state)


def add_catalog_seed(
    shared_dir: Path,
    *,
    category: str,
    title: str,
    description: str,
    example_apps: list[str],
    domain_tag: str,
    added_by: str,
) -> None:
    """Append a dynamic catalog seed entry. Required so new domains
    (via LLM expansion) have at least one capability_gap_monitor-
    eligible target — without a catalog entry, a domain keyword
    fires no gap proposals."""
    if not category:
        raise ValueError("category must be non-empty")
    if not domain_tag.startswith("domain:"):
        raise ValueError(
            f"domain_tag must start with 'domain:'; got {domain_tag!r}"
        )
    state = load_dynamic_state(shared_dir)
    # Dedup by category — re-adding the same seed updates it in place.
    state["catalog_seeds"] = [
        s for s in state["catalog_seeds"]
        if isinstance(s, dict) and s.get("category") != category
    ]
    state["catalog_seeds"].append({
        "category": category,
        "title": title,
        "description": description,
        "example_apps": list(example_apps),
        "tags": [domain_tag, "intent:explore"],
        "added_at": _utc_now_iso(),
        "added_by": added_by,
    })
    _write_dynamic(dynamic_path(shared_dir), state)


def remove_keyword(shared_dir: Path, keyword: str) -> bool:
    """Remove a dynamic keyword. Returns True if it was present."""
    keyword = keyword.strip().lower()
    state = load_dynamic_state(shared_dir)
    if keyword not in state["keywords"]:
        return False
    state["keywords"].pop(keyword)
    _write_dynamic(dynamic_path(shared_dir), state)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Static-vocab helpers (re-exported for monitors that don't need
# the dynamic layer but want a single import surface)
# ─────────────────────────────────────────────────────────────────────────────


def static_keywords() -> dict[str, str]:
    """Public alias for ``_static_keywords`` — used by callers that
    explicitly want the compile-time vocab without the dynamic layer
    (e.g. v1 backward-compat tests)."""
    return _static_keywords()
