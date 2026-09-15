/**
 * HeartbeatDueCheck — decide "is anything due" WITHOUT calling a model.
 *
 * Decision: internal/decision-evolve-overhead-2026-09-07.md D-OH3 ("a
 * heartbeat or cron with nothing due never calls a model"), under D-OH1's
 * standing rule that every Evolve model call must name the free signal it
 * could have used instead. Two days of turns across the five busiest bots
 * (§2) cost $3.02 for 125 heartbeat/cron turns whose usual answer was
 * ``NO_REPLY`` — each one dragging tens of thousands of cached tokens into a
 * call that decided nothing. A heartbeat's objective is "if something is due,
 * do it"; the *if* is a stat() call, not an LLM.
 *
 * ## Where this runs
 *
 * ``TurnObserver``'s ``before_agent_reply`` hook. Verified against the live
 * OC 2026.9.2 bundle on the reference pod: ``runEmbeddedAgent`` awaits
 * ``runBeforeAgentReplyForTurn`` **before** ``executePreparedEmbeddedRun``
 * and, on a truthy ``handled``, returns
 * ``buildHandledBeforeAgentReplyPayloads(reply)`` — i.e. ``NO_REPLY`` — so
 * the model turn is never dispatched. The hook is eligible for exactly the
 * three triggers ``cron`` / ``heartbeat`` / ``user``
 * (``pluginHookAgentTriggerSet``), which is why the trigger gate below can be
 * a strict allowlist rather than a heuristic.
 *
 * ## The conditions file
 *
 * ``HEARTBEAT.json``, beside the bot's ``HEARTBEAT.md`` in its workspace —
 * the machine-readable half of the checklist the model would otherwise read
 * and evaluate in prose. Authored once per bot.
 *
 * ```json
 * {
 *   "version": 1,
 *   "enabled": true,
 *   "conditions": [
 *     {"id": "inbox",  "when": "dir_non_empty", "path": "inbox",
 *      "wake": "Process the queued items in inbox/."},
 *     {"id": "brief",  "when": "time_window", "after": "06:30", "before": "07:30",
 *      "wake": "Deliver the morning brief."},
 *     {"id": "notes",  "when": "file_changed", "path": "memory/notes.md",
 *      "wake": "Re-read memory/notes.md and act on what changed."},
 *     {"id": "sweep",  "when": "every", "interval": "6h",
 *      "wake": "Run the six-hourly sweep."}
 *   ],
 *   "cron": {
 *     "9506f538-340e-4487-ae07-5675cb58b48c": [
 *       {"id": "backup-source", "when": "dir_non_empty", "path": "to-back-up",
 *        "wake": "Run the workspace backup."}
 *     ]
 *   }
 * }
 * ```
 *
 * ## ``conditions`` is the HEARTBEAT scope. Cron opts in per job.
 *
 * A cron job is not a heartbeat: it carries its own instruction ("post the
 * weekly digest", "check the certificate") and merely shares the bot's
 * workspace. Gating it on the heartbeat's conditions would silently kill it
 * — the exact failure the fail-open contract exists to prevent. So the
 * top-level ``conditions`` list applies to the ``heartbeat`` trigger ONLY,
 * and a ``cron`` trigger is claimable only when the top-level ``cron`` map
 * has an entry for THAT job. The key is OC's ``ctx.jobId`` (the cron store's
 * job id), which the 2026.9.2 ``before_agent_reply`` hook context carries
 * alongside ``trigger`` and ``workspaceDir``. A cron job with no entry
 * evaluates to ``unavailable`` — the model runs, exactly as today.
 *
 * Each scope keeps its own state namespace (``cron:<jobId>:<id>``), so a
 * cron wake can never mark a heartbeat condition served.
 *
 * ## The floor the bot cannot lower
 *
 * ``HEARTBEAT.json`` lives in the bot's own workspace, so the bot's model can
 * write the file that decides whether it ever wakes — a condition pointing at
 * a path that never appears is valid, evaluates to "nothing due" forever, and
 * every tick looks like a clean skip. The floor is operator-side and read from
 * ``{sharedDir}/network.json`` only (``heartbeat.max_silence`` pod-wide,
 * ``bots.<id>.heartbeat.max_silence`` per bot, default 24h): when the state
 * file records no wake inside that window, the next decision is a wake with
 * ``reason: "floor: no wake in <N>h"`` whatever the conditions say. Every
 * decision record also carries the file's ``conditions_sha256``, and a change
 * between ticks warns once per process — so a rewrite is visible in the
 * ledger rather than inferred from an absence of turns.
 *
 * ## Fail-open, deliberately
 *
 * This is the one place in Evolve where a switch fails toward DOING the work
 * (contrast D-OH5's "every Evolve switch fails closed"): the alternative to a
 * spurious model call is a silently dead heartbeat, and a heartbeat that
 * stops firing is invisible until something it was watching goes wrong. So a
 * missing file, unreadable file, bad JSON, unknown condition kind, or a path
 * that escapes the workspace all return ``unavailable`` — the model runs,
 * exactly as it does today. Only a file that parses cleanly AND evaluates to
 * "nothing due" can suppress a turn.
 *
 * ## What state it keeps
 *
 * ``{sharedDir}/{botId}/turns/heartbeat-due-state.json`` — per-condition
 * ``lastFiredAt`` / ``lastMtimeMs``, written ONLY when the model is actually
 * woken (a skip must never consume a trigger). ``turns/`` rather than a new
 * per-bot leaf on purpose: it is already bot-writable (1777) with an
 * inheritable evolve-read ACE, so this ships without a new
 * ``BOT_SHARED_SUBDIRS`` entry, a new sudoers grant, and the deploy-perms
 * drift surface all three imply. The ``heartbeat-`` filename prefix keeps it
 * clear of ``turns-<date>.jsonl``, which is the only glob ``load_turns``
 * reads.
 */
