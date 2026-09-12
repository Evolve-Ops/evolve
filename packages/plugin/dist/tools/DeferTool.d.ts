/**
 * DeferTool — Continuity Engine v2.
 *
 * Bots call defer() when they commit to acting later (after a delay, when an
 * event happens, on a schedule). Without it the bot has no persistence between
 * turns and the promise is silently lost — which is the failure mode v1 of CE
 * tried to detect after the fact and v2 just lets the bot prevent.
 *
 * The tool appends a JSONL row to /Users/<bot_id>/.openclaw/workspace/evolve/
 * defer-queue.jsonl. The bot has natural write access there as itself; the
 * defer_runner (running as the evolve user) picks it up via the read+write ACL
 * grant on workspace/evolve/ established by deploy.py's set_evolve_read_acl().
 *
 * Append uses POSIX O_APPEND, which guarantees atomicity for writes ≤ PIPE_BUF
 * (4096 bytes) — well above any defer row. No flock needed for the append path
 * at our throughput; the runner takes a flock when it rewrites the queue file
 * to drop fired rows, which is safe because rewrite is via tempfile + rename.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export declare const DeferToolParamsSchema: import("@sinclair/typebox").TObject<{
    due_at: import("@sinclair/typebox").TString;
    message: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    action: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
}>;
export type DeferToolParams = Static<typeof DeferToolParamsSchema>;
/**
 * Build the defer tool definition. Returned as an SDK tool factory: the
 * gateway calls the factory per-tool-call with a trusted context (sessionKey,
 * channelId, agentId) we can use to bind the row to its origin.
 *
 * Pass this directly to api.registerTool().
 */
export declare function createDeferToolFactory(config: {
    botId: string;
}, logger: PluginLogger): (ctx: {
    sessionKey?: string;
    sessionId?: string;
    messageChannel?: string;
    agentId?: string;
}) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        due_at: import("@sinclair/typebox").TString;
        message: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        action: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        content: {
            type: "text";
            text: string;
        }[];
        isError: boolean;
    } | {
        content: {
            type: "text";
            text: string;
        }[];
        isError?: undefined;
    }>;
};
//# sourceMappingURL=DeferTool.d.ts.map