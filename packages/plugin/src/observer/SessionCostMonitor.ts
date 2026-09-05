/**
 * SessionCostMonitor — in-flight per-session cost sentinel.
 *
 * Spec: internal/spec-session-budget-cap-2026-05-31.md (this PR).
 *
 * Why this exists:
 *
 *   The daily per-bot cost cap (PR #1483's L1 breaker, knob
 *   ``better_engine.budget.per_bot_daily_hard_usd``) is a daily-grain
 *   sweep — it can't catch a single-session runaway in flight. A 33-turn
 *   $7.67 session over 2 hours (motivating incident: team-bot-a session
 *   3d5cde22, 2026-05-29 Slack DM) is only ~38% of a $20 daily cap but
 *   is exactly the shape of runaway we want to stop *while it's still
 *   happening*, not at midnight.
 *
 *   This monitor accumulates per-session cost on each ``llm_output``
 *   event using the same estimate function the rest of the plugin uses
 *   for cost_event records. When a session's accumulated cost crosses
 *   the configured cap (``agents.defaults.sessionBudgetCapUsd``), the
 *   monitor writes a per-session breaker file. The pre-turn check in
 *   TurnObserver's ``handleBeforeAgentRun`` reads that file and rejects
 *   the next turn on that session with a budget-exceeded message.
 *
 * Distinct from the L1 cost breaker:
 *   - L1 cost breaker (BreakerStateReader.ts) — per-bot or pod-wide,
 *     trips on *daily* spend, blocks only auto-source turns.
 *   - Session-budget breaker (this file) — per-session, trips on
 *     *single-session* spend, blocks ALL turns on that session
 *     (including user turns — the whole point is "stop this runaway").
 *
 * Idempotency:
 *   - First crossing writes the breaker file once.
 *   - Subsequent llm_output events for the same session add to the
 *     in-memory bucket but do NOT rewrite the file (the file is
 *     enough for the pre-turn check).
 *   - In-memory map keyed by (botId, sessionId). Bot restart resets
 *     the in-memory bucket but the breaker file on disk persists; an
 *     operator's reset of the file is the canonical "clear this".
 *
 * Fail-open discipline:
 *   - Cap unreadable (openclaw.json unreadable, key absent, malformed) →
 *     no cap, no trip. Never crash the LLM path on a cap-config bug.
 *   - Disk write failure → log and continue. The next event will retry.
 *   - Cap === null/undefined/0/negative → no cap. Treated as "opt-out".
 *
 * Signal emission:
 *   - On first trip we ALSO write a Signal record to the signal store
 *     so the alert lands on the operator's watchlist. The signal write
 *     is best-effort — a write failure must not undo the breaker file
 *     write (the breaker is the load-bearing artifact).
 */

import * as fs from "fs";
import * as path from "path";

/** Per-session cost accumulator. In-memory only; the on-disk
 *  authoritative artifact is the breaker file. */
interface SessionBucket {
  /** Sum of estimateCost() over every llm_output for this session. */
  accumulatedUsd: number;
  /** Set once the breaker file has been written so we don't rewrite
   *  it on every subsequent llm_output event. */
  tripped: boolean;
  /** Last known user_id / channel context (best-effort — used for the
   *  emitted Signal). */
  lastUserId: string | null;
  lastChannelId: string | null;
  lastChannelKind: string | null;
  /** Time of the most recent observation in this session — used to age
   *  out idle buckets when the monitor's prune routine runs. */
  lastSeenMs: number;
}

/** Returned by recordCost — gives the caller enough context to log
 *  what happened without re-reading the in-memory state. */
export interface RecordCostResult {
  /** Total accumulated cost for the session AFTER this call's add. */
  accumulatedUsd: number;
  /** True if THIS call crossed the cap and wrote the breaker file. */
  trippedThisCall: boolean;
  /** True if the session was already tripped before this call (cap
   *  crossed in an earlier call). */
  alreadyTripped: boolean;
  /** The effective cap in USD, or null if no cap is configured. */
  capUsd: number | null;
}

/** Schema written to ``{sharedDir}/breakers/<bot_id>/session-<sid>.json``.
 *  Distinct from the existing ``cost.json`` (daily-grain L1 breaker)
 *  shape because the consumer (pre-turn check) treats them as
 *  separate breaker types — a session breaker that's tripped should
 *  NOT block other sessions on the same bot. */
