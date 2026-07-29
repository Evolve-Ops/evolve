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

// ── pod_status ──────────────────────────────────────────────────────────────

export const PodStatusParamsSchema = Type.Object({});

const POD_STATUS_DESCRIPTION = [
  "High-level snapshot of this Evolve pod.",
  "",
  "Returns the bots in the pod (id, role, tier), the primary bot's id,",
  "and a count of firing signals — both per-bot and pod-wide.",
  "",
  "Use this for 'what bots do I have?', 'what's the overall pod state?',",
  "or as a first probe when admin asks a vague status question.",
].join("\n");

export function createPodStatusToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "pod_status",
    description: POD_STATUS_DESCRIPTION,
    parameters: PodStatusParamsSchema,
    async execute(_toolCallId: string, _rawParams: unknown) {
      return call(logger, socketPath, "pod_status", "/api/primary/state/pod_status");
    },
  });
}

// ── list_signals ────────────────────────────────────────────────────────────

export const ListSignalsParamsSchema = Type.Object({
  state: Type.Optional(
    Type.Union(
      [
        Type.Literal("firing"),
        Type.Literal("snoozed"),
        Type.Literal("resolved"),
        Type.Literal("dismissed"),
        Type.Literal("all"),
      ],
      {
        description:
          "Signal lifecycle state. Default 'firing' — most 'show me " +
          "alerts' questions want live, not historical.",
      },
    ),
  ),
  producer: Type.Optional(
    Type.String({
      description:
        "Filter to one monitor/generator (e.g. 'cost_alert', " +
        "'security_warden'). Use when admin asks about a specific kind " +
        "of alert; omit when admin just asks 'what's firing?'.",
    }),
  ),
  bot: Type.Optional(
    Type.String({
      description: "Filter to signals about one bot. Member-bot id, not channel.",
    }),
  ),
  limit: Type.Optional(
    Type.Integer({
      description: "Cap on N (default 20, max 100).",
      minimum: 1,
      maximum: 100,
    }),
  ),
});

export type ListSignalsParams = Static<typeof ListSignalsParamsSchema>;

const LIST_SIGNALS_DESCRIPTION = [
  "List Signals from the pod's alert store.",
  "",
  "Signals are observations emitted by monitors (cost, security, pod",
  "health, audits, …). Default state is 'firing' — the bot's most",
  "common 'show me alerts' question wants live ones.",
  "",
  "Returns { signals: [...], count, filters: {...} }. Render Team_bot_a-style:",
  "short header, one fact per line. Don't label anything CRITICAL",
  "unless the signal's severity is actually 'critical'.",
].join("\n");

export function createListSignalsToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "list_signals",
    description: LIST_SIGNALS_DESCRIPTION,
    parameters: ListSignalsParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const p = (rawParams ?? {}) as ListSignalsParams;
      const qs = new URLSearchParams();
      if (p.state) qs.set("state", p.state);
      if (p.producer) qs.set("producer", p.producer);
      if (p.bot) qs.set("bot", p.bot);
      if (typeof p.limit === "number") qs.set("limit", String(p.limit));
      const path = `/api/primary/state/signals${qs.toString() ? `?${qs}` : ""}`;
      return call(logger, socketPath, "list_signals", path);
    },
  });
}

// ── list_proposals ──────────────────────────────────────────────────────────

export const ListProposalsParamsSchema = Type.Object({
  state: Type.Optional(
    Type.Union(
      [
        Type.Literal("pending"),
        Type.Literal("snoozed"),
        Type.Literal("applied"),
        Type.Literal("archived"),
        Type.Literal("active"),
        Type.Literal("all"),
      ],
      {
        description:
          "Proposal lifecycle state. 'pending' is the default and the " +
          "one admin almost always means by 'the queue'.",
      },
    ),
  ),
  limit: Type.Optional(
    Type.Integer({
      description: "Cap on N (default 20, max 100).",
      minimum: 1,
      maximum: 100,
    }),
  ),
});

export type ListProposalsParams = Static<typeof ListProposalsParamsSchema>;

const LIST_PROPOSALS_DESCRIPTION = [
  "List proposals from the Better Engine queue.",
  "",
  "Proposals are 'changes Evolve wants to make' (raise a budget cap,",
  "deprecate an unused app, etc.). The summary is small — title, id,",
  "status, motivating signals. For the full envelope (diff /",
  "verification / history) tell admin to open the proposal in the",
  "admin UI; this tool intentionally doesn't dump the whole record.",
].join("\n");

