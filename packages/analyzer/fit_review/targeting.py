"""fit_review.targeting — the pure-Python targeting report (Bite 1).

Spec: docs/spec-fit-reviewer-2026-06-12.md §3.3 + §7 (Bite 1).

The targeting step is the **cheap gate** that runs *before* any LLM. It answers,
per bot, with **no model call**:

    "Given this bot's *declared* purpose and its observation tuples, is there a
     recurring user need that (a) crosses a support floor, (b) is aligned with
     what the bot is for, (c) is not already covered by an installed app, and
     (d) maps to a real, vetted gallery package — or is there nothing worth an
     expensive look?"

It is the discipline that prevents the "138 low-value items" failure mode
structurally, not stylistically (spec §0, §8):

  * **Purpose-anchored** — no *declared* purpose ⇒ no capability pass. Inferred
    purpose may frame targeting but never anchors an emission (spec §1.2, §8).
  * **Support floor** — a noun is a candidate only if
    ``distinct_sessions >= 3`` AND ``distinct_days >= 2``. Below the floor we
    emit nothing for that need. This is the cheap gate that makes "review the
    team PM bot" and "review the thin personal bot" diverge correctly.
  * **Bounded action space** — candidate domains are cross-referenced against the
    *real* gallery ``application_tags`` (14 vetted apps), so a target always
    names a real ``pkg_id``. A candidate that clears the floor but maps to no
    gallery app yields **zero** real targets (the honest Phase-2 Forge trigger,
    spec §4), never a forced bad match.

What this module deliberately does NOT do (later bites — spec §7):
  * No LLM reflection (Bite 3).
  * No Proposal / InstallApp emission (Bite 4).
  * No transcript / cite-or-don't read (Bite 3+). The tuples are
    **targeting-grade, not citation-grade** — they say *where* to look, not
    *what to quote*. ``engagement`` is uniformly 1 in the live store, a dead
    axis; we rank on ``distinct_sessions`` × ``distinct_days`` ×
    ``frustration_share``, never on engagement (spec §1.1).

Design: the core builder ``build_targeting_report`` is I/O-free — tuples,
catalog, and installed-coverage are injected, so it is unit-testable without a
live shared dir. ``build_targeting_report_for_bot`` is the thin wrapper that
wires the real readers (observation window, gallery catalog, installed
manifests).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from fit_review.archetypes import aligned_domains_for

# ─────────────────────────────────────────────────────────────────────────────
# Constants — the support floor (spec §3.3.3) and the emergent bar.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_WINDOW_DAYS = 60  # spec §3.3.1

# The support floor. BOTH must hold for a noun to be a candidate.
SUPPORT_FLOOR_SESSIONS = 3
SUPPORT_FLOOR_DAYS = 2

# A noun that is NOT in the archetype's playbook (``emergent``) can still be a
# candidate, but only if it is *strongly* recurring — a high bar, so a thin /
# off-purpose bot (the negative control) never manufactures a candidate from
# incidental activity. Note the gallery-match boundedness is the real brake: an
# emergent noun with no gallery app yields no real target regardless.
STRONG_EMERGENT_SESSIONS = 8

# Meta / platform nouns that describe the user operating *Evolve itself* through
# the bot (e.g. inspecting the admin system during development), not a
# user-facing capability the bot should acquire. They are never candidates —
# they reflect observability of the dev process, not the bot's purpose domain,
# and map to no gallery app. Excluding them keeps the report purpose-anchored
# and hardens the thin-bot negative control (these dominate a lightly-used bot's
# tuples precisely because it was poked at during setup). This is a closed,
# code-reviewed set — not runtime-evolving.
META_NOUNS: frozenset[str] = frozenset({"evolve-system"})

# Mood vocabulary partition (must match schema.observation.MOOD_VOCABULARY).
_FRUSTRATED_MOOD = "frustrated"


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class NounRollup:
    """Per-noun rollup over the window. The ``noun`` axis is the activity
    domain; ``verb``/``mood`` are summarized, raw text is never present."""

    noun: str
    tuple_count: int
    distinct_sessions: int
    distinct_days: int
    frustration_share: float
    top_verbs: list[tuple[str, int]]  # (verb, count), descending
    alignment: str  # "confirmed" (purpose-aligned) | "emergent"
    above_floor: bool  # distinct_sessions >= floor AND distinct_days >= floor
    covered: bool  # an installed app already covers this domain
    is_candidate: bool  # eligible to drive a gallery shortlist

    def to_dict(self) -> dict[str, Any]:
        return {
            "noun": self.noun,
            "tuple_count": self.tuple_count,
            "distinct_sessions": self.distinct_sessions,
            "distinct_days": self.distinct_days,
            "frustration_share": round(self.frustration_share, 3),
            "top_verbs": [list(v) for v in self.top_verbs],
            "alignment": self.alignment,
            "above_floor": self.above_floor,
            "covered": self.covered,
            "is_candidate": self.is_candidate,
        }


@dataclass
class GalleryMatch:
    """A real gallery app whose ``application_tags`` intersect the bot's
    candidate domains. ``pkg_id`` is a *real* installable id — this is the
    contrast with ``app_suggester``'s illustrative slugs (spec §1.3)."""

    pkg_id: str
    name: str
    application_tags: list[str]
    matched_domains: list[str]  # candidate domains this app covers
    coverage_count: int  # len(matched_domains)
    weight: int  # Σ tuple_count of the matched domains (ranking signal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pkg_id": self.pkg_id,
            "name": self.name,
            "application_tags": list(self.application_tags),
            "matched_domains": list(self.matched_domains),
            "coverage_count": self.coverage_count,
            "weight": self.weight,
        }


