/**
 * Reads L1 (cost) breaker state from the shared-dir file layout.
 *
 * Spec: internal/spec-circuit-breakers-2026-05-21.md §5.3
 *
 * File layout (Python writers — packages/analyzer/breakers/store.py +
 * packages/admin/evolve_admin/breakers_enforce.py):
 *
 *     {sharedDir}/breakers/<bot_id>/cost.json    — per-bot L1
 *     {sharedDir}/breakers/pod/cost.json         — pod-wide L1
 *
 * The plugin only needs to READ these. It is never the writer. Schema
 * is documented in the Python store; we deserialize what we need and
 * ignore extra fields. Fail-open at every error path — with one bit of
 * extra information (``VetoDecision.unreadable``) for the callers that
 * must fail CLOSED instead, see that field's comment.
 *
 * Performance: this runs on every non-user-channel turn via
 * before_agent_run. A local-disk read of a tiny JSON is sub-millisecond,
 * so no caching layer in v1 — keep the code simple and predictable.
 * Add caching only if profiling shows it matters.
 */

import * as fs from "fs";
import * as path from "path";

/**
 * Minimal subset of the breaker record schema we care about for veto
 * decisions. The Python writer includes additional fields (trip_id,
 * motivating_signals, audit_summary, etc.) that we deserialize but
 * don't act on here. Forward-compatible to additional fields.
 */
export interface BreakerRecord {
  bot_id: string;
  type: string;                       // "cost" | "full"
  state: string;                      // "tripped"
  tripped_at: string;
  expires_at: string | null;          // ISO-8601 or null for indefinite
  initiated_by: string;
  reason: string;
  trip_id?: string;
}

export interface VetoDecision {
  vetoed: boolean;
  /** "bot" if the per-bot breaker tripped; "pod" if the pod-wide one tripped. */
  scope?: "bot" | "pod";
  reason?: string;
  tripId?: string;
  /**
   * A breaker file EXISTS on disk but did not yield a usable record —
   * truncated write, disk-full, hand-edit, a schema change mid-deploy.
   *
   * Distinct from ``vetoed`` on purpose. ``vetoed`` keeps its fail-OPEN
   * contract for the turn veto (a parsing glitch must not brick the bot).
   * ``unreadable`` gives a SPEND gate the one extra bit it needs to fail
   * CLOSED instead: an *absent* file legitimately means "not tripped", but
   * a present-but-unreadable one means "unknown", and for anything that
   * spends money unknown must read as tripped. Consumers that want the
   * fail-closed direction opt in by checking this field (subagentRun's
   * ``_refusalFor``, TurnObserver's ``_breakerReplyClaim``); everyone else
   * is unaffected.
   */
  unreadable?: boolean;
}

/**
 * What one breaker file on disk actually is. ``readBreakerFile`` collapses
 * "absent" and "unreadable" into null — which is right for the turn veto
 * and wrong for a spend gate, so the distinction is preserved here.
 */
export type BreakerFileState =
  | { kind: "absent" }
  | { kind: "unreadable"; why: string }
  | { kind: "record"; record: BreakerRecord };

/**
 * Read raw breaker JSON from disk. Returns null when the file is
 * missing, unreadable, or unparseable. Never throws.
 *
 * Fail-open property: a corrupt or truncated file MUST NOT block
 * a turn. We return null exactly as if no trip exists. Mirrors
 * heal.py's defensive read of pause-state.json. Better to let a
 * possibly-vetoable turn through on a parsing glitch than to brick
 * the bot.
 */
export function readBreakerFile(p: string): BreakerRecord | null {
  const state = readBreakerFileState(p);
  return state.kind === "record" ? state.record : null;
}

/**
 * Read one breaker file, keeping "there is no file" separate from "there
 * is a file and I cannot use it". Never throws.
 *
 * Every null-returning branch of the old ``readBreakerFile`` is preserved
 * exactly; this only labels which of the two things happened.
 */
