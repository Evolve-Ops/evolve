# Multi-instance integration aggregation — design pass

**Status:** proposed (awaiting approval)
**Date:** 2026-05-05
**Survey data:** none — single-pod access only at design time. See "Survey: what we couldn't survey" below.
**Predecessor:** [integration-discovery.md](integration-discovery.md), Q4 ("Multi-instance operator surface", deferred at Phase 2 acceptance).

This is the design pass to answer Q4 from the integration-discovery doc. Within-a-pod
discovery is now robust and pluggable (probes architecture, decisions A and B,
PRs #721, #722, #723, #724, #729, #732, #738). The next surface is multi-instance:
"across N OpenClaw pods, here is your integration state." This doc proposes how to
collect, store, and surface that aggregated state.

## Proposed decisions

Two foundational calls. Both are open for review; the rest of the design is
dependent on them.

**A. Push from pod to central, not pull and not cloud-bucket.** Each pod's
evolve user generates a periodic snapshot of its probe results and posts it to
a central aggregator service (a new component: `evolve-central`). The pod's
admin-ui stays loopback-only. The central is the one externally-reachable
thing in the system. Auth is a per-pod bearer token issued by central.

Alternatives considered:
- *Pull* (central reaches into each pod's admin-ui): requires opening up the
  pod's admin-ui beyond loopback, which significantly expands attack surface
  on every pod. Reject.
- *Cloud bucket* (each pod writes to S3/GCS, central reads): adds an external
  dependency and credential to every pod. Privacy: paths and account emails
  end up in a third-party store. Reject for Phase 1; revisit at Phase 4 if
  scale demands it.
- *Hybrid* (local probe → snapshot file in `/Users/Shared/evolve/`, central
  reads via shared filesystem): requires shared-filesystem topology that the
  Evolve pod model doesn't presume. Reject.

Push wins because (a) pods stay loopback, (b) there is exactly one externally
reachable node (central) instead of N, (c) auth is one boundary, (d) a missing
or stale snapshot gracefully degrades to "we last heard from pod X N hours ago"
rather than a hard failure.

**B. Pod-local is source of truth; central is a cache + index.** No
multi-instance UI action ever writes directly to a pod's filesystem from
central. Every action that mutates a pod's state goes through the pod's own
admin-ui (Phase 3, separate design pass). The central can *suggest* a sweep
("12 pods are on legacy `~/.config/gws/`"), but the operator clicks through to
each pod's local UI to execute the action.

This is the multi-instance analog of decision B from the previous design ("no
affordance may break a working integration"): aggregation must never
desynchronize central's view from pod truth. The simplest way to enforce that
is to make central read-only with respect to pod state.

## Operating principle: snapshot age is data, not gating

The central never gates UI on snapshot freshness. A pod that hasn't pushed in
two days still appears in every view, with its last-known state and its
`pushed_at` timestamp surfaced as a chip. Operators decide whether stale data
is actionable. This is the multi-instance analog of "positive evidence
required": the central surfaces what it knows and never silently hides
information.

Concretely: a pod that's offline for a week still shows up in the sweep view
("team-bot-c's pod: ranch-team-bot-c: Slack token_pair, last seen 8 days ago"). The
operator can choose to act on it or not; the central does not infer
"unreachable means uninstalled."

## Why this exists

The integration-discovery design landed within-a-pod aggregation: the Integrations
& Keys page now correctly surfaces every Slack/Workspace/GitHub credential under
its appropriate flavor, with affordances safe for the storage shape it found.

Within a single pod that's enough. For Evolve's actual stated direction — managing
*countless* OpenClaw instances — it isn't. The probe data model already supports
cross-instance aggregation in principle (every `ProbeResult` carries
`bot_id`, `flavor`, `storage_locations`, etc.; aggregating across instances is
just a different `iter` over the same data). The plumbing question is: how do
N pods' worth of probe results get into one place where one operator can act
on them?

This document proposes that mechanism: a push-based snapshot model with a
central aggregator, three operator-facing views (sweep, instance summary,
drift report), and a phased migration that follows the same incremental
shape as the integration-discovery rollout.

## Survey: what we couldn't survey

The previous design pass ran a structured survey across six bots in the live
pod. For multi-instance, the analog would have been a survey across multiple
pods. **That wasn't possible:** at design time, only the live pod is
accessible.

Implications:
- The data shapes the design assumes (per-pod variance, integration drift,
  network.json variance) are extrapolated from prior knowledge of how the
  pod has changed over time, not from comparison across pods.
- The estimated scale (~12 pods near-term, up to ~100 long-term) is from the
  user's stated target, not measured.
- The privacy-leakage analysis (what's in a snapshot) is grounded in the
  probe module's exact outputs, which are already known and bounded.
- After Phase 1 ships and at least 3 pods are reporting, a real survey can
  validate the assumed variance and tighten Phase 2's UI priorities.

This is a known weakness of the design. It does not block Phase 1 (snapshot
collection is well-defined regardless of variance), but it should temper
confidence in Phase 2 UI specifics until real cross-pod data exists.

## Operator scenarios

The user delegated prioritization. This design recommends the following
ranking based on what the probe model is best suited to surface:

1. **Sweep (highest priority).** "Provider × flavor × pod" view: pick a
   provider, see every pod where that provider matched and which flavor
   won. *Why first:* this is the closest fit to the existing probe outputs,
   needs no inferred-baseline notion, and directly answers the most concrete
   question ("which pods are on the legacy CLI?"). It's also the smallest
   useful UI — one screen per provider.

2. **Instance summary (second priority).** Dashboard of dashboards: each pod
   gets a card showing its bots, integrations active, integrations needing
   attention, and last-snapshot age. Click through to drill into a pod's
   local dashboard. *Why second:* this is operator triage, but it requires
   no comparative logic — the data is just per-pod facts, displayed
   side-by-side. Cheap to build once snapshot collection exists.

3. **Drift report (lowest priority — defer).** "Cross-pod diff against a
   baseline." *Why last:* there is no canonical baseline today. The user
   confirmed they don't have one. Drift detection requires either a manually
   designated baseline pod (operator overhead) or an inferred "majority
   flavor" baseline (which can be misleading when 2 of 3 pods are
   misconfigured). Defer until at least Phase 2 has been shipped and
   operators have opinions about what drift they actually want flagged.

The triage scenario ("operator says Slack stopped — show me everywhere
Slack is configured") falls out of the sweep view: the same provider-scoped
grid answers it directly. No separate UI required.

## Topology: push-based snapshot collection

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│   Pod A      │   │   Pod B      │   │   Pod N      │
│              │   │              │   │              │
│ admin-ui     │   │ admin-ui     │   │ admin-ui     │
│ (loopback)   │   │ (loopback)   │   │ (loopback)   │
│      │       │   │      │       │   │      │       │
│      ▼       │   │      ▼       │   │      ▼       │
│ snapshot.py  │   │ snapshot.py  │   │ snapshot.py  │
│      │       │   │      │       │   │      │       │
└──────┼───────┘   └──────┼───────┘   └──────┼───────┘
       │                  │                  │
       │   HTTPS POST     │                  │
       │   bearer token   │                  │
       └─────────┬────────┴──────────────────┘
                 │
                 ▼
       ┌─────────────────────────┐
       │  evolve-central         │
       │                         │
       │  /api/snapshot (push)   │
       │  /api/instances (read)  │
       │  /api/sweep/<provider>  │
       │                         │
       │  storage: snapshots/    │
       │           <instance_id> │
       │           /<ts>.json    │
       └─────────────────────────┘
                 ▲
                 │ HTTPS (web UI)
                 │
       ┌─────────────────────────┐
       │  Evolve Admin (Layer 3) │
       │  one human operator     │
       └─────────────────────────┘
```

### Snapshot generation (pod side)

A new module `packages/admin/evolve_admin/web/snapshot.py` runs the existing
probes against every bot in `network.json`, collects results, and assembles
a single JSON document. The collection logic reuses the existing
`build_probes()` registry and `ProbeContext` plumbing — no new probe code,
no parallel discovery path.

A new `evolve.snapshot` cron job (15-minute cadence by default; configurable
in `network.json` → `multiInstance.pushIntervalSeconds`) generates the
snapshot and POSTs it to the configured central URL with the bearer token.
On failure (network, auth, central down), the job retries with exponential
backoff up to a max of 1 hour, then waits for the next scheduled run.

A snapshot is at most ~50KB for a 7-bot pod (probe results are small structured
records). At 15-minute cadence over 30 days that's about 2,880 snapshots per
pod, or ~145MB on central. This is comfortable for filesystem storage.

### Central aggregator (`evolve-central`)

A separate service that runs *outside* any pod. Most likely deployed on the
same Mac mini as one of the pods initially, but architecturally distinct
(different process, different bind address). For Phase 1 it can run as a
LaunchDaemon under the evolve user on whichever host the operator chooses.

Endpoints:
- `POST /api/snapshot` — accepts a snapshot push. Auth: `Authorization:
  Bearer <pod-token>`. Token resolves to `instance_id`; snapshot is written
  to `snapshots/<instance_id>/<timestamp>.json`. Returns 200 with the
  resolved `instance_id` on success.
- `GET /api/instances` — list registered instances with their last-push age.
- `GET /api/instances/<instance_id>/latest` — most recent snapshot for one
  pod.
- `GET /api/sweep/<provider>` — flatten across all instances; return
  `[{instance_id, bot_id, probe_result}, ...]` filtered to the requested
  provider.

The web UI for the central is a Phase 2 deliverable; Phase 1 ships only the
collection pipeline and the read APIs.

### Why not run central as part of an existing pod's admin-ui?

Two reasons. First, scope: the central is observing pods, including
potentially the pod that hosts it. Mixing the observed and the observer
creates blast-radius problems — a pod outage would take the central with it.
Second, identity: a pod's admin-ui is scoped to that pod's primary user
(per the three-layer pod architecture in product-vision.md). The central is
scoped to the Evolve Admin (Layer 3) operator, who may not be the primary
user of any individual pod. Keeping them as separate processes lets the
auth model on each be independently right.

## Data model

### Snapshot shape

```json
{
  "schema_version": 1,
  "instance_id": "uuid-generated-on-first-push",
  "instance_slug": "operator-friendly-name",
  "captured_at": "2026-05-05T14:23:01Z",
  "evolve_version": "git-sha-or-tag",
  "network": {
    "network_id": "my-network",
    "bot_count": 7,
    "bots": [
      {"id": "team-bot-a", "role": "member", "system_user": "team-bot-a"},
      ...
    ]
  },
  "probe_results": [
    {
      "bot_id": "team-bot-a",
      "provider": "google_workspace",
      "winner": {"probe_name": "legacy_oc_gws_cli", "flavor": "legacy CLI", "...": "..."},
      "evidence": [/* all probes that matched */],
      "warnings": []
    },
    ...
  ]
}
```

The `probe_results` array is the same JSON the keys API already returns to
the within-pod dashboard, aggregated across all bots. Reuse the renderer's
output verbatim — no new shape.

### Instance identity

- **`instance_id`** — UUID4 generated on first push by central, stored
  back into the pod's `network.json` under
  `multiInstance.instanceId` for stability across restarts.
- **`instance_slug`** — operator-supplied display name, also in
  `network.json`. Defaults to the hostname.
- **Authoritative source for instance lifecycle:** central decides when an
  instance "goes away." Default policy: `>30 days silent` → archived in
  central UI (still visible, marked); `>90 days silent` → snapshot history
  retained but instance hidden from default views. Explicit teardown via
  central UI button (Phase 2). Configurable.

### Snapshot retention

- Most recent snapshot: always retained.
- Last 30 days: retained.
- Beyond 30 days: pruned to one snapshot per day for 90 days.
- Beyond 90 days: pruned to one snapshot per week, indefinitely.

Why retain history at all: drift reports (Phase 3) need a baseline. Sweep
operations need to be able to answer "this pod has been on the legacy CLI
for 6 months" — that requires history. Storage cost is modest (~100MB per
pod per year at the prune schedule above).

### Privacy: what's in a snapshot, what's not

What snapshots contain:
- Bot IDs (`team-bot-a`, `team-bot-c`, `team-bot-b`) — already public per network.json
- System usernames (`personal-bot-user` for team-bot-b) — public per network.json
- Account emails from probe results (e.g. Google Workspace
  `granted_services` and `google_account`) — these are the operator-relevant
  emails, exposed in the within-pod dashboard already
- Storage paths (`~/.config/gws/`, `~/.openclaw/workspace/credentials/`) —
  needed for sweep operations
- Masked tokens (`sk-ant-•••••••abc123`) — masking happens at probe time;
  raw tokens never enter the snapshot

What snapshots do **not** contain:
- Raw secrets of any kind. The probe layer is the masking boundary;
  snapshot generation reads only the probe-layer output, never the
  underlying files.
- `.env` file values. The DotenvProbe returns names only; that's what
  ends up in the snapshot.
- Conversation logs, agent_end records, or any user-facing content. This
  design is exclusively about integration metadata.
- Model API responses, prompts, or analytics data. Out of scope.

The decision to *not redact* paths and emails is deliberate: redaction
would defeat the sweep use case. The operator needs to know that
`~/.config/gws/` is the legacy location on pod-A and `~/.openclaw/workspace/credentials/`
is the plugin location on pod-B in order to plan a migration.

The trade-off is access control on the central. Snapshots are PII-ish (paths,
emails) and require auth in front of every read endpoint. See "Auth" below.

## UI surface

Three views, prioritized (sweep first, instance summary second, drift
deferred). Sketches in ASCII; final wireframes when Phase 2 starts.

### Sweep view (Phase 2)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Provider:  [Google Workspace ▾]                                         │
│                                                                          │
│  Across 12 instances · 5 wizard · 4 legacy CLI · 1 plugin · 2 absent    │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────┐       │
│  │ pod-pod-admin-user   │ team-bot-a    │ legacy CLI       │ creds.json  ⚠   │       │
│  │ pod-pod-admin-user   │ admin-bot  │ legacy CLI       │ creds.enc       │       │
│  │ pod-pod-admin-user   │ team-bot-c  │ plugin (workspc) │ token.json + sa │       │
│  │ pod-northsea  │ alpha  │ wizard           │ auth-prof       │       │
│  │ pod-northsea  │ bravo  │ — (not config'd) │                 │       │
│  │ pod-eastside  │ delta  │ legacy CLI ⚠     │ token_cache.… ⚠ │       │
│  │   (8 days since last push)                                  │       │
│  │ ...                                                         │       │
│  └──────────────────────────────────────────────────────────────┘       │
│                                                                          │
│  [ Filter: legacy CLI only ]    [ Group by flavor ]   [ Export CSV ]   │
└──────────────────────────────────────────────────────────────────────────┘
```

Click any row → opens that pod's local admin-ui in a new tab (deep-link to
the bot+provider). All actions still execute against the pod-local UI;
central is never on the write path.

The `⚠` icon flags either:
- A probe ERROR warning carried in the snapshot (sudo failure, malformed
  JSON), surfaced exactly as it appears in the within-pod UI today.
- A stale-snapshot indicator (last push >24h ago).

### Instance summary view (Phase 2)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Instances                                                               │
│                                                                          │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐         │
│  │ pod-pod-admin-user     │  │ pod-northsea    │  │ pod-eastside    │         │
│  │ 7 bots          │  │ 3 bots          │  │ 4 bots          │         │
│  │ 21 integrations │  │  8 integrations │  │ 11 integrations │         │
│  │  3 ⚠ warnings   │  │  0              │  │  1 ⚠ + stale 8d │         │
│  │                 │  │                 │  │                 │         │
│  │ pushed: 12m ago │  │ pushed: 4m ago  │  │ pushed: 8d ago  │         │
│  │ [Open dashboard]│  │ [Open dashboard]│  │ [Open dashboard]│         │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘         │
└──────────────────────────────────────────────────────────────────────────┘
```

### Drift report (deferred)

Sketch only; not building this in Phase 1 or Phase 2. The operator picks a
baseline pod (or central infers "majority flavor" per provider), and the
report shows where each other pod differs. Defer until at least 5 real pods
are reporting and the operator has a felt sense of what drift is worth
flagging vs. what's just intentional variance.

## Authentication / authorization

Two boundaries:

**Push side (pod → central).** Pod sends `Authorization: Bearer <pod-token>`
on every push. Token is issued by central (manual operator step in Phase 1:
the operator generates a token in the central UI, copies it into the
pod's `network.json` under `multiInstance.pushToken`). Token resolves to
`instance_id` server-side. Token revocation: central can invalidate a token,
which causes the pod's pushes to fail; central UI flags the pod as
"unauthenticated, last seen X."

**Read side (operator → central).** The Evolve Admin is a single-tenant
operator. Phase 1 punts auth onto the deployment topology: bind the central
to localhost-only, require the operator to access it via SSH tunnel
(`ssh -L 5051:localhost:5051 ...`). This is the same trust model as the
existing pod admin-ui (loopback + SSH-as-auth). It's not great long-term
but it's not novel — and it lets Phase 1 ship without inventing a new auth
system.

**Phase 4: real auth.** A central reachable from the public internet would
need actual user-facing auth. This design does not propose that auth system;
it explicitly defers it. The recommendation is: do not expose central
externally until that design has been done.

This is consistent with the prompt's instruction to reference existing auth
or note its absence. The existing auth model in Evolve is "loopback +
filesystem-permission + the operator has SSH access" — fine for Phase 1,
needs replacement before any external exposure.

## Migration plan

Three phases, each independently shippable. The first lays foundations; the
second turns them into a usable UI; the third adds cross-instance write
actions (separate design pass).

### Phase 1 — Snapshot collection & central scaffolding

- **Pod side:** new `evolve_admin.web.snapshot` module that runs the existing
  probe registry over every bot in network.json and produces a snapshot
  JSON. New `evolve.snapshot.push` cron job (15-min cadence default) that
  POSTs the snapshot to the configured central URL.
- **Central side:** new `evolve-central` service with three endpoints:
  `POST /api/snapshot`, `GET /api/instances`, `GET /api/instances/<id>/latest`.
  Filesystem storage at `central/snapshots/<instance_id>/<ts>.json`.
- **Config plumbing:** `network.json.multiInstance` block:
  - `enabled: bool`
  - `centralUrl: string`
  - `pushToken: string`
  - `pushIntervalSeconds: int (default 900)`
  - `instanceId: uuid` (populated on first successful push)
  - `instanceSlug: string` (default = hostname)
- **No UI on central.** Phase 1 is about getting data flowing.
- **Verification:** the live pod can push to a local-dev central; central
  has 1 instance; `GET /api/instances/<id>/latest` returns a parseable
  snapshot.

### Phase 2 — Read-only multi-instance UI

- **Sweep view:** Provider × instances × bots grid with flavor and storage
  chips. Filter by flavor. Export CSV. Click-through to pod-local dashboard
  (deep-link).
- **Instance summary view:** card-per-pod with bot/integration/warning
  counts and last-push age. Card click opens pod-local dashboard.
- **Stale-snapshot UI:** chip on every row/card showing snapshot age;
  ≥24h is highlighted, ≥7d is dimmed.
- **Provider scope:** start with the providers the within-pod dashboard
  already covers (workspace, slack, telegram, github, the LLM keys).
- **Verification:** with 2-3 pods reporting, sweep view correctly reflects
  flavor distribution and click-through to each pod-local UI works.

### Phase 3 — Cross-instance write actions (separate design pass)

This is its own design pass — auth model, audit trail, partial-failure
handling, undo semantics all need their own thinking. **Out of scope for
this doc.** Likely shape: operator selects N pods in a sweep view, central
fans out a request to each pod's admin-ui (which still applies its own
within-pod safety checks), and central reports per-pod success/failure.
Write the design doc for this when Phase 2 is shipped and there's at least
one concrete sweep operation operators are explicitly asking to do.

## Resolved questions

(None — first pass.)

## Open questions

**MQ1 — Where does central run?** Recommendation: same Mac mini as the
"home" pod (the operator's primary), as a separate LaunchDaemon under the
evolve user, on a distinct port (5051). This keeps Phase 1 deployment
trivial. A long-term question is whether central should ever move to a
non-pod host (a dedicated VM, a cloud server, etc.). Defer.

**MQ2 — Snapshot schema versioning.** The `schema_version: 1` field is in
the snapshot but no upgrade path is specified. When Phase 4 adds new probe
fields, central needs to handle both old and new pods. Recommendation:
central treats schema_version as opaque metadata; the renderer is
schema-tolerant (missing fields are missing chips). Codify this as central
matures.

**MQ3 — Push failure observability.** A pod whose pushes are silently
failing (token expired, central unreachable) will look "stale" to central
but the operator on that pod won't know unless they check. Recommendation:
the pod-local dashboard surfaces last-successful-push and last-failed-push
in a small footer chip, mirroring the central's stale-snapshot UI. Phase 2
work; tracked here so we don't forget.

**MQ4 — Multi-tenancy.** This design assumes one Evolve Admin observing N
pods that they own. A multi-tenant SaaS turn (different operators, different
fleets, central as shared infrastructure) is a different product and is
explicitly out of scope. If that becomes a goal, this design is a
foundation but not a finished system — auth would need to be redone.

**MQ5 — Phase 3 affordances.** What cross-instance write actions are
worth building first? The operator's intuition is the answer; this design
defers it. The strongest candidate from the integration-discovery work is
"sweep N pods from legacy CLI to wizard for Google Workspace" — but
prioritize based on what feels acute once Phase 2 has been live for a few
weeks.

## Recommendation

Ship Phase 1 first. It's the smallest unit that makes the data exist; once
the data exists, Phase 2 is mechanical. Don't build any UI on central
until at least 2 pods are reporting (the live pod + a test pod) — single-
pod aggregation views are not actually useful and would mislead Phase 2
prioritization.

Order:
1. **Phase 1 — Snapshot collection.** Pod-side snapshot module + cron;
   central scaffolding + push endpoint + read APIs. ~2 weeks of work
   including tests.
2. **Run with 1 reporting pod for ~1 week.** Validate snapshot shape,
   schema, retention. No UI yet.
3. **Bring up a second pod** (the user has plans for this; check before
   committing dates).
4. **Phase 2 — Sweep + instance summary UI.** ~1-2 weeks of frontend work.
   Defer drift report.
5. **Phase 3 design pass** (cross-instance writes) — separate doc, kicked
   off after Phase 2 has been operator-tested for at least 2 weeks.

## What's NOT in this design

- **No new probe types.** This design is purely an aggregation layer over
  the existing probes. Probe model unchanged; new probe additions follow
  the existing path in the integration-discovery design.
- **No multi-tenant SaaS architecture.** Single-Evolve-Admin observing N
  pods. Multi-operator fleet management is a different product.
- **No external auth system for central.** Phase 1 uses loopback +
  SSH-as-auth, same as today's admin-ui. Public-internet-exposable central
  is a separate design pass.
- **No within-pod changes to probes or affordances.** All decisions A and
  B from the integration-discovery doc continue to hold; this design
  consumes their outputs without modifying them.
- **No real-time push.** Snapshots are polled at a configurable interval.
  Sub-minute push is unnecessary for the operator scenarios this design
  serves and would invite live-event-stream complexity.
- **No write actions from central.** Phase 3 covers writes; that's its own
  design pass with its own auth/audit treatment.
- **No schema for "incident" or "alert" cross-pod surfaces.** Aggregation
  is for integration *state*, not for alerting on pod-level events. The
  alerting story already exists per-pod (alerts.channel in network.json);
  cross-pod alert routing would be a separate design.
