/**
 * recurringRequest — conversation-only evidence, the in-bot half.
 *
 * Design: docs/design-app-spec-and-discovery-2026-08-15.md §7.1a
 * ("Conversation-only evidence — how it is detected").
 *
 * An automated morning brief (cron/heartbeat) is discoverable from files.
 * A *conversation-only* one — the user simply asks every morning — leaves
 * no file. §7.1a's answer is that the bot already summarizes each session,
 * so the design "adds one output and some arithmetic — no new pipeline, no
 * new LLM call":
 *
 *   - this module builds the one output: a `recurring_request`
 *     `{label, requester, hour}` stamped onto the `session_summary`
 *     annotation record by SessionSummarizer;
 *   - the arithmetic (same normalized label on >=N of the last M days,
 *     hour within +/-2h) runs pod-side over the accumulated rows, in
 *     `evolve_admin.applications.conversation_recurrence`.
 *
 * PRIVACY CONTRACT (§7.1a, `principle-per-bot-inference`)
 * -------------------------------------------------------
 * Only the label, the requester id, and the local hour leave the bot.
 *
 * Be precise about what that label is, because it lands in
 * {shared_dir}/annotations on every session. It is a bounded,
 * de-duplicated bag of at most MAX_LABEL_TOKENS (6) content tokens taken
 * from the user's OWN request. Design §7.1a defines the label as the
 * normalized ask, so content words survive by construction — and that
 * INCLUDES PROPER NOUNS: "summarize the thread with doctor weinstein"
 * keys as "summarize thread doctor weinstein". There is no NER here and
 * none is implied; do not describe the label as content-free.
 *
 * What it provably cannot carry, and what the tests pin:
 *   - a digit — every non-letter is a separator, so no card number,
 *     phone number, account id or year survives;
 *   - a date or a time — those tokens are in VOLATILE_TOKENS;
 *   - more than 6 tokens, or an unbounded string;
 *   - a quote — tokens are de-duplicated and stopword-stripped, so the
 *     original sentence cannot be reconstructed from the label.
 *
 * Three gates decide whether a row is produced at all — all three must
 * pass, and each one fails CLOSED (no row) rather than emitting a
 * degraded one:
 *
 *   1. `recurringRequestSignal` per-bot DNT flag in network.json
 *      (default ON, only an explicit `false` disables — the shape
 *      `_isPushbackEnabled` / `RecentTranscriptCapture.isEnabled`
 *      already use). This is the flippable off-switch that
 *      `feedback_user_observation_optout` requires of any passive
 *      observation feature.
 *   2. Per-identity do-not-track in the roster overlay
 *      ({sharedDir}/rosters/{botId}.json), keyed `platform:senderId`
 *      exactly like `blocked`. A blocked identity is excluded too — a
 *      blocked sender's traffic must not become an app draft.
 *   3. A resolved HUMAN requester. No captured sender means a heartbeat,
 *      a cron tick or a daemon-initiated turn, which is not a person
 *      asking for something. This is the same resolve-or-omit rule
 *      TurnObserver._buildSpeakerContextBlock applies, and it is load
 *      bearing: measured on the live macOS pod on 2026-08-19, only
 *      3,440 of 12,701 attributable sessions (27%) were `user_turn` —
 *      the other 73% were `heartbeat` (6,842) and `cron_app` (2,348).
 *      Without this gate the detector would fire on the heartbeat
 *      fleet and manufacture a draft for every scheduled job on the pod.
 */

import * as fs from "node:fs";
import * as path from "node:path";

/** The record stamped onto `session_summary`. Exactly the three fields
 *  design §7.1a names — no content, no session id, no message text. */
export interface RecurringRequest {
  /** Normalized, bounded label — see `normalizeRequestLabel`. */
  readonly label: string;
  /** `platform:senderId`, the same identity key the roster overlay uses. */
  readonly requester: string;
  /** Pod-local hour of the request, 0-23. */
  readonly hour: number;
}

/** Who asked. Mirrors the fields `senderRegistry.getSender()` resolves. */
export interface RequesterIdentity {
  readonly platform: string | null;
  readonly senderId: string | null;
}

/** Upper bound on content tokens kept in a label. Bounded on purpose:
 *  a label is a recurrence KEY, not a summary, and a short key is both
 *  more stable across days and structurally unable to carry content. */
export const MAX_LABEL_TOKENS = 6;

/** Below this a "label" is too thin to mean anything ("do it", "thanks")
 *  and would collide with unrelated requests. Fail closed. */
export const MIN_LABEL_TOKENS = 2;

/** Cap on app tags appended to a label, sorted for determinism. */
export const MAX_LABEL_TAGS = 4;

