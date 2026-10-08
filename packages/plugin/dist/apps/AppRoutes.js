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
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { unwrapUserMessage } from "../observer/messageUnwrap.js";
export const CROSSPLAY_ROUTE = {
    appId: "crossplay-coach",
    trigger: /^xplay\b/i,
    entryScript: "scripts/crossplay_route.py",
    verifiedFiles: ["scripts/crossplay_route.py", "scripts/crossplay_coach.py"],
    requiresImage: true,
    tools: ["read"],
    delivery: "chat_reply",
    identity: "sender",
};
export const APP_ROUTES = [CROSSPLAY_ROUTE];
/** Hard bounds on the whole move — the loop this replaces had none. */
export const ROUTE_LIMITS = {
    /** Vision calls per move: the reading, plus one whole-board re-read when
     *  the saved board turns out to be several plays behind. */
    maxVisionCalls: 2,
    maxCommentaryCalls: 1,
    visionTimeoutMs: 45_000,
    commentaryTimeoutMs: 20_000,
    scriptTimeoutMs: 25_000,
    commentaryMaxTokens: 200,
    maxImageBytes: 12 * 1024 * 1024,
};
const MEDIA_NOTE_RE = /\[media attached(?:\s+\d+\/\d+)?:\s*([^\]\n]+)\]/gi;
const IMAGE_EXT_RE = /\.(png|jpe?g|webp|gif|heic)$/i;
/**
 * Image references from OC's inbound media notes, in order. OC writes
 * `[media attached: <ref> (<mime>) "<name>"]` (or `[media attached i/n: …]`)
 * into the prompt; `<ref>` is `media://inbound/<file>` for managed inbound
 * media, else an absolute path. Non-image attachments are skipped.
 */
export function parseMediaRefs(prompt) {
    const refs = [];
    for (const m of String(prompt ?? "").matchAll(MEDIA_NOTE_RE)) {
        const body = m[1].trim();
        if (/^\d+\s+files$/i.test(body))
            continue;
        const ref = body.split(/\s+/)[0] ?? "";
        const mime = /\(([^)]+)\)/.exec(body)?.[1]?.toLowerCase() ?? "";
        const isImage = mime ? mime.startsWith("image") : IMAGE_EXT_RE.test(ref);
        if (ref && isImage)
            refs.push(ref);
    }
    return refs;
}
/** `xplay [game <id>] [fresh]` — anything else on the line is ignored. */
export function parseRouteArgs(line) {
    const words = line.trim().split(/\s+/).slice(1);
    let gameId = "default";
    let fresh = false;
    for (let i = 0; i < words.length; i++) {
        const w = words[i].toLowerCase();
        if (w === "game" && words[i + 1] && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(words[i + 1])) {
            gameId = words[i + 1];
            i++;
        }
        else if (w === "fresh" || w === "new") {
            fresh = true;
        }
    }
    return { gameId, fresh };
}
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
export function matchAppRoute(prompt, routes = APP_ROUTES) {
    const hit = matchShortcut(prompt, routes);
    if (!hit)
        return null;
    if (hit.spec.requiresImage && hit.mediaRefs.length === 0)
        return null;
    return { spec: hit.spec, args: parseRouteArgs(hit.line), mediaRefs: hit.mediaRefs };
}
/**
 * The route whose shortcut this message starts with, WITHOUT the route's
 * image requirement — what the user asked for, not whether the route can
 * answer it. Pure. Shared by ``matchAppRoute`` (routing) and
 * ``shortcutAttribution`` (attribution), so the two can never disagree on
 * what counts as the shortcut.
 */
