# Using the META system (operator guide)

This is the human how-to for running Evolve's META development system day to day. It's the
plain-language companion to two reference docs you rarely need to open:

- [`META-session-guide.md`](META-session-guide.md) — the doctrine the *coordinator agents* follow.
- [`meta-ledger-schema.md`](meta-ledger-schema.md) — the data format behind the scenes.

You don't need either to operate the system. You need this page.

---

## The one idea

> **You drive decisions, not sessions.**

The old way: you opened a coordinator session per area of work, kept it alive, and babysat its
chips — checking each one, merging PRs, relaunching anything that stalled. With a dozen-plus areas
that became a full-time chore.

The new way: the system keeps a small, durable record of every area's live work (the **ledger**),
and two background "brains" keep that record current and auto-handle everything mechanical. You're
pulled in **only for genuine decisions**, which collect in one inbox you drain on your own
schedule. Nothing needs you in real time. Empty inbox = nothing needs you.

---

## Five words to know

- **Aspect** — a long-lived area of work with its own plan (e.g. `ui`, `deploy`, `rsi`, `apps`).
  There are ~12. An aspect is *not* a session — it outlives any session.
- **Coordinator** — a chat session where you *design* the next piece of work for one aspect. You
  open it, think, dispatch the work, and close it. Disposable.
- **Chip** — a background worker session that does one well-scoped task and opens a PR. Fire and
  forget; it reports back by landing its work, not by chatting.
- **Ledger** — a small file per aspect holding its live work-state (what's in flight, what's
  merged, what needs you). The system reads and writes these; you rarely touch them directly.
- **The queue** — your one cross-aspect inbox of everything that needs a human decision.

---

## The pieces at a glance

**Commands you type** (in any Claude Code session — the queue ones work in a throwaway session):

