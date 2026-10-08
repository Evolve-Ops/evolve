/**
 * AppRoutes — an app's explicit shortcut is answered by the app, not by the
 * bot's conversation.
 *
 * Why (internal/finding-cost-forensics-power-bot-2026-09-04.md §2, lever 7):
 * one `xplay` screenshot cost ~$2.35 and ten minutes as a conversational tool
 * loop — 13 model calls, 17 tool calls, 548k tokens of `read` results carried
 * thirteen times, sixty tools loaded, thinking on every step. It recurred at
 * $22.15 (118 calls, 2026-09-17) and $65.98 (304 calls, 2026-09-20). The work
 * is a word-list search; the only thing code cannot do is read the image.
 *
 * So a route claims the turn in `before_agent_reply`, which on OC 2026.9.2
 * runs BEFORE the model is dispatched: `{handled: true, reply}` posts `reply`
 * as the bot's answer and the session's model never runs
 * (reference_oc_before_agent_reply_is_pre_model). The move is then:
 *
 *   1. `prepare`   — the app's script returns the saved board and the vision
 *                    instructions (delta when a board is confirmed).
 *   2. ONE vision call, tool-free, JSON out
 *                    (`runtime.mediaUnderstanding.extractStructuredWithModel`).
 *   3. `resolve`   — the app's script diffs, audits once, saves, solves, and
 *                    returns the reply. A saved board several plays behind
 *                    costs one more whole-board reading, never a loop.
 *   4. ONE commentary call, tool-free, thinking off, bounded output
 *                    (`runtime.llm.complete`). Its line is checked against
 *                    the solver's words; anything else falls back to the
 *                    solver's own call.
 *
 * Both calls use the bot's own provider configuration and credentials
 * (principle-apps-inherit-bot-llm) and the model OC resolved for the turn.
 *
 * Degradation: a bot whose gateway lacks either runtime surface, or whose
 * installed app does not verify, is NOT claimed — the turn goes to the bot's
 * session exactly as before, under the app's session instruction. Once a turn
 * IS claimed it never falls back to the session: a failure ends the move with
 * its reason, because falling back is the expensive loop this exists to end.
 *
 * Declared in the app's spec as `app_route` (gallery/crossplay-coach): tools,
 * delivery, identity, cost. `tests/appRoutes.test.mjs` pins this table to it.
 */
export interface AppRouteSpec {
    /** Canonical app id (apps/appIdentity) — the attribution key. */
    readonly appId: string;
    /** Matched against the first line of the unwrapped user message. */
    readonly trigger: RegExp;
    /** The app's route entry point, relative to the bot workspace. */
    readonly entryScript: string;
    /** Workspace files that must match the installed manifest's shas. */
    readonly verifiedFiles: readonly string[];
    readonly requiresImage: boolean;
    /** Tools the route's model calls may use. Vision reads the image file
     *  directly; neither call is offered a tool. */
    readonly tools: readonly string[];
    readonly delivery: "chat_reply";
    readonly identity: "sender";
}
export declare const CROSSPLAY_ROUTE: AppRouteSpec;
export declare const APP_ROUTES: readonly AppRouteSpec[];
/** Hard bounds on the whole move — the loop this replaces had none. */
export declare const ROUTE_LIMITS: {
    /** Vision calls per move: the reading, plus one whole-board re-read when
     *  the saved board turns out to be several plays behind. */
    readonly maxVisionCalls: 2;
    readonly maxCommentaryCalls: 1;
    readonly visionTimeoutMs: 45000;
    readonly commentaryTimeoutMs: 20000;
    readonly scriptTimeoutMs: 25000;
    readonly commentaryMaxTokens: 200;
    readonly maxImageBytes: number;
};
export interface RouteArgs {
    gameId: string;
    fresh: boolean;
}
export interface RouteMatch {
    spec: AppRouteSpec;
    args: RouteArgs;
    mediaRefs: string[];
}
/**
 * Image references from OC's inbound media notes, in order. OC writes
 * `[media attached: <ref> (<mime>) "<name>"]` (or `[media attached i/n: …]`)
 * into the prompt; `<ref>` is `media://inbound/<file>` for managed inbound
 * media, else an absolute path. Non-image attachments are skipped.
 */
