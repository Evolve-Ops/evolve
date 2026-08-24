/**
 * RecommendationFormatter
 *
 * Formats recommendations for display in chat messages and parses user
 * follow-up replies. Covers §19.2 and §19.3 of the Better Engine spec.
 */
export class RecommendationFormatter {
    /**
     * Format a recommendation as a conversational chat message.
     *
     * Produces the message body from §19.2 — conversational tone, not
     * system-output feel. Includes the rec content followed by lettered
     * action options (for non-button channels) or a note about buttons (Telegram).
     */
    formatMessage(rec, surface, channel) {
        const title = this.getTitle(rec, surface);
        const detail = this.getDetail(rec, surface);
        const acceptLabel = rec.accept_label ?? "Accept";
        // Header line differs by surface
        let header;
        if (surface === "member_bot") {
            header = `Make ${rec.scope_id ?? "your bot"} better 💡`;
        }
        else {
            const score = rec.priority_score != null ? ` [${rec.priority_score}]` : "";
            const urgencyEmoji = this.urgencyEmoji(rec);
            header = `Pod recommendation${score} ${urgencyEmoji}`;
        }
        // Context field — shown as a supporting line when available
        const contextLine = rec.context && rec.context.trim()
            ? `\n${rec.context.trim()}`
            : "";
        // Action options
        const options = this.formatOptions(rec, acceptLabel, channel);
        return `${header}\n\n${title}\n\n${detail}${contextLine}\n\n${options}`;
    }
    /**
     * Format the lettered action options line.
     * For "other" (non-button) channels, returns lettered shortcuts.
     * For Telegram, returns a compact hint since buttons handle it.
     */
    formatOptions(rec, acceptLabelOverride, channel) {
        const acceptLabel = acceptLabelOverride ?? rec.accept_label ?? "Accept";
        if (channel === "telegram") {
            // Telegram uses inline keyboard buttons — just a minimal text fallback
            return `  A · ${acceptLabel.toLowerCase()}\n  S · snooze\n  N · next`;
        }
        // Non-button channels — lettered reply options (§19.2)
        return `  A · ${acceptLabel.toLowerCase()}\n  S · snooze\n  N · next`;
    }
    /**
     * Parse a user reply to detect a follow-up action intent (§19.3).
     *
     * Single-letter shortcuts (a, s, n) are only valid when pendingRecExists
     * is true — caller must pass this flag to avoid false positives.
     *
     * Returns null if the message is not a recognized follow-up.
     */
    parseReply(text, pendingRecExists = false) {
        const normalized = text.trim().toLowerCase();
        // Accept
        if (["yes", "accept", "do it", "ok", "sure"].includes(normalized)) {
            return "accept";
        }
        if (pendingRecExists && normalized === "a") {
            return "accept";
        }
        // Reject
        if (["no", "reject", "skip", "nope", "pass"].includes(normalized)) {
            return "reject";
        }
        // Single-letter "n" is ambiguous — only treat as "next" (not reject)
        // when pendingRecExists; see below.
        // Snooze
        if (["snooze", "later", "not now", "remind me"].includes(normalized)) {
            return "snooze";
        }
        if (pendingRecExists && normalized === "s") {
            return "snooze";
        }
        // Next
        if (["next", "more", "➡️"].includes(normalized)) {
            return "next";
        }
        if (pendingRecExists && normalized === "n") {
            return "next";
        }
        // Context
        if (["why", "context", "more info"].includes(normalized)) {
            return "context";
        }
        return null;
    }
    // ── Private helpers ─────────────────────────────────────────────────────────
    getTitle(rec, surface) {
        if (surface === "member_bot" && rec.member_bot_title) {
            return rec.member_bot_title;
        }
        return rec.title ?? "(no title)";
    }
    getDetail(rec, surface) {
        if (surface === "member_bot" && rec.member_bot_detail) {
            return rec.member_bot_detail;
        }
        return rec.detail ?? "";
    }
    urgencyEmoji(rec) {
        const tags = rec.tags ?? [];
        if (tags.includes("urgency:critical"))
            return "🔴";
        if (tags.includes("urgency:high"))
            return "🟠";
        if (tags.includes("urgency:medium"))
            return "🟡";
        return "🟢";
    }
}
//# sourceMappingURL=RecommendationFormatter.js.map