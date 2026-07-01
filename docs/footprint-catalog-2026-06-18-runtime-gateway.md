# Footprint catalog — F-1b: runtime / hot-path (gateway plugin)

**Date:** 2026-06-18 · **Aspect:** `META:footprint` · **Slice:** F-1b (runtime / hot-path)
**Parent spec:** [docs/spec-footprint-2026-06-18.md](spec-footprint-2026-06-18.md)
**Sibling slices:** F-1a deploy/privilege · F-1c cost/monitors · F-1d config-mutation/appliers

This catalogs **every way Evolve runs *inside* or *intercepts* the bot's turn loop** — the
performance/latency tax of the gateway plugin (`packages/plugin`, TypeScript) loaded into the
OpenClaw process. Each item is tagged on the [four footprint dimensions](spec-footprint-2026-06-18.md)
(**Mutation** = changes how OC behaves · **Runtime** = intercepts the hot path · **Cost** = spends
tokens · **Privilege** = daemons/sudoers/ACLs) and its current toggle-state.

Everything here is grounded in code at the cited `file:line`. Where a memory note or in-code comment
claims a behavior that the code now contradicts, the contradiction is flagged in the notes column.

---

## 0. The master gate — the per-bot tier ladder

Before any hook fires, the plugin resolves a **per-bot integration tier** from
`openclaw.json`'s plugin-config `tier` field. This is the single most important runtime toggle —
it already implements most of a "posture dial" at the gateway layer.

| Tier | observer | injectPodConduct | injectKeywords | modelRouting | deferTool | recordApplicationTool |
|---|---|---|---|---|---|---|
| `off` | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `monitor` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `manage` | ✓ | ✓ | ✗ | ✓ | ✗ | ✓ |
| `full` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