| Command | Use it to… |
|---|---|
| `/queue` | See everything that needs you, across all aspects. Your dashboard. |
| `/reconcile [aspect]` | Refresh the queue against live PR state *now* (don't wait for the timer). |
| `/coherence` | Check for cross-aspect trouble (overlap, collisions, mis-routed work) now. |
| `/launch` | Dispatch already-designed work as chips, or get the list of aspects to open. |
| `/prune` | Archive finished/idle sessions in one pass. |
| `/design "<work>"` | **The default way in.** Describe work in plain words → routes it to the right aspect and opens it. You don't pick the aspect. |
| `/meta <aspect>` | The shortcut when you *already know* the aspect (routing skipped). |
| `/status` | Mid-session pulse inside a coordinator (reconcile its own chips, drive them). |
| `/close` | Checkpoint a coordinator so it's safe to close. |
| `/help` | This quick reference — manual link, current aspect list, commands. |

**Brains that run on their own** (turn on in the Scheduled sidebar):

| Job | Cadence | Does |
|---|---|---|
| `meta-reconcile` | ~2h | Merges provably-safe PRs, relaunches stalled chips, pokes you for the rest. |
| `meta-coherence` | daily | Flags cross-aspect overlap/collisions/mis-routing into your queue. |
| `meta-fleet-watch` | — | **Legacy.** Superseded by `meta-reconcile`; leave it off. |

---

## A day in the life

**1. You get a poke (or you don't).**
The brains run quietly. They notify you *only* when something needs a decision — "META: 2 need
you — rsi green but unverified; diligence D6 decision pending." Silence means everything's handled.

**2. You drain the queue.**
Open any session, run **`/queue`**. You'll see a ranked list grouped by urgency, each with a
recommendation:

```
Queue as of 09:40 · run /reconcile for live
(B) Decisions
 1. [diligence] D6 has no force-run interface — build or defer?  → rec: defer to heal/watchdog
(C) Gates (your action)
 2. [ui] Gate-2: promote + kickstart, eyeball collapse live
 3. [apps] Gate-2: promote + re-scan a flagged bot
```

Act by number: `merge 1` · `snooze 2 monday` · `open 1` (turns this session into that
coordinator) · `dismiss 3`. Want the freshest view first? Run **`/reconcile`** — it sweeps live
state, then shows the queue.

**3. You want to push something forward.**
Just describe it: **`/design "the thing I want to work on"`**. It routes you to the right aspect and
opens a coordinator — *you don't pick the aspect* (it uses the ownership map + each aspect's
mission). Not sure it even fits an existing area? Describe it anyway: it routes to the best aspect,
proposes a *new* one only if genuinely distinct, or flags a merge. Inside, you discuss the change,
update the spec, and dispatch chips; then **`/close`** and walk away — the chips finish on their own
and the reconciler merges them when they're green and reviewed.

(Shortcuts: `/meta <aspect>` skips routing when you already know the home; `/launch` dispatches an
*already-designed* bite as a chip with no session at all.)

**4. The mechanical stuff happens without you.**
Between your touchpoints, `meta-reconcile` merges the safe PRs, relaunches anything that froze, and
keeps every ledger current. You never merge a routine PR by hand again.

**5. Clean up occasionally.**
Sessions pile up. Run **`/prune`** to archive finished ones (merged-PR chips, stale jobs, idle
coordinators) in one pass. Reopen anything later via `/meta <aspect>` — it rebuilds from durable
state in seconds.

That's the whole loop: **get poked → drain the queue → design when you want → let the rest run →
prune.**

---

## What happens automatically vs. what needs you

The line is **provably safe & reversible** (automatic) vs. **judgment or hard-to-undo** (you).

**Automatic (the reconciler does it, no session open):**
- Merge a PR that is green **and** passed its built-in review **and** is cheaply revertible.
- Relaunch a stalled chip from its last checkpoint (bounded).
- **Fix-forward** a PR that fails a *mechanical* gate (scrub / lint / format / a ratchet) or hits a
  merge conflict — auto-dispatch a bounded fix chip (rebase + fix + push). Only a real *test*
  failure (a bug) escalates to you.
- Keep every ledger's state current against live PRs.
- Flag cross-aspect trouble into your queue (the coherence pass).

**Needs you (lands in the queue):**
- **Designing** new work — the upstream conversation *is* your review (Gate 1).
- A change a reviewer flagged, or anything **not cheaply reversible** (sudoers, release pipeline,
  security infra).
- **Promoting to the live pod** — merge-to-main is automatic; pushing to the fleet is always yours.
- Choosing between real product directions.

You're never surprised: anything the system won't do itself shows up in `/queue` with a
recommendation.

---

## Working across many aspects (avoiding overlap)

As you add areas, two of them will eventually want to touch the same page. Three rules keep that
from turning into duplicated or conflicting work:

1. **Carve first — a page is not an aspect.** A *page* is a surface; an *aspect* is a body of work
   with its own plan. "I want to improve the Usage page" is usually a sub-track of an existing
   aspect, not a new one. Over-carving is what *creates* overlap. Keep the aspect count modest.
2. **Every surface has one content owner; `ui` always owns the look.** The Usage page's *numbers*
   belong to one aspect (`model-tiers`); its *styling* belongs to `ui`. So two coordinators can
   touch the same page without colliding — they own different layers. The full map is the "Surface
   ownership" table in [`META-session-guide.md`](META-session-guide.md).
3. **Out-of-lane work routes, it isn't silently done.** If a coordinator notices work that belongs
   to another aspect, it *deposits* a note into that aspect's queue instead of doing it.

And the safety net: **`meta-coherence`** runs daily and flags anything that slips through — the
same topic in two aspects, two PRs touching one file, or work sitting in the wrong aspect — into
your queue with a suggested fix.

---

## One rule that makes it all reliable: chips deposit, they don't report

A chip runs in its own session you never see. So a chip is only "done" when its result is in a
**durable place** — a PR, or a file it was told to write — never just "reported in the chat." This
is why a chip's work never evaporates when its session closes: the answer is on disk, and the
reconciler picks it up on its next pass. (You don't have to do anything for this; it's how chips
are briefed. Just know that's why you can close sessions freely.)

---

## First-time setup

A few toggles turn the autonomy on:

1. **Scheduled jobs — already registered.** `meta-reconcile` (every 2h) and `meta-coherence`
   (daily) are set up and on; `meta-fleet-watch` is disabled (the reconciler replaces it). One
   tip: click **"Run now"** on `meta-reconcile` once in the Scheduled sidebar — that pre-approves
   the tools it uses (`gh`, `spawn_task`) so future unattended runs never pause on a permission
   prompt.
2. **Settings → "Auto-archive on PR close"** → on. Finished chip sessions then clean themselves up
   (safe, because coordinators don't open their own PRs).
3. **Operator default model → Sonnet.** Chips inherit your default; Sonnet is the right tier for
   build work. Bump *your* design session to Opus when you're doing hard design.

Until the scheduled jobs are on, you can run everything by hand: `/reconcile` and `/coherence` do
the same work on demand.

---

## Monitoring the brains (are they alive?)

The two background brains are **scheduled tasks, not daemons** — they fire on a cron *while the app
is open*; they aren't persistent processes. So "is it alive?" isn't "is a process up," it's three
things: **enabled · firing on schedule · completing its run.**

- **Quickest check — `/queue`** ends with a one-line brains-liveness footer (reconcile/coherence
  last-ran + next, with a ⚠ and the fix if one is disabled, overdue, or fired-but-did-nothing).
- **Authoritative control — the Scheduled sidebar.** Shows each task's enabled state, last run, and
  next run; it's where you toggle on/off and click **"Run now."**
- **Did a run actually DO something?** The reconciler writes a heartbeat line per run to
  `~/.claude/meta-reconcile/log/<date>.jsonl`. A recent `lastRunAt` with an *empty* log means the run
  fired but didn't complete — almost always because it paused on a tool it wasn't pre-approved for.
  Fix: click **"Run now"** once and approve its `gh`/`spawn_task` prompts — approvals are remembered
  for future unattended runs.

**Restart / repair:**
- Re-run now → **"Run now"** in the sidebar, or `/reconcile` / `/coherence` (same logic, foreground).
- Got disabled → re-enable in the sidebar.
- Logic looks wrong → edit `~/.claude/scheduled-tasks/<id>/SKILL.md` (the file *is* the task's prompt).

**Caveat:** because they only run while the app is open, "hasn't run in N hours" can just mean the
app was closed — they catch up on next launch. Not a fault.

---

## Gotchas & quick fixes

- **`/prune` says "unavailable in unsupervised mode."** Archiving needs an interactive approval,
  which bypass/auto-permission sessions can't show. Run `/prune` from a normal session.
- **The queue looks stale.** `/queue` shows the last reconciled state (and stamps when). Run
  `/reconcile` for live.
- **A coordinator got archived but it wasn't finished.** No harm — reopen it from the Archived
  list, or just `/meta <aspect>`; it rebuilds the full picture from durable state in seconds. The
  only thing not saved is un-checkpointed in-chat reasoning, so `/close` before stepping away.
- **A chip froze (no new commits ~30 min).** The reconciler relaunches it from its last checkpoint
  automatically; after two tries it escalates to your queue.
- **A PR went red on CI, or a branch conflicts.** You don't have to prompt "fix the CI." If it's a
  *mechanical* gate (scrub, lint, format, a ratchet) or a merge conflict, the reconciler
  auto-dispatches a bounded fix chip (rebase + fix + push); only a *substantive* test failure (a
  real bug) reaches your queue. And chips pre-flight these gates locally before pushing, so most
  never go red in the first place.
- **You want to add a new area of work.** First ask whether it's really a new aspect or a sub-track
  / routing target (see "Working across many aspects"). If it's genuinely new, `/meta <new-id>`
  offers to scaffold it.

---

## One-screen cheat sheet

```
QUICK REFERENCE        /help
SEE WHAT NEEDS ME      /queue            (fresh: /reconcile)
CHECK FOR OVERLAP      /coherence
PUSH AN AREA           /launch  → dispatch ready bites, or open /meta <aspect>
DESIGN A CHANGE        /design "<the work>"   (routes you to the aspect) → … → /close
                       …or /meta <aspect> if you already know the home
CLEAN UP SESSIONS      /prune

RUNS ITSELF            meta-reconcile (~2h)  ·  meta-coherence (daily)
AUTO                   merge safe PRs · relaunch stalls · fix-forward mechanical CI/conflicts · keep state · flag overlap
ALWAYS ME              design · promote-to-fleet · risky/unreviewed merges · product calls
```

The system holds the state; you hold the judgment.
