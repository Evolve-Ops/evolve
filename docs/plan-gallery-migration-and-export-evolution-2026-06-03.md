# Plan: Gallery migration + export-pipeline evolution

**Date:** 2026-06-03
**Status:** Plan (concrete sequence of work; not a spec)
**Related:** [docs/spec-files-pack-hybrid-2026-06-03.md](spec-files-pack-hybrid-2026-06-03.md) (the install-side architecture), [docs/note-gallery-shape-shift-2026-06-03.md](note-gallery-shape-shift-2026-06-03.md) (the why), [docs/spec-scanned-export-2026-06-02.md](spec-scanned-export-2026-06-02.md) (the existing export pipeline)

---

## 0 — What this plan covers

Two things, tied together:

1. **Concretely migrating the 13 existing gallery packages** to the folder-per-app layout (manifest + `files/`). One PR per app for reviewability; per-app source-bot choice + validation checklist.
2. **How the export-for-sharing pipeline evolves** to support the new layout — both for *bringing apps into the gallery* (scanned-export pipeline) and for *operators sharing their own apps* (the protein-tracker scenario from the note).

These are the F-P.7 (migration) and F-P.8 (export evolution) PRs in the files-pack hybrid sprint, written here as a plan rather than a spec because the right shape of each emerges from doing the first few.

---

## Part A — Gallery migration plan

### A.1 — Acceptance criteria (per app)

Before a per-app migration PR can merge, the new `gallery/<slug>/files/` must:

1. **Load + verify cleanly** — `pytest packages/admin/tests/test_files_pack_integrity.py` is green (the F-P.4 sweep that walks every files-pack in the repo).
2. **No orphan reserved tokens** — same test, the orphan-pattern linter pass returns no findings.
3. **Round-trip-install on a fresh bot** — operator runs `evolve-admin gallery install <pkg_id> --bot <fresh-test-bot>` and the v17 acceptance check (runbook §6 of the scanned-export runbook, adapted: HEARTBEAT.md sections + markers + scripts present + manifest stamped + INSTALLED_APPS.md updated) passes.
4. **The package manifest's `files_pack` block is populated** — `format_version`, `files_count`, `snapshot_source_pkg_version`, `sha256` set.
5. **An entry in `gallery/index.json` reflects the new `pkg_version`** (typically the next minor bump from current).

### A.2 — Per-app migration table

For each existing gallery package: an initial guess at which bot to snapshot from, the reason, and special considerations. The "snapshot from" column is a *starting point*; the operator may pick a different source bot if it has a cleaner install.