export declare function parseMediaRefs(prompt: string): string[];
/** `xplay [game <id>] [fresh]` — anything else on the line is ignored. */
export declare function parseRouteArgs(line: string): RouteArgs;
/**
 * The route this message invokes, or null. Pure — no filesystem.
 *
 * OC builds the prompt as `<media note>\n<system events>\n\n<thread
 * context>\n\n<the user's text>` (get-reply `buildReplyPromptBodies`,
 * 2026.9.2), so the shortcut is looked for at the start of each blank-line
 * separated block rather than only on the first line. Context blocks quote
 * earlier messages behind a header line or a sender prefix, so a shortcut
 * someone else sent earlier never starts a block.
 */
export declare function matchAppRoute(prompt: string, routes?: readonly AppRouteSpec[]): RouteMatch | null;
/**
 * The route whose shortcut this message starts with, WITHOUT the route's
 * image requirement — what the user asked for, not whether the route can
 * answer it. Pure. Shared by ``matchAppRoute`` (routing) and
 * ``shortcutAttribution`` (attribution), so the two can never disagree on
 * what counts as the shortcut.
 */
export declare function matchShortcut(prompt: string, routes?: readonly AppRouteSpec[]): {
    spec: AppRouteSpec;
    line: string;
    mediaRefs: string[];
} | null;
/** This bot's installed, status-active manifest for ``appId``, or null. */
export declare function findInstalledManifest(appId: string, workspaceDir: string, statusAllows: ManifestStatusPredicate): any | null;
/**
 * The app a USER message explicitly invoked by its declared shortcut, or
 * null — the ``app_route`` attribution signal for a turn the route did NOT
 * claim (the gateway lacks a runtime surface, the install does not verify, no
 * resolved model, or no image), so the bot's session serves the shortcut
 * under the app's instruction. Before this, only a CLAIMED move recorded
 * ``app_route``; every session-served shortcut resolved ``none`` (incident
 * 2026-09-20 §4 — the lane's turns were all session-served).
 *
 * Requires the app to be installed and active; NOT the route's file hashes —
 * a hash mismatch stops the route from running a script, it does not make the
 * user's shortcut mean some other app. A route that needs an image attributes
 * a text-only shortcut only when the installed manifest says the keyword is
 * required (``usage.trigger_recognition.requires_keyword: true``): then the
 * keyword alone is the app's explicit signal. ``hasImage`` lets a hook whose
 * message omits OC's media note report an attachment it saw another way.
 * Never throws.
 */
export declare function shortcutAttribution(prompt: string, workspaceDir: string, statusAllows: ManifestStatusPredicate, opts?: {
    hasImage?: boolean;
    routes?: readonly AppRouteSpec[];
}): string | null;
export type ManifestStatusPredicate = (manifest: unknown) => boolean;
/**
 * Is this route's app installed on the bot, active, and byte-identical to what
 * its manifest recorded? Returns the reason when not — a route never runs a
 * script the install did not put there.
 */
export declare function checkRouteInstalled(spec: AppRouteSpec, workspaceDir: string, statusAllows: ManifestStatusPredicate): {
    ok: true;
} | {
    ok: false;
    reason: string;
};
export interface ResolvedImage {
    file: string;
    buffer: Buffer;
    mime: string;
    width: number | null;
    height: number | null;
}
/**
 * Resolve a media note reference to a file inside OC's inbound media store.
 *
 * The note is prompt TEXT, and a user can type one. So the reference is
 * reduced to a basename and looked up ONLY under `<stateDir>/media/inbound/`,
 * and the bytes must be an image — a typed `[media attached: /etc/passwd]`
 * can never send a file off the box.
 */
export declare function resolveInboundImage(ref: string, stateDir: string): ResolvedImage | null;
/**
 * Input tokens an image costs a vision model, estimated: Anthropic's
 * documented `w·h/750` after the provider's downscale to a 1568-px long edge
 * and ~1.15 MP. The media-understanding runtime returns no usage, so this is
 * what the receipt can say — marked `usage_source: "estimated"`.
 */
