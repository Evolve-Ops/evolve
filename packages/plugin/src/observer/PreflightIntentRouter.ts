/**
 * PreflightIntentRouter
 *
 * Decides a model-tier hint for the upcoming LLM call based on the user's
 * prompt and bot identity, BEFORE the LLM runs. Sits in the routing ladder
 * between explicit operator/user defaults (above) and the legacy classifier
 * (below) — see ModelRouter._resolveModelAndTier for the full precedence.
 *
 * Three layers (escalating cost), evaluated in order; first to produce a
 * tier wins:
 *
 *   1. bot_prior  — per-bot baseline tier read from network.json. The
 *                   strongest signal — operator explicitly configured this
 *                   bot to default to tier X. Microseconds, free, TTL-
 *                   cached (60s).
 *
 *   2. regex      — narrow high-precision rules. ONLY catches obvious
 *                   cases: explicit deliberation cues ("design a system",
 *                   "help me think through") for tier1; bare acks /
 *                   single-word commands / common factual lookups for
 *                   tier3. Does NOT try to classify general prompts —
 *                   that's haiku's job (Phase 3). Microseconds, free.
 *
 *   3. haiku      — LLM classifier for ambiguous prompts. ~150ms, ~$0.0001.
 *                   Hard 2s timeout; falls back to abstain on timeout.
 *                   Phase 3 — not implemented in this file yet.
 *
 * Phase status (2026-06-06):
 *   - Phase 1: full wiring (PR #2334), router always abstained
 *   - Phase 2 (THIS FILE): bot_prior + regex layers live
 *   - Phase 3: haiku layer (pending)
 *   - Phase 4: disagreement detector + RSI tuning (pending)
 *
 * Why phase the rules so narrowly:
 *   This is the first phase that actually changes pod behavior. A wrong
 *   tier escalation wastes $ (tier1 on trivial turns) or degrades quality
 *   (tier3 on hard turns). Until the disagreement detector exists
 *   (Phase 4), we can't see misroutings clearly. So Phase 2 ships VERY
 *   tight patterns — high precision, low recall — and defaults to abstain.
 *   Phase 4 RSI grows the pattern set from observed disagreement data.
 *
 * Spec: docs/spec-preflight-intent-router-2026-06-06.md (to be written).
 */

import * as fs from "fs";
import * as path from "path";
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { runPinnedSubagent } from "./subagentRun.js";

// ── Public types ─────────────────────────────────────────────────────────────

export type PreflightTier = "tier1" | "tier2" | "tier3";
export type PreflightLayer = "regex" | "bot_prior" | "haiku" | "abstain";

export interface PreflightInput {
  /** The user's prompt text for THIS turn. May be empty (caller handles). */
  userMessage: string;
  /** Bot identity (e.g., "team_bot_a", "atlas-research"). Used by the
   *  bot_prior layer and as context for haiku. */
  botId: string;
  /** Optional last assistant message — Phase 3+ uses this for context-shift
   *  detection ("actually let me reconsider"). Phase 2 ignores it. */
  lastAssistantMessage?: string;
}

export interface PreflightDecision {
  tier: PreflightTier | null;
  reason: string;
  layer: PreflightLayer;
  confidence: number;
  latency_ms: number;
}

/**
 * The "no opinion" decision. Returned when no layer fired.
 */
export const ABSTAIN: PreflightDecision = Object.freeze({
  tier: null,
  reason: "abstain",
  layer: "abstain",
  confidence: 0,
  latency_ms: 0,
});

// ── Regex tables — narrow high-precision patterns ───────────────────────────

