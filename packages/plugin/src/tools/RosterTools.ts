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

import { Type, Static } from "@sinclair/typebox";

import type { PluginLogger } from "openclaw/plugin-sdk/types";

import {
  AdminSocketRequest,
  AdminSocketResponse,
  AdminSocketUnavailable,
  adminSocketRequest,
} from "../util/adminSocket.js";
import { getSender, SenderRecord } from "../util/senderRegistry.js";

// ── Common shapes ──────────────────────────────────────────────────────


/**
 * Transport seam for tests. Real callers omit this and get the live
 * unix-socket call; tests pass a stub that returns canned responses
 * without hitting the daemon.
 */
export type RosterTransport =
  (req: AdminSocketRequest) => Promise<AdminSocketResponse>;


const CHANNEL_ENUM = Type.Union(
  [
    Type.Literal("telegram"),
    Type.Literal("slack"),
    Type.Literal("discord"),
    Type.Literal("whatsapp"),
  ],
  {
    description:
      "Messaging platform whose roster you're modifying. Your own identity " +
      "is resolved from the platform you're actually chatting on; all four " +
      "platforms are supported for Path B.",
  },
);


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


/**
 * Resolve who the speaker is for THIS tool invocation.
 *
 * Two paths, in order of preference:
 *
 * 1. **senderRegistry** (preferred) — the before_agent_run hook in
 *    TurnObserver captures ``event.senderId`` from the OC SDK and
 *    stashes it keyed by runId. Works for DMs AND groups uniformly.
 *
 * 2. **sessionKey trailing positive int** (fallback) — works for
 *    Telegram DMs where chat_id == user_id. Pre-Phase-C.3 path.
 *    Useful when the OC version doesn't populate event.senderId, or
 *    when the registry has rotated the entry. Group sessions hit the
 *    "negative trailing int" branch and return null — the caller
 *    surfaces a "couldn't identify you" envelope.
 *
 * Returns the sender's stable_id (no platform prefix), their real
 * ``platform`` (telegram | slack | discord | whatsapp, or null when it
 * couldn't be threaded), and a source tag the caller logs for
 * diagnostics. ``null`` id means refuse the tool call with a clear
 * envelope.
 */
function resolveSenderId(
  ctx: ToolCtx,
  logger: PluginLogger,
): {
  id: string | null;
  platform: string | null;
  source: "registry" | "sessionKey" | "none";
} {
  // Path 1 — senderRegistry (Phase C.3 — works for groups, all platforms).
  const captured: SenderRecord | null = getSender(ctx.runId);
  if (captured?.senderId) {
    return { id: captured.senderId, platform: captured.platform, source: "registry" };
  }
  // Path 2 — sessionKey trailing positive int (Phase C.2 — DM-only fallback).
  // This trick relies on chat_id == user_id, which holds ONLY for Telegram
  // DMs, so the sender's platform here is telegram by construction.
  const sessionKey = String(ctx.sessionKey ?? "");
  const match = sessionKey.match(/:(\d+)$/);
  if (match) {
    return { id: match[1]!, platform: "telegram", source: "sessionKey" };
  }
  logger.warn(
    `RosterTools: could not resolve sender — runId=${ctx.runId ?? "?"} ` +
    `sessionKey=${sessionKey || "?"}`,
  );
  return { id: null, platform: null, source: "none" };
}


function buildRequesterHeaders(
  senderId: string,
  platform: string,
  sourceBotId: string,
): Record<string, string> {
  // The daemon (routes_bot_users._check_capability) resolves the
  // requester's role by (platform, id). Send the sender's REAL platform
  // so Slack/Discord/WhatsApp senders resolve their own overlay key
  // (audit R1a G-N2, #3378). `platform` is REQUIRED and non-empty here —
  // callers refuse locally when it can't be resolved (see the resolve-or-
  // refuse guard at each call site). We deliberately do NOT default to
  // `telegram`: that fallback mis-attributed a foreign sender onto the
  // telegram id-space (which holds privileged ids), so an unresolved
  // platform could resolve to a privileged telegram identity. Refusing
  // instead keeps the daemon fail-closed.
  return {
    "X-Requester-Identity": `${platform}:${senderId}`,
    "X-Requester-Source-Bot": sourceBotId,
  };
}


function refusalContent(text: string) {
  return {
    content: [{ type: "text" as const, text }],
    isError: true,
  };
}


function successContent(text: string) {
  return {
    content: [{ type: "text" as const, text }],
  };
}