/** Only this much of the request is inspected — a recurring ask is
 *  identified by how it opens, and an unbounded scan would let a long
 *  paste dominate the token budget. */
export const MAX_SCAN_CHARS = 400;

/** Text openers that mark a machine-originated turn. OC threads cron and
 *  heartbeat wake-ups through the same user-message slot as a person, so
 *  the text gate backs up the sender gate. Measured: 62 of 215 captured
 *  "user" turns on the live pod (2026-08-18) opened with `[cron:`. */
export const SYNTHETIC_PREFIXES = ["[cron", "[heartbeat", "[system", "[scheduled", "[trigger"];

/** Sentinel outcomes/messages that are protocol, not a request. */
export const SENTINEL_TEXTS = new Set(["heartbeat_ok", "no_reply", "ok", "ack", "noop"]);

/** Tokens whose VALUE changes every time the same request is made.
 *  Keeping any of them would give the same weekly ask a different label
 *  each day and recurrence would never accumulate — this list is the
 *  difference between a detector that works and one that never fires. */
export const VOLATILE_TOKENS = new Set([
  // weekdays
  "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
  "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
  // relative days. NOTE the deliberate omission of morning/afternoon/
  // evening/night: those are STABLE across repeats of the same habit and
  // are usually the most discriminative token in it ("morning summary" is
  // design §7.1a's own example label). Only tokens whose value changes
  // between two occurrences of the SAME ask belong here.
  "today", "todays", "tomorrow", "tomorrows", "yesterday", "yesterdays",
  // months
  "january", "february", "march", "april", "may", "june", "july",
  "august", "september", "october", "november", "december",
  "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
  // clock / timezone
  "am", "pm", "utc", "gmt", "pst", "pdt", "est", "edt", "cst", "cdt", "mst", "mdt",
  "oclock", "hrs", "hr", "min", "mins",
  // recurrence adverbs — they describe the cadence, which is what the
  // arithmetic measures; keeping them would not add discrimination
  "daily", "weekly", "every", "each", "again", "usual", "usually",
]);

/** Politeness, framing and filler. Dropping them makes "can you please
 *  give me the morning summary" and "morning summary" the same label. */
export const STOPWORDS = new Set([
  "a", "an", "the", "this", "that", "these", "those",
  "i", "im", "ive", "id", "me", "my", "mine", "we", "our", "us", "you", "your", "yours",
  "is", "are", "am", "be", "been", "was", "were", "do", "does", "did", "done",
  "can", "could", "would", "will", "shall", "should", "may", "might", "must",
  "please", "pls", "plz", "thanks", "thank", "hey", "hi", "hello", "yo", "ok", "okay",
  "and", "or", "but", "if", "then", "so", "as", "at", "by", "for", "from", "in",
  "into", "of", "on", "onto", "to", "with", "about", "over", "up", "out",
  "it", "its", "just", "now", "here", "there", "some", "any", "all", "got",
  // question words and greetings — framing, never the subject
  "what", "whats", "when", "where", "who", "whos", "why", "how", "hows",
  "which", "good",
  "want", "wanna", "need", "like", "lets", "let", "gimme", "give", "get",
  "run", "go", "make", "put", "send", "show", "tell", "help",
]);

// ── label normalization ──────────────────────────────────────────────────────

/**
 * Reduce a request to a stable, content-free recurrence key.
 *
 * Returns `null` — never a degraded string — when the text is
 * machine-originated, a protocol sentinel, or too thin to key on. A null
 * means "no row", which is always safe; a wrong label would manufacture a
 * phantom draft, which is not.
 *
 * Deterministic and pure: same input, same output, no clock, no locale.
 * Latin-script only. A request written entirely in a non-latin script
 * normalizes to zero tokens and yields `null` rather than a garbage key —
 * a stated v1 limitation, and the fail-closed direction.
 */
export function normalizeRequestLabel(text: unknown): string | null {
  if (typeof text !== "string") return null;
  // Slice by CODE POINT, not by UTF-16 code unit. `String.prototype.slice`
  // counts surrogate pairs as two, so a single astral character (emoji)
  // before the cap moves the boundary one unit earlier than Python's
  // `text[:MAX_SCAN_CHARS]` does. On a long request with few distinct
  // content tokens early, that lands the two implementations on different
  // labels — measured: with 7 leading emoji the TS side returned null
  // where Python returned a label. The spread iterator walks code points,
  // which is exactly Python's slicing unit.
  const head = [...text].slice(0, MAX_SCAN_CHARS).join("").trim();
  if (!head) return null;

  const lowered = head.toLowerCase();
  for (const prefix of SYNTHETIC_PREFIXES) {
    if (lowered.startsWith(prefix)) return null;
  }
  if (SENTINEL_TEXTS.has(lowered.replace(/[^a-z_]/g, ""))) return null;

  // Anything that is not a latin letter becomes a separator. This is what
  // strips markdown, emoji, punctuation, URLs and — deliberately — every
  // digit, so no number can survive into the label.
  const tokens = lowered.replace(/[^a-z]+/g, " ").split(" ").filter(Boolean);

  const kept: string[] = [];
  const seen = new Set<string>();
  for (const tok of tokens) {
    if (tok.length < 3) continue;             // "to", "of", stray letters
    if (VOLATILE_TOKENS.has(tok)) continue;
    if (STOPWORDS.has(tok)) continue;
    if (seen.has(tok)) continue;
    seen.add(tok);
    kept.push(tok);
    if (kept.length >= MAX_LABEL_TOKENS) break;
  }

  if (kept.length < MIN_LABEL_TOKENS) return null;
  return kept.join(" ");
}

