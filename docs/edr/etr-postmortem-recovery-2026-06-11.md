# ETR Post-Mortem Recovery — what the Evolve Test Rig was, and why it was killed

**Date:** 2026-06-11
**Author:** recovered from git history (read-only archaeology)
**Purpose:** Recover the full story of ETR before designing its successor (EDR,
the Evolve Development Rig). Don't redesign over a grave without reading the
headstone.

> Scrub note: this doc uses placeholders for hosts/users/bots
> (`<laptop>`, `<mini>`, `<admin-user>@<mini>`, `<bot>`). The historical
> code/docs hardcoded none of these — ETR's topology lived in a
> `rig-config.yaml` precisely so names weren't baked in.

---

## TL;DR

ETR (Evolve Test Rig) was a **detect-and-queue regression-detection harness for
an Evolve pod** — a small cluster of laptop-local tools (an SSH-driven Sonnet
worker on a 15-min cron, an Opus nightly reviewer, and a Flask dashboard) that
exercised a live pod against a catalog of operator-curated probes and filed
structured findings when something broke. It was added **2026-04-19 (PR #52)**
and retired **2026-05-22 (PR #1441)** after ~3 weeks of operation. The stated
reason for killing it: its auto-fix path produced **zero successful auto-fixes
in 10 days**, the detect-only successor's catalogs went empty and stayed empty
(last worker activity 2026-05-05), and the job it was meant to do was by then
covered canonically by the **Signal store + monitors + Proposals** arbiter path.

**ETR is NOT the same thing as the app-test surface** removed 2026-06-08/09
(PR #2488). See [§7](#7-etr-vs-the-app-test-surface-distinct).

---

## 1. What ETR was

From the framework README as it stood at retirement
(`docs/ETR_README.md`, verbatim):

> **ETR is a regression-detection harness for an Evolve pod.** It's a
> small cluster of tools that sit next to the system they probe: a
> worker that exercises the pod on a cadence and files structured
> issues when something breaks, plus a dashboard that surfaces those
> issues as a queue you (or a Claude Code session) can drain.

The job, in one line (verbatim):

> Every 15 minutes, check one item from a catalog of operator-curated
> probes. If it passes, move on. If it fails, write a structured
> issue that explains exactly what broke, what was expected, and the
> author's best guess at the cause — then drop it into the queue.

The original spec (`docs/archive/spec-evolve-test-rig-2026-04-19.md`) framed it
as automating the QA cycle —
**discover → describe → investigate → fix → merge → verify** — so that
"expensive human and Opus attention is spent on judgment and not on mechanical
repetition." It was deliberately positioned as a **sibling to the RSI arbiter**,
not a subsystem of it: "The arbiter improves Evolve; the rig verifies it."

### The three-nested-layers framing (load-bearing context for EDR)

The spec introduced a framing that recurs in later Evolve design and is directly
relevant to a "development rig" successor (verbatim table):

| Layer | What improves | Engine | Verification |
|---|---|---|---|
| **L1** | A bot's apps + config | Forge | Application `test_cases`; ETR app-runner (v3) |
| **L2** | A pod's Evolve install | RSI arbiter (PR #51) | ETR catalog (v0-v1) |
| **L3** | The Evolve codebase itself | Meta-arbiter + FeatureManifests (v4) | ETR catalog generated from manifests |

ETR-as-shipped only ever did **L2** (verifying a live pod's Evolve install). L1
(app-level testing in Forge) and L3 (the Evolve *codebase* improving itself, via
"FeatureManifests" + meta-arbiter generators like `architect_generator` /
`scaffolder_generator`) were **spec-only aspirations (v3/v4) that were never
built.** This matters: the L3 ambition — the codebase improving itself with an
opt-in `betterEngine.improveEvolve: true` flag, fork-and-PR-upstream — is the
conceptual ancestor of anything called a "Development Rig."

---

## 2. What it did (capabilities)

**Shipped and operational (L2, detection):**

- **Catalog-driven probing.** Operator-curated markdown catalogs under
  `docs/verification/catalogs/{feature,standing,archived}/`, one "is this still
  working?" check per item. A Sonnet worker walked the active catalog, picked
  the first pending item whose `depends_on` passed, ran its probe, and compared
  stdout to an `Expected` block.
- **Three probe verbs**: `ssh_cmd:` (read-only, gated), `invoke:` (pre-authorized
  mutation — the catalog file *is* the ACL), and `local_cmd:` (runs on the rig
  host). `[opus]`-suffixed items were skipped by the worker for the nightly Opus
  pass. `bake_hours: N` gated soak-period checks.
- **Structured findings queue.** Failures wrote `issues/open/<date>-<id>-<slug>.md`
  with expected/actual/best-guess-cause, fingerprinted the failure signature, and
  cascaded-skipped sibling items sharing a fingerprint.
- **Opus nightly review.** Drained `issues/opus-review/`, one Opus session per
  finding, classifying verdict (real-bug / operational / benign /
  cascading-duplicate / needs-design-decision) and writing a structured
  `## Opus review` recommendation.
- **Flask dashboard** (`tools/etr-dashboard/`): Timeline / Today / Catalog /
  Inbox / Submit-a-bug / Propose-a-feature tabs, env-fix banner, status pills.
  Read-only against the repo filesystem; fired control actions by shelling out
  to the cron script or writing repo files.
- **Safety gating.** The SSH wrapper refused forbidden patterns (`sudo`,
  `rm -rf`, `launchctl ... unload|stop|kickstart`, force-push), logged every
  call to `docs/verification/log/<date>.md`, and honored a `.etr-paused` kill
  switch. The original spec's inviolable rules: no merges to main, no
  force-push, no sudo, no writes under bot homes or shared_dir.
- **Env-contract doctor** (`etr-doctor` + `env-contract.yaml`): declarative
  "what should be true" assertions with whitelisted `--repair`.
- **Catalog linter + CI** (`.github/workflows/catalog-lint.yml`,
  `polling-bypass-lint.yml`): caught catalog-authoring bugs (sudo in `ssh_cmd:`,
  hardcoded names/paths, missing sections).

**Spec-only / never built:**

- **v1** — sandbox pod + environment-independent YAML catalogs + run-on-every-PR
  CI. (The CI workflows that did ship were catalog *linters*, not full-catalog
  runs against a sandbox.)
- **v2** — distributed bug-report importer (`evolve-repro-import`) for users
  beyond the authors.
- **v3** — app-level testing inside Forge (the "app runner" that spins a bot in a
  sandbox and runs its `test_cases`).
- **v4 / L3** — FeatureManifests + meta-arbiter generators; the Evolve *codebase*
  improving itself.

> Note on "v2" / "v3" terminology drift: the original spec used v0–v4 for a
> *staged rollout roadmap*. By retirement, the README used "v2" to mean the
> historical **auto-fix worker** and "v3" to mean the **detect-only worker** that
> replaced it. The kill commit uses the README's sense. They are not the same
> numbering. This doc flags the sense each time.

---

## 3. Architecture

Two roles, not necessarily two machines (topology declared in
`rig-config.yaml`, co-located or SSH-split):

```
 ┌─────────────────────────────┐      probes      ┌───────────────────┐
 │  ETR host (<laptop>)        │  ─────────────▶  │  Pod host (<mini>) │
 │    • dashboard (Flask)      │                  │    • evolve bots   │
 │    • worker (Sonnet, 15min) │  SSH + invoke    │    • admin-ui      │
 │    • opus-review (nightly)  │  ◀── issues ─────│    • shared state  │
 │    • cron + log tail        │                  │                    │
 └─────────────────────────────┘                  └───────────────────┘
        git commits as state — catalogs, issues, sidecars all in-repo
```

**Six programs sharing a filesystem + a GitHub remote** (no database; all state
is git-committed files):

| Component | Historical path | Role |
|---|---|---|
| SSH wrapper + safety gate | `tools/evolve-verify` | every pod command; forbidden-pattern gate, audit log, kill switch |
| Cron / LaunchAgent entry | `tools/evolve-verify-cron.sh` | `worker` (15min) / `opus-review` (nightly) / opus-feature/catalog/auto-merge subcommands; lockfile, 401-auth detection |
| Worker contract | `tools/skills/evolve-verify/SKILL.md` | the ~1,300-line prose spec the Sonnet worker loaded each tick (tracked under `tools/` because `.claude/` is gitignored) |
| Dashboard | `tools/etr-dashboard/` | Flask UI over the repo filesystem |
| Topology config | `tools/etr_config.py` + `docs/verification/rig-config.yaml` | single source of truth for hosts/SSH-target/bot-names/remote |
| Doctor + linter | `tools/etr-doctor`, `tools/etr-pod-doctor`, `tools/etr-catalog-lint` | env-contract audit, pod-contract audit over SSH, catalog structural lint |

**On-disk state** lived under `docs/verification/` (catalogs, `*.state.json`
sidecars, `ACTIVE` pin, `recipes/`, daily `log/`) and `issues/`
(`open/`, `opus-review/`, `fixed/`, `dismissed/`, `reports/`, `features/`).
Ephemeral logs/locks lived in `/tmp/etr-*`.

**Admin-UI tendrils** (later unwired by the kill): a `/api/doctor` endpoint in
`server.py`, an "ETR" status strip in the Overview page, a `rig-health-dot` in
the sidebar footer, and a `WRAPPER_CACHE` block inside
`probes.py::probe_subprocess`.

**Salvage-that-wasn't:** `packages/admin/evolve_admin/etr_polling.py` (cache +
coalesce + backoff primitives born from the 2026-04-25 SSH-wedge incident) had
**zero production callers** at kill time — the planned migration to make it the
live primitive never happened (the tactical TTL cache in `oc_cli.oc_command`
remained the real one).

> Documentation set at retirement (all deleted): `docs/ETR_README.md`,
> `ETR_ARCHITECTURE.md`, `ETR_TOPOLOGY.md`, `ETR_ENV_CONTRACT.md`,
> `ETR_OPERATIONS.md`, `ETR_GLOSSARY.md`, `CATALOG_LINT.md`, nine
> `docs/spec-etr-*.md`, `runbook-etr-v2-deployment.md`, plus the archived
> original spec and the `etr-handoff-2026-04-22.md`. ETR was, by the end,
> *heavily* documented relative to its working footprint.

---

## 4. Timeline

| Date | SHA / PR | Event |
|---|---|---|
| 2026-04-19 | `bc51f5be0`, merge `b269264ae` (**PR #52**) | **Added.** ETR v0 — shared state + SSH wrapper + Sonnet worker. First worker tick was meant to drain the RSI-v2 deploy checklist (PR #51). |
| 2026-04-20 | `e3a564a72` (PR #60), `2c8317745` (PR #61) | Flask dashboard; LaunchAgent install. Worker ticks begin (`etr: worker run ...` commits). |
| 2026-04-22 | `docs/archive/etr-handoff-2026-04-22.md` | Upgrade-handoff grounding doc (D1 catalog discovery, PRs #91/#92). |
| 2026-04-25 | `docs/postmortem-ssh-wedge-2026-04-25.md` | SSH-wedge incident → the polling/cache primitives that became `etr_polling.py`. |
| ~2026-05-02 | (README "Status: detection-only since 2026-05-02") | **v3 cutover**: dropped the entire auto-remediation contract after the auto-fix worker produced zero successful auto-fixes in 10 days. Worker became detect-and-file only. |
| 2026-05-05 | `7f39eb06c` (last `etr: worker run`, "0 checked, 0 filed") | **Last worker activity.** Catalogs (`feature/` and `standing/`) empty; every tick logged "nothing pickable; idle." |
| 2026-05-22 | `937609516` → squashed `e21171996` (**PR #1441**) | **Retired.** −55,112 / +13 across 136 files. |

So: **operated ~3 weeks (Apr 19 → May 5 active), formally retired May 22** — i.e.
it sat idle for ~17 days before removal.

---

## 5. Why it was killed (verbatim rationale)

The rationale **is** recorded — clearly and in detail. From the kill commit
`e21171996` / PR #1441 description (verbatim):

> The detect-and-queue rig (ETR) was an experiment to automate
> regression detection of pod capabilities. After running it for ~3
> weeks the data was clear: v2's auto-fix path produced zero successful
> fixes after 10 days; v3 (detect-only) saw its catalogs go empty and
> stay empty. Last worker activity was 2026-05-05. The system the rig
> was meant to feed, Signals + monitors + Proposals, now covers the
> same job through the canonical arbiter path.

And from the PR #1441 summary (verbatim):

> - v2's auto-fix path produced **zero successful auto-fixes after 10 days**
>   (documented in the v3 README we just deleted)
> - v3 (detect-only) had its catalogs go empty and stay empty; last worker
>   activity was **2026-05-05** (17 days ago)
> - The job ETR set out to do — continuous regression detection of pod
>   capabilities — is now covered canonically by the **Signal store + monitors**
>   path (pod_report, audit, watchdog, host_health, integration_probe, etc.)
> - The one piece I expected to be salvageable, `etr_polling.py` ... turned out
>   to have **zero production callers** — the migration the spec called for never
>   happened

**Three distinct failure findings, separated:**

1. **Auto-fix never worked.** The most ambitious capability (auto-remediation:
   re-verify, recurrence-meta promotion, soak windows, `fix_sha` attribution,
   triage matchers — ~1,500 lines of contract) produced **zero successful
   auto-fixes in 10 days**. Every fix that ever landed was a human in a Claude
   Code session. This was already conceded at the v3 cutover (~May 2), not at
   kill time.

2. **Detection went dark on its own.** Even after retreating to detect-only, the
   catalogs emptied and *stayed* empty — coverage only grew by manual catalog
   authoring, and nobody authored. The rig spent its last fortnight logging
   "nothing pickable; idle." It died of disuse before it was deleted.

3. **The job migrated to a better home.** Continuous regression detection of pod
   capabilities became the **Signal store + monitors → Proposals** arbiter path
   (the canonical observation layer documented in CLAUDE.md). ETR was redundant
   with the system that superseded it.

---

## 6. Analysis for the successor (EDR)

> Mixed quoting/inference. Items marked **[quoted]** are from the recovered
> commit/PR/spec text; items marked **[inference]** are this author's reading.

What a successor must address to avoid repeating ETR's failure:

1. **Don't ship auto-fix as a headline capability without evidence it can land a
   fix.** **[quoted]** ETR's auto-remediation produced zero successful fixes in
   10 days and was the first thing cut. **[inference]** If EDR includes any
   automated change-application, it should start in a measure-only / propose-only
   mode and earn autonomy from a tracked success rate — mirroring how the RSI
   arbiter's autonomy ladder is gated on track record.

2. **Coverage must not depend on humans hand-authoring catalogs.** **[quoted]**
   v3's catalogs "went empty and stayed empty"; coverage grew only by manual
   addition. **[inference]** EDR needs coverage that is generated or
   self-sustaining (from existing tests, manifests, or signals) — a queue that
   only fills when a human remembers to fill it will go dark.

3. **Build on the canonical observation layer, don't fork it.** **[quoted]** The
   stated reason for redundancy is that Signals + monitors + Proposals "now
   covers the same job through the canonical arbiter path." **[inference]** EDR
   should *consume/produce* Signals and Proposals rather than maintain a parallel
   findings store (`issues/*`) and a parallel dashboard. ETR's separate
   git-as-database queue duplicated what the arbiter now owns.

4. **Watch for the salvage trap.** **[quoted]** `etr_polling.py` was built,
   tested, and never wired to a production caller — the migration "never
   happened." **[inference]** Infrastructure built ahead of a consumer tends to
   rot; build the consumer first, or in lockstep.

5. **Beware the doc-to-working-code ratio.** **[inference]** At death ETR carried
   ~7 framework docs + 9 `spec-etr-*` files + a runbook + an archived spec, for a
   system whose live function was "file a markdown issue when a curated probe
   fails." Aspiration (v1–v4, the L3 codebase-self-improvement vision) vastly
   outran what was built and used. A successor should be sized to a demonstrated
   need and grow from use, not from roadmap.

6. **Honest scope check.** **[inference]** ETR-as-built was a **modest L2 pod
   regression harness**, not the grand three-layer development rig its spec
   sketched. If EDR aims at the L3 "Evolve develops itself" vision (FeatureManifests,
   `architect_generator`/`scaffolder_generator`, opt-in `improveEvolve`), it should
   treat that as a genuinely new and unproven build — ETR validated *none* of L3;
   it only ever ran L2 detection, and even that briefly. Do not inherit confidence
   from ETR's existence.

**What did work and is worth keeping** **[inference, from spec/README]**: the
catalog-as-ACL `invoke:` model (authorization established at merge/review time,
not runtime); the forbidden-pattern SSH gate + per-call audit log + `.etr-paused`
kill switch; topology-in-config (no hardcoded hosts/bots); and the clean
escalation split (mechanical → Sonnet, judgment → Opus/human). These were sound
primitives; the failure was in *demand and feedback*, not in the safety/structure
design.

---

## 7. ETR vs. the app-test surface (DISTINCT)

These are two different removals and must not be conflated:

| | **ETR** | **App-test surface** |
|---|---|---|
| Killed by | PR #1441, `e21171996` | PR #2488, `c331551f0` |
| Date | 2026-05-22 | 2026-06-09 (memo `docs/decision-app-tests-2026-06-08.md`) |
| Layer | **L2** — verified the *pod's Evolve install* | **L1** — verified *bot applications* (Forge) |
| What it was | SSH-driven Sonnet worker + Opus reviewer + Flask dashboard probing a live pod against catalogs | Forge test gate, `test_runner.py` / `behavioral_runner.py` / `test_telemetry.py` / `reliability.py`, 9 per-bot `ai.openclaw.evolve.test.<bot>` daemons, `app-test-scheduler` |
| Replaced by | Signal store + monitors + Proposals | audit + coherence (the load-bearing app-correctness verifiers) |
| Net diff | −55,112 / +13 | +598 / −8,803 |

The original ETR spec *did* sketch app-level testing as its "v3" (an "app runner"
inside Forge) — so the two share conceptual DNA in the spec — but the app-test
surface that shipped and was later removed was **built independently** and lived
in `packages/` (Forge gate + admin runners + per-bot daemons), not in ETR's
`tools/etr-*` / `docs/verification/` tree. ETR's own "v3" app-runner was never
built. (See also the memory note "App-test surface killed 2026-06-08".)

---

## Source index (historical paths, all on `main` history pre-#1441)

- `docs/ETR_README.md`, `docs/ETR_ARCHITECTURE.md`, `docs/ETR_TOPOLOGY.md`,
  `docs/ETR_ENV_CONTRACT.md`, `docs/ETR_OPERATIONS.md`, `docs/ETR_GLOSSARY.md`
- `docs/archive/spec-evolve-test-rig-2026-04-19.md` (original spec, archived)
- `docs/archive/etr-handoff-2026-04-22.md`
- `docs/spec-etr-*.md` (9 files), `docs/runbook-etr-v2-deployment.md`,
  `docs/CATALOG_LINT.md`
- `tools/evolve-verify`, `tools/evolve-verify-cron.sh`, `tools/etr_config.py`,
  `tools/etr-dashboard/`, `tools/skills/evolve-verify/SKILL.md`
- `packages/admin/evolve_admin/etr_polling.py` (+ test)
- Add: PR #52 (`b269264ae`) / commit `bc51f5be0`
- Kill: PR #1441 (`e21171996`, squashed `937609516`)
- Distinct removal — app-test surface: PR #2488 (`c331551f0`)
