/**
 * runPinnedSubagent — shared wrapper for the plugin's cheap-LLM subagent
 * call sites (LLMTierClassifier, SessionStruggleJudge,
 * PreflightIntentRouter, SessionSummarizer), all of which pin the
 * operator's classifierModel via the `model:` param.
 *
 * OC 2026.7.1-2 added authorization on provider/model overrides in
 * plugin subagent runs (verified against the installed gateway dist,
 * `createGatewaySubagentRuntime.run` in dist/server-plugins-*.js):
 *
 *   - REQUEST-SCOPED runs (a hook firing during a live gateway request —
 *     which is every call site above) honor the pin only when the
 *     request's CLIENT carries admin scope or
 *     `internal.allowModelOverride`. A Telegram/Slack channel client
 *     never does. The `plugins.entries.evolve.subagent.allowModelOverride`
 *     grant deploy.py writes is NOT consulted on this path.
 *   - FALLBACK-SCOPED runs (no request client) consult that config grant
 *     via `authorizeFallbackModelOverride`.
 *
 * So under OC 2026.7 every request-scoped pinned run throws
 * "provider/model override is not authorized for this plugin subagent
 * run." — which silently degraded all four call sites at once
 * (2026-07-31 fleet incident: tier classifier → keyword fallback,
 * struggle judge → AMBIGUOUS, preflight router → abstain, summarizer →
 * heuristic outcome).
 *
 * Strategy: try the pinned run; on an authorization rejection, log
 * LOUDLY once per process, remember the denial, and retry WITHOUT the
 * pin — the run proceeds on the bot's session-default model. That is a
 * cost regression the operator hears about, not a silent capability
 * loss. Subsequent calls skip the doomed pinned attempt entirely.
 *
 * Any non-authorization error propagates to the caller unchanged — each
 * call site keeps its own degradation path for genuine failures.
 */

import { readCostBreakerDecision } from "../breakers/BreakerStateReader.js";

interface MinimalLogger {
  info(msg: string): void;
  warn(msg: string): void;
  error?(msg: string): void;
}

// ── Cost-breaker gate ───────────────────────────────────────────────────────
//
// A tripped cost breaker used to stop the BOT and leave Evolve's own model
// calls running. Measured on 2026-09-07 while one bot sat "paused": the
// preflight router's run count went 10,407 -> 13,282 and the pod's 7-day
// spend $36.95 -> $42.61 inside an hour. The breaker gated replies; it did
// not gate the plugin's own spend, which is the larger of the two on a
// paused bot because nothing else is happening.
//
// Every plugin-internal model call funnels through runPinnedSubagent (tier
// classifier, struggle judge, preflight router, session summarizer), so this
// is the one place the gate has to exist. It reads the SAME breaker files
// the turn veto reads, so reactivation lifts it with the bot — there is no
// second piece of state to clear.
//
// Two properties the gate is held to, both below:
//   - unknown breaker state refuses (_refusalFor) — a truncated cost.json
//     is exactly the moment the breaker has to hold;
//   - the two helpers that decide which model answers an interactive turn
//     are excused on that turn (_exemptFromGate) — a breaker stops work,
//     it does not re-route it.
//
// Configured once per process (one gateway process hosts one bot's plugin
// instance set — the same rationale as _pinDenied below and
// configureAppAttribution in index.ts), because two of the four call sites
// carry no sharedDir/botId of their own and threading one through them would
// leave the gate optional exactly where it is easiest to forget.

interface SubagentBreakerGate {
  sharedDir: string;
  botId: string;
  logger: MinimalLogger | null;
}

let _breakerGate: SubagentBreakerGate | null = null;
let _lastRefusalLogMs = 0;

/** How often a refusal is logged. Per MINUTE, not per call: a paused bot
 *  still gets a preflight attempt per inbound message, and one line each
 *  would bury the log the operator reads to understand the pause. */
const REFUSAL_LOG_INTERVAL_MS = 60_000;

/**
 * Error thrown by ``runPinnedSubagent`` when the bot's cost breaker is
 * tripped. Distinct type so a call site can tell "paused on purpose" from
 * a genuine failure; all four existing call sites already catch and take
 * their documented degradation path (keyword fallback / AMBIGUOUS /
 * abstain / heuristic outcome), which is the right behaviour here — the
 * bot is paused, so no cheap-LLM refinement is owed.
 */
