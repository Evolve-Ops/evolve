---
title: "Help: Heartbeat Conditions — stop paying for idle ticks"
slug: heartbeat-conditions
audience: public
last_reviewed: 2026-09-07
concepts:
  - heartbeat-conditions
  - heartbeat-json
  - idle-burn
  - due-check
ui_surface: null
---

# Help: Heartbeat Conditions

A heartbeat is a bot waking up on a timer to ask "is there anything to do?".
Most of the time the answer is no — and answering "no" costs real money,
because the question is put to a language model that has to load the bot's
whole context to say it.

On a five-bot pod, heartbeats and cron ticks with nothing to do cost roughly
**$45 a month** — and that number grows with the number of bots and the size
of their context, not with the amount of work being done.

`HEARTBEAT.json` fixes that. It is a short file, written once per bot, that
says **what "something to do" actually means** for that bot in terms a
computer can check: a folder that isn't empty, a file that changed, a time of
day that has arrived. Evolve checks those conditions before the model is
woken. If nothing is due, the tick costs nothing at all — that is where the
saving comes from. If something is due, the bot wakes up and is told exactly
what, prefixed to its usual heartbeat instruction.

**Nothing changes until you write the file.** A bot with no `HEARTBEAT.json`
behaves exactly as it does today: every heartbeat wakes the model.

---

## Where the file goes

Next to the bot's `HEARTBEAT.md`, in its workspace:

```
/Users/<bot>/.openclaw/workspace/HEARTBEAT.json
```

`HEARTBEAT.md` stays exactly as it is. It is still what the bot reads and
follows when it wakes up. `HEARTBEAT.json` is the machine-checkable summary
of *when* that is worth doing.

A ready-to-edit starter ships with Evolve at
`packages/admin/evolve_admin/templates/bot_workspace/HEARTBEAT.json` — copy it
across, change the conditions to match that bot's checklist, and set
`"enabled": true`. It ships disabled, so copying it does not change anything
until you have read it.

---

## The shape of the file

```json
{
  "version": 1,
  "enabled": true,
  "conditions": [
    {
      "id": "inbox",
      "when": "dir_non_empty",
      "path": "inbox",
      "wake": "Process everything in inbox/ and clear it."
    },
    {
      "id": "morning-brief",
      "when": "time_window",
      "after": "06:30",
      "before": "07:30",
      "wake": "Send the morning brief."
    },
    {
      "id": "notes",
      "when": "file_changed",
      "path": "memory/notes.md",
      "wake": "Re-read memory/notes.md and act on anything new."
    },
    {
      "id": "sweep",
      "when": "every",
      "interval": "6h",
      "wake": "Run the six-hourly health sweep from HEARTBEAT.md."
    }
  ]
}
```

Every condition needs three things:

| Field | What it is |
|---|---|
| `id` | A short name, unique in the file. Used to remember when this condition last fired. |
| `when` | Which check to run — see the table below. |
| `wake` | What to tell the bot when *this* condition is what woke it. Write it as an instruction, not a label. |

`enabled: false` parks the whole file without deleting it — the bot goes back
to waking on every heartbeat.

---

## The checks you can use

| `when` | Fires when | Also needs |
|---|---|---|
| `dir_non_empty` | The folder exists and has at least one file in it (dotfiles ignored) | `path` |
| `path_exists` | The file or folder exists | `path` |
| `path_missing` | The file or folder is *not* there | `path` |
| `file_changed` | The file has been modified since this condition last fired | `path` |
| `time_window` | The local clock is inside the window, and this window hasn't been served yet today | `after`, `before` as `HH:MM` |
| `every` | At least this long has passed since this condition last fired | `interval` like `30m`, `6h`, `2d` |

`path` is always **relative to the bot's workspace**. An absolute path, or one
containing `..`, is refused — the file decides whether a turn runs, so it is
not allowed to point outside the bot's own workspace.

Time windows do not wrap around midnight: `after` must be earlier than
`before`. For "overnight", use two conditions.

---

## Cron jobs

**Your cron jobs keep running. `conditions` never applies to them.**

A cron job is not a heartbeat. A heartbeat asks "is there anything to do?";
a cron job already knows what it is for — "post the weekly digest", "check
the certificate". Gating one on the heartbeat's conditions would stop it
firing for reasons that have nothing to do with its job, so the list above
applies to heartbeats **only**.

If you do want a cron job to skip its own idle runs, give that job its own
conditions in a `cron` block, keyed by the job's id:

```json
{
  "version": 1,
  "enabled": true,
  "conditions": [
    { "id": "inbox", "when": "dir_non_empty", "path": "inbox",
      "wake": "Process everything in inbox/ and clear it." }
  ],
  "cron": {
    "9506f538-340e-4487-ae07-5675cb58b48c": [
      { "id": "to-send", "when": "dir_non_empty", "path": "outbox",
        "wake": "Send everything queued in outbox/." }
    ]
  }
}
```

That job now skips when its `outbox` is empty, and nothing else changes. A
cron job that is **not** named in the `cron` block runs every time, exactly as
it does today — which is what every cron job on your pod does until you add
one.

The key is the job's id, which is what the bot's gateway hands us for the
run. `openclaw cron list` prints the id next to each job's name.

The two scopes are kept apart all the way down: a cron run only ever reads
its own conditions, and waking for one never counts as having served the
other, even when the two use the same condition `id`.

