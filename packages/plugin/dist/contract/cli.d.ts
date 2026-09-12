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
export declare function main(argv?: string[]): Promise<number>;
//# sourceMappingURL=cli.d.ts.map