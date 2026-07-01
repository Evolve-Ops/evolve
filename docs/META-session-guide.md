# META (coordinator) full reference

The **operating doctrine** every coordinator reads on session start — the one rule,
**Bootstrap**, **How a META session behaves**, the **Naming convention**, the
**session lifecycle** (`/meta` · `/status` · `/close`), the command quick-reference,
and cost discipline — lives in [`META-bootstrap.md`](META-bootstrap.md), the small
doc kept lean so opening a coordinator is cheap.

**This reference holds the on-demand lookups**, read when a bout needs them rather
than on every bootstrap: the **per-aspect parameters**, a pointer to the **Aspect
registry** (every aspect's row — now its own doc,
[`META-aspect-registry.md`](META-aspect-registry.md)), the **Surface-ownership** routing
map, the **scheduled
automation** (reconciler · fleet watcher · coherence) with its operator surfaces
(`/queue` · `/reconcile` · `/coherence` · `/prune` · `/launch`), and the **intake
router** (`/design`).

---

## Per-aspect parameters (what defines a specific META)

A kickoff instruction should supply these; if any are missing, ask:

- **Aspect** — the subsystem this META owns (e.g. "the model-tier system").
- **META ID** — the short, stable slug (`model-tiers`, `rsi`, …) used as
  `META:<id>` in this session's name and the `[META:<id>]` prefix on every chip it
  spawns. ≤10 chars; derive it from the aspect and register it in the Aspect registry
  ([`META-aspect-registry.md`](META-aspect-registry.md)).
- **Spec doc(s)** — the design source of truth to read on bootstrap.
- **Memory tag** — the project/feedback memory entries to recall.
- **Deploy mechanism** — how this aspect ships (e.g. model-tiers: admin-only =
  pull on the mini + kickstart `ai.evolve.evolve.admin-ui`; routing changes also
  need an evo-gateway kickstart).
- **Key invariants / guardrails** — the non-negotiables (e.g. model-tiers: `max`
  pull-only; no provider literals in logic; platform-path lint blocks new
  `/Users/` literals; rebase before each PR).

### Adding a new META (aspect)