---

## Turning your HEARTBEAT.md into conditions

Read your `HEARTBEAT.md` line by line and ask, for each instruction: *what
would the bot look at to decide whether this applies?* That thing is the
condition.

| A line in HEARTBEAT.md | The condition |
|---|---|
| "Check the inbox folder and deal with anything in it" | `dir_non_empty` on `inbox` |
| "Every morning around 7, send the brief" | `time_window` 06:30–07:30 |
| "If the task list has changed, re-plan" | `file_changed` on the task list |
| "Twice a day, check the backups are fresh" | `every` with `12h` |
| "If the alert flag file is there, escalate" | `path_exists` on the flag |

If a line has no answer — if the only way to know is to think about it — leave
it out. A condition you cannot express is not a reason to skip writing the
file; the lines you *can* express still remove most of the idle ticks, and
anything you leave out simply keeps waking the model as it does now, via an
`every` condition that matches the old cadence.

---

## What the bot sees when something is due

The woken turn is **prefixed** with just the due items:

```
[EVOLVE HEARTBEAT] Evolve checked this bot's HEARTBEAT.json conditions before
this turn. Exactly these are due right now — do them, and skip the rest of the
HEARTBEAT.md checklist:

- Process everything in inbox/ and clear it.  (inbox: inbox holds 3 entries)

Nothing else on the checklist is due. If none of the above needs a message,
reply with the single token NO_REPLY.
```

**This block is added to the bot's usual heartbeat prompt, not instead of it.**
The gateway's own heartbeat instruction — the one that tells the bot to follow
its heartbeat checklist — still arrives underneath, so a bot may still read
`HEARTBEAT.md` before doing the named item. The narrowing tells it what
matters; it does not yet remove the checklist read.

Replacing the prompt outright is a separate change: the gateway takes a
per-bot `heartbeat.prompt` setting, and swapping it for the due-items block on
bots that have a valid `HEARTBEAT.json` would remove the extra read. That is
deliberately not part of this: it writes to each bot's own gateway config,
which is a bigger blast radius than a file you can delete.

So the saving you can count on today is the **skipped** ticks — the ones that
never reach a model at all. A narrower woken turn is a bonus on top.

---

## When something goes wrong, the bot still wakes up

This is the one place in Evolve where an unclear situation resolves toward
*doing the work* rather than skipping it. A heartbeat that quietly stopped
firing is much worse than a heartbeat that fired unnecessarily — you would
not find out until something it was watching went wrong.

So the model runs as normal, exactly as it does today, whenever:

* there is no `HEARTBEAT.json` (the default for every bot);
* the file cannot be read, or isn't valid JSON;
* a condition names a check that doesn't exist, or is missing a field;
* the file says `enabled: false`, or lists no conditions;
* a condition has never fired before (the first tick after you write the file
  always wakes the bot).

A file that is present but broken is reported once in the bot's gateway log,
naming the problem — it never fails silently.

One limit worth knowing: a condition is marked "served" when the bot is woken,
not when the work finishes. If that turn fails outright — the model provider
is down, say — a `time_window` or `every` condition waits for its next slot,
and a `file_changed` misses that one edit (it fires again on the next edit).

### The floor: how long a bot may stay silent

`HEARTBEAT.json` lives in the bot's own workspace, so the bot can edit it —
and a file full of conditions that never fire looks exactly like a quiet week.
So there is a floor the file cannot lower: **if a bot has not been woken for
24 hours, the next tick wakes it whatever its conditions say**, and the
decision is recorded as `floor: no wake in 24h`.

The floor is set in the pod's `network.json`, which only you can edit — never
in a bot's workspace:

```json
{ "heartbeat": { "max_silence": "24h" } }
```

Per bot, `bots.<bot>.heartbeat.max_silence` wins over the pod value. `0` turns
the floor off. If the setting is missing or unreadable, the 24-hour default
applies.

Every decision also records the conditions file's fingerprint
(`conditions_sha256`), and if the file changes while the gateway is running,
that is noted once in the log — so a rewrite is something you can see rather
than something you infer from a bot going quiet.

**A message you send is never affected.** The check only ever applies to
timer-fired heartbeats and cron ticks. Asking your bot about heartbeats is
just a normal message.

---

## Checking that it is working

The weekly cost summary carries a line for it:

```
heartbeats: 6 run / 42 skipped over 7d — $1.26 saved (median $0.0300/run × 42)
```

Every decision — skipped and woken alike — is written to
`{shared_dir}/{bot}/turns/heartbeat-decisions-<date>.jsonl`, so the saving
shows up as a number rather than as an absence. If you ever suspect a
heartbeat has gone quiet, that file tells you whether it was skipped and why:

```json
{"ts":"2026-09-07T14:00:03.123Z","instance":"personal_bot","source":"heartbeat",
 "outcome":"skipped_nothing_due","cost":0,"conditions_evaluated":4,
 "cron_job_id":null,"conditions_sha256":"9f2c…",
 "reason":"nothing due (4 conditions checked)"}
```

`source` says whether it was a heartbeat or a cron tick, `cron_job_id` names
which job on a cron tick, and `conditions_sha256` fingerprints the file the
decision was made from.

---

## Related

* [Cost Optimization](cost-optimization.md) — the caps and controls around spend.
* [Model Economics](model-economics.md) — which model answers which kind of turn.
