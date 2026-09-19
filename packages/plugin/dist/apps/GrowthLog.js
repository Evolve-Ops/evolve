/**
 * GrowthLog — the app growth-log OBSERVER (report-only).
 *
 * Brief: ``internal/dispatch/done/growth-log-observer.md``. That brief cites
 * ``internal/design-app-lineage-2026-08-24.md`` §3 + Q-L1/Q-L2 as the program
 * doc; **that file does not exist in this repo** — not on main, not anywhere in
 * history — so it is named here as the brief's citation and not as something
 * this module was written against. Same posture as ``app_snapshot.py``'s
 * missing ``design-app-suites`` citation. What the brief itself specifies IS
 * implemented, verbatim, and every place the missing design would have settled
 * a detail is called out below with the decision this module took instead.
 *
 * ── What it is ───────────────────────────────────────────────────────────────
 * Apps on a pod are fluid: a user reshapes one in conversation and the bot
 * edits its files in the same turn. Nothing recorded *why*. The growth log is
 * one append-only delta per turn per app: which files moved, and the
 * conversational request that caused it, captured at the moment it happened.
 *
 * **This starts the clock and nothing more.** No UI, no behavior change, no
 * consumer — history cannot be backfilled, so the recorder goes in the ground
 * first. Nothing in this repo reads what it writes.
 *
 * ── Where it observes ────────────────────────────────────────────────────────
 * ``agent_end``, off the same ``event.messages`` payload the struggle detector
 * and ``OutwardActionLedger`` already read — NOT ``before_tool_call``. Three
 * reasons, in order of weight:
 *   1. The turn's user message (the CAUSE) is in scope there and nowhere else.
 *   2. The write has already happened, so the file exists and its ``_evolve``
 *      marker is readable — the strongest per-file ownership evidence.
 *   3. It is off the tool-execution hot path entirely. An observer must never
 *      be able to slow or block a tool call.
 *
 * ── What it can and cannot see (the honest bound) ────────────────────────────
 * A file write is recognised by tool NAME (``isFileWriteTool``) plus a path
 * pulled out of the call's params. Over an uncontrolled upstream tool registry
 * that is fail-OPEN: a renamed editor, an MCP tool nobody enumerated, or —
 * most commonly — a ``bash``/``exec`` heredoc writes a file this module never
 * sees. **That miss is the entire reason the admin-side daily sweep exists**
 * (``packages/analyzer/app_growth_sweep.py``), which re-derives changes from
 * content digests and records them ``attribution: "sweep"`` — honestly
 * second-class, because a sweep record has no cause: by the time it runs, the
 * conversation that caused the change is over.
 *
 * Note the asymmetry with ``ToolCallGate``, which faces the same uncontrolled
 * registry and answers it with DEFAULT-DENY. That is right for an enforcer,
 * where a miss is a security hole. Here a miss is a missing observation with a
 * named backstop, and a default-record would fill the log with every Read the
 * bot ever did. Fail-open is the correct posture for this surface, and it is a
 * different surface.
 *
 * ── How a file is attributed to an app ───────────────────────────────────────
 *   ``"manifest"`` — the path is declared in an app manifest's
 *                    ``files[]``/``realized_files[]`` on THIS bot. Checked
 *                    first: it is a pure in-memory lookup against a cached
 *                    index, and it is the ownership every admin-side reader
 *                    already uses.
 *   ``"marker"``   — the file's own embedded ``_evolve`` marker (see
 *                    ``provenance.py``) names a pkg/spec id that resolves to
 *                    one of this bot's manifests. Costs a bounded read, so it
 *                    only runs when the path lookup missed — which is exactly
 *                    the case it is for: a file the app owns that its manifest
 *                    has not caught up to.
 *   ``"none"``     — neither. Recorded as ``kind: "unattributed_change"`` with
 *                    the turn's app context alongside it, never guessed into an
 *                    app. Q-L2 branch clustering is explicitly NOT this chip;
 *                    these records are the raw material it would need.
 *
 * An unattributed write is only recorded when the TURN carries app attribution
 * (``AppAttribution`` resolved scheduled/explicit/inferred). A file write in a
 * turn with no app context at all is ordinary bot work, not app growth, and
 * recording it would make this a generic file-write log.
 *
 * ── ``footprint[]``, and what happened to D-S3 ───────────────────────────────
 * The brief cites "that app's declared footprint surfaces, D-S3". There is no
 * ``footprint`` field on the App Spec: ``app_snapshot.py`` records in as many
 * words that "a footprint/shared_edits field is a §5-freeze decision", i.e.
 * still unmade. So ``footprint[]`` here carries the app's declared NON-FILE
 * surfaces that this delta actually touched — a changed file that is a
 * manifest ``crons[].script`` emits ``cron:<schedule>:<script>``; a
 * ``scheduled_actions[]`` script emits ``action:<id>``. Derived from the same
 * manifest index, empty for a plain file edit, and honest about being derived
 * rather than declared.
 *
 * ── On-disk layout, and why the two subtrees ─────────────────────────────────
 *   {sharedDir}/app-growth/               ← sticky 1777, the multi-writer root
 *                                            (the ``annotations``/``metrics``
 *                                            convention in deploy.py)
 *   {sharedDir}/app-growth/{botId}/{appId}/{YYYY-MM-DD}.jsonl   ← THIS module
 *   {sharedDir}/app-growth/_sweep/{botId}/{appId}/{...}.jsonl   ← the sweep
 *
 * The two writers are different UNIX users — this runs as the bot, the sweep
 * runs as ``evolve`` — and they must never need to create a directory inside,
 * or append to a file owned by, the other. A shared tree gets that wrong
 * silently: whichever user won the mkdir owns a 0755 directory the other can
 * never write into. So each writer owns a subtree outright and readers glob
 * both. ``_sweep`` cannot collide with a bot id (UNIX account names do not
 * start with ``_``). This is CLAUDE.md's "route on who would own the result"
 * applied before the fact instead of after.
 *
 * Retention follows ownership for the same reason: this module prunes its own
 * subtree (the sweep cannot delete bot-owned files), the sweep prunes the
 * sweep's.
 *
 * ── Bounds ───────────────────────────────────────────────────────────────────
 * One day-file per app per bot per UTC day (rotation). At most
 * ``MAX_FILES_PER_RECORD`` paths on a record, ``MAX_RECORDS_PER_DAY_FILE``
 * records per day-file, ``MAX_CAUSE_CHARS`` of cause text, and
 * ``GROWTH_LOG_RETENTION_DAYS`` days kept. Every one is enforced, not stated.
 *
 * ── Privacy ──────────────────────────────────────────────────────────────────
 * ``cause`` is the user's own request text. Per-bot DNT via network.json
 * ``bots[<botId>].growthLog: false`` (default on) suppresses the text — the
 * delta is still recorded with ``cause: null, cause_source: "dnt"``, because
 * the delta is a fact about the app, not about the user. Same shape as the
 * pushback-signal DNT.
 *
 * ── Failure contract ─────────────────────────────────────────────────────────
 * Best-effort throughout. Nothing here may throw into a turn, and no IO error
 * is worth a warning louder than debug (EACCES included: on a pod where
 * another gateway owns the tree, silence is correct).
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { appIdOf, isCanonicalAppId, NO_APP_ID } from "./appIdentity.js";
export const GROWTH_LOG_SCHEMA_VERSION = 1;
/** Root under sharedDir. Sticky 1777 — see the layout note above. */
export const GROWTH_LOG_ROOT = "app-growth";
/** Subtree the admin-side sweep owns. Never a bot id. */
export const GROWTH_LOG_SWEEP_SEGMENT = "_sweep";
/** App segment for records with no owning app. Never a canonical app id
 *  (``isCanonicalAppId`` rejects a leading underscore). */
