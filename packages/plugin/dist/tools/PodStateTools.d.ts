/**
 * PodStateTools — Bundle 3 of the primary-bot-interface spec.
 *
 * Four read-only OC tools registered only on the primary bot:
 *   - pod_status()                   → GET /api/primary/state/pod_status
 *   - list_signals(...)              → GET /api/primary/state/signals
 *   - list_proposals(...)            → GET /api/primary/state/proposals
 *   - recent_watchdog(...)           → GET /api/primary/state/watchdog
 *
 * Spec: docs/spec-primary-bot-interface-2026-05-14.md §5. The bot
 * calls these when admin asks "what's the pod doing right now?" —
 * grounded reads against shared state rather than guessing from
 * training data.
 *
 * Transport: the admin-daemon UNIX SOCKET (``{sharedDir}/admin-daemon.sock``),
 * NOT loopback TCP :5050. See EvoDispatchClient for the why — admin auth is ON
 * by default (#2621) so a cookieless TCP RPC 401s; the unix socket is exempted
 * + peer-uid bound server-side (#3265 / #3263 / #3267). RPC-2 of that fix. Each
 * factory takes the resolved ``socketPath`` (platform-keyed off ``sharedDir``);
 * a socket-unavailable condition surfaces the same clean tool error a TCP
 * failure produced.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export declare const PodStatusParamsSchema: import("@sinclair/typebox").TObject<{}>;
export declare function createPodStatusToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{}>;
    execute(_toolCallId: string, _rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const ListSignalsParamsSchema: import("@sinclair/typebox").TObject<{
    state: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"firing">, import("@sinclair/typebox").TLiteral<"snoozed">, import("@sinclair/typebox").TLiteral<"resolved">, import("@sinclair/typebox").TLiteral<"dismissed">, import("@sinclair/typebox").TLiteral<"all">]>>;
    producer: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    limit: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
}>;
export type ListSignalsParams = Static<typeof ListSignalsParamsSchema>;
export declare function createListSignalsToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        state: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"firing">, import("@sinclair/typebox").TLiteral<"snoozed">, import("@sinclair/typebox").TLiteral<"resolved">, import("@sinclair/typebox").TLiteral<"dismissed">, import("@sinclair/typebox").TLiteral<"all">]>>;
        producer: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
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
export declare const ListProposalsParamsSchema: import("@sinclair/typebox").TObject<{
    state: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"pending">, import("@sinclair/typebox").TLiteral<"snoozed">, import("@sinclair/typebox").TLiteral<"applied">, import("@sinclair/typebox").TLiteral<"archived">, import("@sinclair/typebox").TLiteral<"active">, import("@sinclair/typebox").TLiteral<"all">]>>;
    limit: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
}>;
export type ListProposalsParams = Static<typeof ListProposalsParamsSchema>;
export declare function createListProposalsToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        state: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"pending">, import("@sinclair/typebox").TLiteral<"snoozed">, import("@sinclair/typebox").TLiteral<"applied">, import("@sinclair/typebox").TLiteral<"archived">, import("@sinclair/typebox").TLiteral<"active">, import("@sinclair/typebox").TLiteral<"all">]>>;
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
export declare const RecentWatchdogParamsSchema: import("@sinclair/typebox").TObject<{
    hours: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
    bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    limit: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
}>;
export type RecentWatchdogParams = Static<typeof RecentWatchdogParamsSchema>;
export declare function createRecentWatchdogToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        hours: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
        bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
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
export declare const SpendRollupParamsSchema: import("@sinclair/typebox").TObject<{
    window: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"1d">, import("@sinclair/typebox").TLiteral<"7d">, import("@sinclair/typebox").TLiteral<"30d">]>>;
    bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
}>;
export type SpendRollupParams = Static<typeof SpendRollupParamsSchema>;
export declare function createSpendRollupToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        window: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"1d">, import("@sinclair/typebox").TLiteral<"7d">, import("@sinclair/typebox").TLiteral<"30d">]>>;
        bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const RecentTurnsParamsSchema: import("@sinclair/typebox").TObject<{
    bot: import("@sinclair/typebox").TString;
    limit: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
}>;
export type RecentTurnsParams = Static<typeof RecentTurnsParamsSchema>;
export declare function createRecentTurnsToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        bot: import("@sinclair/typebox").TString;
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
export declare const DescribeBotParamsSchema: import("@sinclair/typebox").TObject<{
    bot: import("@sinclair/typebox").TString;
}>;
export type DescribeBotParams = Static<typeof DescribeBotParamsSchema>;
export declare function createDescribeBotToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        bot: import("@sinclair/typebox").TString;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const ListAuditsParamsSchema: import("@sinclair/typebox").TObject<{
    bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    limit: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
}>;
export type ListAuditsParams = Static<typeof ListAuditsParamsSchema>;
export declare function createListAuditsToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        bot: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
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