Create an aspect when there's a *durable concern with its own spec + backlog +
invariants* — not speculatively, and **not** necessarily one-per-UI-page (a page is
a surface; an aspect is a body of work — they often won't be 1:1). An aspect with no
spec, memory, or work is pure overhead.

Protocol (the inverse of Bootstrap — populate the trio, then it's launchable):

1. **Pick a short stable id** — ≤10 chars, kebab, unique (`cost-opt`, `reporting`,
   `ai-opt`). Never reused or renamed once chips carry the `[META:<id>]` prefix.
2. **Add a registry row** ([`META-aspect-registry.md`](META-aspect-registry.md)) with the
   five per-aspect parameters above. Rows may start sparse — a one-line mission +
   "spec: TBD" is a valid seed.
3. **Seed the spec doc** — even a stub (`docs/spec-<aspect>-<date>.md`) stating the
   problem + the first decision. The spec is the design source of truth.
4. **Seed a memory entry** — mission + initial state; add its one-line index line
   (terse, ending in a `live → meta-state/<id>.json` pointer).
5. **Seed the structured ledger** — `meta-state/<id>.json` with `aspect`, `mission`,
   `spec`, `memory`, empty `chips`/`gates`/`decisions_pending`/`backlog`, and a first
   `next_action` (schema: `docs/meta-ledger-schema.md`).
6. Thereafter **`/meta <id>`** launches it.

`/meta <unknown-id>` offers to run this scaffold for you.

### Aspect registry

**The registry table now lives in its own doc:**
[`META-aspect-registry.md`](META-aspect-registry.md) — one row per aspect
(spec · memory · deploy · invariants/boundary). `/meta` `/status` `/design` resolve it
via `registry_path` in [`.claude/meta.json`](../.claude/meta.json) (default
`docs/META-aspect-registry.md`), so a project that adopts the substrate reads its **own**
registry, not Evolve's. Read *your* aspect's row there on Bootstrap; the columns and the
new-aspect protocol are defined above (§ "Adding a new META (aspect)").

### Surface ownership (the routing map)

Two coordinators touching the same page — a `ui` bout and a "Usage" bout both editing the Usage
page — is the failure mode overlap creates. The fix is three rules, in order:

**1. Carve first — a surface is not an aspect.** A *page* is a surface; an *aspect* is a body of
work with its own spec + backlog + invariants. "I want to improve the Usage page" is almost always
a **sub-track of an existing aspect or a routing target**, not a new META. Over-carving *creates*
the overlap. Prefer a dozen-ish broad aspects with sub-tracks over many narrow ones.

**2. Every surface has ONE content owner; `ui` always co-owns presentation.** `ui` owns
*presentation* (tokens, primitives, lint, instruction-text standard) across **all** pages; each
page's **content truth** (is the copy/data right? what shows?) has exactly one owning aspect. So a
Usage-page change splits cleanly: the *look* is `ui`'s, the *numbers* are the content owner's.

**3. Out-of-lane work routes — it is not silently done.** A coordinator that finds work outside
its content-ownership **deposits** it into the owner's ledger (`backlog` / `decisions_pending`
with a "routed from `<id>`" note) and keeps only its own layer's slice — the same shape as the
multi-pod "deposit is the only remote verb". The cross-aspect coherence pass + the watcher's
collision check catch leaks.

| Surface / page | Content owner | Notes (`ui` co-owns presentation everywhere) |
|---|---|---|
| Chat / Evo tray ("ask evo…") | `evo-asst` | the assistant's reasoning/context/identity; `ui` co-owns the tray's look |
| Plugins | `skills` | |
| Apps / Gallery | `apps` | |
| Reports / Alerts / Health | `reports` | signal-producer quality + tab UX |
| Proposals / Improvements | `rsi` | generation quality; `reports` owns the alert side |
| Subscriptions | `reports` | |
| Model Economics / Usage / cost | `model-tiers` | usage = cost/economics; `apps` feeds per-app rows by hand-off; per-bot is the cost unit |
| Add-bot wizard | `user-value` | |
| Bot detail / tiles | `user-value` | the bot's value journey |
| Users (per-bot roster / roles / approvals) | `users` | who the users are + their rights + admin↔bot store coherence; `user-value` owns the *value* journey, not the *user* model |
| Deploy / Release / promote | `deploy` | |
| Pods | `multi-pod` | |
| Settings → invasiveness / footprint posture dial | `footprint` | the posture-control contract + the mutate-vs-observe catalog + the safe-disable engine; `ui` co-owns the Settings presentation; each toggle's *implementation* routes to the owning subsystem aspect |
| tokens / primitives / lint / instruction-text | `ui` | the presentation layer itself |

(Ratified 2026-06-14: standalone "Usage" → `model-tiers`; bot detail / tiles → `user-value`.)

---

## The unattended system (scheduled automation + operator surfaces)

The coordinator lifecycle (`/meta` · `/status` · `/close`) lives in
[`META-bootstrap.md`](META-bootstrap.md); these are the *session-less* mechanisms
that drive the safe work between operator touchpoints — the scheduled reconciler and
its predecessor watcher, the cross-aspect coherence pass, and the `/design` intake
router — plus the operator surfaces (`/queue` · `/reconcile` · `/coherence` ·
`/prune` · `/launch`) that expose them.

### The scheduled reconciler (`meta-reconcile`)

`/status` is the operator-invoked return-pulse; **`meta-reconcile` is the same pulse run
unattended on a schedule** — the lever that lets the operator drive *decisions, not sessions*.
Every couple of hours it reads every aspect's ledger, reconciles each chip against `gh`, and:

- **Auto-drives the provably-safe zone** (the mechanical rule, `docs/meta-ledger-schema.md`):
  merges green + two-pass-PASS + reversible PRs (poll JSON, merge manually — never `--auto`),
  **holding any `operator_merge: true` chip for the operator** even when it is green+PASS+reversible,
  and relaunches stalled chips from their checkpoint (bounded ≤2, then escalates). It writes the
  reconciled `bucket`/`pr`/`last_commit` back to the ledger.
- **Fix-forwards mechanical failures.** A PR red on a *mechanical* gate (scrub / lint / format / a
  file-size or other ratchet) or in *merge conflict* gets a bounded auto-dispatched fix chip
  (rebase on `origin/main`, run the failing gate locally, fix, push) — ≤2 attempts, then escalate.
  A *substantive* failure (a real behaviour / unit test) escalates to the queue immediately, never
  auto-patched. This is what keeps a lint-red or stale PR from needing the operator to say "fix
  the CI."
- **Pokes the operator only for the red zone** — held merges (CONCERNS/FAIL, not-reversible,
  `operator_merge:true` while green+PASS, or green-but-unverified), dead chips past the relaunch
  cap, merged-privileged-without-verdict,
  `decisions_pending` forks, cross-aspect collisions, and orphan PRs. Edge-triggered: silence
  means nothing needs you; a clean auto-merge is logged, not poked.
- **Never** sets a two-pass verdict, dispatches a *new* bite, advances a roadmap item, or merges
  anything failing the rule — those stay human-greenlit. Per-aspect `autonomy: "observe"` in a
  ledger dials that aspect down to reconcile-and-poke-only.

Lives: `docs/meta-reconcile-procedure.md` (version-controlled behavior, mirrored to
`~/.claude/scheduled-tasks/meta-reconcile/SKILL.md` — the copy the scheduler runs), the "Scheduled"
sidebar (cadence / on-off), `~/.claude/meta-reconcile/last-seen.json` (state) + `…/log/` (audit trail).
When enabled it **supersedes the fleet watcher's poke** below.

**Operator surfaces (both global skills):** **`/queue`** renders the red zone as one
cross-aspect inbox from any session — act by number (approve / merge / snooze / open / dismiss).
The queue is a *computed projection* of the ledgers (`decisions_pending` + rule-held chips +
operator gates), not a separate store, so it is always current and there is nothing to keep in
sync. **`/reconcile [<id>]`** runs the sweep on demand (foreground + report + show the queue)
instead of waiting for the ~2h timer. Neither lives in the Evolve admin UI — the meta-dev system
stays operator-local, off the shipped product surface.

**Session lifecycle.** With the reconciler + queue you keep **~0 coordinators open** — the
mechanical work is session-less, so a coordinator opens only to *design*. **`/prune`** archives
finished/idle sessions in one pass (merged-PR chips, stale scheduled-task runs, checkpointed
coordinators — `list_sessions` → `archive_session`, per-session confirm); enabling **"Auto-archive
on PR close"** in Settings auto-handles the merged-PR bulk. **`/launch`** lists which aspects to
re-open and the `/meta <id>` for each — opening the session itself stays an operator action (no
tool spawns one; `/queue` → `open #N` turns the current scratch session into one aspect's
coordinator). Net: you are not maintaining a stable of live sessions — you `/prune` the pile and
`/launch` (or `/meta`) only what has design work.

### The fleet watcher (`meta-fleet-watch`)

The observe-only predecessor (superseded by `meta-reconcile`'s richer pulse when that's
enabled; keep one of the two running, not both). So you don't poll each META with "what's
next?", a cheap, edge-triggered watcher runs hourly and pokes the operator **only on state
changes**:

- **Lives:** `~/.claude/scheduled-tasks/meta-fleet-watch/SKILL.md` (behavior), the
  "Scheduled" sidebar (cadence / on-off), `~/.claude/meta-watch/last-seen.json`
  (edge memory).
- **Does:** scans open `claude/*` PRs and pokes (desktop + phone) only on → READY
  (mergeable) / → MERGED / → BLOCKED (a check failed) / → STALLED (no activity
  ~30 min ≈ dead chip). Silent runs are normal. **Report-only** — never merges or
  spawns.
- **Reset:** delete `last-seen.json` for a quiet re-baseline. **Force a run:** "Run
  now" in the Scheduled sidebar.
- **v2 (future opt-in):** wake a specific META via `send_message` on completion,
  for aspects marked "auto-resume" — privileged actions (release-pipeline merges,
  promotions) stay human-greenlit.


### The cross-aspect coherence pass (`meta-coherence`)

The ownership map *prevents* most overlap; the coherence pass *catches* the leaks. It is the
across-aspects sibling of the reconciler (which works within one aspect) and it **reads +
recommends only** — never routes, merges, or reassigns. Daily is plenty.

Each run it reads every ledger + this ownership map + open `claude/*` PRs and flags four things:
**duplicate / overlapping work** (the same surface or topic in two aspects' chips or backlog),
**cross-aspect PR collisions** (two open PRs from different aspects touching one file — deeper and
aspect-aware vs. the watcher's check), **mis-routed items** (a chip/backlog item whose surface
belongs to a different aspect per the map), and **within-aspect duplicates** (two non-terminal chips
in the *same* aspect doing the same work — the after-the-fact safety net for the dispatch-time
`tools/meta-inflight` check, since the reconciler's per-chip status pass never compares two chips).
Each finding is posted to the owning aspect's
`decisions_pending` with `source: "coherence"` and a recommendation, so it surfaces in `/queue`;
new findings trigger one edge-triggered poke. Lives:
`docs/meta-coherence-procedure.md` (version-controlled behavior, mirrored to
`~/.claude/scheduled-tasks/meta-coherence/SKILL.md` — the copy the scheduler runs) + the "Scheduled"
sidebar (cadence) + `~/.claude/meta-coherence/last-seen.json` (state). On demand: **`/coherence`**
(foreground + report). Resolve findings from `/queue` like any other decision — approve the route,
coordinate the PRs, or dismiss.

### Intake routing (`/design`)

At 20+ aspects, making the operator name the right one is friction and a mis-routing risk. So the
default front door is **`/design "<what I want to work on>"`**: the operator describes the work in
plain language and the router sends it to the right home using this ownership map + the aspects'
missions/backlogs. Outcomes: **clear fit** → open that coordinator (with a one-word override
offered); **spans layers** → design in the primary content owner, route the presentation slice to
`ui`; **ambiguous** → ask with a recommendation (a true 50/50 is itself a signal the two aspects
overlap); **no fit** → carve-first check, then propose a *new* aspect only if genuinely distinct
(default: fit an existing one); **redundancy** → flag a possible merge (a deliberate operator op —
`[META:<id>]` ids are sticky — never merge unasked). The router is the **gatekeeper against aspect
proliferation**: it biases hard toward routing into an existing aspect, so the taxonomy stays a
background concern and the operator spends attention on design, not on remembering who owns what.
`/meta <aspect>` remains the explicit shortcut when the home is known; bare `/meta` (or an
unrecognized argument) routes too.

Extend this table when a new surface appears. A surface with no clear owner is a prompt to *route
or carve* — not to spin up a coordinator on the spot.

`/design` also ingests a **GitHub issue** (`/design <#N | issue-url>`): it fetches the normalized
record via `tools/meta-issue` and triages from the issue's title/body/comments identically to free
text (spec `docs/spec-substrate-2026-06-15.md` §4.2).

#### Aspect-label convention

Two repo issue-label forms feed the issue-intake routing prior (generalizing the original
`edr:agent-able`):

- **`<aspect>`** — the issue belongs to that aspect. `<aspect>` MUST match a registry id (the
  Aspect registry, [`META-aspect-registry.md`](META-aspect-registry.md)). It is a *routing prior*: `/design`'s content classification stays
  authoritative and overrides a stale label.
- **`<aspect>:agent-able`** — a human judged the issue dispatchable by an agent; the body must
  carry a `Proof:` line (a falsifiable acceptance test). `tools/meta-issue` surfaces this as
  `agent_able` + `proof`.

`tools/meta-issue` exposes the leading `<aspect>` of any such label as `aspect_hints[]`. After
routing, the recommended write-back tags the issue `meta:routed:<aspect>` so the tracker reflects
the triage outcome. (Optional/stretch: auto-create the aspect labels from the registry with
`gh label create <aspect>` — not yet automated.)
