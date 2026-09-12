/**
 * BoardTool — chat parity with the board (D-MB4).
 *
 * Design: `internal/design-pa-mobile-board-2026-08-31.md` D-MB1 (one writer:
 * the admin daemon) + D-MB4 (the verbs), as amended by
 * `internal/design-pa-board-interface-v2-2026-09-04.md` D-BI7 (lanes are
 * *when*, `owner` is *who*) and the `enrichment{}` block from
 * `internal/design-pa-lists-and-board-2026-09-01.md` §2.
 *
 * The user's board lives in the pod's shared dir and is written by ONE
 * process, the admin daemon. The phone reaches it through a token-gated web
 * surface; the bot reaches it through THIS tool, which is nothing but a
 * request shaper: one daemon call per verb over the admin-daemon unix socket,
 * no local state, no file writes, no fallback path. Daemon unreachable ⇒ the
 * tool refuses and NOTHING was written — the same fail-closed posture as
 * `action.*` (a fallback would stay inducible by killing the socket).
 *
 * IDENTITY. The bot authenticates as itself: the server binds the calling bot
 * from the kernel-reported peer uid of the socket connection, so the paths
 * carry no bot id and this file never sends one. A bot cannot name another
 * bot's board because there is no field in which to name it.
 *
 * CONTEXT ECONOMY (CE-2/CE-3). This is ONE tool with a verb enum, not five
 * tools: five schemas would ride in every prompt of every turn for what is one
 * surface. `list` renders compact lines — never card JSON — and truncates at
 * {@link MAX_LIST_CARDS} cards / {@link MAX_LIST_CHARS} characters, saying so
 * when it does. The board is a place the bot looks things up, not a history
 * tax on every turn.
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

const SOCKET_NAME = "admin-daemon.sock";

/** Root of the bot-facing board API. No bot id: identity is the peer uid. */
const BOARD_BASE = "/api/board-bot";

/** Lanes answer WHEN (D-BI7). There is no bot lane; `owner` answers who. */
export const BOARD_LANES = ["inbox", "today", "later", "done", "dropped"] as const;

/** Who a card is on (D-BI7). `bot` is an offer, not an instruction. */
export const BOARD_OWNERS = ["me", "bot"] as const;

/**
 * The one optional tap-reason a drop may carry (D-BI2). Fixed vocabulary,
 * because this is the learning loop's negative signal and a detector can only
 * count reasons it can compare.
 */
export const DROP_REASONS = [
  "not mine", "already handled", "never", "later than later",
] as const;

/**
 * What `move` says when asked for the retired Bot lane.
 *
 * The lane enum already makes `bot` unschedulable, so a well-formed call
 * cannot reach here — this is for the model that reaches for the lane it
 * remembers. It refuses locally (no daemon round trip for a request that
 * cannot succeed) and names the verb that still does what was meant: hand-over
 * did not go away, it moved from the WHEN axis to the WHO one.
 */
export const MOVE_TO_BOT_REFUSAL =
  "'bot' is not a lane. Lanes answer WHEN (inbox/today/later/done/dropped); " +
  "who a card is on is the separate `owner`. To hand this card to yourself, " +
  "use verb=assign with owner='bot' — it stays in whatever lane it is in. " +
  "Nothing was changed.";

/** Delegation lifecycle the bot reports through `progress`. */
export const DELEGATION_STATES = [
  "accepted", "in_progress", "returned_for_review", "done", "blocked",
] as const;

export const BOARD_VERBS = ["list", "add", "move", "assign", "progress"] as const;
export type BoardVerb = (typeof BOARD_VERBS)[number];

/**
 * Hard caps on what one `list` puts in the model's context.
 *
 * Whichever binds first wins, and the trailer says what was left out so the
 * model narrows its filter rather than assuming it saw everything. A board may
 * legitimately hold thousands of cards; a turn never needs to read them.
 */
export const MAX_LIST_CARDS = 60;
export const MAX_LIST_CHARS = 4000;

/** Characters of a card id shown in a list line — enough to name it back. */
const ID_PREFIX_CHARS = 8;

/**
 * The refusal when the daemon cannot be reached. One text, so the model (and
 * the person reading over its shoulder) always sees the same fact: the board
 * was not read, not written, and not partially anything.
 */
