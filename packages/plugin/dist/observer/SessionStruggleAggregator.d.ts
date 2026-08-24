/**
 * SessionStruggleAggregator
 *
 * Cross-turn struggle signals — the patterns that show up only when you
 * look at a session as a unit, not at each turn in isolation. The
 * sibling-piece to StruggleDetector: that one is pure-function per-turn,
 * this one tracks state across turns.
 *
 * Motivation (2026-06-07 live-pod data): two real multi-turn sessions
 * on one bot exhibited clear conversational struggle that the turn-
 * level detectors couldn't see — a six-turn file-copy session where
 * the bot self-corrected three times in a row ("the --strip-components
 * count was off"), and a bike-repair session where the user said
 * "sounds like you are guessing." Per-turn pattern detection can't
 * catch these without lucky regex hits on highly-variable phrasing.
 * The PATTERN across turns is the real tell:
 *
 *   - User pastes shell-error output three turns running
 *   - Bot says "X was off / let's redo / actually that's not right" repeatedly
 *   - Turn velocity rises above conversation cadence (tight loop)
 *
 * Three features, all rolling-window aggregates:
 *
 *   1. shell_error_paste_count
 *        Count of recent user messages whose text matches BOTH a
 *        shell-prompt pattern AND an error keyword. Structural, not
 *        semantic — distinctive paste-of-output shape that's hard to
 *        false-positive on. Catches "user just ran your command and
 *        it failed" without needing them to SAY it failed.
 *
 *   2. bot_self_correction_count
 *        Count of recent bot messages with self-correcting language —
 *        BROADER than the per-turn restart_markers patterns. Catches
 *        "X was off", "let's redo", "actually that's not right" — the
 *        natural phrases bots use when they realize their last answer
 *        was wrong. A single self-correction is noise; three in a row
 *        is unambiguous signal.
 *
 *   3. turn_velocity_per_min
 *        (N-1) / minutes elapsed across the last N turns. Healthy
 *        multi-turn sessions have natural pauses (user reads, thinks,
 *        types). A sustained turn-every-60-seconds rhythm with rich
 *        content on both sides is "tight back-and-forth loop" — the
 *        shape of unproductive struggle.
 *
 * Per-session state is keyed by sessionId (matches TurnObserver's
 * sessionTurns / sessionLlmData conventions). Cleared on session end
 * via clearSession().
 *
 * Spec: internal/spec-session-struggle-aggregator-2026-06-07.md (to be written).
 */
import type { PluginLogger } from "openclaw/plugin-sdk/types";
/**
 * Check whether a single user message looks like a shell-error paste.
 * Both a prompt indicator AND an error keyword required.
 *
 * Pure function — exported for tests.
 */
export declare function isShellErrorPaste(text: string): boolean;
/**
 * Count self-correction pattern hits in a single bot message. A
 * message with 2+ patterns hitting still only counts once — the
 * point is "this message is a self-correction," not "how many
 * apology words it contained."
 *
 * Pure function — exported for tests.
 */
export declare function isBotSelfCorrection(text: string): boolean;
/**
 * The signal produced by the aggregator at the end of each turn.
 *
 * All counts are rolling — looking back at the last N turns of THIS
 * session. `turn_count` is the sample size (so consumers can gate on
 * "did we observe enough turns to draw conclusions").
 *
 * `turn_velocity_per_min` is `null` when there's fewer than 2 turns
 * to compare timestamps across. Consumers must distinguish null
 * (couldn't measure) from 0 (turns spaced infinitely apart) per the
 * Tri-State Status principle.
 */
export interface SessionStruggleSignal {
    shell_error_paste_count: number;
    bot_self_correction_count: number;
    turn_velocity_per_min: number | null;
    turn_count: number;
}
export declare class SessionStruggleAggregator {
    private readonly logger;
    private readonly sessions;
    constructor(logger: PluginLogger);
    /**
     * Observe a turn's worth of conversation. Call at agent_end with the
     * just-completed turn's user message + assistant reply + timestamp.
     *
     * Bounded — only the last MAX_HISTORY observations per session are
     * kept. Long-running sessions don't grow the map unboundedly.
     *
     * Pure side-effects on the per-session state; never throws.
     */
    observeTurn(sessionId: string, userText: string | null | undefined, assistantText: string | null | undefined, endedAt: Date): void;
    /**
     * Compute the aggregate signal for a session. Returns the empty
     * signal (all zeros / null velocity) when the session has no
     * observations yet — distinguishable from "I observed turns but
     * none triggered" via the `turn_count` field.
     *
     * Pure function — no state mutation. Safe to call multiple times
     * within a turn (idempotent).
     */
    getSessionSignal(sessionId: string): SessionStruggleSignal;
    /** Clean up a session's state. Call on session_end. */
    clearSession(sessionId: string): void;
    /** Diagnostic: how many sessions are being tracked. Exposed for tests. */
    _sessionCountForTest(): number;
}
/**
 * Threshold check: is the aggregate elevated enough to count as
 * "session-level struggle"? Used by the cascade controller to decide
 * whether to skip the per-turn persistence requirement and treat
 * THIS turn's signal as if struggle had already been sustained.
 *
 * Logic: ANY of the three thresholds tripped → struggle. Each
 * threshold is set conservatively (high enough to avoid false-
 * positiving on healthy power-user sessions, low enough to fire
 * on the file-copy case which had 3 self-corrections + 3 shell
 * pastes in 6 minutes).
 *
 * Exported as a constant + function so tests can pin both.
 */
export declare const SESSION_STRUGGLE_THRESHOLDS: Readonly<{
    shell_error_paste_count: 3;
    bot_self_correction_count: 2;
    turn_velocity_per_min: 0.8;
    turn_velocity_min_turn_count: 4;
}>;
/**
 * Returns true when the aggregate signal indicates session-level
 * struggle. Cascade controller treats this as equivalent to having
 * crossed tier2_struggle_threshold with persistence already met.
 */
export declare function isSessionStruggleElevated(signal: SessionStruggleSignal): boolean;
//# sourceMappingURL=SessionStruggleAggregator.d.ts.map