export interface SessionBreakerRecord {
  bot_id: string;
  session_id: string;
  type: "session_budget";
  state: "tripped";
  tripped_at: string;            // ISO-8601 UTC
  cap_usd: number;
  cost_usd: number;              // accumulated cost at trip time
  /** Cheap context: which model + provider were last seen on this
   *  session. Helps the operator triage without spelunking the JSONL. */
  last_model: string | null;
  last_provider: string | null;
  /** Channel-of-origin context (best-effort). */
  user_id: string | null;
  channel_id: string | null;
  channel_kind: string | null;
  /** Free-form explanation rendered in the rejection message. */
  reason: string;
}

export class SessionCostMonitor {
  private buckets: Map<string, SessionBucket> = new Map();
  private readonly sharedDir: string;
  private readonly botId: string;
  /** Resolver for the per-session cap. Injectable so tests can stub
   *  out the openclaw.json read with a synthetic value. */
  private readonly resolveCap: () => number | null;
  /** Optional signal emitter — called on first trip with the breaker
   *  record. Best-effort; failures are swallowed. Injected so tests
   *  can observe the call without writing to a real signal store. */
  private readonly emitSignal: ((rec: SessionBreakerRecord) => void) | null;

  constructor(opts: {
    sharedDir: string;
    botId: string;
    resolveCap: () => number | null;
    emitSignal?: (rec: SessionBreakerRecord) => void;
  }) {
    this.sharedDir = opts.sharedDir;
    this.botId = opts.botId;
    this.resolveCap = opts.resolveCap;
    this.emitSignal = opts.emitSignal ?? null;
  }

  /** Compose the cache key for an (bot, session) pair. The bot is
   *  redundant in normal operation (one monitor instance per gateway)
   *  but the explicit key keeps the test surface obvious. */
  private bucketKey(sessionId: string): string {
    return `${this.botId}::${sessionId}`;
  }

  /** Compose the breaker file path. Mirrors the per-bot ``cost.json``
   *  layout (``breakers/<bot_id>/<type>.json``) but with a session-tagged
   *  filename so multiple session breakers can coexist on a single bot.
   *
   *  The session id can in principle contain characters that are awkward
   *  on disk (slashes from path-style ids, etc.). We sanitize to
   *  ``[A-Za-z0-9_-]`` and truncate to keep filenames human-readable
   *  for operator triage. This is a one-way transform — the on-disk file
   *  carries the original session_id INSIDE the JSON record, so the
   *  filename being sanitized doesn't lose information. */
  private breakerFilePath(sessionId: string): string {
    const sanitized = sessionId
      .replace(/[^A-Za-z0-9_-]/g, "_")
      .slice(0, 64);
    return path.join(
      this.sharedDir,
      "breakers",
      this.botId,
      `session-${sanitized}.json`,
    );
  }

  /** Read the cap and (separately) the current bucket. Useful for
   *  diagnostic logging without going through recordCost. */
  peek(sessionId: string): { accumulatedUsd: number; tripped: boolean } {
    const b = this.buckets.get(this.bucketKey(sessionId));
    if (!b) return { accumulatedUsd: 0, tripped: false };
    return { accumulatedUsd: b.accumulatedUsd, tripped: b.tripped };
  }

