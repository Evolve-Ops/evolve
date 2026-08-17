/**
 * PrefixHashLedger — Phase 0 of context observability.
 *
 * Spec: docs/spec-context-observability-2026-07-30.md §Phasing (Phase 0).
 * Motivated by docs/incident-post-mortem-2026-07-31-cost-containment.md §2:
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

import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";

export const PREFIX_HASH_SCHEMA_VERSION = 1;

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

function shaOrNull(s: string | null | undefined): string | null {
  if (!s) return null;
  return crypto.createHash("sha256").update(s, "utf8").digest("hex");
}

/** Pure record builder — the testable core. */
export function buildPrefixHashRecord(input: PrefixHashRecordInput): PrefixHashRecord {
  const now = input.now ?? new Date();
  const combined = input.combined ?? "";
  const blocks = input.blocks ?? {};
  return {
    schema_version: PREFIX_HASH_SCHEMA_VERSION,
    type: "prefix_hash",
    ts: now.toISOString(),
    bot_id: input.botId,
    session_id: input.sessionId || null,
    turn_id: input.turnId || null,
    path: input.path,
    prefix_sha256: shaOrNull(combined),
    appended_block_shas: {
      capabilities: shaOrNull(blocks.capabilities),
      digest: shaOrNull(blocks.digest),
      narrative: shaOrNull(blocks.narrative),
      speaker: shaOrNull(blocks.speaker),
      cost_downgrade: shaOrNull(blocks.costDowngrade),
    },
    combined_chars: combined.length,
  };
}

/** Date-rolled ledger filename for a given day (UTC). */
export function prefixHashFileName(now: Date): string {
  return `prefix-hashes-${now.toISOString().slice(0, 10)}.jsonl`;
}

interface LoggerLike {
  warn: (msg: string) => void;
  debug?: (msg: string) => void;
}

export class PrefixHashLedger {
  private readonly enabled: boolean;
  private readonly botId: string;
  private readonly turnsDir: string;
  private readonly logger: LoggerLike;
  private dirReady = false;
  private dirFailed = false;

  constructor(cfg: {
    enabled: boolean;
    sharedDir: string;
    botId: string;
    logger: LoggerLike;
  }) {
    this.enabled = cfg.enabled;
    this.botId = cfg.botId;
    // Same dir the turns file lands in — resolve_bot_paths() turns_dir on the
    // admin side already knows it, so the Phase 0 join reads siblings.
    this.turnsDir = path.join(cfg.sharedDir, cfg.botId, "turns");
    this.logger = cfg.logger;
  }

  /** Build + append a record. Never throws; disabled/failed states no-op. */
  record(input: Omit<PrefixHashRecordInput, "botId">): void {
    if (!this.enabled || this.dirFailed) return;
    try {
      const rec = buildPrefixHashRecord({ ...input, botId: this.botId });
      if (!this.dirReady) {
        try {
          fs.mkdirSync(this.turnsDir, { recursive: true });
          this.dirReady = true;
        } catch (mkdirErr: unknown) {
          // EACCES: turns dir owned by another gateway / not yet deployed —
          // permanent for this process, stop trying (same posture as
          // writeTurnToShared). Anything else: log once, stop trying.
          this.dirFailed = true;
          const code = (mkdirErr as NodeJS.ErrnoException)?.code;
          if (code !== "EACCES" && code !== "EPERM") {
            this.logger.warn(`Evolve prefix-hash ledger: cannot create ${this.turnsDir}: ${mkdirErr}`);
          }
          return;
        }
      }
      const file = path.join(this.turnsDir, prefixHashFileName(input.now ?? new Date()));
      fs.appendFileSync(file, JSON.stringify(rec) + "\n", { mode: 0o644 });
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code === "EACCES" || code === "EPERM") {
        this.logger.debug?.(`Evolve prefix-hash ledger: no write access for ${this.botId}, skipping`);
        return;
      }
      this.logger.warn(`Evolve prefix-hash ledger: write failed: ${err}`);
    }
  }
}