export class SubagentBreakerRefusal extends Error {
  readonly scope: string;
  constructor(scope: string, reason: string) {
    super(
      `Evolve subagent refused: the bot's cost breaker is tripped ` +
      `(scope=${scope})${reason ? ` — ${reason}` : ""}`,
    );
    this.name = "SubagentBreakerRefusal";
    this.scope = scope;
  }
}

/**
 * Point the gate at this bot's breaker files. Called once from the plugin
 * entry. Until it is called the gate is INERT — an unconfigured process
 * spends exactly as it did before, which is the fail-open direction: a
 * plugin that failed to wire the gate must not silently stop classifying.
 */
export function configureSubagentBreakerGate(
  cfg: { sharedDir: string; botId: string },
  logger?: MinimalLogger,
): void {
  _breakerGate = {
    sharedDir: cfg.sharedDir,
    botId: cfg.botId,
    logger: logger ?? null,
  };
}

/** @internal test-only — drop the gate configuration and its log clock. */
export function _resetSubagentBreakerGateForTest(): void {
  _breakerGate = null;
  _lastRefusalLogMs = 0;
}

/**
 * The tripped-breaker decision for the configured bot, or null.
 *
 * **Fail CLOSED on unknown state.** An *absent* ``breakers/<bot>/cost.json``
 * legitimately means "not tripped" and keeps today's behaviour (proceed).
 * A file that EXISTS and does not parse means "unknown" — a partial write,
 * a disk-full truncation, a hand-edit, a schema change from a rolling
 * deploy — and for a gate that decides whether to SPEND MONEY, unknown has
 * to read as tripped. A truncated write during a trip is exactly when the
 * file is unreadable, and that is the moment the breaker must hold: the
 * alternative is the 10,407 -> 13,282 preflight bleed this gate exists to
 * stop, resumed silently on a bot the operator paused.
 *
 * Note the asymmetry with ``readCostBreakerDecision``'s ``vetoed`` field,
 * which stays fail-OPEN for the turn veto (a parsing glitch must not brick
 * the bot). That direction is right for a turn and wrong for spend; the
 * ``unreadable`` bit is what lets the two callers differ.
 */
function _refusalFor(now: number): { scope: string; reason: string } | null {
  const gate = _breakerGate;
  if (gate === null) return null;
  let decision;
  try {
    decision = readCostBreakerDecision({
      sharedDir: gate.sharedDir,
      botId: gate.botId,
    });
  } catch (err: any) {
    // readCostBreakerDecision is total by construction, so this is the
    // "cannot happen" branch — and it is still unknown state, so it
    // refuses rather than spends.
    decision = {
      vetoed: false,
      unreadable: true,
      scope: "bot" as const,
      reason: `breaker state unreadable: ${err?.message ?? err}`,
    };
  }
  if (!decision.vetoed && !decision.unreadable) return null;
  const unknown = !decision.vetoed && decision.unreadable === true;
  if (now - _lastRefusalLogMs >= REFUSAL_LOG_INTERVAL_MS) {
    _lastRefusalLogMs = now;
    gate.logger?.info(
      `Evolve: subagent runs refused while the cost breaker is ` +
      `${unknown ? "UNREADABLE (treated as tripped)" : "tripped"} ` +
      `(scope=${decision.scope}, bot=${gate.botId}) — the tier classifier, ` +
      `struggle judge, preflight router and session summarizer stay off ` +
      `until the bot is reactivated. Logged once a minute, not per call.`,
    );
  }
  return {
    scope: decision.scope ?? "bot",
    reason: unknown
      ? `breaker state present but unreadable (${decision.reason ?? "no detail"})`
      : decision.reason ?? "",
  };
}

// ── The interactive-turn carve-out ──────────────────────────────────────────
//
// A tripped cap is a checkpoint, not a downshift: "nothing changes which
// model answers a user turn" is the standing rule the charter does not
// bend on. Two of the four gated helpers DECIDE that model — the tier
// classifier and the preflight router. Refusing them on a bot whose cap
// action is not `checkpoint` (D-CC1: an L1 trip alone does not gate user
// chat) does not stop the turn; it re-routes it, because each call site
// then falls back to its own heuristic and answers on whatever tier that
// heuristic picks. That is a breaker moving a turn between rungs, which is
// the thing the rule forbids.
//
// So the gate excuses exactly those two key tags, and only on an
// interactive turn. `session-judge` and `session-summary` never decide the
// answering model, and every background trigger is refused regardless of
// tag — the paused bot's own machinery is still stopped, which is the
// spend this gate exists to stop (a paused bot's background work is where
// the bleed lives; nothing else is happening on it).
//
// The exemption is opt-IN twice over: by TAG (a future call site with a new
// tag is gated no matter what it passes) and by an explicit
// ``interactiveTurn: true`` from the caller. A call site that says nothing
// is GATED — silence must not be a way to switch a spend gate off.
const INTERACTIVE_EXEMPT_KEY_TAGS: ReadonlySet<string> = new Set([
  "tier-classifier",
  "preflight",
]);

