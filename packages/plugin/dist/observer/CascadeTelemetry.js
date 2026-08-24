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
import * as fs from "fs";
import * as path from "path";
// ── Implementation ───────────────────────────────────────────────────────────
export class CascadeTelemetry {
    config;
    logger;
    initializedDirs = new Set();
    warnedEACCES = false;
    constructor(config, logger) {
        this.config = config;
        this.logger = logger;
    }
    /**
     * Write one Opik-shaped span describing a single agent turn.
     *
     * Best-effort: any I/O failure is logged at debug level and swallowed.
     * This is hot-path code; producer must not block on telemetry.
     */
    recordTurnSpan(input) {
        let span;
        try {
            span = this.buildSpan(input);
        }
        catch (err) {
            // Building the span object should never fail, but if it does, log
            // and bail without throwing into the caller.
            this.logger.debug(`CascadeTelemetry: failed to build span: ${err}`);
            return;
        }
        const date = input.endedAt.toISOString().slice(0, 10);
        const spansDir = path.join(this.config.sharedDir, this.config.botId, "spans");
        if (!this.initializedDirs.has(spansDir)) {
            try {
                fs.mkdirSync(spansDir, { recursive: true });
                this.initializedDirs.add(spansDir);
            }
            catch (err) {
                if (err?.code === "EACCES" && !this.warnedEACCES) {
                    this.warnedEACCES = true;
                    this.logger.warn(`CascadeTelemetry: cannot create spans dir at ${spansDir}; ` +
                        `bot user lacks write on the parent. Run ` +
                        `'sudo evolve-admin deploy <bot>' on the mini — ` +
                        `fix_shared_dir_permissions() pre-creates this dir with ` +
                        `the right ownership. (Warning fires once per process.)`);
                }
                else if (err?.code !== "EACCES") {
                    this.logger.debug(`CascadeTelemetry: mkdir failed: ${err}`);
                }
                return;
            }
        }
        const filePath = path.join(spansDir, `spans-${date}.jsonl`);
        try {
            fs.appendFileSync(filePath, JSON.stringify(span) + "\n", { mode: 0o644 });
        }
        catch (err) {
            // Same EACCES guard as writeTurnToShared — if the file is owned by
            // another bot user, silently skip.
            if (err?.code !== "EACCES" && err?.code !== "EPERM") {
                this.logger.debug(`CascadeTelemetry: append failed: ${err}`);
            }
        }
    }
    /**
     * Build the Opik-span-shaped object. Field names match Python
     * ``OpikSpan.to_dict()``; see ``opik_client.py``.
     *
     * Exposed for unit testing — wraps no I/O.
     */
    buildSpan(input) {
        // Flatten struggle features into dot-notation keys under attributes
        // so they're easy to query in Opik / JSONL search. Mirrors how the
        // Python side already uses attributes for session_id / trigger_kind.
        //
        // Note: tier_used = actual (what billed), tier_intended = cascade's
        // intent. Both are recorded so the calibration loop reads truth
        // (per failure-mode review F8). A divergence flag is set when they
        // differ — that's a model_override_violated signal class.
        const tierDivergent = input.tierUsed !== null
            && input.tierIntended !== null
            && input.tierUsed !== input.tierIntended;
        const attributes = {
            session_id: input.sessionId,
            turn_index: input.turnIndex,
            "cascade.tier_used": input.tierUsed,
            "cascade.tier_intended": input.tierIntended,
            "cascade.tier_divergent": tierDivergent,
            "cascade.tier_chosen_by": input.tierChosenBy,
            // Holdout cohort + variant tagging (spec § 2.3 Component 5).
            // ALWAYS set so the audit layer can rely on the field being
            // present. Defaults: not in holdout, variant "A" (production).
            "cascade.holdout": input.holdout === true,
            "cascade.variant": input.variant ?? (input.holdout === true ? "baseline" : "A"),
        };
        if (input.triggerKind !== undefined) {
            attributes["cascade.trigger_kind"] = input.triggerKind;
        }
        // Granular consent provenance (spec § 4.1). The audit-layer Labeler
        // reads `cascade.consent_source` alongside `cascade.tier_chosen_by`
        // to attribute outcomes — `ui_chip` is the strongest Signal-#1
        // ground-truth, `ask_hint_agreed` is the weaker bot-asked variant,
        // and `bot_initiated` covers session.set_tier MCP usage.
        if (input.consentSource) {
            attributes["cascade.consent_source"] = input.consentSource;
        }
        if (input.struggle) {
            // score may be null on payload drift — record as-is so consumers
            // can distinguish "couldn't measure" from "measured: 0" (spec § 2.7).
            attributes["cascade.struggle.score"] = input.struggle.score;
            for (const [k, v] of Object.entries(input.struggle.features)) {
                attributes[`cascade.struggle.features.${k}`] = v;
            }
            for (const [k, v] of Object.entries(input.struggle.raw)) {
                attributes[`cascade.struggle.raw.${k}`] = v;
            }
            if (input.struggle.payload_drift) {
                // Names the reason cascade couldn't measure this turn (e.g.,
                // "no_messages"). Audit layer / session_rollup buckets by this
                // and emits the cascade_payload_unexpected Signal.
                attributes["cascade.struggle.payload_drift"] = input.struggle.payload_drift;
            }
        }
        if (input.legacySessionClass !== undefined) {
            // Read-compat field — removed at Phase 3.
            attributes["legacy.session_class"] = input.legacySessionClass;
        }
        // OC's per-turn success flag (added 2026-06-06 alongside the struggle-
        // payload sampler diagnostic — PR #2296). The detector's success=false
        // floor caps unmeasured struggle at 0.5, which means the
        // tier2_struggle_threshold=0.65 only triggers when real feature signal
        // is present. Audit consumers need the raw success bit to compute
        // "cascade routed up ∩ turn succeeded" vs "routed up ∩ failed" without
        // recomputing from the annotation JSONL.
        //
        // Gated on `!== undefined` (NOT truthiness) so `false` is preserved —
        // the false case is the load-bearing one for cascade analysis.
        if (input.success !== undefined) {
            attributes["cascade.success"] = input.success;
        }
        // Pre-flight intent router decision (Phase 1 of
        // spec-preflight-intent-router-2026-06-06.md). When the router ran
        // (i.e., user_turn trigger AND router enabled for this bot), four
        // attributes land on the span:
        //   cascade.preflight.tier        tier1/tier2/tier3 or null (abstain)
        //   cascade.preflight.reason      short label e.g. "regex:design_word"
        //   cascade.preflight.layer       "regex" | "bot_prior" | "haiku" | "abstain"
        //   cascade.preflight.confidence  [0, 1]
        //   cascade.preflight.latency_ms  router observation, for SLO tracking
        //
        // Phase 1 ships abstain-only — every user_turn span shows
        // layer="abstain". Phase 2 wires regex; layer flips to "regex" on
        // matching prompts. The audit detector
        // (packages/analyzer/cascade/audit_runner.py) reads these to compute
        // over-escalation / under-escalation / cascade-corrected rates.
        if (input.preflight) {
            attributes["cascade.preflight.tier"] = input.preflight.tier;
            attributes["cascade.preflight.reason"] = input.preflight.reason;
            attributes["cascade.preflight.layer"] = input.preflight.layer;
            attributes["cascade.preflight.confidence"] = input.preflight.confidence;
            attributes["cascade.preflight.latency_ms"] = input.preflight.latency_ms;
        }
        // Cross-turn struggle aggregate (added 2026-06-07). The 3 counts
        // + velocity that the cascade controller uses to short-circuit
        // per-turn persistence when session-level struggle is elevated.
        // Audit layer reads these to compute "aggregate fired but
        // outcome was fine" (false positive) or "no aggregate fired but
        // user struggled" (false negative — needs broader patterns).
        if (input.sessionAggregate) {
            attributes["cascade.session.shell_error_pastes_recent"] =
                input.sessionAggregate.shell_error_paste_count;
            attributes["cascade.session.bot_self_corrections_recent"] =
                input.sessionAggregate.bot_self_correction_count;
            attributes["cascade.session.turn_velocity_per_min"] =
                input.sessionAggregate.turn_velocity_per_min;
            attributes["cascade.session.turn_count"] =
                input.sessionAggregate.turn_count;
        }
        // LLM-as-judge verdict (2026-06-07). Sharpening layer on top of
        // the aggregator's pre-threshold gate — populated only when a
        // prior turn's async judge call completed. Audit layer reads
        // these to grade judge accuracy (true/false positive/negative
        // rate). See sessionJudge field doc above for the grading model.
        if (input.sessionJudge) {
            attributes["cascade.session.judge.verdict"] = input.sessionJudge.verdict;
            attributes["cascade.session.judge.reason"] = input.sessionJudge.reason;
            attributes["cascade.session.judge.latency_ms"] = input.sessionJudge.latency_ms;
            attributes["cascade.session.judge.triggered_by"] = input.sessionJudge.triggered_by;
        }
        const usage = {};
        if (input.inputTokens !== undefined)
            usage.input_tokens = input.inputTokens;
        if (input.outputTokens !== undefined)
            usage.output_tokens = input.outputTokens;
        if (input.cacheReadTokens !== undefined)
            usage.cache_read_tokens = input.cacheReadTokens;
        if (input.cacheWriteTokens !== undefined)
            usage.cache_write_tokens = input.cacheWriteTokens;
        const tags = ["cascade", "turn"];
        if (input.tierUsed)
            tags.push(`tier:${input.tierUsed}`);
        if (input.tierChosenBy === "user_request")
            tags.push("user_chose_tier");
        if (tierDivergent)
            tags.push("tier_divergent");
        if (input.triggerKind)
            tags.push(`trigger:${input.triggerKind}`);
        if (input.runawayTripped) {
            // Audit layer (Python side) picks this up and emits the
            // `cascade_runaway_rate_tripped` Signal at the recorded severity.
            attributes["cascade.runaway_rate.tripped"] = true;
            attributes["cascade.runaway_rate.total_usd"] = input.runawayTotalUsd ?? 0;
            attributes["cascade.runaway_rate.severity"] = input.runawaySeverity ?? "warning";
            tags.push("runaway_tripped");
        }
        if (input.dangerousComboMatched) {
            // Audit layer reads + emits `cascade_dangerous_combo` Signal at
            // WARNING (no escalation logic — every single occurrence matters).
            attributes["cascade.dangerous_combo.matched"] = true;
            if (input.dangerousComboContextTokens !== undefined) {
                attributes["cascade.dangerous_combo.context_tokens"] = input.dangerousComboContextTokens;
            }
            tags.push("dangerous_combo");
        }
        // Shadow-mode CascadeController verdict (spec § 2.2). Phase 2:
        // records what cascade WOULD have decided so Phase 3 cutover review
        // can validate disagreements with the keyword classifier are
        // explainable before flipping cascade live.
        if (input.shadowVerdictTier !== undefined) {
            attributes["cascade.shadow_verdict.tier"] = input.shadowVerdictTier;
        }
        if (input.shadowVerdictEscalationEvent !== undefined) {
            attributes["cascade.shadow_verdict.escalation_event"] = input.shadowVerdictEscalationEvent;
            // Also mirror to the un-prefixed attribute name. The pressure
            // watchdog (packages/analyzer/cascade/pressure_watchdog.py) reads
            // `cascade.escalation_event` to compute escalations_in_15min /
            // escalation_storm. In Phase 2 the shadow verdict IS what cascade
            // would have done; downstream readers should see the same data
            // whether or not the verdict drove routing. Phase 3 cutover will
            // drop the prefixed form and keep only the bare attribute.
            attributes["cascade.escalation_event"] = input.shadowVerdictEscalationEvent;
        }
        if (input.shadowVerdictAskHintEmitted) {
            attributes["cascade.shadow_verdict.ask_hint_emitted"] = true;
            tags.push("shadow_ask_hint");
        }
        if (input.shadowVerdictDisagrees) {
            attributes["cascade.shadow_verdict.disagrees"] = true;
            tags.push("shadow_disagrees");
        }
        const span = {
            name: "bot_session_turn",
            start_time: input.startedAt.toISOString(),
            end_time: input.endedAt.toISOString(),
            type: "general",
            // Use sessionId as the trace_id so all turns in a session group
            // naturally under one Opik trace. span_id is per-turn and
            // deterministic so re-emits collide rather than duplicate.
            trace_id: input.sessionId,
            span_id: `${input.sessionId}:${input.turnIndex}`,
            parent_span_id: null,
            input: {},
            output: {},
            metadata: {},
            tags,
            model: input.model ?? null,
            provider: input.provider ?? null,
            usage,
            total_cost: input.costUsd ?? null,
            error_info: input.error
                ? { message: input.error.message, code: input.error.code ?? null }
                : null,
            bot_id: this.config.botId,
            producer: "cascade_telemetry",
            attributes,
        };
        return span;
    }
}
//# sourceMappingURL=CascadeTelemetry.js.map