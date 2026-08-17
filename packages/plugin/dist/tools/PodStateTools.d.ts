/**
 * PodStateTools — the primary bot's read window into pod state.
 *
 * ONE consolidated tool (``pod_state``) replacing the eight per-endpoint
 * tools that shipped with the primary-bot-interface spec (pod_status,
 * list_signals, list_proposals, recent_watchdog, spend_rollup,
 * recent_turns, describe_bot, list_audits). Overhead-budget B2 v2
 * (docs/spec-evolve-overhead-budget-2026-07-31.md): eight schemas rode in
 * every primary-bot prompt (~6.5k chars raw); one query-enum tool carries
 * the same surface for a fraction of the weight, and gives the model one
 * obvious read tool instead of eight near-siblings to pick between.
 *
 * Spec: docs/spec-primary-bot-interface-2026-05-14.md §5. The bot calls
 * this when admin asks "what's the pod doing right now?" — grounded reads
 * against shared state rather than guessing from training data.
 *
 * Transport: the admin-daemon UNIX SOCKET (``{sharedDir}/admin-daemon.sock``),
 * NOT loopback TCP :5050. See EvoDispatchClient for the why — admin auth is ON
 * by default (#2621) so a cookieless TCP RPC 401s; the unix socket is exempted
 * + peer-uid bound server-side (#3265 / #3263 / #3267). The factory takes the
 * resolved ``socketPath`` (platform-keyed off ``sharedDir``); a
 * socket-unavailable condition surfaces the same clean tool error a TCP
 * failure produced.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export declare const POD_STATE_QUERIES: readonly ["status", "signals", "proposals", "watchdog", "spend", "turns", "bot", "audits"];
export type PodStateQuery = (typeof POD_STATE_QUERIES)[number];
export declare const PodStateParamsSchema: import("@sinclair/typebox").TObject<{
    query: import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"status" | "watchdog" | "signals" | "turns" | "bot" | "proposals" | "spend" | "audits">[]>;
    state: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    producer: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    hours: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
    window: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"1d">, import("@sinclair/typebox").TLiteral<"7d">, import("@sinclair/typebox").TLiteral<"30d">]>>;
    limit: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
}>;
export type PodStateParams = Static<typeof PodStateParamsSchema>;
export declare function createPodStateToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        query: import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"status" | "watchdog" | "signals" | "turns" | "bot" | "proposals" | "spend" | "audits">[]>;
        state: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        producer: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        hours: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
        window: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"1d">, import("@sinclair/typebox").TLiteral<"7d">, import("@sinclair/typebox").TLiteral<"30d">]>>;
        limit: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
//# sourceMappingURL=PodStateTools.d.ts.map