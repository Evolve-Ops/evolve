/**
 * ExecFailureAbsorber — channel hygiene for OpenClaw's raw exec-failure
 * trailers (design: internal/design-exec-failure-hygiene-2026-08-31.md, A1).
 *
 * The invariant: a channel message about an internal failure is either
 * explained-and-actionable in user language, or absorbed — raw
 * `⚠️ 🛠️ Exec failed: …` trailers never reach a user channel. Absorbed is
 * not vanished: every match is appended to the per-bot ledger at
 * {sharedDir}/{botId}/exec-failures/exec-failures-YYYY-MM-DD.jsonl, which
 * the admin-side `bot_exec_failures` Signal producer aggregates (A2).
 *
 * Seam (verified against openclaw/openclaw main AND the deployed
 * 2026.7.1-2 dist, 2026-08-31): the `message_sending` plugin hook fires in
 * the shared outbound delivery pipeline (`applyMessageSendingHook`,
 * src/infra/outbound/deliver-hooks.ts, invoked per-payload from
 * deliver-prepare) for BOTH channel-bound exec-failure flavors:
 *   - the host-composed tool-error warning (`buildFailureWarning`,
 *     src/agents/embedded-agent-runner/run/tool-error-warning.ts) pushed as
 *     its own reply payload when a run ends on a tool failure with no
 *     user-facing reply — the exact family that reached Telegram on
 *     2026-08-31; and
 *   - the exec-approval follow-up direct sends + heartbeat-relayed
 *     notify-on-exit events (both route through `sendMessage` →
 *     `sendDurableMessageBatchCore` → deliver-prepare).
 * A `{cancel: true}` result suppresses the payload
 * (`cancelled_by_message_sending_hook`); a `{content}` result rewrites it.
 * The hook is fail-open host-side (a thrown handler logs and delivery
 * proceeds), and it is CHANNEL-facing only — the model-visible tool result
 * is untouched (A4: the bot still sees and adapts to its own failures).
 *
 * OBSERVE-ONLY by default: every match is ledgered as `would_absorb` /
 * `would_strip` but the message is delivered unchanged. Set the plugin
 * config `execFailureAbsorb: true` (exact boolean, layer2-style fail-safe
 * arming) to actually absorb. Merging is non-arming.
 */
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export declare function isExecFailureTrailerLine(line: string): boolean;
export type AbsorbDecision = {
    /** Lines (trimmed) that matched the trailer family, in order. */
    matched: string[];
    /** Content with matched lines removed; null when nothing remains. */
    remaining: string | null;
};
/**
 * Scan `content` line-by-line, skipping fenced code blocks (a user asking
 * about a pasted trailer must not have their quote eaten — same semantics
 * as OC's own stripInternalTraceLines). Returns null when no line matched.
 */
export declare function decideAbsorb(content: string): AbsorbDecision | null;
export interface ExecFailureAbsorberConfig {
    sharedDir: string;
    botId: string;
    /** True only when plugin config carries the exact boolean `true`. */
    armed: boolean;
    /** Non-null when the arming key was present but not a strict boolean. */
    armedWarning: string | null;
}
type MessageSendingEvent = {
    to?: string;
    content?: string;
    metadata?: Record<string, unknown>;
};
type MessageSendingCtx = {
    channelId?: string;
    accountId?: string;
    conversationId?: string;
    sessionKey?: string;
};
export declare class ExecFailureAbsorber {
    private readonly config;
    private readonly logger;
    private dirInitialized;
    private warnedEACCES;
    constructor(config: ExecFailureAbsorberConfig, logger: PluginLogger);
    register(api: any): void;
    /**
     * message_sending handler. Fail-open BY CONSTRUCTION: every path that is
     * not a confident match returns undefined (deliver unchanged), and any
     * internal error is caught and logged — hygiene must never eat a real
     * reply or block delivery.
     */
    handleMessageSending(event: MessageSendingEvent, ctx: MessageSendingCtx): {
        content?: string;
        cancel?: boolean;
        cancelReason?: string;
    } | undefined;
    private nearMissCount;
    private static readonly NEAR_MISS_MAX_PER_PROCESS;
    private appendNearMiss;
    /** Append one row to the per-bot exec-failures ledger (A1's "absorbed ≠
     *  vanished"). Same conventions as OutwardActionLedger: shared-dir path,
     *  date-sharded UTC filename, one-shot mkdir, EACCES warns once. Returns
     *  whether the row actually landed — armed absorption is CONDITIONED on
     *  it (see handleMessageSending), so the caller must know. */
    private appendLedger;
}
export {};
//# sourceMappingURL=ExecFailureAbsorber.d.ts.map