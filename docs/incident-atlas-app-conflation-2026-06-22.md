# Incident — Atlas "Daily Digest" vanished + apps conflated into one Frankenstein manifest

**Date:** 2026-06-22
**Aspect:** META:apps
**Severity:** Pod-wide structural bug (scanner), surfaced acutely on `atlas`
**Status:** Root-caused; code fix in this PR; live recovery operator-gated (see §6)

---

## 0. One-paragraph summary

Atlas's "search OpenClaw news → summarize → post to a Telegram group" app
("Atlas Daily Digest") disappeared from the apps grid and the surviving
`atlas-article-capture` manifest became a Frankenstein: its `name` says one app,
its `description`/`identity.purpose` says another, and its file set conflates
**four** genuinely-distinct apps. The cause is **not** a one-off — it is two
structural bugs in the app *scanner*'s dedup/merge:

1. **Over-merge of distinct apps on shared files** — four atlas apps that
   *deliberately share a library + data substrate* (`scripts/atlas_lib/`,
   `archive/index.json`, `atlas/.capture-salt`, `atlas/optout.json`) exceed the
   `_dedup_manifests` file-overlap threshold (`ev_overlap >= 0.50`) and get
   collapsed into one manifest, even though their names and objectives are
   unrelated.
2. **Identity churns on every scan** — when two manifests merge,
   `_merge_two_manifests` overwrites the survivor's `description`/`identity.purpose`
   with the loser's *whenever the loser's string is longer* ("longest wins").
   A stable app-id therefore re-states a different purpose every scan, depending
   on which co-merged sibling happened to have the wordiest text.

The `.bak` that hid Daily Digest from the grid was **not** written by Evolve code
(no `.bak-YYYY-MM-DD` writer exists anywhere in the repo — see §4); it is external
(operator triage or the bot). But the loader only loads `*.json`
([`manifest.py:1470`](packages/admin/evolve_admin/applications/manifest.py)), so
any manifest set aside as `.bak` silently leaves the grid — and the Evolve-side
merge is what orphaned the app in the first place.

---

## 1. Evidence — the live atlas manifests (read-only, 2026-06-22)

`/Users/atlas/.openclaw/workspace/manifests/` (read via `sudo /bin/cat` as the
`pod-admin-user` ssh login):

| File | id | name | objective | description / identity.purpose |
|---|---|---|---|---|
| `atlas-article-capture.json` (active, schema 26) | `app_atlas_article_capture` | **Atlas Article Capture** | *"Make every URL a community member shares into a classified, summarized, opt-out-respecting archive entry"* (✓ capture) | *"Automatically generate and distribute a **daily curated news digest** … post … to a Telegram group every morning"* (✗ this is **Daily Digest**) |
| `atlas-daily-digest.json.bak-2026-06-16` (hidden, schema 25) | `atlas-daily-digest` | **Atlas Daily Digest** | *"Give a configured community a single concise morning digest …"* (✓ digest) | *"Atlas Guard is a real-time content moderation and access-control system …"* (✗ this is the **moderation guard**) |

Two independent fingerprints of the bug:

- **`merged_from` on the survivor** literally lists the four apps the scanner
  collapsed: `["atlas-capture", "atlas-content-guard", "atlas-knowledge", "atlas_research"]`.
  Article-capture absorbed the news-capture app, the moderation guard, a
  knowledge app, and a research app.
- **`realized_files`/`evidence_files`** on the survivor include not just the four
  apps' scripts (`atlas_capture.py`, `atlas_guard.py`, `atlas_knowledge.py`,
  `atlas_research.py`, `atlas_digest.py`) but `manifests/atlas-daily-digest.json`
  itself and the digest's launchd plist — the scanner swept the *digest's own
  manifest* into the article-capture cluster as a "file."

The split-brain is diagnostic: `_merge_two_manifests` never touches `objective`,
so each manifest kept its **real** objective while its `description`/`purpose`
flipped to a co-merged sibling's longer text. That is exactly the "longest wins"
overwrite (§3, root cause B) leaving a footprint.

### Design-intent ground truth

[`docs/atlas-app-manifests/README.md`](docs/atlas-app-manifests/README.md) (the
v6 spec pressure-test, 2026-05-20) documents these as **four distinct apps** that
**share substrate by design**:

> Shared substrate across the four: `scripts/atlas_lib/` … `archive/index.json` …
> `atlas/.capture-salt` … `atlas/optout.json` …

