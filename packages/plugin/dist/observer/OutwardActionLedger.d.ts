/**
 * OutwardActionLedger
 *
 * Bot-side outward-action telemetry for the autonomy ladder (Phase B,
 * docs/spec-autonomy-ladder-2026-06-10.md §1.3 + §8 OQ-3 — decided
 * bot-side: this plugin is the only component that legitimately sees
 * tool calls, per principle-per-bot-inference).
 *
 * At every completed agent turn, scan the agent_end ``messages`` payload
 * for MCP tool calls (``tool_use`` content blocks whose name is
 * ``mcp__<server>__<tool>``, plus the OpenAI-style top-level
 * ``tool_calls[]`` shape — the same dual-shape tolerance as
 * StruggleDetector) and append one record per call to:
 *
 *   {sharedDir}/{botId}/outward-actions/actions-YYYY-MM-DD.jsonl
 *
 * (the CascadeTelemetry per-bot path convention; bot user owns the
 * files; UTC-dated; append-only; mode 0644 so the evolve-side reader
 * — packages/analyzer/autonomy/actions_ledger.py — can read them.)
 *
 * Record shape, one JSON object per line:
 *
 *   { ts, integration_id, tool_name, result: "ok"|"error"|"unknown",
 *     session_id, turn_id }
 *
 * Privacy contract: tool NAMES and ids only — never the tool's input,
 * arguments, recipients, or any message content. Classification into
 * outward verbs (send/forward/delete/...) happens evolve-side against
 * the autonomy catalog; this writer records every mcp__ call it sees
 * so the verb vocabulary lives in exactly one place.
 *
 * Failure mode: best-effort. Any I/O error is logged at debug level and
 * swallowed — the plugin's hot path must not block on telemetry writes
 * (the CascadeTelemetry contract).
 */
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export interface OutwardActionLedgerConfig {
    sharedDir: string;
    botId: string;
}
export interface McpToolCall {
    /** The bot's mcp.servers key — the autonomy integration_id. */
    integration_id: string;
    /** Bare tool name with the mcp__<server>__ prefix stripped. */
    tool_name: string;
    /** "ok" | "error" | "unknown" — matched from tool_result blocks. */
    result: "ok" | "error" | "unknown";
    /** The provider's tool_use/call id — evolve-side dedup handle in
     * case a payload is ever delivered twice. Empty when absent. */
    call_id: string;
}
/**
 * Parse an ``mcp__<server>__<tool>`` name into its parts, or null when
 * the name isn't MCP-shaped.
 */
export declare function parseMcpToolName(name: unknown): {
    integration_id: string;
    tool_name: string;
} | null;
/**
 * Extract every MCP tool call from an agent_end messages payload.
 *
 * Shape-tolerant by contract: unexpected payloads yield ``[]`` rather
 * than throwing. Result status is matched from ``tool_result`` blocks
 * by ``tool_use_id`` (Anthropic-style); calls with no matching result
 * block report "unknown".
 */
export declare function extractMcpToolCalls(messages: unknown): McpToolCall[];
export declare class OutwardActionLedger {
    private readonly config;
    private readonly logger;
    private dirInitialized;
    private warnedEACCES;
    constructor(config: OutwardActionLedgerConfig, logger: PluginLogger);
    /**
     * Record every MCP tool call observed in one completed turn.
     * Best-effort; never throws into the caller.
     */
    recordTurn(messages: unknown, sessionId: string, turnId: string): void;
}
//# sourceMappingURL=OutwardActionLedger.d.ts.map