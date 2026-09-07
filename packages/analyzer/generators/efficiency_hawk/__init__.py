"""generators.efficiency_hawk — Efficiency optimizer (L6).

Spec §6. Detects (noun × verb) clusters with unusually high average turn
count, proposes workflow / AGENTS.md notes that may help streamline.
"""

from generators.efficiency_hawk.observe import EfficiencyHawkContext, observe

__all__ = ["EfficiencyHawkContext", "observe"]
