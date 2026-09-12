"""Security Warden top-level evaluator for the arbiter veto pass.

Delegates to the scope evaluator. Future sub-spec work adds prompt-
injection and exfiltration evaluators here.
"""

from __future__ import annotations

from generators.security_warden.evaluators.scope import evaluate_scope
from schema.proposal import Proposal
from schema.veto import VetoResult


def evaluate(proposal: Proposal) -> VetoResult | None:
    """Return a VetoResult from the first evaluator that weighs in.

    Evaluators are tried in order; the first one returning non-None wins.
    L3 ships only the scope evaluator; further evaluators land in the
    Security Warden Completion sub-spec.
    """
    for evaluator in (evaluate_scope,):
        verdict = evaluator(proposal)
        if verdict is not None:
            return verdict
    return None
