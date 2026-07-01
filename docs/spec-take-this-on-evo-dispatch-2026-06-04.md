# spec: "Take this on" → evo dispatch — 2026-06-04

Status: draft.

## Background

The 2026-06-04 review of the Recommendations queue surfaced a recurring
operator pattern: a finding shows up, the operator clicks **Take this
on**, the proposal moves to **In Process**, and then nothing happens.
The proposal sits there waiting for the operator to do the work
offline and click **Mark Complete**.

The operator's direct words:

> You click "Take this on" and it moves it to another bucket, but
> nothing gets accomplished.
> ...
> This is Evolve spotting a problem evolve created. And worst of all,
> there is no "do this to fix it" or "press this button and I will
> take care of it."

That's the gap this spec closes. Today the system has three action
families:

| Action kind | What "Take this on" does |
|---|---|
| `ConfigPatch` / `TierAdjustment` / `UpsertCronJob` / etc. (auto-apply) | Server applies the change directly. Proposal moves to applied. Works. |
| `BuildApp` (external completion) | Forge spawns a session, builds the app, reports back. Works. |
| `Investigation` / `WorkflowInstruction` / `AddSignalCollection` (manual completion) | Proposal moves to In Process. Operator does the work. **Gap.** |

The manual-completion family is where the gap lives. For findings
where Evolve already *knows* what should happen (because Evolve
emitted them), "Take this on" should hand the fix to evo (or the
relevant bot) instead of waiting on the operator.

## Principle

> A finding with a known remediation should never make the operator
> the executor. Either:
>
> (a) the generator emits an *auto-apply* action shape (Slice 2's job
>     — `ConfigPatch`, `UpsertCronJob`, etc.), and "Apply" runs it
>     directly; OR
>
> (b) the action is an `Investigation` because the fix needs a
>     reasoning step, but the generator names a *dispatch target*
>     (evo, or a specific bot, or forge) and Evolve hands the
>     finding off in one click; OR
>
> (c) the action is genuinely operator-only (security review, social
>     judgment, schema migration). The body includes copy-paste-ready
>     bot instructions so even then the operator isn't drafting from
>     scratch.

The operator's role downshifts to **approve / confirm / dismiss**.
The executor is evo, a bot, or the auto-applier — not the operator.

## What this spec covers

The path from `Investigation` proposal to "evo applies the fix."
Specifically:

1. A new optional field on `Proposal`: `dispatch_target`.
2. Generator-side conventions for when to set it.
3. Server-side endpoint that spawns the target's session.
4. Client-side UI changes on the proposal card + modal.
5. Result-handling flow: how evo's outcome reports back to the
   proposal store.
6. Safety: idempotency, retry, failure modes, audit logging.

## Non-goals

- **Auto-applying ConfigPatch / UpsertCronJob from audit_poller** —
  that's Slice 2, runs in parallel with this spec.
- **Replacing forge's BuildApp flow** — BuildApp already does the
  right thing (external completion); this spec extends the model to
  the manual-completion kinds, not the external ones.
- **Operator-only proposals** — the "tell the operator what to type"
  shape (case (c) in the principle) is covered by Slice 2's title +
  body humanization pass. This spec assumes that work and focuses
  on the dispatch flow.
- **General-purpose evo task queue** — this isn't a new evo
  capability. It uses evo's existing MCP `action.proposal.*` surface
  and session-start mechanism.

## Schema additions

### `Proposal.dispatch_target`

```python
@dataclass
class Proposal:
    # ... existing fields ...

    # Slice 3 (2026-06-04): the bot/agent that can resolve this
    # Investigation in one operator click. None means the operator
    # has to do the work themselves (current behavior).
    #
    # Allowed values:
    #   "evo"            — dispatch to the pod's evo bot. Used for
    #                      cross-bot or pod-wide investigations.
    #   "<bot_id>"       — dispatch to a specific bot (e.g. the bot
    #                      whose app the finding is about).
    #   "forge"          — dispatch to forge for app-shape work.
    #   None             — operator-only. "Take this on" behaves
    #                      as today.
    dispatch_target: str | None = None

    # Slice 3: when dispatch_target is set, this is the message the
    # target receives. Generator writes this at emission time so the
    # operator sees what will be sent before clicking "Have evo fix
    # this." If None, server generates a default from the proposal's
    # title + problem body.
    dispatch_message: str | None = None
```