/**
 * Compose the full label: the normalized request head, plus the session's
 * application tags when there are any — design §7.1a's example label
 * ("morning summary: email + calendar + weather") has exactly this shape.
 *
 * Tags are sorted and capped so the label is order-independent and bounded.
 */
export const MAX_TAG_CHARS = 32;

/**
 * Sanitize one application tag for inclusion in a label.
 *
 * Tags come from `APPLICATION_PATTERNS` and from the operator's
 * `network.json::applicationPatterns[].tag`, so they are configuration
 * rather than user content — but the label's privacy contract is stated
 * about the LABEL, and the label is what leaves the bot. An operator tag
 * carrying a digit ("invoice-4111111111111111") or 5,000 characters would
 * otherwise ride straight into the shared annotation and break both the
 * "cannot carry a number" and the bounded-length guarantees.
 *
 * Same alphabet rule as the head: letters survive, everything else is a
 * separator, collapsed to single hyphens and length-capped.
 */
export function sanitizeTag(raw: unknown): string | null {
  const t = String(raw ?? "")
    .toLowerCase()
    .replace(/[^a-z]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, MAX_TAG_CHARS)
    .replace(/-+$/g, "");
  return t || null;
}

export function composeLabel(head: string, appTags?: readonly string[]): string {
  const cleaned: string[] = [];
  for (const raw of appTags ?? []) {
    const t = sanitizeTag(raw);
    if (t) cleaned.push(t);
  }
  const tags = [...new Set(cleaned)].sort().slice(0, MAX_LABEL_TAGS);
  return tags.length ? `${head}: ${tags.join(" + ")}` : head;
}

// ── local hour ───────────────────────────────────────────────────────────────

/**
 * Pod-local hour (0-23) of `at`. Storage stays UTC everywhere else; the
 * hour is local because "asks every morning" is a local-time statement —
 * a UTC hour would smear a 9am ask across two buckets on DST boundaries.
 */
export function localHour(at: Date, timezone: string): number {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      hour: "numeric",
      hourCycle: "h23",
    }).formatToParts(at);
    const raw = parts.find((p) => p.type === "hour")?.value;
    const h = Number(raw);
    if (Number.isInteger(h) && h >= 0 && h <= 23) return h;
  } catch {
    // Unknown/invalid IANA zone — fall through to UTC rather than throw.
  }
  return at.getUTCHours();
}

// ── DNT gates ────────────────────────────────────────────────────────────────

const DNT_CACHE_TTL_MS = 60_000;

let _networkCache: { key: string; value: any; at: number } | null = null;
let _overlayCache: { key: string; value: any; at: number } | null = null;

function readJsonCached(
  filePath: string,
  slot: "network" | "overlay",
  nowMs: number,
): any {
  const cache = slot === "network" ? _networkCache : _overlayCache;
  if (cache && cache.key === filePath && nowMs - cache.at < DNT_CACHE_TTL_MS) {
    return cache.value;
  }
  let value: any = null;
  try {
    value = JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    value = null; // missing / unreadable / invalid — caller decides the default
  }
  const entry = { key: filePath, value, at: nowMs };
  if (slot === "network") _networkCache = entry;
  else _overlayCache = entry;
  return value;
}

/**
 * Per-bot do-not-track switch: `bots.<botId>.recurringRequestSignal` in
 * network.json. Default ON; only an explicit boolean `false` disables, so
 * a typo can never silently switch observation off (and, symmetrically,
 * an unreadable network.json fails OPEN to the default rather than
 * silently disabling a feature the operator believes is running).
 *
 * Same shape as `TurnObserver._isPushbackEnabled` and
 * `RecentTranscriptCapture.isEnabled` — one idiom for every per-bot DNT.
 */
