/**
 * ExpandAppTool — the bot-facing ``expand_app`` tool: Tier-2 of the app
 * capability index (the recognition layer of the "just works" arc).
 *
 * Spec: docs/spec-app-invocation-just-works-2026-06-29.md §2.1. The bot's AGENTS.md
 * carries an always-on Tier-1 menu — one terse ``name — purpose`` line per installed
 * app, each ending ``→ expand_app("<id>")``. When the model decides an app fits the
 * user's intent and wants to know exactly how to run it, it calls ``expand_app(app_id)``
 * and gets that app's full command surface (when-to-invoke, how-to-use, hint words,
 * example triggers, scope, CLI invocations) — pulled into context ONLY on demand, so
 * the heavy per-app detail never sits in every turn's token budget.
 *
 *   bot agent → expand_app({app_id})
 *     └─ POST /api/applications/expand {app_id}   over the admin-daemon UNIX SOCKET
 *          └─ server binds the calling bot from the socket PEER UID (never a request
 *             field), resolves the app in THAT bot's own workspace, and returns the
 *             rendered Tier-2 markdown. Read-only; a bot can only expand its own apps.
 *
 * This is a *disclosure* tool, not a trigger: it tells the model how to use an app it
 * already chose in natural language. It never forces an app and never runs one — it
 * only reveals usage. (The principle-just-works "smarter system, not rigid user"
 * contract: recognition via better context, still the model's choice.)
 *
 * Why the unix socket + peer-uid binding (not TCP, not a botId arg) — identical to
 * DirectoryTools / GoogleTools: the kernel-reported peer uid is the cross-bot-safe
 * identity primitive, so the lookup is scoped to the caller's own apps by construction.
 * Every failure mode (HTTP error, daemon unavailable, bad input) returns a NON-throwing
 * tool envelope — an index fault must never crash the gateway turn.
 *
 * Forward-compat (spec §2.1): the server payload is a clean, tool-agnostic
 * ``{app_id, detail}`` so this can be promoted to a native OpenClaw deferred-tool
 * primitive if/when one lands, without reshaping the tool contract.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { AdminSocketRequest, AdminSocketResponse } from "../util/adminSocket.js";
/**
 * Transport seam for tests. Real callers omit it and get the live unix-socket
 * call; tests pass a stub that returns canned responses without a daemon. Mirrors
 * DirectoryTools' ``DirectoryTransport``.
 */
export type ExpandAppTransport = (req: AdminSocketRequest) => Promise<AdminSocketResponse>;
interface ExpandAppToolConfig {
    /** This bot's shared dir — the admin-daemon socket lives at {sharedDir}/admin-daemon.sock. */
    readonly sharedDir: string;
    /** This bot's id — for diagnostics only; identity is bound server-side by peer uid. */
    readonly botId: string;
    /** Per-call socket override (tests). Real callers omit it. */
    readonly socketPath?: string;
    /** Transport override for tests. Real callers omit it (live socket). */
    readonly transport?: ExpandAppTransport;
}
export declare const ExpandAppParamsSchema: import("@sinclair/typebox").TObject<{
    app_id: import("@sinclair/typebox").TString;
}>;
export type ExpandAppParams = Static<typeof ExpandAppParamsSchema>;
/**
 * Build the ``expand_app`` tool factory.
 *
 * Each call POSTs ``{app_id}`` to ``/api/applications/expand`` over the unix socket; the
 * server binds identity from the peer uid and returns ``{ok, app_id, detail}`` (Tier-2
 * markdown) or a 404 with the ids that WOULD resolve. A socket-unavailable condition
 * surfaces as a clean tool error, not a crash — and never throws into the agent loop.
 */
export declare function createExpandAppToolFactory(config: ExpandAppToolConfig, logger: PluginLogger): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        app_id: import("@sinclair/typebox").TString;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export {};
//# sourceMappingURL=ExpandAppTool.d.ts.map