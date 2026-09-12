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

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

/** The trigger kinds a due-check may ever suppress. */
export const DUE_CHECK_TRIGGERS: ReadonlyArray<string> = ["heartbeat", "cron"];

/** Filename of the per-bot conditions file, beside HEARTBEAT.md. */
export const CONDITIONS_FILENAME = "HEARTBEAT.json";

/** Recognised ``when`` kinds. An unknown kind fails the whole file open. */
export const CONDITION_KINDS: ReadonlyArray<string> = [
  "path_exists",
  "path_missing",
  "dir_non_empty",
  "file_changed",
  "time_window",
  "every",
];

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

export type DueVerdict =
  | {
      decision: "skip";
      due: [];
      reason: string;
      conditionsEvaluated: number;
      /** sha256 of the conditions file the decision was made from. */
      conditionsSha256: string;
      /** OC cron job id when the decision was made in a cron scope. */
      cronJobId: string | null;
    }
  | {
      decision: "wake";
      due: DueItem[];
      reason: string;
      conditionsEvaluated: number;
      /** State to commit once the wake is real. */
      nextState: DueState;
      conditionsSha256: string;
      cronJobId: string | null;
    }
  | {
      decision: "unavailable";
      due: [];
      reason: string;
      /** Set when an operator should hear about it (bad file, not absent one). */
      warn: string | null;
    };

// ── Parsing ───────────────────────────────────────────────────────────────

/** ``30m`` / ``6h`` / ``2d`` / bare minutes → ms. Null when unparseable. */
export function parseInterval(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) {
    return Math.round(raw * 60_000);
  }
  if (typeof raw !== "string") return null;
  const m = /^\s*(\d+(?:\.\d+)?)\s*([mhd])\s*$/.exec(raw.toLowerCase());
  if (!m) return null;
  const n = Number(m[1]);
  if (!Number.isFinite(n) || n <= 0) return null;
  const unit = { m: 60_000, h: 3_600_000, d: 86_400_000 }[m[2] as "m" | "h" | "d"];
  return Math.round(n * unit);
}

/** ``HH:MM`` → minutes since local midnight. Null when unparseable. */
export function parseClock(raw: unknown): number | null {
  if (typeof raw !== "string") return null;
  const m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(raw);
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h > 23 || min > 59) return null;
  return h * 60 + min;
}

/**
 * Parse one scope's condition list. ``label`` names the scope in error
 * messages; ``keyPrefix`` namespaces the per-condition state keys.
 */
function parseConditionList(
  rawConditions: unknown,
  label: string,
  keyPrefix: string,
): { ok: DueCondition[] } | { error: string } {
  if (rawConditions !== undefined && !Array.isArray(rawConditions)) {
    return { error: `${label} must be an array` };
  }
  const conditions: DueCondition[] = [];
  const seen = new Set<string>();
  for (const [i, entry] of ((rawConditions as unknown[]) ?? []).entries()) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      return { error: `${label}[${i}] must be an object` };
    }
    const c = entry as Record<string, unknown>;
    const id = typeof c.id === "string" ? c.id.trim() : "";
    if (!id) return { error: `${label}[${i}] needs a non-empty 'id'` };
    if (seen.has(id)) return { error: `duplicate condition id ${JSON.stringify(id)} in ${label}` };
    seen.add(id);
    const when = typeof c.when === "string" ? c.when.trim() : "";
    if (!CONDITION_KINDS.includes(when)) {
      return {
        error:
          `condition ${JSON.stringify(id)} has unknown 'when' ${JSON.stringify(c.when)} ` +
          `(expected one of ${CONDITION_KINDS.join(", ")})`,
      };
    }
    const wake = typeof c.wake === "string" ? c.wake.trim() : "";
    if (!wake) {
      return {
        error:
          `condition ${JSON.stringify(id)} needs a 'wake' string — it becomes the ` +
          `model's instruction when this condition is what woke it`,
      };
    }
    const cond: DueCondition = { id, when, wake, stateKey: `${keyPrefix}${id}` };
    if (when === "time_window") {
      const after = parseClock(c.after);
      const before = parseClock(c.before);
      if (after === null || before === null) {
        return {
          error: `condition ${JSON.stringify(id)} needs 'after' and 'before' as HH:MM`,
        };
      }
      if (after >= before) {
        return {
          error:
            `condition ${JSON.stringify(id)}: 'after' (${c.after}) must be earlier ` +
            `than 'before' (${c.before}) — windows do not wrap midnight`,
        };
      }
      cond.after = String(c.after);
      cond.before = String(c.before);
    } else if (when === "every") {
      if (parseInterval(c.interval) === null) {
        return {
          error:
            `condition ${JSON.stringify(id)} needs an 'interval' like "30m", "6h" or "1d"`,
        };
      }
      cond.interval = String(c.interval);
    } else {
      const rel = typeof c.path === "string" ? c.path.trim() : "";
      if (!rel) return { error: `condition ${JSON.stringify(id)} needs a 'path'` };
      const bad = unsafeRelativePath(rel);
      if (bad) return { error: `condition ${JSON.stringify(id)} path ${JSON.stringify(rel)}: ${bad}` };
      cond.path = rel;
    }
    conditions.push(cond);
  }
  return { ok: conditions };
}

