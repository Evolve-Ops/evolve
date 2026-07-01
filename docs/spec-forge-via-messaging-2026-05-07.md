# Forge via Messaging — Architecture (2026-05-07)

Status: **shipped (5b7a + 5b7b)**. Companion to [spec-evo-wizard-2026-05-05.md](spec-evo-wizard-2026-05-05.md); concretizes the design contract for the wizard's `FORGE_INTRO` / `FORGE_DESIGN` / `FORGE_CONFIRM` phases (slice 5b7) and for any future in-chat forge surface. Deferred items in §9 (manifest sharing, push notifications, revise/cancel) remain future work.

**What this is.** Forge today is built around the admin-UI flow — the operator opens the dashboard, sees a code diff, clicks approve, the build applies. That model breaks down when the same flow has to happen inside a Telegram or Slack chat: pasting hundreds of lines of code into a DM is hostile, and the wizard surface that motivated this spec doesn't have a "switch to dashboard" affordance for users running `evo wizard` from their phone. This spec proposes a different shape for forge when it's driven from messaging — split design from build, locate approval at the design stage instead of the diff stage, and use a conversation as the alignment loop rather than a giant document review.

**Relationship to other specs.**
- [spec-evo-wizard-2026-05-05.md](spec-evo-wizard-2026-05-05.md) — the wizard surface. §4.2 phase 7 deferred forge integration; this spec is the prerequisite for unblocking it.
- [manifest-spec.md](manifest-spec.md) — current app manifest schema. This spec proposes one new field (`conversational_summary`) to make the design-phase artifact durable.
- [applications.md](applications.md) — gallery + forge UX in the dashboard. The dashboard flow continues to exist; this spec adds a parallel messaging flow that produces the same kind of artifact.

---

## 1. Goals and non-goals

**Goals.**

1. A user running the wizard (or a future `evo forge` command) can author and build a custom app without leaving chat.
2. The approval moment is a conversation about *what the app is*, not a code review.
3. Once the user and bot agree on a design, code generation happens autonomously — the user does not see or approve a diff.
4. After the build completes, the bot proactively DMs the user with: a "done" signal, usage instructions, and a recap of what was built.
5. Failures DM the user too — never silent.
6. `evo` has cheap visibility into in-flight forge state for the calling user/bot — at minimum, "you have a build in progress, ready in ~N min."

**Non-goals.**

1. Removing the dashboard's existing forge UX. The dashboard surface stays for operators who prefer it.
2. Bypassing forge's existing critique/test phases for code correctness. Those still run; they're a quality gate on the autonomously-generated code, not a gate on user intent.
3. Pasting code or diffs into chat. The user's window into the build is the conversational summary plus optional MD download.
4. Building the same app on multiple bots simultaneously. Apps are bot-specific in v1. Manifest sharing between bots is sketched in §7 but deferred.
5. Streaming live build progress into chat ("step 3 of 5..."). Wrap-and-notify is a cleaner UX than progress chatter.

---

## 2. The architectural shift: two-phase forge

Today's forge runs a single pipeline: **Build → Critique × 3 → Test → Gate → Apply**. The Gate is where the operator reviews a diff. That conflates two distinct decisions:
- **What should the app do?** (intent / design)
- **Did we generate it correctly?** (correctness / code review)

In a dashboard those can be folded together — looking at a diff, an experienced operator decides both. In chat they can't. So this spec splits them:

```
PHASE A (design, conversational):
  bot ↔ user dialogue → finalized manifest
                        + conversational_summary
                                |
                                ▼
PHASE B (build, mechanical):
  manifest → code generation → critique × 3 → test
                                                |
                                                ▼
              build complete (or failed) → bot-initiated DM to user
```

**Approval lives at the boundary between A and B**, when the user says "yes, build this" — same shape as `evo guide`'s save/cancel/edit gate from #786. After that point, code generation is autonomous: critique and test rounds run, code lands at `bots/<bot_id>/apps/<app_id>/`, no further human approval. Critique-and-test is doing *correctness* work, not *intent* work; it doesn't need a human in the loop.

