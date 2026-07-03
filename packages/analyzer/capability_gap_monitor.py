"""capability_gap_monitor — Pattern → Signal monitor for RSI app_suggester.

The first Phase 2 RSI substrate piece. Spec:
docs/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".

The problem this solves: ``app_suggester`` is RSI-shaped (capability gap
→ "consider adding X" Proposal on Recommendations) but emits nothing
today because its grounding-Signal type ``app_suggester_gap`` has no
producer. The catalog_match technique alone sits below the confidence
floor, so without a Signal scoping (bot, category) the generator stays
silent.

This monitor closes that loop. For each bot:

  1. Walk ObservationTuples for the last N days (default 30).
  2. Cluster by ``noun`` to surface recurring conversational themes.
  3. Reverse-map each noun → ``domain:*`` tag using the same keyword
     dictionary ``app_suggester`` uses to fingerprint coverage —
     keeping the producer + consumer on the same vocabulary.
  4. Pick catalog entries whose ``domain:*`` tag matches the surfaced
     domain, AND that the bot's installed apps don't already cover.
  5. Strength gate: a candidate only emits when the conversational
     cluster cleared ``MIN_DISTINCT_SESSIONS`` / ``MIN_ENGAGEMENT`` /
     ``MIN_DISTINCT_DAYS``. Single-session blips don't promote into
     RSI recommendations — that's the lesson from the
     session_token_outlier screenshot.
  6. Objective check (v1 light): read the bot's workspace AGENTS.md.
     If a domain keyword appears in the purpose text, mark the
     candidate ``objective_fit="confirmed"``. Otherwise mark
     ``neutral`` and require a stricter strength threshold to emit.
     Unreadable AGENTS.md → skip the bot (conservative — don't fan
     out without knowing the bot's role).
  7. ``signals_store.observe`` for each surviving (bot, category)
     pair. ``app_suggester`` (already on ``surface=improvement``)
     picks the Signal up via its existing grounding path and emits
     a Proposal that lands on Recommendations.
  8. ``signals_store.sweep_resolve`` at the end of a pod-wide run
     so gaps that closed (bot added the app, or stopped asking)
     auto-resolve.

Backward compat: ``app_suggester`` consumes Signals from any producer,
so even before the monitor is wired into a daemon it can be run
manually (``python3 -m capability_gap_monitor --bot foo``) for dev
testing.

What this monitor does NOT do:
  * It does not invoke an LLM. Pure-Python noun→domain mapping +
    keyword check on AGENTS.md. Cost is dominated by the disk read
    of the per-bot observation JSONLs.
  * It does not detect "the bot's purpose contradicts this domain"
    — that requires explicit anti-domain markers we don't ask for
    yet. v2 work.
  * It does not propose specific apps. The catalog category is the
    granularity; ``app_suggester`` renders the catalog entry into
    the operator-facing pitch.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_config import (
    CANONICAL_SHARED_DIR,
    get_members,
    get_primary,
    load_config,
)
from observations.access import ObservationWindow, window as obs_window
from schema.observation import ClusterSpec
from signals import store as signals_store


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

PRODUCER = "capability_gap_monitor"
SIGNAL_TYPE = "app_suggester_gap"

# Window: last N days of observations the monitor walks per bot.
DEFAULT_WINDOW_DAYS = 30

# Strength thresholds. These are deliberately permissive enough to catch
# real patterns but strict enough that one stray session doesn't fire.
# Tune via run_for_bot kwargs.
MIN_DISTINCT_SESSIONS = 3
MIN_ENGAGEMENT_TOTAL = 10
MIN_DISTINCT_DAYS = 3

# Stricter threshold for "neutral" objective fit (the bot's AGENTS.md
# doesn't explicitly mention the domain but doesn't exclude it either).
# Pattern must be unambiguous to emit without confirmed objective fit.
NEUTRAL_MIN_DISTINCT_SESSIONS = 5
NEUTRAL_MIN_ENGAGEMENT_TOTAL = 25

# AGENTS.md read cap. Bot purpose typically lives in the first few KB;
# capping the read keeps the keyword scan cheap even on bots with
# multi-page operator notes.
AGENTS_MD_READ_CAP_BYTES = 8192

# Catalog path — same JSON the app_suggester generator reads.
_CATALOG_PATH = (
    Path(__file__).parent / "generators" / "app_suggester" / "catalog.json"
)


# ─────────────────────────────────────────────────────────────────────────────
# Domain map — re-exported from app_suggester so producer + consumer
# share one vocabulary. A drift between them would cause the monitor to
# emit Signals for categories app_suggester then ignores (or vice versa).
# ─────────────────────────────────────────────────────────────────────────────


def _domain_keywords(shared_dir: Path | None = None) -> dict[str, str]:
    """Return the effective keyword vocabulary for this monitor run.

    Post-Layer-2 plumbing: this delegates to
    ``_merged_vocabulary.effective_keywords`` which returns
    ``static ∪ dynamic``. Callers that don't have a shared_dir
    (tests, dry-run harnesses) pass ``None`` and get static-only
    behavior.

    Fail-closed: if ``_merged_vocabulary`` is unimportable for any
    reason, falls back to the empty map and the monitor silently
    emits nothing.
    """
    try:
        import _merged_vocabulary as mv

        return mv.effective_keywords(shared_dir)
    except ImportError:
        return {}


def _noun_to_domains(noun: str, kw_map: dict[str, str]) -> set[str]:
    """Map a noun (cluster key) to the set of domain tags it implies.

    A noun can match multiple keywords ("workout journal" → fitness +
    productivity) and we surface every implied domain. Empty result
    means "no recognized domain" — the noun gets ignored downstream.
    """
    noun_l = (noun or "").lower()
    if not noun_l:
        return set()
    out: set[str] = set()
    for kw, tag in kw_map.items():
        if kw in noun_l:
            out.add(tag)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Catalog reader
# ─────────────────────────────────────────────────────────────────────────────


def _load_catalog(
    path: Path = _CATALOG_PATH, shared_dir: Path | None = None
) -> list[dict]:
    """Static + dynamic catalog. ``shared_dir`` opt-in: when None,
    behavior is pre-Layer-2 (static only) — preserves the existing
    test fixtures that exercise ``_load_catalog`` in isolation.

    Layer 2 integration: ``detect_capability_gaps`` passes the real
    shared_dir so dynamic catalog seeds added via ``vocab_add`` (or
    the LLM expansion flow once γ ships) are honored end-to-end.
    Without this, a dynamic ``catalog-seed`` entry would never reach
    the operator — silently dropped between writer and reader.
    """
    try:
        import _merged_vocabulary as mv

        return mv.effective_catalog(path, shared_dir)
    except ImportError:
        # Fallback: pure static read for analyzer-only envs.
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict)]


def _categories_for_domain(catalog: list[dict], domain_tag: str) -> list[dict]:
    """Catalog entries whose ``tags`` include the given domain tag."""
    out: list[dict] = []
    for e in catalog:
        tags = e.get("tags") or []
        if isinstance(tags, list) and domain_tag in tags:
            out.append(e)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Bot manifest coverage — re-uses app_suggester's logic so the producer
# and consumer agree on what "already covered" means.
# ─────────────────────────────────────────────────────────────────────────────


def _bot_covered_domains(shared_dir: Path, bot_id: str) -> set[str]:
    """Domain tags the bot's installed apps already cover.

    Passes the merged (static ∪ dynamic) keyword vocab to
    ``_extract_covered_domains`` so a manifest using only
    dynamic-vocab keywords gets credit for covering its domain.
    Without the merge, a bot's "sourdough-tracker" app wouldn't
    register as covering ``domain:food`` (assuming ``sourdough`` is
    a dynamic addition), so the cap-gap monitor would keep emitting
    ``domain:food`` gap proposals — the bug this thread fixes.
    """
    try:
        from generators.app_suggester.observe import (
            _extract_covered_domains,
            _load_bot_manifests,
        )

        return _extract_covered_domains(
            _load_bot_manifests(shared_dir, bot_id),
            kw_map=_domain_keywords(shared_dir),
        )
    except ImportError:
        return set()


# ─────────────────────────────────────────────────────────────────────────────
# Bot purpose reader (workspace/AGENTS.md)
# ─────────────────────────────────────────────────────────────────────────────


def _bot_workspace_agents_md(bot_id: str) -> Path:
    """Return the path to a bot's workspace AGENTS.md.

    Path inferred via the standard openclaw layout (.openclaw/workspace
    under the bot user's home). Uses bot_home() when available; falls
    back to /Users/{bot_id} as a last resort but only for the path
    construction — the read still requires the file to actually be
    present and readable.
    """
    try:
        from evolve_admin.config import bot_home  # type: ignore

        home = bot_home(bot_id)
    except Exception:
        home = Path(f"/Users/{bot_id}")
    return home / ".openclaw" / "workspace" / "AGENTS.md"


def _read_agents_md(bot_id: str) -> str | None:
    """Read the bot's workspace AGENTS.md (purpose, persona, role).

    Uses the direct-read pattern from CLAUDE.md: the evolve user has
    ACL on .openclaw/, so a plain Path.read_text usually works. Falls
    back to ``sudo /bin/cat`` for bots that haven't been redeployed
    through the ACL-setting path yet. Returns None when neither
    succeeds — the caller treats that as "skip this bot."

    Capped at AGENTS_MD_READ_CAP_BYTES — purpose lives near the top and
    we don't need the full file.
    """
    path = _bot_workspace_agents_md(bot_id)
    try:
        with path.open("r", encoding="utf-8") as f:
            return f.read(AGENTS_MD_READ_CAP_BYTES)
    except FileNotFoundError:
        return None
    except PermissionError:
        # Sudo fallback — sudoers grants /bin/cat to the evolve user.
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                return r.stdout[:AGENTS_MD_READ_CAP_BYTES]
        except Exception:
            return None
        return None
    except Exception:
        return None


def _objective_fit(
    purpose_text: str | None,
    domain_tag: str,
    kw_map: dict[str, str],
    *,
    anti_domains: set[str] | None = None,
) -> str:
    """Score the bot's purpose against a candidate domain.

    Returns one of:
      ``"confirmed"``    — keyword for this domain appears in
                           purpose text. Emit at the default
                           strength threshold.
      ``"neutral"``      — purpose loaded but no keyword for this
                           domain. Emit only if pattern is
                           unambiguous (NEUTRAL_MIN_* thresholds).
      ``"contradicted"`` — the bot's AGENTS.md has an explicit
                           "out of scope" marker covering this
                           domain. The capability-gap path drops
                           the candidate entirely — operator has
                           already said "don't suggest this."
      ``"skip"``         — no purpose text available. Conservative;
                           don't emit for this bot.

    ``anti_domains`` — pre-parsed set of ``domain:*`` tags the bot
    has explicitly excluded. Pass empty / None to disable the
    check (callers typically pass the result of
    ``anti_domains.parse_anti_domains(purpose_text)`` once per bot
    and reuse).
    """
    if not purpose_text:
        return "skip"
    if anti_domains and domain_tag in anti_domains:
        return "contradicted"
    purpose_l = purpose_text.lower()
    for kw, tag in kw_map.items():
        if tag == domain_tag and kw in purpose_l:
            return "confirmed"
    return "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _DomainEvidence:
    """Aggregated evidence for one (bot, domain) candidate.

    Built from cluster cells whose noun maps to this domain. Multiple
    nouns can map to the same domain ("workout" + "fitness" both →
    domain:fitness), so we union the underlying clusters before
    applying strength thresholds.
    """

    domain_tag: str
    distinct_sessions: set[str] = field(default_factory=set)
    engagement_total: int = 0
    distinct_days: set[str] = field(default_factory=set)
    example_nouns: list[str] = field(default_factory=list)
    earliest: str = ""
    latest: str = ""

    def add_cell(self, noun: str, cell) -> None:
        # cell is a schema.observation.ClusterCell. We don't have
        # per-day breakdowns on the cell itself; instead we approximate
        # distinct_days from earliest/latest by collecting cells'
        # earliest/latest dates over time. For v1 we use the cell's
        # own (earliest, latest) date range as a proxy: a cluster
        # spanning ≥ MIN_DISTINCT_DAYS calendar days counts toward
        # the threshold. Refinement (per-day tuple walk) is v2.
        if noun and noun not in self.example_nouns:
            self.example_nouns.append(noun)
        self.engagement_total += int(getattr(cell, "engagement_total", 0))
        sessions = int(getattr(cell, "distinct_sessions", 0))
        # ClusterCell exposes session count, not the IDs. We treat each
        # cell's contribution additively: a domain with two nouns
        # appearing across the same 3 sessions effectively gets credit
        # for 3, since session-id-set union isn't reconstructible here.
        # Over-count is bounded by the strength gate's small N.
        # Track session-count as int via _distinct_sessions_count, but
        # use the set form for the per-bot signature so we don't have
        # to rebuild infrastructure: store sentinel ids.
        for i in range(sessions):
            self.distinct_sessions.add(f"{noun}#{cell.earliest}#{i}")
        earliest = getattr(cell, "earliest", "") or ""
        latest = getattr(cell, "latest", "") or ""
        if earliest:
            if not self.earliest or earliest < self.earliest:
                self.earliest = earliest
        if latest:
            if not self.latest or latest > self.latest:
                self.latest = latest
        # ClusterCell exposes only the cell's earliest and latest
        # timestamps — not the per-day breakdown. Treat the entire
        # date span as covered: a cluster spanning May 31 → Jun 5
        # counts as 6 distinct days. This over-counts for clusters
        # with sparse activity inside their span, but the strength
        # gate's MIN_DISTINCT_DAYS=3 is so coarse that the over-count
        # only matters at the boundary, where the cluster being weak
        # would also fail the engagement/session gates.
        if earliest and latest:
            try:
                eday = date.fromisoformat(earliest[:10])
                lday = date.fromisoformat(latest[:10])
                for offset in range((lday - eday).days + 1):
                    self.distinct_days.add(
                        (eday + timedelta(days=offset)).isoformat()
                    )
            except ValueError:
                # Malformed timestamp — fall back to adding just the
                # day strings, no expansion.
                self.distinct_days.add(earliest[:10])
                self.distinct_days.add(latest[:10])

    @property
    def distinct_sessions_count(self) -> int:
        return len(self.distinct_sessions)

    @property
    def distinct_days_count(self) -> int:
        return len(self.distinct_days)


def _passes_strength(
    ev: _DomainEvidence,
    *,
    objective_fit: str,
) -> bool:
    """Apply the appropriate strength gate for this evidence.

    ``confirmed`` fit uses the default thresholds. ``neutral`` fit
    requires the stricter thresholds. ``skip`` never reaches here.
    """
    if objective_fit == "confirmed":
        return (
            ev.distinct_sessions_count >= MIN_DISTINCT_SESSIONS
            and ev.engagement_total >= MIN_ENGAGEMENT_TOTAL
            and ev.distinct_days_count >= MIN_DISTINCT_DAYS
        )
    # neutral
    return (
        ev.distinct_sessions_count >= NEUTRAL_MIN_DISTINCT_SESSIONS
        and ev.engagement_total >= NEUTRAL_MIN_ENGAGEMENT_TOTAL
        and ev.distinct_days_count >= MIN_DISTINCT_DAYS
    )


def detect_capability_gaps(
    bot_id: str,
    shared_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    catalog_path: Path = _CATALOG_PATH,
) -> list[dict]:
    """Compute detections for one bot. Returns dicts ready to pass to
    ``signals_store.observe(**d)``.

    A detection is one ``(bot, category)`` pair that:
      * has a (noun → domain → catalog) chain grounded in observation
        clusters,
      * isn't already covered by the bot's installed apps,
      * cleared the strength gate appropriate to its objective fit.

    Empty list when none of the above hold — including when the bot
    has no observations at all (a fresh deploy or quiet day).
    """
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=window_days - 1)
    # Pass shared_dir so dynamic vocab (Layer 2) overlays the static
    # _DOMAIN_KEYWORDS. Empty dynamic → pre-Layer-2 behavior preserved.
    kw_map = _domain_keywords(shared_dir)
    if not kw_map:
        return []
    # Pass shared_dir so dynamic catalog seeds are merged in. Without
    # this, a dynamic catalog-seed entry added via vocab_add would be
    # invisible to cap-gap detection — the writer would work but the
    # reader wouldn't.
    catalog = _load_catalog(catalog_path, shared_dir=shared_dir)
    if not catalog:
        return []
    covered = _bot_covered_domains(shared_dir, bot_id)
    purpose_text = _read_agents_md(bot_id)
    # Parse explicit "out of scope" markers from AGENTS.md once per
    # bot. The empty set when no markers are present preserves the
    # pre-anti-domain behavior on every bot that hasn't adopted the
    # marker convention. See docs/spec-rsi-proposal-eligibility-
    # 2026-06-05.md §"Phase 2".
    from anti_domains import parse_anti_domains
    anti = parse_anti_domains(purpose_text, shared_dir)

    # window() takes bot_id positionally, then keyword-only start/end/
    # shared_dir. Mismatch of these versus the (shared_dir, bot_id,
    # start, end) shape I first wrote got caught by the tests; pinned
    # the correct call here.
    win = obs_window(
        bot_id,
        start=start,
        end=now,
        shared_dir=shared_dir,
    )
    # Cluster by noun. Single-key spec keeps the key shape consistent
    # with our reverse-mapping (noun → domain).
    cv = win.clusters(by=ClusterSpec(group_by=["noun"]))

    # Aggregate evidence per domain across all matching cells.
    evidence: dict[str, _DomainEvidence] = {}
    for cell in cv.cells:
        # cell.key is a tuple — for group_by=["noun"] it's (noun_str,)
        if not cell.key or cell.key[0] is None:
            continue
        noun = str(cell.key[0])
        domains = _noun_to_domains(noun, kw_map)
        for domain in domains:
            if domain in covered:
                continue
            ev = evidence.setdefault(domain, _DomainEvidence(domain_tag=domain))
            ev.add_cell(noun, cell)

    detections: list[dict] = []
    for domain, ev in evidence.items():
        fit = _objective_fit(
            purpose_text, domain, kw_map, anti_domains=anti
        )
        if fit == "skip":
            continue
        # Anti-domain marker on this bot — operator already said
        # "don't suggest fitness for me." Drop the candidate without
        # emitting; emitting would just generate a Recommendations
        # card the operator dismisses every cycle.
        if fit == "contradicted":
            continue
        if not _passes_strength(ev, objective_fit=fit):
            continue

        # Emit one signal per catalog category that hits this domain.
        for entry in _categories_for_domain(catalog, domain):
            category = entry.get("category") or ""
            if not category:
                continue
            # Drop any pair where this specific category is already
            # subsumed (defense in depth — covered set should have
            # caught it but the catalog entry might have multiple
            # domain tags).
            entry_tags = {
                t for t in (entry.get("tags") or [])
                if isinstance(t, str) and t.startswith("domain:")
            }
            if entry_tags and entry_tags.issubset(covered):
                continue

            signature = f"app_suggester_gap:{bot_id}:{category}"
            title = (
                f"Capability gap: {category.replace('_', ' ')} on {bot_id}"
            )[:120]
            body = (
                f"Observation cluster on {bot_id} surfaces the "
                f"{domain.split(':', 1)[-1]} theme ({ev.distinct_sessions_count} "
                f"sessions, {ev.distinct_days_count} days, engagement "
                f"{ev.engagement_total}). The bot has no installed "
                f"app covering this domain."
            )
            detections.append(
                {
                    "signature": signature,
                    "producer": PRODUCER,
                    "type": SIGNAL_TYPE,
                    "flavor": "activity",
                    "severity": "info",
                    "scope": "bot",
                    "bot_id": bot_id,
                    "title": title,
                    "body": body,
                    "details": {
                        "category": category,
                        "bot_id": bot_id,
                        "domain_tag": domain,
                        "objective_fit": fit,
                        "example_nouns": ev.example_nouns[:5],
                        "distinct_sessions": ev.distinct_sessions_count,
                        "distinct_days": ev.distinct_days_count,
                        "engagement_total": ev.engagement_total,
                        "earliest": ev.earliest,
                        "latest": ev.latest,
                        "window_days": window_days,
                    },
                }
            )
    return detections


# ─────────────────────────────────────────────────────────────────────────────
# Runner — per-bot + pod-wide entry points
# ─────────────────────────────────────────────────────────────────────────────


def run_for_bot(
    bot_id: str,
    shared_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    dry_run: bool = False,
) -> set[str]:
    """Observe detections for one bot. Returns the kept signature set
    so the caller's sweep_resolve can leave them alone."""
    kept: set[str] = set()
    for d in detect_capability_gaps(
        bot_id, shared_dir, now=now, window_days=window_days
    ):
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:
            print(
                f"[capability_gap_monitor] observe failed for "
                f"{d['signature']}: {exc}",
                flush=True,
            )
    return kept


def run_for_pod(
    bot_ids: Iterable[str],
    shared_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    dry_run: bool = False,
) -> dict:
    """Pod-wide entry point. Runs every bot, then sweep_resolves any
    Signal from this producer whose signature wasn't reproduced — i.e.
    the gap closed (app installed, or pattern faded out of the window).

    Returns a small summary dict for the caller's log."""
    bots = list(bot_ids)
    all_kept: set[str] = set()
    per_bot_counts: dict[str, int] = {}
    for bot_id in bots:
        kept = run_for_bot(
            bot_id,
            shared_dir,
            now=now,
            window_days=window_days,
            dry_run=dry_run,
        )
        per_bot_counts[bot_id] = len(kept)
        all_kept.update(kept)
    resolved: list = []
    if not dry_run:
        resolved = signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            kept_signatures=all_kept,
            types={SIGNAL_TYPE},
            bot_ids=set(bots),
            reason="capability gap closed",
        )
    return {
        "producer": PRODUCER,
        "bots_scanned": len(bots),
        "signals_emitted": len(all_kept),
        "signals_resolved": len(resolved),
        "per_bot_counts": per_bot_counts,
    }


