/**
 * SetTierTool — `session.set_tier` MCP tool (spec § 4.1).
 *
 * Bots call this when the user explicitly asks for more or less reasoning
 * capability ("think harder", "be thorough", "just be quick"). The tool
 * is the messaging-app surface equivalent of the admin-UI tier chip
 * (Auto/Fast/Standard/Power per PR #1629 / spec-user-tier-control-2026-05-26).
 *
 * Both surfaces share the same underlying primitive: ModelRouter.setUserTier.
 * The MCP tool calls into it from the plugin side; the chip path calls
 * into it from the gateway HTTP path. Effects are identical.
 *
 * **The bot never sees or sets consent_source.** It's purely a server-
 * side classification of the consent's origin (spec § 4.1 update). The
 * tool handler determines whether the call is:
 *   - "ask_hint_agreed" — there's an active tier1 ask-hint in session
 *     state (Phase 2 cascade controller produces these); bot is
 *     forwarding user agreement
 *   - "bot_initiated" — no ask-hint context; bot is making the call
 *     based on its own reading of user intent
 *
 * Phase 2 first-cut scope: ask-hint tracking isn't wired yet (cascade
 * controller doesn't exist as of this PR), so consent_source is always
 * "bot_initiated" for now. When the cascade controller ships and
 * starts emitting ask-hints, this handler will check for them.
 *
 * **Tier1 evidence gate (spec § 4.1 update, round-2 cost F9).** A future
 * iteration of this tool will gate `choice = "power"` from bot_initiated
 * on either: (a) an active ask-hint, OR (b) a strong tier-power signal
 * in the user's recent message. Deferred from this initial Phase 2 PR
 * to keep scope contained — the gate requires access to session
 * conversation state which is best implemented alongside the cascade
 * controller integration. See SetTierTool TODO at the bottom.
 *
 * Per-bot opt-out is consistent with the existing UI chip:
 * `tiers.json::userTierOverride.enabled = false` disables both surfaces.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import type { ModelRouter } from "../observer/ModelRouter.js";
export declare const SetTierToolParamsSchema: import("@sinclair/typebox").TObject<{
    choice: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"fast">, import("@sinclair/typebox").TLiteral<"standard">, import("@sinclair/typebox").TLiteral<"power">, import("@sinclair/typebox").TLiteral<"max">, import("@sinclair/typebox").TLiteral<"auto">]>;
    reason: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
}>;
export type SetTierToolParams = Static<typeof SetTierToolParamsSchema>;
export interface SetTierToolDeps {
    botId: string;
    modelRouter: ModelRouter;
    /**
     * Optional: returns true if a tier1 ask-hint was emitted by the
     * cascade controller within the last `tier1_ask_no_response_turns`
     * (default 3) turns of this session. Always false in this Phase 2
     * first-cut PR (ask-hints don't exist yet). Wired in a follow-on
     * PR when the cascade controller produces ask-hints.
     */
    hasRecentAskHint?: (sessionKey: string) => boolean;
}
export declare function createSetTierToolFactory(deps: SetTierToolDeps, logger: PluginLogger): (ctx: {
    sessionKey?: string;
    sessionId?: string;
    messageChannel?: string;
    agentId?: string;
}) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        choice: import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"fast">, import("@sinclair/typebox").TLiteral<"standard">, import("@sinclair/typebox").TLiteral<"power">, import("@sinclair/typebox").TLiteral<"max">, import("@sinclair/typebox").TLiteral<"auto">]>;
        reason: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        content: Array<{
            type: "text";
            text: string;
        }>;
        isError?: boolean;
    }>;
};
//# sourceMappingURL=SetTierTool.d.ts.map