/**
 * scheduledAttribution — AL-1.2's before_agent_run capture: join a
 * scheduled (cron-driven) turn to the app it serves and stamp the session
 * via ``AppAttribution.recordScheduled`` (design-app-attribution-2026-08-15
 * §4.1, resolved per AL-0.4). Two lanes, most-specific first:
 *
 *   Lane B — claim file. A wrapper that shells out to ``openclaw agent``
 *   mints a UUIDv4 session id, writes
 *   ``{sharedDir}/{botId}/app-runs/<uuid>.json`` = ``{app_id, label, ts}``,
 *   and passes ``--session-id <uuid>`` (the AL-0.4 contract, probe-verified).
 *   When the claim exists for this turn's sessionId we record it
 *   (source ``"claim_file"``) and consume the file. Claims are swept after
 *   24h so an orphaned claim (wrapper died before the turn ran) can never
 *   pile up. No live producer writes claims yet — the Python writer ships
 *   unwired in ``applications/install_helpers.py``.
 *
 *   Lane A — OC-native cron join. The gateway threads the cron job id
 *   through the hook ctx session key: observed live (OC 2026.7, canary
 *   proof) as `` agent:<agentId>:cron:<job.id>:run:<runId> ``; the bare
 *   `` cron:<job.id> `` form from the store internals (AL-0.4) is tolerated
 *   too — extraction is by exact ``cron`` token, not prefix.
 *   We resolve job id → job NAME from the bot's own cron store —
 *   ``~/.openclaw/cron/jobs.json`` when present (older OC; mtime-cached),
 *   else the SQLite store current OC migrated to
 *   (``~/.openclaw/state/openclaw.sqlite`` ``cron_jobs``, discovered live on
 *   the 2026-08-15 canary proof: at gateway start jobs.json is imported and
 *   renamed ``jobs.json.migrated``, so it no longer exists at runtime). The
 *   SQLite read is same-user, read-only, via ``node:sqlite`` (node ≥22.13;
 *   unavailable → this lane records nothing). Then name → app_id from
 *   ``{sharedDir}/{botId}/app-cron-map.json`` (written at deploy time by the
 *   ``_merge_cron_entries`` call site, keyed by name because Evolve owns no
 *   stable job id at materialization). Record source ``"oc_cron_map"``.
 *
 * No match at any step → record NOTHING (the turn stays honestly "none");
 * this module never guesses. Same fail-open contract as AppAttribution:
 * observation only — never throws into the turn, warns once per process per
 * reason.
 */

import * as fs from "node:fs";
import { createRequire } from "node:module";
import * as os from "node:os";
import * as path from "node:path";

import { recordScheduled } from "./AppAttribution.js";


/** Distinct source strings (design §3 — kept apart for calibration). */
export const SOURCE_OC_CRON_MAP = "oc_cron_map";
export const SOURCE_CLAIM_FILE = "claim_file";

export const CLAIM_DIR_NAME = "app-runs";
export const CRON_MAP_FILENAME = "app-cron-map.json";

/** Extract the cron job id from a gateway session key. Live OC 2026.7 keys a
 *  cron run `` agent:<agentId>:cron:<job.id>:run:<runId> `` (observed on the
 *  canary proof); older/internal forms use bare `` cron:<job.id> ``. Token
 *  scan (exact ``cron`` segment, take the next) covers both without ever
 *  matching a job id that merely STARTS with "cron" (e.g. the
 *  ``cron-migrated-*`` ids the SQLite migration mints). */
export function cronJobIdFromSessionKey(key: string): string | null {
  const parts = key.split(":");
  for (let i = 0; i + 1 < parts.length; i++) {
    if (parts[i] === "cron" && parts[i + 1] && parts[i + 1] !== "run") {
      return parts[i + 1];
    }
  }
  return null;
}
/** Claim files older than this are orphans (wrapper died before its turn
 *  fired, or a consumed-claim unlink failed) — sweep them. */
const CLAIM_MAX_AGE_MS = 24 * 60 * 60_000;
/** Coarse sweep cadence — at most once an hour per process. */
const SWEEP_MIN_INTERVAL_MS = 60 * 60_000;

/** Session ids come from the gateway / wrapper and key a file lookup, so
 *  constrain to a filename-safe charset (UUIDs pass trivially); anything
 *  else is skipped rather than sanitized — never guess, never traverse. */
const SAFE_SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;


interface AttributionLogger {
  warn(msg: string): void;
  debug(msg: string): void;
}

export interface ScheduledAttributionOpts {
  readonly sharedDir: string;
  readonly botId: string;
  readonly logger?: AttributionLogger;
  /** Test seam — defaults to ``os.homedir()`` (the bot's own home). */
  readonly homeDir?: string;
}


interface JsonCacheEntry {
  mtimeMs: number;
  size: number;
  data: unknown;
}

/** mtime+size-keyed parse cache for jobs.json / app-cron-map.json — both are
 *  read on every cron-triggered turn but change only on deploy or job edit. */