export const UNATTRIBUTED_SEGMENT = "_unattributed";
/** Cause text is the user's request, kept verbatim up to this length. */
export const MAX_CAUSE_CHARS = 1000;
/** Paths per record. A turn touching more than this is a bulk operation
 *  whose per-file detail is not what makes the log legible. */
export const MAX_FILES_PER_RECORD = 40;
/** Hard stop per day-file, so a runaway loop cannot fill a pod's disk. */
export const MAX_RECORDS_PER_DAY_FILE = 2000;
/** Day-files older than this are pruned by their own writer. */
export const GROWTH_LOG_RETENTION_DAYS = 90;
/** Bytes read from a file when looking for its ``_evolve`` marker. Markers
 *  are emitted at the head of the file by every writer in provenance.py. */
export const MARKER_SCAN_BYTES = 8192;
const DNT_CACHE_TTL_MS = 60_000;
const MANIFEST_INDEX_TTL_MS = 30_000;
// ── Pure: file-write extraction from an agent_end messages payload ───────────
/** Params keys that carry a single destination path, in preference order. */
const PATH_PARAM_KEYS = [
    "file_path", "path", "filePath", "filename", "file",
    "target_file", "notebook_path", "abs_path",
];
/**
 * Tool-name substrings that mean "this call writes a file".
 *
 * Substring, not equality: the same tool ships as ``write``, ``file_write``,
 * ``Write``, ``str_replace_based_edit_tool`` and half a dozen MCP-namespaced
 * aliases across gateway versions. Deliberately fail-open — see the module
 * docstring's honest-bound note and the sweep that backstops it.
 */
