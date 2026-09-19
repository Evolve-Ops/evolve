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
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
interface CapturedTurn {
    session_id: string;
    turn_index: number;
    ts: string;
    text: string;
}
export interface CaptureInput {
    sessionId: string;
    turnIndex: number;
    userText: string;
    ts?: string;
}
export declare class RecentTranscriptCapture {
    private readonly config;
    private readonly logger;
    private _dirInitialized;
    private _enabled;
    private _enabledCheckedAt;
    constructor(config: EvolveConfig, logger: PluginLogger);
    /**
     * Append a user turn to the rolling buffer. No-ops if scanning is disabled
     * for this bot, or if the text is empty / whitespace-only.
     */
    recordUserTurn(input: CaptureInput): void;
    /** Force-refresh the cached opt-out state. Useful for tests. */
    invalidateConfigCache(): void;
    private isEnabled;
    private bufferDir;
    private ensureDir;
}
/**
 * Apply the retention policy: keep at most MAX_TURNS most-recent entries,
 * AND drop anything older than RETENTION_MS relative to `nowMs`. Whichever
 * cap is tighter wins.
 *
 * Exported for unit tests.
 */
export declare function applyRetention(entries: CapturedTurn[], nowMs: number): CapturedTurn[];
export {};
//# sourceMappingURL=RecentTranscriptCapture.d.ts.map