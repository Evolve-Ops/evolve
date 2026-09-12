#!/usr/bin/env node
/**
 * `node dist/contract/cli.js` — run the OpenClaw compatibility contract.
 *
 * Used by three callers and nobody else:
 *   * the nightly CI matrix (`.github/workflows/oc-contract.yml`), which
 *     installs a candidate OpenClaw to a scratch prefix and runs this;
 *   * the per-PR run against the versions the release claims it was tested
 *     against (`openclaw.tested` in packages/plugin/package.json);
 *   * an operator, by hand, against a versioned prefix on the pod.
 *
 * Flags:
 *   --prefix <dir>     npm prefix holding the OpenClaw under test
 *   --bin <path>       the openclaw executable, when there is no prefix
 *   --repo-root <dir>  Evolve repo root (default: three levels up from dist/)
 *   --json <path>      write the full ContractRun JSON here
 *   --matrix <path>    upsert this run's row into an oc-compatibility.md
 *   --only <ids>       comma-separated check ids
 *
 * Exit code is 1 when the run is not a pass, so the nightly can branch on
 * it. That job is deliberately separate from the plugin's own CI: a new
 * OpenClaw release failing the contract is news about OpenClaw, not a
 * reason to redden someone's unrelated plugin PR.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { createCliProbe, resolveOcInstall } from "./probe.js";
import { rowForRun, upsertRow, MATRIX_HEADER } from "./matrix.js";
import { renderTable, runContract } from "./run.js";

function argValue(argv: string[], flag: string): string | null {
  const i = argv.indexOf(flag);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : null;
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  const here = dirname(fileURLToPath(import.meta.url));
  // dist/contract/cli.js → packages/plugin → packages → <repo root>
  const pluginRoot = resolve(here, "..", "..");
  const repoRoot = argValue(argv, "--repo-root") ?? resolve(pluginRoot, "..", "..");

  let evolveVersion = "0.0.0";
  try {
    evolveVersion = JSON.parse(readFileSync(join(pluginRoot, "package.json"), "utf8")).version ?? "0.0.0";
  } catch {
    /* keep the placeholder; the run still reports which OC it ran against */
  }

  const install = resolveOcInstall({
    prefix: argValue(argv, "--prefix"),
    bin: argValue(argv, "--bin"),
  });
  if (!install) {
    process.stderr.write(
      "No OpenClaw to test against. Pass --prefix <npm prefix> or --bin <path>, " +
      "or set OPENCLAW_CONTRACT_PREFIX. A machine with no OpenClaw installed " +
      "cannot validate a version, and the contract will not pretend otherwise.\n",
    );
    return 2;
  }

  const only = (argValue(argv, "--only") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  const probe = createCliProbe({ install, repoRoot });
  const run = await runContract(probe, { evolveVersion, only });

  process.stdout.write(`${renderTable(run)}\n`);

  const jsonPath = argValue(argv, "--json");
  if (jsonPath) writeFileSync(jsonPath, `${JSON.stringify(run, null, 2)}\n`, "utf8");

  const matrixPath = argValue(argv, "--matrix");
  if (matrixPath) {
    let existing = "";
    try {
      existing = readFileSync(matrixPath, "utf8");
    } catch {
      existing = `# OpenClaw compatibility matrix\n\n${MATRIX_HEADER}\n`;
    }
    writeFileSync(matrixPath, upsertRow(existing, rowForRun(run)), "utf8");
  }

  return run.ok ? 0 : 1;
}

// Only self-execute when this file IS the entrypoint — importing it from a
// test must not run a contract.
if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().then((code) => process.exit(code));
}