export function readBreakerFileState(p: string): BreakerFileState {
  let exists: boolean;
  try {
    exists = fs.existsSync(p);
  } catch {
    // Cannot even stat it — something is there in the way. Unknown, not
    // absent.
    return { kind: "unreadable", why: "stat failed" };
  }
  if (!exists) {
    return { kind: "absent" };
  }
  let text: string;
  try {
    text = fs.readFileSync(p, { encoding: "utf-8" });
  } catch {
    return { kind: "unreadable", why: "read failed" };
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return { kind: "unreadable", why: "unparseable JSON" };
  }
  if (!isPlainObject(data)) {
    return { kind: "unreadable", why: "not a JSON object" };
  }
  // Type-narrow minimally; missing required fields → unreadable
  // (treat as "no usable trip" rather than risk a partial record).
  const rec = data as Record<string, unknown>;
  if (typeof rec.bot_id !== "string"
      || typeof rec.type !== "string"
      || typeof rec.tripped_at !== "string") {
    return { kind: "unreadable", why: "missing required fields" };
  }
  return {
    kind: "record",
    record: {
      bot_id: rec.bot_id,
      type: rec.type,
      state: typeof rec.state === "string" ? rec.state : "tripped",
      tripped_at: rec.tripped_at,
      expires_at: typeof rec.expires_at === "string" ? rec.expires_at : null,
      initiated_by: typeof rec.initiated_by === "string" ? rec.initiated_by : "unknown",
      reason: typeof rec.reason === "string" ? rec.reason : "",
      trip_id: typeof rec.trip_id === "string" ? rec.trip_id : undefined,
    },
  };
}

/**
 * Has this trip expired? Mirrors Python is_expired().
 *
 * - expires_at === null → never expires (indefinite trip)
 * - expires_at unparseable → treat as active (fail-SAFE here, not fail-OPEN;
 *   a malformed expiry shouldn't accidentally clear a trip)
 * - expires_at in the past → expired
 */
export function isExpired(rec: BreakerRecord, now: Date = new Date()): boolean {
  if (rec.expires_at === null || rec.expires_at === undefined) {
    return false;
  }
  const exp = parseIso(rec.expires_at);
  if (exp === null) {
    return false;  // malformed — assume still active
  }
  return now.getTime() >= exp.getTime();
}

/**
 * Resolve the L1 cost-breaker decision for this bot.
 *
 * Returns vetoed=true if EITHER the per-bot breaker OR the pod-wide
 * breaker is tripped and not expired. scope/reason/tripId populated
 * from whichever breaker is active. Per-bot wins over pod-wide if
 * both are active (the more specific scope tends to carry the
 * relevant reason).
 *
 * No caching in v1 — sub-ms reads of two small JSON files on every
 * non-user-channel turn is fine. Add a cache if profiling says
 * otherwise.
 */
export function readCostBreakerDecision(opts: {
  sharedDir: string;
  botId: string;
  now?: Date;
}): VetoDecision {
  const now = opts.now ?? new Date();
  const breakersRoot = path.join(opts.sharedDir, "breakers");

  // Per-bot file first — its reason tends to be more specific.
  const perBotPath = path.join(breakersRoot, opts.botId, "cost.json");
  const perBotState = readBreakerFileState(perBotPath);
  const perBot = perBotState.kind === "record" ? perBotState.record : null;
  if (perBot !== null && !isExpired(perBot, now)) {
    return {
      vetoed: true,
      scope: "bot",
      reason: perBot.reason,
      tripId: perBot.trip_id,
    };
  }

  // Pod-wide fallback.
  const podPath = path.join(breakersRoot, "pod", "cost.json");
  const podState = readBreakerFileState(podPath);
  const pod = podState.kind === "record" ? podState.record : null;
  if (pod !== null && !isExpired(pod, now)) {
    return {
      vetoed: true,
      scope: "pod",
      reason: pod.reason,
      tripId: pod.trip_id,
    };
  }

  // Nothing tripped that we could READ. Report whether a breaker file was
  // nonetheless present-but-unusable, so a spend gate can fail closed on
  // it; ``vetoed`` stays false either way (the turn veto's fail-open
  // contract is unchanged).
  if (perBotState.kind === "unreadable") {
    return { vetoed: false, unreadable: true, scope: "bot", reason: perBotState.why };
  }
  if (podState.kind === "unreadable") {
    return { vetoed: false, unreadable: true, scope: "pod", reason: podState.why };
  }
  return { vetoed: false };
}

// ── internals ────────────────────────────────────────────────────────────────

function isPlainObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

function parseIso(s: string): Date | null {
  // Date.parse accepts ISO-8601 with or without trailing Z. Returns NaN
  // on garbage. Same conservative parsing as the Python side.
  const t = Date.parse(s);
  if (Number.isNaN(t)) {
    return null;
  }
  return new Date(t);
}
