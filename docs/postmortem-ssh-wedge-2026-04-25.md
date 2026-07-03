# Post-mortem: 2026-04-25/26 mini SSH wedge incident

Status: complete (incident resolved 2026-04-26T~04:50Z; monitoring
through 01:00Z UTC measure batch in progress as of writing).

This is a *framework-shaped* post-mortem: the goal isn't to relitigate
who-did-what, it's to map "what happened" → "which framework primitive
would have caught it earlier" → "what changed in the framework as a
result." The incident shaped the v2 ETR design in real time; this doc
captures that mapping for future reference.

---

## TL;DR

For ~30 hours starting 2026-04-25T01:22Z, the mini's userland (sshd,
admin-ui, screensharingd) wedged repeatedly. macOS process count
climbed from baseline ~600 to 1169; openclaw subprocess count climbed
from baseline 8 to 559; the wedge cycle accelerated from once-per-few-
minutes to once-per-90-seconds in the final hour. The ETR worker filed
11 separate findings against the wedge; each was triaged as a transient
or cascading-duplicate and dismissed individually. The structural
pattern was never named until a one-shot agent read every dismissed
finding side-by-side.

The dominant root cause turned out to be admin-ui's `oc_cli.oc_status()`
polling without coalescing — accumulating up to 23 stuck `node openclaw
status --json` subprocesses that admin-ui kept queuing faster than
openclaw could answer. A secondary contributor was the per-bot
LaunchDaemons (`apply.{bot}`, `measure.{bot}`) firing in lockstep with
no jitter, spiking fork load every 5 minutes.

Fixes:
- **PR #330** — jitter wrapper on per-bot daemon installations in `deploy.py`
- **PR #332** — TTL cache + per-bot in-flight dedup in `oc_cli.oc_status()`

After deploy, openclaw procs returned to baseline (13), warm-cache
latency dropped from "queued behind 23 stuck procs" to 63ms, wedge
cycle stopped.

---

## Timeline

| Time (UTC) | Event |
|---|---|
| **2026-04-25T01:22Z** | First wedge. ETR worker files finding 004 (D4 ssh-timeout). Opus verdict: benign / dismissed. |
| 02:05Z | Wedge ongoing. Worker files finding 006 (V2 admin-bot-ssh-timeout). Opus verdict: cascading-duplicate of 004. Dismissed. |
| 02:43Z | Wedge ongoing. Worker files finding 008 (V4 ssh-timeout). Same dismissal. |
| 03:32Z | Wedge ongoing. Worker files finding 010 (P1 budget-hawk-ssh-timeout). Same dismissal. |
| 04:00Z | Cluster ends (briefly). |
| **~23:30Z** | Wedge recurs. Observed during interactive session. Not filed by worker. |
| **2026-04-26T01:00Z** | Daily measure batch fires (worst-case daily load). Wedge through this window. Worker files findings 001-006. |
| ~04:00Z | Other session reads dismissed findings side-by-side. Recognizes the pattern. Writes meta-finding [2026-04-25-011](../issues/opus-review/2026-04-25-011-meta-mini-ssh-wedge-recurring.md) (manually) with the daemon-storm hypothesis. |
| ~04:30Z | Wedge frequency escalating: 04:34Z snapshot has 559 openclaw / 24s wedge; 04:38Z has 559 openclaw / 1m32s wedge (4× degradation in 4 minutes). |
| ~04:32Z | Other session: controlled bisection. `sudo launchctl bootout system/ai.evolve.evolve.admin-ui`. Within seconds, openclaw procs drop 559 → 8. Wedge cycle stops immediately. **Diagnosis confirmed: admin-ui is the engine.** |
| ~04:40Z | PR #330 (jitter) lands. |
| ~04:48Z | PR #332 (oc_cli cache + dedup) lands. |
| ~04:50Z | Re-deploy + re-enable admin-ui on mini. Process count returns to baseline (13 openclaw, 0 node, 648 total). Cache hits observed. |
| Ongoing | Wedge watcher `bsvyssgv5` armed (6h). Continuous reachability probe `b25y2j1im` streaming. 01:00Z UTC daily measure batch is the validation moment. |

