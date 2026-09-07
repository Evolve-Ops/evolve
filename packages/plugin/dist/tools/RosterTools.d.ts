/**
 * Roster admin tools — Phase C.2 (Path B).
 *
 * Spec: internal/spec-user-roster-and-roles-2026-06-07.md §10 (Admin paths
 * — Path B: bot-LLM-direct).
 *
 * Lets a ``primary_user`` (or pod admin) chatting with their bot via a
 * DM say things like "block @alice" or "set this channel to auto-admit"
 * and have the bot mutate its own roster.
 *
 * Four tools registered, each calling the admin-daemon over its unix
 * socket with an ``X-Requester-Identity`` header that names the sender's
 * stable_id. The daemon's per-endpoint capability check (Phase C.1)
 * resolves the requester's role on this bot and refuses with 403 if the
 * capability isn't granted. Defense in depth: an LLM convinced to call
 * these tools by a participant gets back a clean refusal envelope
 * instead of an actual mutation.
 *
 * **DM-only in v1.** The sender's Telegram user_id only appears in
 * ``ctx.sessionKey`` for ``telegram:direct`` sessions (where chat_id ==
 * user_id). For group sessions, sessionKey carries the group's chat_id,
 * not the sender's id — a separate plumbing path (capture user_id at
 * message-receive time and stash for the tool to read) is needed to
 * extend Path B to groups. Path C (evo cross-bot) covers the group
 * case in the meantime.
 *
 * The tools register unconditionally on every bot — the per-call
 * capability check at the daemon handles auth, mirroring the Phase C.1
 * design.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { AdminSocketRequest, AdminSocketResponse } from "../util/adminSocket.js";
/**
 * Transport seam for tests. Real callers omit this and get the live
 * unix-socket call; tests pass a stub that returns canned responses
 * without hitting the daemon.
 */
export type RosterTransport = (req: AdminSocketRequest) => Promise<AdminSocketResponse>;
interface RosterToolConfig {
    /** This bot's id, e.g. "atlas". Sent as X-Requester-Source-Bot for audit. */
    readonly botId: string;
    /**
     * Absolute path to the admin-daemon unix socket
     * (``{sharedDir}/admin-daemon.sock``). Platform-keyed — derived from
     * the plugin's resolved ``sharedDir`` so the path matches what the
     * daemon binds on this OS. When omitted, the live transport falls back
     * to the macOS-shaped ``DEFAULT_ADMIN_DAEMON_SOCKET`` (correct on macOS
     * only).
     */
    readonly socketPath?: string;
    /**
     * Optional transport override for tests. Real callers leave this
     * undefined and get the live unix-socket call. When the daemon-side
     * surface ever changes (e.g. switches transports), the seam is
     * already here.
     */
    readonly transport?: RosterTransport;
}
interface ToolCtx {
    readonly sessionKey?: string | null;
    readonly sessionId?: string | null;
    readonly messageChannel?: string | null;
    readonly agentId?: string | null;
    /** Per-turn run identifier. Phase C.3: tools use this to look up the
     *  captured sender record stashed by before_agent_run. */
    readonly runId?: string | null;
}
export declare const RosterSetRoleParamsSchema: import("@sinclair/typebox").TObject<{
    channel: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"telegram">, import("@sinclair/typebox").TLiteral<"slack">, import("@sinclair/typebox").TLiteral<"discord">, import("@sinclair/typebox").TLiteral<"whatsapp">]>;
    target_id: import("@sinclair/typebox").TString;
    role: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"primary_user">, import("@sinclair/typebox").TLiteral<"participant">]>;
}>;
export type RosterSetRoleParams = Static<typeof RosterSetRoleParamsSchema>;
export declare function createRosterSetRoleToolFactory(config: RosterToolConfig, logger: PluginLogger): (ctx: ToolCtx) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        channel: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"telegram">, import("@sinclair/typebox").TLiteral<"slack">, import("@sinclair/typebox").TLiteral<"discord">, import("@sinclair/typebox").TLiteral<"whatsapp">]>;
        target_id: import("@sinclair/typebox").TString;
        role: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"primary_user">, import("@sinclair/typebox").TLiteral<"participant">]>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const RosterBlockParamsSchema: import("@sinclair/typebox").TObject<{
    channel: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"telegram">, import("@sinclair/typebox").TLiteral<"slack">, import("@sinclair/typebox").TLiteral<"discord">, import("@sinclair/typebox").TLiteral<"whatsapp">]>;
    target_id: import("@sinclair/typebox").TString;
    reason: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
}>;
export type RosterBlockParams = Static<typeof RosterBlockParamsSchema>;
export declare function createRosterBlockToolFactory(config: RosterToolConfig, logger: PluginLogger): (ctx: ToolCtx) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        channel: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"telegram">, import("@sinclair/typebox").TLiteral<"slack">, import("@sinclair/typebox").TLiteral<"discord">, import("@sinclair/typebox").TLiteral<"whatsapp">]>;
        target_id: import("@sinclair/typebox").TString;
        reason: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const RosterUnblockParamsSchema: import("@sinclair/typebox").TObject<{
    channel: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"telegram">, import("@sinclair/typebox").TLiteral<"slack">, import("@sinclair/typebox").TLiteral<"discord">, import("@sinclair/typebox").TLiteral<"whatsapp">]>;
    target_id: import("@sinclair/typebox").TString;
}>;
export type RosterUnblockParams = Static<typeof RosterUnblockParamsSchema>;
export declare function createRosterUnblockToolFactory(config: RosterToolConfig, logger: PluginLogger): (ctx: ToolCtx) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        channel: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"telegram">, import("@sinclair/typebox").TLiteral<"slack">, import("@sinclair/typebox").TLiteral<"discord">, import("@sinclair/typebox").TLiteral<"whatsapp">]>;
        target_id: import("@sinclair/typebox").TString;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const ChannelSetNewcomerModeParamsSchema: import("@sinclair/typebox").TObject<{
    channel: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"telegram">, import("@sinclair/typebox").TLiteral<"slack">, import("@sinclair/typebox").TLiteral<"discord">, import("@sinclair/typebox").TLiteral<"whatsapp">]>;
    mode: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"auto_admit">, import("@sinclair/typebox").TLiteral<"require_approval">, import("@sinclair/typebox").TLiteral<"closed">]>;
    default_engagement_surfaces: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"group">, import("@sinclair/typebox").TLiteral<"dm">]>>>;
}>;
export type ChannelSetNewcomerModeParams = Static<typeof ChannelSetNewcomerModeParamsSchema>;
export declare function createChannelSetNewcomerModeToolFactory(config: RosterToolConfig, logger: PluginLogger): (ctx: ToolCtx) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        channel: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"telegram">, import("@sinclair/typebox").TLiteral<"slack">, import("@sinclair/typebox").TLiteral<"discord">, import("@sinclair/typebox").TLiteral<"whatsapp">]>;
        mode: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"auto_admit">, import("@sinclair/typebox").TLiteral<"require_approval">, import("@sinclair/typebox").TLiteral<"closed">]>;
        default_engagement_surfaces: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"group">, import("@sinclair/typebox").TLiteral<"dm">]>>>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export {};
//# sourceMappingURL=RosterTools.d.ts.map