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
/**
 * Strip the gateway/proxy envelopes from a raw user message, returning
 * the text keyword routing should parse. Idempotent; returns "" for
 * null/undefined/empty input. Bare messages (e.g. Telegram member-bot
 * surfaces, which get no envelope) pass through unchanged apart from
 * trimming.
 */
export declare function unwrapUserMessage(raw: string | null | undefined): string;
//# sourceMappingURL=messageUnwrap.d.ts.map