const WRITE_TOOL_SUBSTRINGS = [
    "write", "edit", "patch", "str_replace", "create_file", "createfile",
    "insert", "append_file", "save_file", "notebook",
];
/** Read-ish names that would otherwise match a write substring. */
const NON_WRITE_TOOL_SUBSTRINGS = ["read", "search", "grep", "list", "glob"];
export function isFileWriteTool(name) {
    if (typeof name !== "string" || !name)
        return false;
    const n = name.toLowerCase();
    if (NON_WRITE_TOOL_SUBSTRINGS.some((s) => n.includes(s)))
        return false;
    return WRITE_TOOL_SUBSTRINGS.some((s) => n.includes(s));
}
/**
 * ``command`` values that mean this call READ the file, on the editor tools
 * that multiplex both verbs behind one write-shaped name
 * (``str_replace_based_edit_tool``, ``text_editor``). Without this a ``view``
 * would be recorded as a change the file never underwent.
 */
const READ_COMMANDS = new Set(["view", "read", "list", "show", "cat"]);
export function isReadCommandInput(input) {
    if (!input || typeof input !== "object")
        return false;
    const cmd = input.command;
    return typeof cmd === "string" && READ_COMMANDS.has(cmd.toLowerCase());
}
/** ``*** Add File: x`` / ``*** Update File: x`` / ``*** Delete File: x``. */
const PATCH_PATH_RE = /^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+)$/gm;
/**
 * Every destination path one tool call names. Shape-tolerant by contract:
 * an unexpected params object yields ``[]`` rather than throwing.
 */
export function pathsFromToolInput(input) {
    if (!input || typeof input !== "object")
        return [];
    const obj = input;
    const out = [];
    for (const key of PATH_PARAM_KEYS) {
        const v = obj[key];
        if (typeof v === "string" && v.trim()) {
            out.push(v.trim());
            break; // one destination per call; the keys are aliases, not a list
        }
    }
    // apply_patch-style envelopes carry their paths inside the patch body.
    // ``content`` is deliberately NOT scanned: on a plain write it holds the
    // file's own bytes, and a doc that quotes an apply_patch header would mint
    // paths the turn never touched.
    for (const key of ["patch", "input", "diff"]) {
        const body = obj[key];
        if (typeof body !== "string" || !body.includes("*** "))
            continue;
        PATCH_PATH_RE.lastIndex = 0;
        let m;
        while ((m = PATCH_PATH_RE.exec(body)) !== null) {
            const p = m[1].trim();
            if (p)
                out.push(p);
        }
    }
    return out;
}
/**
 * Extract every SUCCESSFUL file-write call from an ``agent_end`` messages
 * payload. Tolerates both the Anthropic ``content[].tool_use`` shape and the
 * OpenAI ``tool_calls[].function`` shape — the same dual-shape tolerance
 * ``OutwardActionLedger`` and ``StruggleDetector`` carry.
 *
 * A call whose matching ``tool_result`` is ``is_error`` is dropped: a write
 * that failed did not grow the app. A call with no matching result is KEPT —
 * absence of a result block is a payload-shape fact, not evidence of failure,
 * and the sweep would find the change anyway.
 */
