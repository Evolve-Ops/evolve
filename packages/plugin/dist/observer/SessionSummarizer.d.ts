/**
 * SessionSummarizer
 *
 * Generates a structured session-level summary when a session ends.
 * Written to the same annotation JSONL as turn annotations, as a
 * "session_summary" record.
 *
 * Design goals:
 *   - Capture outcome, complexity, capabilities invoked, promises made
 *   - Detect efficiency problems (high turn count for low complexity)
 *   - Use keyword heuristics for speed/cost; LLM for outcome extraction only
 *   - Keep cost minimal: one optional tier3 call per session, only last 2 turns
 *
 * Schema (type: "session_summary"):
 * {
 *   schema_version: 2,
 *   type: "session_summary",
 *   session_id: string,
 *   ts: string,
 *   bot_id: string,
 *   turn_count: number,
 *   session_class: "productive" | "maintenance" | "ambiguous",
 *   tier_confidence: number,
 *   outcome: string,              — what was accomplished (LLM-extracted or inferred)
 *   complexity: "low"|"medium"|"high",
 *   applications_invoked: string[],
 *   promises_made: string[],      — things the bot committed to do
 *   correction_count: number,
 *   efficiency_flag: boolean,     — true if turns >> expected for complexity
 *   total_input_tokens: number,
 *   total_output_tokens: number,
 *   recurring_request?: {label, requester, hour},  — conversation-only
 *                        evidence (design §7.1a). Present ONLY for
 *                        human-initiated sessions carrying a keyable
 *                        ask, from a requester who has not opted out.
 *                        Absent is the common case and means nothing
 *                        was observed — never "observed nothing".
 * }
 */
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
export interface TurnRecord {
    userMessage: string;
    assistantMessage: string;
    session_class: string;
    class_confidence: number;
    correction_detected: boolean;
    input_tokens: number;
    output_tokens: number;
    role?: string;
    content?: string;
    turnId?: string;
    /** Who sent THIS turn, resolved from the senderRegistry at the moment
     *  the turn was recorded (design §7.1a / review finding).
     *
     *  Captured per-turn rather than read at session-end for three reasons,
     *  each of which was a real defect:
     *    - the label comes from the FIRST user turn, so a session-end read
     *      would key one person's ask under whoever spoke LAST — in a group
     *      chat that both misattributes the request and bypasses the
     *      per-identity do-not-track gate, which would then be checked
     *      against the wrong identity;
     *    - the `session_end` hook's ctx carries no `runId` at all, so that
     *      path could never resolve a sender and the field would appear or
     *      not depending on which of two racing paths won;
     *    - the registry TTLs entries out after 5 minutes, so a session
     *      longer than that could no longer resolve its own opening turn. */
    requester?: {
        platform: string | null;
        senderId: string | null;
    } | null;
}
export declare class SessionSummarizer {
    private config;
    private logger;
    private api;
    constructor(config: EvolveConfig, logger: PluginLogger, api: any);
    summarize(sessionId: string, turns: TurnRecord[], writeFn: (record: Record<string, unknown>) => void): Promise<void>;
    private extractOutcomeLLM;
}
//# sourceMappingURL=SessionSummarizer.d.ts.map