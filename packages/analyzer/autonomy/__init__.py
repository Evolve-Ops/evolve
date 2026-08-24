"""autonomy — per-bot, per-integration autonomy posture (U4.1).

Spec: internal/spec-autonomy-ladder-2026-06-10.md.

Modules:
  - catalog   — kind semantics as data (rungs, verbs, defaults, copy)
  - store     — the intent file at {shared_dir}/bots/<bot>/autonomy.json
  - renderer  — intent → enforcement surfaces (MCP tool deny, guidance)
  - backfill  — observe-first inference for pre-existing integrations
  - coherence — posture↔enforcement drift findings for the audit
"""
