/**
 * Type stubs for the openclaw plugin SDK.
 * The real SDK is provided at runtime by the OpenClaw gateway.
 * These stubs let TypeScript compile without the SDK installed locally.
 */

declare module "openclaw/plugin-sdk/types" {
  export interface PluginLogger {
    info(msg: string): void;
    warn(msg: string): void;
    error(msg: string): void;
    debug(msg: string): void;
  }

  export interface OpenClawConfig {
    [key: string]: unknown;
  }
}

declare module "openclaw/plugin-sdk/plugin-entry" {
  export interface BeforeAgentRunEvent {
    userMessage: string;           // Raw user input, pre-processing
    sessionId: string;
    channelId: string;
    conversationHistory?: any[];   // Prior messages in the session, if available
  }

  /**
   * Decision shape OpenClaw's hook runner expects back from
   * ``before_agent_run``. As of OpenClaw 2026.5.18 the hook is treated as
   * a gate: returning ``null``, ``undefined``, or any non-decision shape
   * is normalized to ``{outcome: "block"}`` with the user-facing message
   * "Your message could not be sent: blocked by <plugin>". Plugins that
   * want the agent to proceed MUST return ``{outcome: "pass"}``
   * explicitly. Plugins that need to short-circuit return
   * ``{outcome: "block", message?: "..."}``.
   *
   * Source of truth: ``openclaw/plugin-sdk/src/plugins/hook-decision-types.d.ts``
   * (``InputGateDecision``); behavior at
   * ``openclaw/dist/hook-runner-global-*.js → runBeforeAgentRun``.
   *
   * Legacy fields (``response`` / ``skipAgent``) from the pre-gate
   * contract are not part of the current shape and are ignored if
   * returned. They are kept off the type so callers don't accidentally
   * rely on them.
   */
  export type BeforeAgentRunResult =
    | { outcome: "pass" }
    | {
        outcome: "block";
        /** Internal plugin-local reason; core does not surface verbatim. */
        reason?: string;
        /** Optional user-facing detail. Appended after BLOCK_MESSAGE_PREFIX. */
        message?: string;
        /** Plugin-defined analytics category (e.g. "cost_limit", "pii"). */
        category?: string;
        /** Opaque plugin metadata; core does not interpret. */
        metadata?: Record<string, unknown>;
      };

  // ── before_tool_call hook (pre-execution tool-call gate) ───────────────────
  //
  // PROVENANCE: unlike the surrounding declarations (mirrored from OC
  // **v2026.6.6**), the ``before_tool_call`` types below were mirrored
  // BYTE-ACCURATELY from OC **v2026.6.11** (the fleet version), which first
  // exposes the pre-execution tool-call gate. Only these types were re-verified
  // at 6.11 — the rest of this stub remains at its 6.6 provenance.
  //
  // Source (OC v2026.6.11): ``openclaw/dist/hook-types-*.d.ts``, bundled from
  //   - ``src/plugins/hook-before-tool-call-result.d.ts`` (result + approval)
  //   - ``src/plugins/hook-types.d.ts`` (tool context + event)
  //   - ``src/plugins/host-hook-json.d.ts`` (PluginJsonValue)
  //   - ``src/infra/diagnostic-trace-context.d.ts`` (DiagnosticTraceContext)
  //
  // Registration follows the SAME pattern as ``before_agent_run``: the plugin
  // registers via ``api.on("before_tool_call", handler)`` (``api`` is typed
  // ``any``), and these exported types annotate the handler's args/return. The
  // OC hook-handler-map entry this satisfies is:
  //
  //   before_tool_call: (
  //     event: PluginHookBeforeToolCallEvent,
  //     ctx: PluginHookToolContext,
  //   ) => Promise<PluginHookBeforeToolCallResult | void>
  //      | PluginHookBeforeToolCallResult
  //      | void;
  //
  // This chip only declares the seam — no enforcement logic, no registration.

  /** Bounded JSON value shape accepted from plugin hooks. */
  export type PluginJsonPrimitive = string | number | boolean | null;
  export type PluginJsonValue =
    | PluginJsonPrimitive
    | PluginJsonValue[]
    | { [key: string]: PluginJsonValue };

  export type DiagnosticTraceContext = {
    /** W3C trace id, 32 lowercase hex chars. */
    readonly traceId: string;
    /** Current span id, 16 lowercase hex chars. */
    readonly spanId?: string;
    /** Parent span id, 16 lowercase hex chars. */
    readonly parentSpanId?: string;
    /** W3C trace flags, 2 lowercase hex chars. Defaults to sampled. */
    readonly traceFlags?: string;
  };

  /** Host-authoritative discriminator for tools that intentionally share names. */
  export type PluginHookToolKind = "code_mode_exec";
  /** Host-authoritative input/runtime family for tools needing policy distinction. */
  export type PluginHookToolInputKind = "javascript" | "typescript";

  export const PluginApprovalResolutions: {
    readonly ALLOW_ONCE: "allow-once";
    readonly ALLOW_ALWAYS: "allow-always";
    readonly DENY: "deny";
    readonly TIMEOUT: "timeout";
    readonly CANCELLED: "cancelled";
  };
  export type PluginApprovalResolution =
    (typeof PluginApprovalResolutions)[keyof typeof PluginApprovalResolutions];

  /** Per-invocation context (second arg) passed to ``before_tool_call``. */
  export type PluginHookToolContext = {
    agentId?: string;
    sessionKey?: string;
    sessionId?: string;
    runId?: string;
    trace?: DiagnosticTraceContext;
    toolName: string;
    toolKind?: PluginHookToolKind;
    toolInputKind?: PluginHookToolInputKind;
    toolCallId?: string;
    getSessionExtension?: (namespace: string) => PluginJsonValue | undefined;
    channelId?: string;
  };

  /** Event (first arg) passed to ``before_tool_call``, before the tool runs. */
  export type PluginHookBeforeToolCallEvent = {
    toolName: string;
    params: Record<string, unknown>;
    toolKind?: PluginHookToolKind;
    toolInputKind?: PluginHookToolInputKind;
    runId?: string;
    toolCallId?: string;
    /**
     * Optional best-effort destination path hints the host derived from
     * ``params`` for well-known tool envelopes (e.g. ``apply_patch``). A
     * convenience hint, not an authoritative parse result — plugins should
     * parse and validate ``params`` themselves when correctness or policy
     * decisions depend on the exact affected paths. Absent for tools the host
     * does not know how to derive paths for.
     */
    derivedPaths?: readonly string[];
  };

  /**
   * Decision returned from ``before_tool_call``. ``void`` ⇒ proceed unchanged.
   * ``block: true`` short-circuits the tool call (``blockReason`` is the
   * plugin-local reason). ``params`` rewrites the tool inputs before execution.
   * ``requireApproval`` gates the call behind a host approval prompt.
   */
  export type PluginHookBeforeToolCallResult = {
    params?: Record<string, unknown>;
    block?: boolean;
    blockReason?: string;
    requireApproval?: {
      title: string;
      description: string;
      severity?: "info" | "warning" | "critical";
      timeoutMs?: number;
      timeoutBehavior?: "allow" | "deny";
      allowedDecisions?: Array<"allow-once" | "allow-always" | "deny">;
      pluginId?: string;
      onResolution?: (decision: PluginApprovalResolution) => Promise<void> | void;
    };
  };

  export function definePluginEntry(config: {
    id: string;
    name: string;
    description: string;
    register(api: any): void;
  }): unknown;
}
