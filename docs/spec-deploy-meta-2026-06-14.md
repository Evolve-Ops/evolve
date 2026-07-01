# Spec: META:deploy — Deploy & update pipeline (coordinator charter)

**Date:** 2026-06-14 · **Status:** seed (scaffolded with the aspect)
**Aspect id:** `deploy` · **Session name:** `META deploy` · **Chip prefix:** `[META:deploy]`

This is the **coordinator charter** for the `deploy` META aspect — the durable
concern of *how Evolve ships code to the fleet and how OpenClaw is updated under it*.
It is not a from-scratch design; the pipeline already exists and is specified in the
inherited corpus below. This doc is the holistic view + ownership boundary + backlog
that a fresh `META:deploy` bootstraps from.

---

## Mission

Make deploying and updating Evolve (and the OpenClaw runtime beneath it) **safe,
legible, and recoverable** — every code change reaches the fleet through a gate, the
operator can always tell *what is running where* and *why*, and any bad move is one
command to undo. The pipeline's mechanics are mostly built (Phase 7); the standing
work is keeping them correct as the system evolves, and closing the **legibility gap**
between what the pipeline does and what the operator sees.

## Scope — what `deploy` owns

The deploy/update machinery as a *living subsystem*:

- **Repo puller** — `repo_puller.py` (the 15-min tick, post-pull hooks, per-tick
  healing, lagging-bot redeploy, untracked-conflict quarantine).
- **Release pipeline** — `release_manager.py` (candidate → Gate 1 → soak → promote /
  rollback / bootstrap), `canary_deploy.py`, `soak_probe.py`, `release_cli.py`, the
  release pointer (`release.json`) + `evolve-stable`/`evolve-previous` tags.
- **Web surfaces** — `web/release_routes.py` ("Complete soak now"), the Overview
  soak banner + "N of M behind stable" banner + per-bot version badges
  (`overview.js`, `home.js`, `index.html` version cells), `recovery.py` pod rollback.
- **Version identity** — `deploy.EVOLVE_VERSION` / `release_manager.version_for_sha`
  (`YYYY.MMDD.PR`), `install.json` (`bot_versions`), deploy-drift detection.
- **OpenClaw updates** — `safe_upgrade.py` + the OC Version banner preflight
  (read-only "is it safe to upgrade?" gate; never auto-runs `npm install -g`).

## Boundary — what `deploy` hands off

- **Cross-OS portability of the deploy path** → `platform` (macOS/Linux seams,
  launchctl/systemd, sudoers/plist goldens). `deploy` owns pipeline *mechanics*
  OS-agnostically; `platform` owns the per-OS substrate they run on.
- **In-flight Phase-7 remediation deltas** (soak risk-tier + active-canary
  validation, D5–D7) stay with `diligence` until they close, then standing
  ownership of that machinery returns here. Coordinate; do not yank mid-flight.
- **Store concurrency (7.1 Phase A–D, SQLite swap)** → `diligence` (it rides the
  canary pipeline but is a store concern, not a deploy-mechanics concern).
- **Per-bot config/app deploy correctness** (manifest materialize, forge installs)
  → `apps`; `deploy` owns the *fleet-wide code* deploy, not per-app payloads.
- **Signal-producer quality** on deploy monitors (`deploy_drift_monitor`,
  `cron_exit_monitor`, `repo_puller_stale`) → `reports`; `deploy` owns the
  mechanism correctness, `reports` owns the alert legibility.

## Inherited design corpus (the spec of record)

- [`spec-state-store-and-deploy-resilience-2026-06-10.md`](spec-state-store-and-deploy-resilience-2026-06-10.md)
  **§2 (7.2) is the release-pipeline spec of record** — pointer, candidate state
  machine, Gate 1/2, promote/rollback/bootstrap, kill-switches, §2.11 bootstrap
  deadlock.
- [`spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md`](spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md)
  + [`spec-delta-active-canary-validation-2026-06-13.md`](spec-delta-active-canary-validation-2026-06-13.md)
  — soak tiering + active probe (D1–D7; diligence-owned in-flight).
- [`spec-safe-upgrade-2026-05-02.md`](spec-safe-upgrade-2026-05-02.md) — OC upgrade preflight.
- [`spec-release-tiers-2026-05-16.md`](spec-release-tiers-2026-05-16.md) — release tiers.
- [`deployment-guide.md`](deployment-guide.md) + [`local-deployment-architecture.md`](local-deployment-architecture.md) — operator/runtime view.

