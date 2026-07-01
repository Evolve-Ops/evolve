# Shared-contract audit — spec-resolver / ownership-policy / path-key consumers

**Date:** 2026-06-29 · **Aspect:** META:apps · **Branch:** `claude/apps-shared-contract-audit`

## Why this audit exists

A GATE-2 review of bot `atlas`'s "Sync Applications" reconciler exposed four bugs
in one bout, all the **same root pattern**: a contract with two or more consumers
where only one side was wired correctly, so the two sides silently disagreed.

| # | PR | Divergence | Symptom |
|---|----|-----------|---------|
| 1 | [#3330](https://github.com/evolve-ops/evolve/pull/3330) | recon-ledger JOIN keyed via CWD-sensitive `Path.resolve()` on one side, real absolutes on the other | keys never matched → 0 owned, 40 false-missing |
| 2 | [#3341](https://github.com/evolve-ops/evolve/pull/3341) | `can_app_own` applied to the markers/scrub side but not the claims (`realized_files[]`) side | apps "claimed" secrets/store files → corrupting Fix=stamp button |
| 3 | (queued) | `can_app_own` not applied at the `realized_files[]` WRITE-population site | invalid claims regenerate each scan |
| 4 | [#3342](https://github.com/evolve-ops/evolve/pull/3342) | Attach action resolved spec_ids via a divergent `_index_instances_by_spec_id`, classifier used the lineage-aware index | "no Instance for p-…" on a path the classifier had resolved |

The fix each time was "make the consumers share ONE path." This audit hunts the
**remaining latent siblings** across three contract families. Shared homes:

- **A — Spec resolution:** `applications/spec_lineage.py` (`resolve_spec`, `build_spec_index`, `prior_spec_ids`, `current_spec_id`) + the `recon_ledger` wrappers (`build_reverse_spec_index`, `lookup_spec`, `_resolve_marker_spec`).
- **B — Ownership policy:** `applications/app_ownership_policy.py::can_app_own`.
- **C — Path canonicalization:** `applications/recon_ledger.py::_ws_rel_key` (the #3330 reference).

## Headline result

The four GATE-2 fixes were **nearly exhaustive** for the families they touched.
The audit found:

- **Family A (spec resolution): ZERO `#3342`-class divergences.** Every on-disk
  spec_id → live-Instance resolution routes through the lineage-aware shared
  resolver. One adjacent **weak spot** (forge dependency check, family-A-adjacent)
  is enumerated as a follow-on — it is not the audited symptom.
- **Family B (ownership policy): one NEW true divergence fixed inline** (the
  apply-fix stamp endpoint — a missed sibling write site of #3301/#3341), plus
  **confirmation + exact file:line of the known queued gap #3** (the
  `realized_files[]` writer), enumerated as a follow-on with its data-cleanup tail.
- **Family C (path canonicalization): the #3330 fix is solid where it lives**;
  the live remaining exposure is three un-migrated joins in `scanner.py` (one
  firing), enumerated as a follow-on.

Inline fixes in this PR are limited to the **clear-cut, low-risk** consistency
unification (the stamp-endpoint guard). Everything substantive — a writer-hygiene
fix needing live-manifest data cleanup, a 3-site hot-file path-key migration, a
forge dependency-resolution change — is enumerated below for per-finding dispatch,
**not** fixed blind here (per the bite's scope guard).

---

## Contract Family A — Spec resolution (spec_id → app/Instance)

**Invariant:** every place that maps an on-disk/marker spec_id to a live Instance
is lineage-aware and uses the one shared resolver (so a retired `prior_spec_ids[]`
entry still resolves to the live app).

**Key discrimination:** most `provenance.get("spec_id")` reads are **spec_id →
Spec-definition-in-gallery** lookups (an in-hand Instance reading its *own* current
spec_id to find its Spec JSON on disk) — NOT **spec_id → Instance** fleet
resolutions. The invariant governs only the latter. Flagging the former would be a
false positive.

| consumer (file:line) | what it does | shared resolver? | verdict |
|---|---|---|---|
| `recon_ledger.py:402,409` | per-bot lineage index for reconciliation | **Y** (`build_spec_index` + `build_reverse_spec_index`) | SAFE — the #3342 fix site |
| `recon_ledger.py:379-396` (`_resolve_marker_spec`) | disk-marker spec_ids → live Instance current id | **Y** | SAFE — sets `via_lineage` on a retired hit |
| `recon_ledger.py:188-199` (`lookup_spec`) | spec_id → entry with explicit `prior_spec_ids` fallback | **Y** | SAFE |
| `reflect.py:146-150,384-399` (`_instance_for_spec`) | row spec_id → owning Instance | **Y** (delegates to `recon.lookup_spec`) | SAFE |
| `manifest_hygiene.py:254-321` | builds spec_index, resolves primary/secondary → owner | **Y** (`build_spec_index` + `current_spec_id`) | SAFE — comment cites retired-chain coverage |
| `lineage_repoint.py:249-298` | finds markers resolving to no live Instance | **Y** | SAFE — repair tool, intentionally lineage-aware |
| `web/routes_applications_sync.py:55-57,248,299` | sync route spec_index/recon for the UI | **Y** (`build_recon_ledger`) | SAFE |
| `scanner.py:3404` | reuse path reads a matched candidate's own current spec_id | N | SAFE — single-instance self-read; candidate found by evidence-overlap |
| `scanner.py:4549` | re-discovery filters candidates with a live current spec_id | N | SAFE — per-candidate; wants the live binding |
| `scanner.py:4931-4933` | dedup: two in-hand instances "same app" if same current spec_id | N | SAFE — compares two in-hand instances, not a fleet lookup |
| `manifest.py:1688`, `spec_drift.py:172,271`, `adopt.py:228,239,424`, `extend_application.py:144`, `lessons_compress.py:306`, `web/server.py`, `web/share_routes.py:86,99`, `scanner.py:743` | in-hand Instance's own spec_id → gallery Spec definition | N | SAFE — spec_id→Spec-on-disk, **not** spec_id→Instance |
| `native_write.py:260-293` (`_bound_spec_id_on_disk`) | reads on-disk spec_id + prior chain so a re-spec extends lineage | N | SAFE — write path; preserves `prior_spec_ids` |
| `migrate_v7.py:120`, `web/share_routes.py:80` (`_resolve_spec_id`) | mint-or-reuse a spec_id for a NEW instance | N | SAFE — mint helper, not a fleet resolution |
| `forge_engine.py:1397-1415` (`_check_app_dependencies`) | declared dep `spec_id` → `apps_dir/{spec_id}.json` existence | **N** | **WEAK** — see follow-on F-A1 |

`_index_instances_by_spec_id` (the #3342 divergent resolver) has **zero remaining
references** anywhere in the tree (grep exit 1) — its removal was complete.

**True `#3342`-class divergences: none.**

### Follow-on F-A1 — `forge_engine._check_app_dependencies` is lineage-unaware (LOW)

- **Site:** `applications/forge_engine.py:1409` — `manifest_path = apps_dir / f"{dep_id}.json"; if not manifest_path.exists(): issue`.
- **Divergence:** maps a declared `app_dependency.spec_id` to a manifest by **filename**, with a bare `.exists()` and no lineage. Assumes `filename == spec_id` (true for legacy conformant apps; v7-arc files are named by `instance_id`).
- **Why NOT the audited symptom:** this is forge-time validation of an operator's *declared* dependency list, not reconciliation resolving an on-disk marker. It does not consult `prior_spec_ids` and does not reproduce "classifier resolved it but resolver couldn't."
- **Latent symptom:** a forge build declaring a dependency on app Y reports a spurious "manifest not found" if Y was re-spec'd to a new id, or if Y is a v7-arc instance whose filename differs from its spec_id.
- **Unify approach:** load the bot's instances and resolve `dep_id` via `spec_lineage.build_spec_index` (lineage-aware) rather than a filename `.exists()`. **Risk: low** (forge-time, advisory issue text), but needs the instance-load wiring + a filename-convention decision, so not a blind swap.

---

## Contract Family B — Ownership policy (`can_app_own`)

**Invariant:** every "may an app own / claim / mark / stamp this path" decision
consumes the single shared `can_app_own` predicate — no divergent copy, no
hand-rolled exclusion list, no un-wired write site.

| decision point (file:line) | decision | consumes `can_app_own`? | verdict |
|---|---|---|---|
| `app_ownership_policy.py:113` | **defines** `can_app_own` | — (source) | built from scanner's canonical artifacts; no divergent copy |
| `recon_ledger.py:451` | marker on never-ownable path → SCRUB_CANDIDATE | **Y** | KEYSTONE |
| `recon_ledger.py:516` | claim on never-ownable path → INVALID_CLAIM (kept out of stampable `missing_marker`) | **Y** | CLAIMS-SIDE KEYSTONE (#3341) |
| `scanner.py:6356` | v5 stamp pass: evidence-string gate | **Y** | runs first |
| `scanner.py:6411` | v5 stamp pass: dir-expansion child filter | **Y** | correct |
| `scanner.py:6466` | v5 stamp pass: belt-and-suspenders final candidate sweep (covers cron scripts) | **Y** | single guarantee point: never registered nor stamped |
| `reflect.py:175,213,248` | maps ledger buckets → findings | **Y** (transitive) | thin reader over a gated ledger |
| `strip_stale_markers.py:133` | scrub reason classification | **Y** (reuses `_is_oc_identity_system_file`, `_OC_TASK_SUBSTRATE_FILENAMES`) | reuses canonical predicates |
| `lineage_repoint.py:295` | skips re-pointing never-ownable markers | **Y** | keystone shared so the two tools never contend |
| `manifest_hygiene.py:384` | **writes** `realized_files[]` (orphan attach) | **Y** (transitive) | orphans come from `reflect`→ledger `attach_candidate` (gated); `paths[]` only narrows |
| `web/server.py:4489` (`/reflect/reconcile`) | attaches file → `realized_files[]` | **Y** (transitive) | delegates to `reconcile_orphan_markers`; non-candidates → `unmatched` |
| `web/routes_applications_sync.py` (scrub) | strips marker only on ledger `scrub_candidate` | **Y** (transitive) | re-builds the ledger; refuses owned/attach/missing |
| `sync.py:61,103` (`compute_uncovered`) | "is this dir an undiscovered app?" | **N** (uses `EVOLVE_PLATFORM_TREES`+`OC_INFRA_DIRS`+`manifests`) | INTENDED — coverage heuristic, a *different* question; documented at L57-61 |
| `scanner.py:1644` (discovery walk) | excludes `EVOLVE_PLATFORM_TREES` from discovery | **N** (raw set) | INTENDED — discovery-phase exclusion; it is the *source artifact* `can_app_own` is built from |
| `provenance.py` (`embed_marker`) | low-level marker writer | **N** | INTENDED — primitive; callers gate (becomes a bug only at an ungated caller → see B-1) |
| `manifest_recovery.py:46,233` | claim-set diff for restore | **N** | INTENDED — set arithmetic over already-claimed paths |
| **`web/server.py:4446` (`/reflect/apply-fix`)** | **stamps/rewrites a marker on a caller-supplied path** | **N → now Y (FIXED here)** | **TRUE DIVERGENCE — see B-1** |
| `migrate_v7.py:1072-1086` (`_build_realized_files`) | **writes** `realized_files[]` from legacy `files[]` | **N** | **TRUE GAP — see follow-on F-B1 (the queued #3)** |
| `extend_application.py:214` | hand-rolled `realized_files[]` append | **N** | **TRUE GAP — see follow-on F-B1** |

### B-1 (FIXED INLINE) — apply-fix stamp endpoint missed the ownership gate

- **Site:** `web/server.py` `POST /api/bots/<bot_id>/reflect/apply-fix` (the `embed_marker` call, formerly unguarded at ~L4446).
- **Divergence:** the endpoint validated path-traversal, existence, and `spec_id`/`file_id` *format*, then called `embed_marker` on the **caller-supplied** `file_path` with no ownability check. The scrub action re-derives from the ledger and the reconcile/attach action narrows the ledger's gated set — only this stamp/rewrite write side trusted the client. A direct sibling-call-site miss of #3301 (Phase-5 stamp writer gated) and #3341 (claims side gated): the same pattern as the #3198 clamp that missed its self-heal call site.
- **Symptom:** `POST {kind:"stamp_marker", file_path:<secret/telemetry path>}` would embed a corrupting `_evolve` text marker into a never-ownable file (`atlas/member-hash-salt.bin`, `evolve/audit_outbox/.../rec-*.json`, `AGENTS.md`) — the exact #3341-class corruption, reachable at the write side.
- **Fix:** consume the shared `can_app_own` predicate before `embed_marker`; refuse a never-ownable `rel` with `400 {denied_by: "ownership_policy"}`. Covers both `stamp_marker` and `rewrite_marker_to_spec` (a never-ownable path carrying a stale marker must be scrubbed, never rewritten).
- **Why low-risk:** by recon-ledger construction the UI only ever offers stamp on `missing_marker` rows and rewrite on `stale_pkg_marker` rows — both are `can_app_own == True` by construction (never-ownable marked paths route to `scrub_candidate` / `invalid_claim`). So the guard **never fires for a legitimate UI flow** — it is pure defense-in-depth against a hand-crafted or stale request. Positive-control test confirms an ordinary script still stamps `200`.
- **Test:** `tests/test_reflect_apply_fix.py::TestOwnershipPolicyGuard` — refuses a secret (`.bin` salt, asserts bytes untouched), an `evolve/` telemetry path, and a rewrite on `AGENTS.md`; plus the positive control.

### Follow-on F-B1 — the `realized_files[]` writer hygiene gap (the queued #3) (SUBSTANTIVE)

**Gap confirmed. Exact write-population site pinned.**

- **Primary site:** `applications/migrate_v7.py:1072-1086` inside `_build_realized_files` — the `out.append({...})` loop iterates `v13["files"]` and emits one `realized_files[]` entry per file with **zero `can_app_own` filtering**. Reached on every scanner mint via `native_write.mint_scanner_detection` → `mint_v7_arc_app` (`native_write.py:520`) → `_extract_instance` (`migrate_v7.py:520`) → `_build_realized_files`, and on every legacy migration.
- **Second site:** `applications/extend_application.py:214` — a capability-add records `file.path` directly into `realized_files[]` with no policy check.
- **Compounding root cause:** `scanner.py:6244-6246` — the *only* `can_app_own`-gated file-registration pass (Phase 5, `_stamp_discovered_files`) explicitly **returns early for v7-arc Instances** ("registration goes through the realized_files writer, not this legacy path"). **That deferred-to "realized_files writer" does not exist** — there is no per-scan pass that populates or re-validates a v7-arc Instance's `realized_files[]` through the policy.
- **Symptom (matches #3 exactly):** the recon ledger correctly *flags* a persisted invalid claim as `invalid_claim` every scan, but the only remediation (`remove_from_realized_files`) has **no auto-apply endpoint** (server.py supports only `attach` and `stamp`/`scrub`). So an invalid `realized_files[]` entry, once written, **persists across every scan** until an operator hand-edits the Instance JSON.
- **Mitigating scope note:** for the *common* fresh-discovery path, `_stub_manifest` (`scanner.py:6566`) emits no `files[]` key, so `_build_realized_files` usually receives an empty list at mint. The gap bites primarily on (a) `migrate_v7` of legacy on-disk manifests that already carry a polluted `files[]`, and (b) `extend_application`.
- **Unify approach (the follow-on bite):**
  1. Filter `_build_realized_files`'s output through `can_app_own` (drop never-ownable entries at mint); add the same guard at `extend_application.py:214`. **+ regression test** pinning "a never-ownable `files[]` entry is not minted into `realized_files[]`."
  2. **Data tail (out of scope for a code-only bite):** a one-time sweep to remove already-persisted invalid `realized_files[]` claims from live manifests (the entries written before the guard). Optionally add an auto-apply `remove_from_realized_files` endpoint so the ledger's `invalid_claim` diagnosis is self-healing instead of operator-manual.
- **Risk:** the code filter is low-risk; the live-manifest data cleanup needs care (don't drop a legitimately-ownable claim) and is the reason this is enumerated, not fixed blind here.

---

## Contract Family C — Path canonicalization (workspace-relative key)

**Invariant:** every join/index/comparison key derived from a path uses the
canonical workspace-relative form on all sides — no `Path.resolve()`/`getcwd()`/
bare-relative `open()` on a *workspace-relative* path in daemon code (CWD=`/`).

**The #3330 fix is solid where it lives.** The recon-ledger join
(`recon_ledger._ws_rel_key`) and its two siblings are all CWD-free:

| consumer (file:line) | canonicalizer | verdict |
|---|---|---|
| `recon_ledger.py:263` (`_ws_rel_key`) | lexical normalize for relative; reduce-from-absolute for absolute; never `resolve()` a relative | SAFE — the reference |
| `sync.py:86` (`_abs`) | anchors relative→`workspace` (absolute) **before** `.resolve()` | SAFE — `.resolve()` only on an absolute (CWD-independent); docstring cites the mirror |
| `manifest_hygiene.py:150` (`_normalize_path`) | same anchor-before-resolve | SAFE — same reasoning; idempotency-set key |
| `scanner.py` / routes `relative_to(workspace)`, `(workspace/rel).resolve()` | operate on genuine absolutes (disk-walk, traversal guards) | SAFE — correct canonicalization *direction* |

Note (maintainability, not a bug): `_ws_rel_key` (→ ws-relative) and
`_abs`/`_normalize_path` (→ absolute) are **three parallel implementations** of the
same anchor-before-canonicalize contract, producing different output forms. Each is
internally self-consistent (both sides of each join use the same one), so there is
no live divergence — but a future fourth join author could pick the wrong one. A
single shared module with both a `ws_rel_key` and an `abs_key` exported from one
place would remove the foot-gun. Tracked as F-C2 (cleanup, optional).

### Follow-on F-C1 — three `scanner.py` joins never adopted the canonicalizer (MEDIUM; one firing)

`scanner.py` compares a workspace-relative disk-walk path against **raw,
un-normalized** manifest `path` strings from `manifest.py:1398` (`file_paths()`).
That field is workspace-relative from migrate_v7/v13 but **absolute** from
`extend_application` — the same "two sides, different canonical form → join miss"
shape as #3330:

- **`scanner.py:7048-7049` (FIRING):** false `unregistered_script` compliance
  finding when a manifest stored an absolute path (the ws-rel disk path never
  matches the absolute claim).
- **`scanner.py:6499` (latent):** duplicate stamp entry with a fresh file_id on
  re-run (the existing claim isn't recognized).
- **`scanner.py:7155-7156` (weak):** dropped owner attribution on misplaced-secret
  findings.
- **Secondary, non-CWD:** `reconciliation.py:240-243` and `provenance.py:650` use
  `entry["path"].lstrip("/")` + `workspace / path`, robust only against the
  *relative* form — an absolute stored path is mangled into a non-existent path
  (potential false `missing_files`).

- **Unify approach:** route the three scanner joins (and the two secondary sites)
  through `_ws_rel_key` so both sides meet on the canonical ws-relative key, +
  regression test pinning "an absolute-stored `realized_files` path no longer
  mis-fires `unregistered_script`."
- **Why enumerated, not fixed inline:** `scanner.py` is a hot file; the change
  spans 3+ sites with **compliance-finding semantics** (getting canonicalization
  wrong could *suppress real* `unregistered_script` findings), and sharing
  `_ws_rel_key` into scanner needs a deferred import or relocation to avoid the
  `recon_ledger → app_ownership_policy → scanner` import cycle. Needs its own bite
  with a live-finding-impact check.

### Follow-on F-C2 — collapse the three parallel path canonicalizers (LOW, optional)

Export `ws_rel_key` and `abs_key` from one shared module; have `_ws_rel_key`,
`sync._abs`, `manifest_hygiene._normalize_path` delegate. Removes the
wrong-canonicalizer foot-gun. No live divergence today, so pure cleanup.

---

## Inline fixes in this PR

1. **B-1** — `web/server.py` apply-fix endpoint now consumes `can_app_own` before
   `embed_marker`; refuses never-ownable paths (`400 denied_by=ownership_policy`)
   for both `stamp_marker` and `rewrite_marker_to_spec`.
   Test: `tests/test_reflect_apply_fix.py::TestOwnershipPolicyGuard` (4 cases, incl.
   positive control).

## Recommended follow-on bites (for per-finding dispatch)

| id | finding | site(s) | risk | shape |
|----|---------|---------|------|-------|
| F-B1 | `realized_files[]` writer hygiene (the queued #3) | `migrate_v7.py:1072`, `extend_application.py:214`, root cause `scanner.py:6244` | code low / **data tail medium** | filter writer through `can_app_own` + one-time live-manifest cleanup + optional auto-apply remove endpoint |
| F-C1 | scanner path-key joins un-canonicalized | `scanner.py:7048,6499,7155` (+ `reconciliation.py:240`, `provenance.py:650`) | medium (hot file, finding semantics) | route through `_ws_rel_key` + regression test |
| F-A1 | forge dependency check lineage-unaware | `forge_engine.py:1409` | low | resolve via `build_spec_index` instead of filename `.exists()` |
| F-C2 | three parallel path canonicalizers | `recon_ledger._ws_rel_key`, `sync._abs`, `manifest_hygiene._normalize_path` | low (cleanup) | single shared module exporting `ws_rel_key`/`abs_key` |
