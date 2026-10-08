/**
 * PreflightIntentRouter
 *
 * Decides a model-tier hint for the upcoming LLM call, BEFORE the LLM
 * runs. Sits in the routing ladder between explicit operator/user
 * defaults (above) and the bot's own default (below) — see
 * ModelRouter._resolveModelAndTier for the full precedence.
 *
 * Three layers, evaluated in order; first to produce a tier wins:
 *
 *   1. bot_prior  — the per-bot prior read from network.json. Either an
 *                   operator's hand-set tier, or — since D-OH2 — the
 *                   rung the nightly analyzer job
 *                   (``packages/analyzer/preflight_prior.py``) LEARNED
 *                   from the last 14 days of this bot's own turns, per
 *                   surface, with the evidence attached. Microseconds,
 *                   free, TTL-cached (60s). Refused below the
 *                   confidence threshold: a bot with thin history stays
 *                   on its primary.
 *
 *   2. regex      — narrow high-precision rules. ONLY catches obvious
 *                   cases: explicit deliberation cues ("design a system",
 *                   "help me think through") for tier1; bare acks /
 *                   single-word commands / common factual lookups for
 *                   tier3. Microseconds, free.
 *
 *   3. haiku      — LLM classifier for ambiguous prompts. **Off by
 *                   default and fails CLOSED** since
 *                   internal/decision-evolve-overhead-2026-09-07.md
 *                   (D-OH2): it is the only layer here that costs money,
 *                   and two days of turns put the entire benefit of
 *                   per-turn LLM tier classification below the cost of
 *                   running it. Gated on the single ``classifierGate``
 *                   switch, shared with the post-turn tier classifier,
 *                   the struggle judge and the session summariser.
 *                   ~150ms and ~$0.0001 when an operator turns it on;
 *                   hard 2s timeout, abstains on timeout.
 *
 * Why the prior moved offline:
 *   Layer 3 was answering, per turn and at the user's latency, a
 *   question that the overnight analyzer can answer better and for free
 *   — it already reads every turn this bot has taken. A prior computed
 *   from two weeks of a bot's actual traffic beats a haiku guess at one
 *   message, does not sit in front of the user, and cannot recurse into
 *   itself (finding-tier-router-self-call-loop-2026-09-07.md).
 */

import * as fs from "fs";
import * as path from "path";
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { runPinnedSubagent, isInteractiveTrigger } from "./subagentRun.js";
import { ClassifierGateReader } from "./classifierGate.js";
import {
  priorRoleForSurface,
  DEFAULT_PRIOR_MIN_CONFIDENCE,
  type BotPrior,
  type RuleRole,
} from "./routingRule.js";

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
  /** The surface (OC channel) this turn arrived on. Selects the
   *  per-surface entry of the learned prior when one exists (D-OH2). */
  surface?: string | null;
  /** The OC trigger for this turn, when the caller knows it. Read only by
   *  the cost-breaker gate in ``subagentRun``: the haiku layer decides
   *  which model answers an interactive turn, so a tripped breaker must
   *  not silently move it to the abstain path. Background triggers are
   *  refused as before. */
  trigger?: string | null;
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


// ── Learned-prior parsing ────────────────────────────────────────────────────

const _TIER_TO_ROLE: Record<string, RuleRole> = {
  tier1: "power",
  tier2: "standard",
  tier3: "fast",
};

const _ROLE_TO_TIER: Record<string, PreflightTier> = {
  power: "tier1",
  standard: "tier2",
  fast: "tier3",
};

function _roleToPreflightTier(role: RuleRole): PreflightTier | null {
  return _ROLE_TO_TIER[role] ?? null;
}

/**
 * Normalise ``bots.<id>.preflight`` into a {@link BotPrior}.
 *
 * Accepts both vocabularies so the operator-typed scalar
 * (``bot_prior: "tier3"``) and the analyzer-written role
 * (``bot_prior: "fast"`` + ``prior_evidence``) parse to the same thing.
 * An operator's hand-set scalar with no evidence block is taken at
 * confidence 1.0 — they said it on purpose. Anything the analyzer wrote
 * carries its measured confidence and is refused below the threshold by
 * ``priorRoleForSurface``.
 *
 * Exported for tests.
 */
