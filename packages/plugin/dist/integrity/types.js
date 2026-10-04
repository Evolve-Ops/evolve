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
 * NOTE ON MIRROR VERSIONS: the tool-result-middleware types in THIS file are
 * pinned at OC v2026.6.6 (commit ``3811001d2783``). The ``before_tool_call``
 * hook types added to ``src/types/openclaw-plugin-sdk.d.ts`` were mirrored
 * separately from OC **v2026.6.11** (the fleet version), which first exposes
 * that pre-execution tool-call gate. Only those newly-added hook types were
 * re-verified at 6.11 — the middleware types here remain at their 6.6
 * provenance and were NOT re-checked against 6.11.
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
// ── Evolve structured outcome contract (the shape handed to the model) ───────
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
export const EVOLVE_APP_INTEGRITY_VERSION = 1;
//# sourceMappingURL=types.js.map