/**
 * TIER1 indicators. These fire when the user is EXPLICITLY asking for
 * deliberative thinking — design work, decision help, weighing options.
 * Each pattern is intentionally narrow:
 *
 *   - The "design"/"architect" verbs require a following article AND a
 *     technical/system noun (system, architecture, schema, ...) so
 *     "design a kitchen", "design a workout", and "design.com" all
 *     abstain. Without the noun list this matched ANY "design a X" or
 *     "architect a X" — including casual home/lifestyle imperatives.
 *   - The "help me" prefix only fires on EXPLICIT deliberation verbs:
 *     "think through" and "decide between". Dropped "decide" / "weigh"
 *     / "figure out" as standalone matches — those are conversational
 *     idiom ("help me figure out my TV size") not requests for opus.
 *     Cost incident 2026-06-07: a single "help me figure out" turn
 *     ran on opus for $0.96 instead of sonnet for ~$0.05.
 *   - The "think through"/"reason about" verbs include optional
 *     `let's |let me ` prefixes so both imperative and self-directed
 *     forms catch.
 *   - "Weigh|consider" patterns require a following options-noun so
 *     idiomatic uses like "that weighs on my mind" abstain.
 *
 * Design principle: FALSE POSITIVES are far more expensive than false
 * negatives here. A missed tier1 escalation costs "user gets sonnet
 * instead of opus" (minor quality dip). A wrong tier1 escalation
 * costs ~18x the token rate for the entire session. When in doubt,
 * leave it to the workhorse tier.
 *
 * The reason field names the specific pattern so the audit layer
 * (Phase 4) can attribute miscalibrations to individual rules and
 * propose tweaks.
 */

/**
 * Nouns that signal genuine system/architecture work. Used as a suffix
 * requirement on the design/architect imperative patterns to prevent
 * casual decor/lifestyle uses from escalating to tier1. Add new entries
 * conservatively — every word here grants opus-routing rights.
 */
const TECHNICAL_NOUNS =
  "system|architecture|solution|api|schema|database|service|workflow|process|algorithm|protocol|infrastructure|framework|pipeline|deployment";