export declare function estimateImageTokens(width: number | null, height: number | null): number;
export interface ModelCallReceipt {
    kind: "vision" | "commentary";
    provider: string;
    model: string;
    inputTokens: number;
    outputTokens: number;
    cacheReadTokens: number;
    cacheWriteTokens: number;
    usageSource: "provider" | "estimated";
    /** Provider-reported USD when the runtime returned it; else priced later. */
    costUsd: number | null;
    latencyMs: number;
    ok: boolean;
}
export interface RouteRunResult {
    reply: string;
    outcome: string;
    calls: ModelCallReceipt[];
    latencyMs: number;
    mode: string | null;
}
export interface VisionRequest {
    image: ResolvedImage;
    instructions: string;
    schemaName: string;
    jsonSchema: unknown;
}
export interface CompletionRequest {
    system: string;
    user: string;
    maxTokens: number;
}
export interface RouteRuntime {
    /** One tool-free vision call. Resolves to the parsed JSON object. */
    vision(req: VisionRequest): Promise<{
        parsed: unknown;
        text: string;
        provider: string;
        model: string;
    }>;
    /** One tool-free, thinking-off completion. */
    complete(req: CompletionRequest): Promise<{
        text: string;
        provider: string;
        model: string;
        usage?: {
            inputTokens?: number;
            outputTokens?: number;
            cacheReadTokens?: number;
            cacheWriteTokens?: number;
            costUsd?: number;
        };
    }>;
    /** Run the app's route script; resolves to its parsed stdout JSON. */
    script(args: string[], stdin: string | null): Promise<any>;
}
interface Logger {
    info(m: string): void;
    warn(m: string): void;
}
/**
 * Pick the "Call:" line: the model's, if it is one line of at most 400
 * characters, starts with `Call:` and names at least one of the words the
 * solver ranked; otherwise the solver's own call. The model can colour the
 * recommendation; it cannot introduce a play.
 */
export declare function acceptCallLine(text: string, allowedWords: readonly string[], fallback: string): string;
/**
 * Run one move. Never throws: every failure becomes the reply that says so.
 * `image` is already resolved; `runtime` is the OC seam (faked in tests).
 */
export declare function runAppRoute(match: RouteMatch, image: ResolvedImage, runtime: RouteRuntime, logger: Logger, now?: () => number): Promise<RouteRunResult>;
/** Can this gateway run a route at all? Checked BEFORE a turn is claimed. */
export declare function runtimeSupportsRoutes(api: any): boolean;
export declare function runScriptJson(workspaceDir: string, entryScript: string, args: string[], stdin: string | null, timeoutMs?: number): Promise<any>;
/**
 * The OC runtime behind `RouteRuntime`. `provider`/`model` are the ones OC
 * resolved for this turn (the `before_agent_reply` hook context carries
 * `modelProviderId` / `modelId`) — the same model the session would have used.
 */
export declare function openClawRouteRuntime(api: any, opts: {
    workspaceDir: string;
    entryScript: string;
    provider: string;
    model: string;
    purpose: string;
}): RouteRuntime;
export interface RouteReceiptSink {
    (result: RouteRunResult, match: RouteMatch, ctx: any): void;
}
/**
 * Glue between OC's hooks and the pipeline, one instance per TurnObserver.
 *
 *   - ``claimsPrompt`` — ``before_model_resolve``: a routed message needs no
 *     model routing, tier classification or preflight call. Cheap: a regex,
 *     then a cached install check.
 *   - ``handle`` — ``before_agent_reply``: run the move, return the claim.
 *   - ``consumeRouted`` — ``agent_end``: if OC ever fires it for a claimed
 *     run, the main session must not record a turn for it; the app's receipt
 *     already did.
 */
export declare class AppRouteHandler {
    private readonly api;
    private readonly logger;
    private readonly statusAllows;
    private readonly sink;
    private readonly routes;
    private readonly installCache;
    private readonly routedRuns;
    private readonly warned;
    private lastWorkspaceDir;
    constructor(api: any, logger: Logger, statusAllows: ManifestStatusPredicate, sink: RouteReceiptSink, routes?: readonly AppRouteSpec[]);
    private warnOnce;
    private stateDir;
    private workspaceDir;
    private installed;
    private eligible;
    claimsPrompt(prompt: string, ctx: any): boolean;
    /** ``shortcutAttribution`` for a user-triggered turn (attribution only —
     *  never claims, never changes routing). Never throws. */
    shortcutAppId(prompt: string, ctx: any, hasImage?: boolean): string | null;
    consumeRouted(runId: unknown): boolean;
    handle(ctx: any, event: any): Promise<{
        handled: true;
        reply: {
            text: string;
        };
        reason: string;
    } | undefined>;
}
export {};
//# sourceMappingURL=AppRoutes.d.ts.map