export function matchShortcut(prompt, routes = APP_ROUTES) {
    const raw = String(prompt ?? "");
    const mediaRefs = parseMediaRefs(raw);
    const body = unwrapUserMessage(raw.replace(MEDIA_NOTE_RE, "").trim());
    const heads = body.split(/\n\s*\n/)
        .map((block) => unwrapUserMessage(block).split("\n").map((l) => l.trim()).find((l) => l) ?? "")
        .filter((l) => l);
    for (const spec of routes) {
        const line = heads.find((h) => spec.trigger.test(h));
        if (line !== undefined)
            return { spec, line, mediaRefs };
    }
    return null;
}
// ── Attribution ─────────────────────────────────────────────────────────────
/** This bot's installed, status-active manifest for ``appId``, or null. */
export function findInstalledManifest(appId, workspaceDir, statusAllows) {
    let files;
    try {
        files = fs.readdirSync(path.join(workspaceDir, "manifests"))
            .filter((f) => f.endsWith(".json") && !f.startsWith(".") && !f.startsWith("_"));
    }
    catch {
        return null;
    }
    for (const fname of files) {
        let m;
        try {
            m = JSON.parse(fs.readFileSync(path.join(workspaceDir, "manifests", fname), "utf8"));
        }
        catch {
            continue;
        }
        if (m?.app_id !== appId)
            continue;
        return statusAllows(m) ? m : null;
    }
    return null;
}
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
export function shortcutAttribution(prompt, workspaceDir, statusAllows, opts = {}) {
    try {
        const hit = matchShortcut(prompt, opts.routes ?? APP_ROUTES);
        if (!hit)
            return null;
        const manifest = findInstalledManifest(hit.spec.appId, workspaceDir, statusAllows);
        if (!manifest)
            return null;
        const hasImage = hit.mediaRefs.length > 0 || opts.hasImage === true;
        if (hit.spec.requiresImage && !hasImage
            && manifest?.usage?.trigger_recognition?.requires_keyword !== true) {
            return null;
        }
        return hit.spec.appId;
    }
    catch {
        return null;
    }
}
function sha256File(file) {
    try {
        return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
    }
    catch {
        return null;
    }
}
/**
 * Is this route's app installed on the bot, active, and byte-identical to what
 * its manifest recorded? Returns the reason when not — a route never runs a
 * script the install did not put there.
 */
