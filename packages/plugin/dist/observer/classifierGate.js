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
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
/** Pod defaults. Off, and cheap if an operator turns it on. */
export const DEFAULT_SAMPLE_RATE = 0.1;
export const DEFAULT_SAMPLE_RATE_WITH_PRIOR = 0.05;
/**
 * The gate every read falls back to. ``enabled: false`` is the whole
 * point — see the module comment.
 */
export const CLOSED_GATE = Object.freeze({
    enabled: false,
    sampleRate: DEFAULT_SAMPLE_RATE,
    source: "unreadable",
});
function clampRate(raw, fallback) {
    if (typeof raw !== "number" || !Number.isFinite(raw))
        return fallback;
    if (raw <= 0)
        return 0;
    if (raw >= 1)
        return 1;
    return raw;
}
/**
 * Read the gate for one bot. Never throws.
 *
 * ``hasConfidentPrior`` selects the lower default sample rate: a bot
 * whose routing is already answered by a learned prior needs less
 * sampling to keep that prior honest than one still being characterised.
 */
export function readClassifierGate(sharedDir, botId, hasConfidentPrior = false) {
    const defaultRate = hasConfidentPrior
        ? DEFAULT_SAMPLE_RATE_WITH_PRIOR
        : DEFAULT_SAMPLE_RATE;
    let network;
    try {
        const raw = fs.readFileSync(path.join(sharedDir, "network.json"), "utf8");
        network = JSON.parse(raw);
    }
    catch {
        // Fail CLOSED. No log: on a pod without network.json this is the
        // normal case and the state it produces (no Evolve model calls) is
        // the state we want anyway.
        return { ...CLOSED_GATE, sampleRate: defaultRate };
    }
    try {
        const pod = network?.cascade?.classifiers ?? {};
        const bot = network?.bots?.[botId]?.classifiers ?? {};
        // Pod-level default is OFF; either layer may turn it on, and the
        // per-bot value wins over the pod value in both directions.
        let enabled = pod?.enabled === true;
        if (bot?.enabled === true)
            enabled = true;
        else if (bot?.enabled === false)
            enabled = false;
        const sampleRate = clampRate(bot?.sample_rate, clampRate(hasConfidentPrior ? pod?.sample_rate_with_prior : pod?.sample_rate, defaultRate));
        return { enabled, sampleRate, source: "config" };
    }
    catch {
        return { ...CLOSED_GATE, sampleRate: defaultRate };
    }
}
/**
 * Stable [0, 1) score for a (botId, sessionId) pair. Same construction
 * as ``HoldoutCohort.hashScore`` — SHA-256, top 53 bits, normalised.
 */
function hashScore(botId, sessionId) {
    const h = crypto.createHash("sha256");
    h.update(botId);
    h.update("\x00");
    h.update(sessionId);
    const digest = h.digest();
    const hi = digest.readUInt32BE(0);
    const lo = digest.readUInt32BE(4);
    return (hi * 2_097_152 + (lo >>> 11)) / 9_007_199_254_740_992;
}
/**
 * Is this session in the sample? Deterministic and stable for the life
 * of the session. A rate of 0 samples nothing; 1 samples everything.
 */
export function shouldSampleSession(botId, sessionId, rate) {
    if (!botId || !sessionId)
        return false;
    if (!(rate > 0))
        return false;
    if (rate >= 1)
        return true;
    return hashScore(botId, sessionId) < rate;
}
/**
 * TTL-cached reader for the hot path. One ``network.json`` read a
 * minute, not one per turn — the same cadence
 * ``TurnObserver._isPreflightEnabled`` and ``_isPushbackEnabled`` use.
 *
 * The cache is keyed on ``hasConfidentPrior`` because that input picks
 * the default sample rate; a bot that gains a prior overnight sees the
 * new rate within one TTL rather than at the next gateway restart.
 */
export class ClassifierGateReader {
    sharedDir;
    botId;
    cached = null;
    static TTL_MS = 60_000;
    constructor(sharedDir, botId) {
        this.sharedDir = sharedDir;
        this.botId = botId;
    }
    read(hasConfidentPrior = false) {
        const now = Date.now();
        if (this.cached &&
            this.cached.withPrior === hasConfidentPrior &&
            now - this.cached.at < ClassifierGateReader.TTL_MS) {
            return this.cached.gate;
        }
        const gate = readClassifierGate(this.sharedDir, this.botId, hasConfidentPrior);
        this.cached = { gate, withPrior: hasConfidentPrior, at: now };
        return gate;
    }
    /** Drop the cache — tests, and the config-reload path. */
    invalidate() {
        this.cached = null;
    }
}
//# sourceMappingURL=classifierGate.js.map