| # | Slug | pkg_id | Snapshot from | Why this bot | Special considerations |
|---|---|---|---|---|---|
| 1 | task-manager | p-9bfa1c84 | atlas | Clean v17 install verified 2026-06-03 — Phase 4.5 ran end-to-end, HEARTBEAT.md section + marker present, all files on disk | Reference / pilot migration. If this works, the rest follow the same pattern. |
| 2 | ea-pack | p-aab5e569 | personal-bot reference | EA Pack scripts on this bot's `scripts/` from the 2026-06-01 install | Cross-app dep: depends on task-manager. Migrate task-manager first; this one's files-pack metadata can reference `app_dependencies` cleanly. |
| 3 | unified-task-system | p-c20a5564 | personal-bot reference | Hand-finished install from this session (2026-06-03); known-good state | Snapshot should NOT include the tasks.json file (it's runtime data, not gallery code). Verify the F-P.3 snapshot tool honors data-file role exclusion. |
| 4 | contacts | p-4136a932 | personal-bot reference | EA Pack's commitment tracker emits contacts files; this bot has the canonical pattern | Mostly markdown templates; small number of placeholders (`{bot_id}` in path patterns) |
| 5 | journal | p-fb9141b4 | personal-bot reference | One file per day pattern; this bot has the canonical layout | Heavy on date-based filename templates; likely no `{bot_id}` substitution needed in content |
| 6 | workspace-backup | p-f9bce546 | personal-bot reference | Daily git backup pattern, runs as launchd at 2am | Sensitive: contains a path to the remote backup repo — needs to be a placeholder, not hardcoded |
| 7 | calendar-sync | p-fe9acef3 | personal-bot reference | Bot with Google Calendar OAuth configured | OAuth tokens live separately (network.json credentials block); snapshot should NOT include them |
| 8 | email-integration | p-341576fa | personal-bot reference | Bot with Gmail OAuth configured | Same OAuth caveat as calendar-sync |
| 9 | github-integration | p-f047a60f | atlas (or personal-bot) | Whoever has the canonical watched-repos config | Watched-repos list is operator-specific config, not code — needs to default to `[]` in the files-pack |
| 10 | morning-briefing | p-a9a74bf7 | personal-bot reference | Has the EA Pack + calendar + email integrations all wired | Cross-app dep: many. The files-pack should NOT bundle dep files; just the briefing script itself. |
| 11 | note-taker | p-f14e9562 | personal-bot reference | Obsidian-integration bot | Vault path is operator-specific — needs to be a config placeholder, not a `{workspace}` substitution |
| 12 | calendar-summary | p-738f057c | personal-bot reference | Calendar-only digest, configurable delivery time | Configurable delivery time = `{user_locale}` candidate (F-P.8 personalization). For F-P.7, hardcode a sensible default; flag for F-P.8 follow-up. |
| 13 | email-triage | p-41e4c5f4 | personal-bot reference | Twice-daily email triage | Operator-defined urgency rules — operator-specific config, NOT code. Default to a sensible starter set. |

### A.3 — Sequencing

Migrations land in **dependency order** so a chain doesn't break:

```
1. task-manager (foundation; no deps)
2. journal (no deps)
3. contacts (no deps)
4. workspace-backup (no deps)
5. unified-task-system (no deps; alternative to task-manager)
6. calendar-sync (no deps; integration)
7. email-integration (no deps; integration)
8. github-integration (no deps; integration)
9. ea-pack (depends on task-manager + contacts)
10. note-taker (depends on calendar-sync)
11. morning-briefing (depends on task-manager + calendar-sync + email-integration)
12. calendar-summary (depends on calendar-sync)
13. email-triage (depends on email-integration)
```

Each migration is its own small PR (one app, one bump). 13 PRs total.

### A.4 — Migration steps (per app)

For each row in the table:

1. **Read on the source bot.** SSH into the mini as the operator, run `evolve-admin snapshot-files-pack --bot <source-bot> --pkg <pkg_id> --out /tmp/<slug>-files`.
2. **Review.** Diff the `/tmp/<slug>-files/manifest.json` against the special-considerations column. Confirm no OAuth tokens, no runtime data, no operator-only config bundled.
3. **Move into the dev checkout.** Copy `/tmp/<slug>-files/` to `gallery/<slug>/files/` on the local clone.
4. **Update the package manifest.** Edit `gallery/<slug>/p-XXX.json`:
   - Add the `files_pack` block (format_version, files_count, snapshot_source_pkg_version, sha256 computed via `compute_files_pack_sha256`)
   - Bump `pkg_version` (typically a minor bump, e.g. `2026.06.03-1.3` → `2026.06.03-1.5` to signal the files-pack landing)
5. **Bump `gallery/index.json`** to match the new `pkg_version`.
6. **Local verification** — `pytest packages/admin/tests/test_files_pack_integrity.py` is green; `pytest packages/admin/tests/test_public_launch_scrub.py` is green.
7. **Open PR** — title `feat(gallery): <slug> vX.Y — add files-pack`; body cites the F-P.5/F-P.7 framing + the per-app special considerations.
8. **End-to-end validation** — after merge + repo-puller cycle, install on a fresh test bot and confirm acceptance criteria A.1.

### A.5 — Rollback (if a migration goes wrong)

The files-pack install path is **best-effort fall-through**: any error returns None and the dispatcher takes the LLM-forge path. So a bad files-pack doesn't break installs — it just fails to save the cost. Rollback options, in order of preference:

1. **Open a follow-up PR that removes the `files_pack` block from the package manifest.** The dispatcher then ignores the files-pack directory entirely. The files-pack stays on disk but is dormant.
2. **Delete the `files/` subdirectory.** Same effect as #1 but more invasive.
3. **Revert the migration PR.** Last resort; only if the manifest changes are deeply tangled with something else.

---

## Part B — Export-pipeline evolution

### B.1 — Two distinct export paths, both evolving

**Path 1: Scanner discovery → gallery candidate** (the scanned-export pipeline, PRs #2005–#2010, S1–S5)

How apps that exist on the pod (forge-generated, or hand-written by operators) become gallery packages. Today's flow:
- Scanner-discovered manifest (rich identity, sparse forge fields)
- Stage 0a-0d derives `build_spec`, `interface_contract`, identifiers
- Operator reviews + publishes to `gallery/<slug>/p-XXX.json`

**After files-pack lands**, this path needs one additional stage:

**Stage 0f — files-pack snapshot.** After Stage 0e operator review of the *manifest*, the operator runs a snapshot step that produces the `gallery/<slug>/files/` directory from the source bot's installed files. The same F-P.3 CLI (`snapshot-files-pack`) is reused — the export pipeline just invokes it as one of its stages.

The operator-review surface (Stage 0e, today's `/export-review` page) grows a "snapshot files" button that runs F-P.3 and shows the resulting files-pack alongside the draft manifest for review.

**Path 2: Operator's existing install → shareable gallery package** (new path; partially built by F-P.3)

The scenario from the architectural note: someone built a protein-tracker on their bot and wants to share it.

After all of F-P.* lands, this becomes:
1. `evolve-admin export-app --bot <yours> --slug protein-tracker` (a new CLI; subsumes F-P.3's `snapshot-files-pack` and adds the manifest-side work)
2. The CLI runs:
   - Snapshot files-pack (F-P.3 logic)
   - Derive a build_spec from the source code (S1's deriver logic)
   - **Personalization scrub (F-P.8)** — see B.2 below
3. Operator reviews via a similar `/export-review` page — both the manifest + the files-pack candidate substitutions
4. Operator publishes: writes the package manifest + files-pack to the operator's local fork of the evolve repo
5. Operator commits, opens a PR upstream (or to a community gallery, eventually)

### B.2 — The personalization scrub (F-P.8 deepened)

Two categories of substitution need to happen at export time. Today (F-P.1–F-P.4) we've handled the first. F-P.8 adds the second.

| Category | Examples | Detection source | Substitution target |
|---|---|---|---|
| **Infrastructure** | `/Users/<bot>/`, `com.<bot_id>.`, workspace paths | network.json bot users + bot ids, plus pattern matching | `{bot_id}`, `{bot_user}`, `{workspace}`, `{shared_dir}`, `{pkg_id}`, `{app_id}`, `{installed_at}` (the v1 KNOWN_PLACEHOLDERS set) |
| **Personalization** | The operator's name, email, locale, custom preferences embedded in app text | Per-bot profile (the same one user_profile_inferrer maintains passively); pattern matching against the operator's known tokens | `{user_name}`, `{user_handle}`, `{user_locale}`, `{user_email}` (new v2 KNOWN_PLACEHOLDERS in the files-pack format) |

**F-P.8 implementation outline:**

1. **Extend `KNOWN_PLACEHOLDERS`** to include the user-* names. Bump `FILES_PACK_FORMAT_VERSION` to "2.0".
2. **Extend `resolve_install_context()`** to accept the additional values; default-source them from the bot's profile when not explicitly provided.
3. **New helper `detect_personalization_candidates(content, profile)`** — scans for the operator's name, email, and other profile-derived tokens. Returns a list of (line, suggestion) pairs the operator reviews.
4. **CLI integration** — `evolve-admin export-app` adds an interactive (or `--non-interactive` batch) review step where each personalization candidate is shown with three options:
   - **Substitute** with the suggested placeholder
   - **Keep verbatim** (it's the app's intent — e.g. a literal "Welcome" string)
   - **Redact** with a sample value ("Alex", "team@example.org", etc.)
5. **Per-file metadata grows** — the files-pack manifest's `placeholders[]` list now includes user-* names alongside infrastructure ones.

**Why this needs F-P.7 first.** The personalization scrub is design-by-corpus: we'll see what real personal data looks like in the existing 13 gallery apps. Inventing the detection heuristics in the abstract risks missing edge cases. After the F-P.7 migration runs, we have 13 known files-packs to validate the F-P.8 scrub against.

### B.3 — Operator-facing surface for B.1 / B.2

The admin UI already has an `/export-review` page (Stage 0e, PR S4, #2009). It will grow:

- **A "Files" tab** alongside the existing manifest tab, showing each file in the candidate files-pack with detected substitution hints (infrastructure + personalization). Operator clicks to accept or override per-file.
- **A "Personalization Review" prompt** when F-P.8 ships — modal/inline per candidate with the three buttons.
- **A "Publish to local fork" button** that writes both the manifest and the files-pack to the operator's local clone, ready for `git commit`.

### B.4 — Trust + safety for community sharing

When the workflow extends to operators sharing apps with *each other* (not just contributing upstream to the evolve repo), three additional checks need to run before publish:

1. **No remote-code execution** — files-pack must not include downloaded binaries; only source files committed at snapshot time.
2. **No outbound network credentials** — files-pack must not include auth tokens, even ones the operator forgot they pasted into a config file. F-P.4's orphan linter can grow patterns for common credential shapes (PAT prefixes, OAuth client secrets, etc.).
3. **License declaration** — the package manifest grows a `license` field. Default to MIT or a permissive license; community-shared packages MUST declare one.

These layer cleanly on top of F-P.8's personalization scrub. F-P.9 (or later) covers the community-sharing trust layer when there's actual demand.

### B.5 — Sequence of work (B.* PRs)

After F-P.7 finishes (all 13 packages migrated):

- **F-P.8** — personalization scrub: extend KNOWN_PLACEHOLDERS, detect+suggest helper, CLI integration, files-pack v2.0 format
- **F-P.9** — Stage 0f in scanned-export pipeline: snapshot button on the operator-review surface
- **F-P.10** — `evolve-admin export-app` unified CLI (subsumes snapshot-files-pack + scanned-export Stage 0a-0e + F-P.8 scrub)
- **F-P.11** (future, on demand) — Community-sharing trust layer

Each is small enough to ship as one PR. The sequence is bounded; no open-ended "rewrite forge" sprint.

---

## Part C — Implementation tracking

### C.1 — PR inventory (this sprint)

Already in flight:
- ✅ F-P.1 (#2040) — foundation library
- ⏳ F-P.2 + F-P.3 (#2041) — install dispatcher + snapshot CLI
- ⏳ F-P.4 (#2043) — orphan linter + gallery integrity test

Next, in order:
- **F-P.5** — first gallery republish (task-manager v1.5 with files-pack snapshotted from atlas)
- **F-P.6** — operator runbook for snapshot + install + roll back
- **F-P.7** — 12 separate PRs, one per gallery package, in the dependency order from A.3
- **F-P.8** — personalization scrub
- **F-P.9** — scanned-export Stage 0f integration
- **F-P.10** — `export-app` unified CLI

### C.2 — Tracking the migration progress

Each F-P.7 sub-PR updates this section of the spec (or a new `docs/gallery-migration-status.md` that this plan delegates to) with:

```
| App | PR | Snapshot from | Notes |
|---|---|---|---|
| task-manager  | #NNNN | atlas              | clean v17 install; pilot migration |
| ea-pack       | #NNNN | personal-bot ref   | depends on task-manager; migrated 2nd |
| ...           | ...   | ...                | ... |
```

Updates incrementally as each migration lands. By the end of F-P.7, every row has a PR + a snapshot source documented.

### C.3 — Risk inventory

The migration introduces three risks worth naming so they're not surprises later:

1. **A snapshot captures runtime data the source bot accumulated.** Mitigation: F-P.3's existing logic excludes `role=data_file` entries from `manifest.files[]`. Per-app validation step (A.4 step 2) catches anything else.

2. **A snapshot captures personal data the operator forgot was in the source.** Mitigation: F-P.4's orphan linter catches reserved tokens. F-P.8's personalization scrub catches the broader category. Until F-P.8 lands, operator manual review at A.4 step 2 is the only catch — flag this in the F-P.7 PR descriptions so reviewers know to look.

3. **The files-pack drifts from the build_spec over time.** When an improvement run regenerates files, the files-pack and the build_spec can diverge. Mitigation: F-P.4's integrity sweep catches SHA mismatch. The `--regenerate` flag (F-P.2) covers the legitimate path of "update both together" once it ships. Until then, treat each F-P.7 migration as the canonical state of that app; subsequent updates either go through `--regenerate` or update files + build_spec in one PR.

---

## Part D — Net trajectory

After F-P.* completes:

- **Install cost**: ~$30 per app per bot → ~$0 per app per bot (gallery files-packs are the canonical artifact)
- **Heartbeat cost**: ~$5–9/day pod-wide → ~$0–0.50/day (Track A's signal-on-actionable mechanism, separate but combining)
- **Gallery shape**: 13 manifest files → 13 folders, each carrying contract + artifact
- **Export pipeline**: scanned-export pipeline gains a snapshot stage; new `export-app` CLI subsumes the operator-share workflow
- **Personal-data hygiene at export time**: orphan linter (F-P.4) + personalization scrub (F-P.8) catch the two categories of substitutable tokens before they land in the repo

Combined: the framework's app architecture becomes operationally cheap enough to ship a real gallery at real scale, AND the export-for-sharing story becomes coherent enough to put in front of operators who want to publish their own apps.

This is the work that closes the public-launch-readiness gap for the gallery side of the framework.