/** Triggers that are nobody's live turn. Everything else is a person waiting. */
const BACKGROUND_TRIGGERS: ReadonlySet<string> = new Set([
  "heartbeat", "cron", "cron_app", "scheduled", "subagent",
]);

/**
 * Is this OC trigger one a person is waiting on?
 *
 * The helper the two routing call sites use to fill ``interactiveTurn``.
 * An ABSENT trigger counts as interactive, matching TurnObserver's own
 * convention on the hooks those two run from
 * (`ctx?.trigger === "user" || ctx?.trigger == null` — OC does not always
 * populate `trigger` on `before_model_resolve` for a user turn). Being
 * wrong in this direction costs one cheap classifier call on a paused bot;
 * being wrong in the other silently re-routes somebody's turn.
 *
 * Note this is a judgement about a TRIGGER, not about the gate: a call
 * site still has to pass the result through ``interactiveTurn`` for the
 * carve-out to apply at all.
 */
export function isInteractiveTrigger(trigger: unknown): boolean {
  const t = typeof trigger === "string" ? trigger.trim().toLowerCase() : "";
  return !BACKGROUND_TRIGGERS.has(t);
}

/** Does the gate stand down for this particular run? */
function _exemptFromGate(params: PinnedSubagentRunParams): boolean {
  if (params.interactiveTurn !== true) return false;
  const m = /(?:^|:)evolve:([a-z0-9-]+)(?::|$)/.exec(params.idempotencyKey ?? "");
  return m !== null && INTERACTIVE_EXEMPT_KEY_TAGS.has(m[1]);
}

// ── Cost-attribution tagging ────────────────────────────────────────────────
//
// OC's subagent runtime derives the run's session key from the
// idempotencyKey ("agent:<agent>:explicit:<idempotencyKey>", verified
// against the installed 2026.7 gateway dist + live sessions.json), and the
// llm_output hook fires for the subagent lane with that sessionKey in ctx —
// while agent_end does NOT fire for plugin subagent runs at all. So the
// idempotencyKey prefix is the ONLY thread connecting a billed subagent LLM
// call back to the Evolve helper that made it, and TurnObserver's llm_output
// capture (the only hook that sees these calls) classifies via this map.
//
// Every call site's key tag must appear here, mapped to the canonical
// cost_event trigger_kind it should bill under ("summarizer" for the
// session summarizer; "classifier" for the tier classifier, struggle judge,
// and preflight router — all classification-shaped analysis calls).
// runPinnedSubagent warns loudly when a key doesn't classify, so a future
// call site that forgets to register its tag is heard, not silently
// mis-billed as bot spend.
export type EvolveSubagentTriggerKind = "summarizer" | "classifier";

const EVOLVE_SUBAGENT_KEY_KINDS: Record<string, EvolveSubagentTriggerKind> = {
  "session-summary": "summarizer",
  "tier-classifier": "classifier",
  "session-judge": "classifier",
  "preflight": "classifier",
};

/**
 * Map an Evolve subagent idempotencyKey ("evolve:<tag>:…") or the session
 * key OC derives from it ("agent:main:explicit:evolve:<tag>:…") to the
 * cost_event trigger_kind for that call site. Returns null for anything
 * that isn't a recognizable Evolve subagent key — callers treat null as
 * "not ours" and fall through to normal handling.
 */
export function classifyEvolveSubagentKey(
  key: unknown,
): EvolveSubagentTriggerKind | null {
  const s = typeof key === "string" ? key : "";
  const m = /(?:^|:)evolve:([a-z0-9-]+)(?::|$)/.exec(s);
  if (!m) return null;
  return EVOLVE_SUBAGENT_KEY_KINDS[m[1]] ?? null;
}

