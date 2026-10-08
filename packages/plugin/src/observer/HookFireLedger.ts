/**
 * HookFireLedger — per-hour count of `before_model_resolve` fires, INCLUDING
 * fires on Evolve's own subagent sessions and early-returning ones.
 *
 * Early warning for recursion (the 2026-09-08 loop: 461 fires in 3 minutes on
 * a bot whose steady state is a dozen a day). Python reads the file.
 *
 *   {sharedDir}/{botId}/turns/hook-fires-<YYYY-MM-DD>.json   (UTC date)
 *   {"schema_version":1,"bot_id":"…","date":"YYYY-MM-DD",
 *    "hours":{"00":{"count":12,"other":0,"keys":{"<sessionKey>":{"n":10,"prefix":"hello"}}}}}
 *
 * Never throws into the hook; every failure is swallowed.
 */

import * as fs from "fs";
import * as path from "path";

export const HOOK_FIRE_MAX_KEYS_PER_HOUR = 50;
export const HOOK_FIRE_PREFIX_CHARS = 80;
export const HOOK_FIRE_FLUSH_INTERVAL_MS = 10_000;

interface KeyEntry { n: number; prefix: string }
interface HourEntry { count: number; other: number; keys: Record<string, KeyEntry> }

export interface HookFireLedgerOptions {
  sharedDir: string;
  botId: string;
  /** Injected clock (epoch ms). Default Date.now. */
  now?: () => number;
  /** Injected directory override (defaults to {sharedDir}/{botId}/turns). */
  dir?: string;
  debug?: (msg: string) => void;
  /** Set false in tests to skip the background timer. Default true. */
  autoFlush?: boolean;
}

export class HookFireLedger {
  private readonly botId: string;
  private readonly dir: string;
  private readonly now: () => number;
  private readonly debug: (msg: string) => void;
  private readonly autoFlush: boolean;
  private readonly days = new Map<string, Record<string, HourEntry>>();
  private readonly loaded = new Set<string>();
  /** Dates with unflushed counts — only these are rewritten, so a long-lived
   *  gateway does not rewrite every past day's file every flush. */
  private readonly dirtyDates = new Set<string>();
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(opts: HookFireLedgerOptions) {
    this.botId = opts.botId;
    this.dir = opts.dir ?? path.join(opts.sharedDir, opts.botId, "turns");
    this.now = opts.now ?? Date.now;
    this.debug = opts.debug ?? (() => {});
    this.autoFlush = opts.autoFlush !== false;
  }

  private fileFor(date: string): string {
    return path.join(this.dir, `hook-fires-${date}.json`);
  }

  /** Load the day's on-disk file once, so a restart does not zero counts. */
  private hoursFor(date: string): Record<string, HourEntry> {
    let hours = this.days.get(date);
    if (!hours) {
      hours = {};
      this.days.set(date, hours);
    }
    if (!this.loaded.has(date)) {
      this.loaded.add(date);
      try {
        const raw = JSON.parse(fs.readFileSync(this.fileFor(date), "utf-8"));
        const disk = raw?.hours;
        if (disk && typeof disk === "object") {
          for (const [hh, h] of Object.entries<any>(disk)) {
            if (!h || typeof h !== "object") continue;
            const keys: Record<string, KeyEntry> = {};
            for (const [k, v] of Object.entries<any>(h.keys ?? {})) {
              keys[k] = {
                n: Number(v?.n) || 0,
                prefix: typeof v?.prefix === "string" ? v.prefix : "",
              };
            }
            hours[hh] = {
              count: Number(h.count) || 0,
              other: Number(h.other) || 0,
              keys,
            };
          }
        }
      } catch { /* no file / unreadable — start empty */ }
    }
    return hours;
  }

  /** Count one fire. Never throws. */
  record(sessionKey: unknown, userMessage: unknown): void {
    try {
      const d = new Date(this.now());
      const iso = d.toISOString();
      const date = iso.slice(0, 10);
      const hh = iso.slice(11, 13);
      const hours = this.hoursFor(date);
      const h = hours[hh] ?? (hours[hh] = { count: 0, other: 0, keys: {} });
      h.count += 1;
      const key = typeof sessionKey === "string" && sessionKey ? sessionKey : "<none>";
      const existing = h.keys[key];
      if (existing) {
        existing.n += 1;
      } else if (Object.keys(h.keys).length < HOOK_FIRE_MAX_KEYS_PER_HOUR) {
        const prefix = typeof userMessage === "string"
          ? userMessage.slice(0, HOOK_FIRE_PREFIX_CHARS)
          : "";
        h.keys[key] = { n: 1, prefix };
      } else {
        h.other += 1;
      }
      this.dirtyDates.add(date);
      this.schedule();
    } catch (err) {
      this.debug(`Evolve: hook-fire ledger record failed: ${err}`);
    }
  }

  private schedule(): void {
    if (!this.autoFlush || this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, HOOK_FIRE_FLUSH_INTERVAL_MS);
    this.timer.unref?.();
  }

  /** Write every dirty day to disk (atomic tmp+rename). Never throws. */
  flush(): void {
    if (this.dirtyDates.size === 0) return;
    try {
      fs.mkdirSync(this.dir, { recursive: true });
    } catch { /* write below will report */ }
    for (const date of [...this.dirtyDates]) {
      const hours = this.days.get(date);
      if (!hours) { this.dirtyDates.delete(date); continue; }
      const dest = this.fileFor(date);
      const tmp = `${dest}.${process.pid}.tmp`;
      try {
        const body = {
          schema_version: 1,
          bot_id: this.botId,
          date,
          hours,
        };
        fs.writeFileSync(tmp, JSON.stringify(body) + "\n", { mode: 0o644 });
        fs.renameSync(tmp, dest);
        this.dirtyDates.delete(date);
      } catch (err) {
        this.debug(`Evolve: hook-fire ledger flush failed: ${err}`);
        try { fs.unlinkSync(tmp); } catch { /* ignore */ }
      }
    }
  }
}