This is a deliberate choice. It trades "operator catches a weird code generation choice at the diff" for "operator agreed on the design and trusts the autonomous critique loop to land it." The trade is good in chat — the alternative (force the user to switch to dashboard for diff review) breaks the value proposition of building from chat at all.

If a build produces something obviously wrong despite passing critique and test, the user finds out at the post-build DM (§5) and can revise via a follow-up turn that re-enters the design loop.

---

## 3. Conversational manifest design

The design phase is a turn-by-turn loop between bot and user, anchored on a **draft manifest** that the bot iteratively refines.

**The shape of each turn:**

| Turn | Bot does | User does |
|---|---|---|
| 1 | Sketches the app from the user's stated goal: "I'm thinking a bot helper that watches your GitHub PRs and pings you when a review's overdue. It'd run every 30 min, only alert on PRs you authored. Sound right?" | Reacts: agrees, modifies, adds, contradicts |
| 2..N | Restates with the user's modifications, pushes back on anything that seems off ("you mentioned alerting at 9am — but your timezone is Pacific and you said you start at 7; want it earlier?"), summarizes new state | Continues until satisfied |
| Final | "Okay, I think we agree. Want me to build it? You can also reply 'show me the spec' if you want the full markdown." | "build" / "show me the spec first" / "actually one more thing..." / "cancel" |

**Bot pushback is part of the design.** When the user says something that contradicts an earlier requirement, or that doesn't match what the bot believes about their context (timezone, current tooling, etc.), the bot raises it. This is not adversarial; it's how alignment happens. The bot is the user's design partner, not their stenographer.

**The "show me the spec" affordance** dumps the in-progress manifest as a markdown document — same content the bot will hand off to code generation. Users who want to read the whole thing can; users who don't, don't have to. The bot offers this proactively at every iteration boundary so it never feels hidden.

### 3.1 The conversational summary as durable artifact

At each turn, the bot maintains a working **conversational summary** — a short paragraph capturing what the app is meant to do, not the full spec. Three uses:

1. **Mid-flow context.** When the bot starts turn N+1, the summary is what it's been building up. Re-rendering it back to the user every turn makes the conversation grounded ("here's where we are: [summary]") rather than a series of disconnected exchanges.

2. **Spec input.** When the user says "build it," the summary is included verbatim in the manifest's `conversational_summary` field. The code generator reads it as part of its build prompt — the user's voice is preserved into the actual implementation.

3. **Post-build recap.** After the build completes, the bot DMs the user with the same summary attached: "I built [app name] — here's what it does: [summary verbatim]. Here's how to use it: [usage]." The user remembers what they asked for; they're not surprised by what they got.

Schema-wise, this is one new field on the manifest:

```jsonc
{
  "conversational_summary": "Watches the user's authored PRs every 30 min. DMs when a review's been pending more than 24h on weekdays. Pacific timezone, 7am–6pm only.",
  // ... other manifest fields
}
```

It's a string, not a list of sub-fields. The whole point is that it's the bot's natural-language framing — structuring it would lose the value.

### 3.2 Approval semantics

When the user says "build it" / "yes" / "go ahead" — same keyword classifier shape as `evo guide`'s save gate — the bot:

1. Finalizes the manifest (`conversational_summary` is locked, all extracted structured fields committed)
2. Persists the draft manifest at `{shared_dir}/applications/{bot_id}/{app_id}.json` with `status: "building"`
3. Kicks off the existing forge code-generation pipeline against the manifest
4. Wraps the wizard turn: "Got it — I'll build [app name]. You'll get a DM when it's ready. ETA ~5 min." Returns the user to normal bot chat.

If the user says "show me the spec," the bot pastes the rendered manifest markdown inline (or attaches it, depending on channel — Telegram supports document attachments) and re-asks the build/edit question on the next turn. No state change.

If the user says "edit" / "change" / "redo," the conversation continues — the bot doesn't lose context.

If the user says "cancel" / "no" — the wizard wraps without persisting anything to applications. The conversation transcript stays in wizard state if we ever want to revive it; not exposed for v1.

---

## 4. Code generation phase

After approval, the existing forge pipeline runs against the finalized manifest:

