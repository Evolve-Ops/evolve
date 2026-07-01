# Spec: Exporting Scanned Manifests

**Date:** 2026-06-02
**Status:** Draft
**Extends:** [docs/spec-export-import-forge-2026-05-26.md](spec-export-import-forge-2026-05-26.md) — adds a pre-stage to handle scanner-discovered apps that have no `build_spec` to sanitize.

---

## 1 — Why this exists

The current export pipeline ([spec-export-import-forge-2026-05-26.md §4](spec-export-import-forge-2026-05-26.md)) is designed for **forged** instances. Its Stage 3 *"Generalize prose"* explicitly affects `build_spec`, validation (Stage 5) checks the authoring-guide rules which assume a build_spec is present, and both worked examples (Atlas, personal-health) are forged apps with rich provenance.

But **the common case on this pod is scanned, not forged.** All of team-bot-a's ten manifests, all of team-bot-c's eleven, team-bot-d's, security-bot's, atlas's, team-bot-b-account's, team-bot-b's, personal-bot-b's — every existing app — were discovered by the scanner, not built by forge. Personal-bot's two apps (task-manager, ea-pack) are the only forge-installed manifests on the pod. If export only works on forged manifests, it can't share anything we've built so far.

What scanned manifests lack, by field:

| Field | Forged | Scanned |
|---|---|---|
| `build_spec` | full prose contract | absent or empty |
| `pkg_id` / `pkg_version` / `gallery_version` | minted by forge | empty |
| `source` / `source_detail` | `gallery_installed` / job_id | absent |
| `improvement_history` | install run record | empty |
| `files[].file_id` / `owned_by` / `created_in_run` | minted by forge | path only |
| `interface_contract.populated_by_forge` | `true`, populated by extractor LLM | populated by scanner LLM (different shape, may be partial) |
| `last_test_output` / `last_test_exit_code` | forge test transcript | absent |

The export pipeline needs a pre-stage that synthesizes the missing fields so the rest of the pipeline can run unchanged.

---

## 2 — The unifying principle

**Export is a reverse-engineering operation when the source is scanned.** The Sanitizer can't generalize a build_spec that doesn't exist; we have to derive one first by reading the actual code. The derived build_spec is then validated by trying to round-trip-forge it on a sandbox bot and diffing the output against the original.

> If the round-trip produces a recognizably equivalent app, the derived spec is good enough to share.
> If it doesn't, the spec needs iteration — either by tightening the derivation prompt, or by an operator-edit step.

This makes export-of-scanned a strictly stronger test of the framework than export-of-forged. A successful team-bot-a→gallery→personal-bot round-trip means the framework can take any existing app on the pod, generalize it, and reproduce it elsewhere from the manifest alone.

---

## 3 — Pipeline changes

Add a **Stage 0** before the existing Stage 1 (Refresh). Run only when `manifest.pkg_id` is empty AND `manifest.build_spec` is empty (the two reliable signals of a scanned source).

```
┌─────────────────────────────────────────────────────────────────┐
│  Stage 0: Derive (NEW — only for scanned manifests)             │
│    0a. Mint identifiers — pkg_id, pkg_version, file_ids         │
│    0b. Derive build_spec from code via LLM                      │
│    0c. Derive interface_contract via existing extractor LLM     │
│    0d. Round-trip validate — re-forge on sandbox, diff           │
│    0e. Operator review of derived spec (mid-pipeline gate)      │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
   (existing Stages 1–7 run unchanged — Refresh, Classify,
    Generalize, Scrub, Validate, Operator Review, Stamp+Write)
```

### 3.1 — Stage 0a: Mint identifiers

Deterministic, no LLM:

- `pkg_id` = `p-` + first 8 hex chars of SHA256(bot_id + manifest.id + scan_timestamp). Stable across re-exports of the same scanned manifest from the same bot.
- `pkg_version` = `{YYYY.MM.DD}-1.0` for first export; subsequent exports increment the patch.
- `gallery_version` = same as `pkg_version` on first export.
- `source` = `scanned_export`.
- `source_detail` = `bot:{bot_id} scan:{scan_run_id}`.
- For each `files[]` entry: assign `file_id` = `f-` + first 8 hex chars of SHA256(path + pkg_id), `owned_by` = pkg_id, `created_in_run` = a synthesized `r-` id.

These exist for forged manifests already; this just synthesizes the same shape for scanned ones.

### 3.2 — Stage 0b: Derive build_spec from code

LLM-driven. Reads the actual workspace files (those classified as `build artifact` in the existing §6 decision tree) and produces a build_spec following the manifest-authoring-guide format.

