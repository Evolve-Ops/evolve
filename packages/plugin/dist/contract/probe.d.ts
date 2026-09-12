/**
 * The real probe: an OpenClaw installed on this machine.
 *
 * Everything here is read-only against the installed runtime. The CLI is
 * always invoked with `HOME` (and `OPENCLAW_CONFIG_DIR`) pointed at a
 * throwaway directory this module owns, so a `config validate` or a
 * `doctor` dry-run inside a contract run cannot see — let alone rewrite —
 * a real bot's `openclaw.json`. `--fix` and its siblings are refused by
 * construction (`assertReadOnlyArgs`), because the nightly matrix run
 * executes this suite unattended: the guard has to be in the code, not in
 * the caller's discipline.
 *
 * Resolution order for the runtime under test (`resolveOcInstall`):
 *   1. an explicit `--prefix` — the versioned, side-by-side install shape
 *      from `oc-runtime-versioned-per-bot` (`/Users/Shared/evolve-oc/<v>/`),
 *      and what CI uses (`npm install --prefix <dir> openclaw@<v>`);
 *   2. an explicit `--bin`, whose package root is derived from it;
 *   3. `OPENCLAW_CONTRACT_PREFIX` / `OPENCLAW_CONTRACT_BIN` in the env;
 *   4. nothing — the caller gets `null` and every check skips. A machine
 *      with no OpenClaw does not silently "pass" the contract.
 */
import type { CliResult, OcProbe } from "./types.js";
/** Where an installed OpenClaw lives, and how to run it. */
export interface OcInstall {
    /** Absolute path to the `openclaw` executable. */
    bin: string;
    /** Absolute path to the installed `openclaw` npm package root. */
    packageRoot: string;
}
/**
 * True when the openclaw binary could not be executed at all — no install,
 * wrong path, a prefix that never got one. Distinct from "the command ran
 * and said no": a check that cannot reach the CLI must SKIP, never pass and
 * never fail. See the note on `OcProbe.cliAvailable`.
 */
export declare function cliUnavailable(r: CliResult): boolean;
/** Throw if `args` would mutate anything. Called on every CLI invocation. */
export declare function assertReadOnlyArgs(args: readonly string[]): void;
/** Locate an installed OpenClaw from a prefix, a bin path, or the env. */
export declare function resolveOcInstall(opts?: {
    prefix?: string | null;
    bin?: string | null;
    env?: NodeJS.ProcessEnv;
}): OcInstall | null;
/**
 * Build a probe against a real installed OpenClaw.
 *
 * `repoRoot` is the Evolve repository root, used by the checks that assert
 * Evolve's own side of a contract —
 * e.g. "the plugin guards `before_model_resolve` against its own subagent
 * sessions". Those halves are as much a part of the contract as OC's
 * behaviour: the 2026-09-07 loop needed BOTH the OC change and the missing
 * guard, and Evolve only controls one of them.
 */
export declare function createCliProbe(opts: {
    install: OcInstall;
    repoRoot: string;
    timeoutMs?: number;
    homeDir?: string;
}): OcProbe;
//# sourceMappingURL=probe.d.ts.map