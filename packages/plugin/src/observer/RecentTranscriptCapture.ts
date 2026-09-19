/**
 * RecentTranscriptCapture
 *
 * Maintains a rolling buffer of raw user turn text per bot at
 * `{sharedDir}/metrics/{botId}/recent-transcripts.json`. The buffer feeds
 * `security_warden` (and any other generator that registers a consumer
 * through the redact-on-read wrapper) so credential exposures in recent
 * sessions can be detected and the user prompted to rotate.
 *
 * Privacy invariants (locked 2026-05-05):
 *   - Retention: at most 200 entries OR 48 hours, whichever caps first.
 *   - Roles: USER text only. Assistant replies are not captured.
 *   - Redaction: raw at capture; consumers requiring redacted text wrap
 *     reads with `generators.security_warden.redact`.
 *   - Opt-out: per-bot via `network.json` `bots[botId].securityScanning =
 *     false` (default true).
 *
 * On-disk format:
 *   [ {session_id, turn_index, ts, text}, ... ]
 *
 * The reader contract is defined by
 * `packages/analyzer/generators/security_warden/observe.py`.
 */

import * as fs from "fs";
import * as path from "path";
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";

const MAX_TURNS = 200;
const RETENTION_MS = 48 * 60 * 60 * 1000; // 48h
const NETWORK_CACHE_TTL_MS = 60_000;

interface CapturedTurn {
  session_id: string;
  turn_index: number;
  ts: string; // ISO 8601
  text: string;
}

export interface CaptureInput {
  sessionId: string;
  turnIndex: number;
  userText: string;
  ts?: string;
}

export class RecentTranscriptCapture {
  private readonly config: EvolveConfig;
  private readonly logger: PluginLogger;
  private _dirInitialized = false;
  // Cached opt-out flag from network.json. Refreshed lazily.
  private _enabled: boolean | null = null;
  private _enabledCheckedAt = 0;

  constructor(config: EvolveConfig, logger: PluginLogger) {
    this.config = config;
    this.logger = logger;
  }

  /**
   * Append a user turn to the rolling buffer. No-ops if scanning is disabled
   * for this bot, or if the text is empty / whitespace-only.
   */
  recordUserTurn(input: CaptureInput): void {
    if (!this.isEnabled()) return;

    const text = (input.userText ?? "").trim();
    if (!text) return;
    if (!input.sessionId || input.sessionId === "unknown") return;

    const entry: CapturedTurn = {
      session_id: input.sessionId,
      turn_index: input.turnIndex,
      ts: input.ts ?? new Date().toISOString(),
      text,
    };

    const dir = this.bufferDir();
    if (!this.ensureDir(dir)) return;

    const filePath = path.join(dir, "recent-transcripts.json");
    const tmpPath = `${filePath}.tmp`;

    let existing: CapturedTurn[] = [];
    try {
      const raw = fs.readFileSync(filePath, "utf8");
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) existing = parsed;
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code !== "ENOENT") {
        // Corrupt JSON — start fresh rather than crash. The capture buffer is
        // an ephemeral cache; losing it costs at most a 48h scanning gap.
        this.logger.warn(`Evolve: recent-transcripts.json read failed (${code ?? err}); starting fresh`);
      }
    }

    existing.push(entry);
    const trimmed = applyRetention(existing, Date.parse(entry.ts));

    try {
      fs.writeFileSync(tmpPath, JSON.stringify(trimmed));
      fs.renameSync(tmpPath, filePath);
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code === "EACCES" || code === "EPERM") {
        this.logger.debug(
          `Evolve: recent-transcripts dir for ${this.config.botId} owned by another gateway, skipping`
        );
        return;
      }
      this.logger.warn(`Evolve: failed to write recent-transcripts.json: ${err}`);
      try { fs.unlinkSync(tmpPath); } catch { /* best-effort cleanup */ }
    }
  }

  /** Force-refresh the cached opt-out state. Useful for tests. */
  invalidateConfigCache(): void {
    this._enabled = null;
    this._enabledCheckedAt = 0;
  }

  private isEnabled(): boolean {
    const now = Date.now();
    if (this._enabled !== null && now - this._enabledCheckedAt < NETWORK_CACHE_TTL_MS) {
      return this._enabled;
    }

    let enabled = true;
    try {
      const networkPath = path.join(this.config.sharedDir, "network.json");
      const raw = fs.readFileSync(networkPath, "utf8");
      const network = JSON.parse(raw);
      const botCfg = network?.bots?.[this.config.botId];
      // Default ON. Only an explicit `false` disables; missing key = enabled.
      if (botCfg && botCfg.securityScanning === false) {
        enabled = false;
      }
    } catch {
      // No network.json or unreadable — fail open (default-on policy).
    }

    this._enabled = enabled;
    this._enabledCheckedAt = now;
    return enabled;
  }

  private bufferDir(): string {
    return path.join(this.config.sharedDir, "metrics", this.config.botId);
  }

  private ensureDir(dir: string): boolean {
    if (this._dirInitialized) return true;
    try {
      fs.mkdirSync(dir, { recursive: true });
      this._dirInitialized = true;
      return true;
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code === "EACCES" || code === "EPERM") {
        this.logger.debug(
          `Evolve: metrics dir for ${this.config.botId} owned by another gateway, skipping`
        );
        return false;
      }
      this.logger.warn(`Evolve: failed to create metrics dir: ${err}`);
      return false;
    }
  }
}

/**
 * Apply the retention policy: keep at most MAX_TURNS most-recent entries,
 * AND drop anything older than RETENTION_MS relative to `nowMs`. Whichever
 * cap is tighter wins.
 *
 * Exported for unit tests.
 */
export function applyRetention(
  entries: CapturedTurn[],
  nowMs: number
): CapturedTurn[] {
  const cutoff = nowMs - RETENTION_MS;
  const fresh = entries.filter((e) => {
    const t = Date.parse(e.ts);
    return Number.isFinite(t) && t >= cutoff;
  });
  if (fresh.length <= MAX_TURNS) return fresh;
  return fresh.slice(fresh.length - MAX_TURNS);
}
