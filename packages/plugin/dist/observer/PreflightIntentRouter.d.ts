/**
 * PreflightIntentRouter
 *
 * Decides a model-tier hint for the upcoming LLM call, BEFORE the LLM
 * runs. Sits in the routing ladder between explicit operator/user
 * defaults (above) and the bot's own default (below) — see
 * ModelRouter._resolveModelAndTier for the full precedence.
 *
 * Three layers, evaluated in order; first to produce a tier wins:
 *
 *   1. bot_prior  — the per-bot prior read from network.json. Either an
 *                   operator's hand-set tier, or — since D-OH2 — the
 *                   rung the nightly analyzer job
 *                   (``packages/analyzer/preflight_prior.py``) LEARNED
 *                   from the last 14 days of this bot's own turns, per
 *                   surface, with the evidence attached. Microseconds,
 *                   free, TTL-cached (60s). Refused below the
 *                   confidence threshold: a bot with thin history stays
 *                   on its primary.
 *
 *   2. regex      — narrow high-precision rules. ONLY catches obvious
 *                   cases: explicit deliberation cues ("design a system",
 *                   "help me think through") for tier1; bare acks /
 *                   single-word commands / common factual lookups for
 *                   tier3. Microseconds, free.
 *
 *   3. haiku      — LLM classifier for ambiguous prompts. **Off by
 *                   default and fails CLOSED** since
 *                   internal/decision-evolve-overhead-2026-09-07.md
 *                   (D-OH2): it is the only layer here that costs money,
 *                   and two days of turns put the entire benefit of
 *                   per-turn LLM tier classification below the cost of
 *                   running it. Gated on the single ``classifierGate``
 *                   switch, shared with the post-turn tier classifier,
 *                   the struggle judge and the session summariser.
 *                   ~150ms and ~$0.0001 when an operator turns it on;
 *                   hard 2s timeout, abstains on timeout.
 *
 * Why the prior moved offline:
 *   Layer 3 was answering, per turn and at the user's latency, a
 *   question that the overnight analyzer can answer better and for free
 *   — it already reads every turn this bot has taken. A prior computed
 *   from two weeks of a bot's actual traffic beats a haiku guess at one
 *   message, does not sit in front of the user, and cannot recurse into
 *   itself (finding-tier-router-self-call-loop-2026-09-07.md).
 */
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { type BotPrior } from "./routingRule.js";
export type PreflightTier = "tier1" | "tier2" | "tier3";
export type PreflightLayer = "regex" | "bot_prior" | "haiku" | "abstain";
export interface PreflightInput {
    /** The user's prompt text for THIS turn. May be empty (caller handles). */
    userMessage: string;
    /** Bot identity (e.g., "team_bot_a", "atlas-research"). Used by the
     *  bot_prior layer and as context for haiku. */
    botId: string;
    /** Optional last assistant message — Phase 3+ uses this for context-shift
     *  detection ("actually let me reconsider"). Phase 2 ignores it. */
    lastAssistantMessage?: string;
    /** The surface (OC channel) this turn arrived on. Selects the
     *  per-surface entry of the learned prior when one exists (D-OH2). */
    surface?: string | null;
    /** The OC trigger for this turn, when the caller knows it. Read only by
     *  the cost-breaker gate in ``subagentRun``: the haiku layer decides
     *  which model answers an interactive turn, so a tripped breaker must
     *  not silently move it to the abstain path. Background triggers are
     *  refused as before. */
    trigger?: string | null;
}
export interface PreflightDecision {
    tier: PreflightTier | null;
    reason: string;
    layer: PreflightLayer;
    confidence: number;
    latency_ms: number;
}
/**
 * The "no opinion" decision. Returned when no layer fired.
 */
export declare const ABSTAIN: PreflightDecision;
/**
 * Scan a pattern list against text; return the first match's reason, or
 * null when nothing fires. Module-level (not bound to the class) so
 * tests can call it directly.
 */
declare function _matchPatterns(text: string, patterns: ReadonlyArray<{
    rx: RegExp;
    reason: string;
}>): string | null;
/**
 * Internal helpers exported only for tests. Production code calls
 * `classify()` which orchestrates the layers.
 */
export declare const _internalForTest: Readonly<{
    TIER1_PATTERNS: readonly {
        rx: RegExp;
        reason: string;
    }[];
    TIER3_PATTERNS: readonly {
        rx: RegExp;
        reason: string;
    }[];
    matchPatterns: typeof _matchPatterns;
}>;
/**
 * Parse haiku's response into a tier (or null when unparseable / ambiguous).
 *
 * Defensive: any of these → null:
 *   - empty / undefined response
 *   - "AMBIGUOUS" or any other non-tier word
 *   - response contains multiple tier words (model didn't follow the
 *     one-word instruction; we don't try to disambiguate)
 *
 * Returns null on null so the caller can fall through to abstain.
 *
 * Exported for tests; not part of the public API.
 */
export declare function _parseHaikuTier(response: string | null | undefined): PreflightTier | null;
/**
 * Normalise ``bots.<id>.preflight`` into a {@link BotPrior}.
 *
 * Accepts both vocabularies so the operator-typed scalar
 * (``bot_prior: "tier3"``) and the analyzer-written role
 * (``bot_prior: "fast"`` + ``prior_evidence``) parse to the same thing.
 * An operator's hand-set scalar with no evidence block is taken at
 * confidence 1.0 — they said it on purpose. Anything the analyzer wrote
 * carries its measured confidence and is refused below the threshold by
 * ``priorRoleForSurface``.
 *
 * Exported for tests.
 */
