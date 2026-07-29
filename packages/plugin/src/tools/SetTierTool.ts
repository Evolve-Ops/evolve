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

import { Type, Static } from "@sinclair/typebox";

import type { PluginLogger } from "openclaw/plugin-sdk/types";

import type { ModelRouter } from "../observer/ModelRouter.js";

// ── Schema ──────────────────────────────────────────────────────────────────

export const SetTierToolParamsSchema = Type.Object({
  choice: Type.Union(
    [
      Type.Literal("fast"),
      Type.Literal("standard"),
      Type.Literal("power"),
      Type.Literal("max"),
      Type.Literal("auto"),
    ],
    {
      description:
        "fast = the quick/cheap role (Haiku-class), standard = the default " +
        "workhorse role (Sonnet-class), power = the high-capability role " +
        "(Opus-class; use sparingly), max = the frontier model " +
        "(Fable-class) at ~2x power cost — only on an explicit user " +
        "request, never the bot's own choice, auto = clear any prior " +
        "choice and hand control back to the cascade. Match the user's " +
        "expressed intent; if unclear, prefer 'auto'.",
    },
  ),
  reason: Type.Optional(
    Type.String({
      description:
        "Brief paraphrase (1-2 sentences) of the user's intent that led " +
        "you to call this. Helps the operator audit tier-change decisions " +
        "in the cascade log. NOT exposed back to the user.",
      maxLength: 500,
    }),
  ),
});

export type SetTierToolParams = Static<typeof SetTierToolParamsSchema>;

const DESCRIPTION = [
  "Set the model capability tier for the rest of this session.",
  "",
  "Call when the user explicitly asks for more or less reasoning",
  "capability:",
  "  - 'think harder', 'this is important', 'be thorough' → choice='power'",
  "  - 'use the absolute best / frontier model', 'spare no expense' → choice='max'",
  "  - 'just be quick', 'don't overthink this', 'simple answer' → choice='fast'",
  "  - 'standard is fine', or to revert to default → choice='standard'",
  "  - 'back to normal', after a complex sub-task is done → choice='auto'",
  "",
  "'max' is the frontier model at ~2x the cost of 'power'. Only set it",
  "on an EXPLICIT user request for the very best model — never your own",
  "self-assessed difficulty. It is blocked for bot-initiated calls by",
  "default.",
  "",
  "Don't surface 'tier' or model names to the user. Translate:",
  "  - 'Sure, I'll think this through more carefully.' (power)",
  "  - 'OK, I'll keep it brief.' (fast)",
  "",
  "The cascade handles automatic escalation on detected struggle —",
  "only call this for explicit user requests, not for self-assessed",
  "difficulty. Returns the resolved choice and the consent classification.",
].join("\n");

// ── Result construction ─────────────────────────────────────────────────────

interface SetTierResult {
  ok: boolean;
  applied_choice?: string;
  consent_source?: string;
  error?: string;
  // L6-P1: tier1 cost-gate metadata, present only when the operator's
  // userTierOverride config caused the tool to downgrade choice="power"
  // → "standard". Absent on the happy path.
  requested_choice?: string;
  tier1_blocked_reason?: string;
  tier1_blocked_detail?: string;
}

function _resultContent(r: SetTierResult): {
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
} {
  if (!r.ok) {
    return {
      content: [{ type: "text", text: `session_set_tier: ${r.error}` }],
      isError: true,
    };
  }
  // Success body. Include the L6-P1 cost-gate fields when they're
  // populated so callers can convey "your power request was downgraded
  // because <reason>" back to the user.
  const body: Record<string, unknown> = {
    ok: true,
    applied_choice: r.applied_choice,
    consent_source: r.consent_source,
  };
  if (r.requested_choice !== undefined) body.requested_choice = r.requested_choice;
  if (r.tier1_blocked_reason !== undefined) body.tier1_blocked_reason = r.tier1_blocked_reason;
  if (r.tier1_blocked_detail !== undefined) body.tier1_blocked_detail = r.tier1_blocked_detail;
  return {
    content: [{ type: "text", text: JSON.stringify(body) }],
  };
}

// ── Factory ─────────────────────────────────────────────────────────────────

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