export function _parseBotPrior(preflight: unknown): BotPrior | null {
  if (!preflight || typeof preflight !== "object") return null;
  const pf = preflight as Record<string, unknown>;
  const rawPrior = pf.bot_prior;
  if (typeof rawPrior !== "string") return null;

  const role: RuleRole | undefined =
    _TIER_TO_ROLE[rawPrior] ??
    (rawPrior === "fast" || rawPrior === "standard" || rawPrior === "power"
      ? (rawPrior as RuleRole)
      : undefined);
  if (!role) return null;

  const ev = (pf.prior_evidence ?? null) as Record<string, unknown> | null;
  // No evidence block → an operator set this by hand. Full confidence.
  const confidence =
    ev && typeof ev.confidence === "number" ? ev.confidence : 1.0;

  const surfaces: Record<string, RuleRole> = {};
  const rawSurfaces = ev && typeof ev.surfaces === "object" ? ev.surfaces : null;
  if (rawSurfaces) {
    for (const [k, v] of Object.entries(rawSurfaces as Record<string, unknown>)) {
      const surfaceRole =
        typeof v === "string"
          ? (_TIER_TO_ROLE[v] ??
             (v === "fast" || v === "standard" || v === "power" ? (v as RuleRole) : undefined))
          : undefined;
      if (surfaceRole) surfaces[k.toLowerCase()] = surfaceRole;
    }
  }

  return {
    role,
    confidence,
    surfaces: Object.keys(surfaces).length > 0 ? surfaces : undefined,
    turns: ev && typeof ev.turns === "number" ? ev.turns : undefined,
    computedAt: ev && typeof ev.computed_at === "string" ? ev.computed_at : undefined,
  };
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
  private _botPriorCache: { prior: BotPrior | null; checkedAt: number } | null = null;
  private static readonly _BOT_PRIOR_TTL_MS = 60_000;

  /**
   * The one switch every Evolve-owned model call reads. Default off,
   * fail closed; TTL-cached on the reader itself.
   */
  private readonly _gate: ClassifierGateReader;
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
    this._gate = new ClassifierGateReader(config.sharedDir, config.botId);
  }

  /**
   * Read the per-bot prior from network.json with a 60s TTL cache.
   *
   * Two shapes live under ``bots.<botId>.preflight`` and both are read:
   *
   *   bot_prior: "tier1" | "tier2" | "tier3"
   *       The original operator-set scalar. An operator who typed it
   *       meant it, so it carries confidence 1.0 and no evidence.
   *
   *   bot_prior: "fast" | "standard" | "power"     (role vocabulary)
   *   prior_evidence: { confidence, turns, surfaces, computed_at }
   *       What the nightly analyzer job (``preflight_prior.py``) writes:
   *       the rung that actually satisfied this bot's user turns over
   *       the last 14 days, per surface, with the evidence that
   *       produced it. Routes only when ``confidence`` clears
   *       ``DEFAULT_PRIOR_MIN_CONFIDENCE`` — a bot with thin history
   *       gets no prior and stays on its primary, which is what it does
   *       today (D-OH2, and the standing rule).
   *
   * Returns null when no usable prior is configured — the common case.
   * Fail-open on a read error: a missing prior is the DEFAULT state, so
   * "no prior" is the same answer an unreadable file should give. (The
   * fail-CLOSED requirement in D-OH2 is about the LLM layers, which read
   * ``classifierGate`` — see ``_isHaikuEnabled`` below.)
   */
  private _getBotPrior(surface?: string | null): PreflightTier | null {
    const now = Date.now();
    let prior = this._botPriorCache?.prior ?? null;
    if (
      !this._botPriorCache ||
      now - this._botPriorCache.checkedAt >= PreflightIntentRouter._BOT_PRIOR_TTL_MS
    ) {
      prior = this._readBotPrior();
      this._botPriorCache = { prior, checkedAt: now };
    }
    const resolved = priorRoleForSurface(prior, surface, DEFAULT_PRIOR_MIN_CONFIDENCE);
    if (!resolved) return null;
    return _roleToPreflightTier(resolved.role);
  }

  /**
   * True when this bot has a prior confident enough to route on. Read
   * by the sampling gate — a characterised bot needs a smaller sample
   * (D-OH4).
   */
  hasConfidentPrior(): boolean {
    const now = Date.now();
    if (
      !this._botPriorCache ||
      now - this._botPriorCache.checkedAt >= PreflightIntentRouter._BOT_PRIOR_TTL_MS
    ) {
      this._botPriorCache = { prior: this._readBotPrior(), checkedAt: now };
    }
    return priorRoleForSurface(this._botPriorCache.prior, null) !== null;
  }

  /** Uncached network.json read. Returns null on any fault. */
  private _readBotPrior(): BotPrior | null {
    try {
      const networkPath = path.join(this.config.sharedDir, "network.json");
      const raw = fs.readFileSync(networkPath, "utf8");
      const network = JSON.parse(raw);
      const pf = network?.bots?.[this.config.botId]?.preflight;
      return _parseBotPrior(pf);
    } catch {
      // Fail-open to "no prior": any read/parse error leaves the bot on
      // its primary, which is the pre-prior behavior. Not logged — for
      // most bots there is simply nothing configured here.
      return null;
    }
  }

  /**
   * May the haiku layer make a model call for this turn?
   *
   * Was: pod default-ON, per-bot opt-out, **fail-open** — an unreadable
   * network.json enabled an LLM call on every ambiguous user turn.
   * Now: the single ``classifierGate`` switch, default OFF, fail
   * CLOSED (D-OH2 / D-OH5). The regex and bot_prior layers above are
   * free and keep running; only the layer that costs money is gated.
   */
  private _isHaikuEnabled(): boolean {
    return this._gate.read(this.hasConfidentPrior()).enabled;
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
    trigger?: string | null,
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
        // This layer picks the rung that answers THIS turn, so on a turn a
        // person is waiting on the cost-breaker gate must not silence it:
        // that would move the turn to the abstain path instead of stopping
        // it. Background work stays refused.
        interactiveTurn: isInteractiveTrigger(trigger),
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
   *   1. bot_prior — operator config, or the offline-learned per-bot
   *      prior for this surface (refused below the confidence bar)
   *   2. regex tier1 — explicit deliberation cues
   *   3. regex tier3 — bare acks / factual lookups / simple commands
   *   4. haiku — LLM classifier; off by default, fails closed (D-OH2)
   *   5. abstain — no layer had an opinion
   *
   * Always resolves; never throws. A fault degrades to ABSTAIN so the
   * legacy classifier handles the turn as it did pre-deploy.
   *
   * latency_ms is observed even on abstain so post-deploy analytics can
   * track router overhead. With the haiku layer closed (the default) the
   * whole call is a regex scan plus a TTL-cached file read — p95 well
   * under 5ms, and no model call in front of the user at all.
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
      const prior = this._getBotPrior(input?.surface ?? null);
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
      // when (a) regex / bot_prior had no opinion AND (b) an operator has
      // opened the classifier gate for this bot. Hard 2s timeout with
      // abstain fallback so a slow API call never blocks the turn beyond
      // user-perceivable latency.
      //
      // OFF by default and fail-closed (D-OH2). On a default pod this
      // branch never runs, `classify` is pure CPU, and a user turn with
      // no confident prior and no regex hit reaches the model on the
      // bot's own primary — which is what it did before any of this
      // existed.
      if (this._isHaikuEnabled()) {
        const haikuDecision = await this._classifyWithHaiku(
          text, input.botId, start, input?.trigger,
        );
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
