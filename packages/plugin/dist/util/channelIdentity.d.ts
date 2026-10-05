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
export declare function resolveSenderPlatform(ctx: ChannelIdentitySources | null | undefined, event?: ChannelIdentitySources | null): string | null;
/**
 * Last-resort platform read off a session key's colon-delimited segments.
 * Exported for testing; prefer ``resolveSenderPlatform``.
 */
export declare function platformFromSessionKey(sessionKey: string): string | null;
/**
 * Is ``raw`` already usable as a channel KIND by some downstream consumer —
 * a messaging platform, an L1 auto tell, or one of the trigger-kind values
 * above? If so it must be passed through untouched.
 */
export declare function isKindShapedChannel(raw: string | null | undefined): boolean;
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
export declare function resolveChannelKindHint(ctx: ChannelIdentitySources | null | undefined): string | null;
//# sourceMappingURL=channelIdentity.d.ts.map