async function callDaemon(
  args: {
    method: "PATCH" | "POST" | "PUT";
    path: string;
    body?: unknown;
    headers: Record<string, string>;
    action: string;
    logger: PluginLogger;
    socketPath?: string;
    transport?: RosterTransport;
  },
) {
  const transport = args.transport ?? adminSocketRequest;
  try {
    const resp = await transport({
      method: args.method,
      path: args.path,
      body: args.body,
      headers: args.headers,
      socketPath: args.socketPath,
    });
    if (resp.status === 403) {
      const detail =
        (resp.body && typeof resp.body === "object" && "detail" in resp.body)
          ? String((resp.body as Record<string, unknown>).detail ?? "")
          : "";
      args.logger.info(`RosterTools: ${args.action} refused: ${detail}`);
      return refusalContent(
        `Refused: ${detail || "you don't have permission to perform this action on this bot."}`,
      );
    }
    if (resp.status >= 400) {
      const err =
        (resp.body && typeof resp.body === "object" && "error" in resp.body)
          ? String((resp.body as Record<string, unknown>).error ?? "")
          : `HTTP ${resp.status}`;
      args.logger.warn(`RosterTools: ${args.action} failed: ${err}`);
      return refusalContent(`Failed: ${err}`);
    }
    args.logger.info(`RosterTools: ${args.action} succeeded`);
    return successContent(`Done. ${args.action} applied.`);
  } catch (err) {
    if (err instanceof AdminSocketUnavailable) {
      args.logger.warn(
        `RosterTools: ${args.action} — admin daemon unavailable: ${err.message}`,
      );
      return refusalContent(
        "Admin daemon is unavailable right now. Try again in a minute or use the admin UI.",
      );
    }
    const msg = err instanceof Error ? err.message : String(err);
    args.logger.error(`RosterTools: ${args.action} unexpected error: ${msg}`);
    return refusalContent(`Unexpected error: ${msg}`);
  }
}


function noSenderRefusal() {
  return refusalContent(
    "Could not identify who is speaking (or which platform they are on). " +
    "The roster mutation requires a verified sender identity — a resolved " +
    "platform AND id — so the bot can check your permission. If this keeps " +
    "happening, the pod admin can make the change from the Users page or via evo.",
  );
}


// ── roster_set_role ────────────────────────────────────────────────────


export const RosterSetRoleParamsSchema = Type.Object({
  channel: CHANNEL_ENUM,
  target_id: Type.String({
    description:
      "Platform-stable id of the user whose role you're changing " +
      "(Telegram numeric user_id, Slack user_id, etc.). NOT the @handle.",
  }),
  role: Type.Union(
    [Type.Literal("primary_user"), Type.Literal("participant")],
    {
      description:
        "Target role. 'admin' comes from pod-admin claims in network.json " +
        "and is not assignable here. To block a user, use the roster_block tool.",
    },
  ),
});
export type RosterSetRoleParams = Static<typeof RosterSetRoleParamsSchema>;


export function createRosterSetRoleToolFactory(
  config: RosterToolConfig,
  logger: PluginLogger,
) {
  return (ctx: ToolCtx) => ({
    name: "roster_set_role",
    description:
      "Set a user's role on THIS bot. Use to promote a participant to " +
      "primary_user (admin-equivalent for this bot) or demote back to " +
      "participant. The bot honors the call only if you (the speaker) " +
      "have the bot.roster.mutate capability — admin or primary_user.",
    parameters: RosterSetRoleParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = rawParams as RosterSetRoleParams;
      const { id: senderId, platform } = resolveSenderId(ctx, logger);
      // Resolve-or-refuse: BOTH id and platform must resolve. An unresolved
      // platform must NOT fall back to a privileged telegram identity (G-N2).
      if (!senderId || !platform) return noSenderRefusal();
      return await callDaemon({
        method: "PATCH",
        path: `/api/admin/bots/${encodeURIComponent(config.botId)}/users/${encodeURIComponent(params.channel)}/${encodeURIComponent(params.target_id)}`,
        body: { role: params.role },
        headers: buildRequesterHeaders(senderId, platform, config.botId),
        socketPath: config.socketPath,
        transport: config.transport,
        action: `set role=${params.role} for ${params.channel}:${params.target_id}`,
        logger,
      });
    },
  });
}


// ── roster_block ────────────────────────────────────────────────────────


export const RosterBlockParamsSchema = Type.Object({
  channel: CHANNEL_ENUM,
  target_id: Type.String({
    description: "Platform-stable id of the user to block (NOT the @handle).",
  }),
  reason: Type.Optional(
    Type.String({
      maxLength: 200,
      description:
        "Optional one-line reason recorded in the audit log " +
        "(e.g. 'spamming the channel', 'compromised account').",
    }),
  ),
});
export type RosterBlockParams = Static<typeof RosterBlockParamsSchema>;