export const DAEMON_UNREACHABLE_REFUSAL =
  "The board is unavailable right now — the Evolve admin daemon is not " +
  "reachable, and the board can only be read or written through it. " +
  "NOTHING was changed. Say so plainly and try again later; do not keep a " +
  "task list anywhere else in the meantime.";

/** Transport seam for tests. Real callers omit it (live unix socket). */
export type BoardTransport =
  (req: AdminSocketRequest) => Promise<AdminSocketResponse>;

export interface BoardToolConfig {
  /** This bot's shared dir — the socket lives at {sharedDir}/admin-daemon.sock. */
  readonly sharedDir: string;
  /** This bot's id — diagnostics only; identity is bound server-side. */
  readonly botId: string;
  /** Per-call socket override (tests). Real callers omit it. */
  readonly socketPath?: string;
  /** Transport override for tests. Real callers omit it. */
  readonly transport?: BoardTransport;
}

// Every enrichment field names where it came from — a fact nobody can check
// later is the thing this shape exists to prevent. `captured_at` is stamped
// by the pod when omitted, which is the honest default.
const EnrichmentFieldSchema = Type.Object({
  value: Type.Unknown(),
  source: Type.String({ description: "Where it came from." }),
  captured_at: Type.Optional(Type.String()),
});

export const BoardParamsSchema = Type.Object({
  verb: Type.Union(BOARD_VERBS.map((v) => Type.Literal(v)), {
    description:
      "list=read it; add=new card; move=change WHEN; assign=change WHO; " +
      "progress=report on a handed card.",
  }),
  id: Type.Optional(Type.String({
    description: "Card id, or a 6+ character prefix of one. Required for " +
      "move/assign/progress.",
  })),
  title: Type.Optional(Type.String({ description: "add: what the card says." })),
  cluster: Type.Optional(Type.String({
    description:
      "Life area (lowercase slug): health, fitness, travel, work, social, " +
      "hobbies, family, home, admin, or one the user already uses.",
  })),
  lane: Type.Optional(Type.Union(BOARD_LANES.map((l) => Type.Literal(l)), {
    description: "WHEN. inbox=untriaged, dropped=won't do. Filter, or add's lane.",
  })),
  to_lane: Type.Optional(Type.Union(BOARD_LANES.map((l) => Type.Literal(l)), {
    description: "move: destination. No 'bot' lane — use assign.",
  })),
  reason: Type.Optional(Type.String({
    description:
      "to_lane=dropped only. One of: " + DROP_REASONS.join("|"),
  })),
  source_id: Type.Optional(Type.String({
    description: "add: upstream id (calendar event, email) — dedup key.",
  })),
  owner: Type.Optional(Type.Union(BOARD_OWNERS.map((o) => Type.Literal(o)), {
    description:
      "WHO: me (the user) or bot (you). On assign, 'bot' OFFERS it to you — " +
      "accept or decline via progress.",
  })),
  state: Type.Optional(Type.Union(DELEGATION_STATES.map((s) => Type.Literal(s)), {
    description: "progress: where the card you were handed now stands.",
  })),
  note: Type.Optional(Type.String({
    description: "add: context. progress: one line on what happened.",
  })),
  cost_to_date: Type.Optional(Type.Number({
    description: "progress: dollars spent on this card so far.",
  })),
  source: Type.Optional(Type.String({
    description: "add: where it came from — chat, calendar, email.",
  })),
  enrichment: Type.Optional(Type.Record(Type.String(), EnrichmentFieldSchema, {
    description:
      "add only: facts worth keeping, captured ONCE now — runtime_min, " +
      "pages, why_saved. Never re-fetched.",
  })),
});
export type BoardParams = Static<typeof BoardParamsSchema>;

const BOARD_DESCRIPTION = [
  "The user's task board — the SAME board they see on their phone. This is",
  "the only place tasks live: never keep a parallel list in a file, a note,",
  "or your own memory.",
  "Lanes are WHEN (inbox/today/later/done/dropped); owner is WHO (me/bot).",
  "No bot lane: to take a card ASSIGN it (owner=bot), don't move it.",
  "Start with verb=list before adding, so you don't duplicate a card.",
].join(" ");

