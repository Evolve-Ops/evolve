# Atlas lineage re-point — OBE: #3330 already resolved the 6 "unresolvable_spec" rows

**Date:** 2026-06-28 · **Aspect:** META:apps · **Mode:** read-only live verification (mini)
**Branch:** `claude/apps-lineage-repoint-atlas`
**Bottom line:** the *original* motivation — "6 `unresolvable_spec` rows on atlas
are at risk of a wrongful strip" — is **overtaken by events**. `#3330` (the
recon-ledger CWD-key join fix) alone moved all of atlas's former retired-marker
orphans into `owned_ok`. There are **zero `unresolvable_spec` rows** on atlas, so
the wrongful-strip risk does not exist.

**Update (follow-up PR):** a follow-up PR nonetheless ships the lineage re-point
CLI — not to fix the (now-empty) scrub bucket, but as the **"lineage hardening"
follow-up this very memo recommends below** (Recommended disposition → Optional
follow-up #1). The
CLI's selection criterion is exactly the one the memo specifies: *"`owned_ok` whose
marker spec_ids resolve to nothing"* — NOT the empty `unresolvable_spec` bucket. It
records the missing `prior_spec_ids[]` lineage and re-points the on-disk markers so
ownership is robust to `realized_files` delisting. It is **dry-run by default**; the
atlas plan it computes is in the PR body for an operator to review before any
`--apply`. The class-iv digest-output de-list (follow-up #2) remains a separate
writer-side bite.

---

## What the chip assumed

The chip (dispatched off the `apps-atlas-sync-investigation` disposition doc) was
scoped to 6 `scrub_candidate / unresolvable_spec` rows on atlas — live data/scripts
(`atlas/sources.json`, `atlas/optout.json`, `atlas/research-{budget,config}.json`,
`scripts/atlas_capture.py`, `digest/*`) carrying markers for a **retired monolithic
Atlas digest app** (`p-c07ba3b2` / `p-6e6c474a`) that was split into three live
successors (Capture Pipeline `p-555f7671`, Daily Digest `p-7b26ba5e`, Article
Capture `p-df9d99a3`). With no `prior_spec_ids[]` recording the split, the retired
markers were expected to resolve to nothing and the files to look orphaned (and be
at risk of a wrongful strip).

The investigation doc §2e explicitly predicted these rows would remain *"genuine
scrub/re-point candidates **even after the join is fixed**."*

**That prediction is empirically false.** It overlooked the recon-ledger OWNED
branch firing on `claimed is not None` *independent of marker resolution*.

## What the live post-#3330 ledger actually shows

`#3330` is merged to `origin/main`, deployed to the mini (deploy-checkout HEAD =
`7108fa58`), and **picked up by the running admin-ui daemon** (pid 92157 started
`Sun Jun 28 21:38`, after the `21:28` pull — so the Sync modal serves the fixed
view, not a stale-module-cache one).

Fresh in-memory rebuild on the live atlas workspace:

```
cd /tmp && sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \
  -m evolve_admin.applications.recon_ledger --bot-id atlas --no-persist --json

counts: owned_ok=19  attach_candidate=3  scrub_candidate=19  missing_marker=17  missing_file=3
unresolvable_spec rows: 0          # ← every scrub row is now ineligible_path
```

All 10 former-orphan files are now **`owned_ok`** with reason `claimed_and_marked`,
resolved to their live successor spec:

| File | on-disk marker (retired) | now bucketed | resolved spec |
|---|---|---|---|
| `scripts/atlas_capture.py` | `pkg=p-6e6c474a` | owned_ok | p-df9d99a3 |
| `atlas/sources.json` | `pkg=p-c07ba3b2` | owned_ok | p-7b26ba5e |
| `atlas/optout.json` | `pkg=p-c07ba3b2,p-6e6c474a` | owned_ok | p-df9d99a3 |
| `atlas/research-budget.json` | (retired) | owned_ok | p-df9d99a3 |
| `atlas/research-config.json` | (retired) | owned_ok | p-df9d99a3 |
| `digest/2026-06-05.md` | `pkg=p-c07ba3b2` | owned_ok | p-df9d99a3 |
| `digest/2026-06-06.md` | (retired) | owned_ok | p-df9d99a3 |
| `digest/source_health-2026-06-0{5,6,7}.json` | (retired) | owned_ok | p-df9d99a3 |

### Why the join fix alone was sufficient

`recon_ledger._classify_bot` checks `claimed = expected.get(<ws-rel key>)` **before**
it considers marker resolution:

```python
if claimed is not None:           # successor's realized_files[] lists this path
    _add(... bucket=OWNED_OK ...)  # OWNED regardless of the marker's (dead) spec_id
    continue
...
else:                              # only reached when NOT claimed
    _add(... SCRUB_CANDIDATE, reason=unresolvable_spec ...)
```

The three successor manifests **already list these paths** in their
`realized_files[]`. Pre-#3330 the join key was CWD-mangled (`/scripts/...`) so
`claimed` was always `None` and every marked file fell through to the
`unresolvable_spec` arm. #3330 made the key canonical workspace-relative, so
`claimed` now hits → the files are OWNED and the `unresolvable_spec` arm is never
reached. The retired marker never gets a vote.

## Recommended disposition

**Close this chip as OBE (resolved by #3330).** The "would be wrongly stripped"
risk it targeted does not exist on atlas: the files are not in any scrub bucket,
the bulk Strip action cannot reach them, and the Sync modal shows them owned.

### Optional, lower-priority follow-up (coordinator's call — a *different* bite)

There is a real but **latent, non-urgent** residual, and it would need a *different*
selection criterion than this chip's (which is now empty):

1. **Lineage hardening (defense-in-depth).** These 10 files are owned **solely** by
   `realized_files[]` membership; their on-disk markers still carry the dead specs
   and no `prior_spec_ids[]` lineage is recorded. If any path is ever delisted from
   `realized_files` (scanner `_merge` dedup, an app re-extract, a manifest rewrite),
   it **instantly reverts** to `unresolvable_spec`. Recording the supersession
   (`record_spec_supersession(successor, p-c07ba3b2 / p-6e6c474a)` on
   `p-df9d99a3` + `p-7b26ba5e`) and/or re-pointing the markers would make ownership
   robust to delisting (reason would harden to `claimed_marker_resolved_via_lineage`).
   The #3299 mechanism is built and unused. **Selection criterion = "owned_ok whose
   marker spec_ids resolve to nothing" — NOT the empty `unresolvable_spec` bucket.**

2. **Class-iv writer hygiene (independent).** The dated digest **outputs**
   (`digest/2026-06-0*.md`, `digest/source_health-*.json`) are generated telemetry
   yet appear in `p-df9d99a3.realized_files[]` as if they were owned *source*. They
   should be **de-listed** from `realized_files` at the writer (forge/scanner), not
   re-pointed. This is the §4f item from the investigation doc.

Both are forward-fixes, not orphan-cleanup, and neither carries the wrongful-strip
urgency that motivated this chip. If the coordinator wants the hardening, it is a
fresh, re-scoped bite — not the deliverable described here.

## Evidence trail (all read-only, mini)

- deploy-checkout HEAD `7108fa58` (#3330) — fix deployed
- admin-ui pid 92157 started `21:38` > #3330 pull `21:28` — daemon runs fixed code
- `recon_ledger --bot-id atlas --no-persist --json` → `unresolvable_spec = 0`
- on-disk markers confirmed still carrying retired specs (table above) yet bucketed
  `owned_ok` — proving the OWNED-via-`claimed` path, not lineage
