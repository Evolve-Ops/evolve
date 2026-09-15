"""signals — monitor-emitted observations, kept separate from RSI proposals.

Spec: internal/spec-alerts-signal-store-2026-05-07.md.

Public entry points are exposed by :mod:`signals.store` (find-or-create,
iter, transition, sweep-resolve) and :mod:`signals.state_machine` (legal
transitions). The Signal record itself lives in :mod:`schema.signal`.
"""
