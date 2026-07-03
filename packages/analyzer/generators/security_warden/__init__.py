"""generators.security_warden — Safety guardian.

Specs:
  - L3 base: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §8
  - L6 sub-spec (in-progress, 1 of 3 detectors shipped):
    docs/archive/specs/spec-security-warden-completion-2026-04-18.md

Capabilities:
  1. Observation-stream scanning for credential exposure (L3)
  2. Tool-scope expansion evaluation during arbiter veto pass (L3)
  3. Observation-stream scanning for prompt-injection attempts (L6 §4)

Still deferred from the L6 sub-spec:
  - Exfiltration scanner (§5) — needs role + destination tagging in the
    capture pipeline before it can run cleanly
  - Machine-level probe daemon (§6)
"""

from generators.security_warden.observe import observe
from generators.security_warden.evaluate import evaluate

__all__ = ["observe", "evaluate"]
