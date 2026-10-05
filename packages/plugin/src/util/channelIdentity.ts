/**
 * Channel-identity resolution off an OC hook ``(event, ctx)`` pair.
 *
 * ── Why this module exists ────────────────────────────────────────────
 * ``ctx.channelId`` is NOT a stable carrier of the channel *type*. Older
 * gateways threaded the channel-TYPE name there ("telegram", "slack"),
 * and a good deal of Evolve's plugin code was written against that
 * assumption. On OC 2026.7.1-2 it carries the actual chat id instead —
 * verified live from a Slack channel turn (team-bot-a, 2026-08-19):
 *
 *     sessionKey:      "agent:main:slack:channel:g0t79fgse"
 *     messageProvider: "slack"
 *     channel:         "slack"
 *     channelId:       "g0t79fgse"     <-- an ID, not a type
 *     chatId:          "G0T79FGSE"
 *     senderId:        "U087LN8U4J0"
 *
 * Reading the type off ``channelId`` therefore yields "g0t79fgse", which
 * ``normalizePlatform`` correctly refuses (null) — and every resolve-or-
 * omit consumer downstream silently degrades. The type-shaped fields
 * (``ctx.channel`` / ``ctx.messageProvider``) are the correct source on
 * this gateway; ``channelId`` stays in the chain as the legacy fallback.
 *
 * Telegram was unaffected (its ``channelId`` still normalizes) which is
 * why the breakage was invisible until a non-Telegram surface was used.
 */

import { isAutoChannelTell } from "../breakers/sourceClassifier.js";
import { normalizePlatform } from "./senderRegistry.js";


/** The subset of the OC hook context these resolvers read. Deliberately
 *  loose — OC's ctx is untyped at the plugin boundary and its shape has
 *  moved between versions. */
export interface ChannelIdentitySources {
  /** OC ≥2026.7: the channel TYPE ("slack", "telegram"). */
  channel?: unknown;
  /** OC ≥2026.7: the same type under its transport-layer name. */
  messageProvider?: unknown;
  /** Legacy gateways: the channel TYPE. OC ≥2026.7: the chat ID. */
  channelId?: unknown;
  /** "agent:main:<type>:<surface>:<id>" — carries the type as a segment. */
  sessionKey?: unknown;
}


function asStr(v: unknown): string {
  return typeof v === "string" ? v : "";
}


/**
 * Resolve the speaker's messaging platform from the hook context, or
 * ``null`` when no source yields a recognized platform.
 *
 * Candidates are tried in order and the FIRST that normalizes to one of
 * the four roster platforms wins; a candidate that does not normalize is
 * skipped rather than terminating the chain (on OC 2026.7 ``channelId``
 * is a chat id, so a plain ``??`` chain over these fields would stop on a
 * value that is present but useless).
 *
 * Resolve-or-null is the contract, NOT resolve-or-guess: an unrecognized
 * value must still yield ``null`` so the consumers keep their omit /
 * refuse behaviour. Defaulting to a platform would mis-attribute a
 * speaker's role across id-spaces — the exact hazard audit R1a G-N2
 * (#3378) closed.
 *
 * The ``sessionKey`` last resort is an EXACT match against a
 * colon-delimited segment, never a substring or a fixed position, so it
 * does not depend on the key's arity or segment order — only on the
 * platform name appearing as its own segment, which is what makes the
 * key routable in the first place. Evolve's own subagent keys
 * ("evolve:<kind>:…", see ``classifyEvolveSubagentKey``) carry no
 * platform segment and resolve to null here, as they should.
 */
export function resolveSenderPlatform(
  ctx: ChannelIdentitySources | null | undefined,
  event?: ChannelIdentitySources | null,
): string | null {
  const candidates = [
    ctx?.channel,
    ctx?.messageProvider,
    // Legacy: gateways before OC 2026.7 threaded the TYPE here. Keep it —
    // a pod can still be running one.
    ctx?.channelId,
    event?.channelId,
  ];
  for (const c of candidates) {
    const p = normalizePlatform(asStr(c));
    if (p) return p;
  }
  return platformFromSessionKey(asStr(ctx?.sessionKey) || asStr(event?.sessionKey));
}


/**
 * Last-resort platform read off a session key's colon-delimited segments.
 * Exported for testing; prefer ``resolveSenderPlatform``.
 */
export function platformFromSessionKey(sessionKey: string): string | null {
  if (!sessionKey) return null;
  for (const seg of sessionKey.split(":")) {
    const s = seg.trim().toLowerCase();
    if (!s) continue;
    // Exact segment only. A prefix match here would let a chat id or a
    // user-chosen name ("slackbot-relay") masquerade as its platform,
    // which is the mis-attribution this whole chain exists to prevent.
    if (normalizePlatform(s) === s) return s;
  }
  return null;
}


