"""Permission posture — per-bot inventory + posture classifier (Phase A read-only).

Spec: internal/spec-permission-posture-2026-05-10.md.

Three sub-surfaces, one composite posture per bot:
  - permission config in openclaw.json (tools.exec, tools.fs, tools.web,
    commands, sandbox)
  - exec approvals (exec-approvals.json — defaults + per-agent)
  - scheduled invocations (.openclaw/cron/jobs.json — agentTurn payloads)

Phase A is observe-and-classify only. The composite score
(tight | moderate | wide | open) and per-axis breakdown are surfaced
to the admin UI. Mutation, baselines, signals, and appliers come in
later phases.
"""
