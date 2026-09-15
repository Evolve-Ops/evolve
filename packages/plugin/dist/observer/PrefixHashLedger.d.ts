/**
 * PrefixHashLedger — Phase 0 of context observability.
 *
 * Spec: internal/spec-context-observability-2026-07-30.md §Phasing (Phase 0).
 * Motivated by internal/incident-post-mortem-2026-07-31-cost-containment.md §2:
 * Evolve appends per-turn-varying content to the system prompt via
 * ``before_prompt_build`` → ``appendSystemContext``, which OC concatenates
 * ahead of every message — plausibly invalidating the whole prompt cache on
 * every byte change. The turns JSONL records token counts, never the prompt,
 * so prefix stability is unmeasurable from existing data. This ledger emits
 * one hash record per ``before_prompt_build`` invocation so the join against
 * ``cache_read_input_tokens`` can answer the root-cause question: **does the
 * prefix change on the turns that miss cache?**
 *
 * Scope note (honest limits): the hash covers the appendSystemContext string
 * Evolve returns — the only part of the system prompt this plugin composes or
 * can see. The OC base system prompt ahead of it is assumed byte-stable; if
 * the join shows cold misses with a STABLE Evolve hash, the churn is upstream
 * of this plugin (base prompt, tool schemas) and §2 is refuted for Evolve's
 * share specifically. Either answer is the point of Phase 0.
 *
 * Schema (type: "prefix_hash", schema_version: 1):
 * {
 *   schema_version: 1,
 *   type: "prefix_hash",
 *   ts: string,                    // ISO-8601 UTC
 *   bot_id: string,
 *   session_id: string | null,
 *   turn_id: string | null,        // ctx.runId — joins to nothing yet; kept for
 *                                  // future OC records that carry it
 *   path: "blocks"|"stay_silent"|"llm_echo",  // which hook return path fired
 *   prefix_sha256: string | null,  // sha256 of the FULL appendSystemContext
 *                                  // returned; null when nothing was appended
 *                                  // (absence is signal: presence flapping is
 *                                  // itself a churn source — never hash "")
 *   appended_block_shas: {         // per-block attribution (spec §Metrics):
 *     capabilities: string|null,   //   which injection is churning is the
 *     digest: string|null,         //   difference between "prefix unstable"
 *     narrative: string|null,      //   and "your 3-min directory digest costs
 *     speaker: string|null,        //   $X/day". null = block absent/empty.
 *   },
 *   combined_chars: number         // length of the appended string (0 = none)
 * }
 *
 * Design:
 *   - DARK BY DEFAULT. Gated on ``prefixHashLedgerEnabled`` (openclaw.json
 *     plugin config), default false. Phase 0 ships dark per spec.
 *   - Records land beside the turns file:
 *     ``{sharedDir}/{botId}/turns/prefix-hashes-YYYY-MM-DD.jsonl`` — same dir,
 *     same ACL story, date-rolled like turns-*.jsonl. Hashes are not content;
 *     they leak only change *frequency* (spec open question 3 — resolved as
 *     "same dir as turns" for Phase 0).
 *   - Never throws into the LLM critical path: every public method swallows
 *     and logs. EACCES is silent-skip (another gateway owns the dir), same
 *     posture as writeTurnToShared.
 *   - One sha256 of a few KB + one appendFileSync per turn — negligible.
 */
export declare const PREFIX_HASH_SCHEMA_VERSION = 1;
/** Which before_prompt_build return path produced this record. */
export type PrefixHashPath = "blocks" | "stay_silent" | "llm_echo";
/** The regular-turn injection blocks, pre-combination. Empty string and
 *  undefined both mean "absent" and record as null. */
export interface PrefixBlocks {
    capabilities?: string;
    digest?: string;
    narrative?: string;
    speaker?: string;
    /** Cost-downgrade attribution note (present only on safety-net turns). */
    costDowngrade?: string;
}
export interface PrefixHashRecordInput {
    botId: string;
    sessionId?: string | null;
    turnId?: string | null;
    path: PrefixHashPath;
    /** The exact appendSystemContext string returned to OC ("" / undefined when
     *  the hook returned nothing). */
    combined: string | null | undefined;
    /** Per-block strings for the regular path; omit on directive paths. */
    blocks?: PrefixBlocks;
    /** Injectable clock for tests. */
    now?: Date;
}
export interface PrefixHashRecord {
    schema_version: number;
    type: "prefix_hash";
    ts: string;
    bot_id: string;
    session_id: string | null;
    turn_id: string | null;
    path: PrefixHashPath;
    prefix_sha256: string | null;
    appended_block_shas: {
        capabilities: string | null;
        digest: string | null;
        narrative: string | null;
        speaker: string | null;
        cost_downgrade: string | null;
    };
    combined_chars: number;
}
/** Pure record builder — the testable core. */
export declare function buildPrefixHashRecord(input: PrefixHashRecordInput): PrefixHashRecord;
/** Date-rolled ledger filename for a given day (UTC). */
export declare function prefixHashFileName(now: Date): string;
interface LoggerLike {
    warn: (msg: string) => void;
    debug?: (msg: string) => void;
}
export declare class PrefixHashLedger {
    private readonly enabled;
    private readonly botId;
    private readonly turnsDir;
    private readonly logger;
    private dirReady;
    private dirFailed;
    constructor(cfg: {
        enabled: boolean;
        sharedDir: string;
        botId: string;
        logger: LoggerLike;
    });
    /** Build + append a record. Never throws; disabled/failed states no-op. */
    record(input: Omit<PrefixHashRecordInput, "botId">): void;
}
export {};
//# sourceMappingURL=PrefixHashLedger.d.ts.map