/** State-key prefix for one cron job's scope. */
export function cronStateKeyPrefix(jobId: string): string {
  return `cron:${jobId}:`;
}

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
export function parseDueConditions(
  raw: string,
): { ok: ParsedConditions } | { error: string } {
  let doc: unknown;
  try {
    doc = JSON.parse(raw);
  } catch (err) {
    return { error: `not valid JSON (${String(err).slice(0, 120)})` };
  }
  if (typeof doc !== "object" || doc === null || Array.isArray(doc)) {
    return { error: "top level must be a JSON object" };
  }
  const obj = doc as Record<string, unknown>;
  if (obj.version !== undefined && obj.version !== 1) {
    return { error: `unsupported version ${JSON.stringify(obj.version)} (expected 1)` };
  }
  // Absent ``enabled`` means enabled: a file an operator wrote is a file they
  // meant. Only an explicit ``false`` parks it.
  const enabled = obj.enabled === undefined ? true : obj.enabled === true;
  if (obj.enabled !== undefined && typeof obj.enabled !== "boolean") {
    return { error: "'enabled' must be a boolean" };
  }
  const heartbeat = parseConditionList(obj.conditions, "'conditions'", "");
  if ("error" in heartbeat) return { error: heartbeat.error };

  const cron: Record<string, DueCondition[]> = {};
  const rawCron = obj.cron;
  if (rawCron !== undefined) {
    if (typeof rawCron !== "object" || rawCron === null || Array.isArray(rawCron)) {
      return {
        error:
          "'cron' must be an object mapping each cron job's id to its own " +
          "conditions — the heartbeat's conditions never gate a cron job",
      };
    }
    for (const [jobId, rawList] of Object.entries(rawCron as Record<string, unknown>)) {
      const key = jobId.trim();
      if (!key) return { error: "'cron' has an empty job id key" };
      const parsed = parseConditionList(rawList, `'cron.${key}'`, cronStateKeyPrefix(key));
      if ("error" in parsed) return { error: parsed.error };
      cron[key] = parsed.ok;
    }
  }
  return { ok: { enabled, conditions: heartbeat.ok, cron } };
}

/**
 * Why ``rel`` may not be used as a workspace-relative path, or null when it
 * is fine. The conditions file is bot-authored content that decides whether
 * a turn runs; it must not be able to point the stat() at another user's
 * home. Absolute paths and ``..`` are both refused outright rather than
 * normalised — an operator who meant a path outside the workspace should
 * hear about it, not get a silently rewritten one.
 */
export function unsafeRelativePath(rel: string): string | null {
  if (path.isAbsolute(rel)) return "must be relative to the workspace, not absolute";
  const parts = rel.split(/[\\/]+/).filter((p) => p.length > 0);
  if (parts.length === 0) return "is empty";
  if (parts.some((p) => p === "..")) return "must not contain '..'";
  return null;
}

// ── Evaluation ────────────────────────────────────────────────────────────

/** Filesystem facts one condition needs. Injectable so tests stay hermetic. */
export interface FsProbe {
  exists(abs: string): boolean;
  isDirectory(abs: string): boolean;
  /** Entries excluding dotfiles; empty when not a readable directory. */
  entryCount(abs: string): number;
  /** mtime in ms, or null when the path is absent/unreadable. */
  mtimeMs(abs: string): number | null;
}

