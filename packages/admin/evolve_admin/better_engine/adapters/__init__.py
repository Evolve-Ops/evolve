"""
better_engine/adapters — Source adapters for the Better Engine.

The original adapter set was a collection of single-purpose readers; most of
that work has migrated to the generator → arbiter pipeline (proposals are
ingested via ``ProposalReaderAdapter``). The adapters that remain here are
the ones whose purpose is genuinely outside the RSI generator portfolio:
onboarding setup tasks and the empty-queue Whimsy fallback.
"""

from .onboarding import OnboardingAdapter
from .whimsy import WhimsyAdapter
from ..proposal_reader import ProposalReaderAdapter

__all__ = [
    "OnboardingAdapter",
    "WhimsyAdapter",
    "ProposalReaderAdapter",
]
