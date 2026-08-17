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

import { Type, Static } from "@sinclair/typebox";

import type { PluginLogger } from "openclaw/plugin-sdk/types";

import { adminSocketRequest } from "../util/adminSocket.js";

function textResult(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    ...(isError ? { isError: true } : {}),
  };
}

async function call(
  logger: PluginLogger,
  socketPath: string | undefined,
  toolName: string,
  urlPath: string,
) {
  try {
    const res = await adminSocketRequest({
      method: "GET",
      path: urlPath,
      socketPath,
    });
    if (res.status === 200) {
      return textResult(JSON.stringify(res.body));
    }
    const err =
      (res.body && typeof res.body === "object" &&
        (res.body as Record<string, unknown>).error) ||
      `HTTP ${res.status}`;
    return textResult(`${toolName} failed: ${err}`, true);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`${toolName}: admin request failed: ${msg}`);
    return textResult(`${toolName}: ${msg}`, true);
  }
}

// ── pod_state (consolidated) ────────────────────────────────────────────────

export const POD_STATE_QUERIES = [
  "status", "signals", "proposals", "watchdog",
  "spend", "turns", "bot", "audits",
] as const;

export type PodStateQuery = (typeof POD_STATE_QUERIES)[number];

export const PodStateParamsSchema = Type.Object({
  query: Type.Union(
    POD_STATE_QUERIES.map((q) => Type.Literal(q)),
    {
      description:
        "What to read. status=pod snapshot (bots, roles, firing counts); " +
        "signals=alert store (default state 'firing'); proposals=Better " +
        "Engine queue (default state 'pending' — what admin means by 'the " +
        "queue'); watchdog=operational events (gateway flaps, drift), " +
        "newest-first; spend=cost/token rollup; turns=recent USER turns for " +
        "one bot (privacy: user text only, opt-out possible); bot=one-bot " +
        "snapshot (role, tier, integrations, signals, 7d spend); " +
        "audits=latest OC security audit per bot.",
    },
  ),
  state: Type.Optional(
    Type.String({
      description:
        "Lifecycle filter. signals: firing|snoozed|resolved|dismissed|all. " +
        "proposals: pending|snoozed|applied|archived|active|all.",
    }),
  ),
  bot: Type.Optional(
    Type.String({
      description:
        "Bot id filter. REQUIRED for query=turns and query=bot; optional " +
        "for signals/watchdog/spend/audits (omit for pod-wide).",
    }),
  ),
  producer: Type.Optional(
    Type.String({
      description:
        "signals only: filter to one monitor (e.g. 'cost_alert', " +
        "'security_warden').",
    }),
  ),
  hours: Type.Optional(
    Type.Integer({
      description: "watchdog only: lookback hours (default 24, max 168).",
      minimum: 1,
      maximum: 168,
    }),
  ),
  window: Type.Optional(
    Type.Union([Type.Literal("1d"), Type.Literal("7d"), Type.Literal("30d")], {
      description: "spend only: rollup window (default 7d).",
    }),
  ),
  limit: Type.Optional(
    Type.Integer({
      description: "Cap on rows returned (defaults vary; max 100).",
      minimum: 1,
      maximum: 100,
    }),
  ),
});

export type PodStateParams = Static<typeof PodStateParamsSchema>;

const POD_STATE_DESCRIPTION = [
  "Read live Evolve pod state (primary bot only). One tool, eight views —",
  "pick with `query`. Start with query=status for vague 'how is the pod?'",
  "questions, then drill in.",
  "",
  "Rendering: short header, one fact per line, dollars not cents; never",
  "label anything CRITICAL unless its severity is actually 'critical'.",
  "Gotchas: spend missing files mean 'no data recorded', not zero;",
  "query=bot returns found=false for unknown ids (say 'I don't recognize",
  "that bot'); turns opted_out=true renders as 'no recent context",
  "available'; audits stale=true means point admin at the Security tab",
  "instead of quoting cached numbers. Proposals are summaries — full",
  "envelopes live in the admin UI.",
].join("\n");

/** endpoint + which params each query forwards. */
const QUERY_ROUTES: Record<PodStateQuery, {
  endpoint: string;
  params: ReadonlyArray<"state" | "bot" | "producer" | "hours" | "window" | "limit">;
  requiresBot?: boolean;
}> = {
  status:    { endpoint: "pod_status", params: [] },
  signals:   { endpoint: "signals",    params: ["state", "producer", "bot", "limit"] },
  proposals: { endpoint: "proposals",  params: ["state", "limit"] },
  watchdog:  { endpoint: "watchdog",   params: ["hours", "bot", "limit"] },
  spend:     { endpoint: "spend",      params: ["window", "bot"] },
  turns:     { endpoint: "turns",      params: ["bot", "limit"], requiresBot: true },
  bot:       { endpoint: "describe",   params: ["bot"], requiresBot: true },
  audits:    { endpoint: "audits",     params: ["bot", "limit"] },
};

export function createPodStateToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "pod_state",
    description: POD_STATE_DESCRIPTION,
    parameters: PodStateParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const p = (rawParams ?? {}) as PodStateParams;
      const route = QUERY_ROUTES[p.query as PodStateQuery];
      if (!route) {
        return textResult(
          `pod_state: unknown query '${String(p.query)}'. ` +
          `Valid: ${POD_STATE_QUERIES.join(", ")}.`,
          true,
        );
      }
      if (route.requiresBot && !(p.bot ?? "").trim()) {
        return textResult(
          `pod_state: query='${p.query}' requires 'bot'.`, true);
      }
      const qs = new URLSearchParams();
      for (const key of route.params) {
        const value = p[key];
        if (value === undefined || value === null) continue;
        const rendered = String(value).trim();
        if (rendered) qs.set(key, rendered);
      }
      const path =
        `/api/primary/state/${route.endpoint}${qs.toString() ? `?${qs}` : ""}`;
      return call(logger, socketPath, "pod_state", path);
    },
  });
}