const TIER1_PATTERNS: ReadonlyArray<{ rx: RegExp; reason: string }> = Object.freeze([
  {
    // EXPLICIT deliberation prefix only. "help me think through X" and
    // "help me decide between X and Y" stay; "help me figure out X" /
    // "help me decide X" / "help me weigh X" no longer match here —
    // those are casual conversational requests for help, not opus.
    rx: /\bhelp me (think (this )?through|decide between)\b/i,
    reason: "regex:explicit_thinking_request",
  },
  {
    rx: /\b(let'?s |let me )?(think (this )?through|reason about|deliberate on)\b/i,
    reason: "regex:think_through",
  },
  {
    rx: /\bwhat'?s the right (call|choice|move|approach|trade.?off)\b/i,
    reason: "regex:decision_help",
  },
  {
    rx: /\b(weigh|consider) (the |my |our )?(options|pros|trade.?offs?|alternatives)\b/i,
    reason: "regex:weigh_options",
  },
  {
    rx: new RegExp(
      `\\bdesign (a |an |the |my |our |some |this )(${TECHNICAL_NOUNS})\\b`,
      "i",
    ),
    reason: "regex:design_imperative",
  },
  {
    rx: new RegExp(
      `\\barchitect (a |an |the |my |our |some |this )(${TECHNICAL_NOUNS})\\b`,
      "i",
    ),
    reason: "regex:architect_imperative",
  },
]);

/**
 * TIER3 indicators. These fire when the message is short AND clearly
 * trivial — bare acks, single-word responses, common factual lookups,
 * simple commands. All anchored to start (`^`) and end (`$`) where
 * appropriate so the patterns don't trip on longer messages that happen
 * to contain the keyword.
 *
 * Critically: a message like "thanks for the detailed analysis, can you
 * also..." starts with "thanks" but is NOT a pure ack — the anchored end
 * on `bare_ack` requires the message to BE just an ack, not start with one.
 */
const TIER3_PATTERNS: ReadonlyArray<{ rx: RegExp; reason: string }> = Object.freeze([
  {
    // Whole message is an ack: "thanks", "Got it.", "Sounds good!"
    rx: /^(thanks|thank you|got it|nice|cool|great|awesome|sounds good)[\s.,!]*$/i,
    reason: "regex:bare_ack",
  },
  {
    // Whole message is yes/no/stop/go: "yes", "ok.", "nope"
    rx: /^(yes|no|yep|nope|sure|ok|okay|alright|stop|go)[\s.,!]*$/i,
    reason: "regex:bare_response",
  },
  {
    // Factual lookup at message start: "What's the weather?", "what's the time"
    rx: /^what'?s the (weather|time|date|day|temperature)\b/i,
    reason: "regex:factual_lookup",
  },
  {
    // Simple command at message start: "Set a timer for 5 min", "set an alarm"
    rx: /^set (a |an |the )?(timer|reminder|alarm|alert)\b/i,
    reason: "regex:simple_command",
  },
]);

/**
 * Scan a pattern list against text; return the first match's reason, or
 * null when nothing fires. Module-level (not bound to the class) so
 * tests can call it directly.
 */
function _matchPatterns(
  text: string,
  patterns: ReadonlyArray<{ rx: RegExp; reason: string }>,
): string | null {
  for (const { rx, reason } of patterns) {
    if (rx.test(text)) return reason;
  }
  return null;
}

/**
 * Internal helpers exported only for tests. Production code calls
 * `classify()` which orchestrates the layers.
 */
export const _internalForTest = Object.freeze({
  TIER1_PATTERNS,
  TIER3_PATTERNS,
  matchPatterns: _matchPatterns,
});

// ── Haiku layer prompt + parser ─────────────────────────────────────────────

/**
 * Haiku classifier prompt. Three-class output (TIER1 / TIER2 / TIER3) plus
 * an AMBIGUOUS escape hatch. Kept deliberately concise so the input tokens
 * stay tiny — the call costs ~$0.0001 with haiku.
 *
 * {bot_id} = bot identity (e.g. "team_bot_a")
 * {user_message} = user prompt, truncated to 500 chars
 *
 * Design notes:
 *   - Tier3 examples lead with "confirmations / acks / bare commands" so
 *     short user replies that didn't trip the regex layer ("yep go ahead",
 *     "actually wait") still route correctly.
 *   - Tier1 examples emphasize WHAT THE USER ASKED FOR ("help me decide"),
 *     not what the bot might need to do (which is unknowable pre-call).
 *   - The "everything else → TIER2" fallback is critical: it stops the
 *     classifier from defaulting to tier3 (cost optimization at the cost
 *     of quality) or tier1 (quality at the cost of cost) when uncertain.
 *
 * One-word response forced for parseability + low output tokens.
 */
const HAIKU_PROMPT_TEMPLATE = `You are routing an AI request to the right model tier for response quality.

TIER1 = needs deep reasoning, multi-step thinking, careful analysis:
- Architecture decisions, design problems
- Weighing trade-offs across multiple options
- Help thinking through a personal/business decision
- Complex writing that needs careful structure

TIER3 = fast, simple, factual, or command-driven:
- Single-step factual lookups
- Confirmations, acknowledgments, bare replies
- Simple commands (set timer, list X, delete Y)
- Single-word responses

TIER2 = the default workhorse — everything in between:
- Standard analysis, writing, code
- Multi-paragraph answers
- Tool-using turns
- Anything ambiguous between the extremes

Bot: {bot_id}
User: {user_message}

Reply with exactly one word: TIER1, TIER2, TIER3, or AMBIGUOUS.`;

/**
 * Parse haiku's response into a tier (or null when unparseable / ambiguous).
 *
 * Defensive: any of these → null:
 *   - empty / undefined response
 *   - "AMBIGUOUS" or any other non-tier word
 *   - response contains multiple tier words (model didn't follow the
 *     one-word instruction; we don't try to disambiguate)
 *
 * Returns null on null so the caller can fall through to abstain.
 *
 * Exported for tests; not part of the public API.
 */
export function _parseHaikuTier(response: string | null | undefined): PreflightTier | null {
  if (!response) return null;
  const text = response.trim().toUpperCase();
  const hasT1 = /\bTIER\s*1\b/.test(text);
  const hasT2 = /\bTIER\s*2\b/.test(text);
  const hasT3 = /\bTIER\s*3\b/.test(text);
  const hitCount = (hasT1 ? 1 : 0) + (hasT2 ? 1 : 0) + (hasT3 ? 1 : 0);
  if (hitCount !== 1) return null; // 0 → abstain (AMBIGUOUS / garbage); >1 → unclear
  if (hasT1) return "tier1";
  if (hasT2) return "tier2";
  return "tier3";
}

// ── Router ───────────────────────────────────────────────────────────────────

export class PreflightIntentRouter {
  private readonly config: EvolveConfig;
  private readonly logger: PluginLogger;
  private readonly api: unknown;

  /**
   * TTL cache for the per-bot prior read from network.json. The bot_prior
   * is operator config and changes very rarely (minutes/hours, not turns),
   * so a 60s cache is fine — matches the cadence of
   * `TurnObserver._isPushbackEnabled` / `_isPreflightEnabled`.
   */
  private _botPriorCache: { tier: PreflightTier | null; checkedAt: number } | null = null;
  private static readonly _BOT_PRIOR_TTL_MS = 60_000;

  /**
   * TTL cache for the haiku-layer enabled flag. Read from
   * `network.json::cascade.preflight.haiku_enabled` (pod default) with
   * `bots.<id>.preflight.haiku_enabled` override. Same TTL as bot_prior.
   */
  private _haikuEnabledCache: { enabled: boolean; checkedAt: number } | null = null;
  /**
   * Hard timeout for the haiku call. Tuned to be well below the user-
   * perceived latency floor on chat surfaces — 2s is the point where a
   * user would start to notice the bot "thinking." If the call exceeds
   * the budget, we abort and abstain (legacy classifier handles the
   * turn at its normal latency).
   */
  private static readonly _HAIKU_TIMEOUT_MS = 2000;

  constructor(config: EvolveConfig, logger: PluginLogger, api: unknown) {
    this.config = config;
    this.logger = logger;
    this.api = api;
  }

  /**
   * Read the per-bot prior tier from network.json with a 60s TTL cache.
   * Returns null when no prior is configured (the default — most bots
   * don't have one). Fail-open: any read/parse error returns null so a
   * router fault never blocks the turn.
   *
   * Network.json shape:
   *   bots.<botId>.preflight.bot_prior: "tier1" | "tier2" | "tier3"
   */
  private _getBotPrior(): PreflightTier | null {
    const now = Date.now();
    if (
      this._botPriorCache &&
      now - this._botPriorCache.checkedAt < PreflightIntentRouter._BOT_PRIOR_TTL_MS
    ) {
      return this._botPriorCache.tier;
    }
    let tier: PreflightTier | null = null;
    try {
      const networkPath = path.join(this.config.sharedDir, "network.json");
      const raw = fs.readFileSync(networkPath, "utf8");
      const network = JSON.parse(raw);
      const botCfg = network?.bots?.[this.config.botId];
      const prior = botCfg?.preflight?.bot_prior;
      if (prior === "tier1" || prior === "tier2" || prior === "tier3") {
        tier = prior;
      }
    } catch {
      // Fail-open: any read/parse error → no prior. The router falls
      // through to regex / abstain. We don't log this — it's the normal
      // case for most bots (no config = no prior).
    }
    this._botPriorCache = { tier, checkedAt: now };
    return tier;
  }

  /**
   * Read whether the haiku layer is enabled for this bot. TTL-cached.
   * Pod default-on; per-bot opt-out via
   * `network.json::bots.<id>.preflight.haiku_enabled: false`.
   *
   * Operators who want python-only routing (no LLM in the path, e.g.
   * for latency-critical bots or to bound API spend) can disable
   * haiku via the pod-level switch
   * `network.json::cascade.preflight.haiku_enabled: false`.
   * Per-bot ON overrides pod-level OFF.
   */
  private _isHaikuEnabled(): boolean {
    const now = Date.now();
    if (
      this._haikuEnabledCache &&
      now - this._haikuEnabledCache.checkedAt < PreflightIntentRouter._BOT_PRIOR_TTL_MS
    ) {
      return this._haikuEnabledCache.enabled;
    }
    let enabled = true; // pod default-on
    try {
      const networkPath = path.join(this.config.sharedDir, "network.json");
      const raw = fs.readFileSync(networkPath, "utf8");
      const network = JSON.parse(raw);
      const podSetting = network?.cascade?.preflight?.haiku_enabled;
      if (podSetting === false) enabled = false;
      const botSetting = network?.bots?.[this.config.botId]?.preflight?.haiku_enabled;
      if (botSetting === false) enabled = false;
      else if (botSetting === true) enabled = true;
    } catch {
      // Fail-open: any read/parse error → haiku enabled. Phase 4
      // disagreement data will surface misroutings; an unparseable
      // config shouldn't silently degrade routing.
    }
    this._haikuEnabledCache = { enabled, checkedAt: now };
    return enabled;
  }

  /**
   * Call haiku to classify an ambiguous prompt. Returns null when:
   *   - api isn't available (constructor was passed null/empty stub)
   *   - subagent call throws or times out
   *   - haiku response isn't parseable into a tier
   *
   * On null, the caller falls through to ABSTAIN — legacy classifier
   * handles the turn. The router contract is "never throw into the
   * hot path"; haiku faults degrade silently.
   *
   * Latency budget: hard 2s timeout on waitForRun. Subagent.run setup
   * adds ~10-50ms; haiku itself is typically ~100-200ms. Worst case
   * the user sees +2.1s on this turn (compared to no router); typical
   * case is +150ms.
   */
  private async _classifyWithHaiku(
    text: string,
    botId: string,
    start: number,
  ): Promise<PreflightDecision | null> {
    // Defensive: missing api or partial api stub (common in tests where
    // the existing tests passed `{}` as api). Skip the layer cleanly
    // rather than crashing.
    const api = this.api as {
      runtime?: {
        subagent?: {
          run?: (input: unknown) => Promise<{ runId: string }>;
          waitForRun?: (input: unknown) => Promise<{ lastMessage?: string }>;
        };
      };
    };
    if (
      typeof api?.runtime?.subagent?.run !== "function" ||
      typeof api?.runtime?.subagent?.waitForRun !== "function"
    ) {
      return null;
    }

    // Truncate user message to cap input tokens. 500 chars matches the
    // existing LLMTierClassifier convention — empirically enough context
    // for classification without bloating the prompt.
    const prompt = HAIKU_PROMPT_TEMPLATE
      .replace("{bot_id}", botId)
      .replace("{user_message}", text.slice(0, 500));

    try {
      // runPinnedSubagent adapts to OC >=2026.7's override authorization:
      // pinned first, unpinned retry (loud, once) when the pin is denied.
      const runResult = await runPinnedSubagent(api, this.logger, {
        idempotencyKey: `evolve:preflight:${botId}:${Date.now()}`,
        message: prompt,
        // Reuses the existing operator-tunable classifier model setting
        // (typically haiku). When unset, OC picks the bot's default.
        model: (this.config as { classifierModel?: string }).classifierModel,
        maxTurns: 1,
      });
      const response = await api.runtime.subagent.waitForRun({
        runId: runResult.runId,
        timeoutMs: PreflightIntentRouter._HAIKU_TIMEOUT_MS,
      });
      const tier = _parseHaikuTier(response?.lastMessage);
      if (tier === null) return null;
      return {
        tier,
        reason: `haiku:${tier}`,
        layer: "haiku",
        // Lower confidence than regex (1.0) and bot_prior (1.0). Haiku is
        // an inference; regex and bot_prior are deterministic rules.
        // Phase 4 disagreement detector will use this to weight haiku-
        // driven misroutings less heavily than regex-driven ones when
        // proposing pattern tweaks.
        confidence: 0.7,
        latency_ms: Date.now() - start,
      };
    } catch (err) {
      this.logger.debug(`Evolve: preflight haiku call failed: ${err}`);
      return null;
    }
  }

  /**
   * Classify the upcoming turn's tier.
   *
   * Layer order (first to produce a tier wins):
   *   1. bot_prior — explicit per-bot operator config
   *   2. regex tier1 — explicit deliberation cues
   *   3. regex tier3 — bare acks / factual lookups / simple commands
   *   4. haiku — LLM classifier for ambiguous prompts (Phase 3)
   *   5. abstain — no layer had an opinion
   *
   * Always resolves; never throws. A fault degrades to ABSTAIN so the
   * legacy classifier handles the turn as it did pre-deploy.
   *
   * latency_ms is observed even on abstain so post-deploy analytics can
   * track router overhead. Phase 2 target: p95 < 5ms (Phase 1 was sub-
   * millisecond; adding the regex scan + one file read adds work but
   * the file read hits the TTL cache 95%+ of the time).
   */
  async classify(input: PreflightInput): Promise<PreflightDecision> {
    const start = Date.now();
    try {
      // Defensive: malformed input → abstain. Phase 1 contract was
      // "never throw"; Phase 2 keeps it.
      const text = (input?.userMessage ?? "").trim();
      if (!text) {
        return {
          tier: null,
          reason: "empty_message",
          layer: "abstain",
          confidence: 0,
          latency_ms: Date.now() - start,
        };
      }

      // Layer 1: bot_prior — operator's per-bot baseline wins outright.
      // This is the strongest signal: the operator EXPLICITLY configured
      // this bot to default to tier X. A regex match in the user's prompt
      // doesn't override that intent.
      const prior = this._getBotPrior();
      if (prior) {
        return {
          tier: prior,
          reason: `bot_prior:${input.botId}`,
          layer: "bot_prior",
          confidence: 1.0,
          latency_ms: Date.now() - start,
        };
      }

      // Layer 2: regex tier1 — explicit deliberation cues. Bias toward
      // ESCALATION on ambiguity: when both a tier1 and tier3 indicator
      // could plausibly match, tier1 wins (user gets the better model).
      // Phase 4 disagreement data will tell us if this bias produces too
      // many over-escalations.
      const t1 = _matchPatterns(text, TIER1_PATTERNS);
      if (t1) {
        return {
          tier: "tier1",
          reason: t1,
          layer: "regex",
          confidence: 1.0,
          latency_ms: Date.now() - start,
        };
      }

      // Layer 3: regex tier3 — short, trivial, factual. Patterns are
      // anchored (full-message or message-start) to keep precision high.
      const t3 = _matchPatterns(text, TIER3_PATTERNS);
      if (t3) {
        return {
          tier: "tier3",
          reason: t3,
          layer: "regex",
          confidence: 1.0,
          latency_ms: Date.now() - start,
        };
      }

      // Layer 4: haiku — LLM classifier for ambiguous prompts. Only fires
      // when (a) regex / bot_prior had no opinion AND (b) haiku is enabled
      // for this bot. Hard 2s timeout with abstain fallback so a slow API
      // call never blocks the turn beyond user-perceivable latency.
      //
      // Default-on at the pod level; operators can disable per-bot for
      // latency-critical or cost-sensitive bots. Phase 4 disagreement
      // data will tell us if haiku's accuracy justifies the +150ms p50.
      if (this._isHaikuEnabled()) {
        const haikuDecision = await this._classifyWithHaiku(text, input.botId, start);
        if (haikuDecision) return haikuDecision;
      }

      // No layer fired — abstain. Legacy classifier handles the turn.
      return { ...ABSTAIN, latency_ms: Date.now() - start };
    } catch (err) {
      this.logger.debug(`Evolve: preflight router failed: ${err}`);
      return { ...ABSTAIN, latency_ms: Date.now() - start };
    }
  }
}