export interface PinnedSubagentRunParams {
  idempotencyKey: string;
  message: string;
  model?: string;
  maxTurns?: number;
  /**
   * The caller asserts this run is serving a turn a person is waiting on
   * (fill it with ``isInteractiveTrigger(ctx?.trigger)``). Read ONLY by the
   * cost-breaker gate's carve-out (see INTERACTIVE_EXEMPT_KEY_TAGS) and
   * never forwarded to OC. Omitted or false ⇒ the gate applies.
   */
  interactiveTurn?: boolean;
}

// Per-process denial memory. One gateway process hosts one plugin
// instance set; once OC rejects a pin it will reject every subsequent
// one until the gateway (and its OC version/config) changes, so the
// flag intentionally lives at module scope rather than per-instance.
let _pinDenied = false;
let _pinDenialLogged = false;

/** @internal test-only — reset the per-process denial memory. */
export function _resetSubagentPinDenialForTest(): void {
  _pinDenied = false;
  _pinDenialLogged = false;
}

/** @internal exposed for diagnostics/tests. */
export function subagentPinDenied(): boolean {
  return _pinDenied;
}

/**
 * True iff `err` is OC's subagent model-override authorization
 * rejection (any of the reason strings the 2026.7 contract emits),
 * as opposed to a transient/infrastructure failure.
 */
export function isSubagentOverrideAuthError(err: unknown): boolean {
  const msg = String((err as { message?: unknown })?.message ?? err ?? "");
  return (
    /override is not authorized for this plugin subagent run/i.test(msg)
    || /not trusted for fallback provider\/model override/i.test(msg)
    || /is not allowlisted for plugin/i.test(msg)
    || /configured subagent\.allowedModels/i.test(msg)
    || /must resolve to a canonical provider\/model target/i.test(msg)
  );
}

export async function runPinnedSubagent(
  api: any,
  logger: MinimalLogger,
  params: PinnedSubagentRunParams,
): Promise<{ runId: string }> {
  // A paused bot's Evolve machinery pauses too. Checked FIRST — before the
  // key-tag warning and before any api call — so a refused run costs
  // nothing at all, not even a wasted attribution warning.
  //
  // Except for the two helpers that decide which model answers an
  // interactive turn: refusing those re-routes the turn instead of
  // stopping it. See INTERACTIVE_EXEMPT_KEY_TAGS.
  if (!_exemptFromGate(params)) {
    const refusal = _refusalFor(Date.now());
    if (refusal !== null) {
      throw new SubagentBreakerRefusal(refusal.scope, refusal.reason);
    }
  }
  if (classifyEvolveSubagentKey(params.idempotencyKey) === null) {
    // Unregistered key tag: this run's LLM cost will be attributed as
    // ordinary bot spend instead of Evolve overhead. Loud so a new call
    // site can't silently regress the overhead ledger (spec Phase A2).
    logger.warn(
      `Evolve: subagent idempotencyKey ${JSON.stringify(params.idempotencyKey)} ` +
      `has no trigger-kind mapping in EVOLVE_SUBAGENT_KEY_KINDS (subagentRun.ts) — ` +
      `its cost will be mis-attributed as bot spend, not Evolve overhead.`,
    );
  }
  // `interactiveTurn` is Evolve's own gate input, not an OC run param.
  const { model, interactiveTurn: _interactiveTurn, ...rest } = params;
  if (model && !_pinDenied) {
    try {
      return await api.runtime.subagent.run({ ...rest, model });
    } catch (err) {
      if (!isSubagentOverrideAuthError(err)) throw err;
      _pinDenied = true;
      if (!_pinDenialLogged) {
        _pinDenialLogged = true;
        const loud = logger.error ?? logger.warn;
        loud.call(
          logger,
          `Evolve: OC rejected the plugin's subagent model pin (${model}). ` +
          `OC >=2026.7 authorizes request-scoped overrides only for admin-scope clients; ` +
          `the plugins.entries.evolve.subagent.allowModelOverride grant covers ` +
          `fallback-scoped runs only. Retrying without the pin — the cheap-LLM helpers ` +
          `(tier classifier, struggle judge, preflight router, session summarizer) now ` +
          `run on the bot's DEFAULT model (cost regression, not capability loss). ` +
          `Original error: ${err}`,
        );
      }
    }
  }
  return await api.runtime.subagent.run(rest);
}