Code: capability matrix [config.ts:40-45](../packages/plugin/src/config.ts#L40); resolution
[config.ts:97-152](../packages/plugin/src/config.ts#L97). **Default = `full`** — an `openclaw.json`
with no `tier` key, or an unknown value, resolves to `full` ([config.ts:113-117](../packages/plugin/src/config.ts#L113)),
i.e. *fail toward maximally-invasive*. The `tier=off` short-circuit and the `observer` gate are at
[index.ts:83-88](../packages/plugin/src/index.ts#L83). This default is in tension with the
spec's "default toward minimal" invariant — see Candidates.

---

## 1. Hot-path hooks (per-turn interception)

Hooks are registered in `TurnObserver.register()` ([TurnObserver.ts:1362](../packages/plugin/src/observer/TurnObserver.ts#L1362)).
All are registered via `api.on(<PluginHookName>, …)`. **Enabling precondition for every
conversation-content hook:** OC ≥ 2026.4.29 silently drops `agent_end`/`llm_output`/etc. unless the
bot's `openclaw.json` has `plugins.entries.evolve.hooks.allowConversationAccess: true` (set by the
deploy path — see [[project_oc_per_bot_hook_optin]]). That flag is the upstream-native opt-in switch
for the entire capture layer.

| Touch / subsystem | What it does (per turn) | Dimensions | Toggle-state | Code refs | Perf / cost / bug notes |
|---|---|---|---|---|---|
| **Plugin `register()`** | Resolves config, attaches all hooks + tools to the api instance. Re-runs on *every* api instance OC creates (per-session/periodic), guarded by a `WeakSet` so same-instance re-init no-ops. | Runtime | Gated by `tier` (`off` ⇒ nothing; `observer=false` ⇒ nothing) | [index.ts:56-103](../packages/plugin/src/index.ts#L56) | The WeakSet replaced an April-2026 module-level boolean that caused *new* api instances to skip hooks (May regression: `hooks:0ms`, no handlers ran). |
| **`llm_output`** | After each LLM call: accumulates per-session model/usage; runs `SessionCostMonitor.recordCost` (per-session budget breaker). | Runtime | Gated by `observer` (`monitor`+). Always on at that tier. | [TurnObserver.ts:1402-1469](../packages/plugin/src/observer/TurnObserver.ts#L1402) | Off the user's critical path (fires post-response). `recordCost` resolves the cap by reading `openclaw.json` **every call** ([TurnObserver.ts:1161-1166](../packages/plugin/src/observer/TurnObserver.ts#L1161)) — sub-ms but un-cached by design. Cap is opt-in (`agents.defaults.sessionBudgetCapUsd`); null ⇒ no-op. **Bug note:** OC 2026.4.29's embedded runner does not invoke this for channel-driven turns ([TurnObserver.ts:1393-1401](../packages/plugin/src/observer/TurnObserver.ts#L1393)) — authoritative cost now flows through `cost_event_converter.py` reading OC's turns JSONL. |
| **`agent_end`** | After each completed turn: polls up to **500 ms** for `llm_output` data to land, then `handleTurn()` — the heavy annotation path (classification, struggle/pushback/triviality detectors, cascade-telemetry span write, session aggregator, async judge dispatch, defer/manifest reflex). | Runtime, Cost | Gated by `observer` (`monitor`+) | hook [TurnObserver.ts:1479-1495](../packages/plugin/src/observer/TurnObserver.ts#L1479); 500 ms poll loop [1486-1489](../packages/plugin/src/observer/TurnObserver.ts#L1486) | Post-response notification — user already has their answer, so the 500 ms poll is not user-perceived latency, but it **holds the turn-complete event** and does real work synchronously before returning. Cost surfaces inside it: the LLM tier-classifier ([TurnObserver.ts:2992](../packages/plugin/src/observer/TurnObserver.ts#L2992)) and the async session-struggle judge (fire-and-forget, 3 s timeout). |
| **`session_end`** | `handleSessionEnd` → flushes deferred summary timer, runs `SessionSummarizer` (may make a tier-3 LLM call to extract session outcome). | Runtime, Cost | Gated by `observer` (`monitor`+); LLM summarization gated by `enableLLMSummarization` (default true) + `summarizerMinTurns` (default 2) | hook [TurnObserver.ts:1499-1505](../packages/plugin/src/observer/TurnObserver.ts#L1499); config [config.ts:69,81,132](../packages/plugin/src/config.ts#L69) | Summarizer also runs on an 8 s idle timer so single-turn cron sessions still get summarized ([TurnObserver.ts:1043-1055](../packages/plugin/src/observer/TurnObserver.ts#L1043)). |
| **`session_start`** | Runs `session_surface.py` as a **child subprocess** (`execFile`, Python via the shared venv) to build the pod-conduct + pending-task `systemAppend`. | **Mutation** (alters the bot's system prompt), Runtime, Cost (process spawn) | Gated by `injectPodConduct \|\| injectKeywords` (`manage`+) | hook [TurnObserver.ts:1372-1386](../packages/plugin/src/observer/TurnObserver.ts#L1372); subprocess [TurnObserver.ts:1905-1949](../packages/plugin/src/observer/TurnObserver.ts#L1905) | Subprocess spawn on the **first turn of every session** — real fork/exec + Python interpreter start cost. Venv-aware fallback to system python3 ([TurnObserver.ts:114-122](../packages/plugin/src/observer/TurnObserver.ts#L114)). This is the literal "Evolve changes the bot's persona" injection. |
| **`before_model_resolve`** | The routing-rewrite hook. (1) Parses operator tier directive from the message envelope; (2) attempts **manifest-trigger interception** (see §2); (3) Better-Engine keyword injection (`full` only); (4) `ModelRouter` rewrite returning `modelOverride` / `authProfileOverride`. | **Mutation** (rewrites which model + auth profile the turn uses), Runtime, Cost (changes the bill; preflight haiku call) | Hook self-skips unless `modelRouting \|\| injectKeywords` ([TurnObserver.ts:1688-1692](../packages/plugin/src/observer/TurnObserver.ts#L1688)) ⇒ `manage`+. Routing branch gated `modelRouting`; keyword branch gated `injectKeywords` (`full`). | hook [TurnObserver.ts:1693-1874](../packages/plugin/src/observer/TurnObserver.ts#L1693); routing [resolveModelRouting TurnObserver.ts:4016-4075](../packages/plugin/src/observer/TurnObserver.ts#L4016); ladder [ModelRouter.ts:2701-2812](../packages/plugin/src/observer/ModelRouter.ts#L2701) | **On the user's critical path** — runs before the model call. This is the single biggest behavior-mutating interception: it can downgrade (runaway/spend-cap → `fast`), honor a user pull (`fast`/`standard`/`power`/`max`), apply the cascade verdict, or apply operator/per-user defaults. Master kill-switch: `routing.enabled` in `evolve-tiers.json`/`network.json` (default **true** — [TurnObserver.ts:1293](../packages/plugin/src/observer/TurnObserver.ts#L1293); honored [ModelRouter.ts:2703](../packages/plugin/src/observer/ModelRouter.ts#L2703)). |
| **`before_agent_run`** | Zero-token keyword short-circuit: detects `evo` keywords / follow-ups from the user message, stashes injection text for `before_model_resolve` to deliver. Always returns `{outcome:"pass"}`. | Runtime, Mutation (stages an injection) | Gated by `injectKeywords` (`full`), wrapped in try/catch for OC versions lacking the hook | [TurnObserver.ts:1882-1892](../packages/plugin/src/observer/TurnObserver.ts#L1882); handler [TurnObserver.ts:4146](../packages/plugin/src/observer/TurnObserver.ts#L4146) | Must return explicit `pass` — current OC normalizes null/undefined to `block`, which previously turned every user message into "blocked by evolve". |
| **`before_prompt_build`** | Injects `appendSystemContext` **every turn**: (a) stay-silent directive when the plugin direct-sent (evo); (b) LLM-echo directive; (c) per-turn Home-narrative banner (primary only); (d) per-turn speaker-context block (who's speaking + roster role). | **Mutation** (rewrites the system prompt every turn), Runtime, Cost (added prompt tokens) | Gated by `injectKeywords` (`full`) | [TurnObserver.ts:1575-1668](../packages/plugin/src/observer/TurnObserver.ts#L1575) | This is the field OC *actually consumes* (pi-embedded silently drops `systemAppend` from `before_model_resolve` — [TurnObserver.ts:1560-1567](../packages/plugin/src/observer/TurnObserver.ts#L1560)). Per-turn narrative + speaker blocks add system-prompt tokens to **every** `full`-tier turn. |
| **`before_agent_reply`** | Suppresses the LLM's reply entirely (`handled:true`) when the plugin already direct-sent the user-visible message for that run. | **Mutation** (drops the model's output) | Gated by `injectKeywords` (`full`) | [TurnObserver.ts:1528-1558](../packages/plugin/src/observer/TurnObserver.ts#L1528) | **Largely dead in current OC:** pi-embedded gates this hook on `trigger==="cron"`, so it never fires for Telegram user turns — kept as defensive code; the real suppression surface is `before_prompt_build` (note in [1532-1542](../packages/plugin/src/observer/TurnObserver.ts#L1532)). |

---

## 2. Hot-path sub-interceptions (inside the hooks above)

| Touch / subsystem | What it does | Dimensions | Toggle-state | Code refs | Perf / cost / bug notes |
|---|---|---|---|---|---|
| **Manifest-trigger interception** (Layer C, agent-freelance-bypass) | In `before_model_resolve`: matches the user message against the bot's compiled `event_triggers[]` (from `plugin_intercept` manifests); on match, **spawns `python3 <script>` synchronously** (25 s hard timeout), direct-sends the script's reply, and stay-quiets the LLM. | **Mutation** (the LLM never sees the message), Runtime, Cost (script may itself call an LLM) | Gated by presence of `plugin_intercept` manifests for the bot (empty list ⇒ O(1) fall-through). Runs inside the `modelRouting \|\| injectKeywords` path. Inactive manifest statuses (paused/hidden/dormant/deprecated) are skipped. | intercept [TurnObserver.ts:1825-1839](../packages/plugin/src/observer/TurnObserver.ts#L1825), [2298](../packages/plugin/src/observer/TurnObserver.ts#L2298); subprocess [TurnObserver.ts:2262-2284](../packages/plugin/src/observer/TurnObserver.ts#L2262); status gate [TurnObserver.ts:603-619](../packages/plugin/src/observer/TurnObserver.ts#L603) | **On the user's critical path with a 25 s timeout** — the heaviest single hot-path interception when active. Trigger cache keyed on manifests-dir mtime; cold-path re-scan throttled. Fails open (logs, falls through to legacy handling). |
| **Pre-flight intent router** (haiku layer) | In `handleBeforeModelResolve`: classifies the turn's intended tier via bot-prior → regex → **haiku LLM call** (2 s hard timeout, abstain on timeout). Stores a per-session decision the routing ladder consults. | Runtime, **Cost** (haiku call), Mutation (biases tier selection) | Runs only inside the `injectKeywords` (`full`) path. Per-bot gate `_isPreflightEnabled()` (pod `cascade.preflight.enabled`, default on; per-bot override). Haiku sub-layer gated by `_isHaikuEnabled()` (default on, per-bot opt-out). | call site [TurnObserver.ts:4625-4645](../packages/plugin/src/observer/TurnObserver.ts#L4625); layers [PreflightIntentRouter.ts:505-585](../packages/plugin/src/observer/PreflightIntentRouter.ts#L505); enable gate [TurnObserver.ts:906-935](../packages/plugin/src/observer/TurnObserver.ts#L906) | **STALE-COMMENT FLAG:** many in-code comments (e.g. [TurnObserver.ts:4592-4598](../packages/plugin/src/observer/TurnObserver.ts#L4592), `resolveModelOverride` step 4b [ModelRouter.ts:2795-2796](../packages/plugin/src/observer/ModelRouter.ts#L2795)) still say "Phase 1 — always ABSTAIN, dead in production." The code at [PreflightIntentRouter.ts:579-583](../packages/plugin/src/observer/PreflightIntentRouter.ts#L579) now has live regex + bot_prior + **haiku** layers, and `classify()` is wired (TurnObserver.ts:4627). When haiku fires this is a real **per-turn LLM call on the critical path** (≈+150 ms p50 per the file's own note). Treat the "abstain-only" comments as outdated. |
| **ModelRouter safety nets** | Per-turn precedence ladder: runaway-rate hard cap → spend-cap → user pull → cascade → classifier/operator-default. Each can rewrite the model. | Mutation, Runtime | Always active when `modelRouting` on and `routing.enabled !== false`. Spend-cap reads `isSpendCapActive` (file-backed flag). | [ModelRouter.ts:2701-2812](../packages/plugin/src/observer/ModelRouter.ts#L2701) | Pure in-memory + occasional file reads; cheap relative to the LLM call it precedes. The runaway/spend-cap branches are *safety* mutations (force `fast`) — disabling them lowers a cost-safety floor, not just a feature. |
| **Better-Engine keyword injection** | `evo` keyword / follow-up / contextual-hint detection → `systemAppend` the LLM echoes; some paths direct-send via channel transport. | Mutation, Runtime, Cost | Gated by `injectKeywords` (`full`) | [TurnObserver.ts:1845-1867](../packages/plugin/src/observer/TurnObserver.ts#L1845) | Talks to the admin server on loopback:5050 for recommendation/evo dispatch; TTL-cached evo block avoids an HTTP call every turn. |

---

## 3. Per-turn IO / ledgers (additive, off critical path)

| Touch / subsystem | What it does | Dimensions | Toggle-state | Code refs | Perf / cost / bug notes |
|---|---|---|---|---|---|
| **Cascade telemetry span** | One Opik-shaped span `appendFileSync`-ed per turn to `{sharedDir}/{botId}/spans/`. | Runtime (IO) | Gated `observability.cascade_telemetry.enabled` (default **true**) + non-`unknown` botId | construct [TurnObserver.ts:1108-1117](../packages/plugin/src/observer/TurnObserver.ts#L1108); write [CascadeTelemetry.ts:333](../packages/plugin/src/observer/CascadeTelemetry.ts#L333); kill-switch [TurnObserver.ts:1183-1191](../packages/plugin/src/observer/TurnObserver.ts#L1183) | One synchronous file append per turn (in `agent_end`, off the user's path). Struggle is still computed + mirrored into the annotation even when emission is killed. |
| **Outward-action ledger** | One line per MCP tool call per turn (names + ids only) to `{sharedDir}/{botId}/outward-actions/`. Feeds the autonomy-ladder rung-3 caps. | Runtime (IO) | **Unconditional** when botId known — *no kill-switch by design* ([TurnObserver.ts:733-742,1119-1126](../packages/plugin/src/observer/TurnObserver.ts#L733)) | [TurnObserver.ts:1119-1126](../packages/plugin/src/observer/TurnObserver.ts#L1119) | Intentionally un-gateable: a kill-switch here would silently disable operator-set autonomy limits. A "passive" posture must reconcile with this (the cap it backs is itself opt-in). |
| **Struggle-payload sampler** | Shape-only snapshot of `event.messages` on up to 20 `success=false`+score-0.5 turns/day. | Runtime (IO) | Self-capping diagnostic (`STRUGGLE_SAMPLE_DAILY_CAP=20`/bot/day); intended to be removed | [TurnObserver.ts:445-585](../packages/plugin/src/observer/TurnObserver.ts#L445) | Temporary diagnostic; never preserves content. |
| **Cost ledger** | `type:"cost_event"` row appended to annotations JSONL per `llm_output`. | Runtime (IO) | Gated `costLedgerEnabled` (default true) | [config.ts:88-89](../packages/plugin/src/config.ts#L88) | "Disable-able if a gateway has IO pressure." |

---

## 4. Registered tools (context-surface footprint, not per-turn hooks)

Each tool registered adds a tool definition to the agent's context (system-prompt token cost on
every turn) and a callable mutation surface — a footprint even though it isn't a hook. All gated by
capability/role at [index.ts:111-201](../packages/plugin/src/index.ts#L111).

| Tool(s) | Dimensions | Toggle-state | Code refs |
|---|---|---|---|
| `defer` | Cost (tokens), Mutation | Gated `deferTool` (`full`) | [index.ts:111-113](../packages/plugin/src/index.ts#L111) |
| `session.set_tier` | Cost, Mutation (routing) | Gated `modelRouting` (`manage`+) | [index.ts:122-129](../packages/plugin/src/index.ts#L122) |
| `record_application` | Cost, Mutation | Gated `recordApplicationTool` (`manage`+) | [index.ts:138-142](../packages/plugin/src/index.ts#L138) |
| Primary tools (`evolve_help_*`, `submit_intake`, pod-state reads ×8) | Cost | Gated `role==="primary"` | [index.ts:157-175](../packages/plugin/src/index.ts#L157) |
| Roster tools (`roster_set_role`/`block`/`unblock`, `channel_set_newcomer_mode`) | Cost, Mutation | **Unconditional on every bot** (capability check deferred to the admin daemon) | [index.ts:188-201](../packages/plugin/src/index.ts#L188) |

---

## 5. Rate-limiting-per-sender / other per-turn middleware

There is **no plugin-level per-sender rate-limit middleware** in the gateway hot path. Rate limiting
exists only *inside individual app trigger scripts* (research / capture apps), surfaced back to the
plugin as `RESEARCH_RATE_LIMITED:` / `CAPTURE_RATE_LIMITED:` stdout protocol outcomes parsed at
[triggerProtocols.ts:142-145,204-207](../packages/plugin/src/observer/triggerProtocols.ts#L142). Per
memory [[project_rate_limit_per_sender_as_bot_primitive]], lifting per-sender state to a bot-level
channel-boundary primitive is *planned, not yet built* — so today it is per-app, not a uniform turn
gate. Worth tracking as a future hot-path interception when it lands.

---

## Candidates to make dialable

What a low-footprint ("Passive / dashboard mode") operator could turn off at the runtime layer, and
the capability lost. The good news: **the tier ladder already provides most of the dial** — the gap
is the default and a couple of unconditional items.

1. **Flip the default tier from `full` → `monitor` (or make it posture-driven).** Today a
   `tier`-less `openclaw.json` lands on `full` ([config.ts:113-117](../packages/plugin/src/config.ts#L113)) —
   the opposite of the spec's "default toward minimal." A Passive posture should map to `monitor`
   (capture-only, zero injection, zero routing, zero hot-path mutation) and only `manage`/`full`
   at Standard/Managed. *Lost at `monitor`:* model routing, pod-conduct/keyword injection, the
   defer + record_application tools. The bot's behavior becomes pure-OpenClaw; Evolve still
   populates dashboards. **This is the single highest-leverage runtime dial** and it already exists —
   it just needs to be the default and surfaced.

2. **`routing.enabled: false` as the Passive default for model-tiers.** Even within `manage`/`full`,
   the routing rewrite is the biggest behavior+cost mutation on the critical path. `routing.enabled`
   ([ModelRouter.ts:2703](../packages/plugin/src/observer/ModelRouter.ts#L2703), default true) already
   short-circuits the whole ladder. *Lost:* tier-cascade optimization, user/operator tier pulls,
   account routing. **Retained safety caveat:** the runaway-rate + spend-cap branches live *inside*
   this ladder — turning routing off also removes those cost-safety forced-downgrades, so a Passive
   posture should either keep the safety nets on a separate gate or accept that floor moves. Co-own
   with `edr`/`model-tiers`.

3. **Pre-flight haiku layer off in Passive/Standard.** `cascade.preflight.enabled` /
   `_isHaikuEnabled()` already give a per-bot opt-out ([TurnObserver.ts:906-935](../packages/plugin/src/observer/TurnObserver.ts#L906)).
   A low-footprint operator turning this off removes a **per-turn haiku LLM call on the critical
   path** (≈+150 ms p50, plus tokens). *Lost:* per-turn intent-based tier prediction for ambiguous
   prompts (the legacy in-session classifier still runs at `agent_end`). **Also: fix the stale
   "abstain-only" comments** so the catalog/operator isn't misled about whether this call fires.

4. **Manifest-trigger interception is data-driven, not posture-driven.** It only runs when the bot
   has `plugin_intercept` manifests, but when active it is a synchronous 25 s-timeout subprocess on
   the critical path. A Passive posture that disables `plugin_intercept` (route those apps through
   the slower agent-tool path instead) removes the spawn from the hot path. *Lost:* deterministic,
   freelance-proof app dispatch. Route the toggle to `apps`/`skills`.

5. **Per-turn `before_prompt_build` injection (narrative + speaker context).** At `full` tier this
   adds system-prompt tokens to **every** turn. Could be reducible to session-start-only (the
   pre-2026-05 behavior) under Standard. *Lost:* live per-turn refresh of the Home banner and
   speaker/role grounding. Route to `evo-asst`/`users`.

6. **`session_start` pod-conduct subprocess.** The Python `session_surface.py` fork/exec on the
   first turn of every session is the clearest "Evolve mutates the persona" touch. Already gated by
   `injectPodConduct` (off at `monitor`). No new dial needed — it falls out of recommendation #1.

7. **Items that resist a simple toggle (flag for the contract, don't naively expose):**
   - **Outward-action ledger** ([TurnObserver.ts:1119-1126](../packages/plugin/src/observer/TurnObserver.ts#L1119))
     is deliberately un-gateable because it backs operator autonomy caps. A Passive posture must
     decide whether autonomy caps exist at all rather than silently half-disabling them — exactly the
     "toggle that leaves a subsystem half-on is worse than no toggle" guardrail.
   - **Roster tools registered unconditionally on every bot** ([index.ts:188-201](../packages/plugin/src/index.ts#L188))
     add tool-surface tokens even at `monitor`/`manage` where the rest of the injection layer is off.
     A Passive posture arguably shouldn't carry mutation tools; the capability check being deferred
     to the admin daemon is a privilege-layer decision, but the *context-token* footprint is paid on
     every bot regardless. Candidate to gate behind `role`/capability.

**Net:** the gateway already has a real, code-level posture mechanism (the tier ladder + a handful of
named kill-switches: `routing.enabled`, `cascade_telemetry.enabled`, `preflight.enabled`,
`costLedgerEnabled`, `allowConversationAccess`). The F-3 posture-dial work is mostly (a) flipping the
**default** toward minimal, (b) mapping the named switches to coherent posture levels, and (c)
deciding the two un-gateable items (outward-action ledger, unconditional roster tools) and the
safety-net-vs-routing coupling explicitly rather than by accident.
