/**
 * CaptureGate — D-TM4 capture from turns, stage one: a free regex gate
 * (`internal/design-pa-tasks-and-follow-through-2026-09-18.md` §1, D-TM4).
 * Session end never comes on OpenClaw; turns do. No match → no I/O. Match →
 * one capture request to the daemon (`POST /api/board-bot/capture`, row
 * `tracker.propose`), which makes the one fast-rung call and every store
 * write; this file writes nothing and calls no model. The user-side set is the
 * continuity engine's `DEFER_PATTERNS`, copied with attribution from
 * `task_extractor.py` (gallery `p-d60c8d6e`; 8bc826f76:packages/analyzer/
 * task_extractor.py) plus three deadline shapes; its `EXTERNAL_ACTION_PATTERNS`
 * were an approval hint with no consumer here, so are not copied. The bot side
 * needs a first-person commitment WITH a follow-up or a time. Counters ride
 * every post plus an hourly report, for the "capture gate live" control.
 */

import { adminSocketRequest } from "../util/adminSocket.js";
import { unwrapUserMessage } from "./messageUnwrap.js";

/** Continuity engine `DEFER_PATTERNS` (verbatim) + three deadline shapes. */
export const DEFER_PATTERNS: readonly RegExp[] = [
  /\blater\b/i, /\btonight\b/i, /\btomorrow\b/i, /\bthis week\b/i, /\bnext week\b/i,
  /\bwhen I (get back|wake up|have time|return)\b/i,
  /\bremind me( to)?\b/i, /\bdon'?t forget( to)?\b/i, /\bfollow[- ]?up\b/i, /\bcheck back\b/i,
  /\bI['’]ll (do|check|send|book|schedule|write|look)\b/i,
  /\bwe('ll| will) (need to|have to|do|handle)\b/i,
  /\bkeep working on\b/i, /\bcontinue (with|on)\b/i,
  /\bfinish (up |off )?(the |this |that )?\w+/i, /\bpick (this|it|that) up\b/i,
  /\bcome back to\b/i, /\bdo this (later|overnight|tomorrow|when)\b/i,
  /\bwork on (this|it|that) (overnight|later|tomorrow)\b/i,
  // Added here: a stated obligation with a deadline ("need to … by the 30th").
  /\b(need|have|got) to\b.{1,80}\bby (the \d{1,2}(st|nd|rd|th)?|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|end of|next)\b/i,
  /\bby (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b/i,
  /\bdeadline\b/i,
];

/** A first-person commitment in the bot's own reply. */
const BOT_FIRST_PERSON = /\b(I['’]ll|I will|I['’]m going to|let me)\b/i;
/** …that is a promise to come back, not a description of this reply. */
const BOT_FOLLOW_THROUGH: readonly RegExp[] = [
  /\b(I['’]ll|I will|let me) (check|get|come|circle) back\b/i,
  /\b(I['’]ll|I will|I['’]m going to) (follow[- ]?up|chase|nudge|remind you|ping you|keep an eye|watch for|let you know|keep you posted)\b/i,
  /\b(I['’]ll|I will|I['’]m going to)\b[^.!?\n]{0,60}\b(tomorrow|tonight|later|this week|next week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|in the morning|this afternoon|this evening|on the \d{1,2}(st|nd|rd|th)?)\b/i,
];

export type Speaker = "user" | "bot";

/** Names of the patterns that fired — empty means the gate did not fire. */
export function gateMatches(text: string, speaker: Speaker): string[] {
  if (!text) return [];
  if (speaker === "user") {
    return DEFER_PATTERNS.filter((p) => p.test(text)).map((p) => p.source);
  }
  if (!BOT_FIRST_PERSON.test(text)) return [];
  return BOT_FOLLOW_THROUGH.filter((p) => p.test(text)).map((p) => p.source);
}

/** The sentence(s) around the first match, bounded — the classifier's input. */
export const MAX_EXCERPT_CHARS = 600;
export function excerptAround(text: string, patternSource: string): string {
  const m = new RegExp(patternSource, "i").exec(text);
  if (!m || text.length <= MAX_EXCERPT_CHARS) return text.slice(0, MAX_EXCERPT_CHARS);
  const start = Math.max(0, m.index - MAX_EXCERPT_CHARS / 2);
  return text.slice(start, start + MAX_EXCERPT_CHARS);
}

export interface CaptureRequest {
  session: string; turn_id: string; speaker: Speaker; text_excerpt: string; patterns: string[];
}

/** Stage one for one turn: zero, one or two capture requests. Pure. */
export function captureRequestsForTurn(
  userText: string, botText: string, session: string, turnId: string,
): CaptureRequest[] {
  const out: CaptureRequest[] = [];
  for (const [speaker, raw] of [["user", unwrapUserMessage(userText)], ["bot", botText ?? ""]] as const) {
    const hits = gateMatches(raw, speaker);
    if (!hits.length) continue;
    out.push({ session, turn_id: turnId, speaker, text_excerpt: excerptAround(raw, hits[0]),
      patterns: hits.slice(0, 5) });
  }
  return out;
}

type Post = (body: Record<string, unknown>) => Promise<void>;
interface Logger { info(m: string): void; warn(m: string): void }

export const GATE_REPORT_INTERVAL_MS = 60 * 60_000;

/** Counters + the fire-and-forget poster. One per gateway. */
export class CaptureGate {
  private evaluated = 0;
  private matched = 0;
  private lastReportMs = 0;

  constructor(private readonly post: Post, private readonly logger?: Logger) {}

  /** agent_end for a user-triggered run. Never throws; never awaits the daemon's work. */
  async observeTurn(userText: string, botText: string, session: string, turnId: string,
    now = Date.now()): Promise<CaptureRequest[]> {
    this.evaluated += 1;
    const reqs = captureRequestsForTurn(userText, botText, session, turnId);
    if (reqs.length) this.matched += 1;
    const due = now - this.lastReportMs >= GATE_REPORT_INTERVAL_MS;
    if (!reqs.length && !due) return reqs;
    this.lastReportMs = now;
    const gate = { evaluated: this.evaluated, matched: this.matched };
    const bodies = reqs.length ? reqs.map((capture) => ({ gate, capture })) : [{ gate }];
    for (const body of bodies) {
      await this.post(body).catch((err) =>
        this.logger?.warn(`Evolve capture gate: daemon post failed (${err}) — nothing captured`));
    }
    return reqs;
  }
}

/** The production poster: one daemon call over the admin socket. */
export function socketPoster(socketPath: string): Post {
  return async (body) => {
    const res = await adminSocketRequest({
      method: "POST", path: "/api/board-bot/capture", body, socketPath, timeoutMs: 5_000,
    });
    if (res.status >= 300) throw new Error(`HTTP ${res.status}`);
  };
}

/** Wire the gate onto `agent_end` — user-triggered runs only (not heartbeat/cron/memory). */
export function registerCaptureGate(
  api: { on: (name: string, fn: (event: any, ctx: any) => Promise<void>, opts?: any) => void },
  gate: CaptureGate,
  extract: (messages: unknown) => { userMessage: string; assistantMessage: string },
): void {
  api.on("agent_end", async (event: any, ctx: any) => {
    const trigger = typeof ctx?.trigger === "string" ? ctx.trigger.trim().toLowerCase() : "";
    if (trigger && trigger !== "user") return;
    const { userMessage, assistantMessage } = extract(event?.messages);
    const session = String(ctx?.sessionKey ?? ctx?.sessionId ?? "unknown");
    const turnId = String(ctx?.runId ?? event?.runId ?? `${session}-${Date.now()}`);
    await gate.observeTurn(userMessage, assistantMessage, session, turnId);
  }, { name: "evolve-capture-gate" });
}
