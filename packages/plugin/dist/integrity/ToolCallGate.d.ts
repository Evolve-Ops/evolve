/**
 * ToolCallGate — Layer-2 per-role tool-loading enforcement (below the LLM).
 *
 * Spec: internal/spec-user-roster-and-roles-2026-06-07.md §8 (Layer 2).
 * Design: internal/design-layer2-tool-loading-filter-2026-07-16.md.
 * Audit: internal/audit-r1a-enforcement-matrix-2026-06-30.md (G-N1 — "the
 *        load-bearing gap": post-admission authorization is not enforced
 *        below the LLM).
 *
 * ── The guarantee ────────────────────────────────────────────────────────────
 * Admission (Layer 1) already fail-closes below the LLM: an un-allowlisted
 * identity never reaches the model. But an ADMITTED speaker — whatever their
 * role — was offered the full tool surface, so a prompt-injection or jailbreak
 * could drive a sensitive tool (roster-mutate, channel-config, outbound Google
 * writes, or the gateway's built-in ``bash``/``exec``/``apply_patch``/file-write)
 * regardless of the LLM's judgment.
 *
 * This gate registers OpenClaw's ``before_tool_call`` hook (fleet OC v2026.6.11)
 * and, PER TOOL CALL, resolves the SPEAKER's role for the current turn and
 * BLOCKS the call when the speaker's capabilities do not permit that tool. The
 * decision runs in-process before the tool executes — a jailbreak cannot invoke
 * a tool the speaker's role forbids, because the block happens below the LLM.
 *
 * ── Fail-CLOSED decision table (for a tool in the capability→tool table) ──────
 *   speaker resolves + has the required capability   → ALLOW  (void)
 *   speaker resolves + lacks the required capability  → DENY
 *   speaker UNRESOLVED (no captured sender / no id)   → DENY   (fail-closed)
 *   any unexpected error while evaluating a gated call → DENY  (fail-closed)
 * A tool NOT in the table is always ALLOWED (documented residual). Read /
 * benign tools (defer, pod_status, list_signals, directory/gmail READ tools)
 * are intentionally ungated.
 *
 * ── Safe rollout: OBSERVE-ONLY by default ────────────────────────────────────
 * A below-LLM blocker can break legitimate tool use on first deploy. The gate
 * ships behind ``config.layer2Enforce`` (network.json ``layer2.enforce``),
 * DEFAULT false:
 *   - observe (default): compute the decision; when it WOULD block, LOG it and
 *     append a record to the per-bot decision ledger, then ALLOW (return void).
 *   - enforce (flag true): actually return ``{ block: true, blockReason }``.
 * The mode is recorded on every entry so the operator can see what enforcement
 * would do before arming it. Merging this code is non-arming.
 *
 * ── Two enforcement domains ──────────────────────────────────────────────────
 *   1. Plugin-registered tools (roster_*, channel_set_newcomer_mode, gmail_send,
 *      calendar_create_event, drive_write_file, …) — named in the table.
 *   2. Gateway BUILT-IN tools (bash/exec/apply_patch/file_write/write/edit) —
 *      these come from the base gateway, not the plugin, and are the real
 *      ``bot.code.modify`` blast radius. Gated here by name + by the
 *      host-authoritative ``toolKind === "code_mode_exec"`` discriminator.
 *
 * Reuses (does NOT reinvent): ``senderRegistry.getSender`` (sender captured in
 * before_agent_run) + ``roleResolver.resolveSpeakerRole`` (fail-closed to
 * participant/[] on any read error). This module holds no role logic of its own.
 */
import type { EvolveConfig } from "../config.js";
import type { PluginHookBeforeToolCallEvent, PluginHookBeforeToolCallResult, PluginHookToolContext } from "openclaw/plugin-sdk/plugin-entry";
interface GateLogger {
    info(msg: string): void;
    warn(msg: string): void;
    error(msg: string): void;
    debug(msg: string): void;
}
/**
 * DEFAULT-DENY classification (adversarial-review hardening, 2026-07-17).
 *
 * The original gate was a NAME DENYLIST: "gate these N exec names, allow
 * everything else." Over an UNCONTROLLED upstream tool registry that is
 * fail-OPEN by construction — a renamed / aliased / newly-shipped exec tool, an
 * Anthropic-family editor (``str_replace_based_edit_tool``, ``text_editor``,
 * ``shell``, ``run_command``…), or any MCP tool the denylist never heard of
 * sails straight through. The review flagged this as the load-bearing hole.
 *
 * The model is now INVERTED to default-deny:
 *   1. SENSITIVE_CAPABILITY_BY_TOOL — the KNOWN sensitive PLUGIN-native tools,
 *      each pinned to its exact required capability (roster / channel / Google
 *      writes / directory-write / tier-change). These are bare plugin names.
 *   2. KNOWN_SAFE_TOOLS — a POSITIVE allowlist of the plugin's own read/benign
 *      tools. Only a tool on this list is treated as ungated.
 *   3. Everything else → DEFAULT-DENY to ``bot.code.modify`` (the highest
 *      blast-radius capability). Unknown exec tools, unknown MCP tools, future
 *      gateway built-ins, renamed editors — all fail CLOSED without having to
 *      be enumerated. ``toolKind === "code_mode_exec"`` is an ADDITIONAL
 *      positive signal for the same class.
 *
 * Net effect: a participant (``[]``) is blocked from ANYTHING not on the safe
 * allowlist when armed; an admin (``*``) / a role holding ``bot.code.modify``
 * is allowed. The safe allowlist is tuned via the observe-only soak: every
 * unclassified tool that falls through to default-deny is drift-logged (see
 * ``logToolDrift``) so the operator can see which real tools to add.
 */
