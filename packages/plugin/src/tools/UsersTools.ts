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

import { join } from "node:path";

import { Type, Static } from "@sinclair/typebox";

import type { PluginLogger } from "openclaw/plugin-sdk/types";

import {
  AdminSocketRequest,
  AdminSocketResponse,
  AdminSocketUnavailable,
  adminSocketRequest,
} from "../util/adminSocket.js";
import { getSender, SenderRecord } from "../util/senderRegistry.js";

const SOCKET_NAME = "admin-daemon.sock";

/** Transport seam for tests — mirrors DirectoryTransport/RosterTransport. */
export type UsersTransport =
  (req: AdminSocketRequest) => Promise<AdminSocketResponse>;

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

function textResult(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    ...(isError ? { isError: true } : {}),
  };
}

/** Same two-path resolution as RosterTools.resolveSenderId — duplicated per this
 *  package's per-tool-file convention rather than imported from a mutation-focused
 *  module. See RosterTools.ts for the full rationale of each path. */
function resolveSenderId(
  ctx: ToolCtx,
  logger: PluginLogger,
): { id: string | null; platform: string | null } {
  const captured: SenderRecord | null = getSender(ctx.runId ?? null);
  if (captured?.senderId) {
    return { id: captured.senderId, platform: captured.platform };
  }
  const sessionKey = String(ctx.sessionKey ?? "");
  const match = sessionKey.match(/:(\d+)$/);
  if (match) {
    return { id: match[1]!, platform: "telegram" };
  }
  logger.warn(
    `UsersTools: could not resolve sender — runId=${ctx.runId ?? "?"} ` +
    `sessionKey=${sessionKey || "?"}`,
  );
  return { id: null, platform: null };
}

function noSenderRefusal() {
  return textResult(
    "Could not identify who is speaking (or which platform they are on) — " +
    "this needs a verified sender identity to answer 'who is asking'.", true);
}

async function callUsersBot(
  path: string,
  config: UsersToolConfig,
  senderId: string,
  platform: string,
  logger: PluginLogger,
  toolName: string,
) {
  const socketPath = config.socketPath ?? join(config.sharedDir, SOCKET_NAME);
  const transport: UsersTransport = config.transport ?? adminSocketRequest;
  try {
    const res = await transport({
      method: "POST",
      path,
      body: config.appId !== undefined ? { app_id: config.appId } : {},
      headers: { "X-Requester-Identity": `${platform}:${senderId}` },
      socketPath,
    });
    if (res.status !== 200) {
      const err =
        (res.body && typeof res.body === "object" &&
          (res.body as Record<string, unknown>).error) ||
        `HTTP ${res.status}`;
      logger.warn(`${toolName} failed (${res.status}): ${err}`);
      return textResult(`${toolName} failed: ${err}`, true);
    }
    const body = (res.body ?? {}) as Record<string, unknown>;
    if (body.refused === true) {
      return textResult(`Refused: ${String(body.reason ?? "not authorized")}`, true);
    }
    return textResult(JSON.stringify(body));
  } catch (err) {
    if (err instanceof AdminSocketUnavailable) {
      logger.warn(`${toolName} — admin daemon unavailable: ${err.message}`);
      return textResult(
        "The roster is unavailable right now (admin daemon unreachable). " +
        "Try again in a moment.", true);
    }
    const msg = err instanceof Error ? err.message : String(err);
    logger.error(`${toolName} unexpected error: ${msg}`);
    return textResult(`${toolName} error: ${msg}`, true);
  }
}

export const UsersWhoamiParamsSchema = Type.Object({});
export type UsersWhoamiParams = Static<typeof UsersWhoamiParamsSchema>;

/** Build the ``users_whoami`` tool factory — the calling user's OWN record. */
export function createUsersWhoamiToolFactory(
  config: UsersToolConfig,
  logger: PluginLogger,
) {
  return (ctx: ToolCtx) => ({
    name: "users_whoami",
    description:
      "Who is the person you're currently talking to? Returns THEIR OWN roster " +
      "record for this turn — display name, channels, role, and which apps they " +
      "are audience for. Use it to put the right owner's name on a card or a " +
      "reply, instead of guessing or hand-typing an id. Never returns anyone " +
      "else's record.",
    parameters: UsersWhoamiParamsSchema,
    async execute(_toolCallId: string, _rawParams: unknown) {
      const { id: senderId, platform } = resolveSenderId(ctx, logger);
      if (!senderId || !platform) return noSenderRefusal();
      return await callUsersBot(
        "/api/users-bot/whoami", config, senderId, platform, logger, "users_whoami");
    },
  });
}

export const UsersListParamsSchema = Type.Object({});
export type UsersListParams = Static<typeof UsersListParamsSchema>;

/** Build the ``users_list`` tool factory — the roster this app may see. */
export function createUsersListToolFactory(
  config: UsersToolConfig,
  logger: PluginLogger,
) {
  return (ctx: ToolCtx) => ({
    name: "users_list",
    description:
      "List the people this bot knows. If this app is open to everyone on the " +
      "bot, returns the whole roster; otherwise returns only the caller's own " +
      "record (an app scoped to owners or named users cannot see who else is " +
      "on the bot through this tool).",
    parameters: UsersListParamsSchema,
    async execute(_toolCallId: string, _rawParams: unknown) {
      const { id: senderId, platform } = resolveSenderId(ctx, logger);
      if (!senderId || !platform) return noSenderRefusal();
      return await callUsersBot(
        "/api/users-bot/list", config, senderId, platform, logger, "users_list");
    },
  });
}