export function createRosterBlockToolFactory(
  config: RosterToolConfig,
  logger: PluginLogger,
) {
  return (ctx: ToolCtx) => ({
    name: "roster_block",
    description:
      "Sticky block — survives re-pairing. Use when a user has lost trust " +
      "and you want to prevent them from re-engaging via /start. The bot " +
      "(a) revokes them from the OC allowlist, (b) writes a sticky deny " +
      "entry, (c) rejects any pending pairing. Requires bot.roster.mutate " +
      "(admin or primary_user).",
    parameters: RosterBlockParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = rawParams as RosterBlockParams;
      const { id: senderId, platform } = resolveSenderId(ctx, logger);
      // Resolve-or-refuse: BOTH id and platform must resolve. An unresolved
      // platform must NOT fall back to a privileged telegram identity (G-N2).
      if (!senderId || !platform) return noSenderRefusal();
      return await callDaemon({
        method: "POST",
        path: `/api/admin/bots/${encodeURIComponent(config.botId)}/users/${encodeURIComponent(params.channel)}/${encodeURIComponent(params.target_id)}/block`,
        body: { reason: params.reason ?? "" },
        headers: buildRequesterHeaders(senderId, platform, config.botId),
        socketPath: config.socketPath,
        transport: config.transport,
        action: `block ${params.channel}:${params.target_id}`,
        logger,
      });
    },
  });
}


// ── roster_unblock ──────────────────────────────────────────────────────


export const RosterUnblockParamsSchema = Type.Object({
  channel: CHANNEL_ENUM,
  target_id: Type.String({
    description: "Platform-stable id to unblock.",
  }),
});
export type RosterUnblockParams = Static<typeof RosterUnblockParamsSchema>;


export function createRosterUnblockToolFactory(
  config: RosterToolConfig,
  logger: PluginLogger,
) {
  return (ctx: ToolCtx) => ({
    name: "roster_unblock",
    description:
      "Remove a user from this bot's block index. Does NOT re-admit them — " +
      "they would have to send /start again and be approved via the normal " +
      "pairing flow. Requires bot.roster.mutate.",
    parameters: RosterUnblockParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = rawParams as RosterUnblockParams;
      const { id: senderId, platform } = resolveSenderId(ctx, logger);
      // Resolve-or-refuse: BOTH id and platform must resolve. An unresolved
      // platform must NOT fall back to a privileged telegram identity (G-N2).
      if (!senderId || !platform) return noSenderRefusal();
      return await callDaemon({
        method: "POST",
        path: `/api/admin/bots/${encodeURIComponent(config.botId)}/users/${encodeURIComponent(params.channel)}/${encodeURIComponent(params.target_id)}/unblock`,
        headers: buildRequesterHeaders(senderId, platform, config.botId),
        socketPath: config.socketPath,
        transport: config.transport,
        action: `unblock ${params.channel}:${params.target_id}`,
        logger,
      });
    },
  });
}


// ── channel_set_newcomer_mode ──────────────────────────────────────────


export const ChannelSetNewcomerModeParamsSchema = Type.Object({
  channel: CHANNEL_ENUM,
  mode: Type.Union(
    [
      Type.Literal("auto_admit"),
      Type.Literal("require_approval"),
      Type.Literal("closed"),
    ],
    {
      description:
        "How this bot treats an unknown sender's /start: " +
        "'auto_admit' (group membership = automatic participant role; for " +
        "trusted private groups), 'require_approval' (existing pairing flow; " +
        "admin approves each /start), 'closed' (operator-only — no pairing " +
        "code issued).",
    },
  ),
  default_engagement_surfaces: Type.Optional(
    Type.Array(
      Type.Union([Type.Literal("group"), Type.Literal("dm")]),
      {
        description:
          "Optional. Surfaces a newly auto-admitted participant is " +
          "honored on; defaults to ['group'] for telegram_group, ['group','dm'] " +
          "otherwise.",
      },
    ),
  ),
});
export type ChannelSetNewcomerModeParams = Static<typeof ChannelSetNewcomerModeParamsSchema>;


export function createChannelSetNewcomerModeToolFactory(
  config: RosterToolConfig,
  logger: PluginLogger,
) {
  return (ctx: ToolCtx) => ({
    name: "channel_set_newcomer_mode",
    description:
      "Set this bot's per-channel newcomer mode. Requires bot.channel.config " +
      "(admin or primary_user).",
    parameters: ChannelSetNewcomerModeParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = rawParams as ChannelSetNewcomerModeParams;
      const { id: senderId, platform } = resolveSenderId(ctx, logger);
      // Resolve-or-refuse: BOTH id and platform must resolve. An unresolved
      // platform must NOT fall back to a privileged telegram identity (G-N2).
      if (!senderId || !platform) return noSenderRefusal();
      const body: Record<string, unknown> = { mode: params.mode };
      if (params.default_engagement_surfaces) {
        body.default_engagement_surfaces = params.default_engagement_surfaces;
      }
      return await callDaemon({
        method: "PUT",
        path: `/api/admin/bots/${encodeURIComponent(config.botId)}/channels/${encodeURIComponent(params.channel)}/newcomer_mode`,
        body,
        headers: buildRequesterHeaders(senderId, platform, config.botId),
        socketPath: config.socketPath,
        transport: config.transport,
        action: `set ${params.channel} newcomer_mode=${params.mode}`,
        logger,
      });
    },
  });
}
