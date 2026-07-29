/**
 * RecommendationFormatter
 *
 * Formats recommendations for display in chat messages and parses user
 * follow-up replies. Covers §19.2 and §19.3 of the Better Engine spec.
 */
export type Surface = "admin" | "member_bot";
export type Channel = "telegram" | "other";
export type FollowUpAction = "accept" | "reject" | "snooze" | "next" | "context";
export declare class RecommendationFormatter {
    /**
     * Format a recommendation as a conversational chat message.
     *
     * Produces the message body from §19.2 — conversational tone, not
     * system-output feel. Includes the rec content followed by lettered
     * action options (for non-button channels) or a note about buttons (Telegram).
     */
    formatMessage(rec: any, surface: Surface, channel: Channel): string;
    /**
     * Format the lettered action options line.
     * For "other" (non-button) channels, returns lettered shortcuts.
     * For Telegram, returns a compact hint since buttons handle it.
     */
    formatOptions(rec: any, acceptLabelOverride?: string, channel?: Channel): string;
    /**
     * Parse a user reply to detect a follow-up action intent (§19.3).
     *
     * Single-letter shortcuts (a, s, n) are only valid when pendingRecExists
     * is true — caller must pass this flag to avoid false positives.
     *
     * Returns null if the message is not a recognized follow-up.
     */
    parseReply(text: string, pendingRecExists?: boolean): FollowUpAction | null;
    private getTitle;
    private getDetail;
    private urgencyEmoji;
}
//# sourceMappingURL=RecommendationFormatter.d.ts.map