**Inputs to the deriver:**
- The full text of every `files[]` entry classified as build artifact (scripts, helpers, hooks).
- The existing manifest's `description`, `identity`, `success_criteria`, `constraints` (scanner-populated, useful as anchors).
- The bot's TASKS.md / TAGS.md / similar docs adjacent to the app.
- The user-profile summary (for de-personalization context — same input the Sanitizer uses in Stage 3).
- The authoring-guide template (so the LLM knows the target shape).

**Prompt shape (informal):**

> You are reverse-engineering an application build spec from its existing code. The code below was written by hand for a specific bot and is in production use. Your job is to produce a build_spec that, if handed to a different bot's LLM, would reproduce a structurally equivalent app.
>
> Read the code carefully. The build_spec must:
> 1. Describe the file layout (paths relative to workspace root).
> 2. Describe the data schema (storage format, fields, enums, terminal states).
> 3. List every CLI command with its flags and exit-code contract.
> 4. Document invariants the code enforces (atomic writes, monotonic IDs, etc.).
> 5. Capture customization points (TAG_ALIASES, categories) with a "customize per bot's domain" note.
> 6. List a test suite that would verify a fresh build.
> 7. **Generalize away** any bot-specific paths, names, integrations, or business rules. The user is {user_profile_summary}; their bot is {bot_id}; their domain is {bot_domain}. Strip references to those.
>
> Code: ```{code_bundle}```
> Existing manifest description: {description}
> Existing manifest identity: {identity}
>
> Return JSON: {"build_spec": "<full markdown text>", "customization_points": [{...}], "notes_for_operator": "<anything ambiguous>"}.

**Default posture:** when ambiguous, lean toward *spec capturing what the code does today*. If the code has bot-specific quirks the deriver thinks are intentional vs. accidents, surface both options in `notes_for_operator` and let the operator decide at Stage 0e.

**Model choice:** Sonnet, not Haiku. Reverse-engineering 38 KB of `unified_task_system.py` to a clean spec is harder than the Sanitizer's prose generalization; the cost premium is justified per-export (~$0.30–$0.80).

### 3.3 — Stage 0c: Derive interface_contract

The existing forge "extractor" LLM ([forge_engine.py BUILTIN_EXTRACTOR_PROMPT](../packages/admin/evolve_admin/applications/forge_engine.py)) already does this — given code, it produces the `interface_contract` block (`data_files`, `cli`, `key_paths`, `enums`, `terminal_states`, `signal_prefixes`).

Reuse the extractor verbatim. Mark the resulting `interface_contract.populated_by_forge = true` and stamp `extracted_at`.

### 3.4 — Stage 0d: Round-trip validate

The key novel piece. Before we trust the derived build_spec, prove it actually round-trips:

1. **Pick a sandbox bot.** A bot specifically used for export validation (separate from production bots). The pod operator nominates one in pod config; default to a fresh ephemeral bot user spun up for the run.
2. **Run forge** on the sandbox using the derived `build_spec`, `interface_contract`, and the rest of the synthesized manifest. Same forge pipeline as a fresh install (Build → Critique → Test → Gate → Apply), with auto-approve since this is a validation run.
3. **Diff** the sandbox forge output against the source's actual files:
   - For each `files[]` entry in the source, compare to the corresponding sandbox file.
   - Compute a structural diff: AST-level for Python (function names, signatures, class structure), string diff for shell + markdown.
   - Categorize each diff as: `equivalent` (same shape, different identifiers), `customization` (the derived spec captured a known customization point so divergence is expected), `divergence` (substantive structural difference).
4. **Score the round-trip.** Acceptance bar: every `files[]` entry must be `equivalent` or `customization`. Any `divergence` blocks the export pending operator review (Stage 0e).

This stage is expensive — a real forge run, ~$0.50–$2 in LLM cost + a few minutes of wall time. Run it once per export attempt; cache the verdict against `(pkg_id, source_files_sha256)` so repeat exports of the same scanned app skip it.

**Why this matters:** without round-trip validation, we have no idea if the derived spec is faithful. The Sanitizer in Stage 3 will happily generalize a broken spec into a broken sharable spec. The round-trip is the only honest check that the reverse-engineering worked.

### 3.5 — Stage 0e: Operator review (derived-spec gate)

Distinct from the existing Stage 6 review. This one shows the operator:

1. **Derived build_spec** — the full prose, with the deriver LLM's `notes_for_operator` block.
2. **Round-trip diff summary** — per-file verdict (equivalent / customization / divergence) with counts.
3. **Customization points** the deriver identified — operator confirms these are real customization seams or rejects.
4. **Divergences** if any — operator decides: accept (the divergence is acceptable), edit the build_spec to fix, or abort.

Operator can:
- Approve → continue to existing Stage 1.
- Edit the build_spec, re-run Stage 0d, re-review.
- Abort → no gallery write.

This gate exists specifically because reverse-engineering is lossy. It gives the operator one explicit "is this the app we meant?" decision before the spec leaves the bot.