/** The trigger kinds a due-check may ever suppress. */
export declare const DUE_CHECK_TRIGGERS: ReadonlyArray<string>;
/** Filename of the per-bot conditions file, beside HEARTBEAT.md. */
export declare const CONDITIONS_FILENAME = "HEARTBEAT.json";
/** Recognised ``when`` kinds. An unknown kind fails the whole file open. */
export declare const CONDITION_KINDS: ReadonlyArray<string>;
export interface DueCondition {
    id: string;
    when: string;
    /** Workspace-relative path. Required for the path-shaped kinds. */
    path?: string;
    /** ``time_window``: local ``HH:MM`` bounds, inclusive start, exclusive end. */
    after?: string;
    before?: string;
    /** ``every``: interval like ``30m`` / ``6h`` / ``2d``. */
    interval?: string;
    /** What to tell the model when this condition is what woke it. */
    wake: string;
    /**
     * Key this condition's per-condition state is filed under. Namespaced by
     * scope (``<id>`` for heartbeat, ``cron:<jobId>:<id>`` for a cron job), so
     * a cron wake structurally cannot mark a heartbeat condition served.
     * ``parseDueConditions`` always sets it; a hand-built condition without one
     * falls back to its ``id``.
     */
    stateKey?: string;
}
export interface ParsedConditions {
    enabled: boolean;
    /** The ``heartbeat`` scope. NEVER evaluated for a cron trigger. */
    conditions: DueCondition[];
    /** Per-cron-job scopes, keyed by OC's ``ctx.jobId``. */
    cron: Record<string, DueCondition[]>;
}
/** One condition that fired, ready to become the model's instruction. */
export interface DueItem {
    id: string;
    wake: string;
    /** The observable that fired, in operator words ("inbox/ has 3 entries"). */
    evidence: string;
}
export interface PerConditionState {
    lastFiredAt?: string;
    lastMtimeMs?: number;
}
export interface DueState {
    version: number;
    conditions: Record<string, PerConditionState>;
    /**
     * When the model was last woken by this check, in any scope. The
     * max-silence floor is measured against it — see the module docstring.
     */
    lastWokeAt?: string;
}
/** Which scope a decision was made in. ``jobId`` is null for the heartbeat. */
export interface DueScope {
    trigger: string;
    jobId?: string | null;
}
export type DueVerdict = {
    decision: "skip";
    due: [];
    reason: string;
    conditionsEvaluated: number;
    /** sha256 of the conditions file the decision was made from. */
    conditionsSha256: string;
    /** OC cron job id when the decision was made in a cron scope. */
    cronJobId: string | null;
} | {
    decision: "wake";
    due: DueItem[];
    reason: string;
    conditionsEvaluated: number;
    /** State to commit once the wake is real. */
    nextState: DueState;
    conditionsSha256: string;
    cronJobId: string | null;
} | {
    decision: "unavailable";
    due: [];
    reason: string;
    /** Set when an operator should hear about it (bad file, not absent one). */
    warn: string | null;
};
/** ``30m`` / ``6h`` / ``2d`` / bare minutes → ms. Null when unparseable. */
export declare function parseInterval(raw: unknown): number | null;
/** ``HH:MM`` → minutes since local midnight. Null when unparseable. */
export declare function parseClock(raw: unknown): number | null;
/** State-key prefix for one cron job's scope. */
export declare function cronStateKeyPrefix(jobId: string): string;
/**
 * Parse + validate a ``HEARTBEAT.json`` body.
 *
 * Returns ``{error}`` for anything an operator got wrong — a caller turns
 * that into ``unavailable`` (model runs) plus one warning. Validation is
 * strict on purpose: a typo'd condition kind that silently evaluated to
 * "not due" would be a heartbeat that stopped firing without saying so.
 *
 * ``conditions`` is the heartbeat scope. ``cron`` is a map of OC cron job id
 * → that job's own conditions; a cron job absent from the map is never
 * claimed (see the module docstring).
 */
export declare function parseDueConditions(raw: string): {
    ok: ParsedConditions;
} | {
    error: string;
};
/**
 * Why ``rel`` may not be used as a workspace-relative path, or null when it
 * is fine. The conditions file is bot-authored content that decides whether
 * a turn runs; it must not be able to point the stat() at another user's
 * home. Absolute paths and ``..`` are both refused outright rather than
 * normalised — an operator who meant a path outside the workspace should
 * hear about it, not get a silently rewritten one.
 */
