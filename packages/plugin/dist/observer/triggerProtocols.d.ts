/**
 * triggerProtocols — stdout protocol parsers for Layer C trigger interception.
 *
 * Phase 2.3 of the agent-freelance-bypass spec
 * (internal/spec-agent-freelance-bypass-phase2-2026-06-06.md). The TurnObserver's
 * Layer C interceptor runs an event_trigger's declared script and parses
 * the stdout per a code-registered protocol declared in the manifest's
 * ``event_triggers[].invocation.stdout_protocol`` field.
 *
 * Protocol parsers are intentionally code-registered (not config-driven):
 * a protocol is a tight contract between plugin and script, and config
 * drift between parser and emitter would be the exact silent-failure
 * mode this spec is meant to close. Adding a new protocol requires a
 * code change to this file AND the corresponding manifest schema enum
 * (docs/schemas/manifest-v7-spec.schema.json + the Python validator).
 *
 * Each parser returns ``ParsedReply``:
 *   * ``text``: what to direct-send to the channel (null → don't send;
 *               script intentionally chose silence)
 *   * ``outcome``: telemetry label, one of:
 *       "answered" | "rate_limited" | "budget_exceeded" | "refused"
 *       | "failed" | "silent"
 *
 * Parsing tolerates non-UTF8 noise, trailing whitespace, and ignores any
 * stdout before the first protocol line. If stdout is empty or no
 * protocol line is recognised, returns ``{text: null, outcome: "silent"}``
 * (the script chose not to reply). Callers can distinguish this from a
 * failure via ``exitCode``: non-zero exit + silent outcome = failure path
 * (caller posts ``fallback_text``); zero exit + silent outcome = clean
 * intentional silence (caller posts nothing).
 */
export interface ParsedReply {
    text: string | null;
    outcome: "answered" | "rate_limited" | "budget_exceeded" | "refused" | "failed" | "silent";
}
export type ProtocolName = "atlas_research" | "atlas_capture" | "raw_text";
/**
 * The registered protocol names. Mirrors the JSON-schema enum at
 * docs/schemas/manifest-v7-spec.schema.json and the Python validator's
 * ``_KNOWN_STDOUT_PROTOCOLS`` frozenset. Keep these three locations in
 * sync — a test pins the cross-language equivalence.
 */
export declare const KNOWN_PROTOCOLS: ReadonlyArray<ProtocolName>;
export declare function isKnownProtocol(name: unknown): name is ProtocolName;
/**
 * Parse stdout per protocol. Always returns a ParsedReply; never throws.
 * Caller branches on ``outcome`` + ``exitCode`` to decide:
 *   * outcome=answered/rate_limited/budget_exceeded/refused → direct-send ``text``
 *   * outcome=silent + exitCode=0 → intentional silence, don't send
 *   * outcome=silent + exitCode!=0 → script crashed, post fallback_text per on_failure
 *   * outcome=failed → ``text`` is the fallback reply per the protocol
 */
export declare function parseProtocol(protocol: ProtocolName, stdout: string): ParsedReply;
//# sourceMappingURL=triggerProtocols.d.ts.map