# Decision codes for the report (spec §3.3, §4, §6).
DECISION_TARGETS_FOUND = "targets_found"  # ≥1 candidate maps to a real pkg_id
DECISION_NO_CANDIDATE = "no_candidate"  # nothing cleared the support floor
DECISION_GAP_NO_GALLERY = "gap_no_gallery_match"  # candidate, but no vetted app (Forge trigger)
DECISION_NO_PURPOSE = "no_purpose"  # no declared purpose ⇒ capability pass must not run


@dataclass
class TargetingReport:
    """The auditable "why did the reviewer look here?" artifact (spec §3.3).

    It is useful on its own and is the first buildable bite precisely because it
    needs no LLM and proves the substrate is sufficient.
    """

    bot_id: str
    window_days: int
    has_declared_purpose: bool
    archetype: str | None
    mission: str | None
    decision: str
    reason: str
    rollups: list[NounRollup]  # all nouns, descending by tuple_count
    candidates: list[NounRollup]  # the surviving above-floor candidate nouns
    shortlist: list[GalleryMatch]  # ranked real-pkg_id gallery matches
    generated_at: str | None = None  # caller-stamped (ISO8601), optional

    @property
    def primary(self) -> GalleryMatch | None:
        """The single best real-pkg_id target, or None. The eventual reflection
        chooses *among* the shortlist; this is the deterministic lead pick."""
        return self.shortlist[0] if self.shortlist else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "fit_review_targeting_report",
            "bot_id": self.bot_id,
            "window_days": self.window_days,
            "has_declared_purpose": self.has_declared_purpose,
            "archetype": self.archetype,
            "mission": self.mission,
            "decision": self.decision,
            "reason": self.reason,
            "support_floor": {
                "distinct_sessions": SUPPORT_FLOOR_SESSIONS,
                "distinct_days": SUPPORT_FLOOR_DAYS,
            },
            "rollups": [r.to_dict() for r in self.rollups],
            "candidates": [c.noun for c in self.candidates],
            "shortlist": [g.to_dict() for g in self.shortlist],
            "primary_pkg_id": self.primary.pkg_id if self.primary else None,
            "generated_at": self.generated_at,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Tuple rollup helpers (pure)
# ─────────────────────────────────────────────────────────────────────────────