def main() -> None:
    """CLI entry. Reads bot list from network.json; runs pod-wide."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Detect capability gaps from observation patterns.",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=CANONICAL_SHARED_DIR,
        help="Pod shared directory (default: platform-keyed canonical shared dir).",
    )
    parser.add_argument(
        "--network",
        default=None,
        help="Path to network.json (default: platform-keyed canonical config).",
    )
    parser.add_argument(
        "--bot",
        action="append",
        default=None,
        help="Limit to one bot (repeatable). Default: every member bot.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print detections; do not write Signals or sweep.",
    )
    args = parser.parse_args()

    bot_ids: list[str]
    if args.bot:
        bot_ids = list(args.bot)
    else:
        # Same pattern as cost_watchdog.main — read network.json for the
        # primary + member bots. Fail-loud rather than fall back to a
        # filesystem scan.
        try:
            config = load_config(args.network)
            primary = get_primary(config)
            members = get_members(config)
            bot_ids = (
                [primary] if primary and primary not in members else []
            ) + members
            bot_ids = [b for b in bot_ids if b]
        except Exception as exc:
            raise SystemExit(
                f"[capability_gap_monitor] could not read member bots: {exc}"
            )
    summary = run_for_pod(
        bot_ids,
        args.shared_dir,
        window_days=args.window_days,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
