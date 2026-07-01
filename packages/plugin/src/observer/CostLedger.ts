/**
 * CostLedger — forensically-tagged cost event emitter.
 *
 * Every billable LLM call gets a JSONL record with enough lineage to answer
 * "what did that cost, and why?" at rollup time. Records land in the same
 * annotations directory as turn/session annotations (reusing the existing
 * writer path + mkdir cache + cross-gateway EACCES handling).
 *
 * Schema (type: "cost_event", schema_version: 2):
 * {
 *   schema_version: 2,
 *   type: "cost_event",
 *   ts: string,                    // ISO-8601 UTC
 *   timestamp_local: string|null,  // ISO-8601 in pod-local tz (v2 — for operators)
 *   bot_id: string,
 *   session_id: string | null,     // OC session id (null when unknown)
 *   trigger_kind: "user_turn"|"heartbeat"|"summarizer"|"classifier"|"task_extractor"|"fallback"|"unknown",
 *   model: string,                 // full model string from the LLM response
 *   provider: string,
 *   cache_state: "fresh"|"warm"|"invalidated"|"unknown",
 *   input_tokens: number,
 *   output_tokens: number,
 *   cache_read_tokens: number,
 *   cache_write_tokens: number,
 *   cost_usd: number,              // 6-decimal-place USD
 *   user_id: string | null,        // (v2) channel-native user id when trigger=user_turn
 *   user_display_name: string|null,// (v2) cached display name; null when no cache hit / DNT
 *   channel_id: string | null,     // (v2) channel-native channel id (Slack channel, Telegram chat)
 *   channel_kind: string | null,   // (v2) "slack_dm"|"slack_channel"|"telegram_dm"|"telegram_group"|
 *                                  //      "discord_dm"|"discord_channel"|"internal"|null
 * }
 *
 * Design notes:
 *   - Emitted at the `llm_output` hook, which fires once per Anthropic call
 *     (including subagent calls from the summarizer + classifier). This gives
 *     us per-call granularity without special-casing each subagent.
 *   - `trigger_kind` is inferred from ctx.trigger (OC's call-reason tag) plus
 *     heuristics on the model + surrounding state. Unknown is a fine default —
 *     rollup code still aggregates correctly, just without attribution.
 *   - `cache_state` is inferred from the Anthropic response's cache-related
 *     token counts. No cache fields → "unknown" (the model may not support
 *     prompt caching at all). See `inferCacheState` for the rules.
 *   - The v2 fields (timestamp_local, user_id, user_display_name, channel_id,
 *     channel_kind) are all optional / nullable. Old v1 records read by
 *     `observations/access.py` flow through unchanged; downstream consumers
 *     (cost_watchdog, Alerts UI) treat absent values as "(unknown)". This
 *     keeps backward compat while letting cost alerts tell the operator
 *     WHO was on the other end of the expensive session.
 */

export type TriggerKind =
  | "user_turn"
  | "heartbeat"
  | "cron_app"
  | "subagent"
  | "summarizer"
  | "classifier"
  | "task_extractor"
  | "fallback"
  | "unknown";

export type CacheState = "fresh" | "warm" | "invalidated" | "unknown";

/** Schema v2: channel-of-origin context for cost events. All fields nullable;
 *  consumers treat null as "unknown". When `trigger_kind` is internal-only
 *  (heartbeat / summarizer / classifier / etc.), `user_id` and `channel_*`
 *  stay null and `channel_kind` is "internal".
 */
export type ChannelKind =
  | "slack_dm"
  | "slack_channel"
  | "telegram_dm"
  | "telegram_group"
  | "discord_dm"
  | "discord_channel"
  | "internal"
  | null;

export interface CostEventInput {
  botId: string;
  sessionId: string | null;
  triggerKind: TriggerKind;
  model: string;
  provider: string;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  costUsd: number;
  // v2 enrichment — all optional so v1 call sites keep working without
  // touching every emitter at once. The TS plugin path doesn't have
  // reliable user/channel context from the OC hook surface; the
  // authoritative population happens in the Python cost_event_converter
  // where the turn-collector record carries user_id + channel directly.
  // These fields exist here so a future TS-side emitter (e.g. if OC
  // starts threading user/channel through llm_output ctx) can populate
  // them without another schema bump.
  userId?: string | null;
  userDisplayName?: string | null;
  channelId?: string | null;
  channelKind?: ChannelKind;
  // ISO-8601 timestamp in the pod's local timezone. Optional; consumers
  // fall back to converting `ts` themselves when absent.
  timestampLocal?: string | null;
}

