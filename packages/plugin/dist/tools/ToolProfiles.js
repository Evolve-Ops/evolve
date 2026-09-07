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
import * as fs from "fs";
import * as path from "path";
// ── Session-kind classification ─────────────────────────────────────────────
//
// MIRROR of packages/analyzer/session_kinds.py. Both sides are pinned against
// packages/analyzer/tests/fixtures/session-kind-cases.json — a rule change that
// lands on only one side reddens the other suite. Keep the two in step.
/** Substrings marking a key as Evolve's own dispatch. */
export const EVOLVE_TAGS = [":evolve:", ":explicit:evolve"];
/** Substrings marking a key as schedule-triggered. */
export const SCHEDULED_TAGS = [":cron:", ":heartbeat", ":scheduled"];
/** Route-segment values that are NOT channel names. */
export const NON_CHANNEL_ROUTES = new Set([
    "explicit", "cron", "main", "subagent", "heartbeat", "scheduled",
]);
/**
 * Session-id prefixes that mark an `explicit` session as a PERSON at a
 * keyboard rather than a program. The admin UI's chat drawer dispatches
 * `openclaw agent --session-id admin-ui-<page>` (`evo.proxy
 * .derive_session_id`) — an explicit id, but an interactive operator
 * conversation, so it classifies as `user` and keeps the full tool surface.
 */
export const CONSOLE_SESSION_PREFIXES = ["admin-ui"];
const EXPLICIT_ROUTE = "explicit";
const SUBAGENT_ROUTE = "subagent";
/**
 * `{kind, channel}` for one session key. `channel` is what the gateway
 * recorded for the turn, if anything — a hint only: a key that names its
 * channel in the route segment is recognized without it.
 */
export function classifySessionKind(sessionKey, channel) {
    const key = typeof sessionKey === "string" ? sessionKey : "";
    const ch = typeof channel === "string" && channel ? channel : null;
    if (EVOLVE_TAGS.some((tag) => key.includes(tag))) {
        return { kind: "evolve_internal", channel: null };
    }
    if (SCHEDULED_TAGS.some((tag) => key.includes(tag))) {
        return { kind: "scheduled", channel: ch };
    }
    if (ch)
        return { kind: "user", channel: ch };
    const parts = key.split(":");
    const route = parts.length > 2 ? parts[2] : "";
    if (route === EXPLICIT_ROUTE) {
        const tail = parts.length > 3 ? parts[3] : "";
        const console_ = CONSOLE_SESSION_PREFIXES.find((p) => tail.startsWith(p));
        // An operator typing into the admin UI's chat drawer: explicit id, live
        // human — a user session, and never trimmed.
        if (console_)
            return { kind: "user", channel: console_ };
        return { kind: "oneshot", channel: null };
    }
    if (route === SUBAGENT_ROUTE)
        return { kind: "subagent", channel: null };
    if (route && !NON_CHANNEL_ROUTES.has(route)) {
        // The route segment IS the channel name (the gateway just did not pass
        // one) — e.g. "agent:main:telegram:direct:@someone".
        return { kind: "user", channel: route };
    }
    return { kind: "other", channel: null };
}
/** The profile a session gets when nothing else applies. Never trims. */
export const FULL_PROFILE_ID = "full";
/**
 * Tools a session with no live human speaker keeps. Everything absent from
 * this list is trimmed for the kinds mapped to `no_live_speaker` below.
 *
 * Weights (chars of `{name, description, parameters}`, measured from a live
 * pod's context-footprint.json, 2026-09-02) are given so the cost of adding a
 * name back is visible at the point of decision.
 */