```
draft manifest (status: "building")
  → forge_engine.run(...)     # existing code generation
    ├─ build code from build_spec + conversational_summary
    ├─ critique × 3
    ├─ test
    └─ apply (writes files under bots/<bot_id>/apps/<app_id>/)
  → on success: manifest.status = "active"; emit "build_complete" notification
  → on failure: manifest.status = "build_failed"; emit "build_failed" notification with last-error string
```

**No human approval in this stage.** The existing Gate phase that previously waited for operator click is **skipped for messaging-driven builds**. We discriminate by the `source` field on the manifest: `bot_created_via_chat` (this spec) vs. `gallery_installed` / `bot_created` (existing dashboard flows). The Gate stays for the dashboard sources; messaging-source manifests skip it.

This is the meaningful behavioral difference from the existing forge surface. The justification: the design-phase conversation IS the gate. By the time we get here, the user has agreed.

**Critique and test still run.** They're protecting against the code generator's own failure modes (bad imports, infinite loops, etc.), which the user can't catch in conversation regardless. If critique/test fails, the build fails — the user finds out in the post-build DM (§5) and can choose to revise.

---

## 5. Bot-initiated DM after build

**Build complete:**

```
✓ Built calendar-watcher

Here's what it does:
[conversational_summary verbatim]

How to use it:
- It runs automatically every 30 minutes — no manual invocation
- I'll DM you when a calendar event's about to start without an agenda
- Reply "snooze" to mute alerts for the day
- Run `evo apps` to see all your installed apps

If something's off, just message me — say something like "the calendar watcher
isn't catching X" and I'll see what we can do.
```

**Build failed:**

```
✗ Couldn't build calendar-watcher

What went wrong:
[short summary of the last error — not a stack trace; the bot's read of why]

The spec we agreed on is here for reference: [spec markdown attached or pasted].

Want to try again with adjustments, or shelve this one? Reply
"try again" / "adjust X / "shelve it" — your call.
```

### 5.1 Notification mechanics

The bot needs to receive a "build done" or "build failed" event for a user it didn't message in this session. Three options for plumbing:

1. **Bot polls forge state on session_start.** Existing `session_surface.py` already injects pending tasks; we'd add forge job state. Pro: reuses existing path. Con: user only sees the result on their next session start, not when the build finishes.

2. **Forge writes to a per-user notification queue; bot reads on every turn.** Drop the notification in `{shared_dir}/notifications/{user_key}.jsonl`; bot's TurnObserver checks the file at session_start AND at every user turn. Pro: low latency. Con: needs a new directory + write contract.

3. **Bot-initiated outbound message via Telegram Bot API.** Forge directly calls the Telegram Bot API using the bot's token (which lives in the bot's `openclaw.json`). Pro: true push. Con: requires forge to read the bot's config; coupling.

**Recommend (2)** for v1. Latency is good (next user turn), the file shape is simple (jsonl: one event per line), and forge already writes to `{shared_dir}` for other artifacts. Outbound push (3) is a v2 nice-to-have that's straightforward to add later.

**Ownership clarification.** The notification queue is an **Evolve component, not OpenClaw**. OpenClaw provides hooks (`session_start`, `llm_output`, etc.) that our plugin attaches to. The queue is net-new Evolve code on top of those hooks:
- Producer: `forge_jobs.py` writes a record when a build finishes (success or failure).
- Consumer: `session_surface.py` reads at OC's `session_start` (catches events accumulated since the last session); `TurnObserver` reads at every user turn (catches events that landed mid-session so the user doesn't have to start a new one).
- Surface: events become a `systemAppend` block — same mechanism POD_CONDUCT, the bot guide, and the wizard already use. OpenClaw doesn't know the queue exists; from its perspective we're just assembling a richer system prompt.

The queue is *not* a new IPC primitive or a separate process. It's a directory of `.jsonl` files plus two existing read sites being taught one new thing. If we ever want true outbound push (option 3 above), that's a separate addition that bypasses OC's chat surface entirely — also all Evolve code.

The bot's TurnObserver checks the per-user notification queue:
- At session_start (catches anything since last session)
- At each user turn (catches anything that landed since the last bot turn)
- After processing, marks events read; pending count goes to zero

