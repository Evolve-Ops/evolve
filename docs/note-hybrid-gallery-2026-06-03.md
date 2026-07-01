# Note: hybrid gallery model

**Date:** 2026-06-03
**Status:** Architectural note (supersedes Part A of [docs/plan-gallery-migration-and-export-evolution-2026-06-03.md](plan-gallery-migration-and-export-evolution-2026-06-03.md))
**Related:** [docs/spec-files-pack-hybrid-2026-06-03.md](spec-files-pack-hybrid-2026-06-03.md), [docs/note-gallery-shape-shift-2026-06-03.md](note-gallery-shape-shift-2026-06-03.md)

---

## What changed

The plan doc (PR #2044) sequenced a forced 13-app migration (F-P.5 → F-P.7) — every gallery package becomes a manifest+files folder, one PR per app, dependency-ordered.

The hybrid model says: **gallery packages don't all need to become files-packs.** Two classes of app coexist permanently in the gallery, and apps move between them when there's a real reason — not on a schedule.

## The two classes

| Class | Shape | Install cost | When it makes sense |
|---|---|---|---|
| **Manifest-only** | `gallery/<slug>/p-<id>.json` only | Full forge cost (~$30/install) | App is rarely installed, or its contract changes faster than its files, or no canonical "good" instance exists yet to snapshot from |
| **Manifest+files** | `gallery/<slug>/p-<id>.json` + `gallery/<slug>/files/` | ~$0/install (copy + substitute) | App is foundational or frequently installed, has a stable file shape, and we have a clean instance to snapshot |

Both are first-class. An app being manifest-only isn't a failure to migrate — it's a valid steady state.

## Promotion mechanism

The natural moment to promote an app from manifest-only to manifest+files is **the first time it gets forged on a real bot**. That forge run produces exactly what the gallery needs: a clean, contract-conformant install. Two viable paths:

1. **Operator-pulled.** Forge completes → admin UI surfaces a "promote this install to gallery files-pack?" affordance → operator reviews + commits. Snapshot CLI (F-P.3) is reused under the hood.

2. **Auto-snapshot on first forge.** Forge engine writes the files-pack candidate to a staging location (e.g. `gallery/_pending/<slug>/`) → operator-review queue picks it up → human approves before it lands in the gallery proper. Higher automation; same gates.

Pick the one that fits the operator workflow. Both rely on the F-P.3 snapshot CLI as the mechanism; only the trigger differs.

## Demotion (less common but possible)

An app can also move backwards — files-pack → manifest-only — when:
- The files drift faster than we can re-snapshot (frequent forge improvements regenerate them)
- The placeholder set becomes insufficient (e.g. the app gains operator-specific config that doesn't reduce cleanly to `{user_*}` tokens)
- The maintenance burden of keeping the files-pack current outweighs the install-cost savings

Demotion is just `rm -rf gallery/<slug>/files/` + remove the `files_pack` block from the package manifest. The dispatcher's best-effort fall-through (F-P.2) means the next install transparently takes the LLM-forge path. No data migration required.

## What this changes about F-P.5 → F-P.10

Old sequence (per plan doc):
- F-P.5: pilot task-manager migration
- F-P.6: operator runbook
- F-P.7: 12 separate PRs migrating the other 12 apps
- F-P.8: personalization scrub
- F-P.9: scanned-export Stage 0f
- F-P.10: `export-app` CLI

New sequence under hybrid model:
- **F-P.5 (this PR):** pilot task-manager migration — demonstrates the workflow and lights up F-P.4 sweeps. Same as before. Worth doing because task-manager is the foundational, high-frequency app where files-pack savings matter most.
- **F-P.6:** operator runbook — same as before, but framed as "how to snapshot an installed app to gallery" rather than "the migration playbook"
- **F-P.7 (changed shape):** promotion mechanism — admin UI surface + auto-staging for "promote this install to a files-pack". Replaces the bulk-migration sprint.
- **F-P.7.x (opportunistic):** individual app promotions as operators choose. Could be 0 in the first month, or 5 over a quarter — driven by actual demand.
- **F-P.8:** personalization scrub — still needed for sharing-with-other-operators flow; arguably MORE useful now since it shields against drift when operators promote arbitrary apps over time.
- **F-P.9 / F-P.10:** scanned-export integration + unified `export-app` CLI. Largely unchanged.

The big swing: **no more 12-PR forced march.** The bulk-migration ROI was never compelling for rarely-installed apps; the hybrid model lets the gallery converge to "files-pack where it matters" through actual use.

## Why this is better

1. **Maintenance burden scales with actual value.** Files-packs need to stay in sync with build_spec when the app evolves. If an app is rarely installed, that maintenance is pure overhead. Hybrid lets unused apps stay cheap-to-maintain.

2. **Newer apps don't get pre-judged.** An app added to the gallery today doesn't need to immediately decide its class. It ships as manifest-only and gets promoted when there's evidence it's worth it (real installs, stable contract).

3. **Forge runs become useful artifacts.** Every "first install on a real bot" is potentially a gallery contribution. The hybrid model turns a one-time install cost into a permanent gallery asset, capturing the forge investment instead of burning it.

4. **The auto-detector gap is less urgent.** The pilot snapshot (this PR) exposed a gap in F-P.3's placeholder detection — bare-token `_BOT_ID = "atlas"` wasn't caught (only path-shaped patterns were). Under bulk-migration, this was an urgent fix before F-P.7. Under hybrid, individual promotions get human review, so detector improvements can land at their own pace.

5. **Less coupling to the gallery's current state.** Today's gallery has 13 manifest-only apps. The plan doc treated this as a debt to be paid down. Hybrid says: that's fine. Some of those 13 are already at steady state.

## What's lost (and why it's OK)

- **No uniform gallery shape.** Operators reading the gallery directory will see some apps with `files/` and some without. The package manifest's `files_pack` field tells you which is which.
- **No predictable cost story for "install all 13 apps".** Under bulk migration we could quote "$0 to install the gallery." Under hybrid, that depends on which apps are files-packs at the time. Marketing concern, not technical.
- **No forcing function for staleness.** Under bulk migration, the F-P.4 integrity sweep would have run against all 13 packs. Under hybrid, it runs only against the promoted ones — which means rarely-promoted apps don't even have a baseline to drift from. Acceptable: the manifest-only apps still get the forge engine's full critic/test pipeline at install time, which is the real correctness gate.

## Tracking implication

The migration tracking table in the plan doc (Part A.2) is retired. Replace it with a simpler ledger in `docs/gallery-promotion-status.md` (or a section here) that lists which apps are currently files-pack class, when they were promoted, and which were demoted. Reflects reality, not a schedule.

## Next concrete step

F-P.5 (this PR) is still the right pilot — task-manager is exactly the kind of app that benefits from files-pack class (foundational, high-frequency, stable contract). The work after it is:

1. **F-P.6:** operator runbook for "snapshot an install to gallery"
2. **F-P.7:** admin UI affordance for promotion (operator-pulled flow first, auto-staging deferred)
3. **F-P.3.x (opportunistic):** improve auto-detector to catch bare bot_id tokens — fixes the pilot's manual-fix gap so future snapshots need less hand-editing

After that, things are demand-driven, not schedule-driven.
