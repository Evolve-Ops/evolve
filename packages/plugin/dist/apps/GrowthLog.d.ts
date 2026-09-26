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
import type { AppAttributionResult } from "./AppAttribution.js";
export declare const GROWTH_LOG_SCHEMA_VERSION = 1;
/** Root under sharedDir. Sticky 1777 — see the layout note above. */
export declare const GROWTH_LOG_ROOT = "app-growth";
/** Subtree the admin-side sweep owns. Never a bot id. */
export declare const GROWTH_LOG_SWEEP_SEGMENT = "_sweep";
/** App segment for records with no owning app. Never a canonical app id
 *  (``isCanonicalAppId`` rejects a leading underscore). */
export declare const UNATTRIBUTED_SEGMENT = "_unattributed";
/** Cause text is the user's request, kept verbatim up to this length. */
export declare const MAX_CAUSE_CHARS = 1000;
/** Paths per record. A turn touching more than this is a bulk operation
 *  whose per-file detail is not what makes the log legible. */
export declare const MAX_FILES_PER_RECORD = 40;
/** Hard stop per day-file, so a runaway loop cannot fill a pod's disk. */
export declare const MAX_RECORDS_PER_DAY_FILE = 2000;
/** Day-files older than this are pruned by their own writer. */
export declare const GROWTH_LOG_RETENTION_DAYS = 90;
/** Bytes read from a file when looking for its ``_evolve`` marker. Markers
 *  are emitted at the head of the file by every writer in provenance.py. */
export declare const MARKER_SCAN_BYTES = 8192;
export type GrowthRecordKind = "app_delta" | "unattributed_change";
export type GrowthAttribution = "manifest" | "marker" | "sweep" | "none";
export type GrowthCauseSource = "user_request" | "dnt" | "none";
export interface GrowthRecord {
    schema_version: typeof GROWTH_LOG_SCHEMA_VERSION;
    kind: GrowthRecordKind;
    ts: string;
    bot_id: string;
    session_id: string | null;
    turn_id: string | null;
    app_id: string | null;
    files: string[];
    footprint: string[];
    cause: string | null;
    cause_source: GrowthCauseSource;
    cause_truncated: boolean;
    attribution: GrowthAttribution;
    /** The turn's own app context, recorded even on an ``app_delta`` so a later
     *  reader can see when file ownership and turn attribution disagreed. */
    turn_app_id: string | null;
    turn_app_attribution: string;
    /** Names of the write tools that produced this delta. Names only. */
    tools: string[];
}
export declare function isFileWriteTool(name: unknown): boolean;
export declare function isReadCommandInput(input: unknown): boolean;
/**
 * Every destination path one tool call names. Shape-tolerant by contract:
 * an unexpected params object yields ``[]`` rather than throwing.
 */
export declare function pathsFromToolInput(input: unknown): string[];
export interface ObservedWrite {
    tool: string;
    path: string;
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
export declare function extractFileWrites(messages: unknown): ObservedWrite[];
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
export declare function normalizeAppPath(raw: unknown, workspaceRoot: string): string | null;
/** The comparison key for a workspace-relative path: case-folded, slash-trimmed. */
export declare function indexKey(relPath: string): string;
/**
 * Every alias a manifest-declared path should be indexed under. Manifests are
 * inconsistent about the leading ``workspace/`` segment (``RecordApplication``
 * accepts both forms in as many words), so index both and look up both.
 */
export declare function pathAliases(key: string): string[];
/**
 * The pkg/spec ids embedded in a file's ``_evolve`` marker, or ``[]``.
 * Handles both the comment form and the JSON ``{"_evolve": {"pkg": …}}``
 * form, which is all ``provenance.py`` ever writes.
 */
export declare function parseEvolveMarkerIds(text: unknown): string[];
export interface OwnershipIndex {
    /** normalized path (and its aliases) → owning app id. */
    byPath: Map<string, string>;
    /** normalized path → the footprint tokens that path realizes. */
    footprintByPath: Map<string, string[]>;
    /** any legacy id a marker may carry (pkg_id/spec_id/id/instance_id) → app id. */
    byMarkerId: Map<string, string>;
}
/**
 * Build the path/marker → app-id index from one bot's manifests directory.
 * Exported for tests; the class caches it behind an mtime + TTL check.
 *
 * Never throws. A manifest that fails to parse is skipped — a hand-edited
 * manifest must not cost the whole index.
 */
export declare function buildOwnershipIndex(manifestsDir: string, workspaceRoot?: string): OwnershipIndex;
export interface GrowthLogConfig {
    sharedDir: string;
    botId: string;
    /** The bot's OpenClaw workspace root. Absolute tool paths are made
     *  relative to it; anything outside is not app surface. */
    workspaceRoot: string;
    /** Manifests directory for this bot — the ownership index's source. */
    manifestsDir: string;
}
interface GrowthLogger {
    debug(msg: string): void;
    warn(msg: string): void;
}
export interface RecordTurnInput {
    messages: unknown;
    sessionId: string | null;
    turnId: string | null;
    ts: string;
    userMessage: string;
    appAttribution: AppAttributionResult | null;
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
export declare function growthAppDir(sharedDir: string, botId: string, appId: string | null): string;
export declare class GrowthLog {
    private readonly config;
    private readonly logger;
    private _index;
    private _indexMtime;
    private _indexCheckedAt;
    private _dnt;
    private _dntCheckedAt;
    /** Per day-file record counts, so the cap costs no stat per write. */
    private readonly _counts;
    private readonly _initializedDirs;
    private _prunedAt;
    constructor(config: GrowthLogConfig, logger: GrowthLogger);
    /**
     * Record this turn's app deltas. Best-effort and never throws.
     *
     * Returns the records written — for tests and for the caller's debug
     * logging. Callers must not depend on the value in production paths.
     */
    recordTurn(input: RecordTurnInput): GrowthRecord[];
    private _recordTurn;
    /** Read the file's own ``_evolve`` marker and resolve it against the index. */
    private _appIdFromMarker;
    private _ownershipIndex;
    private _isDntEnabled;
    /** Append one record to its day-file. Returns false when nothing was written. */
    private _append;
    /**
     * Create the day-file's directory chain, pinning the multi-writer root to
     * sticky 1777 when we are the one who created it. Without the explicit
     * chmod the root lands 0755 under the bot's umask and the ``evolve``-user
     * sweep can never create its own ``_sweep`` sibling — the exact
     * whoever-won-the-mkdir failure the layout note describes.
     */
    private _ensureDirs;
    private _countLines;
    /**
     * Prune this bot's own day-files past the retention horizon. Once per
     * process and then at most daily — the sweep cannot do it for us (it runs
     * as ``evolve`` and these files are bot-owned), and a per-write scan would
     * cost a full tree walk on every turn.
     */
    private _pruneOwnSubtreeOnce;
}
export {};
//# sourceMappingURL=GrowthLog.d.ts.map