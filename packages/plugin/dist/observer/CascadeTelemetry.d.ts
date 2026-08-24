/**
 * CascadeTelemetry
 *
 * The plugin's hot-path Opik span emitter. Writes one span per turn to
 * a JSONL file owned by the bot user, in a shape that matches the
 * Python-side ``OpikSpan.to_dict()`` schema (see
 * ``packages/analyzer/observability/opik_client.py``). The same Python
 * consumer that reads spans from the central observability/spans/ dir
 * also reads the per-bot spans/ subdir we write here — schemas are
 * identical, sources are different.
 *
 * Spec: internal/spec-tier-cascade-2026-05-26.md § 3.
 *
 * Path layout (mirrors writeTurnToShared's per-bot/turns/ pattern):
 *
 *   {sharedDir}/{botId}/spans/spans-YYYY-MM-DD.jsonl
 *
 * One JSON object per line. Append-only. Bot user owns the file.
 *
 * Why a separate file (not in turns/ or annotations/):
 *   - turns-jsonl has its own schema (cost-event-shaped) read by
 *     cost_event_converter.py — mixing would break that reader.
 *   - annotations/ is shared across bot teams and follows a different
 *     per-event-type discriminator pattern.
 *   - spans/ matches the Opik JsonlBackend schema 1:1, so the
 *     analyzer-side rollup needs no special-case handling.
 *
 * Failure mode: best-effort. Any I/O error is logged at debug level and
 * swallowed. The plugin's hot path must not block on telemetry writes.
 * Mirrors the JsonlBackend Python-side timeout pattern.
 */
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import type { StruggleSignal } from "./StruggleDetector.js";
export interface CascadeTelemetryConfig {
    sharedDir: string;
    botId: string;
}
/**
 * Source of the tier decision for a given turn. Tracks which precedence
 * level in ModelRouter actually decided this turn's model.
 *
 * Phase 1 values:
 *   - "classifier": current keyword classifier picked the tier (default
 *     during Phase 1 — keyword classifier still owns routing).
 *   - "spend_cap": daily spend-cap flag forced tier3.
 *   - "user_request": operator set Auto/Fast/Standard/Power via UI chip
 *     or session.set_tier MCP tool (per spec-user-tier-control).
 *   - "user_model_override": operator set /model directly in OC.
 *   - "default": bot default tier (no routing decision applied).
 *
 * Phase 2+ adds: "cascade" (cascade controller's struggle-based escalation).
 */
