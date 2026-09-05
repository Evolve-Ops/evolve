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
// ── Patterns ─────────────────────────────────────────────────────────────────
/**
 * Shell-prompt + error patterns. User message qualifies as a shell-
 * error-paste when BOTH conditions hold: it contains a recognizable
 * shell-prompt line, AND it contains an error keyword. Both required
 * so casual mentions ("I keep getting permission denied") don't trip
 * — those don't have the prompt structure.
 */
const SHELL_PROMPT_PATTERNS = [
    // Generic "user@host:path$" shell prompt
    /(^|\n)[\w@.\-]+:[~/][^\n]*\$\s/,
    // Just a "$ " or "% " or "# " at line start with following content
    /(^|\n)[$%#]\s+\w/,
];
const SHELL_ERROR_KEYWORDS = new RegExp(
// Order matters slightly — more specific patterns first to fail fast
// on the common case "no error." Union of substrings that almost
// never appear in normal conversational text.
"(" +
    "Permission denied" +
    "|No such file or directory" +
    "|command not found" +
    "|cannot (access|open|stat|create|find)" +
    "|fatal: " +
    "|error: " +
    "|failed to " +
    "|tar: " +
    "|scp: " +
    "|ssh: " +
    ")", "i");
/**
 * Check whether a single user message looks like a shell-error paste.
 * Both a prompt indicator AND an error keyword required.
 *
 * Pure function — exported for tests.
 */
export function isShellErrorPaste(text) {
    if (!text || text.length < 8)
        return false;
    const hasPrompt = SHELL_PROMPT_PATTERNS.some((rx) => rx.test(text));
    if (!hasPrompt)
        return false;
    return SHELL_ERROR_KEYWORDS.test(text);
}
/**
 * Bot self-correction language. Broader than the StruggleDetector's
 * restart_markers — those patterns require specific verbs ("let me try
 * a different approach"). Real bots correct themselves with much more
 * varied language; these patterns cover the family:
 *
 *   - "the X was off / wrong / incorrect / missing"
 *   - "that was wrong / I was wrong / I was incorrect"
 *   - "actually let me / oh wait / sorry,"
 *   - "let's redo / try again / try differently / verify"
 *
 * Calibrated on real bot text from 2026-06-07:
 *   - "The tilde isn't expanding over scp sometimes. Try with the full path"
 *   - "The --strip-components count was off"
 *   - "Permission issue — the admin user can't read the other user's files. Try with sudo"
 *   - "Let's verify what actually got created"
 */
const BOT_SELF_CORRECTION_PATTERNS = [
    // "X was/is off/wrong/incorrect" — broader noun-verb-modifier
    /\b(the|that|my|count|number|path|command|syntax|approach)\s+\w*\s*(was|is)\s+(off|wrong|incorrect|missing|too\s+\w+)\b/i,
    // "I was wrong" / "I was guessing" / "I genuinely don't know"
    /\bI (was|am|'?ve been)\s+(wrong|incorrect|guessing|making|off)\b/i,
    /\bI (genuinely|honestly|really)\s+don'?t\s+know\b/i,
    // "let me/let's redo|try (again|differently)|verify"
    /\blet'?s (redo|try (it )?(again|differently)|verify|check)\b/i,
    /\blet me (try (again|something else|a different|differently)|verify|check|reconsider)\b/i,
    // "actually" / "oh wait" / "sorry" prefixed corrections
    /\b(actually|oh wait|sorry,?)\s+(let me|I|that|the)\b/i,
    // "to be straight/honest with you" — admission of prior wrong
    /\bto be (straight|honest)( with you)?\b/i,
    // Existing restart-marker patterns also count
    /\b(a different|another|new) approach\b/i,
    /\bthat (didn'?t|did not) work\b/i,
];
/**
 * Count self-correction pattern hits in a single bot message. A
 * message with 2+ patterns hitting still only counts once — the
 * point is "this message is a self-correction," not "how many
 * apology words it contained."
 *
 * Pure function — exported for tests.
 */
export function isBotSelfCorrection(text) {
    if (!text || text.length < 5)
        return false;
    for (const rx of BOT_SELF_CORRECTION_PATTERNS) {
        if (rx.test(text))
            return true;
    }
    return false;
}
const MAX_HISTORY = 10; // bound — only the last 10 turns of a session count
const EMPTY_SIGNAL = Object.freeze({
    shell_error_paste_count: 0,
    bot_self_correction_count: 0,
    turn_velocity_per_min: null,
    turn_count: 0,
});
// ── Aggregator class ────────────────────────────────────────────────────────
export class SessionStruggleAggregator {
    logger;
    sessions;
    constructor(logger) {
        this.logger = logger;
        this.sessions = new Map();
    }
    /**
     * Observe a turn's worth of conversation. Call at agent_end with the
     * just-completed turn's user message + assistant reply + timestamp.
     *
     * Bounded — only the last MAX_HISTORY observations per session are
     * kept. Long-running sessions don't grow the map unboundedly.
     *
     * Pure side-effects on the per-session state; never throws.
     */
    observeTurn(sessionId, userText, assistantText, endedAt) {
        if (!sessionId || sessionId === "unknown")
            return;
        let state = this.sessions.get(sessionId);
        if (!state) {
            state = { history: [] };
            this.sessions.set(sessionId, state);
        }
        let isShellPaste = false;
        let isSelfCorrection = false;
        try {
            isShellPaste = isShellErrorPaste(userText ?? "");
        }
        catch (err) {
            this.logger.debug(`SessionStruggleAggregator: paste detector failed: ${err}`);
        }
        try {
            isSelfCorrection = isBotSelfCorrection(assistantText ?? "");
        }
        catch (err) {
            this.logger.debug(`SessionStruggleAggregator: self-correction detector failed: ${err}`);
        }
        state.history.push({
            endedAt: endedAt.getTime(),
            userIsShellErrorPaste: isShellPaste,
            botIsSelfCorrection: isSelfCorrection,
        });
        if (state.history.length > MAX_HISTORY) {
            state.history.shift();
        }
    }
    /**
     * Compute the aggregate signal for a session. Returns the empty
     * signal (all zeros / null velocity) when the session has no
     * observations yet — distinguishable from "I observed turns but
     * none triggered" via the `turn_count` field.
     *
     * Pure function — no state mutation. Safe to call multiple times
     * within a turn (idempotent).
     */
    getSessionSignal(sessionId) {
        const state = this.sessions.get(sessionId);
        if (!state || state.history.length === 0) {
            return { ...EMPTY_SIGNAL };
        }
        const h = state.history;
        let shell = 0;
        let correction = 0;
        for (const entry of h) {
            if (entry.userIsShellErrorPaste)
                shell += 1;
            if (entry.botIsSelfCorrection)
                correction += 1;
        }
        // Velocity = (N - 1) turns per minute, computed over the wall-clock
        // span of the rolling window. Requires >= 2 observations.
        let velocity = null;
        if (h.length >= 2) {
            const first = h[0].endedAt;
            const last = h[h.length - 1].endedAt;
            const elapsedMs = last - first;
            if (elapsedMs > 0) {
                velocity = ((h.length - 1) / elapsedMs) * 60_000;
            }
            else {
                // Same-millisecond timestamps (shouldn't happen in production
                // but defensible). Return a very high number rather than divide
                // by zero — captures the "tight loop" intent.
                velocity = 60;
            }
        }
        return {
            shell_error_paste_count: shell,
            bot_self_correction_count: correction,
            turn_velocity_per_min: velocity,
            turn_count: h.length,
        };
    }
    /** Clean up a session's state. Call on session_end. */
    clearSession(sessionId) {
        this.sessions.delete(sessionId);
    }
    /** Diagnostic: how many sessions are being tracked. Exposed for tests. */
    _sessionCountForTest() {
        return this.sessions.size;
    }
}
// ── Threshold helper for cascade controller ─────────────────────────────────
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
export const SESSION_STRUGGLE_THRESHOLDS = Object.freeze({
    shell_error_paste_count: 3,
    bot_self_correction_count: 2,
    turn_velocity_per_min: 0.8,
    // Velocity only counts when there's enough sample to be confident
    // about cadence (a 2-turn session has trivial 0.8/min calculation
    // but doesn't actually indicate a struggle).
    turn_velocity_min_turn_count: 4,
});
/**
 * Returns true when the aggregate signal indicates session-level
 * struggle. Cascade controller treats this as equivalent to having
 * crossed tier2_struggle_threshold with persistence already met.
 */
export function isSessionStruggleElevated(signal) {
    if (signal.shell_error_paste_count >= SESSION_STRUGGLE_THRESHOLDS.shell_error_paste_count) {
        return true;
    }
    if (signal.bot_self_correction_count >= SESSION_STRUGGLE_THRESHOLDS.bot_self_correction_count) {
        return true;
    }
    if (signal.turn_velocity_per_min !== null
        && signal.turn_velocity_per_min > SESSION_STRUGGLE_THRESHOLDS.turn_velocity_per_min
        && signal.turn_count >= SESSION_STRUGGLE_THRESHOLDS.turn_velocity_min_turn_count) {
        return true;
    }
    return false;
}
//# sourceMappingURL=SessionStruggleAggregator.js.map