---

## 4 — Validation rule additions

The authoring-guide validators ([spec-export-import-forge-2026-05-26.md §8 implementation #1](spec-export-import-forge-2026-05-26.md)) need three new rules so derived specs are checked the same way as authored ones:

| Rule | Check |
|---|---|
| `scanned_export_completeness` | If `source = scanned_export`, then `build_spec`, `interface_contract`, all `files[].file_id`, and `pkg_id` must be populated. |
| `roundtrip_verdict_present` | If `source = scanned_export`, then `provenance.roundtrip_verdict` must exist with score + per-file diff summary. |
| `derived_spec_marker` | If `source = scanned_export`, the build_spec must carry a top-line marker (`<!-- derived-from-scan: bot:{bot_id} run:{scan_id} -->`) so downstream consumers can apply extra scrutiny. |

The marker is also what the *next* importer sees — if you adopt a derived spec, the marker travels with it and your bot knows "this came from reverse-engineering, expect higher residual lossiness than a forged-origin spec."

---

## 5 — Failure modes

| Failure | Recovery |
|---|---|
| Deriver LLM produces invalid build_spec (fails authoring-guide validation) | Retry once with the validation errors echoed back; on second failure surface to operator with the deriver's output for manual fix. |
| Round-trip forge fails (sandbox forge errors) | Surface forge errors to operator; treat the same as Stage 6 operator review — accept, edit, or abort. |
| Round-trip succeeds but diff shows substantive divergence | Block at Stage 0e gate; operator must explicitly accept or edit. |
| Sandbox bot unavailable | Surface — defer the export; offer to skip round-trip validation with a warning (the resulting gallery entry gets a `roundtrip_skipped: true` flag and is marked lower-trust). |
| Code references external secrets / API keys | Deriver should flag in `notes_for_operator`; operator confirms the spec abstracts them as `requirements.secrets[]` rather than leaking the values. |

---

## 6 — Worked example: team-bot-a's Unified Task System → gallery

**Source:** team-bot-a, `manifest i-9c16b1c7`, `files[0].path = ops/tools/unified_task_system.py` (38 KB), workspace has TASKS.md / AGENTS.md / no TAGS.md.

**Stage 0a — Mint:**
- `pkg_id` = `p-` + sha256("team-bot-a" + "i-9c16b1c7" + scan_ts)[:8] → e.g. `p-3f8a1234`
- `pkg_version` = `2026.06.02-1.0`
- `source` = `scanned_export`
- File IDs minted deterministically.

**Stage 0b — Derive build_spec:**
Deriver reads `unified_task_system.py`, team-bot-a's `AGENTS.md` § "PENDING TODOS" section (the tombstone pointer), the `tasks.json` schema, the `prune-expired` and `--expires` features team-bot-a has that team-bot-c/personal-bot don't. Produces a build_spec that:
- Specifies the dict-keyed `tasks.json` schema (same as gallery's `p-9bfa1c84` — should converge).
- Specifies `unified_task_system.py` (vs. gallery's `tasks.py` — naming divergence flagged in `notes_for_operator`).
- Specifies `--title` not `--name` (team-bot-a's idiom), `--expires` (team-bot-a has TTLs), `prune-expired` command (team-bot-a-specific).
- Generalizes team-bot-a's `created_by="team-bot-a"` default to `created_by="{bot_id}"`.
- Strips the `pending_todos.json` tombstone reference — that's team-bot-a-historic and shouldn't ship.

**Stage 0c — interface_contract:**
Extractor identifies the richer CLI surface (team-bot-a has `expire`, `prune-expired`, `--title` instead of `--name`). The derived `enums` and `terminal_states` should match the existing gallery `p-9bfa1c84` because the underlying model is the same.

**Stage 0d — Round-trip:**
Forge runs on sandbox bot with the derived build_spec. Sandbox produces:
- `scripts/unified_task_system.py` (sandbox) vs. `ops/tools/unified_task_system.py` (team-bot-a). Path divergence → expected; the derived spec asks for `ops/tools/` per team-bot-a's idiom. Equivalent if path matches; if forge insisted on `scripts/`, that's a divergence.
- AST diff: function-by-function. `cmd_add`, `cmd_list`, `cmd_prune_expired`, etc. all present and signature-equivalent → equivalent.
- `TASKS.md`: structurally similar to team-bot-a's (markdown headings match, table columns match) → customization (free-form prose always diverges; we only check structure).
- Verdict: `~22 equivalent / ~3 customization / 0 divergence` → approved for Stage 0e.

**Stage 0e — Operator review:**
Operator sees the derived spec, agrees `unified_task_system.py` is the right filename (not `tasks.py`), notes the `--expires` feature is genuinely team-bot-a's addition worth promoting to the gallery, and approves. Optionally edits the build_spec to clarify that `prune-expired` is required behavior (vs. optional).

**Stages 1–7:** Run as for forged manifests. Sanitizer scrubs team-bot-a-specific bot name, team-bot-a's category list, team-bot-a's tag aliases (or generalizes them as customization points). Stage 6 review happens again at the end.

**Output:** `{shared_dir}/gallery/local/p-3f8a1234/2026.06.02-1.0.json` — a sharable Spec for team-bot-a's Unified Task System, distinct from the existing `p-9bfa1c84` Task Manager. Both live in the gallery; operators choose which to install based on whether they want the simpler model (p-9bfa1c84) or the richer one with TTL/prune (p-3f8a1234).

---

## 7 — Open questions

1. **Should derived specs and forged specs share a gallery namespace?** Yes per [spec-export-import-forge-2026-05-26.md §9](spec-export-import-forge-2026-05-26.md) (both go under `gallery/<tier>/<spec_id>/`), but the UI should display the source: `scanned_export` vs `gallery_installed` vs `local_authored`. Operators want to see "this came from reverse-engineering team-bot-a's actual install" as a provenance signal.

2. **How does this interact with the existing gallery `p-9bfa1c84`?** Team-bot-a's derived spec is structurally a sibling of the existing Task Manager. Should we offer a "merge" path that promotes team-bot-a's `--expires` feature into `p-9bfa1c84` v2? Probably — but as a separate operator-driven step, not auto. The deriver should *flag* the overlap in `notes_for_operator` ("a similar app exists at p-9bfa1c84 — consider merging").

3. **Sandbox bot lifecycle.** Stage 0d requires a sandbox bot. Options: (a) a dedicated long-lived test bot (`evolve-export-sandbox`), (b) ephemeral user account spun up per export, (c) reuse `forge`'s existing dispatch mechanism with a `--dry-run` mode. (a) is simplest; (b) is cleanest isolation; (c) is least new infrastructure. Defer to implementation.

4. **Round-trip cost ceiling.** $0.50–$2 per export is fine for occasional use, expensive if we ever batch-export an entire pod. Consider a `--skip-roundtrip` flag for batch operations with the trade-off explicit (`roundtrip_skipped: true` lowers trust on the resulting gallery entry).

5. **What if the scanned manifest is wrong?** Scanner clusters files into apps by LLM judgment; sometimes the clustering is imperfect. The deriver inherits whatever clustering the scanner did. If `files[]` includes a file that doesn't actually belong, the derived spec will incorporate it spuriously. Mitigation: the Stage 0e operator review shows the file list explicitly; operator can drop spurious files before the spec is finalized.

---

## 8 — Implementation plan

Slots into the existing plan ([spec-export-import-forge-2026-05-26.md §9](spec-export-import-forge-2026-05-26.md)) as additions, not replacements:

| # | Work | Depends on | LOC est |
|---|---|---|---|
| 1 | Stage 0a — identifier mint (deterministic) | nothing | ~80 |
| 2 | Stage 0b — deriver LLM client + prompt + parser | extractor pattern in forge_engine.py | ~300 |
| 3 | Stage 0c — reuse existing extractor (no new LOC) | forge_engine.py | 0 |
| 4 | Stage 0d — round-trip orchestrator (forge dispatch + AST diff + scoring) | forge dispatch | ~400 |
| 5 | Stage 0e — operator review surface for derived spec | existing review UI | ~UI work |
| 6 | Three new authoring-guide validators (§4) | existing validator infra | ~80 |
| 7 | End-to-end team-bot-a-task-manager export test | round-trip orchestrator | ~250 |

Total ~1110 LOC + UI. Adds about 50% to the existing export-import implementation budget. Worth it: scanned export is the dominant case on the pod and the stronger test of the framework.

**Sequencing:** Items 1–3 first (the synthesis without validation). Then item 6 (validators that gate the new fields). Then item 4 (the round-trip — this is where most novel infrastructure lives). Item 5 UI work can layer in alongside. Item 7 is the end-to-end test that validates the whole chain.

---

## 9 — Relationship to the spec I just wrote

This spec doesn't conflict with [spec-forge-side-effects-2026-06-02.md](spec-forge-side-effects-2026-06-02.md). They address adjacent concerns:

- **spec-forge-side-effects** fixes what forge *installs* (cron/hook/LaunchAgent materialization).
- **spec-scanned-export** fixes what export *consumes* (derives the missing build_spec for scanned manifests).

Both feed the same end-to-end loop you outlined: scan team-bot-a → export team-bot-a → adopt on personal-bot → verify install works including side-effects. The forge-side-effects spec covers steps 6–8 of that loop (the install/verify side); this spec covers step 4 (the export side).
