/**
 * Pod timezone resolver.
 *
 * Reads the pod-wide IANA timezone from {sharedDir}/network.json once at
 * first call and caches the value. Falls back to America/Los_Angeles if the
 * file is missing, unreadable, or has no `timezone` field.
 *
 * Storage stays UTC; this helper is for rendering only (date string keys,
 * UI-facing timestamps). Mirrors the analyzer's _resolve_tz() in measure.py
 * and the dashboard's podTz() in web/index.html — single source of truth is
 * the network.json `timezone` field.
 */

import * as fs from "fs";
import * as path from "path";

const DEFAULT_TZ = "America/Los_Angeles";

let cached: { sharedDir: string; tz: string } | null = null;

export function getPodTimezone(sharedDir: string): string {
  if (cached && cached.sharedDir === sharedDir) return cached.tz;
  let tz = DEFAULT_TZ;
  try {
    const raw = fs.readFileSync(path.join(sharedDir, "network.json"), "utf8");
    const cfg = JSON.parse(raw);
    if (typeof cfg.timezone === "string" && cfg.timezone.trim()) {
      tz = cfg.timezone.trim();
    }
  } catch {
    // file missing / unreadable / invalid JSON — keep default
  }
  cached = { sharedDir, tz };
  return tz;
}

// Test seam: clear the in-process cache (e.g. after rewriting network.json
// in a test). Production code should not need to call this.
export function _resetPodTimezoneCache(): void {
  cached = null;
}