export const realFsProbe: FsProbe = {
  exists(abs) {
    try {
      fs.statSync(abs);
      return true;
    } catch {
      return false;
    }
  },
  isDirectory(abs) {
    try {
      return fs.statSync(abs).isDirectory();
    } catch {
      return false;
    }
  },
  entryCount(abs) {
    try {
      return fs.readdirSync(abs).filter((n) => !n.startsWith(".")).length;
    } catch {
      return 0;
    }
  },
  mtimeMs(abs) {
    try {
      return fs.statSync(abs).mtimeMs;
    } catch {
      return null;
    }
  },
};

/** Minutes since local midnight for ``now``. */
function localMinutes(now: Date): number {
  return now.getHours() * 60 + now.getMinutes();
}

/** Local-midnight instant of ``now``, in ms. */
function localMidnightMs(now: Date): number {
  const d = new Date(now.getTime());
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/**
 * Evaluate every condition against the filesystem and the clock.
 *
 * Pure apart from ``probe``: same inputs, same verdict. Every "unknown"
 * resolves toward DUE — a ``file_changed`` we have never seen before, an
 * ``every`` that has never fired — because the first run after an operator
 * writes the file must wake the model, not swallow the trigger.
 */
export function evaluateConditions(
  conditions: DueCondition[],
  workspaceDir: string,
  state: DueState,
  now: Date,
  probe: FsProbe = realFsProbe,
): { due: DueItem[]; nextState: DueState } {
  const due: DueItem[] = [];
  const nextState: DueState = {
    version: 1,
    conditions: { ...(state.conditions ?? {}) },
    ...(state.lastWokeAt ? { lastWokeAt: state.lastWokeAt } : {}),
  };
  for (const c of conditions) {
    const stateKey = c.stateKey ?? c.id;
    const prior = state.conditions?.[stateKey] ?? {};
    const abs = c.path ? path.join(workspaceDir, c.path) : workspaceDir;
    let fired: string | null = null;
    let mtimeToRecord: number | undefined;
    switch (c.when) {
      case "path_exists":
        if (probe.exists(abs)) fired = `${c.path} exists`;
        break;
      case "path_missing":
        if (!probe.exists(abs)) fired = `${c.path} is missing`;
        break;
      case "dir_non_empty": {
        if (probe.isDirectory(abs)) {
          const n = probe.entryCount(abs);
          if (n > 0) fired = `${c.path} holds ${n} ${n === 1 ? "entry" : "entries"}`;
        }
        break;
      }
      case "file_changed": {
        const mtime = probe.mtimeMs(abs);
        if (mtime === null) break; // absent file cannot have changed
        if (prior.lastMtimeMs === undefined) {
          fired = `${c.path} has not been read since this condition was added`;
          mtimeToRecord = mtime;
        } else if (mtime > prior.lastMtimeMs) {
          fired = `${c.path} changed since the last run`;
          mtimeToRecord = mtime;
        }
        break;
      }
      case "time_window": {
        const after = parseClock(c.after) ?? 0;
        const before = parseClock(c.before) ?? 0;
        const mins = localMinutes(now);
        if (mins < after || mins >= before) break;
        // Fire once per window: a lastFiredAt at or after today's window
        // opening means this window has already been served.
        const windowOpenedMs = localMidnightMs(now) + after * 60_000;
        const lastMs = prior.lastFiredAt ? Date.parse(prior.lastFiredAt) : NaN;
        if (Number.isFinite(lastMs) && lastMs >= windowOpenedMs) break;
        fired = `inside the ${c.after}–${c.before} window`;
        break;
      }
      case "every": {
        const everyMs = parseInterval(c.interval);
        if (everyMs === null) break;
        const lastMs = prior.lastFiredAt ? Date.parse(prior.lastFiredAt) : NaN;
        if (!Number.isFinite(lastMs)) {
          fired = `never run; due every ${c.interval}`;
        } else if (now.getTime() - lastMs >= everyMs) {
          fired = `last run ${new Date(lastMs).toISOString()}; due every ${c.interval}`;
        }
        break;
      }
      default:
        // Unreachable: parseDueConditions rejects unknown kinds. Fail toward
        // waking anyway, so a future kind added to the parser but not here
        // can never silently stop a heartbeat.
        fired = `condition kind '${c.when}' is not evaluable here`;
        break;
    }
    if (fired) {
      due.push({ id: c.id, wake: c.wake, evidence: fired });
      nextState.conditions[stateKey] = {
        ...prior,
        lastFiredAt: now.toISOString(),
        ...(mtimeToRecord !== undefined ? { lastMtimeMs: mtimeToRecord } : {}),
      };
    }
  }
  return { due, nextState };
}

/**
 * The user-visible instruction for a woken turn: the due items, and nothing
 * else. The point of naming them is that the model's job narrows from "read
 * HEARTBEAT.md and work out what applies" to "do these two things" — which
 * is the context reduction, not just a nicety.
 */
export function buildWakeContext(due: DueItem[]): string {
  const lines = [
    "[EVOLVE HEARTBEAT] Evolve checked this bot's HEARTBEAT.json conditions " +
      "before this turn. Exactly these are due right now — do them, and skip " +
      "the rest of the HEARTBEAT.md checklist:",
    "",
  ];
  for (const item of due) {
    lines.push(`- ${item.wake}  (${item.id}: ${item.evidence})`);
  }
  lines.push(
    "",
    "Nothing else on the checklist is due. If none of the above needs a " +
      "message, reply with the single token NO_REPLY.",
  );
  return lines.join("\n");
}

// ── The operator-side floor ───────────────────────────────────────────────

/** Default maximum silence before a wake is forced regardless of conditions. */
export const DEFAULT_MAX_SILENCE_MS = 24 * 3_600_000;

/** ``86400000`` → ``"24h"``. Used verbatim in the floor's reason string. */
export function formatSilenceWindow(ms: number): string {
  if (ms % 3_600_000 === 0) return `${ms / 3_600_000}h`;
  return `${Math.round(ms / 60_000)}m`;
}

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
export function readMaxSilenceMs(sharedDir: string, botId: string): number {
  try {
    const raw = fs.readFileSync(path.join(sharedDir, "network.json"), "utf8");
    const network = JSON.parse(raw);
    const candidates = [
      network?.bots?.[botId]?.heartbeat?.max_silence,
      network?.heartbeat?.max_silence,
    ];
    for (const candidate of candidates) {
      if (candidate === undefined || candidate === null) continue;
      if (candidate === false || candidate === 0) return 0;
      const ms = parseInterval(candidate);
      if (ms !== null) return ms;
    }
  } catch {
    /* missing/unreadable network.json — keep the default floor. */
  }
  return DEFAULT_MAX_SILENCE_MS;
}

// ── The stateful checker ──────────────────────────────────────────────────

export interface HeartbeatDueCheckOptions {
  botId: string;
  sharedDir: string;
  logger?: { info: (m: string) => void; warn: (m: string) => void; debug?: (m: string) => void };
  probe?: FsProbe;
  now?: () => Date;
  /**
   * Test seam for the operator-side floor. Production leaves it unset and
   * the value comes from network.json via ``readMaxSilenceMs``, TTL-cached.
   */
  maxSilenceMs?: () => number;
}

export class HeartbeatDueCheck {
  private readonly botId: string;
  private readonly sharedDir: string;
  private readonly logger: NonNullable<HeartbeatDueCheckOptions["logger"]>;
  private readonly probe: FsProbe;
  private readonly now: () => Date;
  /** Config paths already warned about, so a bad file logs once per process. */
  private readonly warned = new Set<string>();
  /** Last observed conditions-file digest, per config path. */
  private readonly lastSha = new Map<string, string>();
  /** Config paths whose digest change has already been reported. */
  private readonly shaChangeWarned = new Set<string>();
  private readonly maxSilenceOverride?: () => number;
  private cachedMaxSilenceMs: number | null = null;
  private maxSilenceCheckedAt = 0;
  private static readonly MAX_SILENCE_CACHE_TTL_MS = 60_000;

  constructor(opts: HeartbeatDueCheckOptions) {
    this.botId = opts.botId;
    this.sharedDir = opts.sharedDir;
    this.logger = opts.logger ?? { info: () => {}, warn: () => {} };
    this.probe = opts.probe ?? realFsProbe;
    this.now = opts.now ?? (() => new Date());
    this.maxSilenceOverride = opts.maxSilenceMs;
  }

  /** The floor, TTL-cached so the hook path doesn't re-read network.json. */
  maxSilenceMs(): number {
    if (this.maxSilenceOverride) return this.maxSilenceOverride();
    const nowMs = Date.now();
    if (
      this.cachedMaxSilenceMs !== null &&
      nowMs - this.maxSilenceCheckedAt < HeartbeatDueCheck.MAX_SILENCE_CACHE_TTL_MS
    ) {
      return this.cachedMaxSilenceMs;
    }
    this.cachedMaxSilenceMs = readMaxSilenceMs(this.sharedDir, this.botId);
    this.maxSilenceCheckedAt = nowMs;
    return this.cachedMaxSilenceMs;
  }

  /** ``{sharedDir}/{botId}/turns`` — see the module docstring on placement. */
  private turnsDir(): string {
    return path.join(this.sharedDir, this.botId, "turns");
  }

  statePath(): string {
    return path.join(this.turnsDir(), "heartbeat-due-state.json");
  }

  conditionsPath(workspaceDir: string): string {
    return path.join(workspaceDir, CONDITIONS_FILENAME);
  }

  readState(): DueState {
    try {
      const doc = JSON.parse(fs.readFileSync(this.statePath(), "utf8"));
      if (doc && typeof doc === "object" && typeof doc.conditions === "object" && doc.conditions) {
        return {
          version: 1,
          conditions: doc.conditions as Record<string, PerConditionState>,
          ...(typeof doc.lastWokeAt === "string" ? { lastWokeAt: doc.lastWokeAt } : {}),
        };
      }
    } catch {
      /* absent or corrupt — an empty state means "everything is due", which
         is the fail-toward-running direction. */
    }
    return { version: 1, conditions: {} };
  }

  /** Commit ``nextState``. Called only on a real wake. Best-effort. */
  commitState(next: DueState): void {
    const target = this.statePath();
    try {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      const tmp = `${target}.tmp-${process.pid}`;
      fs.writeFileSync(tmp, JSON.stringify(next, null, 2) + "\n", { mode: 0o644 });
      fs.renameSync(tmp, target);
    } catch (err) {
      // A state we could not persist re-fires next tick — noisier, never
      // silent. Log at debug: on a pod where the dir belongs to another
      // gateway this would otherwise warn on every heartbeat.
      this.logger.debug?.(`Evolve heartbeat: could not persist due-state: ${err}`);
    }
  }

  /**
   * The decision. ``unavailable`` means "run the model, exactly as today".
   *
   * ``scope`` selects which conditions apply. A ``heartbeat`` trigger reads
   * the top-level ``conditions``; a ``cron`` trigger reads ONLY
   * ``cron[<ctx.jobId>]`` and is ``unavailable`` when that job has no entry,
   * so an ordinary cron job on a bot with a HEARTBEAT.json keeps running.
   */
  evaluate(workspaceDir: string | null | undefined, scope?: DueScope): DueVerdict {
    const trigger = String(scope?.trigger ?? "heartbeat").toLowerCase();
    const jobId = typeof scope?.jobId === "string" ? scope.jobId.trim() : "";
    const ws = typeof workspaceDir === "string" ? workspaceDir.trim() : "";
    if (!ws) {
      return {
        decision: "unavailable",
        due: [],
        reason: "no workspace dir on the hook context",
        warn: null,
      };
    }
    const cfgPath = this.conditionsPath(ws);
    let raw: string;
    try {
      raw = fs.readFileSync(cfgPath, "utf8");
    } catch (err) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code === "ENOENT") {
        // The default for every bot that has not opted in. Not a warning:
        // "no conditions file" is the documented way to keep today's
        // behaviour, and warning on it would shout at every quiet pod.
        return {
          decision: "unavailable",
          due: [],
          reason: `no ${CONDITIONS_FILENAME} beside HEARTBEAT.md`,
          warn: null,
        };
      }
      return {
        decision: "unavailable",
        due: [],
        reason: `${CONDITIONS_FILENAME} unreadable`,
        warn: `Evolve heartbeat: ${cfgPath} is unreadable (${code ?? err}) — running the model as usual`,
      };
    }
    // Digest EVERY read, before the parse can reject it: a file the bot
    // rewrote into garbage is exactly the change an operator wants to see.
    const sha = this.noteConditionsDigest(cfgPath, raw);
    const parsed = parseDueConditions(raw);
    if ("error" in parsed) {
      return {
        decision: "unavailable",
        due: [],
        reason: `${CONDITIONS_FILENAME} invalid`,
        warn:
          `Evolve heartbeat: ${cfgPath} is invalid — ${parsed.error}. Running the ` +
          `model as usual; fix the file to start skipping idle heartbeats.`,
      };
    }
    if (!parsed.ok.enabled) {
      return {
        decision: "unavailable",
        due: [],
        reason: `${CONDITIONS_FILENAME} has enabled:false`,
        warn: null,
      };
    }

    // ── Scope selection ────────────────────────────────────────────────────
    // A cron job is claimable only when the file names THAT job. Anything
    // else — no cron map, no entry for this job, no job id on the ctx —
    // returns unavailable and the job runs exactly as it does today.
    let conditions: DueCondition[];
    let cronJobId: string | null = null;
    if (trigger === "cron") {
      if (!jobId) {
        return {
          decision: "unavailable",
          due: [],
          reason: "cron trigger with no job id on the hook context",
          warn: null,
        };
      }
      const scoped = parsed.ok.cron[jobId];
      if (!scoped || scoped.length === 0) {
        return {
          decision: "unavailable",
          due: [],
          reason: `${CONDITIONS_FILENAME} declares no conditions for cron job ${jobId}`,
          warn: null,
        };
      }
      conditions = scoped;
      cronJobId = jobId;
    } else {
      conditions = parsed.ok.conditions;
      if (conditions.length === 0) {
        return {
          decision: "unavailable",
          due: [],
          reason: `${CONDITIONS_FILENAME} declares no conditions`,
          warn: null,
        };
      }
    }

    const now = this.now();
    const state = this.readState();
    const { due, nextState } = evaluateConditions(
      conditions,
      ws,
      state,
      now,
      this.probe,
    );
    const evaluated = conditions.length;
    if (due.length === 0) {
      // ── The floor ────────────────────────────────────────────────────────
      // Conditions said nothing is due. They are bot-writable, so before
      // honouring that we check the one bound the bot cannot reach: an
      // operator-side maximum silence, read from network.json. Past it, the
      // model runs whatever the file says.
      const floorMs = this.maxSilenceMs();
      const lastWokeMs = state.lastWokeAt ? Date.parse(state.lastWokeAt) : NaN;
      const silentTooLong =
        !Number.isFinite(lastWokeMs) || now.getTime() - lastWokeMs >= floorMs;
      if (floorMs > 0 && silentTooLong) {
        return {
          decision: "wake",
          due: [],
          reason: `floor: no wake in ${formatSilenceWindow(floorMs)}`,
          conditionsEvaluated: evaluated,
          nextState: { ...nextState, lastWokeAt: now.toISOString() },
          conditionsSha256: sha,
          cronJobId,
        };
      }
      return {
        decision: "skip",
        due: [],
        reason: `nothing due (${evaluated} ${evaluated === 1 ? "condition" : "conditions"} checked)`,
        conditionsEvaluated: evaluated,
        conditionsSha256: sha,
        cronJobId,
      };
    }
    return {
      decision: "wake",
      due,
      reason: `${due.length} of ${evaluated} due: ${due.map((d) => d.id).join(", ")}`,
      conditionsEvaluated: evaluated,
      nextState: { ...nextState, lastWokeAt: now.toISOString() },
      conditionsSha256: sha,
      cronJobId,
    };
  }

  /**
   * Record the conditions file's digest and warn ONCE per process when it
   * changes between ticks. The file is bot-writable; a rewrite that quiets
   * the bot would otherwise look identical to a quiet week.
   */
  private noteConditionsDigest(cfgPath: string, raw: string): string {
    const sha = crypto.createHash("sha256").update(raw).digest("hex");
    const prior = this.lastSha.get(cfgPath);
    this.lastSha.set(cfgPath, sha);
    if (prior && prior !== sha && !this.shaChangeWarned.has(cfgPath)) {
      this.shaChangeWarned.add(cfgPath);
      this.logger.warn(
        `Evolve heartbeat: ${cfgPath} changed while the gateway was running ` +
        `(${prior.slice(0, 12)} → ${sha.slice(0, 12)}). It is bot-writable and it ` +
        `decides whether this bot wakes — check the change was yours. Every ` +
        `decision record carries conditions_sha256.`,
      );
    }
    return sha;
  }

  /** Log an ``unavailable`` warning at most once per config path per process. */
  warnOnce(warn: string | null, key: string): void {
    if (!warn) return;
    if (this.warned.has(key)) return;
    this.warned.add(key);
    this.logger.warn(warn);
  }
}
