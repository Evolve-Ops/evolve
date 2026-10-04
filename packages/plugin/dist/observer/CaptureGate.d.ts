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
/** Continuity engine `DEFER_PATTERNS` (verbatim) + three deadline shapes. */
export declare const DEFER_PATTERNS: readonly RegExp[];
export type Speaker = "user" | "bot";
/** Names of the patterns that fired — empty means the gate did not fire. */
export declare function gateMatches(text: string, speaker: Speaker): string[];
/** The sentence(s) around the first match, bounded — the classifier's input. */
export declare const MAX_EXCERPT_CHARS = 600;
export declare function excerptAround(text: string, patternSource: string): string;
export interface CaptureRequest {
    session: string;
    turn_id: string;
    speaker: Speaker;
    text_excerpt: string;
    patterns: string[];
}
/** Stage one for one turn: zero, one or two capture requests. Pure. */
export declare function captureRequestsForTurn(userText: string, botText: string, session: string, turnId: string): CaptureRequest[];
type Post = (body: Record<string, unknown>) => Promise<void>;
interface Logger {
    info(m: string): void;
    warn(m: string): void;
}
export declare const GATE_REPORT_INTERVAL_MS: number;
/** Counters + the fire-and-forget poster. One per gateway. */
export declare class CaptureGate {
    private readonly post;
    private readonly logger?;
    private evaluated;
    private matched;
    private lastReportMs;
    constructor(post: Post, logger?: Logger | undefined);
    /** agent_end for a user-triggered run. Never throws; never awaits the daemon's work. */
    observeTurn(userText: string, botText: string, session: string, turnId: string, now?: number): Promise<CaptureRequest[]>;
}
/** The production poster: one daemon call over the admin socket. */
export declare function socketPoster(socketPath: string): Post;
/** Wire the gate onto `agent_end` — user-triggered runs only (not heartbeat/cron/memory). */
export declare function registerCaptureGate(api: {
    on: (name: string, fn: (event: any, ctx: any) => Promise<void>, opts?: any) => void;
}, gate: CaptureGate, extract: (messages: unknown) => {
    userMessage: string;
    assistantMessage: string;
}): void;
export {};
//# sourceMappingURL=CaptureGate.d.ts.map