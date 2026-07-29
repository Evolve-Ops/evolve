/**
 * AppIntegrityMiddleware — the §2.2 just-works execution-integrity harness.
 *
 * Registers an OpenClaw agent tool-result middleware
 * (``api.registerAgentToolResultMiddleware``, OC v2026.6.6+) that runs AWAITED,
 * PRE-MODEL on every tool result. For a tool call that ran a KNOWN app's script
 * it substitutes an HONEST structured outcome before the model ever sees the
 * raw output; for every other tool result it is a pure passthrough.
 *
 * Because the transform runs before the model, the model receives the honest
 * result and cannot fake it — this is the un-fakeable property the forge-only
 * (self-reporting) approach lacked. It covers ALL apps and ALL invocation modes
 * with no per-app rebuild.
 *
 * Fail-safety (fleet-wide blast radius — this runs in EVERY tool-result path):
 *   - Non-app tool result → ``void`` (OC keeps the original result unchanged).
 *   - Recognized app FAILURE (positive signal) → replace content with a clean
 *     plain-language message; the raw traceback never reaches the model.
 *   - Recognized app SUCCESS → preserve the original content verbatim and only
 *     annotate ``details`` (non-destructive). If the annotation can't be done
 *     safely, pass through untouched.
 *   - ANY internal error → ``void`` (pass the ORIGINAL, raw, truthful result
 *     through). A middleware bug can never turn a failing script into a
 *     success, nor break a working tool call. (OC additionally fail-closes a
 *     throwing handler to a generic error result — never to a fabricated
 *     success — but we never rely on that: we catch first.)
 *
 * Spec: docs/spec-app-invocation-just-works-2026-06-29.md §2.2.
 */
import { AppScriptRegistry } from "./appScriptRegistry.js";
import { checkGateLivenessOnToolActivity } from "./ToolCallGate.js";
import { extractCommand, readOutcome, buildFailureResult, buildSuccessResult, } from "./toolOutcome.js";
/**
 * Build the middleware handler. Exported (separately from registration) so
 * tests can drive it directly with synthetic events. ``registry`` is injected
 * so tests can supply a fake without touching the filesystem.
 */
export function makeAppIntegrityHandler(registry, logger, onToolActivity) {
    return (event) => {
        // Observed REAL tool activity — lets the Layer-2 gate detect "armed but not
        // live" (a tool ran but before_tool_call never fired). Best-effort, never
        // throws out of the hot path, never changes the result.
        if (onToolActivity) {
            try {
                onToolActivity();
            }
            catch {
                /* liveness probe must never affect the tool-result path */
            }
        }
        try {
            const command = extractCommand(event?.args);
            if (!command)
                return; // not a shell tool / no command → passthrough
            const match = registry.lookup(command);
            if (!match)
                return; // command did not run a known app script → passthrough
            const outcome = readOutcome(event);
            if (outcome.kind === "failed") {
                // High-stakes path: replace the raw output with an honest failure.
                return { result: buildFailureResult(match.appId, match.script, outcome) };
            }
            if (outcome.kind === "ok") {
                // Host-confirmed success: non-destructive annotation. null ⇒ passthrough.
                const ok = buildSuccessResult(match.appId, match.script, outcome, event.result);
                return ok ? { result: ok } : undefined;
            }
            // "unknown" — no authoritative signal; never stamp "ok" on absence of
            // evidence. Pass the raw result through untouched.
            return;
        }
        catch (err) {
            // Fail toward truth: return void so OC keeps the ORIGINAL (raw) result.
            // Never fabricate an outcome from a middleware error.
            try {
                logger.warn(`Evolve app-integrity: handler error, passing original result through — ${err}`);
            }
            catch {
                /* logging must never throw out of the hot path */
            }
            return;
        }
    };
}
/**
 * Wire the harness into the OpenClaw plugin api. No-op (with an info log) on a
 * gateway too old to expose the middleware seam, so the plugin keeps loading on
 * OC < v2026.6.6 — the harness simply stays inactive until the fleet is on a
 * gateway that supports it.
 */
export function registerAppIntegrityMiddleware(api, config, logger) {
    if (typeof api.registerAgentToolResultMiddleware !== "function") {
        logger.info("Evolve app-integrity: gateway does not expose " +
            "registerAgentToolResultMiddleware (needs OpenClaw v2026.6.6+) — " +
            "harness inactive for " + config.botId);
        return;
    }
    const registry = new AppScriptRegistry({
        botId: config.botId,
        sharedDir: config.sharedDir,
        logger,
    });
    // Cross-check the Layer-2 gate's liveness on every observed tool result: if
    // enforcement is armed but before_tool_call has never fired, the gate is
    // armed-but-dead — checkGateLivenessOnToolActivity logs a one-shot ERROR.
    const handler = makeAppIntegrityHandler(registry, logger, () => checkGateLivenessOnToolActivity(config, logger));
    api.registerAgentToolResultMiddleware(handler, { runtimes: ["openclaw"] });
    logger.info(`Evolve app-integrity: tool-result middleware registered for ${config.botId} ` +
        `(covers ${registry.coveredApps().length} app(s))`);
}
//# sourceMappingURL=AppIntegrityMiddleware.js.map