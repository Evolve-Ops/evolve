/**
 * PrimaryBotTools — Bundle 2B of the primary-bot-interface spec.
 *
 * Three OC tools registered only on the primary bot (role === "primary"):
 *   - evolve_help_search(query, k?)   → POST /api/evo/help/search
 *   - evolve_help_read(doc_id)        → POST /api/evo/help/read
 *   - submit_intake(kind, body, ...)  → POST /api/evo/intake (+ promote)
 *
 * Anti-hallucination scaffolding is the load-bearing reason for these
 * tools: the bot shouldn't guess about Evolve internals; it should
 * retrieve. See spec §4.3 and §5.1.
 *
 * Spec: internal/spec-primary-bot-interface-2026-05-14.md.
 *
 * Transport: the admin-daemon UNIX SOCKET (``{sharedDir}/admin-daemon.sock``),
 * NOT loopback TCP :5050. See EvoDispatchClient for the why — admin auth is ON
 * by default (#2621) so a cookieless TCP RPC 401s; the unix socket is exempted
 * + peer-uid bound server-side (#3265 / #3263 / #3267). RPC-2 of that fix. Each
 * factory takes the resolved ``socketPath`` (platform-keyed off ``sharedDir``).
 * The admin server runs as the `evolve` user and has the right ACLs to read
 * {shared_dir}/help_index.json and write {shared_dir}/intake/.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export declare const HelpSearchParamsSchema: import("@sinclair/typebox").TObject<{
    query: import("@sinclair/typebox").TString;
    k: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
}>;
export type HelpSearchParams = Static<typeof HelpSearchParamsSchema>;
export declare function createHelpSearchToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        query: import("@sinclair/typebox").TString;
        k: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TInteger>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const HelpReadParamsSchema: import("@sinclair/typebox").TObject<{
    doc_id: import("@sinclair/typebox").TString;
}>;
export type HelpReadParams = Static<typeof HelpReadParamsSchema>;
export declare function createHelpReadToolFactory(logger: PluginLogger, socketPath?: string): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        doc_id: import("@sinclair/typebox").TString;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const SubmitIntakeParamsSchema: import("@sinclair/typebox").TObject<{
    kind: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"bug">, import("@sinclair/typebox").TLiteral<"feature">, import("@sinclair/typebox").TLiteral<"question">]>;
    body: import("@sinclair/typebox").TString;
    promote: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TBoolean>;
    include_transcript: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TBoolean>;
}>;
export type SubmitIntakeParams = Static<typeof SubmitIntakeParamsSchema>;
export declare function createSubmitIntakeToolFactory(config: {
    botId: string;
    socketPath?: string;
}, logger: PluginLogger): (ctx: {
    sessionKey?: string;
    sessionId?: string;
    messageChannel?: string;
    agentId?: string;
}) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        kind: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"bug">, import("@sinclair/typebox").TLiteral<"feature">, import("@sinclair/typebox").TLiteral<"question">]>;
        body: import("@sinclair/typebox").TString;
        promote: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TBoolean>;
        include_transcript: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TBoolean>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
//# sourceMappingURL=PrimaryBotTools.d.ts.map