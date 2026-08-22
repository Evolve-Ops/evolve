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
export declare const MAX_LABEL_TOKENS = 6;
/** Below this a "label" is too thin to mean anything ("do it", "thanks")
 *  and would collide with unrelated requests. Fail closed. */
export declare const MIN_LABEL_TOKENS = 2;
/** Cap on app tags appended to a label, sorted for determinism. */
export declare const MAX_LABEL_TAGS = 4;
/** Only this much of the request is inspected — a recurring ask is
 *  identified by how it opens, and an unbounded scan would let a long
 *  paste dominate the token budget. */
export declare const MAX_SCAN_CHARS = 400;
/** Text openers that mark a machine-originated turn. OC threads cron and
 *  heartbeat wake-ups through the same user-message slot as a person, so
 *  the text gate backs up the sender gate. Measured: 62 of 215 captured
 *  "user" turns on the live pod (2026-08-18) opened with `[cron:`. */
export declare const SYNTHETIC_PREFIXES: string[];
/** Sentinel outcomes/messages that are protocol, not a request. */
export declare const SENTINEL_TEXTS: Set<string>;
/** Tokens whose VALUE changes every time the same request is made.
 *  Keeping any of them would give the same weekly ask a different label
 *  each day and recurrence would never accumulate — this list is the
 *  difference between a detector that works and one that never fires. */
export declare const VOLATILE_TOKENS: Set<string>;
/** Politeness, framing and filler. Dropping them makes "can you please
 *  give me the morning summary" and "morning summary" the same label. */
export declare const STOPWORDS: Set<string>;
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
export declare function normalizeRequestLabel(text: unknown): string | null;
/**
 * Compose the full label: the normalized request head, plus the session's
 * application tags when there are any — design §7.1a's example label
 * ("morning summary: email + calendar + weather") has exactly this shape.
 *
 * Tags are sorted and capped so the label is order-independent and bounded.
 */
export declare const MAX_TAG_CHARS = 32;
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
export declare function sanitizeTag(raw: unknown): string | null;
export declare function composeLabel(head: string, appTags?: readonly string[]): string;
/**
 * Pod-local hour (0-23) of `at`. Storage stays UTC everywhere else; the
 * hour is local because "asks every morning" is a local-time statement —
 * a UTC hour would smear a 9am ask across two buckets on DST boundaries.
 */
export declare function localHour(at: Date, timezone: string): number;
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
export declare function isRecurringRequestEnabled(sharedDir: string, botId: string, nowMs?: number): boolean;
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
export declare function isRequesterExcluded(sharedDir: string, botId: string, platform: string, senderId: string, nowMs?: number): boolean;
/** Test seam — clears the 60s config caches. Production never needs this. */
export declare function _resetRecurringRequestCaches(): void;
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
export declare function buildRecurringRequest(input: BuildRecurringRequestInput): RecurringRequest | null;
/**
 * The ask of a session is its first user message — so return the whole
 * TURN, not just its text. The caller needs the requester from the SAME
 * turn: keying the first turn's ask under the last turn's sender both
 * misattributes the request and checks the per-identity do-not-track gate
 * against the wrong person.
 *
 * Returns `null` when the session has no user message at all.
 */
export declare function firstUserTurn<T extends {
    userMessage?: unknown;
}>(turns: ReadonlyArray<T>): T | null;
//# sourceMappingURL=recurringRequest.d.ts.map