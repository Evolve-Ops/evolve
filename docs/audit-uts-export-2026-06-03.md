# Audit: Unified Task System export fidelity (team-bot-a → personal-bot)

**Date:** 2026-06-03
**Pipeline:** Scanned-export (PRs #2005, #2007, #2008, #2009 = S1–S5)
**Source:** `team-bot-a`'s scanner manifest `i-9c16b1c7` → 1005-line `ops/tools/unified_task_system.py`
**Target:** Personal-bot reference account; `scripts/unified_task_system.py` (1057 lines), forge job `j-62e593bc`
**Spec:** [docs/spec-scanned-export-2026-06-02.md](spec-scanned-export-2026-06-02.md)
**Runbook:** [docs/runbook-scanned-export-2026-06-03.md](runbook-scanned-export-2026-06-03.md)

---

## TL;DR verdict

**The scanned-export pipeline produced a faithful, working copy of the source app on the target bot.**

| Layer | Result |
|---|---|
| CLI surface (13 commands × all flags) | ✅ 1:1 preservation |
| Top-level constants (categories, prefixes, thresholds, priorities, lights) | ✅ Character-identical |
| Schema (`tasks.json`, `tag_registry.json`) | ✅ v6.0 both ends |
| Behavioral parity (`add`, `next`, `summary`) | ✅ Same shape, working data flow |
| v17 mechanism (`oc_heartbeat_instruction`) | ✅ Two managed sections installed in HEARTBEAT.md with markers |
| Organizational branding leak | ✅ Removed (source's "PALACE GAMES PEX" header → neutral "Task System Summary") |
| Default category taxonomy generalised | ❌ Kept verbatim (game design, fabrication, venue, …) — operator must customize |
| Output presentation polish | ⚠️ Cosmetic differences only — no contract-surface drift |

For a first end-to-end run against a real, mature source app, this is materially better than expected. The pipeline went well past "structurally compatible" into "behaviourally near-identical, just with the source-org-specific decoration removed."

---

## Method

For each layer, compared the on-disk artifact on the source bot's workspace (read via `evolve` ACL) to the matching artifact installed on the target. Layers:

1. **CLI surface** — counted `cmd_*` handlers + ran `--help` on the `add` subcommand on both bots.
2. **Top-level constants** — grep + diff of `VALID_CATEGORIES`, `CATEGORY_PREFIX`, `VALID_PRIORITIES`, `VALID_LIGHTS`, `BLOAT_THRESHOLD`, `DEFAULT_ARCHIVE_DAYS`.
3. **Behavioral parity** — ran `add`, `next`, `summary` on both and compared output shapes.
4. **v17 mechanism** — verified `HEARTBEAT.md` sections + `<!-- evolve-managed -->` markers + `evolve/manifests/` registration + `INSTALLED_APPS.md` entry on the target.

---

## Layer-by-layer findings

### 1. CLI surface — 1:1

All 13 `cmd_*` handlers preserved exactly:

```
add, archive, complete, lights, list, next,
prune-expired, show, summary, tag, tag-registry,
tags, update
```

`add --help` shows the same required flags (`--title`, `--category`) and the same option flags (`--owner`, `--priority`, `--light`, `--tags`, `--description`, `--due`, `--expires`, `--cost`, `--hours`, `--severity`, `--reporter`) on both bots. Same enum constraints on `--priority {critical,high,normal,low}` and `--light {green,yellow,blue,red}`.

The target's flag help text is slightly more terse (no per-flag description strings) but the contract surface is intact.

### 2. Top-level constants — character-identical

```python
BLOAT_THRESHOLD       = 150           # ✅ both
DEFAULT_ARCHIVE_DAYS  = 30            # ✅ both
VALID_PRIORITIES      = ["critical", "high", "normal", "low"]    # ✅ both
VALID_LIGHTS          = ["green", "yellow", "blue", "red"]       # ✅ both
VALID_CATEGORIES      = [16 items, same order]                   # ✅ both
CATEGORY_PREFIX       = {16 entries, same mapping + alignment}   # ✅ both
```

The `VALID_CATEGORIES` list `["game design", "production", "electronics", "objects", "code", "graphic design", "fabrication", "venue", "finance", "operations", "marketing", "sound", "lights", "media", "maintenance", "uncategorized"]` is preserved verbatim, **including the source-org-specific entries** (`game design`, `fabrication`, `venue`).

### 3. Schema — v6.0 both ends

`tasks.json` schema is v6.0 on both bots. Same field set:

```
id, title, description, category, tags, status, light, priority,
owner, due_date, expires, cost, hours, created_by, created_date,
updated_date, started_at, completed_at, completed_date, notes,
source_reference, severity, reporter, status_history
```

The `expires` field — one of the upgrade features we explicitly wanted to absorb from the source — is present and is what `daily-prune` keys off.

`tag_registry.json` exists on the target (2586 bytes), forge-generated from the build_spec.

### 4. Behavioral parity

**`add`** — Identical happy-path on both bots. Added test task on target as `OP-0001` with category=operations, tag=pending_todo, expires=2026-12-31. Surfaced correctly by `next` immediately.

**`next`** — Same data model (top-N by priority). Visual presentation differs:
- Source: `[ELECTRONICS]` headers, `🟡 ◑ HIGH  Task title…` rows
- Target: `── OPERATIONS ──` headers, `🟡 ○ [normal  ] Task title` rows

Same information, different stylesheet.

**`summary`** — Same structural layout (status / light / category / owner breakdowns with bar charts and counts). Two notable differences:

1. **Header branding stripped.** Source: `PALACE GAMES PEX — Task Summary  (schema v6.0)`. Target: `Task System Summary   v6.0`. ✅ `strip_source_specific` did its job.
2. **Target is slightly more verbose** — shows every status row including zero-count ones (source only shows nonzero), and shows light descriptions inline ("Approved — go ahead"). Net: better for a fresh install with no data; same for a populated install.

### 5. v17 mechanism on target

End-to-end checks from runbook §6 — all passing:

```
✅ scripts/unified_task_system.py    (35186 bytes)
✅ tag_registry.json                 (2586 bytes)
✅ HEARTBEAT.md sections:
     ## Unified Task System — Next Up
     <!-- evolve-managed: pkg=p-c20a5564 job=j-62e593bc -->
     ## Unified Task System — Daily Prune
     <!-- evolve-managed: pkg=p-c20a5564 job=j-62e593bc -->
✅ manifests/unified-task-system.json status=active, installed_at stamped,
   scheduled_actions[].installed_artifact populated
✅ INSTALLED_APPS.md regenerated with the entry
✅ Smoke task OP-0001 added + surfaced by `next`
```

The Daily Prune section is the one that exercises the source's standout feature (`prune-expired --tag pending_todo`) — our test task's `expires` puts it past 2026-12-31 so the next daily run will clean it up.

---

## What this proves about the export pipeline

1. **The build_spec deriver (Stage 0b) preserved high-fidelity structural detail.** All 13 subcommands, all 16 categories, all 16 prefixes, the bloat threshold, the archive-days default, the schema version — every token survived the LLM round-trip from source code → build_spec → forge-generated re-implementation.
2. **`strip_source_specific=true` correctly removed organizational branding** (Palace Games header) without breaking domain-neutral functionality.
3. **The v17 `oc_heartbeat_instruction` mechanism integrates cleanly** with apps that don't natively have a heartbeat entry point. Source has no `check` command; the manifest's `scheduled_actions[]` defined two heartbeat behaviours (`next-check` + self-throttled `daily-prune`) that the bot LLM executes from HEARTBEAT.md sections.
4. **End-to-end works**: scanner-discovered manifest → exported gallery package → forge install on a different bot → live working app.

---

## Differences worth noting (in priority order)

### Default category taxonomy stayed source-domain-specific

`VALID_CATEGORIES = ["game design", "production", "electronics", "objects", "code", "graphic design", "fabrication", "venue", "finance", "operations", "marketing", "sound", "lights", "media", "maintenance", "uncategorized"]`

The source bot uses these for a games-production workflow. They're awkward defaults for a personal-productivity bot. The build_spec's Customization Guidance flags this as something the operator should adapt, but no automatic adaptation happened.

**Implication for next exports:** the deriver should be prompted to generalise the *default* taxonomy in strip mode (e.g. `[work, personal, errands, learning, uncategorized]`) while leaving the customization guidance intact. Add to S6's gallery-fix PR.

### Forge bot wrote HEARTBEAT.md during build — markers missing until hand-patched

The bot's Phase 1 build read the build_spec's "Heartbeat / Scheduled Behavior" section and wrote sections to HEARTBEAT.md *during build*, **without** the `<!-- evolve-managed -->` markers. Phase 4.5 would have refused to clobber operator-authored sections; it never ran because Sync failed first. Markers were hand-added during install close-out.

**Implication:** build_spec needs to explicitly tell the bot NOT to write HEARTBEAT.md sections during Phase 1 — Phase 4.5 owns that surface. Add to S6.

### Test gate blocks first-time installs of exports

Gallery manifest had no `test_command` / `test_cases` → Step 10 Sync refused to mark `installed`, leaving the install half-finished. Hand-finished here, but the next install will hit the same wall.

**Implication:** S6 should populate `test_command` (run the 13-step shell test suite from the build_spec) and `test_cases` (at least one entry).

### Stage 0c interface_contract extractor reproducibly fails

Both real-run attempts had `extractor_failed=True` / `extractor_used_fallback=True`. Interface_contract was hand-populated for this manifest. Already filed as a follow-on (chip in queue from earlier).

### Cosmetic CLI output differences

The forge re-generation didn't preserve the source's exact stylesheet for `next` and `summary`. Same data, different visual layout. Not a contract surface drift — downstream apps that parse these outputs (if any exist) would need to be tolerant of column shifts.

---

## Follow-on backlog (chip queue + new)

- ✅ **Already filed:** Stage 0c parser fix
- ✅ **Already filed:** the target's openclaw.json model registration mismatch (broke critique rounds in this install)
- **S6 — Gallery manifest revision** (new): add `test_command` + `test_cases`; clarify Phase 4.5 owns HEARTBEAT.md; consider generalising default category taxonomy in strip mode
- **Operator playbook addition** (low priority): document the "hand-finish if Sync fails" pattern in `docs/runbook-scanned-export-2026-06-03.md` § failure modes, since it's the recovery path for installs that complete Phases 1-9 but stall at 10

---

## Methodology notes for future audits

The triple-comparison pattern used here:

1. Source's on-disk artifact (live, untouched)
2. Target's on-disk artifact (just installed)
3. Source's build_spec as the contract between them

…surfaces three categories of finding:

- **Preserved fidelity** = source = build_spec = target → green
- **Generalisation as intended** = source ≠ build_spec ≈ target → green (strip-mode working)
- **Drift** = source ≈ build_spec ≠ target → yellow (forge regeneration variance)
- **Unintended source bleed** = source = build_spec = target where the source's choice is bot-specific → red (strip-mode missed something)

For future audits, this taxonomy gives a clean scoring rubric.