---

## What the framework did vs what it should have done

### What it did (v1 behavior)

For each catalog item that timed out against the wedged SSH, the v1
worker:
1. Filed a finding with `verdict: ssh-timeout` (or equivalent prose).
2. Sent the finding to Opus for review.
3. Opus matched the signature against an earlier identical finding.
4. Opus's verdict: `cascading-duplicate of <prior-id>`.
5. Human (Pod-admin) reviewed Opus's recommendation and dismissed.

This loop ran ~11 times. Each dismissal was individually correct in v1
terms — these *were* duplicates of each other. But the structural
signal — "the host is wedging every few hours, the wedge frequency is
escalating, the rig itself is contributing to load" — was destroyed
by the dismissal cascade.

The v1 framework had no mechanism to:
- Cluster findings by root cause within or across ticks
- Refuse to run catalog items when the SUT was unreachable
- Track the rig's own footprint on the SUT
- Distinguish "SUT broke" from "rig couldn't reach SUT"
- Auto-attach system-level evidence to infra findings
- Enforce polling-degrades on internal subprocess calls
- Detect daemons installed without jitter

### What it should have done (v2 behavior, post-patch)

| Tick | What v2 worker would do |
|---|---|
| First tick when SSH is wedged | Pre-flight gate (§5.5.2) detects SSH unreachable. Files ONE `host-unreachable` finding. Skips all catalog items. Exits. |
| Subsequent ticks within the hour | Pre-flight gate detects same condition. Sees existing `host-unreachable` finding from <1h ago. Appends to `## Recurrences` instead of filing a new one. Audit log entry: `deferred: host=mini reason=ssh-unreachable`. |
| First tick after recovery | Pre-flight passes. Worker proceeds to catalog work. Re-verifies the items it had skipped. Closes the `host-unreachable` finding. |
| If the rig is contributing to load | Self-load accounting (§5.5.3) detects proc count > 100. Files ONE `deferred-load-shedding` finding. Backs off. |
| If the wedge recurs across hours | Across-hour clustering not strictly required — cluster window is 1h — but if a 4th wedge happened, the recurrence-meta rule (§5.4.1) would promote. |
| Each filed infra finding | Auto-attaches proc count, launchctl state, OOM/fork log excerpts (§5.5.4). The 559-procs detail that took ad-hoc bash to capture would be on the finding by default. |

**Net change**: 11 findings → 1 `host-unreachable` finding with auto-evidence,
auto-attached recurrences, and auto-deferral instead of amplification.

The dashboard's polling-as-amplifier wouldn't even reach the wedge
state in v2 — the lint (`tools/etr-lint-polling-bypass.py`) catches
direct subprocess-to-SUT-binary calls without the cache/coalesce
decorator at PR time. The 04-26 PR #332 fix would have been the
*initial* implementation, not the post-incident fix.

---

## Root cause analysis

### Primary cause: admin-ui's uncached oc_cli polling

`packages/admin/evolve_admin/web/server.py` endpoints at lines 4127
and 5787 fan out `oc_status(bot_id)` per bot on a polling cadence
shorter than openclaw's response time. With 7 bots × multiple polling
endpoints, admin-ui queued 23 `node openclaw status --json` subprocesses
at the wedge moment (the oldest at 3:00 elapsed) — admin-ui was issuing
new requests before openclaw could answer the previous ones.

`oc_status` had no cache, no coalesce, no backoff. Each call spawned a
fresh subprocess. The dashboard's polling rate * (7 bots) * (no
coalesce) = unbounded subprocess accumulation under any latency spike.

**Why this fingerprint maps to "polling without degradation":** the
2026-04-25 framework patches added §5.5.5 (polling-degrades design
rule) and the wrapper §9-§10 (cache/coalesce/backoff at three layers)
specifically to make this class structurally impossible.

### Secondary cause: lockstep daemon firing

`deploy.py` installed per-bot `apply.{bot}` and `measure.{bot}`
LaunchDaemons with identical `StartInterval` and no per-bot offset.
With 6-7 bots, launchd spawned N Python interpreters in the same
second every 5 minutes. macOS handled this fine in normal conditions
but compounded the load when admin-ui was already creating subprocess
backpressure.