## Invariants (non-negotiables)

- **Fleet follows the promoted pointer, never origin tip.** The deploy checkout
  (`/Users/Shared/evolve-repo`) only ever sits at `release.json::stable`; out-of-band
  `git reset` is repaired back by the next tick. Operator levers are
  `release pin`/`rollback`/`bootstrap`, never raw git.
- **Pointer-before-hooks ordering** — `release.json` is persisted *before* the hook
  suite runs (the hooks SIGTERM the admin server mid-job). State first, side effects
  second.
- **Canary-sweep exemption** — the lagging-bot sweep + `deploy_drift_monitor` exempt
  the canary bot during an active soak (else the soak passes on fabricated evidence).
- **You cannot soak-gate a fix to the soak gate** (§2.11) — a fix to the gate itself
  ships via `release bootstrap <ref>` (CLI-only, confirmation banner), not the web
  promote button.
- **OC upgrades are preflighted, never blind** — the banner offers "Check if upgrade
  is safe", not "run `npm install -g openclaw`" (the 2026-04-24 fleet-brick lesson).
- **Operator legibility is a first-class requirement** — every release surface must
  answer "what is running / what changed / is this forward or backward / what do I do"
  without the operator decoding internal identifiers.
- Web release actions run **as `evolve`, zero sudo** (the admin server is `evolve` and
  owns `release.json` + the fleet checkout); no `sudo <cli>` hints in the web UI.
- Privileged/irreversible deploy paths (rollback, bootstrap, sudoers/plist hooks) get
  auditor-grade two-pass review; ordinary revertible pipeline fixes auto-merge on
  green+PASS.

## Deploy mechanism (how `deploy`'s own changes ship)

Admin-only, **canary-gated** (pod runs `pod.release.mode=canary`): merge lands a
candidate → Gate 1 → soak → `release promote` → admin-ui kickstart. Changes to the
release pipeline *itself* may hit the §2.11 bootstrap deadlock — use
`sudo evolve-admin release bootstrap <ref>` (after `--dry-run`) to land a gate fix.
Verify on the live pod (Gate 2) after promote.

---

## D-8 — Consolidated "Updates" element: state → representation contract

**Status:** authoritative · paired with the `ui` chip that owns the render
(`packages/admin/evolve_admin/web/static/js/pages/overview.js` + `index.html`).
This section is the contract; the `ui` chip implements against it. **`deploy`
owns the truth (which state, what it means); `ui` owns the pixels.**

### Why — the consolidation

The Overview originally stacked **three** deploy-owned blocks above the bot grid:

