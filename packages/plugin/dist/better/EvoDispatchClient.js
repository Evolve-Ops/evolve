/**
 * EvoDispatchClient
 *
 * HTTP client for the /api/evo/dispatch endpoint on the admin server.
 * Spec: docs/spec-evo-wizard-2026-05-05.md.
 *
 * Mirrors BetterEngineClient.ts in style — same admin transport, same
 * request shape, same fail-soft posture. Kept as a separate class to keep
 * the evo surface decoupled from BetterEngine internals.
 *
 * Transport: the admin-daemon UNIX SOCKET (``{sharedDir}/admin-daemon.sock``),
 * NOT loopback TCP :5050. The socket path is the keystone of the fix for the
 * "evo dead pod-wide" bug: admin auth is ON by default (#2621), so every TCP
 * path is gated by ``_enforce_device_auth`` and a cookieless plugin RPC gets
 * ``401 device not paired`` — which #3260's fail-loud surfaces to the user as
 * "auth/pairing issue". The unix socket is exempted server-side
 * (``peer_bot_route_exempt`` + per-route bot_id bind, #3265) and made reachable
 * on both OSes (#3263 group-ACE, #3267 SO_PEERCRED). Moving the transport here
 * is what makes the exemption apply end-to-end (RPC-2). The discriminated
 * ``EvoDispatchOutcome`` (#3260) is preserved exactly — a socket-unavailable
 * condition maps to ``reason: "unreachable"``, the same arm a TCP transport
 * failure produced.
 */
import { adminSocketRequest } from "../util/adminSocket.js";
/**
 * Classify a dispatch HTTP response into a discriminated outcome. Pure +
 * exported so the status→reason mapping is unit-testable without standing
 * up an admin server (the transport-failure → ``unreachable`` arm lives in
 * ``dispatch`` itself, since it has no HTTP response to classify).
 *
 * Note: ``empty`` is intentionally NOT produced here. A valid envelope that
 * happens to carry no deliverable body is still a well-formed success at the
 * transport layer; the caller flags ``empty`` against the same fields its
 * delivery path consumes (system_append / direct_send_message), so the two
 * never disagree.
 */
export function classifyDispatchResponse(status, body) {
    if (status === 401 || status === 403) {
        return { ok: false, reason: "unauthorized", status };
    }
    if (status < 200 ||
        status >= 300 ||
        !body ||
        typeof body !== "object" ||
        typeof body.mode !== "string") {
        return { ok: false, reason: "error", status };
    }
    return { ok: true, result: body };
}
/**
 * Honest, non-hallucinated user-facing message for a failed evo command.
 * Surface-aware-help-style accuracy rule: say only what is actually known
 * (transport vs auth vs internal), never invent capabilities or a help
 * screen. Pure + exported so the wording is testable and reused by every
 * fall-through site.
 */