function textResult(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    ...(isError ? { isError: true } : {}),
  };
}

interface BoardCardRow {
  id?: string;
  title?: string;
  lane?: string;
  owner?: string;
  cluster?: string;
  delegation?: string;
  enriched?: string[];
}

/**
 * Render a `list` response as compact lines, bounded by both caps.
 *
 * Exported for the test that proves a 200-card board cannot blow the budget.
 */
export function renderList(payload: Record<string, unknown>): string {
  const cards = Array.isArray(payload.cards)
    ? (payload.cards as BoardCardRow[]) : [];
  const total = typeof payload.total === "number" ? payload.total : cards.length;
  if (cards.length === 0) {
    return total === 0
      ? "The board has no cards matching that."
      : "No cards matched that filter.";
  }
  const lines: string[] = [];
  let chars = 0;
  let shown = 0;
  for (const card of cards) {
    if (shown >= MAX_LIST_CARDS) break;
    const owner = card.owner === "bot"
      ? `bot${card.delegation ? `:${card.delegation}` : ""}`
      : "me";
    const line = [
      String(card.id ?? "").slice(0, ID_PREFIX_CHARS),
      String(card.title ?? ""),
      String(card.lane ?? ""),
      owner,
      String(card.cluster ?? ""),
    ].join(" · ") + (card.enriched?.length ? ` (+${card.enriched.join(",")})` : "");
    if (chars + line.length + 1 > MAX_LIST_CHARS) break;
    lines.push(line);
    chars += line.length + 1;
    shown += 1;
  }
  const head = `${total} card${total === 1 ? "" : "s"}; showing ${shown}.`;
  const tail = shown < total
    ? `\n… ${total - shown} more not shown — narrow it with cluster, lane or owner.`
    : "";
  return `${head}\n${lines.join("\n")}${tail}`;
}

/** One card, one line — what add/move/assign/progress echo back. */
function renderCard(card: Record<string, unknown>, verb: string): string {
  const id = String(card.id ?? "");
  const delegation = card.delegation as Record<string, unknown> | undefined;
  const owner = card.owner === "bot"
    ? `bot${delegation?.state ? ` (${delegation.state})` : ""}`
    : "me";
  return (
    `${verb}: ${id.slice(0, ID_PREFIX_CHARS)} · ${String(card.title ?? "")} · ` +
    `${String(card.lane ?? "")} · ${owner} · ${String(card.cluster ?? "")}`
  );
}

/** The request each verb makes. Kept as data so the shapes stay comparable. */
function buildRequest(p: BoardParams): {
  method: "GET" | "POST"; path: string; body?: Record<string, unknown>;
} | string {
  switch (p.verb) {
    case "list": {
      const qs = new URLSearchParams();
      for (const key of ["cluster", "lane", "owner"] as const) {
        const value = (p[key] ?? "").toString().trim();
        if (value) qs.set(key, value);
      }
      // The wire is capped too (the daemon caps again on its side). `total`
      // in the response is the FULL filtered count, so the "… N more" trailer
      // stays honest without a second round trip.
      qs.set("limit", String(MAX_LIST_CARDS));
      return { method: "GET", path: `${BOARD_BASE}/cards?${qs}` };
    }
    case "add": {
      const title = (p.title ?? "").trim();
      if (!title) return "board add: `title` is required — what is the card?";
      const body: Record<string, unknown> = {
        title,
        cluster: (p.cluster ?? "admin").trim() || "admin",
        source: (p.source ?? "chat").trim() || "chat",
      };
      if (p.lane) body.lane = p.lane;
      if (p.owner) body.owner = p.owner;
      if (p.note) body.note = p.note;
      if (p.source_id) body.source_id = p.source_id.trim();
      if (p.enrichment !== undefined) body.enrichment = p.enrichment;
      return { method: "POST", path: `${BOARD_BASE}/cards`, body };
    }
    case "move": {
      const id = (p.id ?? "").trim();
      if (!id) return "board move: `id` is required (list the board first).";
      if (!p.to_lane) {
        return `board move: \`to_lane\` is required — one of ${BOARD_LANES.join(", ")}.`;
      }
      if ((p.to_lane as string) === "bot") return MOVE_TO_BOT_REFUSAL;
      const body: Record<string, unknown> = { to_lane: p.to_lane };
      // A reason is what a DROP meant; anywhere else it would be a field the
      // learning loop reads as a dismissal that never happened. Refused here
      // as well as in the store, so the model is told rather than surprised
      // by a 400 it has to interpret.
      if (p.reason) {
        if (p.to_lane !== "dropped") {
          return "board move: `reason` belongs to a drop — only send it with " +
            "to_lane='dropped'. Nothing was changed.";
        }
        body.reason = p.reason;
      }
      return {
        method: "POST", path: `${BOARD_BASE}/cards/${encodeURIComponent(id)}/move`,
        body,
      };
    }
    case "assign": {
      const id = (p.id ?? "").trim();
      if (!id) return "board assign: `id` is required (list the board first).";
      if (!p.owner) return "board assign: `owner` is required — 'me' or 'bot'.";
      return {
        method: "POST", path: `${BOARD_BASE}/cards/${encodeURIComponent(id)}/assign`,
        body: { owner: p.owner },
      };
    }
    case "progress": {
      const id = (p.id ?? "").trim();
      if (!id) return "board progress: `id` is required (list the board first).";
      if (!p.state) {
        return `board progress: \`state\` is required — one of ${DELEGATION_STATES.join(", ")}.`;
      }
      const body: Record<string, unknown> = { state: p.state };
      if (p.note) body.note = p.note;
      if (typeof p.cost_to_date === "number") body.cost_to_date = p.cost_to_date;
      return {
        method: "POST",
        path: `${BOARD_BASE}/cards/${encodeURIComponent(id)}/progress`, body,
      };
    }
    default:
      return `board: unknown verb '${String(p.verb)}'. One of ${BOARD_VERBS.join(", ")}.`;
  }
}