So the apps are *supposed* to be separate and *supposed* to share a library and a
data directory. The scanner's file-overlap merge is structurally wrong for any
app family that shares a library — it reads shared infrastructure as "same app."

---

## 2. Timeline reconciliation (promote/scan-race honored)

- `release.json` on the pod is all-`None` (mode not `canary`) → the deploy
  checkout follows origin tip; no release-pointer race to reconcile.
- `atlas-daily-digest.json.bak` content mtime: **Jun 16 22:34 PDT** — last write of
  the digest manifest while still `status:active`, `reconciliation.status:ok`
  (last reconciled `2026-06-16T06:06:22Z`).
- `atlas-article-capture.json` `provenance.created_by:"scanner"`,
  `created_at:"2026-06-16T07:57:41Z"` — the merged survivor was minted by a scan
  on **Jun 16**, *before* PR #2976 merged to main (#2976 commit date Jun 17
  04:56 -0700). **The operator's "the app vanished when dd-b1 landed" is a real
  time-correlation but #2976 is not the causal code** — #2976 only touches
  `install_helpers/manifest/placeholder_lint/delivery_monitor`
  (`git show --stat 42093fb8e`), none of which merge manifests or write `.bak`.
- The current `.scan-status.json` is an **error** state
  (`error_kind:"missing_api_key"`, `2026-06-22T18:34:28Z`): the most recent scan
  aborted in LLM-discovery with no Anthropic key reachable for `atlas`. A live
  re-scan therefore can't run until that is fixed — reinforcing that live
  recovery (§6) is operator-gated.

---

## 3. Root causes — file:line

### Root cause A — over-merge of distinct apps (`_dedup_manifests`)

[`scanner.py:4481`](packages/admin/evolve_admin/applications/scanner.py) runs on
**every scan** ([call site `scanner.py:3385`](packages/admin/evolve_admin/applications/scanner.py))
and globs **all** `*.json` manifests (line 4503), pairwise. The merge trigger
ladder (lines 4564–4581):

```
0. same provenance.spec_id            → merge   (authoritative: same Spec = same app)
1. ev_overlap >= 0.50                  → merge   ← FIRES on apps that share a library
2. name similarity >= 0.85             → merge
3. sim >= 0.55 AND ev_overlap >= 0.20  → merge
4. both-no-app-evidence + shared file  → merge
```

Condition **1** triggers on file overlap **with no name agreement at all**. Four
atlas apps sharing `scripts/atlas_lib/` + `archive/index.json` + `atlas/*` clear
0.50 trivially, so they collapse despite unrelated names/objectives. There is no
"are these actually the same app?" guard.

### Root cause B — identity churns on merge (`_merge_two_manifests`)

[`scanner.py:4404`](packages/admin/evolve_admin/applications/scanner.py):

```python
desc_w = winner.get("description", "") or ""
desc_l = loser.get("description", "") or ""
if desc_l and len(desc_l) > len(desc_w):      # ← "longest wins"
    merged["description"] = desc_l
purpose_w = (winner.get("identity") or {}).get("purpose", "")
purpose_l = (loser.get("identity") or {}).get("purpose", "")
if purpose_l and len(purpose_l) > len(purpose_w):
    merged["identity"]["purpose"] = purpose_l
```

The survivor keeps its own `id`/`name`/`objective` but **adopts the loser's
`description`/`identity.purpose` if longer**. Because the LLM re-clusters slightly
differently each scan (and so different siblings get merged in different orders),
the stable app-id re-states a different purpose every run. This is the "same id,
different description each scan" instability the operator saw in the 11:30 vs
11:34 tile flip.

### Root cause C — orphaned manifest is invisible (`manifest.py` loader)

[`manifest.py:1470`](packages/admin/evolve_admin/applications/manifest.py) loads
only files where `f.suffix == ".json"` and not `_`/`.`-prefixed. A file named
`atlas-daily-digest.json.bak-2026-06-16` has suffix `.bak-2026-06-16` → never
loaded → the app is gone from the grid with no error. The merge orphaned the app;
the loader silently hides whatever was set aside.

---

## 4. What the `.bak` is — and what it is **not**

Searched the whole repo for any writer of a `.json.bak-<date>` name:

