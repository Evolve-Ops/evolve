/**
 * OpenClaw agent tool-result middleware — type surface + Evolve's structured
 * outcome contract.
 *
 * The middleware types here MIRROR OpenClaw's own
 * ``src/plugins/agent-tool-result-middleware-types.ts`` as shipped in
 * **v2026.6.6** (PR #90004, OC source @ commit ``3811001d2783``). We re-declare
 * them locally because the real plugin SDK is provided by the gateway at
 * runtime, not installed at compile time (same reason the rest of the SDK lives
 * in ``src/types/openclaw-plugin-sdk.d.ts`` as stubs). Keep these aligned with
 * OC source — a drift here is a silent contract break.
 *
 * Contract reference (OC source):
 *   - ``OpenClawPluginApi.registerAgentToolResultMiddleware(handler, options?)``
 *     — ``src/plugins/types.ts``. Enabled only for bundled / explicitly-enabled
 *     installed plugins whose manifest declares
 *     ``contracts.agentToolResultMiddleware`` with the targeted runtime ids.
 *   - The runner awaits each handler PRE-MODEL and replaces the tool result the
 *     model sees with the returned ``{result}`` (``void`` ⇒ pass the current
 *     result through unchanged) — ``src/agents/harness/tool-result-middleware.ts``.
 *   - On a handler THROW or an invalid returned result the runner is
 *     **fail-closed** to a generic ``"Tool output unavailable"`` error result
 *     (``buildMiddlewareFailureResult`` → ``details.middlewareError``); it never
 *     fabricates success. Our own handler additionally try/catches and returns
 *     ``void`` on any internal error so the ORIGINAL (raw, truthful) result
 *     reaches the model rather than a generic error.
 */
/** Runtimes a middleware may target. OC v2026.6.6 ships "openclaw" | "codex". */
export type AgentToolResultMiddlewareRuntime = "openclaw" | "codex";
export interface OpenClawAgentToolResultTextBlock {
    type: "text";
    text: string;
}
export interface OpenClawAgentToolResultImageBlock {
    type: "image";
    mimeType: string;
    data: string;
}
export type OpenClawAgentToolResultContentBlock = OpenClawAgentToolResultTextBlock | OpenClawAgentToolResultImageBlock;
/**
 * The tool result the model consumes. ``content`` is the model-visible payload;
 * ``details`` is opaque structured metadata OC passes through (it is where the
 * bash/exec runtimes commonly stash ``exitCode`` / ``stdout`` / ``stderr`` and
 * where OC itself records ``{status:"error", middlewareError:true}`` on a
 * fail-closed middleware). OC types ``details`` as ``unknown``; so do we.
 */
export interface OpenClawAgentToolResult {
    content: OpenClawAgentToolResultContentBlock[];
    details?: unknown;
}
/** Event passed to a tool-result middleware handler (first arg). */
export interface AgentToolResultMiddlewareEvent {
    threadId?: string;
    turnId?: string;
    toolCallId: string;
    toolName: string;
    /** The tool's input. For shell tools this carries the command string. */
    args: unknown;
    cwd?: string;
    /** True when the host already classified the tool call as an error. */
    isError?: boolean;
    result: OpenClawAgentToolResult;
}
/** Per-invocation context (second arg). */
export interface AgentToolResultMiddlewareContext {
    runtime: AgentToolResultMiddlewareRuntime | string;
    agentId?: string;
    sessionId?: string;
    sessionKey?: string;
    runId?: string;
}
export interface AgentToolResultMiddlewareResult {
    result: OpenClawAgentToolResult;
}
export type AgentToolResultMiddleware = (event: AgentToolResultMiddlewareEvent, context: AgentToolResultMiddlewareContext) => Promise<AgentToolResultMiddlewareResult | void> | AgentToolResultMiddlewareResult | void;
export interface AgentToolResultMiddlewareOptions {
    runtimes?: AgentToolResultMiddlewareRuntime[];
}
/**
 * Minimal shape of the OC plugin api fields this module touches. The plugin's
 * ``register(api)`` types ``api`` as ``any``; we narrow just the one method we
 * call so a signature drift surfaces at compile time here.
 */
export interface AgentToolResultMiddlewareApi {
    registerAgentToolResultMiddleware?: (handler: AgentToolResultMiddleware, options?: AgentToolResultMiddlewareOptions) => void;
    logger?: {
        info(msg: string): void;
        warn(msg: string): void;
        error(msg: string): void;
        debug(msg: string): void;
    };
}
/**
 * The honest, structured outcome the harness stamps onto a recognized
 * app-script tool result. One contract for both directions, unified with the
 * existing ``stdout_protocol`` / ``event_triggers.invocation`` success-shape
 * notion (``plugin_intercept_migration.py``: a script either succeeds and its
 * real output is delivered, or it fails and an honest fallback is delivered —
 * never a confabulated success).
 *
 * Carried under ``OpenClawAgentToolResult.details.evolve_app_integrity``:
 *   - ``failed``  → the result CONTENT is replaced with a plain-language
 *     "couldn't do that: <reason>" the model narrates from; the raw traceback
 *     never reaches the model. ``error_summary`` is a short, sanitized reason.
 *   - ``ok``      → the result CONTENT is preserved verbatim (the real output IS
 *     the payload); only this structured marker is added to ``details`` so the
 *     badge / coverage layer and telemetry have a machine-readable signal.
 */
export declare const EVOLVE_APP_INTEGRITY_VERSION: 1;
export interface EvolveAppIntegrityOutcome {
    version: typeof EVOLVE_APP_INTEGRITY_VERSION;
    status: "ok" | "failed";
    /** The recognized application id whose script this tool call ran. */
    app_id: string;
    /** The recognized script path (as matched in the command). */
    script: string;
    /** Real exit code when the harness could read one; null when unknown. */
    exit_code: number | null;
    /** Short, sanitized failure reason. Present only when status === "failed". */
    error_summary?: string;
}
export interface EvolveAppIntegrityDetails {
    evolve_app_integrity: EvolveAppIntegrityOutcome;
}
//# sourceMappingURL=types.d.ts.map