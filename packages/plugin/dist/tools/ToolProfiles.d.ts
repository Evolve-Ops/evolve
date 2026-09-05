/**
 * ToolProfiles — per-session-type tool registration (context-economy CE-2b).
 *
 * Design: internal/design-pa-context-economy-2026-08-31.md §3 CE-2.
 * Brief:  internal/dispatch/done/tool-schema-diet-per-session-type.md.
 *
 * ── Why ──────────────────────────────────────────────────────────────────────
 * The first live run of `tools/context-efficiency-census` (2026-09-02) found
 * tool DESCRIPTIONS were 54% of every call's input — more than the fixed prefix
 * (39%) and nine times the conversation history (6%). Eight single-call
 * sessions carried ~36k input tokens each and made ZERO tool calls: they bought
 * the whole toolset and used none of it.
 *
 * A session's tool surface is decided once, when OC collects the toolset. This
 * module is the declarative table that decides it per SESSION KIND, applied at
 * the plugin's own registration boundary — `api.registerTool` — so it only ever
 * governs Evolve's own tools. OC's built-ins and any MCP toolset are untouched
 * (`tools.profile` / `tools.alsoAllow` in openclaw.json are the operator's knob
 * for those; OC exposes no per-session tool allowlist to a plugin).
 *
 * ── The seam (verified live, 2026-09-02) ─────────────────────────────────────
 * A registered factory is invoked with a per-session ctx carrying `sessionKey`,
 * `sessionId` and `messageChannel`. Proof: the defer tool's queue rows on the
 * reference pod carry a real `session_key`/`session_id` read out of that ctx,
 * so the ctx is populated when the toolset is built. If a future gateway ever
 * invokes a factory with an empty ctx, `classifySessionKind("")` returns
 * `other`, which maps to the FULL profile — the fail-safe direction.
 *
 * ── The contract ─────────────────────────────────────────────────────────────
 *   * `default: full`. Any kind not named in PROFILE_BY_KIND, and any session
 *     whose key does not classify, registers everything. A session never
 *     silently loses a tool because the table forgot it.
 *   * USER sessions are never trimmed. That is a hard guardrail of this chip.
 *   * A trimmed tool is NOT removed. It stays in the toolset under its own
 *     name with a one-line description and refuses when called, naming the
 *     profile — so the model gets a legible refusal, never "tool not found",
 *     and the operator gets a ledger row to widen the profile FROM EVIDENCE.
 *
 * ── What "minimal" means here ────────────────────────────────────────────────
 * App manifests do not declare a tool list (checked: `manifest-v7-spec.schema
 * .json` has no such field), so a background session's needs cannot be derived
 * — they are declared here, and the reasoning is per-tool, not per-count. The
 * `no_live_speaker` profile drops exactly the tools that require a human
 * speaker in the turn: the roster/channel mutations and the directory WRITE
 * (the Layer-2 gate already fail-closes these on an unresolved speaker), the
 * user-driven tier change, and the primary bot's help/intake surface. It keeps
 * everything a background turn can legitimately do — defer, record_application,
 * expand_app, directory_lookup, pod_state, and the whole Google suite, because
 * a scheduled digest genuinely reads mail and calendar.
 *
 * ── A NEW tool defaults to trimmed for the profiled kinds ────────────────────
 * `default: full` is a statement about SESSIONS, not about tools: a session
 * kind this table does not name keeps everything. Within a kind it DOES name,
 * the keep-list is an allowlist, so a tool added to the plugin later is not
 * carried by `no_live_speaker` until someone adds its name here. That is the
 * deliberate direction — a background session's tool surface should grow by a
 * reviewed edit, not by default — and it is not silent: the first background
 * session that reaches for the new tool gets a refusal naming the profile, and
 * the refusal raises a `tool_profile` Signal that says exactly where to widen.
 * A tool a USER session needs is unaffected either way.
 */
