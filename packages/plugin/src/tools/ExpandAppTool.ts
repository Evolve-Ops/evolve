/**
 * ExpandAppTool — the bot-facing ``expand_app`` tool: Tier-2 of the app
 * capability index (the recognition layer of the "just works" arc).
 *
 * Spec: internal/spec-app-invocation-just-works-2026-06-29.md §2.1. The bot's AGENTS.md
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

import { join } from "node:path";

import { Type, Static } from "@sinclair/typebox";

import type { PluginLogger } from "openclaw/plugin-sdk/types";

import {
  AdminSocketRequest,
  AdminSocketResponse,
  AdminSocketUnavailable,
  adminSocketRequest,
} from "../util/adminSocket.js";
import { recordExplicit } from "../apps/AppAttribution.js";


const SOCKET_NAME = "admin-daemon.sock";


/**
 * Transport seam for tests. Real callers omit it and get the live unix-socket
 * call; tests pass a stub that returns canned responses without a daemon. Mirrors
 * DirectoryTools' ``DirectoryTransport``.
 */
export type ExpandAppTransport =
  (req: AdminSocketRequest) => Promise<AdminSocketResponse>;


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


export const ExpandAppParamsSchema = Type.Object({
  app_id: Type.String({
    description:
      "Which installed app to expand — the id shown in the Installed Apps menu " +
      "(its name or display name also work). Returns that app's full usage: when " +
      "to invoke it, how to run it, its commands and arguments, and examples. Use " +
      "it before running an app you haven't used recently, so you call it correctly.",
  }),
});
export type ExpandAppParams = Static<typeof ExpandAppParamsSchema>;


/**
 * Per-invocation context OC hands the tool factory. Mirrors RosterTools'
 * ``ToolCtx`` — ``runId`` keys the run-scoped attribution stash (AL-1.1),
 * ``sessionId`` the session-stickiness map. Both optional: older gateways
 * may omit them, and attribution then simply records nothing.
 */
interface ExpandAppToolCtx {
  readonly runId?: string | null;
  readonly sessionId?: string | null;
}


function textResult(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    ...(isError ? { isError: true } : {}),
  };
}


/**
 * Build the ``expand_app`` tool factory.
 *
 * Each call POSTs ``{app_id}`` to ``/api/applications/expand`` over the unix socket; the
 * server binds identity from the peer uid and returns ``{ok, app_id, detail}`` (Tier-2
 * markdown) or a 404 with the ids that WOULD resolve. A socket-unavailable condition
 * surfaces as a clean tool error, not a crash — and never throws into the agent loop.
 */
export function createExpandAppToolFactory(
  config: ExpandAppToolConfig,
  logger: PluginLogger,
) {
  const socketPath = config.socketPath ?? join(config.sharedDir, SOCKET_NAME);
  const transport: ExpandAppTransport = config.transport ?? adminSocketRequest;

  return (ctx: ExpandAppToolCtx) => ({
    name: "expand_app",
    description:
      "Show how to use one of your installed apps — its commands, arguments, and " +
      "examples. Your Installed Apps menu lists each app by name and purpose with an " +
      "id; call this with that id when you've decided an app fits what the user wants " +
      "and need to know exactly how to run it. It only reveals usage — it does not run " +
      "the app, and it never forces you to use one; choosing the right app for the " +
      "request is still your call.",
    parameters: ExpandAppParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = (rawParams ?? {}) as ExpandAppParams;
      const appId = typeof params.app_id === "string" ? params.app_id.trim() : "";
      if (!appId) {
        return textResult(
          "Provide app_id — the id of the installed app to expand (see your " +
          "Installed Apps menu).", true);
      }
      try {
        const res = await transport({
          method: "POST",
          path: "/api/applications/expand",
          body: { app_id: appId },
          socketPath,
        });
        const obj =
          res.body && typeof res.body === "object"
            ? (res.body as Record<string, unknown>)
            : {};
        if (res.status === 200 && obj.ok === true) {
          // Explicit app-attribution signal (AL-1.1, design §4.2 source 1):
          // the daemon CONFIRMED the app resolves in this bot's own workspace
          // and returned its canonical registry id — record that id (the raw
          // param may be a name/display-name), never an unconfirmed guess.
          const resolvedId =
            typeof obj.app_id === "string" && obj.app_id.trim()
              ? obj.app_id.trim()
              : appId;
          recordExplicit(ctx?.runId, ctx?.sessionId, resolvedId, "expand_app");
          const detail = typeof obj.detail === "string" ? obj.detail : "";
          if (detail.trim()) {
            return textResult(detail);
          }
          // Success with no body to show — degrade to a plain note, not an error.
          return textResult(
            `No usage details are recorded for ${JSON.stringify(appId)} yet.`);
        }
        if (res.status === 404) {
          const available = Array.isArray(obj.available)
            ? (obj.available as unknown[]).filter((x) => typeof x === "string")
            : [];
          const hint = available.length
            ? ` Installed apps you can expand: ${available.join(", ")}.`
            : "";
          return textResult(
            `No installed app matches ${JSON.stringify(appId)}.${hint}`, true);
        }
        const detail =
          "detail" in obj || "error" in obj
            ? String(obj.detail ?? obj.error ?? "")
            : `HTTP ${res.status}`;
        logger.warn(`expand_app failed (${res.status}): ${detail}`);
        return textResult(`expand_app failed: ${detail}`, true);
      } catch (err) {
        if (err instanceof AdminSocketUnavailable) {
          logger.warn(`expand_app — admin daemon unavailable: ${err.message}`);
          return textResult(
            "App details are unavailable right now (admin daemon unreachable). " +
            "Try again in a moment.", true);
        }
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`expand_app unexpected error: ${msg}`);
        return textResult(`expand_app error: ${msg}`, true);
      }
    },
  });
}