/**
 * Channel values that are NOT messaging platforms and NOT L1 auto tells, but
 * which a downstream kind-consumer still reads as load-bearing:
 *
 *   - ``inferTriggerKind`` (TurnObserver.ts) — "cron-event" / "subagent" /
 *     "exec-event" decide trigger_kind, and thence the cascade session class.
 *   - ``cost_event_converter._infer_trigger_kind`` / ``_chat_surface`` — the
 *     same set plus "webchat"/"web"/"admin"; ``_chat_surface`` maps them to
 *     "internal", which gates the defensive user_id/channel_id zeroing.
 *   - ``identity_discovery._NON_HUMAN_CHANNELS`` — dropped before
 *     primary-user discovery, so a value that escapes the set can push an
 *     internal turn into a human candidate's turn_count.
 *
 * ``isKindShapedChannel`` unions these with the platforms and the auto
 * tells. The union is the guard's real contract: the kind hint must not
 * overwrite ANY value a kind-consumer recognizes, and the L1 tells are only
 * three of the seven. Kept honest by the drift test in
 * tests/channelIdentity.test.mjs, which scans those three consumers for
 * channel literals and fails when one appears that this set does not cover.
 */
const KIND_SHAPED_CHANNEL_VALUES = new Set<string>([
  "cron-event", "exec-event", "subagent", "webchat", "web", "admin",
]);


/**
 * Is ``raw`` already usable as a channel KIND by some downstream consumer —
 * a messaging platform, an L1 auto tell, or one of the trigger-kind values
 * above? If so it must be passed through untouched.
 */
export function isKindShapedChannel(raw: string | null | undefined): boolean {
  const v = String(raw ?? "").trim().toLowerCase();
  if (!v) return false;
  return !!normalizePlatform(v) || isAutoChannelTell(v)
      || KIND_SHAPED_CHANNEL_VALUES.has(v);
}


/**
 * Resolve a channel-KIND hint for the per-turn record's ``channel`` field.
 *
 * Distinct from ``resolveSenderPlatform``: this field legitimately holds
 * non-messaging kinds ("heartbeat", "cron", "unknown"), and several
 * consumers read those as load-bearing tells —
 * ``shouldRetagHeartbeatSource`` needs "heartbeat", and ``isAutoSource``
 * treats "unknown" as the auto-activity tell (see the team_bot_a
 * 2026-05-20 drill-down in internal/incident-cost-audit-2026-05-21.md).
 *
 * So the repair here is deliberately one-directional: ``ctx.channelId``
 * is kept verbatim whenever it is ALREADY usable as a kind (it
 * normalizes to a platform — i.e. a legacy type-threading gateway), and
 * the type-shaped fields are consulted only when it is not. The
 * substituted value is always one of the four platform names, so this
 * can never overwrite or suppress an auto tell.
 *
 * Returns ``null`` when no source yields anything; the caller supplies
 * its own fallback (the accumulated prior value, then "unknown").
 */
export function resolveChannelKindHint(
  ctx: ChannelIdentitySources | null | undefined,
): string | null {
  const rawChannelId = asStr(ctx?.channelId);
  // Already usable as a kind by SOME consumer — a legacy type-threading
  // gateway ("telegram"), an L1 auto tell ("heartbeat"/"cron"/"unknown"), or
  // a trigger-kind value ("cron-event"/"subagent"/…). Authoritative; keep
  // verbatim. Checking only the L1 tells here would leave the trigger-kind
  // vocabulary unprotected — the same clobber class, one list short.
  if (isKindShapedChannel(rawChannelId)) return rawChannelId;
  // NOTHING to repair. An ABSENT channelId is not a broken kind — it is
  // the shape a cron / heartbeat / daemon-initiated turn has, and the
  // caller's own fall-through ("unknown", itself the load-bearing auto
  // tell) is the correct answer for it. Substituting a platform name off
  // ctx.channel here would suppress that tell on exactly the turns the
  // tell exists for. The repair applies ONLY to a channelId that is
  // present but unusable — i.e. an actual chat id.
  if (!rawChannelId) return null;
  const typed = normalizePlatform(asStr(ctx?.channel))
             ?? normalizePlatform(asStr(ctx?.messageProvider));
  if (typed) return typed;
  // A chat id with no typed field to swap in: hand it back exactly as
  // the pre-fix code did rather than inventing a kind.
  return rawChannelId;
}