/** Mirrors `session_kinds.ALL_KINDS` in packages/analyzer/session_kinds.py. */
export type SessionKind = "user" | "scheduled" | "evolve_internal" | "oneshot" | "subagent" | "other" | "unindexed";
/** Substrings marking a key as Evolve's own dispatch. */
export declare const EVOLVE_TAGS: readonly [":evolve:", ":explicit:evolve"];
/** Substrings marking a key as schedule-triggered. */
export declare const SCHEDULED_TAGS: readonly [":cron:", ":heartbeat", ":scheduled"];
/** Route-segment values that are NOT channel names. */
export declare const NON_CHANNEL_ROUTES: ReadonlySet<string>;
/**
 * Session-id prefixes that mark an `explicit` session as a PERSON at a
 * keyboard rather than a program. The admin UI's chat drawer dispatches
 * `openclaw agent --session-id admin-ui-<page>` (`evo.proxy
 * .derive_session_id`) — an explicit id, but an interactive operator
 * conversation, so it classifies as `user` and keeps the full tool surface.
 */
export declare const CONSOLE_SESSION_PREFIXES: readonly ["admin-ui"];
/**
 * `{kind, channel}` for one session key. `channel` is what the gateway
 * recorded for the turn, if anything — a hint only: a key that names its
 * channel in the route segment is recognized without it.
 */
export declare function classifySessionKind(sessionKey?: string | null, channel?: string | null): {
    kind: SessionKind;
    channel: string | null;
};
export interface ToolProfile {
    id: string;
    /** `"full"` = register every tool unchanged; otherwise the kept names. */
    tools: "full" | readonly string[];
    /** Operator-legible reason, quoted verbatim in the refusal. */
    why: string;
}
/** The profile a session gets when nothing else applies. Never trims. */
export declare const FULL_PROFILE_ID = "full";
/**
 * Tools a session with no live human speaker keeps. Everything absent from
 * this list is trimmed for the kinds mapped to `no_live_speaker` below.
 *
 * Weights (chars of `{name, description, parameters}`, measured from a live
 * pod's context-footprint.json, 2026-09-02) are given so the cost of adding a
 * name back is visible at the point of decision.
 */
export declare const NO_LIVE_SPEAKER_TOOLS: readonly string[];
export declare const TOOL_PROFILES: Readonly<Record<string, ToolProfile>>;
/**
 * Session kind → profile id. A kind absent here gets {@link FULL_PROFILE_ID}.
 *
 * `user` and `subagent` are deliberately ABSENT rather than mapped to "full":
 * a user session is never trimmed (chip guardrail), and a subagent run is
 * doing whatever its parent user session asked, so it keeps the full surface
 * until evidence says otherwise.
 *
 * `scheduled` and `oneshot` share `no_live_speaker`: a cron/heartbeat turn and
 * a `--session-id`-dispatched one-shot both run with no person in the turn.
 */
export declare const PROFILE_BY_KIND: Readonly<Partial<Record<SessionKind, string>>>;
/** The profile for a session kind. Always resolves — falls back to full. */
export declare function resolveToolProfile(kind: SessionKind): ToolProfile;
/** True iff `profile` carries `toolName`. A "full" profile carries everything. */
export declare function profileAllows(profile: ToolProfile, toolName: string): boolean;
interface ProfileLogger {
    info?: (msg: string) => void;
    warn: (msg: string) => void;
    debug?: (msg: string) => void;
}
/** The per-session ctx OC passes to a tool factory. Every field optional. */
export interface ToolFactoryContext {
    sessionKey?: string;
    sessionId?: string;
    messageChannel?: string;
    agentId?: string;
    [key: string]: unknown;
}
interface ToolDefinition {
    name?: string;
    description?: string;
    parameters?: unknown;
    execute?: unknown;
    [key: string]: unknown;
}
type ToolFactory = (ctx: ToolFactoryContext) => ToolDefinition;
export interface ToolProfileConfig {
    botId: string;
    sharedDir: string;
}
/** One refusal-ledger row. Names and ids only — never tool params. */
export interface ToolProfileRefusalRecord {
    ts: string;
    bot_id: string;
    tool_name: string;
    profile: string;
    session_kind: SessionKind;
    session_key: string | null;
    session_id: string | null;
}
/**
 * The description a trimmed tool rides with. Short on purpose: this string IS
 * the saving. It still names the tool and says what happens on a call, so a
 * model that reaches for it learns why rather than hallucinating around a gap.
 */
