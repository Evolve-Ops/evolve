# U2 watcher proof — event-triggered gallery watcher → real delivery (2026-06-12)

Roadmap: `docs/roadmap-user-value-2026-06-10.md` Phase **U2** — "one
event-triggered watcher running from a gallery template."
Spec: `docs/spec-proactive-delivery-monitor-2026-06-10.md`.
Cross-spec decision (roadmap §8, Round-1 #2): **watchers ride `schedules[]`
(poll, 1–15 min); no new push trigger sources.** A "watcher" = a gallery app
on a poll schedule that delivers a message when a real condition matches.

Subject bot: **`ledger`** (disposable launch-ops PM bot, M4 proof).

## Result: ✅ DELIVERED

| Stage | Status |
|-------|--------|
| Watcher installed from gallery (forge-built) | ⚠️ **built, not formally registered** — files materialized, manifest blocked at `status: updating` by a coherence-gate false-positive (Finding 1); poll cycle run via the watcher's own cron entrypoint |
| Watcher polls + detects real condition | ✅ **YES** — read Task Manager registry, flagged the overdue item |
| Watcher composes delivery message | ✅ **YES** |
| Watcher resolves delivery route | ✅ **YES** (after Finding 2 fix) — `telegram:<primary-user-chat-id>` |
| Watcher invokes `openclaw message send` | ✅ **YES** — real (non-dry-run) send, exit 0 |
| **Message actually delivered to operator** | ✅ **YES** — after the operator `/start`ed the bot (Finding 3); `SWEEP_SENT`, audit `delivery.status: sent`, run-file written |

**Bottom line: a poll-based gallery watcher fired end-to-end and delivered a real
message to the operator's Telegram on a real condition.** First send attempt was
rejected (`chat not found` — DM not yet open); the operator sent `/start` to the
bot, the watcher was re-run, and the send succeeded.

### Delivery evidence (the successful run)

```
$ evening_sweep.py run --bot ledger
… Overdue (1): U2 watcher proof: launch runbook sign-off (overdue test item) …
SWEEP_SENT: 2026-06-12 completed=0 overdue=1 open=0 followups=1   (exit 0)
```

- Audit `memory/evening-sweep-last.json`:
  `{"date":"2026-06-12","sent_at":"2026-06-12T09:47:14-07:00",
  "delivery":{"channel":"telegram","status":"sent"}}`
- Run-file `memory/sweep-runs/2026-06-12.json` (`{date, sent_at}`) — written **only**
  on a successful channel send (exit 0), per the gallery-delivery convention.

## Gate (delivery readiness)

- ledger has `telegram` enabled (`openclaw.json plugins.entries.telegram` +
  `channels.telegram.enabled`).
- `network.json bots.ledger.primary_user.external_ids.telegram` is populated.
- `openclaw message send … --dry-run` (run as ledger) resolves the route
  `telegram:<primary-user-chat-id>`, `via: direct`.

The config-level gate passes. It is **not sufficient** for delivery — see Finding 3.

## Watcher template

**Evening Sweep** (`gallery/evening-sweep/`, `pkg_id p-1d3e8f47`) — chosen because:

- Its required source is the **Task Manager** registry (`tasks.json`), which
  ledger has installed (M4 starter pack). The pure-watcher templates
  (`calendar_watch`, `pre-meeting-brief`) need a `calendar` integration ledger
  does not have, so they were ruled out — exactly as the task anticipated.
- It is **conditional**: emits `SWEEP_SENT:` when there are overdue/open/completed
  items, `SWEEP_EMPTY:` when nothing — i.e. delivers only when a condition matches.
- It is a `user_facing: true` launchd scheduled action delivering to the primary
  channel via `openclaw message send` (the gallery-delivery convention,
  `docs/spec-gallery-delivery-convention-2026-06-11.md`).
- Commitment Tracker / Task Manager themselves were rejected: post-heartbeat-retirement
  they are cache + session-greet only (no proactive poll-delivery).

Installed via the **official gallery pipeline** (`POST /api/gallery/p-1d3e8f47/install`,
`force:true` to bypass a stale `task-manager: updating` manifest strand). The admin
API is device-pairing + CSRF gated; driven over loopback with a device token minted
as the `evolve` service user (holder of the pod admin-auth key — equivalent to pairing).
Forge job `j-0c7a3549`.

## The triggering condition

A real overdue task added to ledger's Task Manager:

```
unified_task_system.py add \
  --title "U2 watcher proof: launch runbook sign-off (overdue test item)" \
  --due 2026-06-10 --priority high
# → UN-0001  (due 2 days before today 2026-06-12 → overdue, status open)
```

## What the watcher produced

Real run (`evening_sweep.py run --bot ledger`, no `--dry-run`), composed message:

```
Evening sweep — Friday, June 12.

No tasks marked complete today.

Overdue (1):
  - U2 watcher proof: launch runbook sign-off (overdue test item)

Commitments due (1):
  - <placeholder-contact> | Follow up on invoice [due: 2020-01-01]
```

(The "Commitments due" line is the optional Contacts fold-in — proves the
dependency-data path too.)

Then:

```
SWEEP_FAILED: 2026-06-12 delivery-error   (exit 2)
```

The underlying `openclaw message send` returned:

```
OutboundDeliveryError: Telegram send failed: chat not found
(chat_id=<primary-user-chat-id>). Likely: bot not started in DM, …
```

`getMe` → token valid (`ok: true`). `getUpdates` → **0 updates** (no one has ever
messaged the bot). A wrong token returns 401 *unauthorized*, not *chat not found* —
so the token is fine; the DM is simply unopened.

## Evidence paths (on the live pod)

- Watcher script: `/Users/ledger/.openclaw/workspace/scripts/evening_sweep.py`
  (+ `evening-sweep-cron.sh`), forge-built by job `j-0c7a3549`.
- Audit record: `/Users/ledger/.openclaw/workspace/memory/evening-sweep-last.json`.
- Task registry: `/Users/ledger/.openclaw/workspace/tasks.json` (UN-0001).
- Run log: `/tmp/ledger-evening-sweep.log`.
- delivery_monitor: **no `on_time` row** for ledger (the app never finalized to
  `active`, and the real send failed). The shared delivery ledger is
  `/Users/Shared/evolve/delivery_monitor/ledger/<date>.jsonl` (note: "ledger" there
  is the record-book name, holding all bots' rows keyed by `bot_id`).

## How the delivery was completed

1. First real run → `SWEEP_FAILED: delivery-error` (Telegram `chat not found`).
2. Operator sent `/start` to ledger's bot (`@…_bot`, name "Ledger").
3. Re-ran `evening_sweep.py run --bot ledger` → `SWEEP_SENT:` (exit 0) → message
   landed in the operator's Telegram.

The watcher required no change between steps 1 and 3 — only the DM had to exist.
(`getUpdates` still showed 0 because ledger's live telegram plugin long-polls and
consumes updates; the send succeeding is the authoritative proof the DM is open.)

## Findings (worth fixing upstream)

1. **Coherence gate blocks installing any app that reads a dependency app's data
   file.** Forge job `j-0c7a3549` failed step 10:
   `MAJOR C-A2: scheduled_action 'evening-sweep-daily' declares input 'tasks.json'
   not in files[] or volatile_paths[]`. `tasks.json` is owned by the Task Manager
   `app_dependency`, not by Evening Sweep. **Fix:** declare cross-app inputs in the
   gallery manifest's `volatile_paths[]`, or have the coherence checker treat a
   scheduled_action input that matches a declared `app_dependency`'s output as
   satisfied. Until then, every dependency-reading watcher fails to formally install
   (files still materialize, but status stays `updating`).

2. **Forge-built watcher read the bot-local `network.json` instead of the shared
   pod `network.json`.** `load_network(bot_id)` only searched
   `/Users/<bot>/.openclaw/{,workspace/}network.json` (neither exists); the route
   lives in `{shared_dir}/network.json`. Result: `SWEEP_SKIPPED: no-delivery-route`.
   The build's critique passes (which should catch this) had failed `rc=1`. Corrected
   on the live bot for this proof (prefer evolve-plugin `sharedDir`, then the
   canonical default). **Fix:** the gallery build_spec should name the canonical
   shared `network.json` path (or reference the convention's `load_network` helper).

3. **"Pairing" (token paste) ≠ "deliverable".** The U1 gate / delivery-readiness
   check verifies config (`external_ids ∩ enabled channels`), but a Telegram bot
   cannot DM a user who has not started it. **This is the same blocker (B) the
   #2774 briefing delivery re-proof hit independently the same day** (see
   `[[project_add_bot_m4_u1_proof]]`): the operator's recorded
   `primary_user.external_ids.telegram` is their id from the primary/evo bot DM, not
   a DM with ledger's own bot — which is why the #2707 activation receipt reached
   them but app/watcher sends do not. **Fix:** the delivery-readiness probe should
   confirm an open DM (non-empty `getUpdates`, or a successful real send) before
   declaring a bot deliverable, and the add-bot wizard should instruct the operator
   to `/start` the new bot, not only paste its token.

### Note on delivery-path correctness vs. the morning briefing

The #2774 re-proof found ledger's **morning-briefing** app still POSTs to the
**removed `/api/message`** endpoint (→ HTTP 404) — the `openclaw message send`
migration (#2695) never reached the gallery briefing build_spec. **Evening Sweep
does not have that bug**: it uses `openclaw message send` (the current convention)
and got all the way to a real Telegram API call. So once blocker (B) is cleared,
this watcher delivers with no further code change — whereas the briefing still needs
its build_spec migrated (filed as chip `task_504a5fc0`).
