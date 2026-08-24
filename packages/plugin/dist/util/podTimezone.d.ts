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
export declare function getPodTimezone(sharedDir: string): string;
export declare function _resetPodTimezoneCache(): void;
//# sourceMappingURL=podTimezone.d.ts.map