/**
 * The parameter schema every trimmed tool carries.
 *
 * Exported so {@link stubChars} and the footprint weigh the SAME shape
 * `trimToolDefinition` actually registers. Duplicating this object literal is
 * how the footprint came to report a trimmed tool as free.
 */
export declare const TRIMMED_PARAMETERS: {
    readonly type: "object";
    readonly properties: {};
    readonly additionalProperties: true;
};
/**
 * What ONE trimmed tool still costs on the wire, measured exactly the way
 * `ToolFootprint.measureFactory` measures a real one (`JSON.stringify` over
 * name + description + parameters).
 *
 * A trimmed tool is NOT removed from the registration: it keeps its name,
 * sheds its schema, and refuses by name. So its cost is small but never zero,
 * and any accounting that treats a trimmed tool as 0 chars understates what a
 * background session actually pays.
 */
export declare function stubChars(name: string, profile: ToolProfile, kind: SessionKind): number;
export declare function trimmedDescription(profile: ToolProfile, kind: SessionKind): string;
/** The refusal a trimmed tool returns when called. Names the profile and why. */
export declare function refusalText(toolName: string, profile: ToolProfile, kind: SessionKind): string;
/**
 * Append a refusal to the per-bot ledger at
 * ``{sharedDir}/{botId}/turns/tool-profile-refusals-<YYYY-MM-DD>.jsonl``.
 *
 * `turns/` on purpose: it is the one per-bot dir this gateway already writes
 * (the prefix-hash ledger and context-footprint.json live there) and that the
 * evolve-user analyzer already reads, so this needs no new dir, no new ACL and
 * no deploy-time pre-create. Mode 0644 at creation — a umask-077 default would
 * land the file 0600 and the evolve-side monitor would read nothing, which is
 * how the exec-failure ledger first went silent.
 *
 * Best-effort in the strictest sense: a telemetry write must never throw into
 * a tool call. The refusal is returned to the model either way.
 */
export declare function writeToolProfileRefusalLedger(config: ToolProfileConfig, rec: ToolProfileRefusalRecord, logger: ProfileLogger): void;
/**
 * The stand-in definition an out-of-profile tool registers under.
 *
 * Deliberately NOT an omission. Omitting the tool would make the model's call
 * fail as "tool not found" — indistinguishable from a broken deploy, and
 * invisible to the operator. Keeping the name with a one-line description and
 * a refusing `execute` collapses the prompt weight while leaving the failure
 * mode legible and recorded.
 */
export declare function trimToolDefinition(def: ToolDefinition, profile: ToolProfile, kind: SessionKind, ctx: ToolFactoryContext, config: ToolProfileConfig, logger: ProfileLogger): ToolDefinition;
/**
 * Wrap one factory so its definition is trimmed when the calling session's
 * profile does not carry it.
 *
 * FAIL-OPEN by construction: if the inner factory throws, or returns something
 * without a usable name, the original result is passed through untouched. A
 * bug in the profile layer must cost tokens, never a tool.
 */
export declare function applyToolProfile(factory: ToolFactory, config: ToolProfileConfig, logger: ProfileLogger): ToolFactory;
/**
 * Install the profile filter on an api: every subsequently registered tool is
 * wrapped. Mutates `api.registerTool` (not a Proxy) so every other property
 * stays identity-stable, the same way ToolFootprint.wrap does.
 *
 * ORDER MATTERS. Install this BEFORE `ToolFootprint.wrap`, so the footprint
 * records the ORIGINAL factories and can weigh them under every profile
 * (CE-2a) rather than re-measuring whatever this filter already trimmed.
 */
export declare function installToolProfileFilter<T extends {
    registerTool: (f: ToolFactory, ...rest: unknown[]) => unknown;
}>(api: T, config: ToolProfileConfig, logger: ProfileLogger): T;
export {};
//# sourceMappingURL=ToolProfiles.d.ts.map