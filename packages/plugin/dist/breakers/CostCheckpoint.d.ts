/**
 * Cost checkpoint — the daily cap is a CHECKPOINT, not a downshift.
 *
 * Operator decision D-CC1..4
 * (internal/decision-cost-cap-checkpoint-2026-09-04.md), refining
 * docs/principle-cost-cap-refuse-turn.md.
 *
 * On 2026-09-03 a bot tripped its $20/day cap and the shipped
 * ``spendCapAction: "downgrade-tier"`` pinned every remaining turn to the
 * cheapest model — undisclosed, behind a mislabeled "selected model
 * unavailable" banner, still billing, and the cheap model then broke an app's
 * fail-closed rule with invented answers. The principle already said a tripped
 * cap REFUSES further LLM calls; downgrade was a stopgap for OpenClaw's missing
 * turn-abort hook (openclaw#92296) that became the schema default.
 *
 * **What replaces it.** Background work stops exactly as before (the L1 veto in
 * ``handleBeforeAgentRun`` is untouched). The first INTERACTIVE turn after a
 * trip is held: ``before_agent_run`` returns ``{outcome: "block", message}``
 * with a fixed message this module renders. That is a zero-spend path with a
 * real response — OpenClaw short-circuits the run before any model is resolved
 * or dispatched, and the user sees the text as the turn's outcome, not a 5xx.
 * It is the same mechanism the per-session budget breaker already uses to block
 * user turns, so it is proven on this surface.
 *
 * Why not the ``LEGACY_CONFIG_REFUSE_SENTINEL`` route in ModelRouter: an
 * unresolvable model ref does stop the spend, but the turn then dies as a
 * gateway error — which violates the principle's own "refuse-turn returns a
 * real response, not a network error" clause. ``before_agent_run`` satisfies
 * both halves at once.
 *
 * Nothing here talks to a model, and nothing here decides authorization: the
 * "continue" answer is recorded by the admin daemon, which resolves the
 * speaker's role itself (see ``cost_checkpoint_bot_routes.py``). The model can
 * neither claim to be an owner nor produce the reply text.
 *
 * READS (never writes) two files the Python side owns:
 *   {sharedDir}/breakers/<bot>/cost.json          — checkpoint state
 *   {sharedDir}/spend-caps/<bot>-<YYYY-MM-DD>.json — cap, spend, action
 */
/** Checkpoint states written by ``breakers.store`` (Python). */
export type CheckpointState = "pending" | "continued" | "declined";
/** The deterministic answers a held turn accepts. */
export type CheckpointAnswer = "continue" | "stop";
export interface CheckpointStatus {
    /** The trip's checkpoint state. */
    readonly state: CheckpointState;
    /** Base cap for today, from the enforcement flag ($). Null when unreadable. */
    readonly capUsd: number | null;
    /** Spend at the moment the cap tripped ($). Null when unreadable. */
    readonly spendUsd: number | null;
    /** What a "continue" answer would add to today's cap ($). */
    readonly incrementUsd: number | null;
    /** Cumulative increment already granted today ($), or null. */
    readonly grantedUsd: number | null;
    /** ISO-8601 auto-recovery time from the breaker record, or null. */
    readonly expiresAt: string | null;
}
/**
 * D-CC3's default grant: +50% of the cap. Mirrors
 * ``spend_caps.DEFAULT_CHECKPOINT_INCREMENT_FRACTION`` — the two are pinned
 * equal by test so the number the user is OFFERED here is the number the
 * daemon GRANTS. (The daemon recomputes it from the same flag rather than
 * trusting this value over the wire; a drift would show as a mismatch
 * between the offer and the ledger, which the pin test exists to prevent.)
 */
export declare const CHECKPOINT_INCREMENT_FRACTION = 0.5;
/**
 * Resolve this bot's cost-checkpoint status, or null when no checkpoint is in
 * force.
 *
 * Null (no hold, turn proceeds) when any of these hold — all fail-open:
 *   - no per-bot L1 cost breaker file, or it has expired
 *   - the record was tripped on an EARLIER pod-local day (see below)
 *   - the record carries no ``checkpoint`` field (a manual ``breaker trip``,
 *     or a pod whose ``spendCapAction`` is not ``checkpoint``)
 *   - the record's checkpoint is ``continued`` — an owner already said yes
 *
 * The day check is not the same clock as the TTL. The record expires 24h
 * after the trip, so a 21:18 trip is still unexpired at 21:18 tomorrow —
 * long after the spend-caps flag rolled and today's spend restarted at $0.
 * Holding a conversation on yesterday's cap (and telling the user they are
 * "paused until tomorrow" on the day that already arrived) is the D-CC3
 * grant leaking past its own stated boundary, so a stale record reads as no
 * hold and the Python side re-trips from scratch if today's spend warrants
 * it.
 *
 * Pod-wide breakers are deliberately NOT consulted: a checkpoint is a question
 * put to one bot's owner about one bot's cap, and the pod-scope trip has no
 * per-bot cap or spend to quote. The pod-wide L1 veto on background work is
 * unchanged and still reads both scopes.
 */
export declare function readCostCheckpoint(opts: {
    sharedDir: string;
    botId: string;
    now?: Date;
}): CheckpointStatus | null;
/**
 * Classify a held turn's message as an answer to the checkpoint, or null.
 *
 * Punctuation and surrounding whitespace are stripped; nothing else is
 * interpreted. Returns null for anything that is not unambiguously one of the
 * two offered answers — including an empty message.
 */
export declare function classifyCheckpointAnswer(message: string | null | undefined): CheckpointAnswer | null;
/**
 * The fixed cap-reached reply — the ONLY thing a held turn emits.
 *
 * Names the bot, the cap, today's spend, the three actions taken, and the two
 * choices. Every value is read from disk; nothing is generated. Compare the
 * copy this replaces, which said "You can still talk to me" while a fourth,
 * undisclosed action degraded every answer.
 */
export declare function renderCapReachedMessage(status: CheckpointStatus, botName: string): string;
/** The reply when someone who is not an owner says "continue". */
export declare function renderNotOwnerMessage(status: CheckpointStatus, botName: string): string;
/** The confirmation after an owner says "continue". */
export declare function renderContinuedMessage(status: CheckpointStatus, botName: string, grantedUsd: number | null): string;
/** The confirmation after "stop", and every turn after it until tomorrow. */
export declare function renderDeclinedMessage(botName: string): string;
/** The reply when the daemon could not record an answer. */
export declare function renderAnswerUnavailableMessage(): string;
//# sourceMappingURL=CostCheckpoint.d.ts.map