export function evoFailureUserMessage(reason) {
    switch (reason) {
        case "unreachable":
            return ("⚠️ evo can't reach its admin service right now (looks like a " +
                "connection issue). Your message wasn't lost — try again shortly, " +
                "or check the admin logs.");
        case "unauthorized":
            return ("⚠️ evo can't reach its admin service right now (looks like an " +
                "auth/pairing issue). Your message wasn't lost — try again shortly, " +
                "or check the admin logs.");
        case "empty":
            return ("⚠️ evo hit an internal error handling that command. Nothing was " +
                "changed — try again shortly, or check the admin logs.");
        case "error":
        default:
            return ("⚠️ evo hit an error reaching its admin service. Try again shortly, " +
                "or check the admin logs.");
    }
}
export class EvoDispatchClient {
    /**
     * Admin-daemon unix-socket path (``{sharedDir}/admin-daemon.sock``). Threaded
     * in from the plugin's resolved ``sharedDir`` via ``adminDaemonSocketPath`` so
     * it is platform-correct (macOS ``/Users/Shared/evolve`` vs Linux
     * ``/var/lib/evolve``). Optional only so existing no-arg call sites keep
     * compiling; when omitted the helper's macOS-shaped default is used (correct
     * on macOS pods, wrong on Linux — every real caller passes the resolved path).
     */
    socketPath;
    constructor(socketPath) {
        this.socketPath = socketPath;
    }
    /**
     * Issue one request to the admin daemon over its unix socket, normalizing
     * the response into the legacy ``{status, body}`` shape this client's
     * classifiers expect. Throws ``AdminSocketUnavailable`` on transport
     * failure — the same condition the old TCP ``req.on("error")`` rejected on
     * — so the ``unreachable`` arm stays intact.
     */
    async adminRequest(method, urlPath, body) {
        const res = await adminSocketRequest({
            method,
            path: urlPath,
            body,
            socketPath: this.socketPath,
        });
        return { status: res.status, body: res.body };
    }
    /**
     * Dispatch an evo command and return a discriminated outcome.
     *
     * Was ``EvoDispatchResult | null``; the null collapsed transport failure,
     * auth rejection, and malformed responses into one indistinguishable
     * value, and the caller's only move was to fall through to the bot's LLM —
     * which then confabulated a confident-but-fake answer with ZERO error
     * signal. The discriminated failure lets the caller fail LOUD instead:
     * tailor the user message, emit telemetry, and stay silent. NEVER throws.
     */
    async dispatch(botId, channel, senderExternalId, rawText) {
        let resp;
        try {
            resp = await this.adminRequest("POST", "/api/evo/dispatch", {
                bot_id: botId,
                channel,
                sender_external_id: senderExternalId,
                raw_text: rawText,
            });
        }
        catch {
            // No HTTP response at all — admin daemon socket unreachable / down /
            // refused / timed out (AdminSocketUnavailable or any other throw).
            return { ok: false, reason: "unreachable" };
        }
        return classifyDispatchResponse(resp.status, resp.body);
    }
    /**
     * Find the active wizard's session_id for the caller identified by
     * (botId, channel, senderExternalId). Used by the plugin's
     * before_agent_run / before_model_resolve handlers to recover from
     * in-memory state loss after a gateway restart — the in-memory
     * BetterSessionState map is wiped by the restart, but the wizard
     * state file on disk is intact, so the admin server can re-derive
     * the session id and the plugin can rejoin the wizard mid-flight.
     *
     * Returns:
     *   - The session_id string when an active wizard exists for this caller
     *   - null when there's no active wizard (no in-flight state for this user)
     *   - null on transport failure (treat as "no wizard, continue normally")
     *
     * Always best-effort — never throws. Costs ~1-2ms when admin is on
     * localhost; called once per user-driven turn when wizardSessionId
     * is null in plugin state.
     */
    async findActiveWizard(botId, channel, senderExternalId) {
        try {
            const params = new URLSearchParams({ bot_id: botId });
            if (channel)
                params.set("channel", channel);
            if (senderExternalId)
                params.set("sender_external_id", senderExternalId);
            const { body } = await this.adminRequest("GET", `/api/evo/wizard/active?${params.toString()}`);
            if (!body || typeof body !== "object")
                return null;
            const wsid = body.wizard_session_id;
            return typeof wsid === "string" && wsid.length > 0 ? wsid : null;
        }
        catch {
            return null;
        }
    }
    /**
     * Process one mid-wizard user message. Plugin calls this on every user
     * turn while a wizardSessionId is set on the BetterSessionState.
     *
     * Returns null on transport failure (caller stays in wizard mode and
     * lets the bot's LLM respond normally — preferable to clearing the
     * session and abandoning the conversation over a flaky network blip).
     * A 404 from the server means the wizard already completed or the
     * state file is missing; signaled as { wizard_session_id: null } so
     * the caller clears its session reference and resumes normal routing.
     */
    async wizardTurn(botId, wizardSessionId, userMessage) {
        try {
            const { body: result } = await this.adminRequest("POST", "/api/evo/wizard/turn", {
                bot_id: botId,
                wizard_session_id: wizardSessionId,
                user_message: userMessage,
            });
            if (!result || typeof result !== "object")
                return null;
            // 404 returns { error, wizard_session_id: null } — surface that
            // shape so the caller knows to clear its session reference.
            if (typeof result.system_append !== "string"
                && result.wizard_session_id === null) {
                return {
                    system_append: "",
                    direct_send_message: null,
                    wizard_session_id: null,
                    phase: "",
                    completed: true,
                    subcommand_brief: null,
                };
            }
            if (typeof result.system_append !== "string")
                return null;
            // Tolerate older admin servers that don't return the newer
            // optional fields (rolling-deploy window).
            if (typeof result.direct_send_message === "undefined") {
                result.direct_send_message = null;
            }
            if (typeof result.subcommand_brief === "undefined") {
                result.subcommand_brief = null;
            }
            return result;
        }
        catch {
            return null;
        }
    }
}
//# sourceMappingURL=EvoDispatchClient.js.map