/**
 * Build the `board` tool factory.
 *
 * Every failure mode — a bad verb, a daemon HTTP error, an unreachable socket
 * — returns a NON-throwing tool envelope: a board fault must never break the
 * turn the user is having.
 */
export function createBoardToolFactory(
  config: BoardToolConfig,
  logger: PluginLogger,
) {
  const socketPath = config.socketPath ?? join(config.sharedDir, SOCKET_NAME);
  const transport: BoardTransport = config.transport ?? adminSocketRequest;

  return (_ctx: Record<string, unknown>) => ({
    name: "board",
    description: BOARD_DESCRIPTION,
    parameters: BoardParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = (rawParams ?? {}) as BoardParams;
      const req = buildRequest(params);
      if (typeof req === "string") return textResult(req, true);
      try {
        const res = await transport({
          method: req.method, path: req.path, body: req.body, socketPath,
        });
        const payload = (res.body && typeof res.body === "object"
          ? res.body as Record<string, unknown> : {});
        if (res.status >= 200 && res.status < 300) {
          if (params.verb === "list") return textResult(renderList(payload));
          // D-BI2: an add whose source id matches something the user already
          // finished or dropped is SKIPPED, not written. Saying so plainly —
          // rather than echoing the settled card as if it were new — is what
          // stops the bot telling the user it added a task it did not add.
          if (payload.skipped) {
            const settled = payload.card as Record<string, unknown> | undefined;
            return textResult(
              `board add: skipped — the user already ${
                payload.lane === "dropped" ? "dropped" : "finished"
              } this (${String(settled?.title ?? "")}). Not re-added.`);
          }
          const card = payload.card;
          if (card && typeof card === "object") {
            return textResult(
              renderCard(card as Record<string, unknown>, params.verb));
          }
          return textResult(`board ${params.verb}: done.`);
        }
        const detail = String(payload.error ?? payload.detail ?? `HTTP ${res.status}`);
        logger.warn(`board ${params.verb} failed (${res.status}): ${detail}`);
        return textResult(`board ${params.verb} failed: ${detail}`, true);
      } catch (err) {
        if (err instanceof AdminSocketUnavailable) {
          logger.warn(`board ${params.verb} — admin daemon unavailable: ${err.message}`);
          return textResult(DAEMON_UNREACHABLE_REFUSAL, true);
        }
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`board ${params.verb} unexpected error: ${msg}`);
        return textResult(`board ${params.verb} error: ${msg}`, true);
      }
    },
  });
}
