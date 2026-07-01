# Spec delta: plain-language updates + `direct`-by-default posture

**Date:** 2026-06-22 · **Aspect:** `deploy` (contract) + `ui` (render) + `footprint` (posture dial)
**Status:** authoritative · supersedes the "flip the default to canary" intent noted in
[`release_manager.py`](../packages/admin/evolve_admin/release_manager.py) (mode-resolution comment).

Builds on [`spec-deploy-meta-2026-06-14.md` §D-8](spec-deploy-meta-2026-06-14.md) (the "Updates"
cell state machine) and [`spec-state-store-and-deploy-resilience-2026-06-10.md` §2](spec-state-store-and-deploy-resilience-2026-06-10.md)
(the release pipeline of record). It does **not** change pipeline mechanics — it changes the
**default posture** and the **operator-facing vocabulary** that sits on top of them.

---

## Motivation

Operator feedback (2026-06-22): the update system reads as convoluted. "Promote", "soak",
"canary" are hyperscale-borrowed terms foreign to an admin who expects a binary
upgrade-or-not, and "one bot upgrades while the others stand by" looks arbitrary. The
questions raised — *is canary necessary? can't rollback alone protect us? scan instead of
soak? is this best practice?* — resolve to three findings:

1. **The simple path is already the default.** `DEFAULT_RELEASE_MODE = "direct"` (instant
   deploy + Gate-1 static scan + one-command rollback). Canary is opt-in; the dev/test pod
   is *explicitly* opted in because it dogfoods the pipeline.
2. **Canary is best practice at *scale*.** Progressive rollout earns its keep when a small
   percentage of *thousands* of nodes yields statistically meaningful, auto-analyzed signal.
   A 10-bot pod running a fixed-time soak on one bot is a *degenerate* canary — a smoke test
   wearing scale vocabulary. The skepticism is legitimate.
