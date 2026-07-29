/**
 * appScriptRegistry — recognizes when a shell tool call ran a KNOWN app's
 * script, and exposes which apps the integrity harness covers.
 *
 * The known-script set is built from this bot's installed manifests (the same
 * ``<bot home>/.openclaw/workspace/manifests/*.json`` source TurnObserver's
 * Layer C reads), drawing script paths from:
 *   - ``realized_files[].path``       — the forge-landed files (absolute paths),
 *                                       filtered to executable extensions;
 *   - ``event_triggers[].invocation.script`` — resolved against the workspace;
 *   - ``interface_contract.cli[].command``    — path-shaped tokens;
 *   - ``blueprint.files[].expected_location`` — gallery-side declared scripts.
 *
 * Recognition is bounded so a non-app command is never mis-handled:
 *   - a command token matches a known script only at a PATH boundary (exact
 *     absolute path, or a "/<workspace-relative>" suffix that itself contains a
 *     directory separator) — never a bare basename;
 *   - the matched token must be in EXECUTABLE POSITION — the first token, or
 *     preceded by an interpreter (python/node/bash/…) or a shell operator
 *     (``&&`` ``||`` ``;`` ``|`` ``(``). An app path that merely appears as a
 *     non-executable argument (``echo /…/foo.py``, ``cat /…/foo.py``) does NOT
 *     match.
 *
 * Even a recognition false-positive can only ever MISLABEL an already-failing
 * command as an app failure — it can never turn a failure into a success — so
 * the auditor invariant (never fabricate success) holds regardless of how loose
 * recognition is. The bounds above keep mislabels vanishingly rare.
 *
 * The pure core (``collectScriptPaths``, ``lookupCommand``, ``tokenize``,
 * ``isExecutablePosition``) is filesystem-free and exported for exhaustive
 * testing; the ``AppScriptRegistry`` class is a thin caching + coverage-write
 * wrapper around it. Caching mirrors TurnObserver._getManifestTriggers:
 * mtime-gated, throttled to one rescan per 5s, never throws (any failure ⇒
 * empty set, fail-open to passthrough).
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
/** Extensions we treat as runnable scripts (data files are excluded). */
const SCRIPT_EXTS = new Set([
    ".py",
    ".sh",
    ".bash",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".rb",
    ".pl",
]);
/** Interpreters whose NEXT token is the script being run. */
const INTERPRETERS = new Set([
    "python",
    "python3",
    "python2",
    "node",
    "nodejs",
    "deno",
    "bun",
    "ts-node",
    "tsx",
    "bash",
    "sh",
    "zsh",
    "ruby",
    "perl",
    "uv",
    "poetry",
    "pipenv",
]);
/** Shell operators after which the next token is a fresh command. */
const SHELL_OPS = new Set(["&&", "||", ";", "|", "(", "{", "&", "!"]);
const RESCAN_THROTTLE_MS = 5_000;
/**
 * Mode for the coverage file. The consumer is the ADMIN SERVER running as the
 * `evolve` user, not this bot user — a 0600 file is invisible to it, which
 * keeps the "May misreport failures" badge on fleet-wide even though the
 * harness is live (the exact bug this constant fixes). Contents are app ids +
 * script paths — non-secret — so world-readable is safe and simplest across
 * both pod shapes (macOS ACLs, Linux ACL-mask semantics).
 */
export const COVERAGE_FILE_MODE = 0o644;
/**
 * Default workspace root for the bot this plugin runs as. The plugin runs
 * INSIDE the gateway as the bot user, so the bot home is the PROCESS home —
 * ``os.homedir()`` ($HOME when set, else the passwd entry). The gateway JobSpec
 * sets HOME to the bot account's home in both launch paths (launchd plist
 * ``EnvironmentVariables``, systemd ``Environment=``); the passwd fallback
 * covers a legacy unit that omits it. Either way the resolved home is the
 * PROCESS user's — the gateway never runs as root — so it can never resolve to
 * root's or another user's home. Never a "/Users" literal: bot homes live
 * under /Users on macOS but /home on the Linux pod, and a hardcoded macOS
 * shape makes this registry scan nothing on Linux — empty covered_apps, so the
 * admin's "May misreport failures" badge never clears there.
 *
 * ``botId`` is only the last-resort fallback (no $HOME AND no passwd entry),
 * where the platform-keyed conventional home is the best remaining guess. That
 * branch is unreached under a correctly-launched gateway; ``logger`` (optional)
 * lets the caller surface it so a real env misconfiguration is observable
 * rather than silently guessed.
 */