When unread events exist, they're injected as a systemAppend block ahead of normal turn handling — same pattern as the wizard's mid-flow injections.

### 5.2 The "user didn't ask, mid-conversation" awkwardness

If the user is mid-conversation about something else when the "build done" event lands, the bot interrupting them with "✓ Built X" is jarring. Two options:

- **(a) Inject as systemAppend at the start of the bot's response.** The bot weaves "by the way, X is built" into whatever they were going to say. Less abrupt but might feel buried.
- **(b) Send as a separate message (out-of-band).** Cleaner separation of concerns: "X is built" is its own message, the user's question gets its own response. Channel-dependent (works on Telegram, Slack with thread replies; trickier in single-thread channels).

**Recommend (a) for v1**, (b) as a v2 toggle. The systemAppend route is mechanically already there from wizard work. The bot can be instructed to mention build completion in a brief lead-in before answering the user's actual question.

---

## 6. In-flight discovery

If the user runs `evo wizard` while a forge job they triggered is still building, OR types `evo` for a recommendation, the bot should know:

- Whether a build is in progress for this user/bot
- Roughly when it's expected to land
- Anything actionable they could do (revise the spec, cancel the build)

Mechanism: `{shared_dir}/forge/jobs/<job_id>.json` already exists with `status: "queued" | "running" | "complete" | "failed"`. We add a small helper `forge_jobs.in_progress_for(bot_id, user_key)` that returns active jobs for the (bot, user) pair.

The wizard's CHALLENGE / GREET / GOALS phases stay focused — they don't mention in-flight builds. Only the WRAP and the bare-`evo` recommendation handler do, and only when there IS an in-flight job. Looks like:

```
By the way — the calendar-watcher you asked for earlier is still being
built (~3 min left). I'll DM you when it's ready. Run `evo forge status`
any time to check.
```

`evo forge status` becomes a new subcommand for this — primary-only, returns status of the user's in-flight builds. Lightweight; `evo forge revise <app_id>` and `evo forge cancel <app_id>` are v2.

---

## 7. Manifest sharing between bots

**Apps are bot-specific in v1.** Each bot owns its own `applications/<bot_id>/<app_id>.json` and the build output lives at `bots/<bot_id>/apps/<app_id>/`. Two users on different bots who want the same app each go through the design conversation independently.

**Once a manifest is finalized, sharing it is a separate user action.** Sketched here, not built:

- `evo share-manifest <app_id> --to <bot_id>` (admin only) writes a copy to `{shared_dir}/manifests/shared/<bot_id>/<app_id>.md` (markdown, frontmatter + body) and DMs the recipient bot's primary: "[other primary] shared a manifest for [app name] — type `evo install <app_id>` to build it on this bot."
- Recipient bot's primary types `evo install <app_id>` → bot reads the shared manifest, kicks off a NEW design conversation pre-seeded with the shared manifest's content. The recipient can accept-as-is or modify before building. Code generation runs locally on the recipient's bot.
- Shared manifests are not "installed apps" — they're templates. Build still happens per-bot.

The point of separating share from install: the recipient's environment may differ (different integrations, different team norms, different scale). The conversation lets them adapt before forging.

This is intentionally deferred. v1 of forge-via-messaging delivers the per-bot path; sharing comes once we see whether anyone authors a manifest worth sharing.

---

## 8. What this changes for `FORGE_OPTIONAL` (5b7)

The wizard's deferred forge phase becomes the entry point to the design conversation. Concrete shape:

- **Trigger condition** — same as before: fires when `top_goals` non-empty AND `apps_accepted` empty. Otherwise skips to wrap.
- **`forge_intro` sub-phase** — bot says "you mentioned [goal] but didn't find a fit in the gallery. Want me to draft a custom app for that? It'll take a few back-and-forth turns to align on what it does, then I'll build it autonomously and DM you when it's ready." Decline → wrap. Opt in → enter the design conversation.
- **`forge_design` sub-phase** — the conversational manifest design from §3. Multi-turn. Engine special-cases like `GUIDE_CONFIRM`: `forge_gather_block` or similar prompt builders, `_handle_forge_design` dispatch. The bot maintains the conversational summary across turns.
- **`forge_confirm` sub-phase** — same shape as `GUIDE_CONFIRM`: render proposed manifest summary, ask "build it?" Save / edit / cancel keyword classifier reused.
- **On confirm** — persist manifest, kick off forge code generation pipeline (skipping the dashboard Gate), return to WRAP with "queued, expect a DM."

