"""
better_engine/engine.py — BetterEngine class (Tier 1).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass

from .model import Recommendation, now_iso
from .storage import (
    load_recommendations,
    load_learning,
    save_learning,
    load_getting_started,
    save_getting_started,
    load_source_freshness,
)
from .scoring import compute_priority_score, urgency_label, freshness_score
from .learning import get_effective_weight, record_feedback
from .snooze import wake_snoozed, snooze_recommendation
from .portfolio import compose_portfolio
from .onboarding import build_onboarding_state, auto_complete_tasks

_log = logging.getLogger(__name__)

def surface_rewrite(rec: "Recommendation", surface: str) -> "Recommendation":
    """Return a copy of *rec* with member-bot-appropriate title/detail for display.

    - If surface != "member_bot", returns *rec* unchanged.
    - If rec.member_bot_title is set, substitutes it as the display title/detail
      (returns a shallow copy — does not mutate the stored rec).
    - Otherwise, returns *rec* unchanged. Adapters that target the bot-user
      audience are responsible for setting member_bot_title at emit time;
      filter_for_surface excludes recs without it from member_bot, so this
      path is only reached defensively.
    """
    import dataclasses

    if surface != "member_bot":
        return rec

    if rec.member_bot_title is None:
        return rec

    copy = dataclasses.replace(rec)
    copy.title = rec.member_bot_title
    if rec.member_bot_detail is not None:
        copy.detail = rec.member_bot_detail
    return copy


class BetterEngine:
    """Better Engine — refresh cycle, merging, surface filtering, feedback."""

    def __init__(self, shared_dir: Path, network: dict) -> None:
        self.shared_dir = shared_dir
        self.network = network
        self.recs_path = shared_dir / "better-engine" / "recommendations.json"
        self.learning_path = shared_dir / "better-engine" / "learning.json"
        self.getting_started_path = shared_dir / "better-engine" / "getting-started.json"

    # ── Merge (§11 step 4) ────────────────────────────────────────────────────

    @staticmethod
    def merge(
        existing: list[Recommendation],
        candidates: list[Recommendation],
    ) -> list[Recommendation]:
        """Merge freshly-generated candidates with the existing recommendation store.

        Rules (§11 step 4):
        - Candidate dedup_key IN existing pending/snoozed/auto_resolved: update mutable
          fields and revive to pending (auto_resolved is system-resolved, not user intent)
        - Candidate dedup_key NOT in existing: add with created_at = now
        - Existing pending/snoozed NOT in candidates: set status = auto_resolved
        - Accepted/rejected/expired recs are never touched (user-intentional)
        """
        # Index existing by dedup_key
        existing_by_key: dict[str, Recommendation] = {r.dedup_key: r for r in existing}
        candidate_keys: set[str] = {c.dedup_key for c in candidates}

        # Statuses that the merge loop should auto_resolve if missing from candidates
        resolvable_statuses = {"pending", "snoozed"}
        # Statuses the merge loop can REVIVE when a matching candidate appears.
        # auto_resolved = system-resolved (not user intent) → eligible for revival.
        revivable_statuses = {"auto_resolved"}
        # Statuses to preserve unchanged regardless — user-intentional terminal states
        preserve_statuses = {"accepted", "rejected", "expired", "resolved"}

        # Process candidates
        result_by_key: dict[str, Recommendation] = {}

        for candidate in candidates:
            key = candidate.dedup_key
            if key in existing_by_key:
                existing_rec = existing_by_key[key]
                if existing_rec.status in resolvable_statuses:
                    # Update mutable fields from the candidate (pending/snoozed only)
                    existing_rec.update_from_candidate(candidate)
                    result_by_key[key] = existing_rec
                elif existing_rec.status in revivable_statuses:
                    # System auto-resolved: revive as a fresh pending candidate so the
                    # item can rotate back into the queue (whimsy rotation, stale health
                    # checks, etc.).  created_at is preserved to maintain history.
                    candidate.created_at = existing_rec.created_at
                    result_by_key[key] = candidate
                else:
                    # preserve_statuses: accepted/rejected/expired/resolved — never touched
                    result_by_key[key] = existing_rec
            else:
                # New recommendation — created_at is already set (now_iso default)
                result_by_key[key] = candidate

        # Pass through existing non-candidate recs
        for rec in existing:
            if rec.dedup_key in result_by_key:
                continue  # already handled
            if rec.status in resolvable_statuses and rec.dedup_key not in candidate_keys:
                # Auto-resolve missing pending/snoozed
                rec.status = "auto_resolved"
                rec.resolved_at = now_iso()
                rec.updated_at = now_iso()
            result_by_key[rec.dedup_key] = rec

        return list(result_by_key.values())

    # ── Run adapters ──────────────────────────────────────────────────────────

    def run_adapters(self, adapters: list) -> list[Recommendation]:
        """Call each adapter's generate() method; catch exceptions per-adapter."""
        candidates: list[Recommendation] = []
        for adapter in adapters:
            adapter_name = type(adapter).__name__
            try:
                results = adapter.generate(self.shared_dir, self.network)
                count = len(results)
                if count:
                    types = {}
                    for r in results:
                        types[r.type] = types.get(r.type, 0) + 1
                    type_summary = ", ".join(
                        f"{t}×{n}" for t, n in sorted(types.items())
                    )
                    _log.info(
                        "Adapter %s generated %d candidate(s): %s",
                        adapter_name,
                        count,
                        type_summary,
                    )
                else:
                    _log.info("Adapter %s generated 0 candidates", adapter_name)
                candidates.extend(results)
            except Exception as exc:
                _log.error(
                    "Adapter %s raised an exception: %s",
                    adapter_name,
                    exc,
                    exc_info=True,
                )
        _log.info(
            "run_adapters complete: %d total candidates from %d adapter(s)",
            len(candidates),
            len(adapters),
        )
        return candidates

    # ── Refresh pipeline (§11 steps 3–14) ────────────────────────────────────

    def refresh(self, adapters: list) -> list[Recommendation]:
        """Run the full refresh pipeline.

        Steps performed:
          1. Load existing recommendations
          2. Run all adapters → candidates
          3. Merge candidates with existing
          4. Wake snoozed recs past snooze_until
          5. Compute base scores (with learning weights)
          6. Tag urgency level
          7. Sort by priority_score DESC
          8. Compose portfolio (stored on self)
          9. Update source_freshness timestamps
         10. Save recommendations atomically
         11. Auto-complete onboarding tasks
         12. Save getting-started atomically if changed

        Returns the full sorted pending list.
        """
        # Ensure storage directory exists
        self.recs_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Load existing
        existing = load_recommendations(self.recs_path)
        existing_by_key = {r.dedup_key: r for r in existing}
        _log.info("refresh() loaded %d existing recs (%d pending)",
                  len(existing),
                  sum(1 for r in existing if r.status == "pending"))

        # 2. Run adapters
        candidates = self.run_adapters(adapters)

        # 3. Merge
        merged = self.merge(existing, candidates)

        # Log merge stats
        new_keys = {c.dedup_key for c in candidates} - set(existing_by_key)
        auto_resolved = [r for r in merged if r.status == "auto_resolved"
                         and r.dedup_key in existing_by_key
                         and existing_by_key[r.dedup_key].status == "pending"]
        _log.info(
            "merge: %d new, %d updated existing, %d auto-resolved, %d total in store",
            len(new_keys),
            len(candidates) - len(new_keys),
            len(auto_resolved),
            len(merged),
        )

        # 3b. Prune auto-resolved arbiter recs.
        # OnboardingAdapter and WhimsyAdapter both reuse a fixed set of
        # dedup_keys (one per task / one per whimsy week), so auto_resolved
        # entries from those sources are useful as "this was last seen
        # healthy" history. ProposalReaderAdapter, by contrast, emits a
        # unique dedup_key per proposal_id — once the proposal is
        # applied/archived the rec has no further purpose and the arbiter
        # store itself owns the proposal's history. Without this prune,
        # arbiter-sourced recs grow without bound (arbiter-bridge V1
        # caught extra=306 on an admin pod after a few weeks of activity).
        before_prune = len(merged)
        merged = [
            rec for rec in merged
            if not (rec.dedup_key.startswith("arbiter::")
                    and rec.status == "auto_resolved")
        ]
        pruned = before_prune - len(merged)
        if pruned:
            _log.info("pruned %d auto_resolved arbiter rec(s)", pruned)

        # 4. Wake snoozed
        merged = wake_snoozed(merged)

        # 5. Load learning + compute scores for pending recs
        learning = load_learning(self.learning_path)
        signals = learning.get("signals", {})

        for rec in merged:
            if rec.status != "pending":
                continue
            # Set learning weight
            rec.learning_weight = get_effective_weight(rec, learning)
            # Always recompute freshness — it changes as the rec ages and snooze_count
            # may have changed (e.g., after a wake from snooze)
            rec.priority_components["freshness"] = freshness_score(
                rec.created_at, rec.snooze_count
            )
            # Compute final score
            rec.priority_score = compute_priority_score(rec)

        # 6. Tag urgency level — update or add the urgency:{level} tag
        for rec in merged:
            if rec.status != "pending":
                continue
            level = urgency_label(rec.priority_score)
            urgency_tag = f"urgency:{level}"
            # Remove any existing urgency tags
            rec.tags = [t for t in rec.tags if not t.startswith("urgency:")]
            rec.tags.append(urgency_tag)

        # 6b. Whimsy dynamic impact recalculation (§8.7)
        # Impact for whimsy scales inversely with pending non-whimsy count.
        pending_non_whimsy = sum(
            1 for r in merged if r.status == "pending" and r.type != "whimsy"
        )
        for rec in merged:
            if rec.type == "whimsy" and rec.status == "pending":
                impact = max(0, 12 - (pending_non_whimsy * 2))
                rec.priority_components["impact"] = impact
                rec.priority_score = compute_priority_score(rec)
                # Re-tag urgency level now that score has changed
                level = urgency_label(rec.priority_score)
                rec.tags = [t for t in rec.tags if not t.startswith("urgency:")]
                rec.tags.append(f"urgency:{level}")

        # 7. Sort pending by priority_score DESC; non-pending at the end
        pending = sorted(
            [r for r in merged if r.status == "pending"],
            key=lambda r: r.priority_score,
            reverse=True,
        )
        non_pending = [r for r in merged if r.status != "pending"]
        sorted_recs = pending + non_pending

        # 8. Compose portfolio (stored on self for inspection)
        self.portfolio = compose_portfolio(pending)

        # 9. Update source_freshness — load existing timestamps, then stamp adapters
        source_freshness = load_source_freshness(self.recs_path)
        ts_now = now_iso()
        for adapter in adapters:
            source_name = getattr(adapter, "source_name", type(adapter).__name__)
            source_freshness[source_name] = ts_now

        # 10. Save recommendations
        # Attach source_freshness to the top of the list via a wrapper dict
        _save_recs_with_freshness(sorted_recs, source_freshness, self.recs_path)

        # 11. Auto-complete onboarding tasks
        getting_started = load_getting_started(self.getting_started_path)
        state = build_onboarding_state(self.shared_dir, self.network)
        original_tasks = dict(getting_started.get("tasks", {}))
        getting_started, newly_completed = auto_complete_tasks(state, getting_started)

        # 12. Save getting-started if changed
        if getting_started.get("tasks", {}) != original_tasks:
            self.getting_started_path.parent.mkdir(parents=True, exist_ok=True)
            save_getting_started(getting_started, self.getting_started_path)

        if newly_completed:
            _log.info("Auto-completed onboarding tasks: %s", newly_completed)

        _log.info(
            "refresh() complete: %d pending recs, %d newly completed onboarding tasks",
            len(pending),
            len(newly_completed),
        )

        return pending

    # ── Surface filtering (§18.2) ─────────────────────────────────────────────

    @staticmethod
    def filter_for_surface(
        recs: list[Recommendation],
        surface: str,
        scope_id: Optional[str] = None,
    ) -> list[Recommendation]:
        """Apply surface-specific filtering rules (§18.2).

        surface="admin": all pending recs; optionally filtered by scope_id.
        surface="member_bot": strict inclusion/exclusion rules.
        """
        if surface == "admin":
            if scope_id:
                return [r for r in recs if r.status == "pending" and r.scope_id == scope_id]
            return [r for r in recs if r.status == "pending"]

        if surface == "member_bot":
            if not scope_id:
                return []

            result: list[Recommendation] = []
            for rec in recs:
                if rec.status != "pending":
                    continue

                # Whimsy is pod-wide; allow it for all member bots regardless of
                # scope/scope_id so the evo keyword always has something to show.
                if rec.type == "whimsy":
                    result.append(rec)
                    continue

                # Always exclude other bots' items and pod-admin-scope items
                if rec.scope_id != scope_id:
                    continue
                if rec.scope == "admin":
                    continue

                # Always exclude security recs
                if rec.type == "security":
                    continue

                # Always exclude recs with no executability path
                if not rec.bot_executable and rec.bridge_strategy is None:
                    continue

                # Always exclude recs without bot-user-facing copy.
                # Admin-targeted adapters (OnboardingAdapter etc.) leave
                # member_bot_title=None; honoring that keeps admin
                # narrative ("Admin_bot has no registered applications") out
                # of the bot's chat with its end user. Whimsy is exempted
                # above because it always populates member_bot_title and
                # is the intentional empty-queue fallback.
                if rec.member_bot_title is None:
                    continue

                # Include if type is eligible for member bot. The
                # ``explore`` type was retired alongside suggestions.py —
                # exploratory recs from generators/app_suggester carry
                # type=app_quality via the proposal_reader bridge.
                if rec.type in ("app_quality", "onboarding"):
                    result.append(rec)
                    continue

                # Operational / cost: include only on urgency:critical exception
                if rec.type in ("operational", "cost"):
                    if "urgency:critical" in rec.tags:
                        result.append(rec)
                    continue

                # Any other type: exclude by default

            return [surface_rewrite(r, surface) for r in result]

        # Unknown surface — return empty list
        return []

    # ── Top recommendation ────────────────────────────────────────────────────

    def get_top(
        self,
        surface: str = "admin",
        scope_id: Optional[str] = None,
    ) -> Optional[Recommendation]:
        """Load recommendations, filter for surface, return the highest-scored one."""
        recs = load_recommendations(self.recs_path)
        filtered = self.filter_for_surface(recs, surface, scope_id)
        return filtered[0] if filtered else None

    # ── Feedback ──────────────────────────────────────────────────────────────

    def record_feedback(
        self,
        rec_id: str,
        signal: str,
        reason: str = "",
    ) -> Optional[Recommendation]:
        """Record feedback signal, update rec status, save both files.

        signal: "accepted" | "rejected" | "snoozed"
        Returns updated rec, or None if not found.
        """
        recs = load_recommendations(self.recs_path)
        learning = load_learning(self.learning_path)

        target: Optional[Recommendation] = None
        for rec in recs:
            if rec.id == rec_id:
                target = rec
                break

        if target is None:
            return None

        # Update learning signals
        learning = record_feedback(target, signal, reason, learning)

        # Update rec status
        if signal == "accepted":
            target.status = "accepted"
            target.feedback_signal = "positive"
            target.feedback_reason = reason
            target.resolved_at = now_iso()
        elif signal == "rejected":
            target.status = "rejected"
            target.feedback_signal = "negative"
            target.feedback_reason = reason
            target.resolved_at = now_iso()
        # snoozed handled separately in snooze()

        target.updated_at = now_iso()

        # Save both files — preserve source_freshness metadata in recs file
        self.recs_path.parent.mkdir(parents=True, exist_ok=True)
        source_freshness = load_source_freshness(self.recs_path)
        _save_recs_with_freshness(recs, source_freshness, self.recs_path)
        self.learning_path.parent.mkdir(parents=True, exist_ok=True)
        save_learning(learning, self.learning_path)

        return target

    # ── Snooze ────────────────────────────────────────────────────────────────

    def snooze(
        self,
        rec_id: str,
        *,
        days_override: Optional[int] = None,
    ) -> Optional[Recommendation]:
        """Snooze a recommendation. Default uses the escalating schedule;
        ``days_override`` (slice 5b8) honors a user-stated duration so a
        natural-language "remind me next week" parses through to the
        actual snooze_until on the rec.

        Returns updated rec, or None if not found.
        """
        recs = load_recommendations(self.recs_path)
        learning = load_learning(self.learning_path)

        target: Optional[Recommendation] = None
        for rec in recs:
            if rec.id == rec_id:
                target = rec
                break

        if target is None:
            return None

        # Apply snooze (with optional duration override)
        target = snooze_recommendation(target, days_override=days_override)

        # Record feedback signal — distinguish escalated vs. user-named
        # so the learning layer can tell them apart later.
        if days_override is not None and days_override > 0:
            reason = f"snooze user-named={days_override}d"
        else:
            reason = f"snooze #{target.snooze_count}, escalated"
        learning = record_feedback(target, "snoozed", reason, learning)

        # Save both files — preserve source_freshness metadata in recs file
        self.recs_path.parent.mkdir(parents=True, exist_ok=True)
        source_freshness = load_source_freshness(self.recs_path)
        _save_recs_with_freshness(recs, source_freshness, self.recs_path)
        self.learning_path.parent.mkdir(parents=True, exist_ok=True)
        save_learning(learning, self.learning_path)

        return target


def _save_recs_with_freshness(
    recs: list[Recommendation],
    source_freshness: dict,
    path: Path,
) -> None:
    """Save recommendations list.

    source_freshness is currently stored in the recommendations.json file as
    a top-level key by wrapping the list.  The storage module's
    load_recommendations() reads only the list items; it tolerates both
    list-format and dict-format files via this wrapper.
    """
    import json
    import os

    # If source_freshness is non-empty, wrap in a dict with metadata
    if source_freshness:
        data = {
            "source_freshness": source_freshness,
            "recommendations": [r.to_dict() for r in recs],
        }
    else:
        # Plain list — compatible with existing storage.load_recommendations()
        data = [r.to_dict() for r in recs]

    text = json.dumps(data, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