export type TierChosenBy = "classifier" | "spend_cap" | "user_request" | "user_model_override" | "default" | "cascade" | "preflight";
export interface RecordTurnSpanInput {
    sessionId: string;
    /** Which turn number within the session (1-indexed). */
    turnIndex: number;
    startedAt: Date;
    endedAt: Date;
    /**
     * The tier the model that ACTUALLY ran belongs to — derived from
     * `llm.model` via `ModelRouter.getTierForModel()` reverse-lookup. Null
     * when the model string doesn't match any configured tier (unknown
     * model, stale config). When null, callers should still pass
     * `tierIntended` so consumers see at least the classifier's intent.
     *
     * Per failure-mode review F8: the calibration loop must read tier_used
     * from reality (what actually billed), not from intent (what the
     * controller wanted). If OC drops a model override, tier_intended and
     * tier_used will diverge — that divergence is itself a signal worth
     * recording.
     */
    tierUsed: string | null;
    /**
     * The tier the classifier (Phase 1) or cascade controller (Phase 2+)
     * INTENDED for this turn. Source of truth for "what cascade thought
     * should happen." Distinct from tier_used (above).
     */
    tierIntended: string | null;
    tierChosenBy: TierChosenBy;
    /**
     * Granular consent provenance for tier_chosen_by="user_request" (spec
     * § 4.1). One of "ui_chip" | "ask_hint_agreed" | "bot_initiated" |
     * "evo_keyword", or undefined when no operator/bot tier-pick is
     * active. The audit-layer Labeler reads this to distinguish UI-chip-
     * driven labels (the strongest Signal-#1) from bot-asked-and-user-
     * agreed labels (a weaker but still useful signal). ``evo_keyword``
     * lands here for ``evo tier X`` (audit #69 Phase B) — same strength
     * as ui_chip since both are direct user requests.
     */
    consentSource?: "ui_chip" | "ask_hint_agreed" | "bot_initiated" | "evo_keyword" | null;
    /**
     * Session trigger source per spec § 2.4. One of:
     *   "user_turn" | "heartbeat" | "cron_app" | "subagent" |
     *   "summarizer" | "classifier" | "task_extractor" | "fallback" | "unknown"
     * Matches the cost_event.trigger_kind enum so cross-system rollups
     * align. Cascade controller (Phase 2+) branches on this — Phase 1 just
     * records it for stratified analysis.
     */
    triggerKind?: string;
    /** Struggle signal from StruggleDetector (omitted if not computed). */
    struggle?: StruggleSignal;
    /** LLM call metadata. */
    model?: string;
    provider?: string;
    inputTokens?: number;
    outputTokens?: number;
    cacheReadTokens?: number;
    cacheWriteTokens?: number;
    costUsd?: number;
    /** OC's success flag for the turn. */
    success?: boolean;
    /**
     * Pre-flight intent router decision recorded at `before_model_resolve`
     * (Phase 1 of spec-preflight-intent-router-2026-06-06.md). Carried
     * through to the span so the audit layer can grade routing quality:
     * agreement / over-escalation / under-escalation / cascade-corrected.
     *
     * Always populated when the router ran — Phase 1 ships the router in
     * abstain-only mode so `layer="abstain"` shows up on every user_turn
     * span, proving the wiring before Phase 2 enables the regex layer.
     * Omitted entirely on heartbeat / cron / subagent / etc. (router only
     * runs on user_turn triggers).
     */
    preflight?: {
        /** tier1/tier2/tier3 or null when the router abstained. */
        tier: "tier1" | "tier2" | "tier3" | null;
        /** Short label naming why this tier was chosen (or "abstain"). */
        reason: string;
        /** Which layer fired: "regex" | "bot_prior" | "haiku" | "abstain". */
        layer: "regex" | "bot_prior" | "haiku" | "abstain";
        /** [0, 1] — confidence in the decision. Abstain is 0. */
        confidence: number;
        /** Wall-clock ms the router cost (observation, for SLO tracking). */
        latency_ms: number;
    };
    /**
     * Cross-turn struggle aggregate (added 2026-06-07 alongside the
     * SessionStruggleAggregator). Rolling counts over the last N turns
     * of this session — what per-turn detectors can't see.
     *
     * Surfaces on the span so the audit layer can grade whether the
     * aggregate-driven escalations correlate with real outcomes (same
     * pattern as the preflight disagreement detector — give the audit
     * layer the data, let it produce the verdict). Always populated
     * when the aggregator ran, even when all counts are zero.
     */
    sessionAggregate?: {
        shell_error_paste_count: number;
        bot_self_correction_count: number;
        turn_velocity_per_min: number | null;
        turn_count: number;
    };
    /**
     * LLM-as-judge decision from the session-struggle judge (added
     * 2026-06-07 alongside the aggregator). Carried through to the span
     * so the audit layer can grade judge accuracy:
     *   - "judge said STRUGGLING + outcome was bad" → true positive
     *   - "judge said STRUGGLING + outcome was fine" → false positive
     *   - "judge said OK + outcome was bad" → false negative
     *   - "judge said AMBIGUOUS" → punted (audit treats as no-info)
     *
     * Populated only when the aggregator's pre-thresholds tripped AND
     * a prior turn's async judge call completed (verdicts apply to
     * FUTURE turns, not the turn that triggered the call). The verdict
     * on THIS turn's span is the most-recent verdict observed.
     */
    sessionJudge?: {
        /** STRUGGLING / OK / AMBIGUOUS */
        verdict: "STRUGGLING" | "OK" | "AMBIGUOUS";
        /** Short rationale from the LLM (single sentence or phrase). */
        reason: string;
        /** Wall-clock latency of the judge call (observation, for SLO). */
        latency_ms: number;
        /** Which aggregator pre-threshold triggered the judge call. */
        triggered_by: "shell_paste" | "self_correction" | "velocity" | "multiple";
    };
    /** Optional error info if the turn failed. */
    error?: {
        message: string;
        code?: string;
    };
    /**
     * Session-class label from the legacy classifier, captured for read-compat
     * during Phase 1-3 migration. Removed entirely once Phase 3 ships.
     */
    legacySessionClass?: string;
    /**
     * Runaway-rate hard cap state (spec § 2.6). Populated when the per-
     * session rolling-window spend crossed the threshold this turn (or
     * a prior turn — the trip is sticky). Audit layer reads these fields
     * to emit `cascade_runaway_rate_tripped` Signal.
     */
    runawayTripped?: boolean;
    runawayTotalUsd?: number;
    runawaySeverity?: "warning" | "critical";
    /**
     * Dangerous-combo detector match (spec § 2.6 + DangerousComboDetector.ts).
     * Set when ALL FOUR features match: background trigger + tier1 +
     * cascade-decided + large context. Audit layer reads to emit
     * `cascade_dangerous_combo` Signal.
     */
    dangerousComboMatched?: boolean;
    dangerousComboContextTokens?: number;
    /**
     * Holdout cohort assignment (spec § 2.3 Component 5). True for
     * sessions deterministically assigned to the un-cascaded baseline.
     * Phase 4 audit layer uses these spans as the un-contaminated
     * reference signal for the learning loop. Set on every span; the
     * audit layer filters by it.
     */
    holdout?: boolean;
    /**
     * Variant tag for shadow A/B (spec § 2.3 Component 2). Phase 2:
     * production is "A", holdout cohort is "baseline". Phase 4+ shadow
     * variants will get "B", "C", etc. Set on every span.
     */
    variant?: string;
    /**
     * Shadow-mode CascadeController verdict (spec § 2.2). The verdict
     * the cascade controller WOULD have produced for this turn — recorded
     * here for validation but NOT applied to routing (the keyword
     * classifier still drives the actual model selection in Phase 2).
     * Phase 3 cutover wires the verdict to drive routing.
     *
     * shadow_verdict_tier: what cascade said the tier should be
     * shadow_verdict_escalation_event: "escalated" | "deescalated" | "held"
     * shadow_verdict_ask_hint_emitted: true when cascade would have
     *   suggested asking the user about tier1 escalation
     * shadow_verdict_disagrees: true when shadow_verdict != tier_intended
     *   (the classifier-recommended tier). Phase 2 metric: % of turns
     *   where cascade and classifier disagree. Phase 3 cutover decision
     *   hinges on these disagreements being explainable.
     */
    shadowVerdictTier?: string;
    shadowVerdictEscalationEvent?: "escalated" | "deescalated" | "held";
    shadowVerdictAskHintEmitted?: boolean;
    shadowVerdictDisagrees?: boolean;
}
export declare class CascadeTelemetry {
    private readonly config;
    private readonly logger;
    private readonly initializedDirs;
    private warnedEACCES;
    constructor(config: CascadeTelemetryConfig, logger: PluginLogger);
    /**
     * Write one Opik-shaped span describing a single agent turn.
     *
     * Best-effort: any I/O failure is logged at debug level and swallowed.
     * This is hot-path code; producer must not block on telemetry.
     */
    recordTurnSpan(input: RecordTurnSpanInput): void;
    /**
     * Build the Opik-span-shaped object. Field names match Python
     * ``OpikSpan.to_dict()``; see ``opik_client.py``.
     *
     * Exposed for unit testing — wraps no I/O.
     */
    buildSpan(input: RecordTurnSpanInput): Record<string, unknown>;
}
//# sourceMappingURL=CascadeTelemetry.d.ts.map