export function extractFileWrites(messages) {
    if (!Array.isArray(messages))
        return [];
    const errorById = new Set();
    for (const msg of messages) {
        const content = msg?.content;
        if (!Array.isArray(content))
            continue;
        for (const block of content) {
            const b = block;
            if (!b || b.type !== "tool_result" || b.is_error !== true)
                continue;
            if (typeof b.tool_use_id === "string")
                errorById.add(b.tool_use_id);
        }
    }
    const writes = [];
    const push = (name, id, input) => {
        if (!isFileWriteTool(name))
            return;
        if (isReadCommandInput(input))
            return;
        if (typeof id === "string" && errorById.has(id))
            return;
        for (const p of pathsFromToolInput(input)) {
            writes.push({ tool: String(name), path: p });
        }
    };
    for (const msg of messages) {
        const m = msg;
        if (!m)
            continue;
        if (Array.isArray(m.content)) {
            for (const block of m.content) {
                const b = block;
                if (b && b.type === "tool_use")
                    push(b.name, b.id, b.input);
            }
        }
        if (Array.isArray(m.tool_calls)) {
            for (const tc of m.tool_calls) {
                const t = tc;
                const fn = t?.function;
                if (!fn)
                    continue;
                let args = fn.arguments;
                if (typeof args === "string") {
                    try {
                        args = JSON.parse(args);
                    }
                    catch {
                        args = null;
                    }
                }
                push(fn.name, t?.id, args);
            }
        }
    }
    return writes;
}
// ── Pure: path normalization + marker parsing ────────────────────────────────
/**
 * Normalize one path to a workspace-RELATIVE path, case preserved.
 *
 * Strips a ``layer: `` prefix the way ``manifest_recovery._norm_path`` does and
 * resolves an absolute path against the workspace root. Returns ``null`` for
 * anything outside the workspace — a write to ``/tmp`` or to the bot's
 * dotfiles is not app surface.
 *
 * Case is deliberately PRESERVED here and folded only by ``indexKey``. The
 * returned value is used to open the file to read its marker, and a lowercased
 * path opens nothing on a case-sensitive filesystem (every Linux pod).
 */
export function normalizeAppPath(raw, workspaceRoot) {
    if (typeof raw !== "string")
        return null;
    let s = raw.trim().replace(/^[a-z_]+:\s*/, "");
    if (!s)
        return null;
    if (s.startsWith("~/"))
        s = path.join(process.env.HOME ?? "", s.slice(2));
    if (path.isAbsolute(s)) {
        const rel = path.relative(workspaceRoot, path.normalize(s));
        if (!rel || rel.startsWith("..") || path.isAbsolute(rel))
            return null;
        s = rel;
    }
    s = path.normalize(s).replace(/^(\.\/)+/, "");
    if (s.startsWith(".."))
        return null;
    return s.replace(/^\/+|\/+$/g, "") || null;
}
/** The comparison key for a workspace-relative path: case-folded, slash-trimmed. */
export function indexKey(relPath) {
    return relPath.replace(/^\/+|\/+$/g, "").toLowerCase();
}
/**
 * Every alias a manifest-declared path should be indexed under. Manifests are
 * inconsistent about the leading ``workspace/`` segment (``RecordApplication``
 * accepts both forms in as many words), so index both and look up both.
 */
export function pathAliases(key) {
    const out = [key];
    if (key.startsWith("workspace/"))
        out.push(key.slice("workspace/".length));
    else
        out.push(`workspace/${key}`);
    return out;
}
/** ``evolve: pkg=<id>[@ver][,<id>…] file=<id>`` — mirrors provenance._MARKER_RE. */
const MARKER_RE = /evolve:\s+(?:pkg|spec)=([^\s]+)\s+file=([^\s]+)/;
/**
 * The pkg/spec ids embedded in a file's ``_evolve`` marker, or ``[]``.
 * Handles both the comment form and the JSON ``{"_evolve": {"pkg": …}}``
 * form, which is all ``provenance.py`` ever writes.
 */
