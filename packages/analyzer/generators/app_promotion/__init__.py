"""generators.app_promotion — the scheduled caller for the promotion offer.

AL-1.7 built the whole promotion path — the readiness score, the offer gate,
the Proposal, the bot's conversational hop, the applier — and then shipped it
with **nothing that runs it on a schedule**. ``app_promotion_sweep`` is a real
producer with a real entry point, but an entry point is not a caller: brief
``docs/build-AL-1.7-promotion.md`` §8.3 step 4 recorded that gap in the one
deliverable whose purpose is that the roadmap not read AL-1.7 as done, in round
2's phrasing — *a producer with no caller is not a producer.*

This package is that caller, by the route §8.3 step 4 itself recommends: a
charter under ``packages/analyzer/generators/`` makes ``generator_runner``'s
daily sweep the scheduler, and gives ``arbiter.track_record`` the registered
generator it previously logged "generator not loaded" about every time one of
these Proposals was accepted.

**What it adds, deliberately, is nothing but the call.** The decision — which
draft, whether it may be offered at all, and what the Proposal says — lives in
``evolve_admin.applications.app_promotion_sweep.plan_offer``, shared verbatim
with the operator CLI. A second copy of "may this be offered" is how a cadence
cap ends up enforced on one path and not the other.

**It proposes nothing on the operator's pod today, and that is correct.**
Re-measured 2026-08-21, after AL-1.6c's conversation-evidence wiring merged:
``files=74 with_draft_id=0 eligible_to_offer=0``, every draft blocked by
``not_ready`` because only one readiness dimension has a producer. Scheduling a
sweep does not manufacture a candidate, and the constants that would were not
touched (brief §2's standing rule).
"""

from .observe import GENERATOR_ID, PromotionOfferContext, observe  # re-export

__all__ = ["GENERATOR_ID", "PromotionOfferContext", "observe"]
