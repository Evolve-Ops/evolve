/**
 * OutwardActionLedger
 *
 * Bot-side outward-action telemetry for the autonomy ladder (Phase B,
 * internal/spec-autonomy-ladder-2026-06-10.md §1.3 + §8 OQ-3 — decided
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

import * as fs from "fs";
import * as path from "path";
import type { PluginLogger } from "openclaw/plugin-sdk/types";

// ── Public types ─────────────────────────────────────────────────────────────

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

const MCP_PREFIX = "mcp__";

// ── Pure extraction (exported for tests) ────────────────────────────────────

/**
 * Parse an ``mcp__<server>__<tool>`` name into its parts, or null when
 * the name isn't MCP-shaped.
 */
export function parseMcpToolName(
  name: unknown,
): { integration_id: string; tool_name: string } | null {
  if (typeof name !== "string" || !name.startsWith(MCP_PREFIX)) return null;
  const rest = name.slice(MCP_PREFIX.length);
  const sep = rest.indexOf("__");
  if (sep <= 0 || sep + 2 >= rest.length) return null;
  return {
    integration_id: rest.slice(0, sep),
    tool_name: rest.slice(sep + 2),
  };
}

/**
 * Extract every MCP tool call from an agent_end messages payload.
 *
 * Shape-tolerant by contract: unexpected payloads yield ``[]`` rather
 * than throwing. Result status is matched from ``tool_result`` blocks
 * by ``tool_use_id`` (Anthropic-style); calls with no matching result
 * block report "unknown".
 */
export function extractMcpToolCalls(messages: unknown): McpToolCall[] {
  if (!Array.isArray(messages)) return [];

  // First pass: collect tool_result status by tool_use_id.
  const resultById = new Map<string, "ok" | "error">();
  for (const msg of messages) {
    const content = (msg as Record<string, unknown> | null)?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      const b = block as Record<string, unknown> | null;
      if (!b || b.type !== "tool_result") continue;
      const id = b.tool_use_id;
      if (typeof id !== "string") continue;
      resultById.set(id, b.is_error === true ? "error" : "ok");
    }
  }

  const calls: McpToolCall[] = [];
  const push = (name: unknown, id: unknown) => {
    const parsed = parseMcpToolName(name);
    if (!parsed) return;
    const callId = typeof id === "string" ? id : "";
    const result = callId ? resultById.get(callId) ?? "unknown" : "unknown";
    calls.push({ ...parsed, result, call_id: callId });
  };

  for (const msg of messages) {
    const m = msg as Record<string, unknown> | null;
    if (!m) continue;
    // Anthropic-style: content[] blocks with type === "tool_use".
    if (Array.isArray(m.content)) {
      for (const block of m.content) {
        const b = block as Record<string, unknown> | null;
        if (b && b.type === "tool_use") push(b.name, b.id);
      }
    }
    // OpenAI-style: top-level tool_calls[] with function.name.
    if (Array.isArray(m.tool_calls)) {
      for (const tc of m.tool_calls) {
        const t = tc as Record<string, unknown> | null;
        const fn = t?.function as Record<string, unknown> | undefined;
        if (fn) push(fn.name, t?.id);
      }
    }
  }
  return calls;
}

// ── Writer ───────────────────────────────────────────────────────────────────

export class OutwardActionLedger {
  private readonly config: OutwardActionLedgerConfig;
  private readonly logger: PluginLogger;
  private dirInitialized = false;
  private warnedEACCES = false;

  constructor(config: OutwardActionLedgerConfig, logger: PluginLogger) {
    this.config = config;
    this.logger = logger;
  }

  /**
   * Record every MCP tool call observed in one completed turn.
   * Best-effort; never throws into the caller.
   */
  recordTurn(messages: unknown, sessionId: string, turnId: string): void {
    let calls: McpToolCall[];
    try {
      calls = extractMcpToolCalls(messages);
    } catch (err) {
      this.logger.debug(`OutwardActionLedger: extract failed: ${err}`);
      return;
    }
    if (calls.length === 0) return;

    const now = new Date();
    const ledgerDir = path.join(
      this.config.sharedDir,
      this.config.botId,
      "outward-actions",
    );
    if (!this.dirInitialized) {
      try {
        fs.mkdirSync(ledgerDir, { recursive: true });
        this.dirInitialized = true;
      } catch (err: any) {
        if (err?.code === "EACCES" && !this.warnedEACCES) {
          this.warnedEACCES = true;
          this.logger.warn(
            `OutwardActionLedger: cannot create ledger dir at ${ledgerDir}; ` +
            `bot user lacks write on the parent. Run ` +
            `'sudo evolve-admin deploy <bot>' on the pod host. ` +
            `(Warning fires once per process.)`,
          );
        } else if (err?.code !== "EACCES") {
          this.logger.debug(`OutwardActionLedger: mkdir failed: ${err}`);
        }
        return;
      }
    }

    const filePath = path.join(
      ledgerDir,
      `actions-${now.toISOString().slice(0, 10)}.jsonl`,
    );
    const lines = calls
      .map((c) =>
        JSON.stringify({
          ts: now.toISOString(),
          integration_id: c.integration_id,
          tool_name: c.tool_name,
          result: c.result,
          call_id: c.call_id,
          session_id: sessionId,
          turn_id: turnId,
        }),
      )
      .join("\n");
    try {
      fs.appendFileSync(filePath, lines + "\n", { mode: 0o644 });
    } catch (err: any) {
      if (err?.code === "ENOENT") {
        // The dir vanished after we cached its creation — re-create on
        // the next turn instead of failing silently until restart.
        this.dirInitialized = false;
      }
      if (err?.code !== "EACCES" && err?.code !== "EPERM") {
        this.logger.debug(`OutwardActionLedger: append failed: ${err}`);
      }
    }
  }
}
