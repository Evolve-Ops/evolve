/**
 * EvoDispatchClient
 *
 * HTTP client for the /api/evo/dispatch endpoint on the admin server.
 * Spec: internal/spec-evo-wizard-2026-05-05.md.
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
export type EvoDispatchMode = "speak" | "send_direct";
export interface EvoDispatchResult {
    subcommand: string;
    role: "primary" | "secondary";
    mode: EvoDispatchMode;
    system_append: string | null;
    direct_message: string | null;
    wizard_session_id: string | null;
    /**
     * Bare user-facing body (no "respond verbatim" preamble) for the
     * plugin to deliver directly via the channel API on Telegram. When
     * present, the plugin sends this to the user and tells the LLM to
     * stay silent rather than relying on it to echo system_append. Null
     * for wizard / conversational paths where the LLM is supposed to
     * engage with the user.
     */
    direct_send_message: string | null;
    /**
     * One-line description of what this subcommand does (e.g. "show
     * available evo commands", "one-time Google OAuth client setup
     * for this bot"). Threaded into the plugin's stay-silent
     * directive so the LLM is briefed in plain English instead of
     * left to speculate. Optional — null falls back to the directive's
     * generic wording. Auto-derived server-side from the subcommand
     * registry's short_help.
     */
    subcommand_brief: string | null;
    /**
     * Session-scoped tier override directive from the dispatch handler.
     * Audit #69 Phase B — populated by ``evo tier {auto|fast|standard|
     * power}`` handlers. Shape:
     *   {choice: "auto"|"fast"|"standard"|"power",
     *    consent_source: "evo_keyword"}
     * Null on every other subcommand (most calls).
     *
     * When present, the plugin calls
     *   modelRouter.setUserTier(sessionKey, choice, consent_source)
     * after delivering the acknowledgment text. Takes effect on the
     * NEXT user turn — routing on this turn already ran.
     */
    session_tier_override: {
        choice: string;
        consent_source: string;
    } | null;
}
/**
 * Why a dispatch failed. Lets callers tailor the user-facing message and
 * telemetry instead of collapsing every failure into a single null (which
 * is what let the bot LLM confabulate a fake help screen on auth/transport
 * breaks — the bug this discriminated result exists to kill).
 *
 *   - ``unreachable``  — transport failure (admin daemon down, ECONNREFUSED,
 *                        timeout). No HTTP response came back at all.
 *   - ``unauthorized`` — HTTP 401/403. The loopback RPC was rejected (device
 *                        auth / pairing). Recurs whenever a device pairs and
 *                        the plugin's RPC isn't exempted (see the auth-401
 *                        memory). ``status`` carries the code.
 *   - ``empty``        — a valid envelope came back but the handler produced
 *                        nothing deliverable (no system_append, no
 *                        direct_send_message). Synthesised by the caller, not
 *                        this client, because emptiness is evaluated against
 *                        the same fields the delivery path uses.
 *   - ``error``        — any other non-2xx status, or a 2xx body that isn't a
 *                        valid envelope (malformed / schema drift). ``status``
 *                        carries the code when there was one.
 */
export type EvoDispatchFailureReason = "unreachable" | "unauthorized" | "empty" | "error";
export interface EvoDispatchFailure {
    ok: false;
    reason: EvoDispatchFailureReason;
    /** HTTP status code, when a response actually came back. */
    status?: number;
}
export interface EvoDispatchSuccess {
    ok: true;
    result: EvoDispatchResult;
}
/**
 * Discriminated result of ``EvoDispatchClient.dispatch``. Replaces the old
 * ``EvoDispatchResult | null`` so the caller can fail LOUD (honest user
 * message + telemetry) instead of silently handing the turn to the LLM.
 */
export type EvoDispatchOutcome = EvoDispatchSuccess | EvoDispatchFailure;
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
export declare function classifyDispatchResponse(status: number, body: any): EvoDispatchOutcome;
/**
 * Honest, non-hallucinated user-facing message for a failed evo command.
 * Surface-aware-help-style accuracy rule: say only what is actually known
 * (transport vs auth vs internal), never invent capabilities or a help
 * screen. Pure + exported so the wording is testable and reused by every
 * fall-through site.
 */
export declare function evoFailureUserMessage(reason: EvoDispatchFailureReason): string;
export interface WizardTurnResult {
    system_append: string;
    /**
     * Body to direct-send via channel transport (Telegram Bot API, etc.)
     * for verbatim-mode wizard phases. Null on agenda-mode phases where
     * the LLM is expected to engage. Mirrors EvoDispatchResult's field
     * of the same name. See evolve_admin.evo.wizard.engine.TurnResult
     * and evolve_admin.evo.wizard.phases.RenderMode for the mode split.
     */
    direct_send_message: string | null;
    /** Same key when more turns expected; null when wrap-up happened on this turn. */
    wizard_session_id: string | null;
    phase: string;
    completed: boolean;
    /**
     * One-line description of which wizard the user is in (e.g.
     * "`evo setup-google` — per-bot Google OAuth client setup wizard,
     * ~4 steps"). Plugin's before_prompt_build directive uses this to
     * brief the LLM in plain English so it stops speculating about
     * what the user's mid-wizard reply means. Optional — null falls
     * back to the directive's generic wording. Derived server-side
     * from the wizard state's audience field.
     */
    subcommand_brief: string | null;
}
export declare class EvoDispatchClient {
    /**
     * Admin-daemon unix-socket path (``{sharedDir}/admin-daemon.sock``). Threaded
     * in from the plugin's resolved ``sharedDir`` via ``adminDaemonSocketPath`` so
     * it is platform-correct (macOS ``/Users/Shared/evolve`` vs Linux
     * ``/var/lib/evolve``). Optional only so existing no-arg call sites keep
     * compiling; when omitted the helper's macOS-shaped default is used (correct
     * on macOS pods, wrong on Linux — every real caller passes the resolved path).
     */
    private readonly socketPath?;
    constructor(socketPath?: string);
    /**
     * Issue one request to the admin daemon over its unix socket, normalizing
     * the response into the legacy ``{status, body}`` shape this client's
     * classifiers expect. Throws ``AdminSocketUnavailable`` on transport
     * failure — the same condition the old TCP ``req.on("error")`` rejected on
     * — so the ``unreachable`` arm stays intact.
     */
    private adminRequest;
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
    dispatch(botId: string, channel: string | null, senderExternalId: string | null, rawText: string): Promise<EvoDispatchOutcome>;
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
    findActiveWizard(botId: string, channel: string | null, senderExternalId: string | null): Promise<string | null>;
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
    wizardTurn(botId: string, wizardSessionId: string, userMessage: string): Promise<WizardTurnResult | null>;
}
//# sourceMappingURL=EvoDispatchClient.d.ts.map