- The **only** `.bak-<timestamp>` writer is
  [`mcp_admin/catalog.py:229`](packages/analyzer/mcp_admin/catalog.py), which uses
  `strftime("%Y%m%dT%H%M%SZ")` (e.g. `20260616T073441Z`) for `pod-catalog.json`,
  **not** manifests, and **not** the ISO `%Y-%m-%d` form.
- The scanner's own loser-disposal is either `path.unlink()` (delete,
  [`scanner.py:4598`](packages/admin/evolve_admin/applications/scanner.py)) or
  `_archive_to_history()` → `_history/<stem>_<reason>_<%Y%m%dT%H%M%SZ>.json`
  ([`scanner.py:4643`](packages/admin/evolve_admin/applications/scanner.py)).
- `grep -rn '\.bak' packages/admin/evolve_admin/applications/` → no manifest
  `.bak` writer.

**Conclusion:** `atlas-daily-digest.json.bak-2026-06-16` was created **outside
Evolve** — operator triage on 2026-06-16 (the date Daily Digest was noticed gone)
or the bot itself. The Evolve-side defect is that the merge orphaned the app and
the loader gives no signal when a manifest is set aside. The fix must (a) stop the
over-merge so the apps stay distinct, and (b) make recovery a documented,
operator-runnable step rather than a silent hand-edit (§6).

---

## 5. The fix (this PR)

A single shared predicate, `_are_distinct_apps()`, encodes "two established apps
with clearly-different names are NOT the same app, even if their files overlap,"
applied at every file/name-overlap decision point, plus removal of the
length-based identity overwrite:

1. **`_merge_two_manifests` — identity is stable, fill-only.** The survivor's
   `description`/`identity.purpose` is part of its identity; adopt the loser's
   **only when the survivor's is empty** (never "if longer"). Kills root cause B.
2. **`_dedup_manifests` conditions 1/2/3 — distinct-app veto.** A merge driven by
   file overlap *or* name similarity is vetoed when both sides are established,
   distinctly-named apps. This had to cover cond 2/3 (not just cond 1, the
   original target): the shared **`Atlas ` name prefix** inflates the raw
   SequenceMatcher score to ≥0.55, so two distinct apps (e.g. *Atlas Weekly
   Recap* vs *Atlas Article Capture*, raw `sim`=0.57) reached cond 3 on shared
   substrate. Cond 0 (same Spec) and cond 4 (no-app-evidence shells, no
   objective) are untouched. Kills root cause A.
3. **`_match_detected_to_existing` pass (d) — same veto.** The evidence-overlap
   *matcher* won't re-absorb a freshly-detected distinct app into a
   differently-named existing manifest, so a re-scan re-splits the family
   instead of feeding it back into the conflated survivor (makes the self-heal
   real — pinned by `test_matcher_does_not_absorb_distinct_app_on_evidence_overlap`).

The predicate is conservative (keep-bias toward dedup): it only vetoes when
**both** manifests have non-empty names **and** both carry a non-empty
objective/purpose/description. The name comparison uses
`_distinctive_name_similarity`, which **strips the shared leading tokens** (the
bot-name prefix) and is **symmetric** (`max` of both SequenceMatcher directions),
so the inflated-prefix and order-asymmetry traps cannot mis-read two distinct
apps as similar — nor a true re-mint as distinct (a prefix/superset name like
*"Atlas Daily Digest"* vs *"Atlas Daily Digest Job"* scores 1.0 → not distinct →
still merges). Thin/nameless re-mints and un-hydrated v7-arc instances are never
vetoed.

See [`test_scanner_distinct_app_dedup.py`](packages/admin/tests/test_scanner_distinct_app_dedup.py)
(12 tests: distinct split, identity stable across two consecutive scans, over-drop
guards for true duplicates / thin re-mints / v7-arc) and
[`test_manifest_recovery.py`](packages/admin/tests/test_manifest_recovery.py)
(recovery + de-conflation + the recovered pair staying split under the fixed dedup).

---

## 6. Live recovery on atlas — operator-gated

A re-scan with the fixed code stops *future* conflation but does not, by itself,
un-hide the `.bak` (the loader ignores it) or strip the already-absorbed files
from `atlas-article-capture.json`; and the live scan currently errors on a
missing API key (§2). This PR adds an operator-runnable recovery command —
`evolve_admin/applications/manifest_recovery.py`, wired as
`application restore-manifest` — that validates, refuses unsafe overwrites,
supports `--dry-run`, and conservatively de-conflates a sibling. Steps for the
operator to run on the pod (not executed here — privileged live-manifest writes):