/** Classify a call's cache posture from Anthropic's usage fields.
 *
 * Semantics (what the rollup needs to see):
 *   - "warm"        — cache hit: the prompt cache carried most of the context
 *   - "invalidated" — prompt was re-written fresh; we paid cacheWrite cost
 *                     without meaningful cacheRead benefit. This is the
 *                     post-key-rotation / post-config-churn fingerprint.
 *   - "fresh"       — first turn in a new session; no cache fields at all
 *   - "unknown"     — model doesn't expose cache fields or usage missing
 */
export function inferCacheState(
  cacheReadTokens: number,
  cacheWriteTokens: number,
  inputTokens: number
): CacheState {
  if (cacheReadTokens === 0 && cacheWriteTokens === 0) {
    // No cache participation — either a first turn, a model without
    // prompt-cache support, or an opt-out. Treat as fresh only if there
    // actually *were* input tokens; otherwise "unknown" (malformed response).
    return inputTokens > 0 ? "fresh" : "unknown";
  }
  const totalPrompt = inputTokens + cacheReadTokens + cacheWriteTokens;
  if (totalPrompt === 0) return "unknown";
  const readRatio = cacheReadTokens / totalPrompt;
  // Warm when ≥80% of the prompt came from the cache. Invalidated when we
  // wrote a lot of cache but read little — i.e. we re-paid cacheWrite cost
  // without enjoying cacheRead savings. The thresholds are intentionally
  // generous; we'd rather a borderline case land in "unknown" than be
  // misclassified as a confident "invalidated".
  if (readRatio >= 0.80) return "warm";
  if (cacheWriteTokens > 0 && readRatio <= 0.10) return "invalidated";
  return "unknown";
}

/** Map OC's `ctx.trigger` plus surrounding hints to our canonical TriggerKind.
 *
 *  OC itself sets `ctx.trigger` to values like "user_message", "cron",
 *  "heartbeat", "subagent" etc. depending on gateway version. We normalise
 *  them here so the ledger is stable across gateway upgrades.
 *
 *  Subagent calls (classifier + summarizer) look identical at the llm_output
 *  layer; the `idempotencyKey` on the subagent run *does* have a stable
 *  prefix, but OC doesn't thread that back through to llm_output ctx in every
 *  version. For now we classify subagent calls as "unknown" here and let the
 *  emit call-site (if it's inside the summarizer or classifier) override to
 *  the precise kind when it has the context to do so.
 */
export function inferTriggerKind(
  ctxTrigger: unknown,
  modelHint?: string
): TriggerKind {
  const t = typeof ctxTrigger === "string" ? ctxTrigger.toLowerCase() : "";
  if (t.includes("user") || t.includes("message") || t === "chat") return "user_turn";
  if (t.includes("heartbeat") || t.includes("cron") || t.includes("timer")) return "heartbeat";
  if (t.includes("summar")) return "summarizer";
  if (t.includes("classif") || t.includes("tier")) return "classifier";
  if (t.includes("task") && t.includes("extract")) return "task_extractor";
  if (t.includes("fallback") || t.includes("retry")) return "fallback";

  // Haiku-only calls with no session are almost always the subagent classifier
  // in current gateway versions. Not a hard rule — just a heuristic that keeps
  // attribution non-zero until OC threads ctx.trigger through consistently.
  if (modelHint && modelHint.toLowerCase().includes("haiku")) return "classifier";
  return "unknown";
}

export const COST_EVENT_SCHEMA_VERSION = 2;

/**
 * Build the cost_event record for JSONL append. Pure function — the actual
 * write is delegated to the caller (TurnObserver's `writeAnnotation`, which
 * owns the mkdir cache + EACCES handling).
 *
 * Schema v2: optional user/channel/local-time fields. When the caller
 * doesn't supply them they're emitted as null so the record shape is
 * stable across all writers; readers tolerant of v1 records will simply
 * see null where v1 had nothing — same downstream behavior.
 */
export function buildCostEvent(
  input: CostEventInput,
  now: Date = new Date()
): Record<string, unknown> {
  return {
    schema_version: COST_EVENT_SCHEMA_VERSION,
    type: "cost_event",
    ts: now.toISOString(),
    timestamp_local: input.timestampLocal ?? null,
    bot_id: input.botId,
    session_id: input.sessionId,
    trigger_kind: input.triggerKind,
    model: input.model,
    provider: input.provider,
    cache_state: inferCacheState(
      input.cacheReadTokens,
      input.cacheWriteTokens,
      input.inputTokens
    ),
    input_tokens: input.inputTokens,
    output_tokens: input.outputTokens,
    cache_read_tokens: input.cacheReadTokens,
    cache_write_tokens: input.cacheWriteTokens,
    cost_usd: input.costUsd,
    user_id: input.userId ?? null,
    user_display_name: input.userDisplayName ?? null,
    channel_id: input.channelId ?? null,
    channel_kind: input.channelKind ?? null,
  };
}