export declare function _parseBotPrior(preflight: unknown): BotPrior | null;
export declare class PreflightIntentRouter {
    private readonly config;
    private readonly logger;
    private readonly api;
    /**
     * TTL cache for the per-bot prior read from network.json. The bot_prior
     * is operator config and changes very rarely (minutes/hours, not turns),
     * so a 60s cache is fine — matches the cadence of
     * `TurnObserver._isPushbackEnabled` / `_isPreflightEnabled`.
     */
    private _botPriorCache;
    private static readonly _BOT_PRIOR_TTL_MS;
    /**
     * The one switch every Evolve-owned model call reads. Default off,
     * fail closed; TTL-cached on the reader itself.
     */
    private readonly _gate;
    /**
     * Hard timeout for the haiku call. Tuned to be well below the user-
     * perceived latency floor on chat surfaces — 2s is the point where a
     * user would start to notice the bot "thinking." If the call exceeds
     * the budget, we abort and abstain (legacy classifier handles the
     * turn at its normal latency).
     */
    private static readonly _HAIKU_TIMEOUT_MS;
    constructor(config: EvolveConfig, logger: PluginLogger, api: unknown);
    /**
     * Read the per-bot prior from network.json with a 60s TTL cache.
     *
     * Two shapes live under ``bots.<botId>.preflight`` and both are read:
     *
     *   bot_prior: "tier1" | "tier2" | "tier3"
     *       The original operator-set scalar. An operator who typed it
     *       meant it, so it carries confidence 1.0 and no evidence.
     *
     *   bot_prior: "fast" | "standard" | "power"     (role vocabulary)
     *   prior_evidence: { confidence, turns, surfaces, computed_at }
     *       What the nightly analyzer job (``preflight_prior.py``) writes:
     *       the rung that actually satisfied this bot's user turns over
     *       the last 14 days, per surface, with the evidence that
     *       produced it. Routes only when ``confidence`` clears
     *       ``DEFAULT_PRIOR_MIN_CONFIDENCE`` — a bot with thin history
     *       gets no prior and stays on its primary, which is what it does
     *       today (D-OH2, and the standing rule).
     *
     * Returns null when no usable prior is configured — the common case.
     * Fail-open on a read error: a missing prior is the DEFAULT state, so
     * "no prior" is the same answer an unreadable file should give. (The
     * fail-CLOSED requirement in D-OH2 is about the LLM layers, which read
     * ``classifierGate`` — see ``_isHaikuEnabled`` below.)
     */
    private _getBotPrior;
    /**
     * True when this bot has a prior confident enough to route on. Read
     * by the sampling gate — a characterised bot needs a smaller sample
     * (D-OH4).
     */
    hasConfidentPrior(): boolean;
    /** Uncached network.json read. Returns null on any fault. */
    private _readBotPrior;
    /**
     * May the haiku layer make a model call for this turn?
     *
     * Was: pod default-ON, per-bot opt-out, **fail-open** — an unreadable
     * network.json enabled an LLM call on every ambiguous user turn.
     * Now: the single ``classifierGate`` switch, default OFF, fail
     * CLOSED (D-OH2 / D-OH5). The regex and bot_prior layers above are
     * free and keep running; only the layer that costs money is gated.
     */
    private _isHaikuEnabled;
    /**
     * Call haiku to classify an ambiguous prompt. Returns null when:
     *   - api isn't available (constructor was passed null/empty stub)
     *   - subagent call throws or times out
     *   - haiku response isn't parseable into a tier
     *
     * On null, the caller falls through to ABSTAIN — legacy classifier
     * handles the turn. The router contract is "never throw into the
     * hot path"; haiku faults degrade silently.
     *
     * Latency budget: hard 2s timeout on waitForRun. Subagent.run setup
     * adds ~10-50ms; haiku itself is typically ~100-200ms. Worst case
     * the user sees +2.1s on this turn (compared to no router); typical
     * case is +150ms.
     */
    private _classifyWithHaiku;
    /**
     * Classify the upcoming turn's tier.
     *
     * Layer order (first to produce a tier wins):
     *   1. bot_prior — operator config, or the offline-learned per-bot
     *      prior for this surface (refused below the confidence bar)
     *   2. regex tier1 — explicit deliberation cues
     *   3. regex tier3 — bare acks / factual lookups / simple commands
     *   4. haiku — LLM classifier; off by default, fails closed (D-OH2)
     *   5. abstain — no layer had an opinion
     *
     * Always resolves; never throws. A fault degrades to ABSTAIN so the
     * legacy classifier handles the turn as it did pre-deploy.
     *
     * latency_ms is observed even on abstain so post-deploy analytics can
     * track router overhead. With the haiku layer closed (the default) the
     * whole call is a regex scan plus a TTL-cached file read — p95 well
     * under 5ms, and no model call in front of the user at all.
     */
    classify(input: PreflightInput): Promise<PreflightDecision>;
}
export {};
//# sourceMappingURL=PreflightIntentRouter.d.ts.map