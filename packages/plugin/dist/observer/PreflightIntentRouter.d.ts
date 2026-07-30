/**
 * PreflightIntentRouter
 *
 * Decides a model-tier hint for the upcoming LLM call based on the user's
 * prompt and bot identity, BEFORE the LLM runs. Sits in the routing ladder
 * between explicit operator/user defaults (above) and the legacy classifier
 * (below) — see ModelRouter._resolveModelAndTier for the full precedence.
 *
 * Three layers (escalating cost), evaluated in order; first to produce a
 * tier wins:
 *
 *   1. bot_prior  — per-bot baseline tier read from network.json. The
 *                   strongest signal — operator explicitly configured this
 *                   bot to default to tier X. Microseconds, free, TTL-
 *                   cached (60s).
 *
 *   2. regex      — narrow high-precision rules. ONLY catches obvious
 *                   cases: explicit deliberation cues ("design a system",
 *                   "help me think through") for tier1; bare acks /
 *                   single-word commands / common factual lookups for
 *                   tier3. Does NOT try to classify general prompts —
 *                   that's haiku's job (Phase 3). Microseconds, free.
 *
 *   3. haiku      — LLM classifier for ambiguous prompts. ~150ms, ~$0.0001.
 *                   Hard 2s timeout; falls back to abstain on timeout.
 *                   Phase 3 — not implemented in this file yet.
 *
 * Phase status (2026-06-06):
 *   - Phase 1: full wiring (PR #2334), router always abstained
 *   - Phase 2 (THIS FILE): bot_prior + regex layers live
 *   - Phase 3: haiku layer (pending)
 *   - Phase 4: disagreement detector + RSI tuning (pending)
 *
 * Why phase the rules so narrowly:
 *   This is the first phase that actually changes pod behavior. A wrong
 *   tier escalation wastes $ (tier1 on trivial turns) or degrades quality
 *   (tier3 on hard turns). Until the disagreement detector exists
 *   (Phase 4), we can't see misroutings clearly. So Phase 2 ships VERY
 *   tight patterns — high precision, low recall — and defaults to abstain.
 *   Phase 4 RSI grows the pattern set from observed disagreement data.
 *
 * Spec: docs/spec-preflight-intent-router-2026-06-06.md (to be written).
 */
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
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
     * TTL cache for the haiku-layer enabled flag. Read from
     * `network.json::cascade.preflight.haiku_enabled` (pod default) with
     * `bots.<id>.preflight.haiku_enabled` override. Same TTL as bot_prior.
     */
    private _haikuEnabledCache;
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
     * Read the per-bot prior tier from network.json with a 60s TTL cache.
     * Returns null when no prior is configured (the default — most bots
     * don't have one). Fail-open: any read/parse error returns null so a
     * router fault never blocks the turn.
     *
     * Network.json shape:
     *   bots.<botId>.preflight.bot_prior: "tier1" | "tier2" | "tier3"
     */
    private _getBotPrior;
    /**
     * Read whether the haiku layer is enabled for this bot. TTL-cached.
     * Pod default-on; per-bot opt-out via
     * `network.json::bots.<id>.preflight.haiku_enabled: false`.
     *
     * Operators who want python-only routing (no LLM in the path, e.g.
     * for latency-critical bots or to bound API spend) can disable
     * haiku via the pod-level switch
     * `network.json::cascade.preflight.haiku_enabled: false`.
     * Per-bot ON overrides pod-level OFF.
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
     *   1. bot_prior — explicit per-bot operator config
     *   2. regex tier1 — explicit deliberation cues
     *   3. regex tier3 — bare acks / factual lookups / simple commands
     *   4. haiku — LLM classifier for ambiguous prompts (Phase 3)
     *   5. abstain — no layer had an opinion
     *
     * Always resolves; never throws. A fault degrades to ABSTAIN so the
     * legacy classifier handles the turn as it did pre-deploy.
     *
     * latency_ms is observed even on abstain so post-deploy analytics can
     * track router overhead. Phase 2 target: p95 < 5ms (Phase 1 was sub-
     * millisecond; adding the regex scan + one file read adds work but
     * the file read hits the TTL cache 95%+ of the time).
     */
    classify(input: PreflightInput): Promise<PreflightDecision>;
}
export {};
//# sourceMappingURL=PreflightIntentRouter.d.ts.map