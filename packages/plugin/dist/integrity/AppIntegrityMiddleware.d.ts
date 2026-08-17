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
import type { EvolveConfig } from "../config.js";
import type { AgentToolResultMiddleware, AgentToolResultMiddlewareApi } from "./types.js";
interface MiddlewareLogger {
    info(msg: string): void;
    warn(msg: string): void;
    error(msg: string): void;
    debug(msg: string): void;
}
/**
 * Build the middleware handler. Exported (separately from registration) so
 * tests can drive it directly with synthetic events. ``registry`` is injected
 * so tests can supply a fake without touching the filesystem.
 */
export declare function makeAppIntegrityHandler(registry: {
    lookup(command: string): {
        appId: string;
        script: string;
    } | null;
}, logger: MiddlewareLogger, onToolActivity?: () => void): AgentToolResultMiddleware;
/**
 * Wire the harness into the OpenClaw plugin api. No-op (with an info log) on a
 * gateway too old to expose the middleware seam, so the plugin keeps loading on
 * OC < v2026.6.6 — the harness simply stays inactive until the fleet is on a
 * gateway that supports it.
 */
export declare function registerAppIntegrityMiddleware(api: AgentToolResultMiddlewareApi, config: EvolveConfig, logger: MiddlewareLogger): void;
export {};
//# sourceMappingURL=AppIntegrityMiddleware.d.ts.map