export function isRecurringRequestEnabled(
  sharedDir: string,
  botId: string,
  nowMs: number = Date.now(),
): boolean {
  const network = readJsonCached(path.join(sharedDir, "network.json"), "network", nowMs);
  const botCfg = network?.bots?.[botId];
  return !(botCfg && botCfg.recurringRequestSignal === false);
}

/**
 * Per-identity exclusion, read from the roster overlay
 * ({sharedDir}/rosters/{botId}.json) on the `platform:senderId` key —
 * the same key space `blocked` and `ignored` already use.
 *
 * Excluded when EITHER:
 *   - `do_not_track[key]` is present (design §7.1a: "Users with
 *     do-not-track set are excluded"), or
 *   - `blocked[key]` is present — a blocked identity's traffic must never
 *     become evidence for an app draft.
 *
 * NOTE (stated gap, not a silent one): nothing in the tree WRITES
 * `do_not_track` yet — the per-user privacy surface belongs to the Users
 * page, not to this chip. Until that writer exists the effective
 * user-facing switch is the per-bot flag above. This read side ships now
 * so the honoring is in place from v1, as
 * `feedback_user_observation_optout` requires, rather than being retro-fitted.
 */
export function isRequesterExcluded(
  sharedDir: string,
  botId: string,
  platform: string,
  senderId: string,
  nowMs: number = Date.now(),
): boolean {
  const overlay = readJsonCached(
    path.join(sharedDir, "rosters", `${botId}.json`),
    "overlay",
    nowMs,
  );
  if (!overlay) return false; // no overlay → no exclusions (fresh bot)
  const key = `${platform}:${senderId}`;
  const dnt = overlay.do_not_track || overlay.doNotTrack || {};
  if (key in dnt) return true;
  const blocked = overlay.blocked || {};
  return key in blocked;
}

/** Test seam — clears the 60s config caches. Production never needs this. */
export function _resetRecurringRequestCaches(): void {
  _networkCache = null;
  _overlayCache = null;
}

// ── the one output ───────────────────────────────────────────────────────────

export interface BuildRecurringRequestInput {
  /** The user's own words for the request — the FIRST user message of the
   *  session, which is the ask; later turns are the conversation about it. */
  readonly requestText: unknown;
  /** `applications_invoked` for the session, if any. */
  readonly appTags?: readonly string[];
  /** Resolved sender, from `senderRegistry.getSender(runId)`. */
  readonly requester: RequesterIdentity | null | undefined;
  /** When the request happened. Defaults to now. */
  readonly at?: Date;
  /** Pod IANA timezone, from `getPodTimezone(sharedDir)`. */
  readonly timezone: string;
  /** For the DNT gates. Omit both to skip them (already checked upstream). */
  readonly sharedDir?: string;
  readonly botId?: string;
  readonly nowMs?: number;
}

/**
 * Build the `recurring_request` row for one session, or `null`.
 *
 * `null` is the common and correct answer: most sessions are machine
 * triggered, have no human requester, or carry no keyable ask. Only a
 * human-initiated session with a normalizable request produces a row.
 */
export function buildRecurringRequest(
  input: BuildRecurringRequestInput,
): RecurringRequest | null {
  const platform = input.requester?.platform;
  const senderId = input.requester?.senderId;
  // Gate 3 — resolve-or-omit. No platform or no sender id means this turn
  // has no identified human behind it; do NOT guess a platform (the same
  // rule TurnObserver._buildSpeakerContextBlock enforces, for the same
  // reason: a guessed platform mis-attributes across id spaces).
  if (!platform || !senderId) return null;

  const nowMs = input.nowMs ?? Date.now();
  if (input.sharedDir && input.botId) {
    // Gate 1 — per-bot DNT.
    if (!isRecurringRequestEnabled(input.sharedDir, input.botId, nowMs)) return null;
    // Gate 2 — per-identity DNT / blocked.
    if (isRequesterExcluded(input.sharedDir, input.botId, platform, senderId, nowMs)) {
      return null;
    }
  }

  const head = normalizeRequestLabel(input.requestText);
  if (!head) return null;

  return {
    label: composeLabel(head, input.appTags),
    requester: `${platform}:${senderId}`,
    hour: localHour(input.at ?? new Date(), input.timezone),
  };
}

/**
 * The ask of a session is its first user message — so return the whole
 * TURN, not just its text. The caller needs the requester from the SAME
 * turn: keying the first turn's ask under the last turn's sender both
 * misattributes the request and checks the per-identity do-not-track gate
 * against the wrong person.
 *
 * Returns `null` when the session has no user message at all.
 */
export function firstUserTurn<T extends { userMessage?: unknown }>(
  turns: ReadonlyArray<T>,
): T | null {
  for (const t of turns) {
    if (typeof t?.userMessage === "string" && t.userMessage.trim()) return t;
  }
  return null;
}