export function createListProposalsToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "list_proposals",
    description: LIST_PROPOSALS_DESCRIPTION,
    parameters: ListProposalsParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const p = (rawParams ?? {}) as ListProposalsParams;
      const qs = new URLSearchParams();
      if (p.state) qs.set("state", p.state);
      if (typeof p.limit === "number") qs.set("limit", String(p.limit));
      const path = `/api/primary/state/proposals${qs.toString() ? `?${qs}` : ""}`;
      return call(logger, socketPath, "list_proposals", path);
    },
  });
}

// ── recent_watchdog ─────────────────────────────────────────────────────────

export const RecentWatchdogParamsSchema = Type.Object({
  hours: Type.Optional(
    Type.Integer({
      description:
        "Lookback window in hours. Default 24, max 168 (one week). " +
        "Larger windows are useful for 'what happened this week?' " +
        "kinds of questions.",
      minimum: 1,
      maximum: 168,
    }),
  ),
  bot: Type.Optional(
    Type.String({
      description: "Filter to watchdog events about one bot.",
    }),
  ),
  limit: Type.Optional(
    Type.Integer({
      description: "Cap on N (default 20, max 100).",
      minimum: 1,
      maximum: 100,
    }),
  ),
});

export type RecentWatchdogParams = Static<typeof RecentWatchdogParamsSchema>;

const RECENT_WATCHDOG_DESCRIPTION = [
  "Recent operational watchdog events (gateway flaps, config drift,",
  "verification reliability drops, …).",
  "",
  "Use this for 'why did the pod misbehave' or 'what happened around",
  "<time>' questions where the answer is platform-side rather than a",
  "Signal or Proposal. Returns events newest-first.",
].join("\n");

export function createRecentWatchdogToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "recent_watchdog",
    description: RECENT_WATCHDOG_DESCRIPTION,
    parameters: RecentWatchdogParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const p = (rawParams ?? {}) as RecentWatchdogParams;
      const qs = new URLSearchParams();
      if (typeof p.hours === "number") qs.set("hours", String(p.hours));
      if (p.bot) qs.set("bot", p.bot);
      if (typeof p.limit === "number") qs.set("limit", String(p.limit));
      const path = `/api/primary/state/watchdog${qs.toString() ? `?${qs}` : ""}`;
      return call(logger, socketPath, "recent_watchdog", path);
    },
  });
}

// ── spend_rollup ────────────────────────────────────────────────────────────

export const SpendRollupParamsSchema = Type.Object({
  window: Type.Optional(
    Type.Union(
      [Type.Literal("1d"), Type.Literal("7d"), Type.Literal("30d")],
      {
        description:
          "Time window for the rollup. Default '7d'. Use '1d' for " +
          "'today', '30d' for monthly trends.",
      },
    ),
  ),
  bot: Type.Optional(
    Type.String({
      description:
        "Filter to one bot. Omit for pod-wide totals across every " +
        "member bot.",
    }),
  ),
});

export type SpendRollupParams = Static<typeof SpendRollupParamsSchema>;

const SPEND_ROLLUP_DESCRIPTION = [
  "Spend / token rollup for a bot or the whole pod.",
  "",
  "Returns total_usd, input_tokens, output_tokens, top models by",
  "cost, and (pod-wide) a per-bot breakdown. Numbers come from",
  "daily rollup files written by the analyzer — missing files mean",
  "'no data recorded', not 'zero spend'.",
  "",
  "Use this when admin asks about cost / spend / token usage. For",
  "rendering, follow Team_bot_a style: short header, one fact per line,",
  "use dollar amounts not raw cents.",
].join("\n");

export function createSpendRollupToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "spend_rollup",
    description: SPEND_ROLLUP_DESCRIPTION,
    parameters: SpendRollupParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const p = (rawParams ?? {}) as SpendRollupParams;
      const qs = new URLSearchParams();
      if (p.window) qs.set("window", p.window);
      if (p.bot) qs.set("bot", p.bot);
      const path = `/api/primary/state/spend${qs.toString() ? `?${qs}` : ""}`;
      return call(logger, socketPath, "spend_rollup", path);
    },
  });
}

// ── recent_turns ────────────────────────────────────────────────────────────

export const RecentTurnsParamsSchema = Type.Object({
  bot: Type.String({
    description:
      "Which bot's recent user turns to retrieve. Required — there " +
      "is no pod-wide variant; transcripts are scoped per bot.",
  }),
  limit: Type.Optional(
    Type.Integer({
      description: "Cap on N (default 10, max 50).",
      minimum: 1,
      maximum: 50,
    }),
  ),
});