PR #330's `bash -c "sleep $((RANDOM % N)); exec ..."` jitter
deconcentrates the spawn moments without changing the per-tick work.

**Why this fingerprint maps to a standing test:** the new H5 item in
`pod-health-invariants.md` parses `launchctl print` output and flags
any installed daemon without the jitter wrapper. Any future deploy
that re-introduces the unjittered shape gets caught at the next worker
tick.

### Why the original hypothesis was right but incomplete

The original meta-finding (011 as written 2026-04-26T~04:00Z) attributed
the wedge entirely to daemon-spawn storm. That hypothesis was *consistent
with the evidence available at the time* — the wedge timing roughly
aligned with the 5-min cycle, the recurrence-meta rule cited
deploy.py:3408+ as the suspect locations, and the `bash -c sleep
$((RANDOM%N))` fix shape was identifiable from the daemon
installation code.

What the hypothesis *missed* was that admin-ui's polling was the
sustaining engine — the daemon storm provided initial spikes, but
admin-ui's accumulating queue is what made each spike take longer to
recover from. This wasn't visible from reading findings; it required
running `ps aux | grep openclaw | wc -l` during a wedge moment, which
no finding had captured.

The framework's response: SKILL §5.5.4 mandates auto-attaching exactly
this evidence (proc count, launchctl, top, OOM/fork log) to every
infra finding. Future incidents start with the data this incident
required ad-hoc collection to obtain.

---

## Framework changes triggered by this incident

In chronological order of how they emerged in the design conversation:

