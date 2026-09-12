"""generators.persona_tuner — Voice/tone optimizer (L6).

Spec §4. Detects sustained frustration patterns in observation tuples,
proposes AgentsAppend tone guidelines with evidence, and occasionally
SoulEdit proposals for deeper voice recalibrations (always human-approval).
"""

from generators.persona_tuner.observe import PersonaTunerContext, observe

__all__ = ["PersonaTunerContext", "observe"]
