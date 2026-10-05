/**
 * KeywordHandler
 *
 * Handles evo/evolve keyword detection and follow-up action routing for the
 * Better Engine messaging surface (§19 of the Better Engine spec).
 *
 * Because before_agent_run is not yet in the SDK, this uses the system-prompt
 * injection fallback: the keyword instruction is injected via before_model_resolve,
 * and the LLM responds verbatim with the formatted recommendation.
 */
import type { BetterEngineClient } from "./BetterEngineClient.js";
import type { RecommendationFormatter, Surface, FollowUpAction } from "./RecommendationFormatter.js";
export declare class KeywordHandler {
    private readonly client;
    private readonly formatter;
    constructor(client: BetterEngineClient, formatter: RecommendationFormatter);
    /**
     * Returns true if the message is the bare evo/evolve keyword.
     * Case-insensitive, trimmed, exact match only (§2.3 of keyword spec).
     *
     * Does NOT match "evo <subcommand>" — use parseEvoCommand for that.
     */
    isEvoKeyword(message: string): boolean;
    /**
     * Parse an `evo`-prefixed user message into a subcommand structure.
     *
     * Returns null if the message is not an evo command at all. Otherwise:
     *   - Bare "evo"/"evolve" → { subcommand: "evo", args: "", isBare: true }
     *   - "evo help"          → { subcommand: "help", args: "", isBare: false }
     *   - "evo default better"→ { subcommand: "default", args: "better", isBare: false }
     *
     * Tolerates a single trailing punctuation char on the bare form ("evo.",
     * "evo!"), which is common in chat. Subcommand name is lowercased; args
     * keep their original case (the dispatcher decides).
     *
     * Mirrors `evolve_admin.evo.subcommands.parse` on the Python side.
     */
    parseEvoCommand(message: string): {
        subcommand: string;
        args: string;
        isBare: boolean;
    } | null;
    /**
     * Build the system prompt injection for the keyword fallback path.
     *
     * When rec is null (empty queue), injects the "all caught up" message.
     * The LLM is instructed to respond verbatim with the formatted content.
     */
    buildKeywordInjection(formattedMessage: string): string;
    /**
     * Build the injection for a follow-up action (accept/reject/snooze/next/context).
     * The response message returned by handleFollowUp is injected the same way.
     */
    buildFollowUpInjection(responseMessage: string): string;
    /**
     * Build a "stay silent" injection used when the plugin has dispatched the
     * response directly to the user (e.g. via Telegram Bot API). The LLM
     * should NOT also generate a response — that would create a duplicate or
     * a confusing second message that contradicts the rec.
     *
     * Uses strong, repeated directives because earlier "respond verbatim" style
     * instructions were observed being ignored by the model on Admin_bot's setup
     * (LLM hallucinated "no pending tasks" responses despite the systemAppend).
     *
     * Pass `sentMessage` so the LLM knows exactly what the user just received
     * and doesn't try to fill the void with hallucinated framing (orphan-claim
     * tables, "App Posture notices", etc. were observed). With the preview in
     * hand the model has nothing to invent and a clear "don't repeat this" anchor.
     */
    buildStaySilentInjection(sentMessage?: string): string;
    /**
     * Handle a follow-up action (accept/reject/snooze/next/context) given the
     * currently pending recommendation.
     *
     * Returns the response message to inject into the system prompt.
     */
    handleFollowUp(action: FollowUpAction, pendingRec: any, botId: string, surface: Surface, channel?: "telegram" | "other"): Promise<{
        message: string;
        nextRec: any | null;
        clearPending: boolean;
    }>;
}
//# sourceMappingURL=KeywordHandler.d.ts.map