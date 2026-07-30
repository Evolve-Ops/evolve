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