export function createSetTierToolFactory(
  deps: SetTierToolDeps,
  logger: PluginLogger,
) {
  const { botId, modelRouter, hasRecentAskHint } = deps;

  return (ctx: {
    sessionKey?: string;
    sessionId?: string;
    messageChannel?: string;
    agentId?: string;
  }) => {
    return {
      // RENAMED 2026-05-28: was "session.set_tier" until Anthropic
      // enforced tool-name regex ^[a-zA-Z0-9_-]{1,128}$ server-side
      // (dot rejected). Underscore form is the canonical name now —
      // every cross-reference (manifest.contracts.tools at
      // packages/plugin/openclaw.plugin.json, prompts, docs, the log
      // messages below) MUST match this string exactly. PR #1742's
      // contractsToolsManifest test guards manifest↔code alignment.
      name: "session_set_tier",
      description: DESCRIPTION,
      parameters: SetTierToolParamsSchema,
      async execute(_toolCallId: string, rawParams: unknown) {
        const params = rawParams as SetTierToolParams;

        // ── Input validation ────────────────────────────────────────────────
        if (!params || typeof params !== "object") {
          return _resultContent({ ok: false, error: "missing params" });
        }
        const choice = (params.choice ?? "").trim().toLowerCase();
        if (!["fast", "standard", "power", "max", "auto"].includes(choice)) {
          return _resultContent({
            ok: false,
            error: `unknown choice '${params.choice}'. Valid: fast | standard | power | max | auto`,
          });
        }

        // ── Session context ─────────────────────────────────────────────────
        const sessionKey = ctx.sessionKey ?? ctx.sessionId;
        if (!sessionKey) {
          logger.warn(
            "session_set_tier: ctx.sessionKey/sessionId missing — cannot " +
            "scope tier override. Bot's call has no effect.",
          );
          return _resultContent({
            ok: false,
            error: "session context missing; tier change has no effect",
          });
        }

        // ── Consent-source classification (server-side, spec § 4.1) ─────────
        // "auto" clears the override entirely → no consent_source recorded.
        // For all other choices, determine the source server-side.
        // Phase 2 first-cut: hasRecentAskHint is always undefined → all
        // calls classify as bot_initiated. When ask-hints exist, this
        // branches.
        let consentSource: "ui_chip" | "ask_hint_agreed" | "bot_initiated" = "bot_initiated";
        if (hasRecentAskHint && hasRecentAskHint(String(sessionKey))) {
          consentSource = "ask_hint_agreed";
        }

        // ── Per-role cost gate (L6-P1 + spec §max semantics #6) ────────────
        // Bot-initiated escalation to a capped role (power / max) is gated
        // by the operator's config + per-role daily caps. Without this a
        // chatty bot could pin Opus (or, worse, Fable) for a whole session
        // unilaterally.
        //
        // canEscalateToRole() centralizes the three gates on ModelRouter
        // (feature_disabled / bot_initiated_disabled / daily_cap_exhausted).
        // `max` is bot-initiated-blocked by DEFAULT (allowBotInitiated.max
        // defaults false), so a bot self-escalating to max is normally
        // rejected here unless the operator explicitly opted in. On block,
        // we degrade down the chain (max→power→standard) until a role is
        // allowed, returning an informative result so the bot can convey
        // why the requested escalation didn't fully land.
        let appliedChoice = choice;
        let costGateBlocked: { reason: string; detail: string } | null = null;
        if (choice === "power" || choice === "max") {
          let role: string = choice;
          let firstBlock: { reason: string; detail: string } | null = null;
          for (let i = 0; i < 3; i++) {
            const gate = modelRouter.canEscalateToRole(role);
            if (gate.allowed) break;
            if (!firstBlock) {
              firstBlock = {
                reason: gate.reason ?? "blocked",
                detail: gate.detail ?? `${role} escalation blocked by operator config`,
              };
            }
            const next = modelRouter.degradeRoleOnCap(role);
            if (next === role) { role = next; break; }
            role = next;
          }
          if (role !== choice) {
            appliedChoice = role;
            costGateBlocked = firstBlock;
            logger.warn(
              `session_set_tier: bot=${botId} session=${String(sessionKey).slice(0, 12)} ` +
              `${choice} escalation BLOCKED (${costGateBlocked?.reason}); ` +
              `degrading "${choice}" → "${role}"`,
            );
          }
        }

        // ── Apply ───────────────────────────────────────────────────────────
        try {
          modelRouter.setUserTier(
            String(sessionKey),
            appliedChoice === "auto" ? null : appliedChoice,
            consentSource,
          );
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          logger.warn(`session_set_tier: setUserTier threw: ${msg}`);
          return _resultContent({ ok: false, error: `internal: ${msg}` });
        }

        const reasonSuffix = params.reason ? ` reason="${params.reason.slice(0, 80)}"` : "";
        const blockedSuffix = costGateBlocked
          ? ` (requested=${choice} downgraded to ${appliedChoice}: ${costGateBlocked.reason})`
          : "";
        logger.info(
          `session_set_tier: bot=${botId} session=${String(sessionKey).slice(0, 12)} ` +
          `choice=${appliedChoice} consent_source=${consentSource}${reasonSuffix}${blockedSuffix}`,
        );

        return _resultContent({
          ok: true,
          applied_choice: appliedChoice,
          consent_source: consentSource,
          // When the cost gate fires, surface enough detail for the bot to
          // explain the outcome in user-facing prose. _resultContent only
          // includes these fields when they're populated, so successful
          // (non-blocked) calls stay terse.
          requested_choice: costGateBlocked ? choice : undefined,
          tier1_blocked_reason: costGateBlocked?.reason,
          tier1_blocked_detail: costGateBlocked?.detail,
        });
      },
    };
  };
}

// TODO (Phase 2 follow-on): tier1 evidence gate per spec § 4.1 update.
// When choice === "power" AND consentSource === "bot_initiated", verify
// either (a) a recent ask-hint exists (this requires hasRecentAskHint to
// be wired), OR (b) the user's last message contains a strong tier-power
// signal (regex match + length floor). If neither, downgrade to "standard"
// and emit `bot_initiated_tier1_denied` Signal via the cascade span. The
// gate plus signal emission ride alongside the cascade controller PR
// since both depend on its ask-hint mechanism.
