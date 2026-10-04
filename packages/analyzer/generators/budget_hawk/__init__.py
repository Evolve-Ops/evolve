"""generators.budget_hawk — Cost guardian (L3).

Spec: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §7.

Reads cost metrics (``cost.daily_usd`` etc.) and emits:
  - Investigation proposals when daily spend crosses the warn cap
  - TierAdjustment proposals when spend approaches the hard cap
  - Anomaly investigations on outlier days

Also implements ``evaluate(proposal)`` for the arbiter's veto pass —
proposals that would incur cost on a bot over its hard cap get vetoed
(unless urgency is operational_urgent or security_critical).
"""

from generators.budget_hawk.observe import observe
from generators.budget_hawk.evaluate import evaluate

__all__ = ["observe", "evaluate"]
