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

  export function definePluginEntry(config: {
    id: string;
    name: string;
    description: string;
    register(api: any): void;
  }): unknown;
}