def _tuple_day(t: Any) -> str | None:
    """Calendar (UTC) day of a tuple's start, as ``YYYY-MM-DD``, or None."""
    raw = getattr(t, "timestamp_start", "") or ""
    if not raw:
        return None
    # The day prefix is stable across the ISO shapes the store writes; avoid a
    # full parse since we only need the date for distinct-day counting.
    return raw[:10] if len(raw) >= 10 else None


def _roll_up_nouns(tuples: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Accumulate per-noun stats from a stream of ObservationTuples.

    Returns ``{noun: {tuple_count, sessions:set, days:set, frustrated:int,
    verbs:Counter}}``. Streams once; tolerant of missing fields.
    """
    acc: dict[str, dict[str, Any]] = {}
    for t in tuples:
        noun = (getattr(t, "noun", "") or "").strip().lower()
        if not noun:
            continue
        bucket = acc.get(noun)
        if bucket is None:
            bucket = {
                "tuple_count": 0,
                "sessions": set(),
                "days": set(),
                "frustrated": 0,
                "verbs": Counter(),
            }
            acc[noun] = bucket
        bucket["tuple_count"] += 1
        sid = getattr(t, "session_id", None)
        if sid:
            bucket["sessions"].add(sid)
        day = _tuple_day(t)
        if day:
            bucket["days"].add(day)
        if getattr(t, "mood", None) == _FRUSTRATED_MOOD:
            bucket["frustrated"] += 1
        verb = getattr(t, "verb", None)
        if verb:
            bucket["verbs"][verb] += 1
    return acc


# ─────────────────────────────────────────────────────────────────────────────
# Core builder (I/O-free — inject tuples / catalog / coverage)
# ─────────────────────────────────────────────────────────────────────────────


def build_targeting_report(
    *,
    bot_id: str,
    purpose: dict[str, Any] | None,
    tuples: Iterable[Any],
    catalog: list[dict[str, Any]],
    covered_domains: Iterable[str] = (),
    installed_pkg_ids: Iterable[str] = (),
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> TargetingReport:
    """Compute the targeting report. Pure: all inputs are injected.

    Args:
      purpose: the bot's purpose dict (``bot_purpose.get_bot_purpose`` shape) or
        None. A capability pass requires a *declared* purpose (``captured ==
        "declared"`` with an archetype); inferred purpose frames the rollups but
        emits no candidates (spec §1.2, §8).
      tuples: an iterable of ObservationTuple (already window-filtered by the
        caller; ``window_days`` is recorded for provenance only).
      catalog: the gallery catalog (list of ``{pkg_id, name, application_tags,
        ...}`` dicts).
      covered_domains: domain tags already covered by the bot's installed apps.
      installed_pkg_ids: pkg_ids already installed (excluded from the shortlist).
    """
    archetype = None
    mission = None
    declared = False
    if isinstance(purpose, dict):
        archetype = (purpose.get("archetype") or "").strip().lower() or None
        mission = (purpose.get("mission") or "").strip() or None
        captured = purpose.get("captured", "declared")
        # Declared = operator-set anchor (confidence forced to 1.0 by
        # bot_purpose.normalize_purpose). Only a declared purpose may anchor a
        # capability emission.
        declared = bool(archetype) and captured == "declared"

    aligned = aligned_domains_for(archetype) if declared else frozenset()
    covered = {d.strip().lower() for d in covered_domains if d}
    installed = {p for p in installed_pkg_ids if p}

    # ── Roll up the tuples and build per-noun rows. ──────────────────────────
    acc = _roll_up_nouns(tuples)
    rollups: list[NounRollup] = []
    for noun, b in acc.items():
        distinct_sessions = len(b["sessions"])
        distinct_days = len(b["days"])
        tuple_count = b["tuple_count"]
        frustration_share = (b["frustrated"] / tuple_count) if tuple_count else 0.0
        top_verbs = sorted(b["verbs"].items(), key=lambda kv: (-kv[1], kv[0]))
        alignment = "confirmed" if noun in aligned else "emergent"
        above_floor = (
            distinct_sessions >= SUPPORT_FLOOR_SESSIONS
            and distinct_days >= SUPPORT_FLOOR_DAYS
        )
        is_covered = noun in covered
        # Candidate gate: declared purpose required; not a meta/platform noun;
        # above floor; not already covered; AND either purpose-aligned
        # (confirmed) or strongly emergent.
        is_candidate = (
            declared
            and noun not in META_NOUNS
            and above_floor
            and not is_covered
            and (
                alignment == "confirmed"
                or distinct_sessions >= STRONG_EMERGENT_SESSIONS
            )
        )
        rollups.append(
            NounRollup(
                noun=noun,
                tuple_count=tuple_count,
                distinct_sessions=distinct_sessions,
                distinct_days=distinct_days,
                frustration_share=frustration_share,
                top_verbs=top_verbs,
                alignment=alignment,
                above_floor=above_floor,
                covered=is_covered,
                is_candidate=is_candidate,
            )
        )

    # Deterministic order: by tuple_count desc, then noun asc.
    rollups.sort(key=lambda r: (-r.tuple_count, r.noun))
    candidates = [r for r in rollups if r.is_candidate]

    # ── Cross-reference candidate domains against the real gallery. ──────────
    candidate_domains = {r.noun for r in candidates}
    weight_by_domain = {r.noun: r.tuple_count for r in candidates}
    shortlist = _gallery_shortlist(
        catalog, candidate_domains, weight_by_domain, installed
    )

    decision, reason = _decide(declared, archetype, candidates, shortlist)
    return TargetingReport(
        bot_id=bot_id,
        window_days=window_days,
        has_declared_purpose=declared,
        archetype=archetype,
        mission=mission,
        decision=decision,
        reason=reason,
        rollups=rollups,
        candidates=candidates,
        shortlist=shortlist,
    )


def _gallery_shortlist(
    catalog: list[dict[str, Any]],
    candidate_domains: set[str],
    weight_by_domain: dict[str, int],
    installed_pkg_ids: set[str],
) -> list[GalleryMatch]:
    """Rank gallery apps by how much of the bot's candidate need they cover.

    An app is on the shortlist iff at least one of its ``application_tags`` is a
    candidate domain and it is not already installed. Ranked by domains-covered
    (desc), then by Σ tuple_count of covered domains (desc), then name (asc) for
    a stable order. The lead pick is the app that covers the most of what the
    bot actually does.
    """
    if not candidate_domains:
        return []
    matches: list[GalleryMatch] = []
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        pkg_id = str(entry.get("pkg_id") or "")
        name = str(entry.get("name") or "")
        if not pkg_id or not name or pkg_id in installed_pkg_ids:
            continue
        tags = entry.get("application_tags") or []
        if not isinstance(tags, list):
            continue
        tag_set = {str(t).strip().lower() for t in tags if t}
        matched = sorted(tag_set & candidate_domains)
        if not matched:
            continue
        weight = sum(weight_by_domain.get(d, 0) for d in matched)
        matches.append(
            GalleryMatch(
                pkg_id=pkg_id,
                name=name,
                application_tags=[str(t) for t in tags],
                matched_domains=matched,
                coverage_count=len(matched),
                weight=weight,
            )
        )
    matches.sort(key=lambda m: (-m.coverage_count, -m.weight, m.name))
    return matches


def _decide(
    declared: bool,
    archetype: str | None,
    candidates: list[NounRollup],
    shortlist: list[GalleryMatch],
) -> tuple[str, str]:
    """Map the computed state to a decision code + human-readable reason."""
    if not declared:
        return (
            DECISION_NO_PURPOSE,
            "No declared purpose anchor — the capability pass does not run "
            "without one (inferred purpose may frame targeting but never "
            "anchors an emission).",
        )
    if not candidates:
        return (
            DECISION_NO_CANDIDATE,
            "No purpose-aligned noun cleared the support floor "
            f"(distinct_sessions >= {SUPPORT_FLOOR_SESSIONS}, "
            f"distinct_days >= {SUPPORT_FLOOR_DAYS}). The engine declines to "
            "fabricate a need when the substrate is thin.",
        )
    cand_names = ", ".join(c.noun for c in candidates)
    if not shortlist:
        return (
            DECISION_GAP_NO_GALLERY,
            f"Candidate need(s) cleared the floor ({cand_names}) but no vetted "
            "gallery app covers them — an evidenced gap (the honest Phase-2 "
            "Forge trigger), not a forced gallery match.",
        )
    lead = shortlist[0]
    return (
        DECISION_TARGETS_FOUND,
        f"Purpose-aligned need(s) above the floor ({cand_names}) map to "
        f"{len(shortlist)} real gallery app(s); lead target "
        f"{lead.name} ({lead.pkg_id}) covers "
        f"{', '.join(lead.matched_domains)}.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Wrapper — wires the real readers (the only I/O in this module)
# ─────────────────────────────────────────────────────────────────────────────


def build_targeting_report_for_bot(
    bot_id: str,
    *,
    network: dict[str, Any],
    shared_dir: Path,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    catalog: list[dict[str, Any]] | None = None,
) -> TargetingReport:
    """Assemble inputs from the real pod substrate and build the report.

    Reads:
      * purpose from ``network.json::bots[bot_id].purpose`` (via bot_purpose),
      * observation tuples for the window from the shared observation store,
      * installed-app coverage from ``{shared_dir}/applications/{bot_id}/``,
      * the gallery catalog (the 14 real apps) — injectable for tests.
    """
    from bot_purpose import get_bot_purpose

    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=window_days)

    purpose = get_bot_purpose(network, bot_id)
    tuples = list(_read_window_tuples(bot_id, shared_dir, start, now))
    if catalog is None:
        catalog = _load_gallery_catalog()
    covered, installed = _installed_coverage(shared_dir, bot_id)

    return build_targeting_report(
        bot_id=bot_id,
        purpose=purpose,
        tuples=tuples,
        catalog=catalog,
        covered_domains=covered,
        installed_pkg_ids=installed,
        window_days=window_days,
    )


def _read_window_tuples(
    bot_id: str, shared_dir: Path, start: datetime, end: datetime
) -> Iterable[Any]:
    """Stream observation tuples for the window. Empty on any read failure —
    a missing store is a thin bot, not an error."""
    try:
        from observations.tuples import read_tuples_range
    except ImportError:
        return []
    try:
        return list(read_tuples_range(shared_dir, bot_id, start, end))
    except Exception:
        return []


def _load_gallery_catalog() -> list[dict[str, Any]]:
    """Load the 14-app gallery catalog. Reuses ``gallery_recommender``'s loader
    so the catalog path has a single source of truth — and so the Fit Reviewer
    is visibly reading the *real* gallery (the contrast with ``app_suggester``,
    spec §1.3)."""
    try:
        from gallery_recommender import load_catalog

        return load_catalog()
    except Exception:
        return []


def _installed_coverage(
    shared_dir: Path, bot_id: str
) -> tuple[set[str], set[str]]:
    """Return (covered_domain_tags, installed_pkg_ids) for the bot.

    Reads installed manifests at ``{shared_dir}/applications/{bot_id}/*.json``
    (the same location ``app_suggester`` uses). Covered domains come from each
    manifest's ``application_tags`` — the gallery convention — so the comparison
    against observation nouns is the same shared vocabulary. A bot with no apps
    (the proof bots today) yields two empty sets.
    """
    covered: set[str] = set()
    installed: set[str] = set()
    app_dir = shared_dir / "applications" / bot_id
    if not app_dir.exists():
        return covered, installed
    import json

    for f in sorted(app_dir.iterdir()):
        if (
            not f.is_file()
            or f.suffix != ".json"
            or f.name.startswith(("_", "."))
        ):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ("pkg_id", "spec_id", "app_id"):
            v = data.get(key)
            if isinstance(v, str) and v:
                installed.add(v)
        tags = data.get("application_tags") or []
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, str) and t.strip():
                    covered.add(t.strip().lower())
    return covered, installed