/**
 * KNOWN sensitive PLUGIN-native tools → the single capability each requires.
 * Bare plugin tool names (never namespaced), enumerated from ``index.ts``
 * registration. Capability ids are the 8 built-ins shared with the Python side
 * (capabilities.py) and the TS role resolver
 * (roleResolver.DEFAULT_ROLE_CAPABILITIES). This table is NOT the whole gated
 * set — anything not here and not in KNOWN_SAFE_TOOLS is default-denied.
 */
export declare const SENSITIVE_CAPABILITY_BY_TOOL: Readonly<Record<string, string>>;
/**
 * POSITIVE allowlist of KNOWN-SAFE read/benign tools. A tool here (after MCP
 * normalization) is treated as ungated. Enumerated from ``index.ts`` — the
 * plugin's own read tools + benign self-report reflexes. Classified read-vs-write
 * honestly: every WRITE / OUTBOUND / MUTATING tool is in the sensitive table or
 * left to default-deny, NEVER here.
 *
 * A tool NOT in this set and NOT in the sensitive table is default-denied to
 * ``bot.code.modify`` — so adding a benign tool here is how you STOP over-gating
 * it, and the observe-only drift log tells you which ones need adding.
 */
export declare const KNOWN_SAFE_TOOLS: ReadonlySet<string>;
/**
 * Host-authoritative tool-KIND → capability. OC tags code-execution tools that
 * intentionally share a name with ``toolKind: "code_mode_exec"``. This is an
 * ADDITIONAL positive signal for the code.modify class (default-deny already
 * catches unknown names; this pins the capability precisely when the host tells
 * us the kind, and wins over the safe allowlist — a code_mode_exec tool is
 * NEVER treated as safe even if its bare name happens to collide with one).
 */
export declare const CAPABILITY_BY_TOOL_KIND: Readonly<Record<string, string>>;
/**
 * Normalize a possibly host-namespaced tool name to the bare name the tables
 * key on. MCP tools arrive as ``mcp__<server>__<tool>`` (e.g.
 * ``mcp__gmail__send``, ``mcp__x__bash``); other hosts may prefix similarly.
 * Strip the ``mcp__<server>__`` envelope so the bare ``<tool>`` matches the
 * tables. A name that still does not classify falls through to default-deny.
 *
 * NB: only the LEADING ``mcp__<server>__`` segment is stripped (server names do
 * not contain ``__``); the remaining tool name keeps its own underscores
 * (``mcp__gmail__send_message`` → ``send_message``). We do NOT lowercase — the
 * allowlist match stays case-EXACT so a case-variant (``POD_STATUS``) can never
 * widen the safe set; it simply default-denies (fail-closed).
 */
export declare function normalizeToolName(raw: string | undefined | null): string;
/** How a tool was classified — drives drift logging + block-reason precision. */
export type ToolClassificationVia = "table" | "toolKind" | "safe" | "default_deny";
export interface ToolClassification {
    /** The bare (normalized) name the tables were matched against. */
    readonly normalizedName: string;
    /** The raw name as received (may be MCP-namespaced). */
    readonly rawName: string;
    /** Required capability, or ``undefined`` when the tool is known-safe/ungated. */
    readonly requiredCapability: string | undefined;
    /** Which rule classified it. */
    readonly via: ToolClassificationVia;
}
/**
 * Classify a tool call under the default-deny model. Pure + total — never
 * throws, so the handler can classify before any IO and fail-close on a later
 * error. Order is fail-closed-biased: sensitive table → host toolKind →
 * known-safe allowlist → default-deny.
 */
export declare function classifyTool(toolName: string | undefined | null, toolKind?: string | null): ToolClassification;
/**
 * The capability a call requires, or ``undefined`` when the tool is known-safe.
 * Thin wrapper over ``classifyTool`` (kept for the table-sanity tests and any
 * caller that only needs the capability). Under the default-deny model this
 * returns a capability for EVERY tool that is not on the known-safe allowlist.
 */
