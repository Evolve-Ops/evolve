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
import type {
  RecommendationFormatter,
  Surface,
  FollowUpAction,
} from "./RecommendationFormatter.js";

export class KeywordHandler {
  constructor(
    private readonly client: BetterEngineClient,
    private readonly formatter: RecommendationFormatter,
  ) {}

  /**
   * Returns true if the message is the bare evo/evolve keyword.
   * Case-insensitive, trimmed, exact match only (§2.3 of keyword spec).
   *
   * Does NOT match "evo <subcommand>" — use parseEvoCommand for that.
   */
  isEvoKeyword(message: string): boolean {
    const normalized = message.trim().toLowerCase();
    return normalized === "evo" || normalized === "evolve";
  }

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
  parseEvoCommand(message: string): { subcommand: string; args: string; isBare: boolean } | null {
    const text = (message ?? "").trim();
    if (!text) return null;

    let stripped = text;
    const last = stripped[stripped.length - 1];
    if (".!?,;".includes(last)) {
      stripped = stripped.slice(0, -1).trimEnd();
    }
    const parts = stripped.split(/\s+/);
    if (parts.length === 0) return null;
    const head = parts[0].toLowerCase();
    if (head !== "evo" && head !== "evolve") return null;

    if (parts.length === 1) {
      return { subcommand: "evo", args: "", isBare: true };
    }
    const sub = parts[1].toLowerCase();
    const args = parts.slice(2).join(" ");
    return { subcommand: sub, args, isBare: false };
  }

  /**
   * Build the system prompt injection for the keyword fallback path.
   *
   * When rec is null (empty queue), injects the "all caught up" message.
   * The LLM is instructed to respond verbatim with the formatted content.
   */
  buildKeywordInjection(formattedMessage: string): string {
    return (
      `IMPORTANT: The user has typed 'evo' or 'evolve'. ` +
      `Respond ONLY with the following message, verbatim. ` +
      `Do not add commentary, framing, or any additional text:\n\n` +
      formattedMessage
    );
  }

  /**
   * Build the injection for a follow-up action (accept/reject/snooze/next/context).
   * The response message returned by handleFollowUp is injected the same way.
   */
  buildFollowUpInjection(responseMessage: string): string {
    return (
      `IMPORTANT: The user has replied to a pending recommendation. ` +
      `Respond ONLY with the following message, verbatim. ` +
      `Do not add commentary or framing:\n\n` +
      responseMessage
    );
  }

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
  buildStaySilentInjection(sentMessage?: string): string {
    const trimmed = (sentMessage ?? "").trim();
    const preview =
      trimmed.length > 600 ? trimmed.slice(0, 600) + "\n…(truncated)" : trimmed;
    const sentBlock = preview
      ? `\nThe user has ALREADY received this message from the Evolve plugin:\n` +
        `<<<BEGIN_DELIVERED_MESSAGE>>>\n${preview}\n<<<END_DELIVERED_MESSAGE>>>\n` +
        `Do NOT repeat, rephrase, expand, summarize, restate, or contradict ` +
        `any of the above. Do NOT prepend a header or append a follow-up ` +
        `question. The user can read what's already on their screen.\n`
      : "";
    return (
      `CRITICAL — STAY SILENT. DO NOT RESPOND TO THE USER'S 'evo' MESSAGE.\n\n` +
      `The Evolve plugin has already sent the response to the user out of ` +
      `band (Telegram Bot API). Anything you generate now arrives as a ` +
      `second, contradictory message.\n` +
      sentBlock +
      `\nYour ENTIRE response must be at most a single short word. Pick one: ` +
      `"" (empty), "." , or "OK". Nothing else. No bullet lists, no headers, ` +
      `no tables, no follow-up questions, no mentions of pending tasks, ` +
      `orphans, app posture, the shared directory, the admin UI, or any ` +
      `other Evolve internals. The plugin already handled the user's request.\n\n` +
      `If the user replies A/accept, S/snooze, or N/next on a subsequent ` +
      `turn, acknowledge briefly: "Got it", "Snoozed", or "Sure" respectively.`
    );
  }