### Status transitions

The existing status machine gets one new state, **`dispatched`**:

```
pending → dispatched   (operator clicked "Have evo fix this")
dispatched → applied   (target reported success)
dispatched → failed    (target reported failure or didn't report
                        within the timeout)
dispatched → in_process (target asked the operator to take over —
                        cleanly hands back, no state lost)
```

`dispatched` is a sibling of `applied` and `in_process`. Proposals in
this state render with a "Dispatched to evo at <time>" badge and a
"Cancel dispatch" button. The status auto-transitions to one of the
terminal states when the target reports back.

## Server-side: new endpoint

### `POST /api/arbiter/proposals/<id>/dispatch`

Mirrors the existing `/act` endpoint but for the dispatch flow.

Request body:
```json
{
  "target": "evo",       // optional override of proposal.dispatch_target
  "message": "...",      // optional override of proposal.dispatch_message
}
```

Behavior:
1. Load the proposal. 404 if not found.
2. Reject if the proposal's status isn't `pending`.
3. Reject if `dispatch_target` is None and the request didn't
   override.
4. Use the existing session-start mechanism for the target:
   - `evo` → spawn an evo session via the existing evo-runner socket
     (the same one `evo revise` and friends use).
   - `<bot_id>` → use the bot's gateway HTTP API to start a session
     with the dispatch message.
   - `forge` → enqueue a forge job (same as `BuildApp`).
5. Transition proposal status to `dispatched`. Record the
   dispatch_at timestamp, the resolved target, and the session id
   from step 4 in `proposal.dispatch_state`.
6. Return the updated proposal view.

The handler is idempotent on dispatch state: if the proposal is
already `dispatched`, return its current state instead of
re-dispatching. Prevents double-fire on a double-click.

### `POST /api/arbiter/proposals/<id>/dispatch/result`

The target reports its outcome here. Called by:
- evo's MCP tool `action.proposal.report_dispatch_result`.
- Bots' gateway, when the dispatched session completes.

Request body:
```json
{
  "outcome": "applied|failed|in_process",
  "message": "...",       // free-form explanation
  "applied_changes": [...]  // optional structured patch summary
}
```

Behavior:
1. Verify caller (mTLS + session token).
2. Reject if the proposal isn't in `dispatched` state.
3. Apply the status transition. Record the result in
   `proposal.dispatch_state.result`.
4. Append to the proposal history.
5. Optionally notify the operator via the existing alerts dispatcher
   (subscription-shaped — they'll see it on whichever channel they've
   wired up).

### `POST /api/arbiter/proposals/<id>/dispatch/cancel`

Operator cancellation path. Transitions `dispatched → pending`. Best-
effort cancels the target session — but the source of truth is the
proposal state; if the session already applied changes before the
cancel, the operator sees the changes anyway via the next
`/result` callback.

## Generator-side conventions

When emitting an `Investigation` proposal, the generator decides:

1. **Is this auto-applicable?** If yes, emit a non-Investigation
   action kind (ConfigPatch / UpsertCronJob / etc.). That's Slice 2's
   territory.
