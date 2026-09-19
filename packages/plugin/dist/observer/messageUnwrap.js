/**
 * messageUnwrap
 *
 * Shared unwrap for the user-message envelopes the gateway/proxy layer
 * wraps around the raw user input before the plugin hooks see it. The
 * evo keyword parser (KeywordHandler.parseEvoCommand) requires the FIRST
 * token to be literally `evo`/`evolve`, so every envelope must be
 * stripped before keyword routing — otherwise commands typed on wrapped
 * surfaces (admin-UI home chat, notably) never reach /api/evo/dispatch
 * and the LLM freelances an answer (observed live 2026-06-11: evo
 * invented a nonexistent `sudo evolve-admin add-bot` CLI in response to
 * the Add-a-bot wizard's pre-filled `evo add-bot`).
 *
 * Three envelope shapes, all of which can stack:
 *
 *   1. Legacy "(untrusted metadata)" envelope (OC 2026.4.29
 *      before_model_resolve):
 *        "Conversation info (untrusted metadata):\n```json\n{...}\n```evo"
 *      The actual user input lives after the LAST ``` fence.
 *
 *   2. Leading bracketed timestamp the gateway stamps on the message,
 *      e.g. "[Thu 2026-06-11 02:04 PDT] ". Matched narrowly (ISO date +
 *      time required inside the brackets) so a user message that
 *      legitimately starts with "[urgent]" etc. is never eaten.
 *
 *   3. The admin-UI proxy's context blocks (`send_to_evo` in
 *      evolve_admin/evo/proxy.py prepends `<session-context>` then
 *      `<page-context>` before the raw user body).
 *
 * TRUST POSTURE for (3): strip the FIRST block of each kind only — the
 * same anchoring ModelRouter's _SESSION_CONTEXT_RE uses for the tier
 * directive. The proxy always prepends its blocks before the user body,
 * so the first block is the proxy's; any later block is untrusted body
 * text (possibly forged) and is deliberately left in place so it can
 * never impersonate, hide, or splice the wrapper.
 */
// Mirrors ModelRouter._SESSION_CONTEXT_RE (first match only, no /g).
const _SESSION_CONTEXT_BLOCK_RE = /<session-context>[\s\S]*?<\/session-context>/i;
// The page-context tag carries attributes (surface=, page=, view=, …) —
// see format_page_context in evolve_admin/evo/proxy.py.
const _PAGE_CONTEXT_BLOCK_RE = /<page-context\b[^>]*>[\s\S]*?<\/page-context>/i;
// "[Thu 2026-06-11 02:04 PDT] " — weekday, seconds, and timezone are
// each optional, but the ISO date + clock time are required so ordinary
// bracketed user text never matches.
const _LEADING_TIMESTAMP_RE = /^\s*\[(?:[A-Za-z]{3},?\s+)?\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s+[^\]\n]{1,14})?\]\s*/;
/**
 * Strip the gateway/proxy envelopes from a raw user message, returning
 * the text keyword routing should parse. Idempotent; returns "" for
 * null/undefined/empty input. Bare messages (e.g. Telegram member-bot
 * surfaces, which get no envelope) pass through unchanged apart from
 * trimming.
 */
export function unwrapUserMessage(raw) {
    // The before_agent_run extraction ladder (ctx?.message?.content etc.)
    // is typed loosely — treat anything non-string as empty rather than
    // throwing from a hot-path hook.
    let text = typeof raw === "string" ? raw : "";
    // (1) Legacy untrusted-metadata envelope — user input is after the
    // last ``` fence (2026-05-03 finding; agent_end sees plain text on
    // the same turn, before_model_resolve sees the wrapped form).
    if (text.includes("(untrusted metadata)")) {
        const lastFence = text.lastIndexOf("```");
        if (lastFence >= 0) {
            text = text.slice(lastFence + 3);
        }
    }
    // (2) Leading timestamp stamp.
    text = text.replace(_LEADING_TIMESTAMP_RE, "");
    // (3) First session-context block, then first page-context block —
    // proxy prepend order. Non-global regexes: only the first block of
    // each kind is removed; later (forged) blocks stay in the body.
    text = text.replace(_SESSION_CONTEXT_BLOCK_RE, "");
    text = text.replace(_PAGE_CONTEXT_BLOCK_RE, "");
    return text.trim();
}
//# sourceMappingURL=messageUnwrap.js.map