export function parseEvolveMarkerIds(text) {
    if (typeof text !== "string" || !text)
        return [];
    let refs = "";
    const m = MARKER_RE.exec(text);
    if (m) {
        refs = m[1];
    }
    else {
        // JSON form: {"_evolve": {"pkg": "p-a3f91c8b@2026.04.15-1.3", ...}}
        const j = /"_evolve"\s*:\s*\{[^}]*?"(?:pkg|spec)"\s*:\s*"([^"]+)"/.exec(text);
        if (!j)
            return [];
        refs = j[1];
    }
    return refs
        .split(",")
        .map((r) => r.split("@")[0].trim())
        .filter((r) => r.length > 0);
}
function emptyIndex() {
    return { byPath: new Map(), footprintByPath: new Map(), byMarkerId: new Map() };
}
function manifestDeclaredPaths(m) {
    const out = [];
    for (const key of ["files", "realized_files", "evidence_files"]) {
        const entries = m[key];
        if (!Array.isArray(entries))
            continue;
        for (const e of entries) {
            if (typeof e === "string")
                out.push(e);
            else if (e && typeof e === "object") {
                const p = e.path;
                if (typeof p === "string")
                    out.push(p);
            }
        }
    }
    return out;
}
/**
 * Footprint tokens this manifest declares, keyed by the script path that
 * realizes each one. ``crons[]`` is normalized through the same
 * string-or-dict tolerance ``manifest.cron_dicts()`` applies on the Python
 * side; ``scheduled_actions[]`` is dict-only by schema.
 */
function manifestFootprint(m, workspaceRoot) {
    const out = new Map();
    const add = (rawPath, token) => {
        // Cron scripts are routinely stored absolute; route them through the
        // same normalizer the file index uses so both sides key alike.
        const rel = normalizeAppPath(rawPath, workspaceRoot || "/");
        if (!rel)
            return;
        const key = indexKey(rel);
        for (const alias of pathAliases(key)) {
            const list = out.get(alias) ?? [];
            if (!list.includes(token))
                list.push(token);
            out.set(alias, list);
        }
    };
    const crons = m.crons;
    if (Array.isArray(crons)) {
        for (const c of crons) {
            if (!c || typeof c !== "object")
                continue;
            const cd = c;
            const script = cd.script ?? cd.script_path;
            const schedule = typeof cd.schedule === "string" ? cd.schedule : "";
            add(script, `cron:${schedule}:${typeof script === "string" ? script : ""}`);
        }
    }
    const actions = m.scheduled_actions;
    if (Array.isArray(actions)) {
        for (const a of actions) {
            if (!a || typeof a !== "object")
                continue;
            const ad = a;
            const id = typeof ad.id === "string" && ad.id ? ad.id : "unnamed";
            add(ad.script ?? ad.script_path, `action:${id}`);
        }
    }
    return out;
}
/**
 * Build the path/marker → app-id index from one bot's manifests directory.
 * Exported for tests; the class caches it behind an mtime + TTL check.
 *
 * Never throws. A manifest that fails to parse is skipped — a hand-edited
 * manifest must not cost the whole index.
 */