export function checkRouteInstalled(spec, workspaceDir, statusAllows) {
    const manifestsDir = path.join(workspaceDir, "manifests");
    let files;
    try {
        files = fs.readdirSync(manifestsDir).filter((f) => f.endsWith(".json") && !f.startsWith("."));
    }
    catch {
        return { ok: false, reason: "no manifests directory" };
    }
    for (const fname of files) {
        let m;
        try {
            m = JSON.parse(fs.readFileSync(path.join(manifestsDir, fname), "utf8"));
        }
        catch {
            continue;
        }
        if (m?.app_id !== spec.appId)
            continue;
        if (!statusAllows(m))
            return { ok: false, reason: `app ${spec.appId} is not active` };
        const recorded = new Map();
        const entries = Array.isArray(m?.package?.files) ? m.package.files : [];
        for (const e of entries) {
            if (typeof e?.path === "string" && typeof e?.sha256 === "string") {
                recorded.set(e.path, e.sha256.toLowerCase());
            }
        }
        for (const rel of spec.verifiedFiles) {
            const want = recorded.get(rel);
            if (!want)
                return { ok: false, reason: `manifest records no sha for ${rel}` };
            if (sha256File(path.join(workspaceDir, rel)) !== want) {
                return { ok: false, reason: `${rel} does not match its installed sha` };
            }
        }
        return { ok: true };
    }
    return { ok: false, reason: `app ${spec.appId} is not installed` };
}
function sniffImage(buf) {
    if (buf.length >= 24 && buf.readUInt32BE(0) === 0x89504e47) {
        return { mime: "image/png", width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
    }
    if (buf.length >= 4 && buf[0] === 0xff && buf[1] === 0xd8) {
        let i = 2;
        while (i + 9 < buf.length) {
            if (buf[i] !== 0xff) {
                i++;
                continue;
            }
            const marker = buf[i + 1];
            const len = buf.readUInt16BE(i + 2);
            if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) {
                return { mime: "image/jpeg", height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
            }
            i += 2 + len;
        }
        return { mime: "image/jpeg", width: null, height: null };
    }
    if (buf.length >= 12 && buf.toString("ascii", 0, 4) === "RIFF" && buf.toString("ascii", 8, 12) === "WEBP") {
        return { mime: "image/webp", width: null, height: null };
    }
    return null;
}
/**
 * Resolve a media note reference to a file inside OC's inbound media store.
 *
 * The note is prompt TEXT, and a user can type one. So the reference is
 * reduced to a basename and looked up ONLY under `<stateDir>/media/inbound/`,
 * and the bytes must be an image — a typed `[media attached: /etc/passwd]`
 * can never send a file off the box.
 */
export function resolveInboundImage(ref, stateDir) {
    const base = path.basename(ref.replace(/^media:\/\/inbound\//, ""));
    if (!base || base === "." || base === "..")
        return null;
    const inbound = path.join(stateDir, "media", "inbound");
    const file = path.join(inbound, base);
    if (path.dirname(file) !== inbound)
        return null;
    try {
        const st = fs.lstatSync(file);
        if (!st.isFile() || st.size <= 0 || st.size > ROUTE_LIMITS.maxImageBytes)
            return null;
        const buffer = fs.readFileSync(file);
        const sniffed = sniffImage(buffer);
        if (!sniffed)
            return null;
        return { file, buffer, ...sniffed };
    }
    catch {
        return null;
    }
}
/**
 * Input tokens an image costs a vision model, estimated: Anthropic's
 * documented `w·h/750` after the provider's downscale to a 1568-px long edge
 * and ~1.15 MP. The media-understanding runtime returns no usage, so this is
 * what the receipt can say — marked `usage_source: "estimated"`.
 */
export function estimateImageTokens(width, height) {
    if (!width || !height)
        return 1600;
    const scale = Math.min(1, 1568 / Math.max(width, height), Math.sqrt(1_150_000 / (width * height)));
    return Math.ceil((width * scale * height * scale) / 750);
}
const textTokens = (s) => Math.ceil((s ?? "").length / 4);
const failReply = (what) => `Crossplay Coach couldn't finish this move: ${what}. Nothing was changed — send the screenshot with xplay to try again.`;
/**
 * Pick the "Call:" line: the model's, if it is one line of at most 400
 * characters, starts with `Call:` and names at least one of the words the
 * solver ranked; otherwise the solver's own call. The model can colour the
 * recommendation; it cannot introduce a play.
 */
export function acceptCallLine(text, allowedWords, fallback) {
    const line = String(text ?? "").trim().replace(/\s*\n+\s*/g, " ");
    if (!/^call:/i.test(line) || line.length > 400)
        return fallback;
    const names = allowedWords.some((w) => /^[A-Za-z]{2,15}$/.test(w)
        && new RegExp(`\\b${w}\\b`, "i").test(line));
    return names ? "Call:" + line.slice(5) : fallback;
}
/**
 * Run one move. Never throws: every failure becomes the reply that says so.
 * `image` is already resolved; `runtime` is the OC seam (faked in tests).
 */
export async function runAppRoute(match, image, runtime, logger, now = Date.now) {
    const started = now();
    const calls = [];
    let mode = null;
    const done = (reply, outcome) => ({
        reply, outcome, calls, latencyMs: now() - started, mode,
    });
    let prepared;
    try {
        prepared = await runtime.script(["prepare", "--game", match.args.gameId, ...(match.args.fresh ? ["--fresh"] : [])], null);
    }
    catch (err) {
        logger.warn(`Evolve app route ${match.spec.appId}: prepare failed — ${err}`);
        return done(failReply("the app could not load the saved game"), "error");
    }
    if (!prepared?.vision)
        return done(String(prepared?.reply ?? failReply("the app returned nothing")), "error");
    let visionSpec = prepared.vision;
    let afterStale = null;
    for (let attempt = 0; attempt < ROUTE_LIMITS.maxVisionCalls; attempt++) {
        mode = String(visionSpec.mode);
        const t0 = now();
        let reading;
        let receipt;
        try {
            const out = await runtime.vision({
                image,
                instructions: String(visionSpec.instructions),
                schemaName: String(visionSpec.schema_name ?? "board"),
                jsonSchema: visionSpec.json_schema,
            });
            reading = out.parsed;
            receipt = {
                kind: "vision", provider: out.provider, model: out.model,
                inputTokens: estimateImageTokens(image.width, image.height)
                    + textTokens(String(visionSpec.instructions)) + textTokens(JSON.stringify(visionSpec.json_schema)),
                outputTokens: textTokens(out.text || JSON.stringify(out.parsed ?? "")),
                cacheReadTokens: 0, cacheWriteTokens: 0, usageSource: "estimated",
                costUsd: null, latencyMs: now() - t0, ok: true,
            };
            calls.push(receipt);
        }
        catch (err) {
            logger.warn(`Evolve app route ${match.spec.appId}: vision call failed — ${err}`);
            return done(failReply("reading the screenshot failed"), "vision_failed");
        }
        let resolved;
        try {
            resolved = await runtime.script(["resolve"], JSON.stringify({
                game_id: match.args.gameId,
                mode,
                fresh: match.args.fresh,
                vision: reading,
                ...(afterStale ? { after_stale: afterStale } : {}),
            }));
        }
        catch (err) {
            logger.warn(`Evolve app route ${match.spec.appId}: resolve failed — ${err}`);
            return done(failReply("the solver did not run"), "error");
        }
        const outcome = String(resolved?.outcome ?? "error");
        if (outcome === "stale" && attempt + 1 < ROUTE_LIMITS.maxVisionCalls && resolved.vision) {
            visionSpec = resolved.vision;
            afterStale = { ...(resolved.detail ?? {}), reason: resolved.reason };
            continue;
        }
        if (outcome !== "solved") {
            return done(String(resolved?.reply ?? failReply("the board could not be confirmed")), outcome);
        }
        // ── The one commentary call ──────────────────────────────────────────
        const req = resolved.commentary ?? {};
        const fallback = String(resolved.default_call ?? "");
        let callLine = fallback;
        const t1 = now();
        try {
            const out = await runtime.complete({
                system: String(req.system ?? ""),
                user: String(req.user ?? ""),
                maxTokens: Math.min(Number(req.max_tokens) || ROUTE_LIMITS.commentaryMaxTokens, ROUTE_LIMITS.commentaryMaxTokens),
            });
            const usage = out.usage ?? {};
            const reported = usage.inputTokens !== undefined || usage.outputTokens !== undefined;
            calls.push({
                kind: "commentary", provider: out.provider, model: out.model,
                inputTokens: reported ? Number(usage.inputTokens ?? 0) : textTokens(String(req.system) + String(req.user)),
                outputTokens: reported ? Number(usage.outputTokens ?? 0) : textTokens(out.text),
                cacheReadTokens: Number(usage.cacheReadTokens ?? 0),
                cacheWriteTokens: Number(usage.cacheWriteTokens ?? 0),
                usageSource: reported ? "provider" : "estimated",
                costUsd: typeof usage.costUsd === "number" ? usage.costUsd : null,
                latencyMs: now() - t1, ok: true,
            });
            callLine = acceptCallLine(out.text, Array.isArray(req.allowed_words) ? req.allowed_words : [], fallback);
        }
        catch (err) {
            // The plays are already computed; the commentary is garnish. Say the
            // solver's own call rather than lose the move.
            logger.warn(`Evolve app route ${match.spec.appId}: commentary call failed — ${err}`);
        }
        const reply = [String(resolved.reply_head ?? ""), "", callLine, String(resolved.reply_tail ?? "")]
            .join("\n").replace(/\n{3,}/g, "\n\n").trim();
        return done(reply, "solved");
    }
    return done(failReply("the saved game and the screenshot could not be reconciled"), "stale");
}
// ── OC runtime adapter ──────────────────────────────────────────────────────
/** Can this gateway run a route at all? Checked BEFORE a turn is claimed. */
export function runtimeSupportsRoutes(api) {
    return typeof api?.runtime?.mediaUnderstanding?.extractStructuredWithModel === "function"
        && typeof api?.runtime?.llm?.complete === "function"
        && typeof api?.runtime?.config?.current === "function";
}
function parseJsonLoose(text) {
    const t = String(text ?? "").trim().replace(/^```(?:json)?\s*/i, "").replace(/```\s*$/, "");
    return JSON.parse(t);
}
export function runScriptJson(workspaceDir, entryScript, args, stdin, timeoutMs = ROUTE_LIMITS.scriptTimeoutMs) {
    return new Promise((resolve, reject) => {
        const child = spawn("python3", [path.join(workspaceDir, entryScript), ...args], {
            cwd: workspaceDir,
            stdio: ["pipe", "pipe", "pipe"],
            timeout: timeoutMs,
        });
        let out = "";
        let err = "";
        child.stdout.on("data", (c) => { out += c.toString("utf8"); });
        child.stderr.on("data", (c) => { err += c.toString("utf8"); });
        child.on("error", reject);
        child.on("close", (code) => {
            try {
                resolve(JSON.parse(out));
            }
            catch {
                reject(new Error(`exit ${code}: ${(err || out).slice(0, 300)}`));
            }
        });
        child.stdin.end(stdin ?? "");
    });
}
/**
 * The OC runtime behind `RouteRuntime`. `provider`/`model` are the ones OC
 * resolved for this turn (the `before_agent_reply` hook context carries
 * `modelProviderId` / `modelId`) — the same model the session would have used.
 */
export function openClawRouteRuntime(api, opts) {
    return {
        async vision(req) {
            const res = await api.runtime.mediaUnderstanding.extractStructuredWithModel({
                input: [{ type: "image", buffer: req.image.buffer, fileName: path.basename(req.image.file), mime: req.image.mime }],
                instructions: req.instructions,
                schemaName: req.schemaName,
                jsonSchema: req.jsonSchema,
                jsonMode: true,
                cfg: api.runtime.config.current(),
                provider: opts.provider,
                model: opts.model,
                timeoutMs: ROUTE_LIMITS.visionTimeoutMs,
            });
            const parsed = res?.parsed !== undefined ? res.parsed : parseJsonLoose(String(res?.text ?? ""));
            return { parsed, text: String(res?.text ?? ""), provider: String(res?.provider ?? opts.provider), model: String(res?.model ?? opts.model) };
        },
        async complete(req) {
            const params = {
                messages: [{ role: "user", content: req.user }],
                systemPrompt: req.system,
                maxTokens: req.maxTokens,
                reasoning: "off",
                purpose: opts.purpose,
                signal: AbortSignal.timeout(ROUTE_LIMITS.commentaryTimeoutMs),
            };
            let res;
            try {
                res = await api.runtime.llm.complete(params);
            }
            catch (err) {
                // A model with no "off" thinking level is refused before any request
                // is sent; ask again at the model's default rather than lose the line.
                if (!/thinking level|reasoning/i.test(String(err?.message ?? err)))
                    throw err;
                delete params.reasoning;
                res = await api.runtime.llm.complete(params);
            }
            const u = res?.usage ?? {};
            return {
                text: String(res?.text ?? ""),
                provider: String(res?.provider ?? opts.provider),
                model: String(res?.model ?? opts.model),
                usage: {
                    inputTokens: u.inputTokens, outputTokens: u.outputTokens,
                    cacheReadTokens: u.cacheReadTokens, cacheWriteTokens: u.cacheWriteTokens,
                    costUsd: u.costUsd,
                },
            };
        },
        script(args, stdin) {
            return runScriptJson(opts.workspaceDir, opts.entryScript, args, stdin);
        },
    };
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
export class AppRouteHandler {
    api;
    logger;
    statusAllows;
    sink;
    routes;
    installCache = new Map();
    routedRuns = new Set();
    warned = new Set();
    lastWorkspaceDir = null;
    constructor(api, logger, statusAllows, sink, routes = APP_ROUTES) {
        this.api = api;
        this.logger = logger;
        this.statusAllows = statusAllows;
        this.sink = sink;
        this.routes = routes;
    }
    warnOnce(key, msg) {
        if (this.warned.has(key))
            return;
        this.warned.add(key);
        this.logger.warn(msg);
    }
    stateDir() {
        try {
            const d = this.api?.runtime?.state?.resolveStateDir?.();
            if (typeof d === "string" && d)
                return d;
        }
        catch { /* fall through */ }
        const env = process.env.OPENCLAW_STATE_DIR?.trim();
        if (env)
            return env;
        return path.join(process.env.HOME || "", ".openclaw");
    }
    workspaceDir(ctx) {
        const d = typeof ctx?.workspaceDir === "string" && ctx.workspaceDir.trim() ? ctx.workspaceDir : null;
        if (d)
            this.lastWorkspaceDir = d;
        return d ?? this.lastWorkspaceDir ?? path.join(this.stateDir(), "workspace");
    }
    installed(spec, workspaceDir) {
        const key = `${spec.appId}\0${workspaceDir}`;
        const hit = this.installCache.get(key);
        if (hit && Date.now() - hit.at < 10_000)
            return hit.ok;
        const res = checkRouteInstalled(spec, workspaceDir, this.statusAllows);
        this.installCache.set(key, { at: Date.now(), ok: res.ok, reason: res.ok ? undefined : res.reason });
        if (!res.ok && hit?.ok !== false) {
            this.logger.info(`Evolve app route ${spec.appId}: not routing — ${res.reason}; the bot's session handles the shortcut`);
        }
        return res.ok;
    }
    eligible(prompt, ctx) {
        const trigger = String(ctx?.trigger ?? "user").toLowerCase();
        if (trigger !== "user")
            return null;
        const match = matchAppRoute(prompt, this.routes);
        if (!match)
            return null;
        if (!runtimeSupportsRoutes(this.api)) {
            this.warnOnce("runtime", `Evolve app routes: this gateway lacks runtime.mediaUnderstanding / runtime.llm — ${match.spec.appId} stays in the bot's session`);
            return null;
        }
        return this.installed(match.spec, this.workspaceDir(ctx)) ? match : null;
    }
    claimsPrompt(prompt, ctx) {
        try {
            return this.eligible(prompt, ctx) !== null;
        }
        catch {
            return false;
        }
    }
    /** ``shortcutAttribution`` for a user-triggered turn (attribution only —
     *  never claims, never changes routing). Never throws. */
    shortcutAppId(prompt, ctx, hasImage = false) {
        try {
            const trigger = String(ctx?.trigger ?? "user").toLowerCase();
            if (trigger !== "user")
                return null;
            if (!matchShortcut(prompt, this.routes))
                return null; // cheap regex first
            return shortcutAttribution(prompt, this.workspaceDir(ctx), this.statusAllows, {
                hasImage, routes: this.routes,
            });
        }
        catch {
            return null;
        }
    }
    consumeRouted(runId) {
        return typeof runId === "string" && this.routedRuns.delete(runId);
    }
    async handle(ctx, event) {
        let match;
        try {
            match = this.eligible(String(event?.cleanedBody ?? ""), ctx);
        }
        catch (err) {
            this.logger.warn(`Evolve app routes: match error — ${err}`);
            return undefined;
        }
        if (!match)
            return undefined;
        const provider = typeof ctx?.modelProviderId === "string" ? ctx.modelProviderId : "";
        const model = typeof ctx?.modelId === "string" ? ctx.modelId : "";
        if (!provider || !model) {
            this.warnOnce("model", `Evolve app route ${match.spec.appId}: no resolved model on the hook context — the bot's session handles the shortcut`);
            return undefined;
        }
        const workspaceDir = this.workspaceDir(ctx);
        const runId = typeof ctx?.runId === "string" ? ctx.runId : null;
        if (runId) {
            if (this.routedRuns.size >= 256)
                this.routedRuns.clear();
            this.routedRuns.add(runId);
        }
        let result;
        const image = match.mediaRefs.map((r) => resolveInboundImage(r, this.stateDir())).find((i) => i) ?? null;
        if (!image) {
            result = { reply: failReply("the screenshot could not be opened"), outcome: "no_image", calls: [], latencyMs: 0, mode: null };
        }
        else {
            const runtime = openClawRouteRuntime(this.api, {
                workspaceDir, entryScript: match.spec.entryScript, provider, model,
                purpose: `${match.spec.appId} app route`,
            });
            result = await runAppRoute(match, image, runtime, this.logger);
        }
        this.logger.info(`Evolve app route ${match.spec.appId}: ${result.outcome} in ${result.latencyMs}ms, ` +
            `${result.calls.length} model call(s)${result.mode ? `, ${result.mode} reading` : ""}`);
        try {
            this.sink(result, match, ctx);
        }
        catch (err) {
            this.logger.warn(`Evolve app route ${match.spec.appId}: receipt write failed — ${err}`);
        }
        return { handled: true, reply: { text: result.reply }, reason: `evolve app route ${match.spec.appId}` };
    }
}
//# sourceMappingURL=AppRoutes.js.map