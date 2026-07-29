"""Content scanner for indirect prompt-injection patterns.

Spec: docs/spec-prompt-injection-scanner-2026-05-10.md.

Today's identity audit hashes the markdown files each bot reads at
session start (AGENTS.md, SOUL.md, HEARTBEAT.md, …) and signals on
diff. That catches *that* a file changed but is blind to *how* —
operator follow-up is manual diff against the backup.

This package extends the audit with a pattern matcher: out of every
hash-changed file, classify what kind of change happened. Benign
markdown edit → silent. Injection payload → fire a typed signal
with the matching excerpt + ±1 line of context.

Phase A scope:
  - Pattern catalog at {shared_dir}/policy/content-scan-patterns.json
    (operator-curated; bootstrapped with ~10 seed patterns).
  - Scanner runs every audit cycle; per-file hash cache means most
    cycles do near-zero work.
  - Evolve-marker allowlist for HTML comments — Evolve uses
    <!-- evolve-handoff:* --> and similar regions in AGENTS.md /
    POD_CONDUCT.md; naive HTML-comment matching without this gate
    would fire on every bot.
  - content_scan_match Signal type, dedupes per (bot, file, pattern_id,
    line_range).
  - Operator suppression flow with 30-day expiry.
"""