export function defaultWorkspaceRoot(botId, logger) {
    let home = "";
    try {
        home = os.homedir() || "";
    }
    catch {
        home = "";
    }
    if (!home.trim()) {
        home = (process.platform === "darwin" ? "/Users/" : "/home/") + botId;
        logger?.debug(`Evolve app-integrity: no resolvable process home (HOME unset/empty and ` +
            `no passwd entry) — falling back to conventional home ${home} for ${botId}`);
    }
    return path.join(home, ".openclaw", "workspace");
}
// ── Pure core (filesystem-free, exported for tests) ──────────────────────────
/** app id from a manifest, best-effort. */
export function appIdOf(manifest) {
    const m = manifest;
    if (!m || typeof m !== "object")
        return "unknown";
    for (const k of ["pkg_id", "id", "spec_id", "instance_id"]) {
        const v = m[k];
        if (typeof v === "string" && v.trim())
            return v;
    }
    return "unknown";
}
/** Resolve a manifest-declared path to absolute (workspace-relative ⇒ abs). */
export function toAbs(p, workspaceRoot) {
    if (typeof p !== "string" || !p.trim())
        return null;
    const t = p.trim();
    if (path.isAbsolute(t))
        return path.normalize(t);
    // Strip a leading "./" and "workspace/" so both "workspace/scripts/x.py" and
    // "scripts/x.py" land at the same absolute path.
    const cleaned = t.replace(/^\.?\/*/, "").replace(/^workspace\//, "");
    return path.normalize(path.join(workspaceRoot, cleaned));
}
/** Path relative to the bot workspace (forward-slashed); "" when outside it. */
export function toWorkspaceRel(abs, workspaceRoot) {
    const rel = path.relative(workspaceRoot, abs);
    if (!rel || rel.startsWith("..") || path.isAbsolute(rel))
        return "";
    const fwd = rel.split(path.sep).join("/");
    return fwd.includes("/") ? fwd : ""; // require a dir separator (no bare basenames)
}
/** Resolve every script-shaped path declared by one manifest (absolute). */
export function collectScriptPaths(manifest, workspaceRoot) {
    const out = [];
    const m = manifest;
    if (!m || typeof m !== "object")
        return out;
    const add = (p) => {
        if (typeof p !== "string" || !p.trim())
            return;
        const abs = toAbs(p.trim(), workspaceRoot);
        if (!abs)
            return;
        if (!SCRIPT_EXTS.has(path.extname(abs).toLowerCase()))
            return;
        if (!out.includes(abs))
            out.push(abs);
    };
    // realized_files[].path
    const realized = m["realized_files"];
    if (Array.isArray(realized)) {
        for (const rf of realized) {
            if (rf && typeof rf === "object")
                add(rf["path"]);
        }
    }
    // event_triggers[].invocation.script
    const triggers = m["event_triggers"];
    if (Array.isArray(triggers)) {
        for (const t of triggers) {
            const inv = t && typeof t === "object" ? t["invocation"] : null;
            if (inv && typeof inv === "object")
                add(inv["script"]);
        }
    }
    // blueprint.files[].expected_location | .path
    const blueprint = m["blueprint"];
    const bfiles = blueprint && typeof blueprint === "object"
        ? blueprint["files"]
        : null;
    if (Array.isArray(bfiles)) {
        for (const f of bfiles) {
            if (f && typeof f === "object") {
                const fr = f;
                add(fr["expected_location"]);
                add(fr["path"]);
            }
        }
    }
    // interface_contract.cli[].command — extract path-shaped tokens.
    const ic = m["interface_contract"];
    const cli = ic && typeof ic === "object" ? ic["cli"] : null;
    if (Array.isArray(cli)) {
        for (const c of cli) {
            const cmd = c && typeof c === "object" ? c["command"] : null;
            if (typeof cmd === "string") {
                for (const tok of tokenize(cmd)) {
                    const t = stripQuotes(tok);
                    if (t.includes("/") && SCRIPT_EXTS.has(path.extname(t).toLowerCase()))
                        add(t);
                }
            }
        }
    }
    return out;
}
/** Build the flat known-script list for one manifest. */
export function knownScriptsForManifest(manifest, workspaceRoot) {
    const appId = appIdOf(manifest);
    return collectScriptPaths(manifest, workspaceRoot).map((absPath) => ({
        appId,
        absPath,
        relPath: toWorkspaceRel(absPath, workspaceRoot),
    }));
}
/** Group a known-script list into per-app coverage. */
export function coverageOf(scripts) {
    const map = new Map();
    for (const ks of scripts) {
        let set = map.get(ks.appId);
        if (!set) {
            set = new Set();
            map.set(ks.appId, set);
        }
        set.add(ks.absPath);
    }
    return [...map.entries()]
        .map(([appId, set]) => ({ appId, scripts: [...set].sort() }))
        .sort((a, b) => a.appId.localeCompare(b.appId));
}
/**
 * Look up which app (if any) a shell command ran against a known-script list.
 * Pure; never throws. First match in EXECUTABLE position wins.
 */
export function lookupCommand(command, scripts) {
    if (!command || typeof command !== "string" || scripts.length === 0)
        return null;
    const tokens = tokenize(command);
    for (let i = 0; i < tokens.length; i++) {
        if (!isExecutablePosition(tokens, i))
            continue;
        const tok = stripQuotes(tokens[i] ?? "");
        if (!tok)
            continue;
        const match = matchToken(tok, scripts);
        if (match)
            return { appId: match.appId, script: match.absPath || match.relPath };
    }
    return null;
}
function matchToken(tok, scripts) {
    // Exact absolute-path match first.
    for (const ks of scripts) {
        if (ks.absPath === tok)
            return ks;
    }
    // Workspace-relative: token equals rel, or token ends with "/<rel>".
    for (const ks of scripts) {
        if (!ks.relPath)
            continue;
        if (tok === ks.relPath || tok.endsWith("/" + ks.relPath))
            return ks;
    }
    return null;
}
// ── Command tokenization (best-effort, pure) ─────────────────────────────────
/**
 * Split a shell command into tokens, keeping shell operators as their own
 * tokens and respecting single/double quotes (best-effort — not a full shell
 * parser, which is unnecessary: we only need path tokens at boundaries).
 */
export function tokenize(command) {
    const tokens = [];
    let cur = "";
    let quote = null;
    const push = () => {
        if (cur) {
            tokens.push(cur);
            cur = "";
        }
    };
    for (let i = 0; i < command.length; i++) {
        const ch = command[i] ?? "";
        if (quote) {
            if (ch === quote)
                quote = null;
            else
                cur += ch;
            continue;
        }
        if (ch === '"' || ch === "'") {
            quote = ch;
            continue;
        }
        if (ch === " " || ch === "\t" || ch === "\n" || ch === "\r") {
            push();
            continue;
        }
        // Operators: treat &&, ||, ;, |, (, ), {, } as standalone tokens.
        if (ch === "&" ||
            ch === "|" ||
            ch === ";" ||
            ch === "(" ||
            ch === ")" ||
            ch === "{" ||
            ch === "}") {
            push();
            const next = command[i + 1];
            if ((ch === "&" && next === "&") || (ch === "|" && next === "|")) {
                tokens.push(ch + ch);
                i++;
            }
            else {
                tokens.push(ch);
            }
            continue;
        }
        cur += ch;
    }
    push();
    return tokens;
}
export function stripQuotes(tok) {
    if (tok.length >= 2) {
        const a = tok[0];
        const b = tok[tok.length - 1];
        if ((a === '"' && b === '"') || (a === "'" && b === "'"))
            return tok.slice(1, -1);
    }
    return tok;
}
/**
 * Is token ``i`` in command position (the program being executed), as opposed
 * to a mere argument? True when it's the first token, or the previous
 * non-empty token is an interpreter or a shell operator. Env-assignment
 * prefixes (``FOO=bar python x.py``) and ``sudo`` / ``env`` / ``time`` /
 * ``exec`` / ``command`` wrappers are skipped over.
 */
export function isExecutablePosition(tokens, i) {
    if (i === 0)
        return true;
    let j = i - 1;
    while (j >= 0) {
        const prev = tokens[j] ?? "";
        if (prev === "") {
            j--;
            continue;
        }
        if (SHELL_OPS.has(prev))
            return true;
        if (INTERPRETERS.has(prev))
            return true;
        // Transparent wrappers + interpreter flags: keep scanning left.
        if (prev === "sudo" ||
            prev === "env" ||
            prev === "time" ||
            prev === "exec" ||
            prev === "command" ||
            prev === "nice" ||
            prev === "nohup" ||
            prev === "-u" ||
            prev === "run" ||
            /^[A-Za-z_][A-Za-z0-9_]*=/.test(prev) // FOO=bar env assignment
        ) {
            j--;
            continue;
        }
        return false;
    }
    return true;
}
// ── Caching wrapper ──────────────────────────────────────────────────────────
export class AppScriptRegistry {
    botId;
    sharedDir;
    logger;
    workspaceRoot;
    manifestsDir;
    scripts = [];
    coverage = [];
    loaded = false;
    lastScanAt = 0;
    lastDirMtime = -1;
    /** Hash of the covered set last written to the coverage file. */
    lastCoverageKey = "";
    constructor(opts) {
        this.botId = opts.botId;
        this.sharedDir = opts.sharedDir;
        this.logger = opts.logger;
        // Trim-guard the seam: an empty/whitespace override would otherwise become
        // a cwd-relative "manifests" dir. Only a non-blank override wins.
        this.workspaceRoot = opts.workspaceRoot?.trim()
            ? opts.workspaceRoot
            : defaultWorkspaceRoot(this.botId, this.logger);
        this.manifestsDir = path.join(this.workspaceRoot, "manifests");
    }
    /** Look up which app (if any) a shell command ran. Total; never throws. */
    lookup(command) {
        this.refresh();
        return lookupCommand(command, this.scripts);
    }
    /** The apps this harness currently covers (for the badge/validator layer). */
    coveredApps() {
        this.refresh();
        return this.coverage;
    }
    /** (Re)build the known-script set when stale. Never throws. */
    refresh() {
        const now = Date.now();
        if (this.loaded && now - this.lastScanAt < RESCAN_THROTTLE_MS)
            return;
        let dirMtime = -1;
        try {
            dirMtime = fs.statSync(this.manifestsDir).mtimeMs;
        }
        catch {
            this.setEmpty(now);
            return;
        }
        if (this.loaded && dirMtime === this.lastDirMtime) {
            this.lastScanAt = now;
            return;
        }
        let files;
        try {
            files = fs.readdirSync(this.manifestsDir);
        }
        catch {
            this.setEmpty(now);
            this.lastDirMtime = dirMtime;
            return;
        }
        const scripts = [];
        const seen = new Set();
        for (const fname of files) {
            if (!fname.endsWith(".json"))
                continue;
            if (fname.startsWith(".") || fname.startsWith("_"))
                continue;
            let manifest;
            try {
                manifest = JSON.parse(fs.readFileSync(path.join(this.manifestsDir, fname), "utf8"));
            }
            catch (err) {
                this.logger.warn(`Evolve app-integrity: manifest ${fname} unreadable — ${err}`);
                continue;
            }
            for (const ks of knownScriptsForManifest(manifest, this.workspaceRoot)) {
                const key = ks.appId + " " + ks.absPath;
                if (seen.has(key))
                    continue;
                seen.add(key);
                scripts.push(ks);
            }
        }
        this.scripts = scripts;
        this.coverage = coverageOf(scripts);
        this.loaded = true;
        this.lastScanAt = now;
        this.lastDirMtime = dirMtime;
        this.writeCoverage();
    }
    setEmpty(now) {
        this.scripts = [];
        this.coverage = [];
        this.loaded = true;
        this.lastScanAt = now;
        this.writeCoverage();
    }
    /**
     * Best-effort write of the coverage signal the badge/validator layer (Bite
     * 2a) consumes. Only rewrites when the covered set changed. Never throws —
     * coverage is a convenience signal, not on the integrity-critical path.
     */
    writeCoverage() {
        try {
            const key = JSON.stringify(this.coverage);
            if (key === this.lastCoverageKey)
                return;
            const dir = path.join(this.sharedDir, this.botId);
            const dest = path.join(dir, "app-integrity-coverage.json");
            const payload = {
                version: 1,
                bot_id: this.botId,
                generated_by: "evolve-plugin/app-integrity-middleware",
                covered_apps: this.coverage,
            };
            const tmp = dest + ".tmp";
            // Normally {shared_dir}/{bot} pre-exists 0755 from deploy_bot(); if the
            // plugin ever mints it itself, the same umask 077 that once made the
            // FILE 0600 would make the DIR 0700 — untraversable by the evolve-user
            // admin reader, defeating the 0644 file inside. Assert 0755 on the leaf
            // dir only when we created it (never touch deploy-managed perms).
            const dirExisted = fs.existsSync(dir);
            fs.mkdirSync(dir, { recursive: true });
            if (!dirExisted)
                fs.chmodSync(dir, 0o755);
            fs.writeFileSync(tmp, JSON.stringify(payload, null, 2), {
                encoding: "utf8",
                mode: COVERAGE_FILE_MODE,
            });
            // The mode option is umask-masked at create and ignored entirely when a
            // stale tmp already exists, so assert the mode explicitly on every write
            // — including rewrite-on-change. Single-file chmod only (never the tree:
            // on Linux a recursive chmod would recalculate ACL masks on unrelated
            // entries). The subsequent rename replaces the dest inode, so a legacy
            // 0600 dest inherits nothing.
            fs.chmodSync(tmp, COVERAGE_FILE_MODE);
            fs.renameSync(tmp, dest);
            this.lastCoverageKey = key;
        }
        catch (err) {
            this.logger.debug(`Evolve app-integrity: coverage write skipped — ${err}`);
        }
    }
}
//# sourceMappingURL=appScriptRegistry.js.map