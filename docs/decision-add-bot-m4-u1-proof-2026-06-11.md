# Decision: Add-bot M4 — the U1 activation proof run (Ledger)

**Date:** 2026-06-11 (02:04–04:00 PT, single session)
**Surface:** `evo add-bot` driven end-to-end on the live pod via the engine API (the same `/api/evo/dispatch` + `/api/evo/wizard/turn` calls the messaging plugin makes), by the pod admin's agent
**Participants:** Pod-Admin (operator, via agent) + evo (wizard engine)
**Outcome:** **Ledger** — a project-manager bot ("maintain a living ledger of design decisions, commitments, and blockers for the launch project"), created conversationally in ~17 wall-clock minutes including a live production-bug fix; purpose captured, consent + tone recorded, 3 of 6 starter apps installed. The 24-hour first-briefing leg is **structurally unreachable today at three independent layers** — the biggest being a pod-wide delivery regression this proof run discovered (OpenClaw 2026.6 removed the gateway endpoint every gallery app delivers through; broken since Jun 3, masked as "unmeasurable" in the delivery ledger).
**Why this doc exists:** M4 of [spec-add-bot-wizard-build-delta-2026-06-10.md](spec-add-bot-wizard-build-delta-2026-06-10.md) §9 — the U1 proof artifact from [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §5, in the Atlas-session pattern ([reference-conversational-bot-creation-2026-05-19.md](reference-conversational-bot-creation-2026-05-19.md)). It is an honest capture: what worked, what broke, what it cost.

---

## TL;DR scorecard

| U1 leg | Result |
|---|---|
| Fresh bot created end-to-end conversationally | ✅ ~2 min of operator conversation + ~90 s provision; smoke check passed |
| `purpose{}` captured (declared, confidence 1.0) | ✅ persisted to the bot's `network.json` block |
| Starter pack installed via dependency machinery | ⚠️ 3 of 6 queued installs built + installed (all three as v7-arc native mints); the rest blocked by the coherence gate doing its job (§Findings 5) |
| Consent + tone recorded | ✅ ground-rules guide written; `privacy_seed` on every install job (manifest v24) |
| Briefing decision recorded, default-on | ✅ `briefing{enabled: true, time: "07:00"}` in network.json; offered as the explicit opt-out turn |
| Briefing app installed + scheduled | ❌ the coherence gate refused the build with one CRITICAL finding: *messaging output declared, no messaging-capable integration* — i.e., the day-1 channel-less state itself (§The 07:00 window) |
| **First briefing delivered < 24 h** | ❌ at the original run (three independent layers) → ✅ **delivered 2026-06-12** via the fixed `openclaw message send` path to the operator's Telegram DM (re-proof — §Delivery evidence — completed); not within the literal 24 h of creation, and the `on_time` monitor row lands at the next 07:00 |
| New platform bugs found | ✅ 4 found: wizard-extractor outage (**fixed + merged mid-run**, #2685); admin-UI chat entry-point routing (**fixed same morning**, #2687); pod-wide delivery regression (filed); v7-arc native write drops the wizard's consent seed (filed) |

The conversation passed the copy review against [principle-plex-test.md](principle-plex-test.md) (§Copy pass below).

---

## Timeline

| Clock (PT) | Event |
|---|---|
| 02:04 | Entry attempt via the admin-UI chat surface — **failed** (finding 1: the home-chat wrapper defeats evo-command routing; evo's LLM freelanced an answer and invented a nonexistent CLI) |
| 02:09 | Wizard started via the engine API; `ab_need` prompt rendered |
| 02:10 | First need answer extracted **nothing** — finding 2: the wizard's LLM extractor had been dead on the pod since the Jun 10 socket-cutover sweep (#2621 routed its `api.anthropic.com` call to the local admin socket) |
| 02:10–02:22 | Root-caused, fixed at both altitudes, tested, PR opened, auto-merged on green, pulled to the pod, admin daemon kickstarted ([#2685](https://github.com/evolve-ops/evolve/pull/2685)). The in-flight wizard session survived both daemon restarts |
| 02:22–02:25 | The conversation proper: need → audience → purpose confirm → pack adjust → briefing yes → consent + tone → name → plan confirm → credentials (borrow) |
| 02:25–02:26 | Provision: account, home dir, key, pod registration, gateway on, smoke check — ~90 s, all stages relayed in plain words |
| 02:26 | Wrap delivered; finalize wrote `purpose{}` + `briefing{}` + ground-rules guide and queued 6 install jobs (sequential, foundations first) |
| 02:26–02:44 | Pack builds, sequential, each forged by Ledger's own agent: contacts ✓, calendar-sync ✓, note-taker ✓ (**v7-arc native writes** — Slice 3a's first real-world mints); task-manager ✗, commitment-tracker ✗, **morning-briefing ✗** (coherence gate — §The 07:00 window) |
| 02:44–03:50 | Investigation: why the briefing can't deliver on this pod at all — the three-layer finding (§The 07:00 window), the consent-seed regression, evidence capture |

Creation → "bot exists, switched on, apps building": **17 minutes**, ~13 of which were the live platform fix. The pure conversation, for an operator who knows what they want, is **~3 minutes**.

---

## The conversation (annotated, lightly scrubbed)

Driven through the engine API, so agenda-phase evo lines below are the engine's captured understanding (on a real channel evo's LLM renders them conversationally); **plan, build-progress, and wrap messages are verbatim** what any operator sees. Operator lines are verbatim throughout. The credentials step's sibling-bot name list is paraphrased rather than quoted, per [PLACEHOLDER_NAMING.md](PLACEHOLDER_NAMING.md).

**Need (why before what).**

> **op:** I keep losing track of the launch work — decisions we make in design sessions, things I said I'd do, what's blocked waiting on someone. I want a bot that keeps that ledger for me. More co-pilot than research assistant: it should keep the list itself, not just nag me.

Extractor filled: the need, co-pilot (vs research assistant), a one-line mission draft, and `project-manager` as closest role — from one natural sentence. The why-before-what ordering from the reference design held: no app or integration talk yet.

**Audience.**

> **op:** Just me. Direct messages — Telegram once I get around to connecting it. Nobody else needs to see it.

Captured: `just me`, surface "Telegram direct messages", private feed. Because the audience is single-person, the wizard later skipped group-only questions (Telegram privacy mode) — correct gating.

**Purpose confirm.** The engine mirrored the job back in plain words and asked for an explicit yes. One turn.

**Starter apps (the role suggests a set).** The Project Pack proposal, with an honest cost preview:

> **evo (engine agenda):** 6 apps, about 12 minutes and $22.61 to build — Task Manager, Contacts, Calendar Sync, Meeting Note-taker, Commitment Tracker, Pre-Meeting Brief. OK?
> **op:** Drop the Pre-Meeting Brief — nothing's connected to a calendar yet. Keep the rest.
> **evo:** One change at a time, please — tell me what to drop, or what to add.
> **op:** Drop the Pre-Meeting Brief
> **evo:** Updated — the set is now: Task Manager, Contacts, Calendar Sync, Meeting Note-taker, Commitment Tracker. … 5 apps, about 10 minutes and $18.87 to build — OK?
> **op:** Yes, that set works.
> *(not recognized as a yes; re-prompted)*
> **op:** yes

Adjust-by-name worked. Two friction nits recorded in §Findings (the "one change at a time" rejection of a compound-but-single-intent sentence, and the strict yes-classifier).

**Briefing (the one default, explicit opt-out).** Offered in the decided wording ("…delivered here each morning around 7. Want it? You can say no, or change the time later."). Operator: yes.

**Consent + tone (non-skippable).**

> **op:** Consent: it keeps notes from our chats to do its job — decisions, commitments, blockers — and I can tell it 'forget that' or 'stop keeping notes' any time. That works for me, yes.
> **op:** Tone: house default is fine — short and direct, no fluff.

**Name (after role lock).**

> **op:** Call it Ledger — that's exactly the job. Account name ledger is fine.

**The plan echo (verbatim operator-visible):**

```
Here's the plan:

Bot:    Ledger  (account: ledger)
Job:    Maintain a living ledger of design decisions, commitments, and blockers for the launch project
Role:   project manager
For:    just me
Where:  Telegram direct messages
Feed:   private feed for me
Apps:   Task Manager, Contacts, Calendar Sync, Meeting Note-taker, Commitment Tracker  (about 10 min and $18.87 to build)
Brief:  morning briefing each day around 7 (changeable)
Notice: This bot keeps notes from our chats—decisions, commitments, blockers—to do its job. You can tell it
        'forget that' or 'stop keeping notes' any time.
OptOut: say 'forget that' or 'stop keeping notes'
Tone:   short and direct, no fluff

Nothing is built yet — this is the plan, and it's only saved once you confirm.

Reply **yes** to save it, tell me what to change ("make it weekly", "call it Almanac"), or **cancel** to drop it.
```

**Credentials.** One credential, as designed (Claude-assumed): borrow a copy from a sibling bot (the engine listed the pod's eight existing bots by name), paste a fresh key, or skip. Operator: *"use atlas's key."* Provision started immediately.

**Build progress (verbatim, polled with "status"):**

```
Building **Ledger** — here's where things stand:

✓ Checks passed
✓ Bot account created
✓ Home folder ready
✓ Connected to Claude
✓ Added to your pod
… right now: switching it on

Still working — check back with me in a minute.
```

**The wrap (verbatim):**

```
Done ✓ **Ledger** is up and running.

✓ Checks passed
✓ Bot account created
✓ Home folder ready
✓ Connected to Claude
✓ Added to your pod
✓ Switched on
✓ Passed its final check — it's ready to think

One note: Ledger runs on a copy of atlas's key. The copy is independent — if you ever replace atlas's key,
Ledger keeps its own.

Its apps are being built now — about 12 minutes, nothing for you to do: Task Manager, Contacts, Calendar Sync,
Meeting Note-taker, Commitment Tracker, Morning Briefing. You can watch them land on the Apps page.

Your morning briefing is set up — the first one lands by 7 tomorrow morning, even before Ledger has anything
connected. Change the time any time on the admin page.

I've written down the ground rules you set — what people are told, how they opt out, and how Ledger should
sound. Ledger reads them at the start of every conversation.

What's next: Ledger doesn't have a place to chat yet. Open the Bots list on the admin page to connect one
(Telegram, Slack, and friends) — and to see everything else about it.
```

---

## Evidence

**`purpose{}` + `briefing{}` on the bot's `network.json` block** (storage decision §4 of the delta — network.json — confirmed in practice):

```json
"purpose": {
  "archetype": "project-manager",
  "mission": "Maintain a living ledger of design decisions, commitments, and blockers for the launch project",
  "captured": "declared",
  "confidence": 1.0,
  "reviewed_at": "2026-06-11T09:24:58Z"
},
"briefing": { "enabled": true, "decided_at": "2026-06-11T09:26:37Z", "time": "07:00" }
```

**Ground rules** at `{shared_dir}/bot_guides/ledger.md` — audience, tone in frontmatter; mission, consent notice, opt-out in the body; `authored_by: pod admin (via evo add-bot)`.

**Consent seeding — worked, then was dropped (regression, filed).** Every queued install job carried `privacy_seed` in `context_snapshot`, and forge merged it into the v24 manifests: `manifests/_history/contacts_v1_.json` (schema_version 24) carries `privacy.consent_notice` with the wizard's exact words. But the Slice 3a **v7-arc native write then replaced the live manifest with the instance shape, which carries no privacy block — and neither does the bound spec**. The wizard's consent answers survive only in `_history`, where nothing reads them (§Findings 6). No `audience_scoping_seed` was queued — correct, single-person audience.

**Slice 3a — first real-world v7-arc native mints** (#2677's native-write path, previously proven only on the backfill):

```
[2026-06-11T09:33:37Z] v7-arc native write: spec p-4136a932 bound at /Users/Shared/evolve/gallery/builtin/p-4136a932/2026.05.20-1.0.json, instance contacts at /Users/ledger/.openclaw/workspace/manifests/contacts.json
[2026-06-11T09:35:20Z] v7-arc native write: spec p-fe9acef3 bound at /Users/Shared/evolve/gallery/builtin/p-fe9acef3/2026.05.20-1.0.json, instance calendar-sync at /Users/ledger/.openclaw/workspace/manifests/calendar-sync.json
[2026-06-11T09:39:05Z] v7-arc native write: spec p-f14e9562 bound at /Users/Shared/evolve/gallery/builtin/p-f14e9562/2026.05.20-1.0.json, instance meeting-note-taker at /Users/ledger/.openclaw/workspace/manifests/meeting-note-taker.json
```

**Pack build outcomes** (sequential, foundations first, per the M3 driver):

Every app was forge-built *by Ledger's own agent* with its borrowed key (apps inherit the bot's LLM — no central credential), sequentially, 2–4 minutes each:

| App | Result |
|---|---|
| Contacts | ✅ built + installed (v7-arc native), schedule registered |
| Calendar Sync | ✅ built + installed (v7-arc native), schedule registered |
| Meeting Note-taker | ✅ built + installed (v7-arc native), schedule registered |
| Morning Briefing | ❌ **coherence gate, one CRITICAL finding**: `scheduled_action 'morning-briefing' declares a messaging output but requirements.integrations[] contains no messaging-capable integration` — no plist, no schedule (§The 07:00 window) |
| Commitment Tracker | ❌ coherence gate (undeclared scheduled-action inputs/outputs + the same messaging-output finding) |
| Task Manager | ❌ same gate, same class (its gallery package also declares `files_pack` whose directory is missing from the deployed gallery — drift noted) |

The gate blocking incoherent builds is the verifier doing its job — no incoherent app shipped to a fresh bot — but a 3-of-6 pack on day one is a real activation tax (§Findings 5), and the briefing block is structural, not a build flake (§The 07:00 window).

**Schedules on the host:** `ai.evolve.ledger.calendar-sync.plist` and `ai.evolve.ledger.note-taker.plist` installed and loaded; **no** `ai.evolve.ledger.morning-briefing.plist` exists (§The 07:00 window).

---

## Copy pass (Plex test)

Reviewed every operator-visible string in the transcript against [principle-plex-test.md](principle-plex-test.md):

- **Passes.** No "provision/forge/manifest/gateway/plugin/RSI" anywhere. Progress lines name outcomes ("✓ Bot account created", "✓ Connected to Claude", "Switched on"), not stages. The borrowed-key caveat ("the copy is independent…") states a subtle technical fact in operator words. The wrap's "doesn't have a place to chat yet" is the day-1 channel gap in honest plain English, with a next step.
- **Nit:** the wrap says the first briefing "lands by 7 **tomorrow** morning" — for a bot created at 02:26 the schedule would fire *today* at 07:00, so the copy is wrong in the midnight-to-7am creation window. (Moot tonight for a worse reason — the promise itself silently failed; finding 4.)
- **Nit:** the engine's re-prompt "A simple yes or no works here" is fine; the shape-phase "One change at a time, please" is also fine copy — the friction is behavioral (§Findings 7), not wording.

---

## Findings (honest rough edges)

**1. The admin-UI chat entry point cannot start the wizard (bug — fixed same morning, #2687).** M3's "Add a bot" button pre-fills `evo add-bot` into the Home chat, but `/api/home/chat` wraps every message in a timestamp + `<session-context>` envelope before it reaches evo's gateway, and the plugin's keyword parser requires the literal first token to be `evo`. The command never reaches the dispatcher; evo's LLM freelances an answer (in our run it invented `sudo evolve-admin add-bot`, which does not exist). The Telegram path is unaffected (messages arrive bare). Filed from this run and fixed upstream within hours ([#2687](https://github.com/evolve-ops/evolve/pull/2687): unwrap the proxy envelope before evo keyword routing).

**2. The wizard's LLM extractor was dead pod-wide (bug, fixed live — [#2685](https://github.com/evolve-ops/evolve/pull/2685)).** The Jun 10 socket-cutover sweep (#2621) swapped `urlopen` → `urlopen_admin` on every evo HTTP call site, including the extractor's `api.anthropic.com` call. `urlopen_admin` strips a request to its path and sends it to the *local admin daemon's unix socket*, so extraction silently returned `{}` and no agenda phase could ever advance — for `add-bot`, setup, guide authoring, and app-create alike, since the cutover. Fixed at both altitudes (host guard in `urlopen_admin` makes the misuse class impossible; the extractor uses plain `urlopen`), regression-tested, merged and deployed mid-run. The wizard session survived the daemon restarts and resumed where it left off — the engine's persistence doing exactly what it promised.

**3. The U1 delivery promise is broken pod-wide, and has been since Jun 3 (P0-grade, filed).** OpenClaw 2026.6.1 (installed on the host Jun 3) removed the gateway's `POST /api/message` endpoint. Every gallery app delivers through that endpoint by convention — morning-briefing (including the v2.1 spec written *after* the removal), evening-sweep, pre-meeting-brief, email-triage, calendar-summary, note-taker, the EA-pack scripts. Verified live: 404 on two bots' gateways; the EA morning/evening launchd jobs on the baseline bots exit 1; **zero `on_time` rows have ever appeared in the delivery-monitor ledger** — every row is "unmeasurable". The supported 2026.6 surfaces are `POST /tools/invoke` (works, but no messaging tool is policy-exposed) and `openclaw agent --deliver` (an agent turn, not a plain send). Migration task filed: pick the convention once, fix the gallery + deployed instances, and teach the delivery monitor that scheduler-exit-1 + no run file is `did_not_run`, not "unmeasurable". The U2 canary soak (started Jun 11) would have caught this within the week; the M4 proof run caught it on day one.

**4. The day-1 channel gap breaks the briefing promise twice (design gap for U1 — the §The 07:00 window analysis).** The wizard captures the *intended* surface but provisioning configures no channel, and the coherence gate then refuses the briefing app on a channel-less bot (its CRITICAL messaging-output-without-integration finding is the channel gap restated as a manifest invariant). The result tonight: the wrap promised "the first one lands by 7" and that promise silently failed eight minutes later when the gate blocked the install — **nothing told the operator**. Three decisions needed at a design sync: (a) channel connect inside the wizard (token-paste step, same shape as credentials) vs. deferred briefing that self-schedules when the first channel connects; (b) what a briefing should do on a channel-less bot (the §6 no-data mode covered empty *data*, not absent *channel*); (c) finalize honesty — if a queued install fails after the wrap, the operator should hear about it in the same conversation surface, not discover it on the Forge Jobs page.

**5. Forge findings from the pack leg.** (a) Pack installs LLM-forge every app from its build spec on the target bot; the Project Pack's two most complex apps (Task Manager 58 KB manifest, Commitment Tracker 37 KB) both produced manifests the coherence gate refused — undeclared scheduled-action inputs/outputs, messaging outputs with no declared messaging integration. A 3-of-6 install rate on day one is a real activation tax: either these two packages need deterministic `files_pack` payloads (Task Manager declares one whose directory is missing from the deployed gallery — drift), or the forge prompt needs to teach the coherence contract better. (b) Both critique rounds on the fresh bot failed with `Model override "anthropic/claude-haiku-4-5" is not allowed for agent "main"` — fresh-provision config doesn't allowlist the critique model, so every critique pass on a new bot is silently skipped (non-fatal, quality-reducing). (c) `Phase 2.5: static analysis skipped (non-fatal): name 'api_key' is not defined` — a latent NameError in forge_engine's static-analysis step.

**6. v7-arc native write drops the wizard's consent seed (privacy-contract regression, filed).** M3's consent seeding works — the v24 manifests carried `privacy.consent_notice` with the operator's words — but Slice 3a's native-write conversion replaces the live manifest with the v7-arc instance shape, and the privacy block lands in neither the instance nor the bound spec. The consent answers survive only in `manifests/_history/`, where no enforcement or display path reads them. (Ironically, the two gate-blocked apps retain their seeded privacy — they were never converted.) Per-instance answers are instance-layer data; the native-write path needs to carry them as an instance/hydration overlay. Task filed with repro paths and a regression-test sketch.

**7. Conversation friction nits.** The shape phase rejects a compound-but-single-intent sentence ("Drop X — keep the rest") with "one change at a time"; the deterministic yes-classifier rejects "Yes, that set works." while accepting "yes". Both are one-line classifier improvements; neither blocked the flow.

**8. Known gaps carried in (per the M4 brief, noted not fixed).** (a) The smoke check is structural-only — a shape-valid garbage key would pass it. (b) The wizard doesn't pass the captured `{mission}` into the briefing app's config (the no-data composer reads `config.mission` and would say "My job here: …" on day one). Assessed for ride-along: the consent/audience seeds flow because manifest v24 gives them a first-class manifest home that forge merges; mission has none — the pass-through needs an agreed seam across engine → pack-driver → forge → app config. Medium change; belongs with the delivery-migration work, not this doc.

---

## The 07:00 window

There was no window to watch. `ai.evolve.ledger.morning-briefing.plist` was never installed (calendar-sync's and note-taker's schedules were — the machinery works when an app passes the gate). Nothing fired at 07:00 because nothing was scheduled, and the wizard's promise — *"Your morning briefing is set up — the first one lands by 7"* — silently did not come true. The operator was never told; the only trace is a failed job on the Forge Jobs page.

**Why the U1 activation metric cannot be met by this pod today — three independent layers, any one of which is sufficient to break it:**

1. **Day-1 bots have no messaging channel.** The wizard captures intent ("Telegram direct messages") but provisioning configures no channel; connecting one is a separate, operator-driven step (BotFather token). Until then there is no human-visible surface for a briefing to land on.
2. **The coherence gate refuses the briefing app on channel-less bots.** Its one CRITICAL finding — messaging output with no messaging-capable integration — *is* layer 1, restated as a manifest invariant. The §6 no-data mode anticipated empty *data* on day one; nobody anticipated no *channel*. As long as this holds, the briefing default-on promise can never be kept for a wizard-created bot, on any pod.
3. **The pod's delivery surface is gone.** OpenClaw 2026.6.1 (installed Jun 3) removed `POST /api/message`, the endpoint every gallery app's send helper uses. Verified live: 404 on two bots' gateways; the EA-pack morning/evening launchd jobs on the baseline bots exit 1; **zero `on_time` rows have ever appeared in the delivery-monitor ledger** — every classified window is "unmeasurable". Even the pod's established, telegram-connected bots have delivered no scheduled message for eight days.

The fix order is 3 → (2 + 1 together): migrate the gallery delivery convention to a supported OC 2026.6 surface (task filed — including teaching the delivery monitor that scheduler-exit-1 + no run file is `did_not_run`, not "unmeasurable"); then decide at a design sync how a day-1 briefing should work — channel connect inside the wizard (a token-paste step, same shape as credentials), an admin-surface delivery fallback, or an explicitly deferred briefing that schedules itself when the first channel connects (with the wrap copy saying so honestly).

---

## Verdict

Creation is **proven**: a genuinely useful bot, specified in plain conversation in ~3 minutes, provisioned in ~90 seconds, with purpose, consent, tone, and the briefing decision flowing into the right stores — and the engine's state machinery shrugging off two daemon restarts mid-conversation. The activation metric — first briefing in 24 h — is **structurally unreachable today**, at three independent layers (§The 07:00 window): day-1 bots have no channel, the coherence gate therefore refuses the briefing app, and the pod's delivery surface has been silently gone since the Jun 3 OpenClaw upgrade. None of the three is the wizard's fault; all three were invisible until something tried to walk the whole U1 path end to end, which is exactly what this milestone was for.

Substantive output of M4: the working creation flow, this causal map, four filed bugs (one fixed and merged mid-run — the wizard extractor outage), and the design questions U1 needs answered before the activation metric can be real (finding 4).

**Keep-or-retire:** Ledger is a keeper candidate (launch-ops is real dogfood); the operator decides. Retire path: `evolve-admin retire ledger`. If kept: connect a channel, and once the delivery migration lands, re-run the briefing install (it is one gallery install, not a re-provision).

---

## 2026-06-11 follow-up — the three layers closed, and the Ledger re-proof

Same day as the run above, all three layers from §The 07:00 window got fixes:

| Layer | Fix | Status |
|---|---|---|
| 3. Delivery surface gone (OC 2026.6 removed `POST /api/message`) | Gallery delivery migrated to `openclaw message send`; delivery monitor made exit-status-aware ([#2695](https://github.com/evolve-ops/evolve/pull/2695), + [#2698](https://github.com/evolve-ops/evolve/pull/2698)/[#2699](https://github.com/evolve-ops/evolve/pull/2699)) | ✅ deployed 06-11; delivery works pod-wide again |
| 1 + 2. Day-1 channel gap + the gate's (correct) refusal being silent | **Offer-now, auto-activate-later** (design sync 06-11): the wizard offers a Telegram token-paste turn after the briefing offer; otherwise the recorded `briefing{}` decision auto-activates when the bot's first messaging channel connects. Either path, no silent failure ([#2707](https://github.com/evolve-ops/evolve/pull/2707)) | ✅ merged 06-11 |

> **Re-proof caveat (2026-06-12):** layer 3's "delivery works pod-wide again" held for the **EA-pack** bots, but the **gallery morning-briefing build_spec was missed by the migration** — a freshly-created wizard bot's briefing still POSTs to the removed `/api/message` and 404s. The auto-activation (#2707) half of layers 1+2 is confirmed live; the *delivery* it activates is still blocked. See **§Delivery evidence** below; fix filed back to the META.

What #2707 changes, mapped to finding 4's three design questions:

- **(a) channel connect inside the wizard** — yes, as an optional `AB_CHANNEL` turn (token verified live, held in memory only, applied after the build; skip is first-class). A single-person bot also gets the admin recorded as its `primary_user` on the connected channel, so the delivery route resolves on day one. **And** the deferred-briefing half: the channel-registration chokepoint (`write_oc_config`) fires `briefing_activation` on a bot's first messaging channel — the recorded decision installs the briefing through the normal gallery path (calendar foundation folded in, `{mission}` + time seeded). Ledger's stranded `status: "updating"` briefing manifest counts as not-installed and is retried.
- **(b) what C-A4 means now** — the gate stays; the forge approval path deterministically declares the bot's *live* connected channel(s) in `requirements.integrations[]`, so C-A4 verifies real channel state instead of refusing every briefing. Channel-less bot → refusal stands, **loudly** (`system.app_install_failed` through the dispatcher, in operator words, with the "connect a channel and it sets itself up" promise).
- **(c) finalize honesty** — every post-wrap install failure notifies the operator; a completed auto-activation pushes a `decisions.briefing_activated` receipt; and the wrap's no-channel briefing copy now promises only what's true: *"it switches itself on the moment {name} gets a place to chat"* — the "lands by 7" line only renders when a channel is actually connected.

**Proof state.** The auto-activation path is proven on the synthetic harness (`tests/test_briefing_activation.py`: find-or-skip semantics incl. Ledger's stranded-manifest state, the zero→some channel transition, the loud failure + receipt events; `tests/test_evo_add_bot.py`: the offer turn, token-never-on-disk, all four wrap outcomes). No BotFather token was available to the proof session, so the live Ledger leg is a one-paste operator step:

1. Create a bot with @BotFather → paste the token on the admin **Skills page → Telegram skill → "+ Ledger"** button. *(An earlier draft of this step read "Bots → ledger → Skills → Telegram" — that path does not exist; corrected 2026-06-12 from the live re-proof.)* That single write fires the activation: Calendar Sync (if missing) + Morning Briefing queue as forge jobs; a 🟢 *"ledger's morning briefing is set up — the first one lands at the next 07:00"* receipt arrives on the operator channel, with a route note if step 2 is still pending. **Confirmed live 2026-06-12: the paste through that button fired #2707's `briefing_activation` correctly** — the Morning Briefing forge job ran and the `decisions.briefing_activated` receipt dispatched in the same pass (evidence below).
2. If the receipt carries the route note, record yourself as ledger's person (Users → Set primary user, or DM ledger the primary passphrase) — the briefing's `openclaw message send` route resolves via `bots.ledger.primary_user.external_ids`.
3. Evidence to append below after the next 07:00: `/Users/ledger/.openclaw/workspace/memory/briefing-runs/<date>.json`, the `on_time` row in `{shared_dir}/delivery_monitor/ledger/<date>.jsonl`, and the message itself on Telegram. (To capture same-day, temporarily set `delivery_time` in ledger's `morning_briefing/config.json` a few minutes ahead, then restore.)

### Delivery evidence (to complete the U1 artifact)

_Re-proof run on the live pod 2026-06-12, 01:26–01:45 PDT, after the operator pasted ledger's bot token via the **Skills page → Telegram skill → "+ Ledger"** button._

**Realization + auto-activation: proven.** The token paste fired #2707's `briefing_activation` end to end. Forge job `j-9382fe56` (gallery `2026.06.11-2.2`, spec `p-a9a74bf7`) built and **approved** the Morning Briefing manifest at 08:26:18Z (01:26 PDT): `manifest_shape: v7-arc`, `scheduled_actions[0].state: "active"` with an `Hour 7 / Minute 0` cron, four realized files verified (`scripts/morning_briefing.py`, `scripts/morning-briefing-cron.sh`, `morning_briefing/config.json`, `scripts/morning-briefing.plist`), and `ai.evolve.ledger.morning-briefing.plist` installed + loaded (`StartCalendarInterval 07:00`; `runs = 0` only because 07:00 has not yet elapsed — pod time at check was 01:44 PDT). **`approved` → realized was automatic and effectively instantaneous**: for a v7-arc briefing the `approved` status *is* the realized state (plist on disk + `scheduled_actions[0]` active), reached in the same forge pass the token paste triggered — well inside an hour of the paste, no separate runner needed.

**Delivery: ran, but did not deliver — blocked at three independent layers, none the wizard's fault.** Per the tri-state honesty rule, this run can prove **ran**, not **delivered**.

| Evidence | Where | Result |
|---|---|---|
| Briefing installed by auto-activation | forge `j-9382fe56` (spec `p-a9a74bf7`) `complete`; `ai.evolve.ledger.morning-briefing.plist` loaded, `StartCalendarInterval 07:00`, `scheduled_actions[0].state=active`, 4 files verified | ✅ |
| Activation receipt | operator channel: `decisions.briefing_activated`, `dedup_key briefing-activated/ledger/j-9382fe56`, `result: "sent"` 08:26:18Z — incl. the honest route note (*"One step left so it knows who to message…"*) | ✅ |
| Run record | `memory/briefing-runs/2026-06-12.json` | ❌ never written — `send` 404s **before** `write_run` (dir absent) |
| Delivery monitor | `on_time` row for `ledger/morning-briefing` | ❌ no row — realized manifest declares `outputs: []` / no `delivery_contract`, so the monitor's user-facing filter (`delivery_monitor.py::_derived_user_facing`) excludes the briefing from the monitored set entirely |
| The message | ledger's Telegram DM at ~07:00 | ❌ not delivered (both delivery surfaces fail — see below) |

**Why delivery is blocked (verbatim live evidence, 2026-06-12 01:44 PDT):**

1. **The realized briefing script targets a removed endpoint (platform bug).** `morning_briefing.py` delivers via `POST http://127.0.0.1:{port}/api/message` — the endpoint OpenClaw 2026.6.1 removed (finding 3 above). Running the exact command the 07:00 cron runs (`scripts/morning-briefing-cron.sh` → `morning_briefing.py send`):
   ```
   $ morning_briefing.py send --force
   BRIEFING_FAILED: 2026-06-12 gateway-error: HTTP Error 404: Not Found   (exit 2)
   ```
   A direct `POST /api/message` on ledger's gateway returns **HTTP 404**. Root cause: the bound gallery build_spec (`{shared_dir}/gallery/builtin/p-a9a74bf7/2026.05.20-1.0.json`) still instructs the forge LLM to *"POST plain text to `/api/message`"* (`bot_guidance[7]`, `identity/scope_includes[6]`). The Jun‑11 delivery migration (#2695/#2698/#2699) moved the **EA-pack** scripts and the delivery monitor onto `openclaw message send`, but **never migrated the gallery morning-briefing build_spec** — so every wizard-created bot's briefing inherits the dead endpoint. (Ledger is the *only* bot on the pod with a `morning_briefing.py`; the established bots run the already-migrated EA-pack, which is why "delivery works pod-wide again" held for them and masked this for the gallery path.)
2. **The supported surface can't reach the operator yet (operator pairing step).** Delivering the same composed briefing through the correct surface also fails:
   ```
   $ openclaw message send --channel telegram --target <operator-chat-id> -m "<briefing>"
   OutboundDeliveryError: Telegram send failed: chat not found … Likely: bot not started in DM …
   ```
   The token is valid (`getMe` ok) and ledger's Telegram channel is `enabled` with `dmPolicy: "pairing"`, but the operator has not yet started/paired a DM with *ledger's own* bot. `bots.ledger.primary_user.external_ids.telegram` is recorded — but recording the id is not the same as an established Telegram conversation; Telegram won't let a bot message a user who never initiated, so the operator must DM the ledger bot once (step 2 above: "DM ledger the primary passphrase"). The activation receipt's own route note flagged exactly this. (Pairing with the operator's *primary* bot is why the `decisions.briefing_activated` receipt itself reached the operator — that is a different bot than ledger.)
3. **The monitor is blind to the failure (observability gap).** Because the realized manifest declares `outputs: []` and no `delivery_contract`, the delivery monitor excludes the briefing from its monitored set — so the 07:00 window can fail silently with no `ran_undelivered` / `did_not_run` row, defeating the monitor whose stated purpose is to catch exactly this.

No `delivery_time` edit was needed or made (left at `07:00`): `cmd_send` does not gate on `delivery_time` — the launchd `StartCalendarInterval` governs the window — so the M4 note's "set `delivery_time` a few minutes ahead" lever is a no-op for this build; `launchctl kickstart` (or the manual `send` above) is the faithful window simulation, and it fails identically. Nothing was left mutated on the pod: no run file, no delivered message, `delivery_time` unchanged.

**Verdict (U1 re-proof).** Creation, realization, and **#2707 auto-activation through the corrected token-paste path are proven**. A first briefing actually *delivered* is **still unreachable for a freshly-created bot** — now for three reasons different from the original run: the gallery briefing build_spec was missed by the delivery migration (dead `/api/message`), the operator's per-bot Telegram pairing is incomplete, and the realized briefing is not even in the delivery monitor's scope. Layers 1 and 3 are platform fixes (filed back to the META); layer 2 is the operator's one-time DM to the ledger bot. None is the wizard's fault — all three were invisible until something walked the full path again, which is what this re-proof was for.

> **Resolution (2026-06-12, [#2792](https://github.com/evolve-ops/evolve/pull/2792)).** Layers 1 + 3 (the two platform bugs) fixed. Root cause for **both**: the bound builtin Spec (`gallery/builtin/p-a9a74bf7/2026.05.20-1.0.json`) was generated *before* #2695 and never re-seeded — a gallery install binds the pre-existing builtin Spec rather than re-reading the repo package, so #2695's gallery migration never reached the pod. **Layer 1:** #2695 had in fact already migrated the morning-briefing build_spec to `openclaw message send`; #2792 cleans the two residual positive "POST to the gateway" prose instructions (`identity.user` here, pre-meeting-brief `description`) and **re-seeds the live builtin Spec** (verified: `bot_guidance`/`scope_includes` carry no `/api/message`; delivery uses `openclaw message send`). **Layer 3:** the realized manifest's `scheduled_actions[]` are extracted from the workspace and drop the Spec's `outputs[]`/`delivery_contract`; #2792 adds an approval-time stamp (`_stamp_scheduled_delivery_contracts`, sibling to #2707's channel stamp) that re-asserts `outputs[].channel` + a `user_facing` `delivery_contract` from live channel state, so user-facing scheduled deliveries are always monitored. **Still open:** `ledger`'s stranded instance needs a re-forge to pick up the corrected Spec (sequenced post-deploy, so it also exercises the new stamp); layer 2 (operator DM to ledger) is unchanged; and the structural propagation gap (repo gallery edits don't auto-reach existing pods' builtin Specs) is flagged for a separate change.
>
> **Propagation-gap follow-up.** The structural gap is now closed by a deploy-time builtin re-seed (`migrate_v7.reseed_builtin_specs`), run every repo-puller tick (`repo_puller._run_gallery_reseed_hook`, alongside the openclaw-config validation hook — every tick, not gated on the pull diff, so it also heals already-stranded builtins). `migrate_gallery_package` now stamps each builtin Spec with the repo package's `pkg_version` + a content hash (`seeded_from_pkg_version` / `seeded_from_pkg_sha256`); the sweep regenerates the bound builtin Spec in place — reusing `migrate_gallery_package` against the fixed `2026.05.20-1.0.json` file — whenever the builtin carries no seed-provenance (the stranded class above), the repo `pkg_version` is newer, or the recorded version matches but the source content drifted. Idempotent, runs as `evolve` (the builtin tier is evolve-owned), and never touches operator-edited `gallery/local/` Specs.

### Delivery evidence — completed (2026-06-12 16:08–16:33 PDT, post-#2792 promotion)

_The closing re-proof: with #2792 + #2795 promoted to the live fleet, `ledger`'s morning briefing was re-forged onto the supported send path and **delivered end-to-end to the operator's Telegram DM**. This completes the U1 delivery leg._

**Setup — the fix went live mid-session.** The pod runs **canary release mode**, so the deploy checkout follows the `evolve-stable` pointer, not origin tip. At session start the pointer was pinned at **#2788** — so #2792's `_stamp_scheduled_delivery_contracts` and #2795's re-seed hook were **not yet live** (they were in the soaking candidate #2800). The candidate promoted on the next repo-puller tick: stable `f3bcab34` (#2788) → `82c97d5e` (#2800) at `2026-06-12T23:08:28Z` (16:08 PDT), and the promote hooks kickstarted admin-ui onto the new code. The re-seed then confirmed live on the bound builtin Spec (`gallery/builtin/p-a9a74bf7/2026.05.20-1.0.json`): `seeded_from_pkg_version` / `seeded_from_pkg_sha256` present, zero `/api/message`, delivery via `openclaw message send`.

**The re-forge surfaced (and routed around) a second platform bug.** Driving the stranded instance through the **normal gallery install path** auto-approved and — thanks to #2792's stamp — wrote a real `delivery_contract` onto the manifest, but it **kept the dead `/api/message` script unchanged**. Root cause: `forge_engine.assemble_context_package` only reads the gallery package's `build_spec` (snapshotted into the job's `context_snapshot`) on a *first* install; on a **re**-build it read the v7-arc Instance's own `build_spec`, which is empty — so the build LLM got an empty spec and the forge agent kept the prior files. A v7-arc re-forge therefore **cannot pick up a corrected Spec** — the exact "Still open" item above. Fixed durably in **[#2803](https://github.com/evolve-ops/evolve/pull/2803)** (rebuild falls back to the snapshotted `build_spec`); for this live proof the stranded instance manifest was moved aside so the forge took its first-install path. The clean re-forge (`$3.51`, auto-approved) then regenerated `scripts/morning_briefing.py` onto `openclaw message send` (`resolve_route` → `--channel=telegram --target=… --message=… --json`, with the launchd `PATH`/`cwd` handling) and the manifest carried the `delivery_contract` (`user_facing`, run-file evidence `memory/briefing-runs/{date}.json`). Pod left in the correct end state: manifest `active`, script clean, plist `Hour 7 / Minute 0`, `config.delivery_time 07:00`.

**The three artifacts — tri-state honest.** A real fire of the loaded plist (`launchctl kickstart … ai.evolve.ledger.morning-briefing`, runs as `ledger` — the exact 07:00 path) at 16:32:

| Artifact | Where | Result |
|---|---|---|
| (c) **the message** | the operator's Telegram DM | ✅ **DELIVERED** — gateway log `2026-06-12T16:32:32 [telegram] outbound send ok accountId=default … messageId=4 operation=sendMessage deliveryKind=text` + `[ws] ⇄ res ✓ message.action … channel=telegram`; an accepted `openclaw message send`. (Pairing was established earlier the same day — the operator `/start`ed the `ledger` bot, clearing the prior re-proof's layer 2; the evening-sweep's `outbound send ok messageId=3` at 09:47 was the first proof.) |
| (b) **run record** | `/Users/ledger/.openclaw/workspace/memory/briefing-runs/2026-06-12.json` | ✅ written — `sent_at 2026-06-12T16:32:32-07:00`, `channel_delivery: "telegram"`, `composer: template` (no-data/day-one mode), message *"Good morning. Today — Friday, June 12. Nothing on the calendar today. Tomorrow: nothing scheduled"* |
| (a) **delivery-monitor `on_time` row** | `{shared_dir}/delivery_monitor/ledger/2026-06-12.jsonl` | ⚠️ **briefing now in scope** (delivery_contract present) — the prior re-proof had it *excluded* entirely; layer-3 observability fix confirmed. The recorded row for today's **07:00** window is `unmeasurable` (the briefing genuinely did not fire at 07:00 — the fix landed midday), and the 16:32 manual fire is off the 07:00 schedule so it produces no 07:00 row. A genuine `on_time` requires a delivery *within* the 07:00 window; the **first true `on_time` lands at the next 07:00** under the now-fixed script (or would require shifting the shared Spec's schedule, which this proof deliberately did **not** do). |

**Verdict (U1 — delivery leg).** **PROVEN.** A `ledger` morning briefing was composed by the fixed script and **delivered to the operator's Telegram DM** via the supported `openclaw message send` surface, with the per-day run record written (artifacts **b** + **c**). The delivery monitor now **scores** the briefing (the layer-3 exclusion is gone). The only thing the same-day run cannot show is an `on_time` *monitor classification*, because an honest `on_time` needs a delivery inside the 07:00 window — that arrives at the next 07:00, now that every blocker is fixed: the gallery send-path migration (#2695/#2792), the builtin re-seed propagation (#2795), the forge rebuild build_spec gap (#2803), and the operator's one-time DM pairing (done). All three original §The 07:00 window layers, plus the two new platform bugs the re-proofs surfaced, are now closed.

**`ledger`'s role.** `ledger` has now served its **U1** purpose end-to-end (creation → activation → first delivery). It remains the **U2 delivery-watcher drill** subject through **2026-06-18**, after which it retires (`evolve-admin retire-bot ledger`) — it is **not** retired here.