export declare function requiredCapabilityFor(toolName: string | undefined | null, toolKind?: string | null): string | undefined;
/** Outcome of evaluating a GATED call's authorization. */
export interface GateEvaluation {
    /** True ⇒ speaker is authorized; the call is allowed. */
    readonly allowed: boolean;
    /** False ⇒ no captured sender for this runId (fail-closed deny). */
    readonly senderResolved: boolean;
    /** Resolved role, or null when the speaker was unresolved. */
    readonly role: string | null;
    /** How the role was resolved (roleResolver.resolvedBy), or null. */
    readonly resolvedBy: string | null;
    /** Speaker stable id, or null when unresolved. */
    readonly senderId: string | null;
    /** Speaker platform used for resolution, or null when unresolved. */
    readonly platform: string | null;
    /** Human-readable block reason (empty string when allowed). */
    readonly reason: string;
}
/**
 * Resolve the speaker for ``runId`` and decide whether they may call a tool
 * requiring ``requiredCap``. Reuses the shared sender registry + role resolver;
 * holds no role logic of its own. Never throws in normal operation
 * (resolveSpeakerRole is itself fail-closed); the caller additionally wraps this
 * in a fail-closed try/catch.
 */
export declare function evaluateGatedCall(config: Pick<EvolveConfig, "botId" | "sharedDir">, requiredCap: string, runId: string | null | undefined): GateEvaluation;
/** One decision-ledger row. Names/ids only — never tool params or message content. */
interface GateLedgerRecord {
    ts: string;
    bot_id: string;
    mode: "observe" | "enforce";
    /** What the gate did: "block" (enforce), "would_block" (observe), "error_block". */
    action: "block" | "would_block" | "error_block";
    tool_name: string;
    tool_kind: string | null;
    required_capability: string;
    role: string | null;
    resolved_by: string | null;
    sender_id: string | null;
    platform: string | null;
    sender_resolved: boolean;
    run_id: string | null;
    reason: string;
}
/**
 * Best-effort append of a gate decision to the per-bot decision ledger:
 *
 *   {sharedDir}/{botId}/layer2-gate/decisions-YYYY-MM-DD.jsonl
 *
 * This is the plugin's established "signal path": the plugin writes a per-bot
 * JSONL ledger under sharedDir (the OutwardActionLedger convention) that the
 * evolve-side Python can later read and project into the Signal store for the
 * Alerts page. UTC-dated, append-only, one JSON object per line. Any IO error
 * is swallowed — a telemetry write must never block or crash the tool-call path.
 */
export declare function writeGateDecisionLedger(config: Pick<EvolveConfig, "botId" | "sharedDir">, rec: GateLedgerRecord, logger: GateLogger): void;
/** Called at the top of every handler invocation — the positive fire signal. */
export declare function noteBeforeToolCallFired(): void;
/** True once the before_tool_call handler has fired at least once. */
export declare function beforeToolCallHasFired(): boolean;
/**
 * Cross-check for the tool-result middleware to call on observed tool activity.
 * If enforcement is armed for this bot but before_tool_call has NEVER fired,
 * the gate is armed-but-dead (the tool ran without the gate seeing it) — emit a
 * one-shot ERROR. No-op when observe-only or when the gate is provably live.
 */
export declare function checkGateLivenessOnToolActivity(config: Pick<EvolveConfig, "botId" | "layer2Enforce">, logger: GateLogger): void;
/** Test helper — reset the drift + liveness module state between tests. */
export declare function _resetGateStateForTests(): void;
export declare const OC_MIN_VERSION_FOR_HOOK = "2026.6.11";
/**
 * Best-effort discovery of the running gateway's OC version. There is no typed
 * accessor (the SDK types the gateway config as an opaque bag), so we probe the
 * few places a version realistically surfaces. Returns the raw version string
 * or ``null`` when it can't be determined.
 */
export declare function detectOcVersion(api: {
    config?: Record<string, unknown>;
    openclawVersion?: unknown;
    version?: unknown;
}): string | null;
/**
 * Decide whether it is SAFE to honor enforce=true given the discovered gateway
 * version. Returns true (honor) when the version is unknown OR >= the min; false
 * (refuse, stay observe) when it is KNOWN and below the min. Logs an ERROR on a
 * refusal so the operator sees why arming was downgraded.
 */
export declare function enforceAllowedByVersion(api: {
    config?: Record<string, unknown>;
}, config: Pick<EvolveConfig, "botId">, logger: GateLogger): boolean;
/**
 * Build the ``before_tool_call`` handler. Exported separately from registration
 * so tests can drive it directly with synthetic events. Returns:
 *   - ``void``               → allow the call unchanged (ungated, or authorized,
 *                              or observe-mode would-block)
 *   - ``{ block: true, … }`` → veto the call (enforce-mode deny / error)
 */
export declare function makeBeforeToolCallHandler(config: EvolveConfig, logger: GateLogger): (event: PluginHookBeforeToolCallEvent, ctx?: PluginHookToolContext) => PluginHookBeforeToolCallResult | void;
/**
 * Register the Layer-2 ``before_tool_call`` gate on the OpenClaw plugin api.
 * Registered the SAME way TurnObserver registers before_agent_run: ``api.on``.
 * No-op (with an info log) on a gateway too old to expose the hook, so the
 * plugin keeps loading on OC < v2026.6.11 — the gate simply stays inactive.
 */
export declare function registerToolCallGate(api: {
    on?: (event: string, handler: (...args: any[]) => any, opts?: unknown) => void;
    config?: Record<string, unknown>;
}, config: EvolveConfig, logger: GateLogger): void;
export {};
//# sourceMappingURL=ToolCallGate.d.ts.map