Estimated scope for 5b7 implementation, given this design: ~900-1100 lines (phases, prompts, engine handlers, forge_jobs glue, notification queue read at session_start, tests). Larger than 5b5 because of the new notification mechanism and the manifest-skipping-Gate path.

---

## 9. Phased delivery

After starting implementation, scope split into two PRs because the foundations are independently useful and the wizard wiring touches manifest construction details that warrant their own reviewable change. Original "single PR" plan revised to:

| Slice | What | When |
|---|---|---|
| 5b7a | Foundations: `forge_engine.run_forge_job(auto_approve_actor=...)` parameter, `evo/notifications.py` queue (write/read/mark-read/render), `session_surface.py` reads notifications when `--user-key` set, plugin TurnObserver passes `--user-key` derived from session context | Shipped |
| 5b7b | Wizard FORGE phases (`FORGE_INTRO`/`FORGE_DESIGN`/`FORGE_CONFIRM`), conversational manifest design, manifest construction from gathered state, forge job kickoff in daemon thread with auto-approve, in-flight discovery hint in WRAP | Shipped |
| 5b8  | (deferred) Manifest sharing between bots: `evo share-manifest`, `evo install <shared>`, recipient-side design conversation | Future |
| 5b9  | (deferred) Outbound push notifications via Telegram Bot API rather than next-turn polling | Future |
| 5b10 | (deferred) `evo forge revise <app_id>` and `evo forge cancel <app_id>` | Future |

**Why split.** 5b7a is genuinely useful on its own — the notification queue is reusable beyond forge (any future async event the bot wants to surface to the user can write to it; arbiter proposals reaching apply, security-warden findings, etc. all have the same shape). Shipping the foundation first lets follow-up work iterate against a stable surface.

5b7b's manifest-construction details turned out to need careful thought — `forge_jobs.create_install_job` assumes a `pkg_id` from the gallery, while bot_created manifests don't have one. Resolving that cleanly is its own design decision (new job factory? generic constructor? thread `pkg_id=None` through?), better made in a focused PR than under time pressure inside a foundation PR.

The deferred items are real but not prerequisites for either 5b7a or 5b7b.

---

## 10. Open questions

1. **Where exactly does forge skip the Gate?** The forge engine has the gate hardcoded in its phase ordering. Cleanest is a `skip_gate: bool` parameter on `forge_engine.run()` set based on the manifest's `source`. Alternative: a separate "messaging-driven" job type with its own phase list. The first is less code but couples; the second is more code but cleaner.

2. **What if the user disconnects mid-design?** Today's wizard saves state across turns in `{shared_dir}/wizard/`. Forge design state would round-trip in the same file. If the user comes back days later, do we resume or start fresh? Resume by default; have an explicit "start over" command. This matches existing wizard resume semantics.

3. **What about builds that take longer than expected?** The "ETA ~5 min" is a guess. If builds blow past that, do we send an interim "still working" DM? Or stay silent until done? Recommend: silent until done. Users running `evo` see the in-progress hint anyway.

4. **What's the failure recovery UX?** §5 sketches "try again / adjust / shelve" but the actual implementation needs a path. Probably: `evo forge revise <app_id>` re-enters the design conversation pre-seeded with the failed spec. v2 work.

5. **Does the manifest's `conversational_summary` field need a length cap?** Probably yes — say 1000 chars. Over that and the bot is hoarding context that should be in the spec body. Worth a soft constraint in the design conversation: bot pushes back if the summary balloons.

6. **What happens to apps that were authored via dashboard before this surface existed?** Nothing — they keep working. Their `source` field is `bot_created` (without `_via_chat` suffix); the messaging-aware code paths only fire on the new source value. No migration needed.
