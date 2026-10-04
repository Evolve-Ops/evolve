/**
 * Per-run sender registry.
 *
 * Phase C.3 of internal/spec-user-roster-and-roles-2026-06-07.md. The OC SDK's
 * ``PluginHookBeforeAgentRunEvent`` carries ``senderId`` and
 * ``senderIsOwner`` fields that identify who triggered the current turn —
 * including for group messages where ``ctx.sessionKey`` only carries the
 * group's chat_id, not the sender's user_id. This module is the shared
 * stash between the before_agent_run hook (which captures the senderId)
 * and the roster mutation tools (which need it to attribute the call).
 *
 * Why a module-level Map instead of a field on TurnObserver: tools are
 * registered independently of the observer and don't have a reference to
 * it. A module-level Map keyed on ``runId`` gives both sides a
 * cooperative seam without threading observer-references through the
 * tool factory chain.
 *
 * Lifecycle:
 *   - before_agent_run fires → captureSender(runId, …)
 *   - the LLM picks a tool → tool.execute() → getSender(runId)
 *   - turn ends, runId becomes irrelevant
 *
 * The Map is bounded to MAX_ENTRIES with FIFO eviction so a long-running
 * gateway can't grow it forever. Entries also TTL out after TTL_MS so a
 * stale runId never resolves a long-ago sender as the current speaker
 * — important because runIds are not globally unique forever.
 */
/**
 * Normalize an OC channel-TYPE value (the channel *type* name like
 * "telegram"/"slack") to one of the four roster platforms, or ``null``
 * when it isn't a recognized messaging platform (e.g. "web", "unknown",
 * a raw chat id, "").
 *
 * WHERE the type comes from is util/channelIdentity.ts's job, not this
 * function's — and it is NOT reliably ``ctx.channelId``: on OC ≥2026.7
 * that field carries the chat id. Call ``resolveSenderPlatform`` rather
 * than picking a ctx field yourself.
 *
 * Prefix-matches so a surface-suffixed value ("telegram_group",
 * "slack_dm") still maps to its base platform.
 */
export declare function normalizePlatform(raw: string | null | undefined): string | null;
export interface SenderRecord {
    /** Stable id from the channel layer (Telegram user_id, Slack user_id, etc.). */
    readonly senderId: string;
    /** Channel context attached to the event when OC threads it through. */
    readonly channelId: string | null;
    /** Real messaging platform of the sender — one of telegram | slack |
     *  discord | whatsapp, normalized from the channel-type at capture
     *  time — or ``null`` when OC didn't thread a recognizable platform.
     *  Consumers key the daemon capability check + the speaker-context
     *  overlay lookup on ``(platform, senderId)``, so this must be the
     *  sender's ACTUAL platform, not a hard-coded one (audit R1a G-N2). */
    readonly platform: string | null;
    /** Whether OC's channel-side identity check flags this sender as the
     *  bot owner — informational, NOT a substitute for the daemon's
     *  capability check. */
    readonly senderIsOwner: boolean;
    /** Wall-clock ms when captured. Used for TTL eviction. */
    readonly capturedAt: number;
}
/**
 * Stash the sender for ``runId``. Called from the before_agent_run hook
 * in TurnObserver. Empty/falsy ``runId`` is a no-op — that's the
 * unusual case where OC didn't issue a runId for this turn (cron tick,
 * heartbeat, etc.) and there's no incoming user message to attribute.
 */
export declare function captureSender(runId: string | undefined | null, fields: {
    senderId?: string | null;
    channelId?: string | null;
    /** Channel-TYPE / platform hint — resolve it with
     *  channelIdentity.resolveSenderPlatform, not from a single ctx
     *  field. Normalized (and re-validated) here. */
    platform?: string | null;
    senderIsOwner?: boolean | null;
}): void;
/**
 * Retrieve the sender stashed for ``runId``. Returns null when:
 *   - runId is falsy
 *   - no record was captured for this runId
 *   - the record has TTL'd out
 *
 * Callers should treat null as "cannot identify the speaker" and refuse
 * with a clear envelope rather than fall back to a less-trusted source.
 */
export declare function getSender(runId: string | undefined | null): SenderRecord | null;
/** Test helper — clear the registry. Tests in different files share
 *  module state; this prevents bleed-over without exposing the internals. */
export declare function _resetForTests(): void;
//# sourceMappingURL=senderRegistry.d.ts.map