  /** Add the cost of one llm_output event to the session's bucket.
   *  If this call crosses the cap, write the breaker file (once).
   *
   *  Context fields are best-effort: when the gateway doesn't thread
   *  user_id / channel through llm_output ctx, we record null and the
   *  emitted Signal carries null for those fields. */
  recordCost(opts: {
    sessionId: string;
    callCostUsd: number;
    model?: string | null;
    provider?: string | null;
    userId?: string | null;
    channelId?: string | null;
    channelKind?: string | null;
    now?: Date;
  }): RecordCostResult {
    const now = opts.now ?? new Date();
    const capUsd = this.resolveCapSafe();

    const key = this.bucketKey(opts.sessionId);
    let bucket = this.buckets.get(key);
    if (!bucket) {
      bucket = {
        accumulatedUsd: 0,
        tripped: false,
        lastUserId: null,
        lastChannelId: null,
        lastChannelKind: null,
        lastSeenMs: now.getTime(),
      };
      this.buckets.set(key, bucket);
    }

    // Negative or non-finite costs are dropped (estimateCost returns 0
    // for unknown models — non-finite would mean an upstream bug; we
    // refuse to let it poison the bucket).
    if (Number.isFinite(opts.callCostUsd) && opts.callCostUsd > 0) {
      bucket.accumulatedUsd += opts.callCostUsd;
    }
    bucket.lastSeenMs = now.getTime();
    if (opts.userId) bucket.lastUserId = opts.userId;
    if (opts.channelId) bucket.lastChannelId = opts.channelId;
    if (opts.channelKind) bucket.lastChannelKind = opts.channelKind;

    const alreadyTripped = bucket.tripped;

    // Cap check: only trip when the cap is a finite positive number AND
    // the accumulated cost has crossed it AND we haven't already
    // tripped (idempotency).
    let trippedThisCall = false;
    if (
      !alreadyTripped
      && capUsd !== null
      && Number.isFinite(capUsd)
      && capUsd > 0
      && bucket.accumulatedUsd >= capUsd
    ) {
      bucket.tripped = true;
      trippedThisCall = true;
      const rec: SessionBreakerRecord = {
        bot_id: this.botId,
        session_id: opts.sessionId,
        type: "session_budget",
        state: "tripped",
        tripped_at: now.toISOString(),
        cap_usd: capUsd,
        cost_usd: round6(bucket.accumulatedUsd),
        last_model: opts.model ?? null,
        last_provider: opts.provider ?? null,
        user_id: bucket.lastUserId,
        channel_id: bucket.lastChannelId,
        channel_kind: bucket.lastChannelKind,
        reason: (
          `Session cost $${round6(bucket.accumulatedUsd).toFixed(4)} `
          + `exceeded per-session cap $${capUsd.toFixed(2)}.`
        ),
      };
      this.writeBreakerFile(rec);
      if (this.emitSignal) {
        try {
          this.emitSignal(rec);
        } catch {
          // Signal emission is best-effort. The breaker file already
          // landed; failing to emit a Signal must not unwind that.
        }
      }
    }

    return {
      accumulatedUsd: round6(bucket.accumulatedUsd),
      trippedThisCall,
      alreadyTripped,
      capUsd,
    };
  }

  /** Wrap the operator-provided cap resolver in a try/catch so a
   *  config-read bug never crashes the LLM path. */
  private resolveCapSafe(): number | null {
    try {
      const cap = this.resolveCap();
      if (cap === null || cap === undefined) return null;
      if (!Number.isFinite(cap)) return null;
      if (cap <= 0) return null;
      return cap;
    } catch {
      return null;
    }
  }

  /** Write the breaker file atomically (same-dir tempfile + rename),
   *  matching the recovery._atomic_write_json pattern used by the
   *  Python breaker store. Idempotent at the call-site: this is only
   *  invoked from inside the !alreadyTripped branch above.
   *
   *  If the breaker file already exists on disk (e.g. a prior crash
   *  left a record from before the bucket was repopulated in memory),
   *  we still rewrite — the post-condition is "file with current
   *  numbers" not "file written exactly once across plugin lifetime."
   *  Operator resets DELETE the file, so this isn't a leak vector.
   *
   *  Fail-soft: any I/O error here is silently swallowed. The
   *  next llm_output event will retry the write because bucket.tripped
   *  is set inside the same function only AFTER writeBreakerFile returns
   *  — wait, it's set BEFORE in the caller, so a write failure won't
   *  retry. Trade-off: refusing to set tripped=true until write succeeds
   *  would mean every subsequent event also tries to write (silent
   *  retry storm). Setting it on first attempt with no retry is
   *  simpler and matches the pattern in the Python store; the breaker
   *  file is a *signal* to the pre-turn check, not the source of truth.
   *  If the next attempt fails the bucket still reports trippedThisCall
   *  → false but the in-memory tripped=true keeps the monitor honest
   *  for the lifetime of this gateway. */
  private writeBreakerFile(rec: SessionBreakerRecord): void {
    const filePath = this.breakerFilePath(rec.session_id);
    const dir = path.dirname(filePath);
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch {
      return;
    }
    const tmp = `${filePath}.tmp.${process.pid}`;
    try {
      fs.writeFileSync(tmp, JSON.stringify(rec, null, 2) + "\n", {
        encoding: "utf-8",
      });
      fs.renameSync(tmp, filePath);
    } catch {
      try {
        fs.unlinkSync(tmp);
      } catch {
        /* ignore */
      }
    }
  }

