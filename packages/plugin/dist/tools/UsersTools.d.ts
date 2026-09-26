/**
 * UsersTools — the app-facing identity read verbs: ``users_whoami`` / ``users_list``.
 *
 * `internal/dispatch/done/users-roster-read-only-surface.md` item 2 (design-app-access
 * §"Identity per turn"; design-application-platform-2026-09-22 §2 row 1). An app running
 * in a bot's turn (the personal assistant is the first caller) needs "who is asking" to
 * put the owner on a card, without importing the roster's internals — the exact monolith
 * tell design-application-platform's forbidden list names.
 *
 *   bot agent → users_whoami({})       (read)
 *     └─ POST /api/users-bot/whoami {app_id?}   over the admin-daemon UNIX SOCKET
 *   bot agent → users_list({})         (read)
 *     └─ POST /api/users-bot/list   {app_id?}   over the admin-daemon UNIX SOCKET
 *          └─ server binds the calling BOT from the socket PEER UID (never a request
 *             field, identical to DirectoryTools/RosterTools) and the calling PERSON
 *             from ``X-Requester-Identity`` (RosterTools' own sender-resolution +
 *             header-building — duplicated here rather than imported, matching this
 *             package's per-tool-file convention).
 *
 * ``appId`` in ``UsersToolConfig`` is a STATIC value baked in when the surface that
 * instantiates this app's tools knows which app it is instantiating for (the same shape
 * ``DirectoryToolConfig.botId`` already uses) — this file does not invent app-identity
 * resolution mid-turn. Omitted (``undefined``) for a plain bot-conversation call not
 * mediated by any particular app, which is always allowed (no audience to enforce).
 *
 * Every failure mode returns a NON-throwing tool envelope, same discipline as
 * DirectoryTools/RosterTools — a users-lookup fault must never crash the gateway turn.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { AdminSocketRequest, AdminSocketResponse } from "../util/adminSocket.js";
/** Transport seam for tests — mirrors DirectoryTransport/RosterTransport. */
export type UsersTransport = (req: AdminSocketRequest) => Promise<AdminSocketResponse>;
interface UsersToolConfig {
    /** This bot's shared dir — the admin-daemon socket lives at {sharedDir}/admin-daemon.sock. */
    readonly sharedDir: string;
    /** This bot's id — for diagnostics only; identity is bound server-side by peer uid. */
    readonly botId: string;
    /** The app these tools are instantiated for, when app-mediated. Static, not resolved
     *  mid-turn — see the module docstring. Omitted for a plain bot conversation. */
    readonly appId?: string;
    /** Per-call socket override (tests). */
    readonly socketPath?: string;
    /** Transport override (tests). */
    readonly transport?: UsersTransport;
}
interface ToolCtx {
    readonly sessionKey?: string | null;
    readonly runId?: string | null;
}
export declare const UsersWhoamiParamsSchema: import("@sinclair/typebox").TObject<{}>;
export type UsersWhoamiParams = Static<typeof UsersWhoamiParamsSchema>;
/** Build the ``users_whoami`` tool factory — the calling user's OWN record. */
export declare function createUsersWhoamiToolFactory(config: UsersToolConfig, logger: PluginLogger): (ctx: ToolCtx) => {
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
export declare const UsersListParamsSchema: import("@sinclair/typebox").TObject<{}>;
export type UsersListParams = Static<typeof UsersListParamsSchema>;
/** Build the ``users_list`` tool factory — the roster this app may see. */
export declare function createUsersListToolFactory(config: UsersToolConfig, logger: PluginLogger): (ctx: ToolCtx) => {
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
export {};
//# sourceMappingURL=UsersTools.d.ts.map