1. **Dry-run first** to review exactly what will change:
   `sudo evolve-admin application restore-manifest atlas --from atlas-daily-digest.json.bak-2026-06-16 --unmerge-from atlas-article-capture --dry-run`
2. **Restore Daily Digest + de-conflate** (drop `--dry-run`). The command writes
   `atlas-daily-digest.json` (un-hiding it from the grid), archives the `.bak`
   into `_history/`, and strips from `atlas-article-capture.json` only the files
   that are **uniquely** Daily Digest's — i.e. referenced by no other surviving
   app. The shared substrate (`scripts/atlas_lib/*`, `archive/index.json`,
   `atlas/sources.json`) is **kept**, protected by the votes of the surviving
   Atlas apps (Weekly Recap / On-Demand Research) that legitimately use it. It
   refuses to overwrite a *different* active id without `--force`.
   - Caveat (review the dry-run): if a shared file is referenced by *no* other
     surviving manifest, the heuristic cannot tell it from an absorbed file and
     would strip it — the `--dry-run` output is the safety valve.
3. **Fix the missing Anthropic key for `atlas`** (`.scan-status` error), then
   **re-scan**. With the fixed dedup + matcher veto, the re-detected family stays
   split (the moderation guard / knowledge / research apps that are still folded
   into `merged_from` re-detect as their own manifests instead of re-absorbing).
4. Verify the grid shows Daily Digest, Article Capture, On-Demand Research, and
   Weekly Recap as four distinct apps, and that a second consecutive scan leaves
   each app's `name`/`description`/`identity.purpose` unchanged (identity stable).

---

## 7. Two-pass review

**Build-agent self-review** (silent-failure checklist): identity fill-only is
direction-correct; the dedup veto is computed once per pair and gates cond 1/2/3
only; the matcher veto is a no-op for v7-arc instances (raw instance carries no
name/objective); recovery uses the blessed `evolve_util.atomic_write_json`.

**Independent adversarial reviewer** — verdict **CONCERNS, no BLOCKERs**. The
core mechanism held up (distinctive-name veto correctly blocks the Atlas
over-merge that raw cond 3 allowed; identity fill-only correct; v7-arc no-op
real). Findings and disposition:

| # | Sev | Finding | Disposition |
|---|-----|---------|-------------|
| 1 | MAJOR | `_distinctive_name_similarity` hardcoded `1.0` for any prefix/superset name pair → "Atlas Research" vs "Atlas Research Budget Guard" could re-merge | **Fixed** — prefix case now scores `shared_prefix_tokens / max_tokens` (0.75 for a one-token qualifier = same-ish; 0.50 for two extra distinctive tokens = vetoable). Test `test_distinctive_name_similarity_handles_prefix_superset` + `test_prefix_superset_distinct_apps_are_not_merged`. |
| 2 | MAJOR | Same-app legacy re-mint under a drifted, dissimilar name now won't auto-merge (the veto fires) | **Accepted trade** — keep-bias: a missed merge is a transient duplicate, not data loss; cond 0 (same `spec_id`) is unaffected so all v7-arc re-mints still merge; the prior over-merge destroyed app identity, the failure we are eliminating. The operator can rename or run `application scan --dedup-existing`. |
| 3 | MINOR | `restore_orphaned_manifest` joined operator `--from`/`--unmerge-from` to `caps_dir` with no containment check (path traversal) | **Fixed** — `_require_within` rejects any path resolving outside the manifests dir (src, derived target, sibling). Test `test_rejects_path_traversal`. |
| 4 | MINOR | Partial-failure window: target restored, then sibling strip could raise, leaving conflation + restored copy | **Fixed** — sibling strip is isolated in try/except; a failed de-conflation reports success-with-warning (restore is the primary goal) and is idempotent on re-run. |
| 5 | MINOR | `plan_unmerge_files` strips a file co-owned by exactly {restored, sibling} (no third claimant) | **Documented** (§6 caveat) — intended keep-bias; `--dry-run` is the review/safety valve. |
| 6 | NIT | Comment implied the veto and cond 3 compute the same similarity metric | **Fixed** — comment now states the veto is strictly more conservative (prefix-stripped) than cond 3's raw floor. |

Post-fix: 1297 scanner/manifest/match tests + the two new suites green;
ruff-baseline / dup-primitive / except-pass / pyright clean.
