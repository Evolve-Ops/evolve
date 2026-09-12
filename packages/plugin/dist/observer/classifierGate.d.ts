/**
 * classifierGate — the ONE switch for every Evolve model call that runs
 * around a turn rather than as the turn.
 *
 * Decision: internal/decision-evolve-overhead-2026-09-07.md D-OH2 / D-OH5.
 *
 * Three call sites used to fire per turn or per session, each with its
 * own cache write and its own fail-OPEN gate:
 *
 *   - the preflight haiku layer  (PreflightIntentRouter._classifyWithHaiku)
 *   - the post-turn tier classifier (LLMTierClassifier, at agent_end)
 *   - the every-session struggle judge (SessionStruggleJudge)
 *
 * and a fourth, the session summariser's outcome extraction, ran at
 * session end. All four now read this one gate, and it is **off by
 * default and fails CLOSED**: an unreadable or unparseable
 * ``network.json`` means off, not on. That inversion is the point.
 * Every one of the old gates failed open, so the way to turn the
 * machinery ON was to break the config — which is precisely the state a
 * pod is in when it can least afford four extra model calls per turn.
 * D-OH5: "Every Evolve switch fails closed."
 *
 * Config shape (``{sharedDir}/network.json``):
 *
 *   cascade.classifiers.enabled                 pod-wide, default false
 *   cascade.classifiers.sample_rate             default 0.10
 *   cascade.classifiers.sample_rate_with_prior  default 0.05
 *   bots.<botId>.classifiers.enabled            per-bot override (either way)
 *   bots.<botId>.classifiers.sample_rate        per-bot override
 *
 * Sampling (D-OH4): where a judgement is still wanted, it is wanted on a
 * SAMPLE, not on every session. The sample is a deterministic hash of
 * (botId, sessionId) — the same shape ``HoldoutCohort`` uses — so a
 * session is either in or out for its whole life, restarts included, and
 * a replay of the same sessions samples the same sessions.
 */
export interface ClassifierGate {
    /** May Evolve make its own model calls around a turn at all? */
    enabled: boolean;
    /** Fraction of sessions a kept judgement runs on. */
    sampleRate: number;
    /** Whether the gate came from config or from the fail-closed default. */
    source: "config" | "default" | "unreadable";
}
/** Pod defaults. Off, and cheap if an operator turns it on. */
export declare const DEFAULT_SAMPLE_RATE = 0.1;
export declare const DEFAULT_SAMPLE_RATE_WITH_PRIOR = 0.05;
/**
 * The gate every read falls back to. ``enabled: false`` is the whole
 * point — see the module comment.
 */
export declare const CLOSED_GATE: Readonly<ClassifierGate>;
/**
 * Read the gate for one bot. Never throws.
 *
 * ``hasConfidentPrior`` selects the lower default sample rate: a bot
 * whose routing is already answered by a learned prior needs less
 * sampling to keep that prior honest than one still being characterised.
 */
export declare function readClassifierGate(sharedDir: string, botId: string, hasConfidentPrior?: boolean): ClassifierGate;
/**
 * Is this session in the sample? Deterministic and stable for the life
 * of the session. A rate of 0 samples nothing; 1 samples everything.
 */
export declare function shouldSampleSession(botId: string, sessionId: string, rate: number): boolean;
/**
 * TTL-cached reader for the hot path. One ``network.json`` read a
 * minute, not one per turn — the same cadence
 * ``TurnObserver._isPreflightEnabled`` and ``_isPushbackEnabled`` use.
 *
 * The cache is keyed on ``hasConfidentPrior`` because that input picks
 * the default sample rate; a bot that gains a prior overnight sees the
 * new rate within one TTL rather than at the next gateway restart.
 */
export declare class ClassifierGateReader {
    private readonly sharedDir;
    private readonly botId;
    private cached;
    private static readonly TTL_MS;
    constructor(sharedDir: string, botId: string);
    read(hasConfidentPrior?: boolean): ClassifierGate;
    /** Drop the cache — tests, and the config-reload path. */
    invalidate(): void;
}
//# sourceMappingURL=classifierGate.d.ts.map