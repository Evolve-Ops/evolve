/**
 * TurnObserver
 *
 * Hooks into every completed agent turn and writes a structured annotation
 * to the evolve annotation log alongside the standard turns JSONL.
 *
 * Annotation schema:
 * {
 *   turn_id:             string    — unique turn identifier
 *   session_id:          string    — session this turn belongs to
 *   ts:                  string    — ISO timestamp
 *   bot_id:              string    — which bot produced this turn
 *   session_class:       string    — "productive" | "maintenance" | "ambiguous"
 *   class_signals:       string[]  — keywords/patterns that drove classification
 *   class_confidence:    number    — 0-1 confidence in session class
 *   model_tier:          string    — legacy model tier (tier0-tier3); kept for transition
 *   model_role:          string    — model role selected (fast|standard|power|max|judge)
 *   model_selected:      string    — actual model string used (from routing decision)
 *   resolution_turn:     number    — which turn number this is (1=first turn)
 *   correction_detected: boolean   — was a correction signal present in the user message?
 *   task_id:             string    — groups turns belonging to the same session task
 *   input_tokens:        number    — from llm_output hook
 *   output_tokens:       number    — from turns log
 *   cache_write_tokens:  number    — from turns log
 *   cache_read_tokens:   number    — from turns log
 *   auth_mode:           string    — "token" | "api_key"
 *   cost_estimated:      number    — estimated cost (may be 0 on MAX)
 * }
 */
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { type EvolveSubagentTriggerKind } from "./subagentRun.js";
import { ModelRouter } from "./ModelRouter.js";
import { type TierChosenBy } from "./CascadeTelemetry.js";
import { type EvoDispatchFailureReason } from "../better/EvoDispatchClient.js";
export declare function evolvePythonBin(): string;
/**
 * Resolve the Evolve ``packages/analyzer`` directory — the dir holding the
 * standalone analyzer scripts the plugin spawns (session_surface.py) —
 * from plugin config.
 *
 * Prefers the explicit ``repoRoot`` (the deploy checkout, injected by
 * deploy.py from ``platform_profile.deploy_checkout_default``):
 *   - macOS  → /Users/Shared/evolve-repo
 *   - Linux  → /var/lib/evolve/repo
 *
 * Falls back to the legacy macOS-sibling derivation
 * (``dirname(sharedDir)/evolve-repo``) only when ``repoRoot`` is absent —
 * i.e. for bots whose openclaw.json was written before this key existed
 * and hasn't been redeployed yet.
 *
 * The legacy derivation is CORRECT on macOS (sharedDir=/Users/Shared/evolve
 * → /Users/Shared/evolve-repo, the real deploy checkout) but WRONG on Linux:
 * sharedDir=/var/lib/evolve → /var/lib/evolve-repo, whereas the deploy
 * checkout is /var/lib/evolve/repo (a CHILD of sharedDir, not a sibling).
 * On a live Linux pod that produced "can't open file
 * '/var/lib/evolve-repo/packages/analyzer/session_surface.py'" and the
 * per-turn capability block never rendered. The repoRoot key fixes that
 * without disturbing the macOS path.
 */
export declare function resolveAnalyzerDir(config: {
    repoRoot?: string;
    sharedDir: string;
}): string;
/**
 * Extract the last NON-EMPTY user and assistant message text from the
 * agent_end event's messages array. Content can be a plain string or an
 * array of content blocks ({type:"text", text:"..."}). We take the *last
 * non-empty text* of each role.
 *
 * The "non-empty" qualifier (added 2026-06-06 after pushback-detector
 * audit) matters because OC's agent loop frequently ends turns with
 * non-text content blocks:
 *
 *   user:      "what's the weather"
 *   assistant: [text, tool_use]
 *   user:      [tool_result]           ← no text
 *   assistant: [tool_use]              ← no text either; agent loop done
 *
 * The pre-2026-06-06 code unconditionally overwrote on each iteration,
 * so the trailing role-blocks-without-text zeroed out the captured text.
 * Audit of 654 detector-enabled annotations across 9 bots showed 98% of
 * multi-turn pushback detections returned `no_prior_turn` because the
 * stored TurnRecord had empty userMessage / assistantMessage.
 *
 * The "last non-empty per role" semantic preserves the user's prompt
 * (regardless of trailing tool_result messages) and the bot's text reply
 * (regardless of trailing tool_use messages). The order-of-iteration
 * still wins ties: later non-empty messages overwrite earlier ones.
 */
declare function extractMessages(messages: unknown): {
    userMessage: string;
    assistantMessage: string;
};
/**
 * Test-only export of extractMessages. Re-exported so the regression
 * tests can pin the "last non-empty per role" behavior on the exact
 * shapes that broke pushback detection in production.
 */
export declare const _extractMessagesForTest: typeof extractMessages;
/**
 * Map a turn's trigger_kind to the session_class the ModelRouter
 * understands (the same vocabulary the keyword/LLM classifier uses).
 * Used by the pre-classification anchor in resolveModelRouting so the
 * FIRST turn of an auto-driven session lands on the correct tier
 * BEFORE model selection runs.
 *
 * Mapping rationale:
 *   - heartbeat / cron_app → "background"      (clock-fired work)
 *   - subagent / summarizer / classifier /
 *     task_extractor / fallback     → "maintenance"  (in-session scaffolding)
 *   - user_turn / unknown           → null  (let the agent_end classifier
 *                                            decide with full context;
 *                                            don't anchor prematurely)
 *
 * Keep aligned with cost_event_converter.py's trigger_kind taxonomy
 * AND with tile_metrics._SCHEDULED_KINDS — the three layers (plugin
 * classification, cost rollup, tile presentation) must agree on what
 * "scheduled" vs "background" means or the operator sees inconsistent
 * stories on the dashboard.
 */
export declare function _triggerKindToSessionClass(triggerKind: string): string | null;
/**
 * Build the shared-turn record payload for one of Evolve's own subagent LLM
 * calls (summarizer / tier classifier / struggle judge / preflight router),
 * captured at the llm_output hook.
 *
 * Why llm_output and not agent_end: OC's plugin-subagent lane
 * (agentRunTracking: "plugin_subagent") never fires agent_end — verified
 * against the live 2026.7 gateway (diag agent_end lines cover only parent
 * sessions) — so the normal handleTurn → writeTurnToShared path never sees
 * these calls and they billed invisibly ($0.0000 in the Phase A2 overhead
 * rollup while the summarizer ran dozens of times a day). llm_output DOES
 * fire for the lane (the attempt runner dispatches it unconditionally with
 * usage + ctx.sessionKey), so the capture writes the turn record here.
 *
 * `source` carries the canonical trigger_kind ("summarizer"/"classifier");
 * cost_event_converter passes it through to cost_event.trigger_kind, which
 * is what context_health --overhead's EVOLVE_TRIGGER_KINDS bucket reads.
 * Model-independent by construction: post-#3531 these runs are typically
 * UNPINNED (bot-default model), so no model heuristic could distinguish
 * them — the session-key tag is the only reliable signal.
 *
 * Returns null when the event carries no billed tokens (an errored attempt)
 * — zero-token rows are noise the converter would drop anyway.
 * Pure; exported for tests.
 */