const _jsonCache: Map<string, JsonCacheEntry> = new Map();
/** job id → name resolved from the SQLite cron store; a job's name is
 *  effectively immutable for its id, so a short TTL only bounds staleness
 *  after a delete/re-create. */
const _sqliteNameCache: Map<string, { name: string | null; at: number }> = new Map();
const SQLITE_NAME_TTL_MS = 5 * 60_000;
const MAX_SQLITE_NAME_ENTRIES = 256;
const _warnedReasons: Set<string> = new Set();
let _lastSweepAt = 0;


function warnOnce(logger: AttributionLogger | undefined, reason: string, err: unknown): void {
  if (_warnedReasons.has(reason)) return;
  _warnedReasons.add(reason);
  try {
    logger?.warn(
      `Evolve scheduled-attribution: ${reason} — recording nothing (warns ` +
        `once per process per reason): ${err}`,
    );
  } catch {
    /* logging must never throw out of the hot path */
  }
}


function firstString(...candidates: unknown[]): string | null {
  for (const c of candidates) {
    if (typeof c === "string" && c) return c;
  }
  return null;
}


/** Read + parse a JSON file through the mtime cache. Missing file → null
 *  (cache entry dropped so a later create is seen). Unparseable → null,
 *  cached until the file changes (warn-once — a bad file must not re-parse
 *  and re-warn on every turn). */
function readJsonCached(filePath: string, logger?: AttributionLogger): unknown {
  let st: fs.Stats;
  try {
    st = fs.statSync(filePath);
  } catch {
    _jsonCache.delete(filePath);
    return null;
  }
  const cached = _jsonCache.get(filePath);
  if (cached && cached.mtimeMs === st.mtimeMs && cached.size === st.size) {
    return cached.data;
  }
  let data: unknown = null;
  try {
    data = JSON.parse(fs.readFileSync(filePath, "utf-8"));
  } catch (err) {
    warnOnce(logger, `unparseable JSON at ${filePath}`, err);
    data = null;
  }
  _jsonCache.set(filePath, { mtimeMs: st.mtimeMs, size: st.size, data });
  return data;
}


/** ``jobs.json`` → the job's ``name`` for a gateway-assigned job id, or null.
 *  Tolerates both the wrapped ``{jobs: [...]}`` shape the OC store writes and
 *  a bare array (defensive — never throw over shape drift). */
function jobNameById(jobsData: unknown, jobId: string): string | null {
  const list = Array.isArray(jobsData)
    ? jobsData
    : Array.isArray((jobsData as { jobs?: unknown })?.jobs)
      ? (jobsData as { jobs: unknown[] }).jobs
      : null;
  if (!list) return null;
  for (const entry of list) {
    const j = entry as { id?: unknown; name?: unknown };
    if (j && j.id === jobId) {
      return typeof j.name === "string" && j.name ? j.name : null;
    }
  }
  return null;
}


/** job id → name from the OC SQLite cron store (``cron_jobs``), the runtime
 *  source of truth on current OC (jobs.json is import-once). Same-user
 *  read-only open per cache miss; ``node:sqlite`` needs node ≥22.13 — where
 *  unavailable this returns null (warn-once) and the lane records nothing. */
function jobNameFromSqliteStore(
  home: string,
  jobId: string,
  logger?: AttributionLogger,
): string | null {
  const cached = _sqliteNameCache.get(jobId);
  if (cached && Date.now() - cached.at < SQLITE_NAME_TTL_MS) return cached.name;
  const dbPath = path.join(home, ".openclaw", "state", "openclaw.sqlite");
  let name: string | null = null;
  try {
    if (!fs.existsSync(dbPath)) return null;
    // Lazy, tolerant load: top-level ``import "node:sqlite"`` would crash the
    // whole plugin on older node; createRequire keeps the failure local.
    const requireHere = createRequire(import.meta.url);
    const { DatabaseSync } = requireHere("node:sqlite");
    const db = new DatabaseSync(dbPath, { readOnly: true });
    try {
      const row = db
        .prepare("SELECT name FROM cron_jobs WHERE job_id = ?")
        .get(jobId) as { name?: unknown } | undefined;
      if (row && typeof row.name === "string" && row.name) name = row.name;
    } finally {
      db.close();
    }
  } catch (err) {
    // Infra failure (no node:sqlite, locked DB, schema drift) — do not
    // negative-cache; the next turn may succeed after the condition clears.
    warnOnce(logger, "cron store (sqlite) read failed", err);
    return null;
  }
  if (_sqliteNameCache.size >= MAX_SQLITE_NAME_ENTRIES) {
    const oldest = _sqliteNameCache.keys().next().value;
    if (oldest !== undefined) _sqliteNameCache.delete(oldest);
  }
  _sqliteNameCache.set(jobId, { name, at: Date.now() });
  return name;
}


/** Best-effort removal of claim files past CLAIM_MAX_AGE_MS. Rate-limited to
 *  once per SWEEP_MIN_INTERVAL_MS per process; a missing dir is the normal
 *  case (no producer yet) and costs one statSync-shaped readdir failure. */
