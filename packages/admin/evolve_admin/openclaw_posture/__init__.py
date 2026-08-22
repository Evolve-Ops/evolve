"""OpenClaw Posture Doctor (OCP-rules).

A doctor for the OpenClaw configuration posture of every pod bot. Catches
capability regressions — changes that silently degrade what a bot can do
for its users.

See ``docs/spec-openclaw-posture-doctor-2026-05-20.md`` for the full
design. This package implements **Phase 0.5** of the spec: a standalone
single-state check for OCP013 (approval-surface gap), shipped before the
snapshot/gate infrastructure of Phases 1–3.

The rule registry will grow as later phases land; the module API is
stable from Phase 0.5 onward (``Finding``, ``DoctorResult``,
``run_doctor``).
"""

from .doctor import (
    APPROVAL_SURFACE_CAPABILITY,
    DoctorResult,
    Finding,
    run_doctor,
)

__all__ = [
    "APPROVAL_SURFACE_CAPABILITY",
    "DoctorResult",
    "Finding",
    "run_doctor",
]