export type RecentTurnsParams = Static<typeof RecentTurnsParamsSchema>;

const RECENT_TURNS_DESCRIPTION = [
  "Recent USER turns recorded for one bot.",
  "",
  "Privacy invariants: USER text only (assistant replies are NOT",
  "captured); retention is 200 turns or 48 hours, whichever caps",
  "first; per-bot opt-out via securityScanning=false in network.json.",
  "",
  "If the bot has opted out, the response sets opted_out=true with",
  "an empty turns list — render that as 'no recent context available'",
  "rather than 'no recent activity'. Use this primarily as context",
  "for filing bug reports or answering 'what was admin doing on",
  "admin_bot recently?'.",
].join("\n");

export function createRecentTurnsToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "recent_turns",
    description: RECENT_TURNS_DESCRIPTION,
    parameters: RecentTurnsParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const p = (rawParams ?? {}) as RecentTurnsParams;
      const bot = (p.bot ?? "").trim();
      if (!bot) {
        return textResult("recent_turns: 'bot' is required.", true);
      }
      const qs = new URLSearchParams();
      qs.set("bot", bot);
      if (typeof p.limit === "number") qs.set("limit", String(p.limit));
      return call(logger, socketPath, "recent_turns", `/api/primary/state/turns?${qs}`);
    },
  });
}

// ── describe_bot ────────────────────────────────────────────────────────────

export const DescribeBotParamsSchema = Type.Object({
  bot: Type.String({
    description: "Which bot to describe. Use list_bots / pod_status first if unsure.",
  }),
});

export type DescribeBotParams = Static<typeof DescribeBotParamsSchema>;

const DESCRIBE_BOT_DESCRIPTION = [
  "One-call snapshot of one bot — role, tier, integrations, recent",
  "firing signals, and 7-day spend.",
  "",
  "Use this when admin asks 'tell me about admin_bot' or 'what's the",
  "state of team_bot_a'. Returns found=false (NOT a 404) when the bot id",
  "is unknown — render 'I don't recognize that bot' if found is",
  "false.",
].join("\n");

export function createDescribeBotToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "describe_bot",
    description: DESCRIBE_BOT_DESCRIPTION,
    parameters: DescribeBotParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const p = (rawParams ?? {}) as DescribeBotParams;
      const bot = (p.bot ?? "").trim();
      if (!bot) {
        return textResult("describe_bot: 'bot' is required.", true);
      }
      const qs = new URLSearchParams();
      qs.set("bot", bot);
      return call(logger, socketPath, "describe_bot", `/api/primary/state/describe?${qs}`);
    },
  });
}

// ── list_audits ─────────────────────────────────────────────────────────────

export const ListAuditsParamsSchema = Type.Object({
  bot: Type.Optional(
    Type.String({
      description:
        "Filter to one bot's latest audit. Omit to get the latest " +
        "audit per bot (cap on count via limit).",
    }),
  ),
  limit: Type.Optional(
    Type.Integer({
      description: "Cap on bots returned (default 5, max 50).",
      minimum: 1,
      maximum: 50,
    }),
  ),
});

export type ListAuditsParams = Static<typeof ListAuditsParamsSchema>;

const LIST_AUDITS_DESCRIPTION = [
  "Latest OC security audit results (one per bot, latest only).",
  "",
  "Use this when admin asks 'is anything failing audit?' or 'why is",
  "team_bot_a's audit flagging X?'. Returns per-bot findings + summary",
  "counts (critical/warn/info).",
  "",
  "If stale=true, the admin server hasn't yet swept since startup",
  "(or the data is hours old). Tell admin to refresh via the",
  "Security tab rather than reporting the cached numbers as fresh.",
  "",
  "This tool returns the LATEST audit per bot, not a history — the",
  "cache only holds the current state. Historical audits are not",
  "yet persisted.",
].join("\n");

export function createListAuditsToolFactory(logger: PluginLogger, socketPath?: string) {
  return (_ctx: Record<string, unknown>) => ({
    name: "list_audits",
    description: LIST_AUDITS_DESCRIPTION,
    parameters: ListAuditsParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const p = (rawParams ?? {}) as ListAuditsParams;
      const qs = new URLSearchParams();
      if (p.bot) qs.set("bot", p.bot);
      if (typeof p.limit === "number") qs.set("limit", String(p.limit));
      const path = `/api/primary/state/audits${qs.toString() ? `?${qs}` : ""}`;
      return call(logger, socketPath, "list_audits", path);
    },
  });
}