export const NO_LIVE_SPEAKER_TOOLS = [
    "defer", // 1454 — a background turn may commit to act later
    "record_application", // 2772 — a forge/build turn ships something app-shaped
    "expand_app", // 862 — reading how to use an installed app
    "directory_lookup", // 764 — resolving a person; READ only
    "pod_state", // 2435 — grounded pod reads (primary bot)
    // Google suite: a scheduled digest genuinely reads mail and calendar.
    "gmail_list_messages", "gmail_get_message", "gmail_list_labels",
    "gmail_label_message", "gmail_mark_read", "gmail_mark_unread",
    "gmail_archive_message", "gmail_trash_message", "gmail_delete_message",
    "gmail_send",
    "calendar_list_events", "calendar_create_event",
    "drive_list_files", "drive_search", "drive_read_file", "drive_write_file",
];
export const TOOL_PROFILES = {
    [FULL_PROFILE_ID]: {
        id: FULL_PROFILE_ID,
        tools: "full",
        why: "every registered tool (the default for any session this table does not name)",
    },
    no_live_speaker: {
        id: "no_live_speaker",
        tools: NO_LIVE_SPEAKER_TOOLS,
        why: "this session has no live human speaker, so the roster, channel-config, " +
            "directory-write, tier-change and help/intake tools cannot apply",
    },
    evolve_dispatch: {
        id: "evolve_dispatch",
        tools: [],
        why: "Evolve dispatched this session for one judgment and reads only the " +
            "reply text, so no Evolve tool keeps its schema. Every tool is still " +
            "REGISTERED, as a name-only stub that refuses by name — trimming is " +
            "not removal, and this profile is no exception",
    },
};
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
export const PROFILE_BY_KIND = {
    scheduled: "no_live_speaker",
    oneshot: "no_live_speaker",
    evolve_internal: "evolve_dispatch",
};
/** The profile for a session kind. Always resolves — falls back to full. */
export function resolveToolProfile(kind) {
    const id = PROFILE_BY_KIND[kind];
    return (id && TOOL_PROFILES[id]) || TOOL_PROFILES[FULL_PROFILE_ID];
}
/** True iff `profile` carries `toolName`. A "full" profile carries everything. */
export function profileAllows(profile, toolName) {
    return profile.tools === "full" || profile.tools.includes(toolName);
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
export const TRIMMED_PARAMETERS = {
    type: "object",
    properties: {},
    additionalProperties: true,
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
export function stubChars(name, profile, kind) {
    return JSON.stringify({
        name,
        description: trimmedDescription(profile, kind),
        parameters: TRIMMED_PARAMETERS,
    }).length;
}
export function trimmedDescription(profile, kind) {
    return (`Not available in this session (tool profile "${profile.id}", session kind ` +
        `"${kind}"). Calling it returns a refusal, not a result.`);
}
/** The refusal a trimmed tool returns when called. Names the profile and why. */
export function refusalText(toolName, profile, kind) {
    return (`${toolName} is not available in this session. Tool profile ` +
        `"${profile.id}" applies because this is a "${kind}" session: ` +
        `${profile.why}. Nothing was done. If this session genuinely needs ` +
        `${toolName}, an operator widens the profile — the refusal is recorded.`);
}
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
export function writeToolProfileRefusalLedger(config, rec, logger) {
    try {
        const dir = path.join(config.sharedDir, config.botId, "turns");
        fs.mkdirSync(dir, { recursive: true });
        const day = rec.ts.slice(0, 10); // YYYY-MM-DD (UTC, from toISOString)
        const file = path.join(dir, `tool-profile-refusals-${day}.jsonl`);
        fs.appendFileSync(file, JSON.stringify(rec) + "\n", { mode: 0o644 });
    }
    catch (err) {
        try {
            logger.debug?.(`Evolve tool profiles: ledger append failed (continuing): ${err}`);
        }
        catch {
            /* logging must never throw out of the hot path */
        }
    }
}
/**
 * The stand-in definition an out-of-profile tool registers under.
 *
 * Deliberately NOT an omission. Omitting the tool would make the model's call
 * fail as "tool not found" — indistinguishable from a broken deploy, and
 * invisible to the operator. Keeping the name with a one-line description and
 * a refusing `execute` collapses the prompt weight while leaving the failure
 * mode legible and recorded.
 */
export function trimToolDefinition(def, profile, kind, ctx, config, logger) {
    const name = String(def?.name ?? "unknown_tool");
    const text = refusalText(name, profile, kind);
    return {
        // Spread first so any field a future tool definition adds (and the
        // gateway may need) survives; the three heavy ones below are replaced.
        // Every tool today returns exactly {name, description, parameters,
        // execute}, so today this spread carries nothing extra.
        ...def,
        name,
        description: trimmedDescription(profile, kind),
        parameters: { ...TRIMMED_PARAMETERS },
        async execute() {
            writeToolProfileRefusalLedger(config, {
                ts: new Date().toISOString(),
                bot_id: config.botId,
                tool_name: name,
                profile: profile.id,
                session_kind: kind,
                session_key: typeof ctx.sessionKey === "string" ? ctx.sessionKey : null,
                session_id: typeof ctx.sessionId === "string" ? ctx.sessionId : null,
            }, logger);
            try {
                logger.info?.(`Evolve tool profiles: refused ${name} under profile ${profile.id} ` +
                    `(session kind ${kind})`);
            }
            catch { /* never throw out of a tool call */ }
            return { content: [{ type: "text", text }], isError: true };
        },
    };
}
/**
 * Wrap one factory so its definition is trimmed when the calling session's
 * profile does not carry it.
 *
 * FAIL-OPEN by construction: if the inner factory throws, or returns something
 * without a usable name, the original result is passed through untouched. A
 * bug in the profile layer must cost tokens, never a tool.
 */
export function applyToolProfile(factory, config, logger) {
    return (ctx) => {
        const def = factory(ctx);
        try {
            const name = typeof def?.name === "string" ? def.name : "";
            if (!name)
                return def;
            const { kind } = classifySessionKind(ctx?.sessionKey, ctx?.messageChannel);
            const profile = resolveToolProfile(kind);
            if (profileAllows(profile, name))
                return def;
            return trimToolDefinition(def, profile, kind, ctx ?? {}, config, logger);
        }
        catch (err) {
            try {
                logger.warn(`Evolve tool profiles: passthrough after error on a tool definition: ${err}`);
            }
            catch { /* never throw out of registration */ }
            return def;
        }
    };
}
/**
 * Install the profile filter on an api: every subsequently registered tool is
 * wrapped. Mutates `api.registerTool` (not a Proxy) so every other property
 * stays identity-stable, the same way ToolFootprint.wrap does.
 *
 * ORDER MATTERS. Install this BEFORE `ToolFootprint.wrap`, so the footprint
 * records the ORIGINAL factories and can weigh them under every profile
 * (CE-2a) rather than re-measuring whatever this filter already trimmed.
 */
export function installToolProfileFilter(api, config, logger) {
    const original = api.registerTool.bind(api);
    api.registerTool = function (factory, ...rest) {
        return original(applyToolProfile(factory, config, logger), ...rest);
    };
    return api;
}
//# sourceMappingURL=ToolProfiles.js.map