export declare function _buildEvolveSubagentTurn(kind: EvolveSubagentTriggerKind, event: {
    model?: unknown;
    provider?: unknown;
    usage?: unknown;
} | null | undefined): {
    llm: SessionLlmData;
    costEstimated: number;
} | null;
/**
 * Map ModelRouter's at-decision-time driver string onto the span's
 * ``cascade.tier_chosen_by`` enum.
 *
 * The router knows what actually drove THIS turn's model selection
 * (set inside _resolveModelAndTier). This function is the trusted
 * translation; TurnObserver calls it once per agent_end to populate
 * the span attribute.
 *
 * Why this exists as a pure function
 * ----------------------------------
 * Earlier in-line code re-derived chosenBy from CURRENT state at
 * telemetry time (post-turn) — querying isSpendCapForced /
 * getUserTier / etc. That broke on the runaway-rate trip: a turn
 * that BREACHED the cap during its own execution would post-turn
 * show isSpendCapForced=true even though the routing decision (made
 * pre-turn) never went through the safety-net branch. The span got
 * stamped "spend_cap" with tier_used=tier2 — telemetry claiming the
 * safety net forced Sonnet, which is impossible. Observed in production
 * 2026-06-03 at 07:27 UTC, a single $33.64 Sonnet turn mis-tagged
 * spend_cap-driven.
 *
 * The fix: trust the router's at-decision-time stamp. The breach
 * event still travels on a separate field (cascade.runaway_rate.
 * tripped) — distinguishing "this turn DECIDED on tier X" from
 * "this turn triggered a cap that affects future turns."
 *
 * Driver mapping (router enum → span enum):
 *   "runaway"          → "spend_cap"   (safety-net family;
 *                                       runawayTripped flag carries
 *                                       the finer signal)
 *   "spend_cap"        → "spend_cap"
 *   "user_request"     → "user_request"
 *   "cascade"          → "cascade"     (gated on isCascadeEnabled)
 *   "classifier"       → "classifier"
 *   "operator_default" → "default"     (audit #69 Phase A; no
 *                                       dedicated span enum yet —
 *                                       indistinguishable from bot
 *                                       default at consumer level)
 *   "user_default"     → "default"     (audit #69 Phase C; same as
 *                                       operator_default above)
 *   null               → fall back to legacy heuristic
 *
 * Fallback heuristic (when router didn't record a driver):
 *   • userTierForChosenBy is set    → "user_request"
 *   • userModelOverride is set      → "user_model_override"
 *   • modelTier === "tier3"         → "classifier" (only path to
 *                                     tier3 once overrides are out)
 *   • otherwise                      → "default"
 *
 * Pure function — no I/O, no module state — for testability.
 */
export declare function _computeChosenBy(routerDriver: string | null, userTierForChosenBy: string | null, userModelOverride: unknown, cascadeEnabled: boolean, modelTier: string | null): TierChosenBy;
/**
 * System-prompt note injected (via before_prompt_build → appendSystemContext)
 * on turns whose model was forced down by a cost safety net.
 *
 * Why the note exists: the installed OC gateway (dist 2026.7.1-2, verified on
 * the reference pod) renders a user-visible "Model Fallback: <active>
 * (selected <configured>; selected model unavailable)" banner whenever the
 * model used differs from the session's configured model. The banner's reason
 * is built ONLY from provider-failure attempts; a hook override has none, so
 * OC falls back to the literal "selected model unavailable" — a false claim of
 * provider outage when the real cause is Evolve's cost breaker. The
 * before_model_resolve result supports no reason/label field
 * (mergeBeforeModelResolve keeps only modelOverride/providerOverride), so the
 * banner text itself cannot be corrected from the plugin. What we CAN do is
 * brief the bot, which otherwise cannot observe its own routing and would
 * confabulate an outage when the user asks about the banner.
 *
 * Pure function — no I/O, no module state — for testability.
 */
export declare function _buildCostDowngradeNotice(driver: "spend_cap" | "runaway", model: string): string;
/** Maximum struggle-payload samples written per bot per UTC day. */
export declare const STRUGGLE_SAMPLE_DAILY_CAP = 20;
/**
 * Return true if a turn matches the sampler's interest predicate:
 *   - OC marked ``success: false``
 *   - struggle detector returned exactly 0.5 (the success-floor clamp)
 *
 * Pure function — no I/O, no state — exported for testability.
 */
export declare function _shouldCaptureStruggleSample(event: {
    success?: unknown;
} | null | undefined, signal: {
    score: number | null;
} | null | undefined): boolean;
/**
 * Produce a shape-only summary of ``event.messages``. Records role, content
 * shape, block types, presence of detector-relevant flag fields
 * (``is_error``, ``name``, ``tool_use_id``, etc.), and text/content lengths.
 * NEVER preserves text content, tool-call argument values, or tool-result
 * bodies — the diagnostic is structural only.
 *
 * Returns ``{ totalMessages, truncated, sample[] }`` where ``sample`` is the
 * tail of up to ``STRUGGLE_SAMPLE_MAX_MESSAGES`` messages with per-message
 * block shape (up to ``STRUGGLE_SAMPLE_MAX_BLOCKS`` blocks each).
 *
 * Pure function — exported for testability.
 */
export declare function _sanitizeMessagesForShape(messages: unknown): {
    totalMessages: number;
    truncated: boolean;
    sample: Array<Record<string, unknown>>;
    notArray?: true;
    raw_type?: string;
};
/**
 * Return true when the manifest's lifecycle status allows its
 * event_triggers[] to intercept. Pure function — exported for
 * testability (see tests/turnObserver.manifestTriggerStatus.test.mjs).
 */