1. the **"N of M behind release"** lag banner (element `#ov-sync-banner`),
2. the **"RELEASE PIPELINE"** control panel (D-5, #2949),
3. the **"OpenClaw update available / Safe to upgrade"** banner
   (`#ov-oc-banner`, reads `/api/oc/version`).

Three always-on rows cost vertical space the steady state does not earn — a
correctly-deployed fleet shows **nothing actionable** yet pays three rows. The
`ui` chip collapses all three into:

- **(a)** a 5th **"UPDATES"** cell in the summary band — always present, one line,
  severity-colored; this is where **routine** state lives, and
- **(b)** a **click-to-expand panel** (collapsed by default) holding the full D-5
  controls + OC safety detail + per-bot lag list.

The contract's job is to make this lossless: **steady state = zero rows above the
bots** (the cell carries it), **only genuinely actionable + urgent state surfaces a
slim one-line banner**, and **every control survives** in the expand region.

### Data sources (normalized inputs)

All already on the status payload / existing endpoints — no new fetch is required
for the cell; the two starred fields below are the only **server-side additions**
this contract asks for (§ data-contract deltas).

| Symbol | Source | Shape / meaning |
|---|---|---|
| `mode` | `_statusData.release.mode` | `"canary"` \| absent → `"direct"` |
| `stableV` | `release.stable_version` | promoted fleet pointer (date-led `YYYY.MMDD.PR`) |
| `prevV` | `release.previous_version` | what `stable` replaced (rollback target) |
| `promotedAt` ★ | `release.stable.promoted_at` | **ISO of the last pointer move** — needed to tell transient-redeploy lag from stuck lag (D-3). Stamp it where `_stamp_recency` already writes pointer fields. |
| `cand` | `release.candidate` | `{state: "checking"\|"soaking"\|"failed", sha, soak_started_at, commits_ahead, commit_date}` or `null` |
| `pin` ★ | `release.pin` | `{sha, reason}` or `null` — auto-promotion is **held** while set. **Surface it in `release_ui_view`** (today it's in `ReleaseState` but not the UI view). |
| `corrupt` ★ | `release.corrupt` | `true` when `release.json` is unparseable → promotion frozen, a Signal fires. Surface the boolean (already an error path in `release_status`). |
| `behind` | per-bot `b.evolve_synced === false` | members only — exclude `role==="primary"` and `canary_bot`; under canary this is recomputed against `stableV`, so `false` = genuinely lagging the pointer |
| `latest` | `_statusData.latest_release` | direct-mode only: `{tier: "security"\|"feature"\|"maintenance", version, headline, link}` |
| `oc` | `GET /api/oc/version` | `{update_available, installed, latest, safety_check: {running, ok, stale, summary, checked_at} \| null}` — the OpenClaw runtime axis, **independent of the Evolve pipeline** |

★ = server-side data-contract addition this contract depends on; small, deploy-owned,
listed again under § data-contract deltas. The cell degrades gracefully if absent
(pin → treated as not-pinned; `promotedAt` → lag treated as stuck after one poll,
i.e. fail-loud, never fail-silent).

### Derived flags

- `REDEPLOY_GRACE` — the self-heal window for post-promote lag. Set to **2 puller
  ticks (≈ 30 min)**: one tick to notice + redeploy lagging bots, one for slack. A
  single missed tick must not flip the state to loud.
- `lagTransient = behind.length > 0 && promotedAt && (now − promotedAt) ≤ REDEPLOY_GRACE`
  — the **expected** post-promote redeploy window. **This is the D-3 fold (below).**
- `lagStuck = behind.length > 0 && (!promotedAt || (now − promotedAt) > REDEPLOY_GRACE)`
  — a deploy genuinely failed to land; the pipeline's own lagging-bot sweep has had
  its window and the bots are still behind. (No `promotedAt` ⇒ treat as stuck — fail
  loud, not silent.)
- `pinned = pin != null && !corrupt`
- `ocReady = oc.update_available === true` (sub-label varies on `safety_check`)

### State enumeration + priority order

The cell shows the **single highest-priority** state that is true. Mutually
exclusive by construction except where noted; the rank breaks ties.

| # | State id | Condition (first match wins) | Cell label | Severity | Banner? |
|---|----------|------------------------------|------------|----------|---------|
| 1 | `pipeline-halted` | `corrupt` | **Release halted** | loud | **LOUD** |
| 2 | `lag-stuck` | `lagStuck` | **N of M behind release** | loud | **LOUD** |
| 3 | `update-security` | direct: `latest.tier==="security"` & `behind.length>0`; (canary: a security-flagged candidate, future hook) | **Security update** | loud | **LOUD** |
| 4 | `oc-update` | `ocReady` | **OpenClaw update** (sub: ✅ safe / ❌ unsafe / "check upgrade") | amber | quiet |
| 5 | `pin-held` | `pinned` | **Promotion frozen** (Pinned) | neutral | quiet |
| 6 | `candidate-soaking` | `cand.state==="soaking"` | **Update soaking** (· ~Nm left) | neutral | quiet |
| 7 | `candidate-checking` | `cand.state==="checking"` | **Update gating** | neutral | quiet |
| 8 | `candidate-blocked` | `cand.state==="failed"` (transient) → escalates to `shipping-stalled` (loud) if the failing tip persists past `REDEPLOY_GRACE` with no newer candidate | **Update blocked** | amber | quiet (→ LOUD on escalation) |
| 9 | `lag-redeploying` | `lagTransient` | **Redeploying…** | neutral | quiet |
| 10 | `update-available` | direct: `latest.tier∈{feature,maintenance}` & `behind.length>0` | **Update available** | amber | quiet |
| 11 | `up-to-date` | (else) | **Up to date** | success | quiet |

**Ratification vs the proposed order.** The chip's seed order was *lag/failed >
OpenClaw-ready > soaking/promotable > pinned > up-to-date*. Ratified, with three
deliberate adjustments:

- **+`pipeline-halted` at the top.** A corrupt `release.json` freezes *all*
  promotion and already fires a Signal — it outranks even lag. (Not in the seed
  list; it is a real state.)
- **+`update-security` above OpenClaw-ready.** A security release is urgent in a way
  a routine OpenClaw bump is not; it earns the loud lane.
- **`pin-held` moved *above* `candidate-soaking`** (the seed had soaking above
  pinned). Rationale: while a pin is active a soaking candidate **will not
  auto-promote**, so a cell reading "Update soaking — auto-promotes when the soak
  passes" would be a lie. The pin is *why nothing is shipping*; it must win the cell.
  Defense-in-depth: the soaking sub-label must never assert "auto-promotes" while
  `pinned`. ("failed promote" from the seed is not a separate cell state — a failed
  *promote action* renders its outcome in the expand region (existing
  `_renderPromoteOutcome`, loud line + step log); its durable consequence, a fleet
  left behind, surfaces as `lag-stuck` once `REDEPLOY_GRACE` elapses.)

### Loud vs quiet — the one rule

> **A state surfaces the slim one-line banner above the bots iff its severity is
> `loud` AND it is not currently snoozed/acked. The cell always renders,
> severity-colored, regardless.**

So `loud` ⇔ banner; `success`/`neutral`/`amber` ⇔ cell-only. Routine motion
(gating, soaking, the transient redeploy window, an available OpenClaw bump, a
pin the operator set themselves) is **quiet** — it lives in the cell and the
expand panel, never a row. Only a broken or security-urgent deploy gets a row.

**Snooze / ack downgrade the *banner*, never the *cell*.** `evolve.releaseSnooze.<tier>`
(direct, security never snoozes) and `evolve.releaseAck.canary.<stableVersion>`
(canary lag, re-arms when the pointer next advances) **suppress the loud banner only**
— the cell keeps telling the truth (e.g. an acked `lag-stuck` still shows "behind"
in amber/loud color, just without the row). This preserves both keys' existing
semantics while fitting the new model.

### The D-3 fold (transient post-promote lag) — **this resolves D-3 here**

D-3 in the backlog below ("post-promote *behind* banner shows a yellow ⚠ during
the *expected* redeploy window") is **fixed by this contract and must not be fixed
twice.** The mechanism:

- After a promote, `stableV` advances and `promotedAt` is stamped. Member bots are
  redeployed by the puller's lagging-bot sweep over the next tick(s). During that
  window `evolve_synced===false` for the not-yet-redeployed bots — **expected, self-
  healing.**
- `lagTransient` (within `REDEPLOY_GRACE` of `promotedAt`) → state `lag-redeploying`,
  **neutral, quiet** ("Redeploying…"). No banner. No "⚠ behind".
- `lagStuck` (past `REDEPLOY_GRACE`, *or* no `promotedAt` to vouch for it) → state
  `lag-stuck`, **loud, banner.** A deploy genuinely failed.

The distinguisher is **time-since-pointer-move**, server-stamped (`promotedAt`),
not a client clock guess — the same discipline as D-2's server-stamped recency
fields. Mark D-3 **folded into D-8** in the backlog; close it without a second fix.

### Controls preserved (the expand region)

Every D-5 control remains reachable inside the collapse panel — nothing is dropped,
only moved out of the always-on rows. The expand region renders as **one
consolidated "Release & update" card** (`renderUpdatesDrawer` →
`_drawerCanarySection` / `_drawerDirectSection` → `#ov-release-panel`), merging
what were three separate boxes into stacked rows: the Evolve release-pipeline
track (promoted pointer, candidate soak with a single git-backed ETA, lag
warning, and all controls co-located) and the OpenClaw runtime track. A single
source of truth backs each fact — the duplicate soak ETA that the panel and the
soak banner each computed (and could disagree on) is gone. `#ov-sync-banner` is
reserved for live promote/rollback job progress.

| Control | Backing today | Reachable from |
|---------|---------------|----------------|
| **Complete soak now** | `POST /api/release/promote` (web, runs as `evolve`, zero sudo) | expand — only when `cand.state==="soaking"` & `!pinned` |
| **Roll back** | `release_rollback` — CLI today (`evolve-admin release rollback`); web route is a deploy follow-up | expand — only when `prevV` exists & differs from `stableV` |
| **Pin** / **Freeze auto-promotion** | `release_pin` (CLI). "Freeze" = pin-in-place (`ref==stable`); "Pin" = pin to a chosen ref. Both set `state.pin`. | expand — when `!pinned` |
| **Unpin** | `release_unpin` (CLI) | expand — when `pinned` |
| **Re-check** (OpenClaw safety) | `POST /api/oc/safe-upgrade/check` → poll `/api/oc/version` (web) | expand — when `oc.update_available` |
| **Acknowledge** (lag) | client-only `evolve.releaseAck.canary.<stableVersion>` | expand or banner — when `lag-stuck` banner is showing |
| **Snooze** (direct update) | client-only `evolve.releaseSnooze.<tier>` (security never snoozes) | banner — direct-mode `update-*` states |

**localStorage keys that MUST keep working** (no rename, same semantics):

- `evolve.releaseSnooze.<tier>` — `tier ∈ {feature, maintenance}` (security excluded);
  TTL 7d (feature) / 30d (maintenance). Suppresses the **banner** only.
- `evolve.releaseAck.canary.<stableVersion>` — keyed by stable version so a *new*
  release's lag re-surfaces; suppresses the **banner** only.

The CLI-only controls (Roll back / Pin / Unpin) stay operator-reachable as the
documented `sudo evolve-admin release …` commands surfaced in the expand region's
help text **today**; wiring them to `evolve`-run web routes (mirroring the
zero-sudo promote route) is a separate deploy bite — the cell/expand contract does
not depend on it landing first. **No `sudo <cli>` hints in any web button** — if a
web route is added it runs as `evolve` (the admin server owns `release.json`).

### Derivation algorithm (reference — pure, no IO)

```
function updatesCellState({mode, release, latest, oc, bots, now}):
  rel       = release || {}
  corrupt   = rel.corrupt === true
  pin       = rel.pin || null
  cand      = rel.candidate || null
  stableV   = rel.stable_version || null
  prevV     = rel.previous_version || null
  promoted  = rel.stable && rel.stable.promoted_at
  members   = bots.filter(b => b.role !== 'primary' && b.id !== rel.canary_bot)
  behind    = members.filter(b => b.evolve_synced === false)
  grace     = 30 * 60 * 1000
  transient = behind.length && promoted && (now - Date.parse(promoted)) <= grace
  stuck     = behind.length && (!promoted || (now - Date.parse(promoted)) > grace)
  pinned    = !!pin && !corrupt

  if (corrupt)                                  return S('pipeline-halted',  'loud')
  if (stuck)                                    return S('lag-stuck',        'loud')
  if (mode!=='canary' && latest?.tier==='security' && behind.length)
                                                return S('update-security',  'loud')
  if (oc?.update_available)                      return S('oc-update',        'amber')
  if (pinned)                                    return S('pin-held',         'neutral')
  if (cand?.state==='soaking')                  return S('candidate-soaking','neutral')
  if (cand?.state==='checking')                 return S('candidate-checking','neutral')
  if (cand?.state==='failed')                   return S('candidate-blocked','amber')   // → loud if persists
  if (transient)                                return S('lag-redeploying',  'neutral')
  if (mode!=='canary' && latest && behind.length)
                                                return S('update-available', 'amber')
  return S('up-to-date', 'success')
}
// banner = (state.severity === 'loud') && !snoozedOrAcked(state)
```

### Test-case checklist

The `ui` chip implements against this table; the machine-readable mirror lives at
[`fixtures/deploy-updates-cell-states.json`](fixtures/deploy-updates-cell-states.json)
(new file, pure data — no web asset touched).

| # | Inputs (abbreviated) | Expected state | Cell label | Severity | Banner |
|---|----------------------|----------------|------------|----------|--------|
| 1 | `release.corrupt=true` | `pipeline-halted` | Release halted | loud | yes |
| 2 | 3 bots behind, `promoted_at` 2 h ago | `lag-stuck` | 3 of 10 behind release | loud | yes |
| 3 | 3 bots behind, `promoted_at` 5 min ago | `lag-redeploying` | Redeploying… | neutral | no |
| 4 | 3 bots behind, **no** `promoted_at` | `lag-stuck` (fail-loud) | 3 of 10 behind release | loud | yes |
| 5 | direct, `latest.tier=security`, 1 behind | `update-security` | Security update | loud | yes |
| 6 | direct, `latest.tier=security`, snoozed N/A (security never snoozes) | `update-security` | Security update | loud | yes |
| 7 | `oc.update_available`, `safety_check.ok=true` | `oc-update` | OpenClaw update · ✅ safe | amber | no |
| 8 | `oc.update_available`, `safety_check=null` | `oc-update` | OpenClaw update · check upgrade | amber | no |
| 9 | `pin` set, candidate soaking | `pin-held` | Promotion frozen | neutral | no |
| 10 | candidate soaking, not pinned | `candidate-soaking` | Update soaking · ~Nm left | neutral | no |
| 11 | candidate checking | `candidate-checking` | Update gating | neutral | no |
| 12 | `candidate.state=failed`, recent | `candidate-blocked` | Update blocked | amber | no |
| 13 | direct, `latest.tier=maintenance`, 2 behind | `update-available` | Update available | amber | no |
| 14 | fleet on stable, nothing in flight, no OC update | `up-to-date` | Up to date | success | no |
| 15 | `lag-stuck` AND acked for this `stableVersion` | `lag-stuck` | 3 of 10 behind release | loud | **no** (ack suppresses banner, cell stays) |
| 16 | OC update ready AND a candidate soaking | `oc-update` (rank 4 > 6) | OpenClaw update | amber | no |
| 17 | pinned AND OC update ready | `oc-update` (rank 4 > 5) | OpenClaw update | amber | no |

### Data-contract deltas (server-side, deploy-owned, small)

These three additions to `release_ui_view` (`release_manager.py`) are what the cell
needs; each is a few lines and does not touch `web/`:

1. **`stable.promoted_at`** — ISO of the last pointer move; stamp alongside the
   existing `_stamp_recency` recency fields. Powers the D-3 transient/stuck split.
2. **`pin`** — surface the existing `ReleaseState.pin` (`{sha, reason}` or `null`) in
   the view. Powers `pin-held`.
3. **`corrupt`** — surface the boolean already implied by the corrupt-`release.json`
   error path. Powers `pipeline-halted`.

Until they land the cell degrades fail-loud (no `pin` → not pinned; no `promoted_at`
→ lag reads stuck after one poll; corrupt still detectable via the status error).

---

## Backlog (seeded from the 2026-06-14 promote incident)

The operator hit "Complete soak now", got **"Promote failed: tick ran but did not
promote — see steps above"** (no steps shown), then a second press promoted, then
"8 of 8 bots behind". **Diagnosis: the deploy was correct end-to-end** — fleet + all
10 bots landed on the real tip `e970a96e` (#2884, 5 commits ahead of the prior
stable). All three surprises were **legibility bugs, not deploy failures**:

- **D-1 — promote-error UX (top priority; this is what bit the operator).** The web
  promote path (`release_routes.py:128`) falls through to a CLI-flavored generic
  message when the tick returns no `promoted_to` and no `error`. Root cause: a
  **candidate-replacement race** — while the soaking candidate (#2883) was being
  promoted, `origin/main` had advanced to #2884, so the tick correctly *replaced* the
  candidate and restarted Gate 1 instead of promoting. This is benign and normal.
  Fixes: (a) surface the captured `rt.steps` in the web job drawer (they exist in the
  job log but aren't shown); (b) detect the replacement case (tick returns empty
  `promoted_to` but a *different* `candidate_sha` in `checking`) and report it as
  info — "a newer commit (#NNNN) arrived and is now soaking; promote it once Gate 1
  passes" — not "Promote failed".
- **D-2 — version legibility.** `version_for_sha` makes the trailing component the
  **PR number** (`(#NNNN)`), assigned at PR-creation, not a monotonic build counter.
  #2884 merged *after* #2885/#2886, so the tip has a *lower* trailing number than the
  prior stable → `previous v2885 > stable v2884` reads as a backward move to a human
  (the full date-first string sorts correctly; only the eyeballed tail misleads).
  Make release/version surfaces legible: lead with short-sha + commit date and/or the
  ancestry relationship ("5 commits ahead of previous"); de-emphasize the PR ordinal.
  Also reconcile the stale top-level `install.json::version` (showed
  `2026.0611.2759` while every bot was at `2026.0614.2884`).
- **D-3 — post-promote "behind" banner. → FOLDED INTO D-8.** "N of M behind stable"
  shows a yellow ⚠ during the *expected* post-promote redeploy window (self-heals
  within a tick). **Resolved by the D-8 contract above** (`lag-redeploying`, neutral
  + quiet, distinguished from `lag-stuck` by server-stamped `stable.promoted_at` vs
  `REDEPLOY_GRACE`). Do **not** fix this separately — implement D-8 and D-3 closes
  with it; the only standalone work left is the `stable.promoted_at` stamp (a D-8
  data-contract delta).
- **D-4 — mcp-bridge kickstart timeout.** Recurring puller-hook WARN: "restart
  ai.evolve.evolve.mcp-bridge failed: kickstart timed out after 15.0 s". Real
  reliability nit in the promote/redeploy hook path — investigate the timeout vs. the
  bridge's start cost.

### In-flight ledger

See [[project_deploy_meta_2026_06_14]] for the live in-flight ledger (PRs/chips +
state). Nothing dispatched yet at scaffold time.
