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
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve as resolvePath } from "node:path";
/**
 * Flags that make a CLI invocation a WRITE. A contract run must never pass
 * one — see the module docstring. Matched on the whole argument so
 * `--fixtures` (a hypothetical read-only flag) is not caught by accident.
 */
const WRITE_FLAGS = new Set([
    "--fix",
    "--write",
    "--apply",
    "--repair",
    "--migrate",
    "-f",
]);
/**
 * True when the openclaw binary could not be executed at all — no install,
 * wrong path, a prefix that never got one. Distinct from "the command ran
 * and said no": a check that cannot reach the CLI must SKIP, never pass and
 * never fail. See the note on `OcProbe.cliAvailable`.
 */
export function cliUnavailable(r) {
    const text = `${r.stdout}\n${r.stderr}`;
    return r.code === 127 || /command not found|ENOENT|no such file or directory|spawnSync/i.test(text);
}
/** Throw if `args` would mutate anything. Called on every CLI invocation. */
export function assertReadOnlyArgs(args) {
    for (const a of args) {
        if (WRITE_FLAGS.has(a)) {
            throw new Error(`contract probe refused a write-capable OpenClaw invocation: ${a} ` +
                `(the contract is read-only by construction — see src/contract/probe.ts)`);
        }
    }
}
/** Locate an installed OpenClaw from a prefix, a bin path, or the env. */
export function resolveOcInstall(opts = {}) {
    const env = opts.env ?? process.env;
    const prefix = opts.prefix ?? env.OPENCLAW_CONTRACT_PREFIX ?? null;
    if (prefix) {
        // Two layouts, because both are real and they differ. `npm install -g
        // --prefix <dir>` — the side-by-side runtime shape from
        // `oc-runtime-versioned-per-bot` — writes lib/node_modules + bin/. A plain
        // `npm install --prefix <dir>` — what CI does, since it needs no root —
        // writes node_modules + node_modules/.bin. Resolving only the first is
        // how this workflow's second run reported "no OpenClaw to test against"
        // seconds after installing one.
        for (const [packageRoot, bin] of [
            [join(prefix, "lib", "node_modules", "openclaw"), join(prefix, "bin", "openclaw")],
            [join(prefix, "node_modules", "openclaw"), join(prefix, "node_modules", ".bin", "openclaw")],
        ]) {
            if (!existsSync(packageRoot))
                continue;
            return { bin: existsSync(bin) ? bin : join(packageRoot, "bin", "openclaw"), packageRoot };
        }
        return null;
    }
    const bin = opts.bin ?? env.OPENCLAW_CONTRACT_BIN ?? null;
    if (bin && existsSync(bin)) {
        // <prefix>/bin/openclaw → <prefix>/lib/node_modules/openclaw
        const packageRoot = resolvePath(bin, "..", "..", "lib", "node_modules", "openclaw");
        return { bin, packageRoot: existsSync(packageRoot) ? packageRoot : "" };
    }
    return null;
}
/** Recursively collect readable text files under `root`, bounded. */
function collectTextFiles(root, maxFiles) {
    const out = [];
    const stack = [root];
    const TEXT = /\.(js|mjs|cjs|ts|d\.ts|json)$/;
    while (stack.length && out.length < maxFiles) {
        const dir = stack.pop();
        let entries;
        try {
            entries = readdirSync(dir);
        }
        catch {
            continue;
        }
        for (const name of entries) {
            if (name === "node_modules" || name.startsWith("."))
                continue;
            const p = join(dir, name);
            let st;
            try {
                st = statSync(p);
            }
            catch {
                continue;
            }
            if (st.isDirectory()) {
                stack.push(p);
            }
            else if (TEXT.test(name)) {
                out.push(p);
                if (out.length >= maxFiles)
                    break;
            }
        }
    }
    return out;
}
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
export function createCliProbe(opts) {
    const timeout = opts.timeoutMs ?? 60_000;
    const home = opts.homeDir ?? mkdtempSync(join(tmpdir(), "evolve-oc-contract-"));
    mkdirSync(join(home, ".openclaw"), { recursive: true });
    let runtimeText = null;
    const readRuntimeText = () => {
        if (runtimeText !== null)
            return runtimeText;
        const parts = [];
        if (opts.install.packageRoot) {
            for (const f of collectTextFiles(opts.install.packageRoot, 4000)) {
                try {
                    parts.push(readFileSync(f, "utf8"));
                }
                catch {
                    /* unreadable file — the scan is best-effort evidence, not a gate */
                }
            }
        }
        runtimeText = parts.join("\n");
        return runtimeText;
    };
    const cli = async (args) => {
        assertReadOnlyArgs(args);
        const r = spawnSync(opts.install.bin, args, {
            cwd: home,
            timeout,
            encoding: "utf8",
            env: {
                ...process.env,
                HOME: home,
                OPENCLAW_CONFIG_DIR: join(home, ".openclaw"),
                // Never let a contract run inherit a live pod's service-repair posture.
                OPENCLAW_SERVICE_REPAIR_POLICY: "",
                CI: "1",
            },
        });
        return {
            code: typeof r.status === "number" ? r.status : 127,
            stdout: r.stdout ?? "",
            stderr: r.stderr ?? (r.error ? String(r.error) : ""),
        };
    };
    let cliOk = null;
    return {
        async cliAvailable() {
            if (cliOk === null)
                cliOk = !cliUnavailable(await cli(["--version"]));
            return cliOk;
        },
        async version() {
            if (opts.install.packageRoot) {
                try {
                    const pkg = JSON.parse(readFileSync(join(opts.install.packageRoot, "package.json"), "utf8"));
                    if (typeof pkg.version === "string")
                        return pkg.version;
                }
                catch {
                    /* fall through to the CLI */
                }
            }
            const r = await cli(["--version"]);
            const m = r.stdout.match(/\d{4}\.\d+\.\d+/);
            return m ? m[0] : null;
        },
        cli,
        async scratchDir(name) {
            const dir = join(home, name);
            mkdirSync(dir, { recursive: true });
            return dir;
        },
        async stageConfig(config) {
            writeFileSync(join(home, ".openclaw", "openclaw.json"), JSON.stringify(config, null, 2), "utf8");
        },
        async validateConfig(config) {
            writeFileSync(join(home, ".openclaw", "openclaw.json"), JSON.stringify(config, null, 2), "utf8");
            const r = await cli(["config", "validate", "--json"]);
            const raw = `${r.stdout}\n${r.stderr}`.trim();
            return {
                ok: r.code === 0,
                messages: raw.split("\n").map((l) => l.trim()).filter(Boolean),
                raw,
            };
        },
        async runtimeAvailable() {
            return readRuntimeText().length > 0;
        },
        async runtimeMentions(needle) {
            return readRuntimeText().includes(needle);
        },
        async evolveSource(repoRelPath) {
            try {
                return readFileSync(join(opts.repoRoot, repoRelPath), "utf8");
            }
            catch {
                return null;
            }
        },
    };
}
//# sourceMappingURL=probe.js.map