  /** Clear in-memory state for a session (e.g. on session_end). The
   *  on-disk breaker file is NOT touched — operator owns the reset. */
  forget(sessionId: string): void {
    this.buckets.delete(this.bucketKey(sessionId));
  }

  /** Drop in-memory buckets idle beyond ``maxAgeMs``. Optional hygiene
   *  hook; the daemon doesn't need to call this for correctness. */
  pruneIdle(maxAgeMs: number, now: Date = new Date()): number {
    const cutoff = now.getTime() - maxAgeMs;
    let removed = 0;
    for (const [key, bucket] of this.buckets) {
      if (bucket.lastSeenMs < cutoff) {
        this.buckets.delete(key);
        removed++;
      }
    }
    return removed;
  }
}


/** Round to 6 decimal places to match the CostLedger schema's precision. */
function round6(n: number): number {
  return Math.round(n * 1_000_000) / 1_000_000;
}


/** Read ``agents.defaults.sessionBudgetCapUsd`` from a bot's openclaw.json.
 *
 *  Pure file read — no side effects. Returns null on any failure path
 *  (file missing, unreadable, unparseable, key absent, key non-numeric).
 *  Mirrors the fail-open semantics of BreakerStateReader: a config
 *  glitch must never lock a bot out of turns. */
export function readSessionBudgetCap(opts: {
  botHome: string;
  now?: Date;
}): number | null {
  const openclawPath = path.join(opts.botHome, ".openclaw", "openclaw.json");
  let text: string;
  try {
    if (!fs.existsSync(openclawPath)) return null;
    text = fs.readFileSync(openclawPath, { encoding: "utf-8" });
  } catch {
    return null;
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isPlainObject(data)) return null;
  const agents = (data as Record<string, unknown>).agents;
  if (!isPlainObject(agents)) return null;
  const defaults = (agents as Record<string, unknown>).defaults;
  if (!isPlainObject(defaults)) return null;
  const cap = (defaults as Record<string, unknown>).sessionBudgetCapUsd;
  if (cap === null || cap === undefined) return null;
  if (typeof cap !== "number") return null;
  if (!Number.isFinite(cap)) return null;
  if (cap <= 0) return null;
  return cap;
}


/** Check whether a session-budget breaker file exists for this session.
 *  Used by the pre-turn check in handleBeforeAgentRun to decide whether
 *  to reject the next turn.
 *
 *  Returns the parsed record (so the rejection message can carry the
 *  cap + cost) or null if no trip is active.
 *
 *  Fail-open at every error path: a corrupt or unreadable file is
 *  treated as "no trip" rather than locking the bot out. */
export function readSessionBudgetBreaker(opts: {
  sharedDir: string;
  botId: string;
  sessionId: string;
}): SessionBreakerRecord | null {
  const sanitized = opts.sessionId
    .replace(/[^A-Za-z0-9_-]/g, "_")
    .slice(0, 64);
  const filePath = path.join(
    opts.sharedDir,
    "breakers",
    opts.botId,
    `session-${sanitized}.json`,
  );
  let text: string;
  try {
    if (!fs.existsSync(filePath)) return null;
    text = fs.readFileSync(filePath, { encoding: "utf-8" });
  } catch {
    return null;
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isPlainObject(data)) return null;
  const rec = data as Record<string, unknown>;
  if (
    typeof rec.bot_id !== "string"
    || typeof rec.session_id !== "string"
    || typeof rec.cap_usd !== "number"
    || typeof rec.cost_usd !== "number"
  ) {
    return null;
  }
  return {
    bot_id: rec.bot_id,
    session_id: rec.session_id,
    type: "session_budget",
    state: "tripped",
    tripped_at: typeof rec.tripped_at === "string" ? rec.tripped_at : "",
    cap_usd: rec.cap_usd,
    cost_usd: rec.cost_usd,
    last_model: typeof rec.last_model === "string" ? rec.last_model : null,
    last_provider: typeof rec.last_provider === "string" ? rec.last_provider : null,
    user_id: typeof rec.user_id === "string" ? rec.user_id : null,
    channel_id: typeof rec.channel_id === "string" ? rec.channel_id : null,
    channel_kind: typeof rec.channel_kind === "string" ? rec.channel_kind : null,
    reason: typeof rec.reason === "string" ? rec.reason : "",
  };
}


function isPlainObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}