  /**
   * Handle a follow-up action (accept/reject/snooze/next/context) given the
   * currently pending recommendation.
   *
   * Returns the response message to inject into the system prompt.
   */
  async handleFollowUp(
    action: FollowUpAction,
    pendingRec: any,
    botId: string,
    surface: Surface,
    channel: "telegram" | "other" = "other",
  ): Promise<{ message: string; nextRec: any | null; clearPending: boolean }> {
    switch (action) {
      case "accept": {
        if (pendingRec.bridge_strategy === "task_queue") {
          // Queue it for the admin; confirm to the user
          await this.client.addPendingAdminTask(
            botId,
            pendingRec,
            new Date().toISOString(),
          );
          await this.client.acceptRecommendation(pendingRec.id);
          return {
            message:
              "Got it — I've flagged this for your Evolve admin.\n" +
              "Next time you're in the admin dashboard (or message the Evolve bot), " +
              "it'll be at the top of the list.",
            nextRec: null,
            clearPending: true,
          };
        }
        // Bot-executable accept
        await this.client.acceptRecommendation(pendingRec.id);
        const acceptLabel = pendingRec.accept_label ?? "accept";
        return {
          message: `Got it — starting "${acceptLabel}" now.`,
          nextRec: null,
          clearPending: true,
        };
      }

      case "reject": {
        await this.client.rejectRecommendation(pendingRec.id);
        // Offer the next rec after rejection
        const next = await this.client.getNextRecommendation(
          botId,
          surface,
          pendingRec.id,
        );
        let message = "No problem — skipping that one.";
        if (next) {
          const nextMsg = this.formatter.formatMessage(next, surface, channel);
          message += "\n\nNext up:\n\n" + nextMsg;
        }
        return {
          message,
          nextRec: next,
          // Always clear the old rejected rec; nextRec (if any) is tracked
          // by the caller via the else-if chain in TurnObserver.
          clearPending: true,
        };
      }

      case "snooze": {
        await this.client.snoozeRecommendation(pendingRec.id);
        // Offer the next rec after snoozing
        const next = await this.client.getNextRecommendation(
          botId,
          surface,
          pendingRec.id,
        );
        let message = "Snoozed — I'll bring this back in a couple of days.";
        if (next) {
          const nextMsg = this.formatter.formatMessage(next, surface, channel);
          message += "\n\nNext up:\n\n" + nextMsg;
        }
        return {
          message,
          nextRec: next,
          // Always clear the old snoozed rec; nextRec (if any) is tracked
          // by the caller via the else-if chain in TurnObserver.
          clearPending: true,
        };
      }

      case "next": {
        // Record that the user dismissed the current rec (neutral signal)
        void this.client.recordIgnored(pendingRec.id);
        const next = await this.client.getNextRecommendation(
          botId,
          surface,
          pendingRec.id,
        );
        if (!next) {
          return {
            message:
              "You're all caught up 🎉 I'll let you know when there's something worth your attention.\n" +
              "Type 'evolve' any time to check again.",
            nextRec: null,
            clearPending: true,
          };
        }
        const nextMsg = this.formatter.formatMessage(next, surface, channel);
        return {
          message: nextMsg,
          nextRec: next,
          clearPending: false, // keep pending, updated to new rec
        };
      }

      case "context": {
        // Show the full context field of the pending rec
        const contextText =
          pendingRec.context?.trim() || "No additional context available.";
        // Re-show the options so the user can still act
        const options = this.formatter.formatOptions(pendingRec, undefined, channel);
        return {
          message: `${contextText}\n\n${options}`,
          nextRec: pendingRec, // still the same pending rec
          clearPending: false,
        };
      }

      default: {
        return {
          message: "Sorry, I didn't understand that response.",
          nextRec: pendingRec,
          clearPending: false,
        };
      }
    }
  }
}
