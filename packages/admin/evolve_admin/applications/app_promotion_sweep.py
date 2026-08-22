"""app_promotion_sweep — the producer that actually mints promotion Proposals.

Design ``design-app-spec-and-discovery-2026-08-15.md`` §7.2 / §10.
Brief ``docs/build-AL-1.7-promotion.md`` §4 item 1.

WHY THIS MODULE EXISTS, stated plainly because it was nearly missed. The first
cut of AL-1.7 shipped the gate (``app_promotion.evaluate_offer``), the proposal
builder (``build_promotion_proposal``) and the cadence counter
(``recent_offer_count``) with **no production caller at all** — an independent
review of #3734 found that every one of them was reachable only from tests, and
that brief §8.3's "the chain runs end to end without a single change to it" was
therefore false. A gate nothing calls does not gate anything. This module is the
caller.

WHAT IT IS NOT. It is **not** a change to ``scanner.py`` — that file is another
chip's this cycle (brief §5) and racing it is how this repo produced a
duplicate-kwarg ``SyntaxError`` behind a clean "Successfully rebased". This
sweep reads manifests the scanner already wrote and proposes over them; it
discovers nothing and writes no manifest.

**IT WILL PROPOSE NOTHING ON THE POD TODAY, AND THAT IS THE CORRECT BEHAVIOUR.**
Measured 2026-08-19 over all 79 discovered manifests: ``eligible_to_offer=0``,
because only the ``evidence`` dimension has a producer and
``app_readiness.MIN_DIMENSIONS_FOR_OFFER`` is 2. A sweep that returned candidates
today would only do so because a threshold had been lowered to make it —
this arc's recurring vacuity failure. The sweep is written so that the day
AL-1.6c's recurrence producer lands, it starts finding candidates **on merit and
with no change here**.

CADENCE, ONE MORE TIME, BECAUSE THE SWEEP IS WHERE IT BITES. Design §7.2 caps
offers at one per bot per day. The count comes from the proposal store across
**every** subdir a promotion can be in — pending, snoozed, applied, archived —
because answering an offer must not reset the cap. And the cap is applied
*per bot*: two bots each getting one offer is the design; one bot getting two is
not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import app_promotion as _promotion
from .app_readiness import score_readiness

logger = logging.getLogger(__name__)

__all__ = [
    "Candidate",
    "SweepResult",
    "plan_offer",
    "select_candidates",
    "sweep_bot",
]

#: Every proposal subdir a promotion offer can be sitting in. See the module
#: docstring: counting only ``pending`` would let a "no" license a new offer.
CADENCE_SUBDIRS: tuple[str, ...] = ("pending", "snoozed", "applied", "archived")


@dataclass(frozen=True)
class Candidate:
    """One discovered draft, scored and gated."""

    manifest_stem: str
    manifest: Mapping[str, Any]
    readiness: Any
    decision: Any  # app_promotion.OfferDecision

    @property
    def offerable(self) -> bool:
        return bool(getattr(self.decision, "allowed", False))

    @property
    def score(self) -> int:
        return int(getattr(self.readiness, "score", 0) or 0)


@dataclass(frozen=True)
class SweepResult:
    """What one sweep of one bot did, and why it did not do more.

    ``skipped`` carries every rejected draft's blocker list. A sweep that
    reports only what it proposed cannot be debugged — "why did nothing
    happen?" is the question this whole arc keeps having to answer with a
    manual re-measurement, and the answer belongs in the return value.
    """

    bot_id: str
    considered: int
    offered: tuple[str, ...] = field(default_factory=tuple)
    skipped: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)
    cadence_blocked: bool = False
    #: The resolved promotion policy for this sweep. On the result rather than
    #: only in a log line so ``--dry-run`` can show an operator that
    #: ``self_promote`` is actually being read, and that a requested
    #: ``auto_promote`` was refused.
    policy: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "considered": self.considered,
            "offered": list(self.offered),
            "skipped": [{"stem": s, "blockers": list(b)} for s, b in self.skipped],
            "cadence_blocked": self.cadence_blocked,
            "policy": self.policy,
        }


def select_candidates(
    manifests: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    bot_id: str,
    recent_offers: int,
    now: datetime,
) -> list[Candidate]:
    """Score and gate every draft, ranked best-first. **Pure.**

    ``manifests`` is ``[(stem, manifest), ...]`` — the caller does the reading,
    so this stays testable without a pod and cannot be surprised by a
    filesystem.

    Ranking is by readiness score descending, then stem ascending. The tiebreak
    is not decoration: without it two drafts with equal scores would be offered
    in filesystem order, so which one a user is asked about would depend on
    directory iteration — and the answer would change between runs.
    """
    candidates: list[Candidate] = []
    for stem, manifest in manifests:
        readiness = score_readiness(manifest)
        decision = _promotion.evaluate_offer(
            manifest,
            bot_id=bot_id,
            readiness=readiness,
            recent_offers=recent_offers,
            now=now,
        )
        candidates.append(
            Candidate(
                manifest_stem=stem,
                manifest=manifest,
                readiness=readiness,
                decision=decision,
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.manifest_stem))
    return candidates


def plan_offer(
    shared_dir: Path,
    bot_id: str,
    *,
    network: Any,
    read_manifests: Callable[[str], Iterable[tuple[str, Mapping[str, Any]]]],
    now: datetime | None = None,
) -> tuple[SweepResult, Any]:
    """Decide what ``bot_id`` should be offered, and mint it — **without writing**.

    Returns ``(result, proposal_or_None)``. The proposal comes back in ``draft``
    status: whoever writes it owns the transition, because the two callers write
    it differently and neither may quietly become the other.

    * :func:`sweep_bot` (the CLI / direct path) transitions it to ``pending``
      and writes it with ``arbiter.store``.
    * ``generators.app_promotion.observe`` returns it to ``generator_runner``,
      which runs it through ``arbiter.ingest`` — dedup, charter invariants and
      the rejection cooldown — and writes it there.

    Split out of ``sweep_bot`` so the scheduled path and the operator path make
    the SAME decision. A second copy of "which draft, and may it be offered" is
    how a cadence cap ends up enforced on one path and not the other.
    """
    now = now or datetime.now(timezone.utc)

    # Resolve the policy once per sweep. Two reasons, both found by round 6 of
    # the #3734 review, which measured ``promotion_policy`` as having NO
    # production caller:
    #
    # 1. It is what makes ``auto_promote``'s refusal WARNING reachable. An
    #    operator who sets the key true was previously told nothing, by
    #    anything — the refusal existed only in a function nothing ran.
    # 2. ``self_promote`` reaches the offer through ``audience_for``; surfacing
    #    the resolved policy on the result is what lets an operator confirm the
    #    knob is being read rather than infer it.
    policy = _promotion.promotion_policy(network)

    try:
        from arbiter import store as _astore

        existing = [
            p
            for subdir in CADENCE_SUBDIRS
            for p in _astore.iter_proposals(shared_dir, subdirs=(subdir,))  # type: ignore[arg-type]
        ]
    except Exception as e:  # noqa: BLE001 — reported, never swallowed
        logger.warning(
            "app_promotion_sweep: could not read the proposal store for %s (%s) — "
            "SKIPPING this bot rather than offering with no cadence memory",
            bot_id,
            e,
        )
        # Fail CLOSED. An unreadable store means the cadence cap has no memory,
        # and an offer made with no memory is exactly the pestering design §10
        # names. Skipping costs one day; offering blind costs the user's trust.
        return (
            SweepResult(
                bot_id=bot_id, considered=0, cadence_blocked=True,
                policy=policy.to_dict(),
            ),
            None,
        )

    recent = _promotion.recent_offer_count(existing, bot_id, now=now)
    manifests = list(read_manifests(bot_id))
    candidates = select_candidates(
        manifests, bot_id=bot_id, recent_offers=recent, now=now
    )
    skipped = tuple(
        (c.manifest_stem, tuple(getattr(c.decision, "blockers", ()) or ()))
        for c in candidates
        if not c.offerable
    )
    offerable = [c for c in candidates if c.offerable]
    if not offerable:
        return (
            SweepResult(
                bot_id=bot_id,
                considered=len(candidates),
                skipped=skipped,
                cadence_blocked=recent >= 1,
                policy=policy.to_dict(),
            ),
            None,
        )

    best = offerable[0]
    taken = [
        tid
        for tid in (
            _promotion.resolve_app_id(m) for _stem, m in manifests
        )
        if tid
    ]
    proposal = _promotion.build_promotion_proposal(
        best.manifest,
        bot_id=bot_id,
        manifest_stem=best.manifest_stem,
        network=network,
        readiness=best.readiness,
        taken_app_ids=taken,
        now=now,
    )
    if proposal is None:
        # Identity blocked — refuse rather than invent. Reported as a skip so
        # the reason is visible instead of looking like "nothing was eligible".
        return (
            SweepResult(
                bot_id=bot_id,
                considered=len(candidates),
                skipped=skipped + ((best.manifest_stem, ("identity_blocked",)),),
                policy=policy.to_dict(),
            ),
            None,
        )
    return (
        SweepResult(
            bot_id=bot_id,
            considered=len(candidates),
            offered=(best.manifest_stem,),
            skipped=skipped,
            policy=policy.to_dict(),
        ),
        proposal,
    )


def sweep_bot(
    shared_dir: Path,
    bot_id: str,
    *,
    network: Any,
    read_manifests: Callable[[str], Iterable[tuple[str, Mapping[str, Any]]]],
    now: datetime | None = None,
    dry_run: bool = False,
) -> SweepResult:
    """Offer at most ONE promotion for ``bot_id``. Design §7.2's cadence cap.

    ``read_manifests`` is injected rather than imported so this function can be
    exercised without a bot home; the CLI/daemon caller passes the real reader.

    ``dry_run`` scores and gates but writes nothing — which is how an operator
    can ask "what WOULD you offer?" without an offer landing in someone's
    channel. Given the pod's current state that answer is "nothing", and being
    able to show that without side effects is the point.

    Writes at most one Proposal, in ``pending``. Returns what happened either
    way, including the blockers for everything it declined.

    The DECISION lives in :func:`plan_offer`, which the scheduled generator path
    shares — this function is the direct-write half.
    """
    result, proposal = plan_offer(
        shared_dir,
        bot_id,
        network=network,
        read_manifests=read_manifests,
        now=now,
    )
    if proposal is None or dry_run:
        return result

    from arbiter import store as _astore

    proposal.status = "pending"
    try:
        _astore.write_proposal(proposal, shared_dir)
    except Exception as e:  # noqa: BLE001 — reported, never swallowed
        # A write that failed is NOT an offer, and must never be reported as
        # one: ``offered`` is what the cadence cap and the operator both read,
        # so a phantom entry would suppress tomorrow's real offer for an offer
        # that never reached anyone. Reported as a skip with the reason.
        stem = result.offered[0] if result.offered else "?"
        logger.warning(
            "app_promotion_sweep: failed to write the promotion proposal for "
            "%s on %s — NOT offered (%s)",
            stem,
            bot_id,
            e,
        )
        return replace(
            result,
            offered=(),
            skipped=result.skipped + ((stem, ("write_failed",)),),
        )
    logger.info(
        "app_promotion_sweep: offered promotion of %s on %s (audience=%s)",
        getattr(proposal.action, "app_id", "?"),
        bot_id,
        proposal.approval_audience,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — because a producer nothing runs is the F3 defect one level up
# ─────────────────────────────────────────────────────────────────────────────
#
# The round-1 review of #3734 found the gate had no caller. Shipping the caller
# with no entry point of its own would be the same defect moved up a level, so
# this module is runnable directly, following the convention CLAUDE.md already
# documents for ``signals.retention``:
#
#     python3 -m evolve_admin.applications.app_promotion_sweep --shared-dir … --dry-run
#
# **It is now also SCHEDULED, and this says by what** (brief §8.3 step 4,
# closed 2026-08-21). ``packages/analyzer/generators/app_promotion/`` carries the
# charter; ``generator_runner`` builds its context per member bot and calls
# ``observe()`` on the daily cadence, which runs :func:`plan_offer` and hands the
# Proposal to ``arbiter.ingest``. No LaunchDaemon and no cron entry were added —
# the daily generator sweep is an existing scheduled caller, so this introduces
# no new deploy surface and no new on-disk location on either pod.
#
# The charter also gives ``arbiter.track_record`` the registered generator it
# used to log "generator not loaded" about whenever one of these was accepted.
#
# The CLI stays, because "what WOULD you offer, right now, with no side
# effects?" is a question the scheduled path cannot answer:
#
#     python3 -m evolve_admin.applications.app_promotion_sweep --shared-dir … --dry-run
#
# **What scheduling does NOT do is make an offer appear.** ``eligible_to_offer``
# was re-measured at **0 across all 74 readable manifests on 2026-08-21**, after
# AL-1.6c's conversation-evidence wiring merged, and brief §8.3 step 3 records
# why: the ``recurrence`` dimension's carrier still has no writer, so the
# composite renormalises onto one dimension and ``MIN_DIMENSIONS_FOR_OFFER = 2``
# suppresses every draft. A scheduled sweep that proposes nothing is the correct
# behaviour today; it is what makes the number real rather than asserted.


def _read_manifests_for_bot(bot_id: str) -> list[tuple[str, Mapping[str, Any]]]:
    """The real reader: every manifest in the bot's canonical manifests dir.

    Uses the admin server's already-hardened helpers rather than re-deriving the
    path — they own the bot-user resolution and the permission fallbacks, and a
    second path derivation is how read and write end up on different files.
    """
    from pathlib import Path as _Path

    from ..web.server import _list_manifests_as_bot, _read_manifest_as_bot

    out: list[tuple[str, Mapping[str, Any]]] = []
    for raw_path in _list_manifests_as_bot(bot_id):
        stem = _Path(raw_path).stem
        manifest = _read_manifest_as_bot(bot_id, stem)
        if isinstance(manifest, dict):
            out.append((stem, manifest))
    return out


def _main(argv: Sequence[str]) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="app_promotion_sweep",
        description=(
            "Offer at most one app promotion per bot per day (design §7.2). "
            "Writes Proposals; never writes a manifest and never touches the "
            "scanner."
        ),
    )
    parser.add_argument("--shared-dir", required=True)
    parser.add_argument(
        "--bot",
        action="append",
        default=None,
        help="Bot id; repeatable. Omit to sweep every bot in network.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and gate, write nothing. Use this to ask what it WOULD offer.",
    )
    args = parser.parse_args(list(argv))

    from ..config import load_network

    network = load_network() or {}
    bots = args.bot or sorted((network.get("bots") or {}).keys())
    if not bots:
        print("app_promotion_sweep: no bots to sweep", file=sys.stderr)
        return 0

    results = [
        sweep_bot(
            Path(args.shared_dir),
            bot_id,
            network=network,
            read_manifests=_read_manifests_for_bot,
            dry_run=args.dry_run,
        ).to_dict()
        for bot_id in bots
    ]
    print(json.dumps({"dry_run": bool(args.dry_run), "results": results}, indent=2))
    # Exit 0 even when nothing was offered: "nothing eligible" is the expected
    # steady state today (§8.1), not a failure. A non-zero exit would make the
    # correct outcome look like a broken job to whatever schedules it.
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