2. **Otherwise: who knows how to fix this?**
   - If the fix lives inside one bot's workspace → `dispatch_target =
     <bot_id>`. The bot has the context, the credentials, and the
     scope.
   - If the fix is cross-bot or pod-wide → `dispatch_target = "evo"`.
     Evo can act on multiple bots and has the pod-wide view.
   - If the fix is app-shape (new manifest, new script, install) →
     `dispatch_target = "forge"`.
   - If reasoning needs a human (security review, social judgment,
     schema migration) → `dispatch_target = None`. Operator does it.

3. **Write `dispatch_message`** as the instruction the target
   receives. Be specific:

   ```
   GOOD: "Investigate the gap between ea-pack's manifest claim
   (success_criteria.observable_outcomes[3] — commitments
   surfaced as follow-ups within 24h) and the actual
   evening_sweep.py implementation. Either patch
   evening_sweep.py to call commitment_tracker.py --list-due
   and surface the output, or remove the manifest claim if
   the feature isn't intended. Reference: proposal id
   {proposal_id}."

   BAD: "Fix ea-pack."
   ```

   The message is sent verbatim. Operator sees it in the proposal
   modal before clicking "Have evo fix this."

### audit_poller mapping (motivating example)

For the `app_audit_tier3` proposals currently on the test pod, the
mapping at v1:

| Audit category | Action kind | dispatch_target |
|---|---|---|
| `broken_path` | `ConfigPatch` (Slice 2) | n/a — auto-apply |
| `missing_functionality` (cron derivable from manifest) | `UpsertCronJob` (Slice 2) | n/a — auto-apply |
| `missing_functionality` (genuine code gap) | `Investigation` | `<bot_id>` |
| `behavior_mismatch` | `Investigation` | `<bot_id>` |
| `dead_code` | `Investigation` | `<bot_id>` (or `None` if subjective) |
| `drift` (TAG_ALIASES, persona-style) | `Investigation` | `None` (operator judgment) |
| `manifest_drift` (cron name mismatch) | `ConfigPatch` (Slice 2) | n/a — auto-apply |

Slice 2 handles the auto-apply rows. Slice 3 handles the dispatchable
rows. Operator-only rows stay as today (but with Slice 2's
plainer titles + copy-paste bot instructions in the body).

## Client-side: UI changes

### Proposal card

When `dispatch_target` is set, the "Take this on" button becomes:

- `dispatch_target == "evo"`        → **Have evo fix this**
- `dispatch_target == "<bot_id>"`   → **Send to {bot_id}**
- `dispatch_target == "forge"`      → **Have forge handle this**

(Investigation proposals with `dispatch_target == None` keep the
current "Take this on" button and current behavior.)

### Pre-dispatch confirmation modal

Click triggers a small confirmation modal:

```
┌─ Send this to evo? ────────────────────────────────────┐
│                                                         │
│  Evo will receive:                                      │
│                                                         │
│  > Investigate the gap between ea-pack's manifest      │
│  > claim and evening_sweep.py implementation. Either    │
│  > patch the sweep or remove the manifest claim.        │
│                                                         │
│  Evo will report back when done. You can cancel from   │
│  the proposal at any time.                              │
│                                                         │
│              [ Cancel ]    [ Send to evo ]              │
└─────────────────────────────────────────────────────────┘
```

The dispatch_message is shown verbatim so the operator can adjust
their expectations before sending.

### Dispatched state

The proposal card shows:

```
⚙ Dispatched to evo · 14 min ago · [ Cancel dispatch ]
```

The operator can poll for the result by refreshing, or wait for the
alerts dispatcher notification on their messaging channel.

### Result rendering

On `dispatch/result` callback:

- `outcome == "applied"` → proposal renders the standard "applied,
  awaiting verify" state.
- `outcome == "failed"` → proposal renders with a red banner and the
  failure message. "Retry" + "Take this over manually" + "Dismiss"
  buttons.
- `outcome == "in_process"` → target asked operator to take over.
  Proposal renders in the existing In Process state with the
  target's notes prepended to the body.

## Safety + invariants

1. **One dispatch in flight per proposal.** The endpoint rejects
   re-dispatch on a proposal already in `dispatched` state. Prevents
   double-fire.
2. **Idempotent target session.** When the target starts the session,
   it includes the proposal id in its turn context. If the target
   ever sees the same proposal twice (network retry, multi-dispatch
   races), it should detect via the proposal's current status before
   acting.
3. **Auto-cancel on stale dispatches.** A scheduled job sweeps
   proposals in `dispatched` state older than 24 h and transitions
   them to `failed` with reason "target didn't report in 24h." Stops
   ghosts.
4. **Operator can always cancel.** The Cancel button transitions the
   proposal back to `pending`. Target sees the status change on its
   next read and aborts if not yet applied.
5. **Audit log.** Every dispatch + result + cancel writes to the
   existing admin-actions JSONL log so the post-hoc review surface
   has receipts.
6. **No dispatch on archived proposals.** Endpoint 4xxs if proposal
   isn't in `pending`.

## Out of scope for v1

- **Mid-flight progress updates from the target.** The target only
  reports at the end. v1.x can add a `dispatch/progress` endpoint
  if operator feedback wants it.
- **Operator-edited dispatch_message.** v1 sends the
  generator-written message verbatim. v1.x can add an "edit before
  sending" textarea in the confirmation modal.
- **Bulk dispatch.** Each proposal dispatches individually. The
  PR #1 coalescer's "Take all" button doesn't yet apply to
  Investigation-with-dispatch; operator confirms each.
- **Cross-target dispatch.** If evo fails, the proposal doesn't
  auto-redispatch to a different target. Operator picks the next
  action manually.

## Implementation phases

- **Phase 3.1 — Schema + server endpoints.**
  Add `Proposal.dispatch_target` / `dispatch_message` /
  `dispatch_state`. Add the three endpoints. Add the
  `dispatched` status to the state machine. No UI changes yet;
  generators can start setting the field, but the operator can't
  see it.

- **Phase 3.2 — Client UI.**
  Card-side button label change + confirmation modal +
  dispatched-state badge + result rendering. Existing
  `arbiterAct` handler routes to `/dispatch` when
  `dispatch_target` is set.

- **Phase 3.3 — Evo MCP tool.**
  `action.proposal.report_dispatch_result` exposed on evo's MCP
  surface. Evo's prompt includes the dispatch instructions when it
  receives a dispatched-proposal turn.

- **Phase 3.4 — audit_poller mapping.**
  Per the table in §"audit_poller mapping" — populate
  `dispatch_target` on the Investigation-class emissions.
  Coordinate timing with Slice 2 (the auto-apply rows shouldn't be
  Investigation anymore by the time this lands).

- **Phase 3.5 — Stale-dispatch sweep.**
  Cron job that scans `dispatched` proposals older than 24h and
  fails them with the timeout reason.

Each phase ships independently. The order matters: 3.1 unblocks 3.2
and 3.4 in parallel. 3.5 can land last.

## Open questions

1. **What's the right dispatch target for a forge-emitted finding?**
   The audit caught what forge built wrong. Should dispatch go back
   to forge ("retry the install with this patch") or to evo ("read
   the audit, decide whether forge needs a code change")? Lean
   forge-retry for mechanical fixes, evo for "did forge get it wrong
   or did the operator change requirements." This needs operator
   input once Phase 3.4 lands.

2. **How does the operator know dispatch succeeded without watching
   the UI?** Default: alerts-dispatcher notification on their
   messaging channel ("Evo applied X for ea-pack"). Subscribable like
   other event types. Or do we want this always-on?

3. **What's the dispatch_message budget?** Long enough to be
   specific, short enough that evo's context isn't burned on it.
   Lean: cap at 800 chars; if the generator's natural output is
   longer, truncate with "...see proposal {id} for full context"
   and rely on evo to pull the full proposal via MCP.

4. **Should `dispatched` count as `In Process` for the Inbox split?**
   Today In Process is its own subtab. `dispatched` could go either
   in the Inbox (the operator is waiting on evo, not on themselves)
   or In Process (the proposal is "out for execution"). Lean: own
   small section between Inbox and In Process: "Dispatched (awaiting
   target)."

## Related

- [docs/spec-recommendations-rework-2026-06-02.md](spec-recommendations-rework-2026-06-02.md)
  — the rework parent
- `project_evo_account_separation` (memory) — evo runs as its own
  macOS user with limited write paths; the dispatch endpoint uses
  the existing admin-daemon socket
- `project_evo_oc_native_architecture` (memory) — evo is the OC bot
  supercharged by Evolve scaffolding; dispatching to evo extends the
  existing scaffolding rather than adding new infrastructure
- `feedback_each_bot_applies_its_own_changes` (memory) — per-bot
  dispatch (`dispatch_target = "<bot_id>"`) preserves this invariant
- `docs/spec-app-audit-2026-05-16.md` — the audit generator whose
  output motivates this spec