| Change | Spec location | Description |
|---|---|---|
| Real-time clustering | SKILL §5.5.1 | Within-tick + within-hour same-signature collapse |
| Pre-flight health gate | SKILL §5.5.2 | One `host-unreachable` per hour, not N test failures |
| Self-load accounting | SKILL §5.5.3 | Worker tracks SUT proc count; backs off above threshold |
| Auto-attach infra evidence | SKILL §5.5.4 | proc count + launchctl + log show on env-broken findings |
| Polling-degrades rule | SKILL §5.5.5 | Codified as cross-layer design rule |
| `host-unreachable` verdict | SKILL §5.1 | Distinct from `env-broken` (SUT broke) |
| `deferred-load-shedding` verdict | SKILL §5.1 | Distinct from `awaiting-triage` |
| Wrapper cache+coalesce+backoff | Wrapper §9 | Shell-layer polling-degrades implementation |
| Python polling primitives | Wrapper §10.2 | `@cached_call` decorator (where PR #332's primitives extract to) |
| HTTP polling primitives | Wrapper §10.3 | `etr_polling.httpx_client()` factory |
| Polling-bypass lint | spec-etr-lint-polling-bypass-2026-04-26.md | CI enforcement of polling-degrades at every layer |
| H5 standing item | pod-health-invariants.md | Detects daemons installed without jitter (regression guard) |

These artifacts collectively guarantee: **for the next polling-shaped
code path, this class of incident cannot recur without explicit
opt-out at PR review time.**

---

## Validation evidence

Process / latency comparison:

| Metric | Pre-fix worst (04:38Z) | Post-fix baseline (04:50Z+) |
|---|---|---|
| Total processes | 1169 | 648 |
| openclaw procs | 559 | 13 |
| node procs | 23 | 0 |
| sudo procs | (varied) | 2 |
| Wedge frequency | 1 per 2-3 min, escalating | none in monitoring window |
| `/api/status` warm | (queued behind 23 stuck procs) | 63 ms |
| `/api/status` cold | wedge | 4.3 s |
| Load avg (15 min) | 9.39 | 7.40 → trending down |

Active monitoring:
- `bsvyssgv5` — wedge watcher armed for 6h; silent firing means fix is holding
- `b25y2j1im` — continuous reachability probe; streaming `banner_ok` since 04:45:25Z

Validation gate: 01:00Z UTC daily measure batch is the historical
worst-case spike. If the wedge watcher is silent through that window,
the combined PR #330 + #332 fixes are confirmed against the most
stressful daily workload.

---

## Lessons

### Things the framework now handles structurally

- **A wedge produces 1 finding, not N.** Pre-flight gate + clustering.
- **The rig backs off when it's contributing to load.** Self-load.
- **Infra findings carry the system-level snapshot needed to diagnose.** Auto-evidence.
- **The class of "polling without degradation" cannot pass code review.** Lint.
- **Daemons installed without jitter are detected at the next tick.** H5 standing item.
- **Recurring patterns escalate, not dismiss.** Anti-cascade rules + dismissal friction.

### Things still requiring human judgment

- **Whether a wedge is genuinely temporary vs structural.** The framework
  files `host-unreachable` deferred records; a human still decides whether
  to accept "host unreachable for 3 hours" as normal operation or
  investigate. The framework reduces the *noise* of investigation
  (one finding, full evidence, recurrence visible) but doesn't make
  the investigation itself.
- **What counts as a polling-shaped function name.** The lint heuristic
  catches obvious cases but the `# polling-bypass: <reason>` escape
  hatch is operator-judged at PR review.
- **Whether to back off vs continue when load is elevated.** The
  threshold default is 100 procs. Tuning is per-pod operator decision.

### Anti-pattern this incident exemplifies

**Per-test verification with no system-level awareness amplifies the
failures it's monitoring.** The dashboard, designed to monitor pod
health, was the dominant cause of the pod's degradation. The worker,
designed to file failures, generated more findings as the SUT got
worse — flooding the dismissal queue and hiding the structural signal.

The framework's response is to make system-level awareness a
first-class primitive (framework spec §4.5), not an
afterthought-checked-at-PR-review.

---

## Cross-references

- Meta-finding (resolved): [2026-04-25-011](../issues/opus-review/2026-04-25-011-meta-mini-ssh-wedge-recurring.md)
- Original dismissed findings: [004](../issues/dismissed/2026-04-25-004-d4-mini-ssh-timeout.md), [006](../issues/dismissed/2026-04-25-006-v2-admin-bot-ssh-timeout.md), [008](../issues/dismissed/2026-04-25-008-v4-ssh-timeout-mini-unreachable.md), [010](../issues/dismissed/2026-04-25-010-p1-budget-hawk-proposals-ssh-timeout.md)
- Framework spec: [§4.5 system-level awareness](spec-etr-phase-framework-2026-04-25.md)
- SKILL spec: [§5.5 structural awareness primitives](spec-etr-skill-v2-2026-04-25.md)
- Wrapper spec: [§9-§10 polling primitives at three layers](spec-etr-wrapper-v2-2026-04-25.md)
- Lint spec: [polling-bypass enforcement](spec-etr-lint-polling-bypass-2026-04-26.md)
- Standing test: [pod-health-invariants H5 (jitter detector)](verification/phase-4-tests/standing/pod-health-invariants.md)

---

## Outstanding follow-ups

1. **Watch the 01:00Z validation window.** If wedge watcher fires,
   we get a snapshot of what slipped through — reopen 011 if so.
2. **Move 011 to `archived/`** after the validation window passes
   silently. Keep the cross-references in the post-mortem stable.
3. **Update the four dismissed findings** (004, 006, 008, 010) with
   a back-reference to 011 and this post-mortem. Their
   `dismissal_reason` fields are now misleading; the back-reference
   makes the trail navigable.
4. **Extract `oc_status` primitives into `etr_polling/`** once the
   tactical PR #332 has soaked. Per Wrapper §10.4 implementation
   order step 2.
5. **Ship the polling-bypass lint in warning-only mode.** Identifies
   how much existing code needs to be retrofitted before the lint
   can promote to hard-fail.
6. **Calibrate the self-load threshold (default 100 procs).** The
   value is conservative; measure on a healthy steady-state and
   tune in `rig-config.yaml`.

These are tracked here, not as separate findings, because they're
follow-ups on a resolved incident — they don't fit the
catalog/finding loop. They could be migrated to `issues/phase-0-intake/`
as feature proposals if the operator wants tracking.
