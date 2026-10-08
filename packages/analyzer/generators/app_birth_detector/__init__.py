"""generators.app_birth_detector — propose BuildApp for orphan file clusters.

Closes the audit gap: today RSI can `ManifestUpdate` an existing app or
`InstallApp` from the gallery, but nothing emits `BuildApp` to create a
brand-new app from scratch. The schema, applier, and forge_sweep for
BuildApp have existed since the spec-evo-wizard-2026-05-05 work — this
is the missing generator that drives them.

Signal: workspace files that don't appear in any manifest's `files[]`
record *and* that don't carry an embedded `# evolve: spec=...` /
`"_evolve": {...}` provenance marker (the realised-side ownership
check, added after the 2026-05-25 ops/tools/ false positive where the
detector counted forge-realised files as orphans). Conservative
heuristics to avoid spamming the proposal queue:

  - Only Python or shell scripts ≥ 8 non-blank lines (skips toy files).
  - Each must be paired with a sibling JSON/YAML data file in the same
    directory. Solo scripts in the workspace root are likely throwaways
    and don't qualify.
  - Cluster orphans by parent directory.

Per-cluster framing depends on what *else* lives in the same directory:

  - Fully unmanaged → BuildApp ("promote to a managed app"). Stub
    manifest carries the rebuild build_spec described below.
  - Mixed, single owning app → ManifestUpdate(add_files) targeting the
    existing app ("finish migration into <app>"). No forge dispatch —
    the bot already realised the rest of the app; we just attach the
    stragglers to its manifest so test runs and version tracking cover
    them.
  - Mixed, multiple owning apps → no proposal. Cross-app dirs need
    operator judgment, not an automated guess.
  - Fully managed → no proposal (no unmanaged scripts/data → no
    cluster in the first place).

Stub manifest the BuildApp branch carries:

  - id     = sanitized directory name
  - name   = Title Case of the directory
  - files  = the orphan list
  - build_spec = "Rebuild these files through the forge so they come
                 under proper lifecycle management. Existing content
                 reproduced below for reference."
  - test_exemption_reason = "Pending bot-side determination during forge"
                            (satisfies the forge test gate; bot can
                            replace with a real test during build)

The forge dispatch (which is now bot-driven, post PR#1150) will read
the build_spec, regenerate the files through the bot's own LLM, and
produce a clean v5-shaped manifest. Operator gates approval via the
arbiter's standard review queue.

Cadence: weekly. Generators emit deterministically per (bot, cluster)
so re-runs of the same situation surface the same proposal id and
self-resolve once the operator approves (orphans become claimed by
the new or expanded manifest's files).
"""

from .observe import GENERATOR_ID, observe  # re-export

__all__ = ["GENERATOR_ID", "observe"]
