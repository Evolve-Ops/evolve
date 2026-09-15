"""verify — Verification daemon and claim evaluation (L2).

Spec: docs/archive/specs/spec-rsi-layer-2-verify-and-sysadmin-2026-04-18.md §4.

Submodules:
  - ``daemon``: the cron entrypoint. Scans proposals in ``applied/`` for those
    past their claim window and dispatches outcomes.
  - ``evaluate``: pure claim-evaluation logic (direction × magnitude against
    a resolved metric value).
  - ``dispatch``: routes the outcome to succeeded / failed_reverted /
    failed_flagged / failed_revert_failed, calling the applier's revert
    when appropriate.
  - ``verification_result``: writes the audit artifact for each verified
    proposal.

The verify daemon is pure logic — no LLM calls — so it runs on a 5-minute
cron without meaningful cost.
"""

from verify.daemon import VerifyDaemon, run_once
from verify.evaluate import ClaimOutcome, evaluate_claim
from verify.dispatch import DispatchResult, dispatch_outcome
from verify.verification_result import VerificationResult, write_result

__all__ = [
    "ClaimOutcome",
    "DispatchResult",
    "VerificationResult",
    "VerifyDaemon",
    "dispatch_outcome",
    "evaluate_claim",
    "run_once",
    "write_result",
]