function sweepStaleClaims(claimDir: string, logger?: AttributionLogger): void {
  const now = Date.now();
  if (now - _lastSweepAt < SWEEP_MIN_INTERVAL_MS) return;
  _lastSweepAt = now;
  let names: string[];
  try {
    names = fs.readdirSync(claimDir);
  } catch {
    return; // no claim dir → no producer has ever run — nothing to sweep
  }
  for (const name of names) {
    if (!name.endsWith(".json")) continue;
    const file = path.join(claimDir, name);
    try {
      if (now - fs.statSync(file).mtimeMs > CLAIM_MAX_AGE_MS) fs.unlinkSync(file);
    } catch (err) {
      warnOnce(logger, "claim sweep unlink failed", err);
    }
  }
}


/** Lane B: consume ``{claimDir}/<sessionId>.json`` if present. Returns true
 *  only when an attribution was recorded. The claim is single-use — unlink
 *  best-effort whether it parsed or not (a malformed claim is consumed, not
 *  retried forever; the 24h sweep is the backstop when unlink fails). */
function consumeClaim(
  claimDir: string,
  sessionId: string,
  logger?: AttributionLogger,
): boolean {
  const file = path.join(claimDir, `${sessionId}.json`);
  let raw: string;
  try {
    raw = fs.readFileSync(file, "utf-8");
  } catch {
    return false; // no claim for this session — the overwhelmingly common case
  }
  let recorded = false;
  try {
    const claim = JSON.parse(raw) as { app_id?: unknown };
    const appId = typeof claim?.app_id === "string" ? claim.app_id.trim() : "";
    if (appId) {
      recordScheduled(sessionId, appId, SOURCE_CLAIM_FILE);
      recorded = true;
    } else {
      warnOnce(logger, `claim file ${file} carries no app_id`, "skipping");
    }
  } catch (err) {
    warnOnce(logger, `unparseable claim file ${file}`, err);
  }
  try {
    fs.unlinkSync(file);
  } catch (err) {
    warnOnce(logger, "claim unlink failed (sweep will collect it)", err);
  }
  return recorded;
}


/**
 * The before_agent_run capture. TurnObserver calls this (call-site lines
 * only — the module boundary rule) on every turn at every active tier;
 * non-scheduled turns exit on the first prefix/existence check. Never
 * throws.
 */
export function captureScheduledAttribution(
  event: unknown,
  ctx: unknown,
  opts: ScheduledAttributionOpts,
): void {
  try {
    const ev = event as { sessionId?: unknown } | null | undefined;
    const c = ctx as { sessionId?: unknown; sessionKey?: unknown } | null | undefined;
    // The stamp key must be the session UUID the annotation build later
    // resolves against (agent_end reads ctx.sessionId), NOT the gateway
    // session KEY — for a cron turn the key is "cron:<job.id>".
    const sessionId = firstString(ev?.sessionId, c?.sessionId);
    if (!sessionId || !SAFE_SESSION_ID.test(sessionId)) return;

    const claimDir = path.join(opts.sharedDir, opts.botId, CLAIM_DIR_NAME);
    sweepStaleClaims(claimDir, opts.logger);
    // Lane B first: a claim names THIS session explicitly — more specific
    // than the cron map, so it wins when both somehow match (brief §Lane B).
    if (consumeClaim(claimDir, sessionId, opts.logger)) return;

    // Lane A: gateway cron session key → job id → name → app_id.
    const key = firstString(c?.sessionKey);
    if (!key) return;
    const jobId = cronJobIdFromSessionKey(key);
    if (!jobId) return;
    const home = opts.homeDir ?? os.homedir();
    // jobs.json first (older OC keeps it live; also the cheap fixture path),
    // else the SQLite store current OC migrates it into at gateway start.
    const jobs = readJsonCached(
      path.join(home, ".openclaw", "cron", "jobs.json"),
      opts.logger,
    );
    const name =
      jobNameById(jobs, jobId) ?? jobNameFromSqliteStore(home, jobId, opts.logger);
    if (!name) return;
    const map = readJsonCached(
      path.join(opts.sharedDir, opts.botId, CRON_MAP_FILENAME),
      opts.logger,
    );
    const appId =
      map && typeof map === "object" && !Array.isArray(map)
        ? (map as Record<string, unknown>)[name]
        : undefined;
    if (typeof appId !== "string" || !appId.trim()) return;
    recordScheduled(sessionId, appId.trim(), SOURCE_OC_CRON_MAP);
  } catch (err) {
    warnOnce(opts.logger, "captureScheduledAttribution failed", err);
  }
}


/** Test helper — clear the parse/name caches, sweep timer, and warn-once set. */
export function _resetScheduledAttributionForTests(): void {
  _jsonCache.clear();
  _sqliteNameCache.clear();
  _warnedReasons.clear();
  _lastSweepAt = 0;
}
