# Add-Bot Wizard — Build Delta (2026-06-10)

**Status:** decision doc (Round 1 spec session per [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §8)
**Bases:** [spec-add-bot-wizard-2026-05-28.md](spec-add-bot-wizard-2026-05-28.md) (the form-wizard spec; PR α/β/γ plan) + [reference-conversational-bot-creation-2026-05-19.md](reference-conversational-bot-creation-2026-05-19.md) (the frozen conversational design). This is a **delta** describing what changes to build the conversational wizard *now* — not a rewrite of either base.
**Locked decisions (design sync 2026-06-10, not relitigated here):** build now; **evo-hosted** (the primary bot hosts the conversation, per [spec-primary-bot-interface-2026-05-14.md](spec-primary-bot-interface-2026-05-14.md)); **Claude-assumed** (per the multi-runtime deferral, [spec-multi-runtime-bots-2026-05-31.md](spec-multi-runtime-bots-2026-05-31.md) §5 — no runtime question in the wizard); **Morning Briefing default-on** for new bots with a channel, opt-out during creation (ships in code per product-defaults-in-code); **consent + tone non-skippable** per the reference design.

---

## 1. What's already built — the delta's starting line

The 05-28 spec planned PRs α→β→γ→δ→ε. Status check against main:

| 05-28 plan | State, 2026-06-10 | Where |
|---|---|---|
| **PR α** — `provision-bot` CLI (user/UID/port/onboard/deploy pipeline + rollback) | **Shipped** | `provision-bot` in [cli.py](../packages/admin/evolve_admin/cli.py) |
| **PR β** — wizard backend endpoints + credential borrow | **Shipped** — `/api/wizard/preview`, `/api/wizard/provision` (async job), `/api/credentials/borrow`, `/api/wizard/borrow-candidates` | [wizard_routes.py](../packages/admin/evolve_admin/web/wizard_routes.py) |
| **PR γ** — 5-screen form UI in the admin SPA | **Not built** | — |
| **PR δ** — save-as-template + sanitizer | Not built (still out of scope) | — |
| **PR ε** — chat-driven layer | **Its substrate exists.** The evo wizard *engine* — phase machine, per-user state, LLM extraction, verbatim/agenda render modes, abandon-stale-session handling — is live and already runs multiple conversational chains (setup wizard, guide authoring, forge-from-messaging, app-create, provider setup) | [evo/wizard/](../packages/admin/evolve_admin/evo/wizard/) — `engine.py`, `phases.py`, `state.py`, `extractor.py` |
| Extended bot-template format (§3) + 5 built-in archetype templates (§3.2) | Format **not** extended; only `morning-briefing` + `test-minimal` exist | [gallery/bot-templates/](../gallery/bot-templates/) |

So the build is **not** "PR ε someday, after γ": the conversation host, the backend, and the provisioning substrate all exist. What's missing is the add-bot **phase chain**, the **purpose/pack/briefing/consent content** of the conversation, and the decided defaults.

## 2. The delta, in one table

| Dimension | 05-28 spec said | Build now |
|---|---|---|
| Primary surface | Form-driven 5-screen modal; chat layer deferred to ε | **Conversational, evo-hosted** — a new phase chain on the existing evo wizard engine, reachable as `evo add-bot` (admin-only; see §3.1). The form wizard (γ) is demoted — see §2.1 |
| Runtime question | (implicit Claude) | **Explicitly none.** Claude-assumed per the multi-runtime memo; the only auth question is the Anthropic credential (borrow / paste / skip), via the shipped borrow endpoint |
| Opening question | Screen 1 "What do you want this bot to do?" + template picker | **Why-before-what** per the reference design: need → audience → role, *then* shape. Purpose capture (§4) is the anchor, not a description textarea |
| Apps step | Screen 5 checkbox list from template | **Archetype starter pack** preselected from the purpose archetype (§5), adjustable in conversation |
| Proactive default | — | **Morning Briefing default-on** with an explicit opt-out turn (§6) |
| Consent + tone | Privacy-mode checkbox (Telegram-specific) | **Non-skippable consent + tone phases** for any bot with an audience (§7) |
| Naming | Screen 2, second step | **Deferred until role is locked**, proposed with rationale (reference design Phase 3) |

### 2.1 What happens to PR γ (the form UI) — options

- **A. Build γ first, chat after.** The 05-28 sequence. Rejected by the design sync: it delays the U1 activation proof behind a UI that the conversational path doesn't need.
- **B. Drop γ entirely.** Cheapest, but leaves the admin UI with no add-bot affordance at all, and the form remains the right floor for operators who hate chat (the 05-28 spec's "both layers coexist" reasoning still holds).
- **C. Defer γ; ship a thin admin-UI entry point now** — an "Add a bot" button that explains the bot is created in conversation with your main bot and deep-links/copies the `evo add-bot` invocation. β endpoints stay UI-ready so γ can land later unchanged.

**Recommendation: C.** The β API contract is the floor now; the form is a later convenience, not the foundation. (Plex-test note for that entry point: the copy says "your main bot", never "evo dispatch" or "primary-bot interface".)

---

## 3. The conversation — phase chain on the existing engine

### 3.1 Hosting and entry

A new chain in [evo/wizard/phases.py](../packages/admin/evolve_admin/evo/wizard/phases.py), entered via a new `add-bot` subcommand in the [evo registry](../packages/admin/evolve_admin/evo/subcommands.py), **admin-role only** (creating bots is a pod-operator action; the registry's role gates already model this). The existing `wizard` subcommand keeps its meaning — *user setup for this bot* — untouched; the two are different conversations and must not share a name. Engine mechanics are reused as-is: `agenda` render mode for the open-ended phases (the LLM converses, the extractor fills targets), `verbatim` for the deterministic ones (plans, credential checklists, progress), per-user state with resume, and the abandon-stale-session pass.

Privileged operations (user creation, network.json writes, deploys) never run in evo's process: every mutating step calls the shipped PR β endpoints / admin-daemon boundary, consistent with the evo account-separation posture. The chain is a *driver* of `provision_bot()` and friends, exactly as the reference design's implementer notes prescribe.

### 3.2 The chain

| Phase | Mode | Captures / does | Base |
|---|---|---|---|
| `AB_NEED` | agenda | The underlying need and who it serves; research-assistant vs co-pilot framing | Reference Phase 1 (why before what) |
| `AB_AUDIENCE` | agenda | Who else interacts; one feed or several; surfaces | Reference Phase 2 |
| `AB_PURPOSE` | agenda | Confirms `purpose{archetype, mission}` back to the operator in plain words (§4) | Effectiveness layer §4 |
| `AB_SHAPE` | agenda | Integrations follow role; apps follow audience → starter pack proposal (§5), adjusted conversationally | Reference Phase 4 |
| `AB_BRIEFING` | agenda | Briefing default-on, explicit opt-out turn (§6) — only offered when a channel exists | Design-sync decision |
| `AB_CONSENT_TONE` | agenda, **non-skippable** | Consent posture + tone (§7) | Reference Phase 5 |
| `AB_NAME` | agenda | Names proposed with rationale, after role is locked; bot-name ≠ account-name surfaced | Reference Phase 3 |
| `AB_PLAN` | verbatim | The full plan echoed for confirmation (bot, surface, apps, briefing, consent, credentials needed) | Reference transcript |
| `AB_CREDENTIALS` | agenda | Walks each credential: borrow (via `/api/credentials/borrow`) / paste / skip; validates before moving on | 05-28 §6 |
| `AB_PROVISION` | verbatim | Drives `/api/wizard/provision`, relays stage progress, on failure offers retry-from-step or rollback (PR α semantics) | 05-28 §5 |
| `AB_SMOKE_WRAP` | verbatim | Smoke-test DM + result; never leaves a broken bot — rollback or quarantine on failure | Reference implementer notes |

Exit conditions, back-tracking ("actually, make it weekly"), and partial-resume all come free from the engine's existing state machinery. **Failure honesty rule:** provision-stage errors are relayed truthfully (what failed, what was rolled back) — no "fixed itself" messaging, per the U2 thread of the roadmap.

## 4. Purpose capture — the anchor

Adopt the field **exactly as specced in [spec-effectiveness-layer-2026-06-09.md](spec-effectiveness-layer-2026-06-09.md) §4** — same keys, same archetype enum (`personal-assistant`, `project-manager`, `home-automation`, `research-analyst`, `customer-facing`, `custom`), no parallel shape:

```jsonc
"purpose": {
  "archetype": "research-analyst",
  "mission": "Watch the ecosystem and post a daily digest to the group.",
  "captured": "declared",
  "confidence": 1.0,
  "reviewed_at": "2026-06-10T…"
}
```

The wizard is the **declared** path (`confidence: 1.0`); inference backfill for existing bots is the other half of U1.1 and belongs to the effectiveness-layer build, not this spec.

**Storage options:** (a) the bot's block in `network.json`; (b) a per-bot file in the bot's evolve workspace. **Recommendation: (a).** `network.json` is the explicit source of truth for pod membership and per-bot metadata; purpose is read pod-wide (tiles, Layer-2 reviewer, starter-pack selection) and written rarely, by the admin server on the wizard's behalf — the same write path that registers the bot. The effectiveness-layer spec left this open; this doc proposes closing it as (a) at the design sync.

One vocabulary, three consumers: the archetype the wizard captures **is** the key for starter packs (§5) **and** the Layer-2 playbook selector. No mapping tables.

## 5. Archetype starter packs

Mechanism options for role → preinstalled app set:

- **A. Meta-spec bundles** — one gallery package per archetype that declares its apps as `app_dependencies` and ships no scripts. The pattern exists and is proven: the EA Pack bundle already installs its seven dependencies through the gallery dependency machinery (`required`/`reason` per dep, ordered install — see [gallery/index.json](../gallery/index.json) and the morning-briefing `app_dependencies` block).
- **B. Extended bot-templates** (05-28 §3 suggestive mode) — finish the template-format extension and ship the five archetype templates.
- **C. Code map** — archetype → list of `pkg_id`s in the wizard config.

**Recommendation: A, with B deferred.** Bundles reuse shipped machinery, are visible/editable in the gallery like any app, keep cost preview honest (the dep list *is* the build list), and avoid finishing the template-format work just to express an app list. The 05-28 template extension stays the right vehicle for *strict-mode CLI* deploys and operator-authored archetypes (PR δ) — later.

Starter map (v1, adjusted conversationally in `AB_SHAPE`):

| Archetype | Pack contents (existing gallery apps) |
|---|---|
| `personal-assistant` | EA Pack (Morning Briefing, Evening Sweep, Pre-Meeting Brief, Commitment Tracker + data foundations) — or the briefing-centered subset when the operator wants lighter |
| `project-manager` | Commitment Tracker, Task Manager, Note-Taker, Pre-Meeting Brief |
| `research-analyst` | The community-research pattern (daily-digest, article-capture, on-demand-research) — these apps exist as the side-loaded reference set, not gallery entries yet; the pack ships when they're promoted into the gallery, plus watcher templates as U2.4 lands |
| `home-automation` | (thin today — pack ships when the gallery has the apps; the wizard says so honestly rather than padding) |
| `customer-facing` / `custom` | No pack; gallery browse offered |

Apps queue as forge jobs at finalize (unchanged 05-28 §7 machinery); cost/time preview is conversational: "Four apps, about two minutes and $1.60 to build — OK?"

## 6. Morning Briefing — default-on, opt-out at creation

Decided; this section is mechanics, not relitigation.

- **Trigger:** any new bot with a messaging channel configured gets Morning Briefing queued at finalize and its schedule registered so the **first delivery lands within 24h** — the U1 activation metric.
- **Opt-out is an explicit turn**, not buried: *"One default I'll set up: a short morning briefing — your schedule and anything that needs you, delivered here each morning. Want it? You can say no, or change the time later."* Declining at creation is recorded; nothing nags.
- **Ships in code:** the default lives in the wizard/finalize path, never as a per-pod proposal (product-defaults-in-code; a fresh install must not render dead affordances).
- **Degraded-content requirement (the honest part):** a day-1 bot usually has no calendar/email integration yet. The briefing's gallery deps already degrade (email is optional; calendar reads empty until creds), but "empty briefing" fails the point. Requirement on the U1 build: the first briefings must say something useful with zero integrations — what the bot is set up to do, what it's waiting on (e.g., "connect a calendar and I'll put your day here"), anything it observed. If the current briefing app can't do that without contortions, the U1 track owns a small "no-data mode" addition to it — flagged, not hidden.

## 7. Consent + tone — non-skippable

Per the reference design Phase 5, the wizard never finishes a bot with an audience without explicit answers to:

1. **Consent:** who can see what this bot collects, and how they opt out. For group-surface bots: a pinned intro notice + an opt-out gesture (the reference design's reaction-to-exclude pattern); for personal bots: the observation/DNT posture stated plainly. Recorded in the bot's conduct/guide files via the existing `apply_template` stage. When manifest Slice 2 lands (`privacy{}` first-class — [spec-manifest-v7-slicing-2026-06-10.md](spec-manifest-v7-slicing-2026-06-10.md) §4), the wizard's consent answers also populate the installed apps' `privacy.consent_notice` — until then, conduct prose is the single home (a known, bounded double-write window; flagged in both docs).
2. **Tone:** the pod's message style (short header, one fact per line, conversational close — per [operator-message-style.md](operator-message-style.md)) confirmed or adjusted per-bot, landing in the bot's SOUL/conduct files.

Telegram's privacy-mode decision (05-28 friction #6) folds into the consent phase with plain-English framing: *"Should the bot see every message in the group, or only messages that mention it?"*

## 8. Copy — the Plex test, applied

All wizard copy is a primary surface under [principle-plex-test.md](principle-plex-test.md): no "provision", "forge", "manifest", "gateway", "plugin install", "RSI" — the operator hears "setting up the account", "building the app", "checking the connection". Progress lines name outcomes, not stages (`✓ Bot account created`, not `✓ dscl user + createhomedir`). Sample register (matching the reference transcript, which remains the canonical example):

```
evo:  Before we pick apps — who is this bot for, just you or a group?
op:   A Telegram group of enthusiasts. And me — I want the research privately too.
evo:  Two audiences then. Should they see the same content, or do you
      want a private feed for things only you see?
...
evo:  One default I'll set up: a short morning briefing, delivered here
      each morning. Want it?
```

Acceptance: a copy review pass against the principle doc is part of every milestone below, and the smoke-test transcript in M4 is reviewed for jargon before the proof artifact is accepted.

## 9. Build plan — session-sized milestones, each with a proof artifact

| # | Session scope | Proof artifact |
|---|---|---|
| **M1** | `evo add-bot` chain skeleton: registry entry (admin-only), phases `AB_NEED → AB_PURPOSE → AB_NAME → AB_PLAN`, extraction targets, purpose written to the chosen store. No provisioning yet | Transcript: a conversation that ends with a confirmed plan + a `purpose{}` block persisted with `captured: "declared"` |
| **M2** | Provisioning from conversation: `AB_CREDENTIALS` (borrow/paste/skip via shipped endpoints) + `AB_PROVISION` driving `/api/wizard/provision` with stage relay, retry, rollback; `AB_SMOKE_WRAP` | A fresh bot created end-to-end conversationally on the dev pod; a deliberately failed stage shown rolling back honestly in-transcript |
| **M3** | Defaults: archetype bundle packages in the gallery + `AB_SHAPE` pack proposal; `AB_BRIEFING` default-on/opt-out; `AB_CONSENT_TONE` non-skippable; admin-UI entry point (§2.1 option C) | A bot whose starter pack installed via dependency machinery, briefing scheduled, consent + tone recorded; an opt-out run recorded too |
| **M4** | **The U1 proof** (roadmap §5): fresh bot, created conversationally, purpose captured, starter pack installed, **first briefing delivered within 24h**; copy pass against the Plex principle | The transcript + delivery evidence written up as a decision doc (the Atlas-session pattern) — this is the roadmap's U1 proof artifact |

M1/M2 are sequential; M3's three defaults are parallelizable within the session; M4 is calendar-bound by the 24h delivery. The 05-28 spec's test plan (§8) still governs α/β regressions; new tests follow the engine's existing phase-chain test pattern.

## 10. Open questions

1. **Purpose storage** — confirm `network.json` bot block over a workspace file (§4; recommendation: network.json).
2. **Briefing time default** — fixed default (e.g., 7am local) vs. asked in `AB_BRIEFING`? Recommend: default 7am, mention it's changeable; don't add a turn.
3. **Secondary entry points** — should `evo add-bot` also be reachable from the admin UI chat surface (home chat), or messaging-channel evo only? Recommend: both — same chain, the engine doesn't care.
4. **Pack contents for `home-automation`** — ship the archetype with an honest "no pack yet" (recommended) or hold the archetype out of the wizard until apps exist?
5. **Does M4's 24h-delivery proof require the briefing no-data mode (§6) first?** If the current app's empty-state is unacceptable, the no-data work joins M3.