export declare function _manifestStatusAllowsTriggers(manifest: unknown): boolean;
/** Accumulated LLM usage data per session, populated by llm_output events. */
interface SessionLlmData {
    model: string;
    provider: string;
    channel: string;
    source: string;
    inputTokens: number;
    outputTokens: number;
    cacheReadTokens: number;
    cacheWriteTokens: number;
}
export declare class TurnObserver {
    private config;
    private logger;
    private sessionTurnCounts;
    private sessionTaskIds;
    private sessionTurns;
    private sessionLlmData;
    /** Cached LLM classification result, populated async on first ambiguous turn. */
    private sessionLlmClassifications;
    /**
     * Sessions where any earlier turn was observed with source="heartbeat".
     *
     * Workaround for openclaw/openclaw#84825: OC loses `isHeartbeat=true`
     * across follow-up turns in a heartbeat session, so sub-runs record
     * `trigger=user` even though the session is still a heartbeat retry
     * storm. The per-turn JSONL `source` field is the canonical input
     * for the Usage page's "By Source" rollup; without re-tagging,
     * heartbeat runaways are misattributed as legitimate user demand.
     *
     * The channel field stays "heartbeat" across all sub-runs, so we
     * use it as the gating condition at write time: if a turn's
     * channel=="heartbeat" AND this set contains the session, the
     * source is re-tagged to "heartbeat". Fail-safe: sessions never
     * triggered by heartbeat are never re-tagged (no false positives).
     *
     * In-memory only — heartbeat sessions are short-lived (max minutes),
     * so plugin restarts don't matter.
     */
    private _heartbeatTriggeredSessions;
    private summarizer;
    private modelRouter;
    /**
     * Expose the ModelRouter for tools/components that need to call its
     * public methods (e.g., the `session.set_tier` MCP tool calling
     * setUserTier). Read-only — callers should not replace the instance.
     */
    getModelRouter(): ModelRouter;
    private llmClassifier;
    /**
     * Pre-flight intent router (Phase 1 of
     * spec-preflight-intent-router-2026-06-06.md). Runs at
     * before_model_resolve on every user_turn (when enabled for the bot)
     * and stores the decision on ModelRouter for the resolution ladder
     * to consult. Phase 1 ships abstain-only; spans still get
     * `cascade.preflight.layer="abstain"` to prove the wiring.
     */
    private preflightRouter;
    /**
     * Per-session pre-flight decision recorded at before_model_resolve.
     * Persisted across the model call (which happens between the hook
     * and agent_end) so the span writer can mirror it onto the cascade
     * span. Cleared on session end alongside the rest of session state.
     */
    private _sessionPreflightDecisions;
    /**
     * Per-bot DNT-style flag for the pre-flight router. Read from
     * `network.json::cascade.preflight.enabled` (pod default) with
     * `bots.<botId>.preflight.enabled` override. TTL-cached so the
     * hot-path before_model_resolve doesn't read network.json on every
     * turn (mirrors `_isPushbackEnabled` shape).
     */
    private _preflightEnabled;
    private _preflightEnabledCheckedAt;
    private static readonly _PREFLIGHT_CACHE_TTL_MS;
    private recentTranscript;
    /**
     * Hot-path Opik-shaped span emitter for cascade telemetry. Per
     * docs/spec-tier-cascade-2026-05-26.md Phase 1: emit per-turn struggle
     * + tier-used spans so we have data to validate the cascade design
     * against before flipping routing in Phase 2.
     *
     * null when disabled via network.json::observability.cascade_telemetry.enabled
     * = false (kill-switch). Note that struggle is still computed and
     * mirrored into the existing turn annotation even when emission is
     * off — the kill-switch suppresses the new spans/ file but not the
     * annotation enrichment.
     */
    private cascadeTelemetry;
    /**
     * Outward-action ledger for the autonomy ladder (Phase B,
     * spec-autonomy-ladder §1.3 / OQ-3 — bot-side counters). Records one
     * line per MCP tool call per turn (names + ids only, never content)
     * to {sharedDir}/{botId}/outward-actions/; the evolve-side limits
     * daemon and streak producer read it. Always on when botId is known —
     * the ledger is the data source the rung-3 caps depend on, so a
     * kill-switch here would silently disable an operator-set limit.
     */
    private outwardActionLedger;
    private prefixHashLedger;
    /**
     * CascadeController in shadow mode (spec § 2.2 Phase 2). Computes
     * the decision the cascade WOULD have made for each turn — recorded
     * to span attributes but NOT applied to routing. The keyword
     * classifier still drives actual model selection until Phase 3
     * cutover. Phase 2 shadow data is what Phase 3 cutover decision
     * hinges on (% disagreement explainable).
     */
    private cascadeController;
    /**
     * Cross-turn struggle aggregator (added 2026-06-07 after live-pod
     * audit showed real conversational struggle didn't trip any per-turn
     * detector). Tracks shell-error-paste count, bot-self-correction
     * count, and turn velocity across a session's history; cascade
     * controller reads the aggregate signal alongside per-turn struggle.
     *
     * Spec: docs/spec-session-struggle-aggregator-2026-06-07.md (to be written).
     */
    private sessionAggregator;
    /**
     * Session-level LLM-as-judge (added 2026-06-07 alongside the
     * aggregator). When the aggregator's PRE-thresholds trip — any
     * single shell-error paste OR bot self-correction OR sustained
     * high velocity — the judge fires async at agent_end with the
     * last N turns of conversation. Verdict (STRUGGLING / OK /
     * AMBIGUOUS) is stored under sessionId and applied to the NEXT
     * turn's cascade decision.
     *
     * Cheap wide-net (regex/pattern) + smart sharpening (LLM) — the
     * design pattern from the operator's 2026-06-07 conversation:
     * "use Python to cast a wide net, then call LLM to look for actual
     * fish." Same escalating-cost pattern as PreflightIntentRouter.
     */
    private sessionJudge;
    /**
     * Per-session verdict from the LLM judge. Populated async at
     * agent_end (when pre-thresholds trip and the judge runs); read
     * by the next turn's cascade decision call. Cleared on session
     * end and on LRU prune.
     */
    private _sessionJudgeVerdicts;
    /**
     * Drift reasons we've already logged-once for this process. Per spec
     * § 2.7, payload-drift conditions log once per reason per process and
     * the audit layer correlates via the span attribute, not log scraping.
     */
    private readonly _loggedDriftReasons;
    /**
     * Per-bot DNT flag for the user-pushback signal
     * (spec-user-pushback-signal-2026-05-30 § 6). Read from
     * `network.json::bots[botId].pushbackSignal`, default ON.
     * Cached with a 60s TTL so the hot-path turn loop doesn't read
     * network.json on every turn.
     */
    private _pushbackEnabled;
    private _pushbackEnabledCheckedAt;
    private static readonly _PUSHBACK_DNT_CACHE_TTL_MS;
    /**
     * Read the per-bot pushback DNT flag from network.json with a 60s TTL
     * cache. Default-on (missing key → enabled). Mirrors the cache shape
     * used by RecentTranscriptCapture.isEnabled() — same trade-off: low
     * read cost on the hot path, ~1-minute lag for operator-flip to take
     * effect, fail-open on any error.
     */
    private _isPushbackEnabled;
    /**
     * Read the last assistant text from sessionTurns for the given session,
     * or undefined when no prior turn exists. Used by the pre-flight router
     * for context-shift detection (Phase 2+; Phase 1 ignores it).
     *
     * Stateless wrapper — just walks sessionTurns from the end looking for
     * the most recent non-empty assistantMessage. Mirrors the "last
     * non-empty per role" semantic that the post-2026-06-06 extractMessages
     * fix established.
     */
    private _getLastAssistantText;
    /**
     * Fire the session-struggle LLM judge in the background. Builds the
     * conversation snippet from sessionTurns, calls the judge, and stores
     * the verdict for the NEXT turn's cascade decision. Promise is meant
     * to be fired-and-forgotten — failures degrade to AMBIGUOUS (which
     * the cascade controller treats as no-signal).
     *
     * Bounded by the SessionStruggleJudge's 3s timeout. Worst case: a
     * turn's verdict isn't ready by the next turn (just no judge signal
     * that turn) — system continues operating on the aggregator's
     * elevation signal alone.
     */
    private _fireJudgeAsync;
    /**
     * Read the per-bot pre-flight intent router gate from network.json
     * with a 60s TTL cache. Default-on at the pod level; per-bot can
     * opt out via `bots.<botId>.preflight.enabled: false`. Mirrors
     * the cache shape of `_isPushbackEnabled` — low read cost on the
     * hot path, ~1-minute lag for operator flips to take effect,
     * fail-open on any error.
     *
     * Phase 1: even when enabled, the router returns ABSTAIN and the
     * routing ladder falls through to existing behavior. The gate
     * exists from day one so the per-bot opt-out works once Phase 2
     * ships the regex layer.
     */
    private _isPreflightEnabled;
    /**
     * Per-session holdout-cohort assignment. Computed lazily on first
     * turn of each session via the deterministic hash in HoldoutCohort.
     * Tagged onto cascade telemetry spans so the Phase 4 audit layer
     * can identify the un-cascaded reference cohort.
     *
     * Phase 2 first-cut: independently hash each session (no subagent
     * inheritance yet — that requires parent-session-id tracking which
     * isn't surfaced in agent_end ctx). TODO when subagent dispatch
     * exposes parent_session_id.
     */
    private readonly _sessionHoldoutAssignment;
    private _isHoldoutSession;
    /**
     * Tracks dirs we have already created+chmod-ed so we skip the syscalls on
     * subsequent turns.  Populated lazily; reset only on process restart.
     */
    private _initializedDirs;
    /**
     * Struggle-payload sampler state. Tracks UTC date + count so the daily cap
     * (``STRUGGLE_SAMPLE_DAILY_CAP``) resets at midnight. See the module-level
     * comment above ``_sanitizeMessagesForShape`` for why this exists and the
     * scope guard.
     */
    private _struggleSampleDate;
    private _struggleSamplesToday;
    private betterSessionState;
    private readonly betterClient;
    private readonly betterFormatter;
    private readonly keywordHandler;
    private readonly evoDispatchClient;
    private _beforeAgentRunActive;
    /**
     * Cross-hook injection store.
     *
     * before_agent_run fires with the user message and detects keywords /
     * follow-ups, but the gateway in use does not honour skipAgent:true.
     * It stores the injection text here; before_model_resolve picks it up
     * on the same turn and returns it as systemAppend so the LLM echoes it.
     *
     * Key: sessionKey  Value: systemAppend string to inject
     */
    private _pendingKeywordInjection;
    /**
     * RunIds where before_model_resolve already handled an "evo" keyword.
     *
     * The before_model_resolve path injects the formatted recommendation as
     * systemAppend — the LLM echoes it. The agent_end path independently
     * detects "evo" and direct-sends via Telegram. If both fire on the same
     * turn the user gets two messages, so agent_end consumes (and removes)
     * the runId here before deciding whether to fallback-send.
     *
     * Bounded at 1024 entries with FIFO eviction; runIds that never see
     * agent_end (CLI sessions, errored runs) age out without leaking.
     */
    private _evoHandledRuns;
    /**
     * RunIds whose model resolution this plugin has already answered once.
     *
     * OC re-fires before_model_resolve for the SAME runId on every
     * provider-failover attempt (verified against the installed OC
     * 2026.7.1-2 dist: the fallback walk loops back through the embedded
     * runner's resolveHookModelSelection). Re-emitting our routing
     * override there hijacks the failover candidate's model slot — the
     * 2026-07-31 incident's `FailoverError: Unknown model:
     * google/anthropic/claude-haiku-4-5` — and, even emitted coherently,
     * would re-pin the exact model whose failure started the walk.
     * resolveModelRouting stands down (returns no override) on a repeat
     * fire unless a cost safety net is forcing.
     *
     * Bounded at 1024 entries with FIFO eviction, same shape as
     * _evoHandledRuns above.
     */
    private _routedRunIds;
    private _directSentRuns;
    /**
     * Runs where the plugin chose the LLM-echo fallback for `evo` (Slack,
     * primary bots — anywhere ``_sendEvoDirectToTelegram`` doesn't apply).
     * Value is the verbatim system_append text the dispatcher returned —
     * we re-inject it via ``appendSystemContext`` in ``before_prompt_build``
     * because pi-embedded silently drops the ``systemAppend`` field
     * returned from ``before_model_resolve`` (see the comment block on
     * the before_prompt_build hook).
     *
     * Without this, Slack / primary bots saw `evo help` reach the LLM with
     * no verbatim directive — the LLM would either fabricate its own help
     * (team_bot_a on Slack, evolve on Telegram-primary) or ramble about
     * "openclaw evolve isn't a CLI command" (team_bot_c on Slack). Mirrors the
     * stay-silent path used after direct-send succeeds.
     */
    private _llmEchoRuns;
    /**
     * Runs whose model override was forced by a cost safety net (spend_cap /
     * runaway). Written by resolveModelRouting, consumed by before_prompt_build
     * to inject a bot-visible attribution note on the same turn.
     *
     * Why this exists: the installed OC gateway (verified against dist
     * 2026.7.1-2) renders a "Model Fallback: <active> (selected <configured>;
     * selected model unavailable)" banner whenever the model actually used
     * differs from the session's configured model. The fallback reason comes
     * exclusively from provider-failure attempts; a hook-driven override has
     * zero attempts, so the banner's default reason falsely claims a provider
     * outage. The before_model_resolve result carries ONLY
     * modelOverride/providerOverride (mergeBeforeModelResolve) — there is no
     * reason/label field to correct the banner — so the honest channel we own
     * is the system prompt: tell the BOT why it was downgraded so it attributes
     * the change to the cost cap instead of confabulating an outage.
     *
     * Bounded at 1024 with FIFO eviction (same posture as _evoHandledRuns);
     * entries are kept after consumption so a prompt rebuild on the same run
     * re-injects consistently.
     */
    private _costDowngradeRuns;
    /**
     * In-process counter of evo-dispatch failures by reason, since process
     * start. Cheap per-turn telemetry (PART 2 of the fail-loud work): every
     * recognized-but-failed evo command bumps this AND emits a structured log
     * line. Deliberately does NOT fire a pod-wide Signal — the external
     * black-box evo-probe monitor ([META:reports]) owns the authoritative
     * alert; this in-band layer is complementary (user-facing + per-turn).
     * Read by ``getEvoDispatchFailureCounts`` (tests / introspection).
     */
    private _evoDispatchFailureCounts;
    /**
     * Cached render of the per-turn [INSTALLED CAPABILITIES] block (skills +
     * configured-integration tools). Computed by spawning
     * ``session_surface.py --capabilities-only`` and re-used across turns so
     * we don't pay a Python subprocess on the LLM hot path every turn.
     *
     * Why cached-and-replayed-per-turn rather than session_start only: the
     * block has to reach EXISTING long-running Telegram sessions, which
     * session_start fires for only once and never re-fires (see
     * _handleEvoFallback's note). before_prompt_build is the only hook this
     * gateway consumes every turn, so the block ships there — and capabilities
     * are stable enough that a TTL-cached render is fresh enough. CA-P1
     * (#3080) shipped non-functional precisely because it relied on
     * session_start; this is the fix.
     */
    private _capBlock;
    /** Dedupes concurrent capability-block computes (one in-flight at a time). */
    private _capBlockInflight;
    /**
     * TTL cache for the per-turn directory-digest block (user-directory Phase 3a).
     * Same shape/discipline as the capability block: at most one socket
     * round-trip per TTL window; a directory-read fault serves the last-good
     * digest (bounded staleness) instead of flapping the block to "" — a
     * presence flap is two full prompt-cache invalidations (post-mortem §2).
     */
    private _dirDigestBlock;
    /** Dedupes concurrent directory-digest fetches (one in-flight at a time). */
    private _dirDigestInflight;
    /** Byte-stable narrative render (identical text → identical bytes). */
    private _narrativeStable;
    /**
     * Last non-empty speaker block per session. Daemon-triggered turns
     * (heartbeat/cron) have no captured sender, and dropping the block for one
     * turn then restoring it on the next human turn churns the system prefix
     * twice for zero information. Reused ONLY when there is no sender at all —
     * a real sender that resolves to no block (G-N2 resolve-or-omit) must NOT
     * inherit another speaker's block. Pruned with the other session maps.
     */
    private _lastSpeakerBlockBySession;
    /**
     * Deferred session summarization timers.
     *
     * After each turn we schedule a summarization to fire in 8 seconds.  On a
     * subsequent turn the previous timer is cancelled and rescheduled, so the
     * summarizer only runs once the session goes idle.  When session_end fires
     * it cancels the pending timer and runs summarization immediately.
     *
     * This ensures session_summary records are written even when session_end
     * does not fire (common for single-turn cron-triggered sessions).
     */
    private readonly _pendingSummaryTimers;
    /** Sessions that have already been summarized — prevents double-write. */
    private readonly _summarizedSessions;
    /**
     * Layer C trigger cache. Per-bot list of compiled event_triggers[] rules,
     * loaded from {/Users/<bot>/.openclaw/workspace/manifests/}*.json. Refreshed
     * when the directory mtime changes.
     *
     * Phase 2.3 of the agent-freelance-bypass spec
     * (docs/spec-agent-freelance-bypass-phase2-2026-06-06.md). The actual
     * matching + interception happens in _interceptManifestTrigger; this
     * cache is the cold-path source.
     *
     * Empty list (not null) = "loaded, no triggers" — null means "not yet
     * loaded." A bot with zero plugin_intercept manifests gets the empty
     * list after the first scan and falls through cheaply on every turn.
     */
    private _manifestTriggers;
    /** mtime of the manifests directory at last cache load. Triggers
     *  re-scan when changed. */
    private _manifestTriggersScanMtime;
    /** When the cache was last refreshed. Throttles re-scan attempts
     *  even if mtime can't be read (defensive against directory churn). */
    private _manifestTriggersLastScan;
    constructor(config: EvolveConfig, logger: PluginLogger, api?: any);
    /** SessionCostMonitor instance — populated in constructor. */
    private readonly sessionCostMonitor;
    private readonly pressureFlagsReader;
    /**
     * Read the cascade-telemetry kill-switch from network.json. Default
     * true (per spec § 4.3 and the Phase-1 rollout decision).
     *
     * Failure modes (missing file, malformed JSON) silently default to
     * enabled — telemetry is purely additive, and a misconfigured config
     * file shouldn't accidentally silence the new data source. Operator
     * surfaces an explicit ``cascade_telemetry.enabled: false`` to disable.
     */
    private isCascadeTelemetryEnabled;
    /** Map a Better Engine surface to the channel name the dispatcher expects.
     *  Returns null when no specific channel applies (admin surface, etc.) —
     *  the dispatcher's identity resolver tolerates null and falls back to
     *  treating the sender as primary in v1. */
    private _channelForSurface;
    /** Called when SessionCostMonitor crosses the per-session cap (PR C).
     *  The breaker file IS the load-bearing artifact for the pre-turn
     *  rejection; this hook is the place to surface the trip as an
     *  operator-visible event without going through the Python signal
     *  store directly. The cost_watchdog daemon picks up the breaker
     *  file and emits the actual Signal on its next run
     *  (signals.session_budget_emit.collect_for_bot).
     *
     *  Today this is log-only: the trip line + reason already land in
     *  the plugin log via the info call at the recordCost site, and
     *  cost_watchdog materializes the Signal. Kept as a separate hook so
     *  a future PR can wire a faster path (e.g. unix-socket call to the
     *  admin daemon) without re-plumbing the monitor's emitSignal callback. */
    private _emitSessionBudgetSignal;
    /** Best-effort extraction of the calling user's stable channel-side ID for
     *  identity resolution. Returns null if we can't determine it — the
     *  dispatcher's resolver then falls back to treating the sender as
     *  primary, which is the right v1 default for unconfigured cases.
     *
     *  Telegram: ctx.sessionKey ends with the numeric chat ID, e.g.
     *    "agent:main:telegram:direct:987654321" → "987654321"
     *  Slack / Discord: shape is not yet documented; returning null until we
     *  observe real sessionKey values. Once primary capture lands for those
     *  channels we'll tighten this. */
    private _extractSenderExternalId;
    private loadModelRouterConfig;
    /**
     * Load the per-user-per-bot tier preferences file
     * (``{sharedDir}/{botId}/user-tier-prefs.json``). Audit #69 Phase C.
     * Returns ``{users: {}}`` when the file is missing or malformed —
     * the plugin then routes per the operator default only.
     */
    private _loadUserTierPrefsFile;
    register(api: any): void;
    private handleSessionStart;
    /**
     * Safety valve: if session_end never fires (crashed gateway, OC restart) the
     * per-session Maps would grow indefinitely.  Cap at 500 entries and evict the
     * oldest when exceeded — JS Maps iterate in insertion order so the first key
     * is always the oldest session.
     */
    /**
     * Record that the plugin successfully direct-sent the user-visible
     * response for ``runId``. Read by the before_agent_reply hook to
     * suppress the LLM's hallucinated second message. Bounded by a size
     * cap (1024 entries, oldest evicted) — runs are typically consumed
     * by before_agent_reply within a few seconds, but the cap prevents
     * unbounded growth if the hook is somehow skipped.
     *
     * Pass ``undefined`` when ``ctx.runId`` isn't available — the call
     * is a safe no-op.
     */
    /**
     * Manifests directory for this bot. The plugin reads JSON files from
     * here directly — same path the per-bot scanner writes to.
     */
    private _manifestsDir;
    /**
     * Refresh the compiled trigger cache from manifest files. Cheap on the
     * common path (mtime unchanged → reuse cached list). Throttled to one
     * scan per 5 seconds even if the directory mtime read fails, so a
     * directory-listing error can't burn CPU.
     *
     * Returns the cached list. Never throws — on any failure the cache is
     * set to [] (loaded, no triggers) and the bot's hot path falls through.
     */
    private _getManifestTriggers;
    /**
     * Compile one event_triggers[] entry into a runnable trigger record.
     * Returns null when the entry isn't usable (no pattern, unknown
     * protocol, invalid regex). Compilation failures are logged at warn
     * level — the Phase 2.1 install-time validator should have caught these
     * already, but the plugin defends against operator hand-edits.
     */
    private _compileTrigger;
    /**
     * Infer the manifest-style channel kind from the plugin's current
     * sessionKey/surface. Uses the Telegram convention that chat_ids in
     * group chats are negative and DMs are positive. Returns one of the
     * manifest channel enum values, or "unknown" when no inference is
     * possible (caller's predicate accepts "unknown" by being liberal).
     */
    private _inferChannelFromCtx;
    /**
     * Substitute {token} placeholders in a string from the available context.
     * Unknown tokens are left as literals; we warn once per turn so the
     * operator notices manifest drift.
     */
    private _substituteTemplate;
    /**
     * Recursively substitute placeholders in the request_payload tree.
     * Strings are run through _substituteTemplate; objects/arrays are
     * traversed; other types pass through.
     */
    private _substitutePayload;
    /**
     * Run the script as a subprocess and resolve to {exitCode, stdout, stderr}.
     * Timeout is hard-killed at 25s (matches atlas's documented end-to-end
     * timeout). Always resolves (never rejects); caller decides what to do
     * with non-zero exit.
     */
    private _runScript;
    /**
     * Intercept the turn if any of this bot's compiled triggers match the
     * incoming message. Returns true if intercepted (caller should mark
     * the run direct-sent and stay-quiet the LLM); false otherwise.
     *
     * Walks triggers in compile order; first match wins. The Phase 2.1
     * install-time validator catches malformed contracts, but every step
     * here defends against runtime failure modes (regex throw, fs error,
     * subprocess crash, parse failure) — every failure path posts the
     * fallback_text (or stays silent if on_failure=silent) and marks the
     * run direct-sent so the LLM never freelances.
     */
    private _interceptManifestTrigger;
    /**
     * Handle a trigger's failure path. Posts fallback_text if
     * on_failure=post_fallback; stays silent if on_failure=silent.
     * Either way, marks the run direct-sent so the LLM never freelances.
     * Returns true unconditionally (the caller should stay-quiet the LLM).
     */
    private _handleTriggerFailure;
    private _markDirectSent;
    /**
     * Mark a run as LLM-echo: the plugin chose not to direct-send (Slack,
     * primary bot, etc.) and is relying on the LLM to echo the dispatcher's
     * ``system_append`` body verbatim. We re-inject the same text via
     * ``appendSystemContext`` in ``before_prompt_build`` because pi-embedded
     * silently drops the ``systemAppend`` returned from
     * ``before_model_resolve``.
     *
     * ``systemAppendText`` is the full directive (already includes the
     * "Respond ONLY with the following message, verbatim" framing — the
     * dispatcher wraps the body via ``_speak_verbatim`` server-side).
     *
     * Bounded at the same 1024 cap as ``_directSentRuns`` — single
     * eviction map shape, so cleanup parity is automatic.
     */
    private _markLLMEcho;
    /**
     * Shared non-narratable echo-relay directive (channel-agnostic substrate).
     *
     * Both the SUCCESS verbatim relay (``_llmEchoVerbatimInstruction``) and the
     * FAILURE relay (``_evoErrorRelayInstruction``) converge here so they speak
     * with ONE wording style. The framing presents ``body`` as the assistant's
     * OWN already-composed reply for this turn — there is no "the system told me
     * to repeat X" frame left for a divergent model to narrate as reported speech
     * (the live VPS leak this whole change exists to kill: a model emitted "the
     * system message says I should respond verbatim:…" instead of just the body).
     *
     * Options:
     *   - ``extraProhibitions`` — appended after the base instruction, before the
     *     separator. The FAILURE relay uses this for the anti-confabulation
     *     guarantees (no invention, no help screen) #3260 exists for.
     *   - ``delimitersAreOutput`` — when the body is the dispatcher's
     *     ``═══ evo … ═══``-framed block, tell the model the delimiter lines are
     *     part of the required output (operators use them to distinguish
     *     plugin-relayed content from bot-LLM freelance). Kept ONLY when
     *     functionally needed — the failure relay omits it so a divergent model
     *     has less to describe.
     */
    private _composedReplyDirective;
    /**
     * Build the SUCCESS verbatim-echo directive for the LLM-echo path.
     *
     * ``wrappedBody`` is the dispatcher's ``═══ evo … ═══``-framed body.
     *
     * Hardened (channel-agnostic echo rework): this used to open with "Respond
     * ONLY with the following message, verbatim:" — the SAME narratable frame the
     * failure relay carried before F2 (#3270). On Slack/Discord a SUCCESSFUL
     * ``evo help`` routes through this directive, and a divergent model narrated
     * that frame as reported speech ("the system says I should respond verbatim:
     * …") exactly like the failure case did on Telegram-less channels. It now
     * delegates to ``_composedReplyDirective`` so the body is presented as the
     * assistant's OWN already-composed reply — no "repeat this verbatim" frame to
     * narrate. The delimiter lines are kept (operators rely on them) via
     * ``delimitersAreOutput``; that is the one functional difference from the
     * failure relay, which stands alone unwrapped.
     */
    private _llmEchoVerbatimInstruction;
    /**
     * Snapshot of evo-dispatch failure counts since process start. Exposed for
     * tests + introspection; the live signal is the structured log line emitted
     * by ``_recordEvoDispatchFailure``.
     */
    getEvoDispatchFailureCounts(): Record<EvoDispatchFailureReason, number>;
    /**
     * PART 2 — telemetry. Record an evo-dispatch failure: bump the in-process
     * counter AND emit one structured, greppable log line. Cheap, never throws,
     * never blocks the turn, and does NOT fire a pod-wide Signal (the external
     * evo-probe monitor owns that). The single ``evo dispatch FAILED`` token is
     * the corroboration anchor for the out-of-band probe / log scrapers.
     */
    private _recordEvoDispatchFailure;
    /**
     * The directive injected when the plugin must RELAY an honest evo error
     * through the LLM (no direct-send channel: admin/primary surface, or a
     * Telegram send that failed). The preferred delivery is the direct-send
     * path (``_deliverEvoFailure`` tries it first on member_bot), which bypasses
     * the LLM entirely; this echo path is the FALLBACK for surfaces that have no
     * channel-direct emit (admin/primary) or when the Telegram send failed.
     *
     * Hardened wording (fail-loud-direct-send): the old phrasing — "Respond ONLY
     * with the following message, verbatim:" then the body — framed the message
     * as a third-party instruction to repeat. Weaker / instruction-divergent
     * models (observed live on the VPS, bot ``evo_vps``) narrated that frame as
     * reported speech ("The system says I should respond verbatim with: ⚠️ evo
     * can't reach…") instead of just emitting the message — leaking the directive
     * into the user's chat. The fix removes the "repeat this verbatim" framing:
     * the body is presented as the assistant's OWN reply for this turn that has
     * ALREADY been composed, so there is no "the system told me to say X" frame
     * left to narrate. We deliberately do NOT delimiter-wrap the body here (unlike
     * ``_llmEchoVerbatimInstruction``): the honest error stands alone as a clean
     * first-person message and wrapper markers only give a divergent model more
     * to describe. The prohibitions (no invention, no help screen) are kept — they
     * are the anti-confabulation guarantee #3260 exists for.
     *
     * Delegates to ``_composedReplyDirective`` (shared with the SUCCESS relay) so
     * both directives carry one non-narratable wording style; the error-specific
     * prohibitions ride in ``extraProhibitions``, and the body is left unwrapped
     * (``delimitersAreOutput`` omitted).
     */
    private _evoErrorRelayInstruction;
    /**
     * PART 1 — fail loud. Build + deliver the honest "evo failed" response for
     * a RECOGNIZED evo command (or mid-wizard turn) whose dispatch failed, and
     * return the injection string the caller should surface. NEVER falls
     * through to a raw LLM answer — that is the entire point.
     *
     *   - Emits telemetry first (counter + structured log).
     *   - On member_bot, direct-sends the message via the Bot API and marks the
     *     run direct-sent (before_agent_reply drops the LLM's output;
     *     before_prompt_build injects stay-quiet) — returns a stay-silent
     *     injection for the legacy systemAppend / _pendingKeywordInjection path.
     *   - Otherwise (admin/primary surface, or direct-send unavailable), marks
     *     the run llm-echo with a verbatim-relay directive so before_prompt_build
     *     makes the LLM relay the honest error verbatim — returns that directive.
     *
     * Caller wiring: before_agent_run stores the return in
     * ``_pendingKeywordInjection``; before_model_resolve returns it as
     * ``systemAppend``. The run-markers set here are what actually guarantee
     * the LLM never confabulates, independent of which gateway hook fired.
     */
    private _deliverEvoFailure;
    /**
     * PART 3 — gateway startup self-test. One-shot, best-effort, NON-blocking
     * ``evo help`` dispatch run once per gateway start (tier=full only). A FAIL
     * line at boot is the earliest possible signal that the evo path is broken
     * (auth / transport / schema) — before any user ever types ``evo``. Must
     * never delay or crash startup; fire-and-forget from ``register``.
     */
    private _runEvoSelfTest;
    /**
     * ``before_agent_reply`` decision — the PRIMARY post-direct-send LLM
     * suppression surface on OC ≥ 2026.6.x.
     *
     * When the plugin already direct-sent the user-visible body for this
     * run (``_directSentRuns`` has the runId), we return
     * ``{ handled: true }``. On OC 2026.6.10 the user-message reply path
     * (``get-reply-*.js → runBeforeAgentReply``) fires this hook
     * unconditionally with ``trigger: "user"`` BEFORE the model turn and,
     * on ``handled`` truthy, short-circuits to ``{ text: "NO_REPLY" }`` —
     * OC's recognized silent-reply sentinel — so the model turn is skipped
     * entirely and NO second chat bubble (no stray ".") is produced.
     *
     * Returning ``undefined`` lets OC dispatch the reply normally — that's
     * the path for ordinary conversation turns and for the ``_llmEchoRuns``
     * relay (wizard/agenda + verbatim echo), which deliberately wants the
     * model to speak and is NEVER in ``_directSentRuns``.
     *
     * Extracted from the inline ``api.on`` callback so the suppression
     * decision is unit-testable.
     */
    handleBeforeAgentReply(runId: string | undefined | null): {
        handled: boolean;
        reason?: string;
    } | undefined;
    /**
     * The directive injected via ``before_prompt_build``'s
     * ``appendSystemContext`` when the plugin already direct-sent the
     * response for this run.
     *
     * Refined around a finding from real-world test logs: the LLM was
     * spending tokens on speculative work ("what does evo setup-google
     * mean?") AND ignoring negative-framed prohibitions ("don't reply").
     * The fix has two halves:
     *
     *   1. BRIEF the LLM in plain English about what's happening — what
     *      Evolve is, what the user's keyword pattern means, that the
     *      plugin owns the conversation. Eliminates 80% of the LLM's
     *      speculative work because there's nothing left for it to
     *      figure out by investigation.
     *
     *   2. POSITIVE TASK: produce the recognized silent-reply sentinel
     *      ``NO_REPLY`` (uppercase, alone). A specific, achievable,
     *      constrained output the LLM can succeed at — and one OC's
     *      channel-outbound layer treats as "show the user nothing"
     *      (isSilentCommentaryProgressText), so even if the model does
     *      comply on this fallback path the user sees no bubble. Models
     *      comply with positive constrained tasks far more reliably than
     *      with negative prohibitions ("don't do X").
     *
     * FALLBACK ONLY (OC ≥ 2026.6.x). As of OC 2026.6.10 the
     * ``before_agent_reply`` hook fires for user-message turns BEFORE
     * the LLM runs (get-reply-*.js → runBeforeAgentReply, trigger
     * "user"), and the ``before_agent_reply`` handler above returns
     * ``{ handled: true }`` for direct-sent runs — short-circuiting OC
     * to ``NO_REPLY`` and skipping the model turn entirely. So on a
     * current build this directive never reaches the model for a
     * direct-sent run. It is kept as defensive code for a hypothetical
     * OC build that skips that hook.
     *
     * History: an earlier build instructed the LLM to emit a bare "."
     * here (see #1153) because the pre-6.x hook was cron-gated and never
     * fired for Telegram user turns; that "." surfaced as a distracting
     * second chat bubble after the direct-sent body — the bug this
     * change removes. We switched the fallback from "." to ``NO_REPLY``
     * so even the fallback produces no visible bubble.
     *
     * Brief is generic — works for both dispatch turns (user just
     * typed `evo <something>`) and wizard turns (user is mid-flow in
     * an evo wizard, replying with data). Avoiding per-run plumbing
     * keeps the directive a static string.
     */
    private _stayQuietSystemContext;
    private static readonly _HOME_NARRATIVE_MAX_AGE_MS;
    /**
     * Read the cached Home-page narrative and render the per-turn
     * injection block, or return "" when there's nothing fresh to inject.
     *
     * Soft-fails on every error path (no cache, malformed JSON, non-dict
     * payload, empty text, stale beyond the 6h window). The hook in
     * before_prompt_build is on the LLM critical path; a corrupt cache
     * file must never block or slow down a turn.
     */
    private _renderPerTurnNarrativeBlock;
    private static readonly _CAP_BLOCK_TTL_MS;
    private static readonly _CAP_BLOCK_TIMEOUT_MS;
    private static readonly _DIR_DIGEST_TTL_MS;
    private static readonly _DIR_DIGEST_TIMEOUT_MS;
    /**
     * How long a failed renderer may keep serving its last-good block before
     * degrading to "" (StickyBlockCache). Bounded so a permanently-broken
     * renderer cannot pin week-old content; generous because both blocks are
     * advisory hints (enforcement never reads them) and a presence flap costs
     * two full prompt-cache invalidations (post-mortem §2).
     */
    private static readonly _BLOCK_MAX_STALE_MS;
    /**
     * Return the cached [INSTALLED CAPABILITIES] block, recomputing it via
     * ``session_surface.py --capabilities-only`` when the cache is cold or
     * past its TTL. Returns "" when this bot gets no Evolve injection
     * (injectPodConduct off — i.e. tier off/monitor) or the bot has no
     * skills/configured-integration tools.
     *
     * Soft-fails to "" on every error path (missing script, Python error,
     * timeout) and caches the empty result for the TTL so a transient fault
     * doesn't hammer the subprocess each turn. Concurrent calls share one
     * in-flight compute.
     */
    private _renderCapabilitiesBlock;
    /**
     * Per-turn directory-digest block (user-directory Phase 3a).
     *
     * Grows the speaker-context injection from "who is speaking now" to a
     * size-bounded index of THIS bot's admitted roster + named contacts, so the
     * bot stops hand-typing IDs/emails into prose (the §0 address-book bug). The
     * block is framed to OUTRANK USER.md for IDs/emails.
     *
     * Resolution goes through the admin daemon's bot-facing route over the unix
     * socket (GET /api/directory/digest), which binds this bot from the socket
     * peer uid and resolves via resolve_persons — THE one read path (spec §10
     * invariant #1). So the digest cannot diverge from the admin Users page, and
     * it can only ever see this bot's own directory. The server caps the row
     * count and strips the behavioral profile + audit trail before it crosses.
     *
     * Hot-path discipline (every turn): TTL-cached + single-in-flight, and
     * soft-fails to "" on ANY error (socket down, timeout, bad payload) so a
     * directory-read fault degrades to today's block instead of breaking the
     * turn. Gated on injectPodConduct — same as the capability block; a bot at
     * tier off/monitor gets no Evolve-authored context injected.
     */
    private _renderDirectoryDigestBlock;
    /**
     * Phase C.4 — per-turn speaker context block.
     *
     * Reads the captured sender for this turn from the senderRegistry
     * (populated by the before_agent_run hook), resolves their role on
     * this bot via the roleResolver (reads the overlay + network.json),
     * and returns a short systemAppend block telling the LLM who's
     * speaking and what they can do.
     *
     * Returns "" when there's no captured sender (heartbeats, cron
     * ticks, daemon-initiated turns) — those don't represent a human
     * speaker and the block would be misleading.
     *
     * Spec: docs/spec-user-roster-and-roles-2026-06-07.md §enforcement
     * Layer 4 (POD_CONDUCT injection).
     */
    private _buildSpeakerContextBlock;
    private _pruneSessionMapsIfOversized;
    private handleTurn;
    /**
     * Evo keyword fallback for long-running sessions.
     *
     * before_model_resolve and before_agent_run are ignored by this gateway.
     * session_start only fires once per OC session (i.e., never for existing
     * long-running Telegram chats).  This method runs after the turn (agent_end)
     * and sends the top recommendation directly via Telegram Bot API if the user
     * said 'evo' or 'evolve'.
     */
    private _handleEvoFallback;
    /**
     * Send an already-formatted evo message directly to the user's
     * Telegram chat. Used by:
     *   - bare ``evo`` (Legacy Behavior 1) — passes the BetterEngine ``rec``
     *     so the helper auto-accepts on successful delivery.
     *   - ``evo <subcommand>`` (PR #886 follow-up) — passes ``rec=null``;
     *     direct_send_message body is the canonical handler output and
     *     there's no rec to auto-accept.
     *
     * Returns true if the Telegram send succeeded; false (with a logged
     * reason) on any failure mode — chat ID can't be resolved, no
     * botToken, network error, etc. The caller uses the return value to
     * choose between a stay-silent systemAppend (success) and a
     * verbatim-echo fallback (failure).
     *
     * Auto-accepts the rec on success when ``rec`` is non-null
     * (fire-and-forget — accept failure doesn't undo the user-visible
     * delivery, and we don't want to block the LLM call on a rec-engine
     * round-trip).
     */
    private _sendEvoDirectToTelegram;
    /** Send a plain-text message via the Telegram Bot API. */
    private _sendTelegramMessage;
    private handleSessionEnd;
    /**
     * Determine the Better Engine surface for this bot.
     * The Evolve admin bot (role === "primary") uses "admin" surface.
     * All other bots (role === "member") use "member_bot".
     */
    private getBetterSurface;
    /** Get or create Better Engine session state for a session. */
    private getBetterState;
    /**
     * Extract the model routing result (original before_model_resolve logic).
     * Factored out so it can be merged with Better Engine results.
     */
    private resolveModelRouting;
    /**
     * Force tier3 (grunt) for this turn when the LLM's job is just to
     * echo dispatcher content verbatim or stay silent — both `evo`
     * paths fit that shape. Caller merges the returned object into the
     * before_model_resolve hook return.
     *
     * Two motivations beyond raw cost (the bot would otherwise burn a
     * Sonnet/Opus turn just to emit "."):
     *
     *   1. Calibration — every `evo X` exercises the cheapest model.
     *      If Haiku reliably complies with the verbatim / stay-silent
     *      directive, that's strong evidence compliance is robust
     *      across the model tier stack, and a future LLM swap is
     *      less likely to silently break the surface.
     *   2. Account-routing parity — tier3 typically routes through a
     *      separate auth profile (provisioned cheaper / on a metered
     *      account), keeping evo's near-constant background chatter
     *      off the primary auth profile's rate budget.
     *
     * Returns ``{}`` (no override) when:
     *   - the bot has no tier3 configured (legacy pod, no tiers.json)
     *   - routing is disabled in config
     *   - the caller explicitly opted out via ctx.userModelOverride
     *
     * Logs at info so operators can confirm from gateway.log that
     * evo turns landed on tier3.
     */
    private _evoModelOverride;
    /**
     * Handle the before_agent_run hook — zero-token keyword short-circuit path.
     *
     * Detects evo keywords and follow-up actions from the user message.
     *
     * The gateway in use does not honour skipAgent:true, so instead of trying
     * to short-circuit the agent we store the injection text in
     * _pendingKeywordInjection keyed by sessionKey.  before_model_resolve picks
     * it up on the same turn and returns it as systemAppend, so the LLM echoes
     * the formatted recommendation verbatim.
     *
     * Always returns ``{outcome: "pass"}`` — agent always runs; injection is
     * delivered via before_model_resolve. The plugin never wants to block the
     * agent here; legacy ``return null`` worked under the pre-2026.5 OC hook
     * contract but the current ``runBeforeAgentRun`` normalizes null/undefined
     * to ``{outcome: "block"}`` (see openclaw/dist/hook-runner-global-*.js).
     * Without the explicit pass decision every non-keyword user message comes
     * back as "Your message could not be sent: blocked by evolve" — observed
     * Phase 4 of docs/spec-evo-oc-native-2026-05-19.md trying to bring up the
     * admin UI proxy against evo's gateway.
     */
    private handleBeforeAgentRun;
    /**
     * Handle Better Engine behaviors in before_model_resolve.
     *
     * Returns an object that may include `systemAppend` if a keyword injection,
     * follow-up response, or contextual hint should be added to the system prompt.
     *
     * Behavior 1 — Keyword injection (evo/evolve fallback path)
     * Behavior 2 — Follow-up action detection (accept/reject/snooze/next/context)
     * Behavior 3 — Contextual discovery hint injection
     */
    private handleBeforeModelResolve;
    /**
     * Load rec-hints.json for the bot, match triggers against the user message,
     * and return the highest-priority matching hint (or null).
     *
     * File path: /Users/{botId}/.openclaw/workspace/evolve/rec-hints.json
     * Skipped if file is missing, unreadable, or older than 2 hours (§20.4).
     * Only types eligible for hints: explore, app_quality, onboarding (§20.4).
     */
    private loadMatchingHint;
    private writeTurnToShared;
    /**
     * Write a shape-only diagnostic sample for the next few success=false
     * turns where the struggle detector hit the 0.5 floor. See the module-
     * level comment block above ``_sanitizeMessagesForShape`` for context.
     *
     * Output path: ``{sharedDir}/{botId}/cascade/struggle-debug/<date>.jsonl``.
     * The ``cascade/`` parent already has the correct ACL for this gateway
     * (spans land alongside), so no /tmp + sudo dance is needed. If a
     * different gateway owns the dir we fail silently — the owning gateway
     * will capture its own samples.
     *
     * Rate limited to ``STRUGGLE_SAMPLE_DAILY_CAP`` writes per UTC day per
     * bot. Counter resets at the first sample on a new UTC date.
     */
    private _writeStruggleSample;
    private writeAnnotation;
}
export {};
//# sourceMappingURL=TurnObserver.d.ts.map