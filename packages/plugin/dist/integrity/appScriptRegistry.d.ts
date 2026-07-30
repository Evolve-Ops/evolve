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
export interface KnownScript {
    appId: string;
    /** Absolute path as resolved from the manifest. */
    absPath: string;
    /** Workspace-relative path (always contains a "/"); "" when not derivable. */
    relPath: string;
}
export interface AppCoverage {
    appId: string;
    scripts: string[];
}
interface RegistryLogger {
    info(msg: string): void;
    warn(msg: string): void;
    error(msg: string): void;
    debug(msg: string): void;
}
/**
 * Mode for the coverage file. The consumer is the ADMIN SERVER running as the
 * `evolve` user, not this bot user — a 0600 file is invisible to it, which
 * keeps the "May misreport failures" badge on fleet-wide even though the
 * harness is live (the exact bug this constant fixes). Contents are app ids +
 * script paths — non-secret — so world-readable is safe and simplest across
 * both pod shapes (macOS ACLs, Linux ACL-mask semantics).
 */
export declare const COVERAGE_FILE_MODE = 420;
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
export declare function defaultWorkspaceRoot(botId: string, logger?: RegistryLogger): string;
/** app id from a manifest, best-effort. */
export declare function appIdOf(manifest: unknown): string;
/** Resolve a manifest-declared path to absolute (workspace-relative ⇒ abs). */
export declare function toAbs(p: string, workspaceRoot: string): string | null;
/** Path relative to the bot workspace (forward-slashed); "" when outside it. */
export declare function toWorkspaceRel(abs: string, workspaceRoot: string): string;
/** Resolve every script-shaped path declared by one manifest (absolute). */
export declare function collectScriptPaths(manifest: unknown, workspaceRoot: string): string[];
/** Build the flat known-script list for one manifest. */
export declare function knownScriptsForManifest(manifest: unknown, workspaceRoot: string): KnownScript[];
/** Group a known-script list into per-app coverage. */
export declare function coverageOf(scripts: KnownScript[]): AppCoverage[];
/**
 * Look up which app (if any) a shell command ran against a known-script list.
 * Pure; never throws. First match in EXECUTABLE position wins.
 */
export declare function lookupCommand(command: string, scripts: KnownScript[]): {
    appId: string;
    script: string;
} | null;
/**
 * Split a shell command into tokens, keeping shell operators as their own
 * tokens and respecting single/double quotes (best-effort — not a full shell
 * parser, which is unnecessary: we only need path tokens at boundaries).
 */
export declare function tokenize(command: string): string[];
export declare function stripQuotes(tok: string): string;
/**
 * Is token ``i`` in command position (the program being executed), as opposed
 * to a mere argument? True when it's the first token, or the previous
 * non-empty token is an interpreter or a shell operator. Env-assignment
 * prefixes (``FOO=bar python x.py``) and ``sudo`` / ``env`` / ``time`` /
 * ``exec`` / ``command`` wrappers are skipped over.
 */
export declare function isExecutablePosition(tokens: string[], i: number): boolean;
export declare class AppScriptRegistry {
    private readonly botId;
    private readonly sharedDir;
    private readonly logger;
    private readonly workspaceRoot;
    private readonly manifestsDir;
    private scripts;
    private coverage;
    private loaded;
    private lastScanAt;
    private lastDirMtime;
    /** Hash of the covered set last written to the coverage file. */
    private lastCoverageKey;
    constructor(opts: {
        botId: string;
        sharedDir: string;
        logger: RegistryLogger;
        /** Test seam only — production callers omit it (⇒ defaultWorkspaceRoot). */
        workspaceRoot?: string;
    });
    /** Look up which app (if any) a shell command ran. Total; never throws. */
    lookup(command: string): {
        appId: string;
        script: string;
    } | null;
    /** The apps this harness currently covers (for the badge/validator layer). */
    coveredApps(): AppCoverage[];
    /** (Re)build the known-script set when stale. Never throws. */
    private refresh;
    private setEmpty;
    /**
     * Best-effort write of the coverage signal the badge/validator layer (Bite
     * 2a) consumes. Only rewrites when the covered set changed. Never throws —
     * coverage is a convenience signal, not on the integrity-critical path.
     */
    private writeCoverage;
}
export {};
//# sourceMappingURL=appScriptRegistry.d.ts.map