export function buildOwnershipIndex(manifestsDir, workspaceRoot = "") {
    const index = emptyIndex();
    let names;
    try {
        names = fs.readdirSync(manifestsDir);
    }
    catch {
        return index;
    }
    for (const name of names) {
        if (!name.endsWith(".json") || name.startsWith(".") || name.startsWith("_"))
            continue;
        let m;
        try {
            m = JSON.parse(fs.readFileSync(path.join(manifestsDir, name), "utf8"));
        }
        catch {
            continue;
        }
        if (!m || typeof m !== "object")
            continue;
        const appId = appIdOf(m);
        if (!appId || appId === NO_APP_ID)
            continue;
        for (const raw of manifestDeclaredPaths(m)) {
            // Manifests normally store workspace-relative paths, but the scanner
            // has written absolute ones; passing the real workspace root lets those
            // resolve instead of indexing an unmatchable "users/<bot>/..." key.
            const rel = normalizeAppPath(raw, workspaceRoot || "/");
            if (!rel)
                continue;
            for (const alias of pathAliases(indexKey(rel))) {
                if (!index.byPath.has(alias))
                    index.byPath.set(alias, appId);
            }
        }
        for (const [p, tokens] of manifestFootprint(m, workspaceRoot)) {
            const existing = index.footprintByPath.get(p) ?? [];
            index.footprintByPath.set(p, [...new Set([...existing, ...tokens])]);
        }
        // identity: see apps/appIdentity.appIdOf — which is what resolved `appId`
        // above and is the ONLY place this module answers "which app is this?".
        // The literal field names below are the REVERSE index: a file's `_evolve`
        // marker on disk carries whatever id was stamped into it (see
        // provenance.py — the marker namespace), so to turn that stamped string
        // back into the canonical id we have to accept every field the chain could
        // have resolved FROM. Reading them as a resolution order would be the
        // 1.4b bug; reading them as marker-lookup keys is the point, and the
        // mapping's VALUE is always appIdOf's answer, never a field read.
        for (const field of ["app_id", "pkg_id", "id", "spec_id", "instance_id"]) {
            const v = m[field];
            if (typeof v === "string" && v.trim() && !index.byMarkerId.has(v.trim())) {
                index.byMarkerId.set(v.trim(), appId);
            }
        }
    }
    return index;
}
/**
 * Day-file directory for one (bot, app). ``appId`` null ⇒ the unattributed
 * bucket, as does a non-conforming legacy id (``isCanonicalAppId`` false) —
 * such an id is not safe as a path segment.
 *
 * **The record's ``app_id`` FIELD is authoritative; the directory is a
 * physical index** — the same contract the arbiter store states for a
 * proposal's status vs its subdir. A reader groups by the field.
 */
