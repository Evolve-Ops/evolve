"""proposal_synthesizer — substantiveness gate + LLM synthesizer.

Spec: internal/spec-proposal-synthesizer-2026-05-10.md.

Phase 1 (this package's current scope): the deterministic
substantiveness gate plus the on-disk candidate store. The LLM
synthesizer lands in Phase 3+ and shares this package.

Public entry points (Phase 1):

  - :mod:`proposal_synthesizer.store` — atomic IO + iteration helpers
    for the candidate store
  - :mod:`proposal_synthesizer.gate` — the four gate rules
    (repetition, magnitude, aggregation, concreteness)
  - :mod:`proposal_synthesizer.config` — thresholds, per-variant
    overrides
  - :mod:`proposal_synthesizer.run` — sweep + gate entrypoint
    (``python3 -m proposal_synthesizer.run``)
"""
