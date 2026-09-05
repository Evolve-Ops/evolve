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

interface MinimalLogger {
  info(msg: string): void;
  warn(msg: string): void;
  error?(msg: string): void;
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
  const { model, ...rest } = params;
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