export function growthAppDir(sharedDir, botId, appId) {
    const seg = appId && isCanonicalAppId(appId) ? appId : UNATTRIBUTED_SEGMENT;
    return path.join(sharedDir, GROWTH_LOG_ROOT, botId, seg);
}
export class GrowthLog {
    config;
    logger;
    _index = null;
    _indexMtime = 0;
    _indexCheckedAt = 0;
    _dnt = null;
    _dntCheckedAt = 0;
    /** Per day-file record counts, so the cap costs no stat per write. */
    _counts = new Map();
    _initializedDirs = new Set();
    _prunedAt = 0;
    constructor(config, logger) {
        this.config = config;
        this.logger = logger;
    }
    /**
     * Record this turn's app deltas. Best-effort and never throws.
     *
     * Returns the records written — for tests and for the caller's debug
     * logging. Callers must not depend on the value in production paths.
     */
    recordTurn(input) {
        try {
            return this._recordTurn(input);
        }
        catch (err) {
            this.logger.debug(`Evolve growth log: record failed (continuing): ${err}`);
            return [];
        }
    }
    _recordTurn(input) {
        const writes = extractFileWrites(input.messages);
        if (writes.length === 0)
            return [];
        const index = this._ownershipIndex();
        const turnAppId = input.appAttribution?.app_id ?? null;
        const turnGrade = input.appAttribution?.app_attribution ?? "none";
        const buckets = new Map();
        for (const w of writes) {
            const rel = normalizeAppPath(w.path, this.config.workspaceRoot);
            if (!rel)
                continue; // outside the workspace — not app surface
            const key = indexKey(rel);
            let appId = null;
            let attribution = "none";
            for (const alias of pathAliases(key)) {
                const owner = index.byPath.get(alias);
                if (owner) {
                    appId = owner;
                    attribution = "manifest";
                    break;
                }
            }
            if (!appId) {
                const viaMarker = this._appIdFromMarker(rel, index);
                if (viaMarker) {
                    appId = viaMarker;
                    attribution = "marker";
                }
            }
            // An unowned write is app growth only when the TURN is app-attributed.
            if (!appId && turnGrade === "none")
                continue;
            const bucketKey = appId ?? UNATTRIBUTED_SEGMENT;
            const b = buckets.get(bucketKey) ?? {
                appId, attribution, files: [], footprint: [], tools: [],
            };
            if (!b.files.includes(rel) && b.files.length < MAX_FILES_PER_RECORD) {
                b.files.push(rel);
                for (const alias of pathAliases(key)) {
                    for (const token of index.footprintByPath.get(alias) ?? []) {
                        if (!b.footprint.includes(token))
                            b.footprint.push(token);
                    }
                }
            }
            if (!b.tools.includes(w.tool))
                b.tools.push(w.tool);
            // A manifest match anywhere in the bucket outranks a marker match.
            if (attribution === "manifest")
                b.attribution = "manifest";
            buckets.set(bucketKey, b);
        }
        if (buckets.size === 0)
            return [];
        const dnt = this._isDntEnabled();
        const rawCause = (input.userMessage ?? "").trim();
        const causeSource = dnt ? "dnt" : rawCause ? "user_request" : "none";
        const cause = causeSource === "user_request"
            ? rawCause.slice(0, MAX_CAUSE_CHARS)
            : null;
        const written = [];
        for (const b of buckets.values()) {
            const record = {
                schema_version: GROWTH_LOG_SCHEMA_VERSION,
                kind: b.appId ? "app_delta" : "unattributed_change",
                ts: input.ts,
                bot_id: this.config.botId,
                session_id: input.sessionId,
                turn_id: input.turnId,
                app_id: b.appId,
                files: b.files,
                footprint: b.footprint,
                cause,
                cause_source: causeSource,
                cause_truncated: causeSource === "user_request" && rawCause.length > MAX_CAUSE_CHARS,
                attribution: b.appId ? b.attribution : "none",
                turn_app_id: turnAppId,
                turn_app_attribution: turnGrade,
                tools: b.tools,
            };
            if (this._append(record))
                written.push(record);
        }
        this._pruneOwnSubtreeOnce();
        return written;
    }
    /** Read the file's own ``_evolve`` marker and resolve it against the index. */
    _appIdFromMarker(relPath, index) {
        if (index.byMarkerId.size === 0)
            return null;
        let head;
        try {
            const abs = path.join(this.config.workspaceRoot, relPath);
            const fd = fs.openSync(abs, "r");
            try {
                const buf = Buffer.alloc(MARKER_SCAN_BYTES);
                const n = fs.readSync(fd, buf, 0, MARKER_SCAN_BYTES, 0);
                head = buf.subarray(0, n).toString("utf8");
            }
            finally {
                fs.closeSync(fd);
            }
        }
        catch {
            // Deleted, binary-unreadable, or permission-denied — no marker evidence.
            return null;
        }
        for (const id of parseEvolveMarkerIds(head)) {
            const appId = index.byMarkerId.get(id);
            if (appId)
                return appId;
        }
        return null;
    }
    _ownershipIndex() {
        const now = Date.now();
        if (this._index !== null && now - this._indexCheckedAt < MANIFEST_INDEX_TTL_MS) {
            return this._index;
        }
        this._indexCheckedAt = now;
        let mtime = 0;
        try {
            mtime = fs.statSync(this.config.manifestsDir).mtimeMs;
        }
        catch {
            this._index = emptyIndex();
            this._indexMtime = 0;
            return this._index;
        }
        if (this._index !== null && mtime === this._indexMtime)
            return this._index;
        this._index = buildOwnershipIndex(this.config.manifestsDir, this.config.workspaceRoot);
        this._indexMtime = mtime;
        return this._index;
    }
    _isDntEnabled() {
        const now = Date.now();
        if (this._dnt !== null && now - this._dntCheckedAt < DNT_CACHE_TTL_MS)
            return this._dnt;
        let dnt = false;
        try {
            const network = JSON.parse(fs.readFileSync(path.join(this.config.sharedDir, "network.json"), "utf8"));
            if (network?.bots?.[this.config.botId]?.growthLog === false)
                dnt = true;
        }
        catch {
            // Unreadable network.json — fail-open to the default-on policy, exactly
            // as the pushback-signal DNT does.
        }
        this._dnt = dnt;
        this._dntCheckedAt = now;
        return dnt;
    }
    /** Append one record to its day-file. Returns false when nothing was written. */
    _append(record) {
        const dir = growthAppDir(this.config.sharedDir, this.config.botId, record.app_id);
        const day = record.ts.slice(0, 10);
        const file = path.join(dir, `${day}.jsonl`);
        const seen = this._counts.get(file);
        if (seen !== undefined && seen >= MAX_RECORDS_PER_DAY_FILE)
            return false;
        if (!this._initializedDirs.has(dir)) {
            if (!this._ensureDirs(dir))
                return false;
            this._initializedDirs.add(dir);
            if (seen === undefined)
                this._counts.set(file, this._countLines(file));
            if ((this._counts.get(file) ?? 0) >= MAX_RECORDS_PER_DAY_FILE)
                return false;
        }
        try {
            fs.appendFileSync(file, JSON.stringify(record) + "\n", { mode: 0o644 });
        }
        catch (err) {
            const code = err?.code;
            if (code === "ENOENT")
                this._initializedDirs.delete(dir);
            if (code !== "EACCES" && code !== "EPERM") {
                this.logger.debug(`Evolve growth log: append failed: ${err}`);
            }
            return false;
        }
        this._counts.set(file, (this._counts.get(file) ?? 0) + 1);
        return true;
    }
    /**
     * Create the day-file's directory chain, pinning the multi-writer root to
     * sticky 1777 when we are the one who created it. Without the explicit
     * chmod the root lands 0755 under the bot's umask and the ``evolve``-user
     * sweep can never create its own ``_sweep`` sibling — the exact
     * whoever-won-the-mkdir failure the layout note describes.
     */
    _ensureDirs(dir) {
        const root = path.join(this.config.sharedDir, GROWTH_LOG_ROOT);
        try {
            const existedBefore = fs.existsSync(root);
            fs.mkdirSync(dir, { recursive: true });
            if (!existedBefore) {
                try {
                    fs.chmodSync(root, 0o1777);
                }
                catch { /* not ours to widen */ }
            }
            return true;
        }
        catch (err) {
            const code = err?.code;
            if (code !== "EACCES" && code !== "EPERM") {
                this.logger.debug(`Evolve growth log: mkdir failed: ${err}`);
            }
            return false;
        }
    }
    _countLines(file) {
        try {
            const text = fs.readFileSync(file, "utf8");
            if (!text)
                return 0;
            return text.split("\n").filter((l) => l.length > 0).length;
        }
        catch {
            return 0;
        }
    }
    /**
     * Prune this bot's own day-files past the retention horizon. Once per
     * process and then at most daily — the sweep cannot do it for us (it runs
     * as ``evolve`` and these files are bot-owned), and a per-write scan would
     * cost a full tree walk on every turn.
     */
    _pruneOwnSubtreeOnce() {
        const now = Date.now();
        if (this._prunedAt && now - this._prunedAt < 24 * 60 * 60_000)
            return;
        this._prunedAt = now;
        const cutoff = new Date(now - GROWTH_LOG_RETENTION_DAYS * 24 * 60 * 60_000)
            .toISOString().slice(0, 10);
        const botRoot = path.join(this.config.sharedDir, GROWTH_LOG_ROOT, this.config.botId);
        try {
            for (const appSeg of fs.readdirSync(botRoot)) {
                const appDir = path.join(botRoot, appSeg);
                let entries;
                try {
                    entries = fs.readdirSync(appDir);
                }
                catch {
                    continue;
                }
                for (const name of entries) {
                    if (!name.endsWith(".jsonl"))
                        continue;
                    if (name.slice(0, 10) >= cutoff)
                        continue;
                    try {
                        fs.unlinkSync(path.join(appDir, name));
                        this._counts.delete(path.join(appDir, name));
                    }
                    catch { /* another writer's file, or already gone */ }
                }
            }
        }
        catch {
            // No tree yet, or not readable. Nothing to prune.
        }
    }
}
//# sourceMappingURL=GrowthLog.js.map