export declare function unsafeRelativePath(rel: string): string | null;
/** Filesystem facts one condition needs. Injectable so tests stay hermetic. */
export interface FsProbe {
    exists(abs: string): boolean;
    isDirectory(abs: string): boolean;
    /** Entries excluding dotfiles; empty when not a readable directory. */
    entryCount(abs: string): number;
    /** mtime in ms, or null when the path is absent/unreadable. */
    mtimeMs(abs: string): number | null;
}
export declare const realFsProbe: FsProbe;
/**
 * Evaluate every condition against the filesystem and the clock.
 *
 * Pure apart from ``probe``: same inputs, same verdict. Every "unknown"
 * resolves toward DUE — a ``file_changed`` we have never seen before, an
 * ``every`` that has never fired — because the first run after an operator
 * writes the file must wake the model, not swallow the trigger.
 */
export declare function evaluateConditions(conditions: DueCondition[], workspaceDir: string, state: DueState, now: Date, probe?: FsProbe): {
    due: DueItem[];
    nextState: DueState;
};
/**
 * The user-visible instruction for a woken turn: the due items, and nothing
 * else. The point of naming them is that the model's job narrows from "read
 * HEARTBEAT.md and work out what applies" to "do these two things" — which
 * is the context reduction, not just a nicety.
 */
export declare function buildWakeContext(due: DueItem[]): string;
/** Default maximum silence before a wake is forced regardless of conditions. */
export declare const DEFAULT_MAX_SILENCE_MS: number;
/** ``86400000`` → ``"24h"``. Used verbatim in the floor's reason string. */
export declare function formatSilenceWindow(ms: number): string;
/**
 * The max-silence floor for this bot, in ms. 0 disables it.
 *
 * Read from ``{sharedDir}/network.json`` ONLY — never from the workspace.
 * The whole point is a bound the bot's own model cannot lower by rewriting
 * the conditions file it also authors: ``heartbeat.max_silence`` pod-wide,
 * ``bots.<botId>.heartbeat.max_silence`` per bot, both in the same interval
 * syntax as an ``every`` condition (``"24h"``, ``"90m"``, or bare minutes).
 * An explicit ``0`` / ``false`` turns the floor off — an operator decision,
 * made in an operator-owned file. Anything unreadable or unparseable falls
 * back to the 24h default, which is the fail-toward-running direction.
 */
export declare function readMaxSilenceMs(sharedDir: string, botId: string): number;
export interface HeartbeatDueCheckOptions {
    botId: string;
    sharedDir: string;
    logger?: {
        info: (m: string) => void;
        warn: (m: string) => void;
        debug?: (m: string) => void;
    };
    probe?: FsProbe;
    now?: () => Date;
    /**
     * Test seam for the operator-side floor. Production leaves it unset and
     * the value comes from network.json via ``readMaxSilenceMs``, TTL-cached.
     */
    maxSilenceMs?: () => number;
}
export declare class HeartbeatDueCheck {
    private readonly botId;
    private readonly sharedDir;
    private readonly logger;
    private readonly probe;
    private readonly now;
    /** Config paths already warned about, so a bad file logs once per process. */
    private readonly warned;
    /** Last observed conditions-file digest, per config path. */
    private readonly lastSha;
    /** Config paths whose digest change has already been reported. */
    private readonly shaChangeWarned;
    private readonly maxSilenceOverride?;
    private cachedMaxSilenceMs;
    private maxSilenceCheckedAt;
    private static readonly MAX_SILENCE_CACHE_TTL_MS;
    constructor(opts: HeartbeatDueCheckOptions);
    /** The floor, TTL-cached so the hook path doesn't re-read network.json. */
    maxSilenceMs(): number;
    /** ``{sharedDir}/{botId}/turns`` — see the module docstring on placement. */
    private turnsDir;
    statePath(): string;
    conditionsPath(workspaceDir: string): string;
    readState(): DueState;
    /** Commit ``nextState``. Called only on a real wake. Best-effort. */
    commitState(next: DueState): void;
    /**
     * The decision. ``unavailable`` means "run the model, exactly as today".
     *
     * ``scope`` selects which conditions apply. A ``heartbeat`` trigger reads
     * the top-level ``conditions``; a ``cron`` trigger reads ONLY
     * ``cron[<ctx.jobId>]`` and is ``unavailable`` when that job has no entry,
     * so an ordinary cron job on a bot with a HEARTBEAT.json keeps running.
     */
    evaluate(workspaceDir: string | null | undefined, scope?: DueScope): DueVerdict;
    /**
     * Record the conditions file's digest and warn ONCE per process when it
     * changes between ticks. The file is bot-writable; a rewrite that quiets
     * the bot would otherwise look identical to a quiet week.
     */
    private noteConditionsDigest;
    /** Log an ``unavailable`` warning at most once per config path per process. */
    warnOnce(warn: string | null, key: string): void;
}
//# sourceMappingURL=HeartbeatDueCheck.d.ts.map