3. **Rollback is necessary but not sufficient — for the reversible majority it *is* the
   plan.** The pipeline already tiers changes (`skip`/`short`/`full`,
   [`spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md`](spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md)):
   docs/tests promote on scan + rollback-net; ordinary reversible code gets a quick active
   probe; only **irreversible/privileged** changes (auth/sudoers/perms, migrations,
   already-sent messages) get a real dwell — because for those, rollback restores the *code*
   but not the *consequence*. A static scan cannot catch the runtime/cost class ("compiles
   fine, but loops the bot into runaway spend"), so scan does not *replace* the probe; it
   precedes it.

**Decision:** keep the layered protection, but make `direct` the honest default and stop
leaking pipeline vocabulary to the operator.

---

## Part 1 — Posture: `direct` is the default; `canary` is opt-in

- **`DEFAULT_RELEASE_MODE = "direct"` is locked.** The previously-planned "follow-up PR
  flips the default to canary" is **cancelled.** A fresh install ships the simple path:
  instant deploy, Gate-1 static scan as the pre-flight, one-command rollback as the net.
- **Release posture is a facet of the META:footprint dial** (Passive/Standard/Managed):
  - **Passive / Standard → `direct`** — instant deploy + scan + rollback. The right ergonomic
    for a single pod or small, supervised fleet.
  - **Managed → `canary`** — the gated candidate→scan→probe→promote pipeline. For large or
    unsupervised fleets where the cost of exposing every user-facing bot to an unvetted
    (sometimes auto-merged) change at once outweighs the latency + ceremony.
  - Wiring the posture→mode mapping into the dial is a **`footprint`-owned** deposit (see
    §routing); `deploy` owns the mode mechanics, `footprint` owns the dial.
- **When canary earns its keep (doc):** large fleet, unattended operation, or a high rate of
  autonomous/auto-merged change. Otherwise prefer `direct`. This rationale ships in the
  deployment guide so the choice is legible, not folklore.

The live dev/test pod stays on `canary` for now (preserves the dogfood testbed); flipping it
to `direct` is a later one-line `pod.release.mode` change once the relabel is verified.

---

## Part 2 — Operator-facing vocabulary (the contract `ui` implements)

The operator sees **one model in both modes**: `Up to date → Update available → Updating… →
Live`, with **Undo** always one action away. Internal pipeline terms never surface.

### State → label

`deploy` owns the state ids (§D-8 enumeration); this table adds the **plain label** each
state renders, per mode. `ui` renders these strings; it must not invent new ones.

| §D-8 state id | Internal meaning | Direct label (default) | Canary label (opt-in) |
|---|---|---|---|
| `up-to-date` | fleet on current version | **Up to date** | **Up to date** |
| `update-available` | new commit not yet on fleet | **Update available** | **Update available** |
| `candidate-checking` | Gate-1 static scan running | **Checking update…** | **Checking update…** |
| `lag-redeploying` | deploying to fleet, in grace | **Updating…** *(N of M)* | **Updating…** *(N of M)* |
| `candidate-soaking` | active-probe / dwell | *(n/a in direct)* | **Testing update…** *(~Nm left, only if it dwells)* |
| `candidate-blocked` | a candidate failed its gate | **Update blocked** | **Update blocked** |
| `lag-stuck` | genuine stuck lag past grace | **Update failed on N bots — roll back?** | **Update failed on N bots — roll back?** |
| `pin-held` | auto-promotion frozen | **Auto-updates paused** | **Auto-updates paused** |
| `pipeline-halted` | corrupt `release.json` | **Updates halted — needs attention** | **Updates halted — needs attention** |
| `update-security` | security-tier update pending | **Security update** | **Security update** |
| `oc-update` | OpenClaw runtime update | **OpenClaw update** *(✅ safe / check)* | **OpenClaw update** *(✅ safe / check)* |

### Button / control text

| Current (internal) | New (operator) | Shown when |
|---|---|---|
| Complete soak now | **Make live now** | canary, an update is testing & not paused |
| Roll back | **Undo last update** *(→ v‹prev›)* | a previous version exists & differs |
| Pin / Freeze auto-promotion | **Pause auto-updates** | not paused |
| Unpin | **Resume auto-updates** | paused |
| Check if upgrade is safe | **Check OpenClaw update** | an OC update is available |

### Glossary (internal → operator) — kill on sight in any operator-facing string

| Internal | Operator |
|---|---|
| soak / soaking | testing |
| canary bot | test bot |
| candidate | update |
| promote / promoted | make live / live |
| stable (pointer) | current version |
| behind release / behind stable | waiting to update / updating |
| rollback | undo / roll back |
| bootstrap | *(CLI-only operator escape hatch — never surfaced in the web UI)* |
| pin | pause auto-updates |

**Scope:** the cell label + the "Release & update" drawer rows (`_drawerCanarySection`,
`_drawerDirectSection`) + button labels in
[`overview.js`](../packages/admin/evolve_admin/web/static/js/pages/overview.js) and the
version cells in [`index.html`](../packages/admin/evolve_admin/web/index.html). The
underlying state ids, CSS classes, localStorage keys (`evolve.releaseSnooze.*`,
`evolve.releaseAck.*`), and API routes are **unchanged** — this is a string-layer relabel
with no behavior delta. Theme parity (dark + light) per `docs/style-guide.md` is required.

**Invariant preserved:** the `pin-held`/"Auto-updates paused" sub-label must still never
claim a testing update "auto-promotes" while paused (§D-8 defense-in-depth).

---

## Part 3 — Right-sizing the canary mechanism (follow-up, not this bout)

Under `direct`-default most pods never soak, so making the active-probe the *visible* norm
(vs. a fixed time dwell) chiefly benefits pods that *stay* on `canary`. Recorded as a deploy
follow-up (D-9), not built here.

---

## Routing / ownership

- **deploy** (this contract): locks `DEFAULT_RELEASE_MODE = "direct"`, owns the state→label
  table + glossary, owns the when-to-use-canary doc.
- **ui** (chip): implements the relabel in `overview.js` + `index.html` against this table;
  theme parity; no new state ids / keys / routes.
- **footprint** (deposit): maps the Passive/Standard/Managed dial → `direct`/`canary`. Not
  spawned from this bout; routed to META:footprint.

## Test obligations

- The §D-8 fixture [`fixtures/deploy-updates-cell-states.json`](fixtures/deploy-updates-cell-states.json)
  gains a `label` expectation per (state, mode); the `ui` chip asserts the rendered cell text
  matches. No pipeline/test behavior changes — a pure string-layer assertion.
