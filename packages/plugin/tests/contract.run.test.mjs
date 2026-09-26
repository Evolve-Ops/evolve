/**
 * Contract run aggregation and the published matrix row.
 *
 * The load-bearing rule here: a run with any SKIP is not a pass. The Update
 * card reads `ok` to decide whether a version is offered at all, so "we
 * could not tell" has to read as "not yet validated" — fail-safe in the
 * direction that costs a wait rather than a recovery
 * (internal/design-oc-upgrade-safety-2026-09-08.md §4).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/contract.run.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { failingIds, renderTable, runContract, summarize } from "../dist/contract/run.js";
import { MATRIX_HEADER, formatRow, parseRow, rowForRun, upsertRow } from "../dist/contract/matrix.js";
import { assertReadOnlyArgs, cliUnavailable, resolveOcInstall } from "../dist/contract/probe.js";
import { makeProbe } from "./contract.fakes.mjs";

const OPTS = {
  evolveVersion: "0.1.0",
  now: () => new Date("2026-09-09T04:05:06Z"),
  elapsed: (() => { let n = 0; return () => (n += 250); })(),
};

test("a skip keeps the run out of 'ok'", async () => {
  const run = await runContract(makeProbe({ runtimeAvailable: false }), { ...OPTS, elapsed: () => 0 });
  assert.equal(run.ok, false);
  assert.ok(run.results.some((r) => r.status === "skip"));
  assert.ok(failingIds(run).includes("hook-ctx-fields-present"));
  assert.match(summarize(run), /could not be checked/);
});

test("a check that throws becomes a skip, not a crash and not a pass", async () => {
  const base = makeProbe();
  const probe = { ...base, evolveSource: async () => { throw new Error("boom"); } };
  const run = await runContract(probe, { ...OPTS, elapsed: () => 0 });
  assert.equal(run.ok, false);
  const r = run.results.find((x) => x.id === "subagent-reentry-guarded");
  assert.equal(r.status, "skip");
  assert.match(r.detail, /boom/);
});

test("--only narrows the run", async () => {
  const run = await runContract(makeProbe(), { ...OPTS, elapsed: () => 0, only: ["hook-ctx-fields-present"] });
  assert.equal(run.results.length, 1);
  assert.equal(run.results[0].id, "hook-ctx-fields-present");
  assert.equal(run.ok, true);
});

test("an empty selection is not a pass", async () => {
  const run = await runContract(makeProbe(), { ...OPTS, elapsed: () => 0, only: ["no-such-check"] });
  assert.equal(run.ok, false);
  assert.equal(run.results.length, 0);
});

test("the run JSON carries the fields the admin side reads", async () => {
  const run = await runContract(makeProbe(), OPTS);
  const parsed = JSON.parse(JSON.stringify(run));
  assert.equal(parsed.ocVersion, "2026.9.2");
  assert.equal(parsed.evolveVersion, "0.1.0");
  assert.equal(parsed.startedAt, "2026-09-09T04:05:06.000Z");
  assert.equal(typeof parsed.durationMs, "number");
  assert.equal(typeof parsed.ok, "boolean");
  for (const r of parsed.results) {
    assert.ok(["pass", "fail", "skip"].includes(r.status));
    assert.equal(typeof r.id, "string");
    assert.equal(typeof r.incident, "string");
  }
});

test("renderTable names the version and every check", async () => {
  const run = await runContract(makeProbe(), { ...OPTS, elapsed: () => 0 });
  const table = renderTable(run);
  assert.match(table, /OpenClaw 2026\.9\.2/);
  for (const r of run.results) assert.ok(table.includes(r.id));
});

// ── the published matrix ────────────────────────────────────────────────────

test("a matrix row round-trips", () => {
  const row = {
    ocVersion: "2026.9.3",
    evolveVersion: "0.1.0",
    result: "fail",
    failing: ["subagent-reentry-guarded", "plugin-install-flags-accepted"],
    checkedAt: "2026-09-09",
  };
  assert.deepEqual(parseRow(formatRow(row)), row);
});

test("a passing row round-trips with an empty failing list", () => {
  const row = { ocVersion: "2026.9.2", evolveVersion: "0.1.0", result: "pass", failing: [], checkedAt: "2026-09-09" };
  assert.deepEqual(parseRow(formatRow(row)), row);
});

test("parseRow ignores the header and the separator", () => {
  for (const line of MATRIX_HEADER.split("\n")) assert.equal(parseRow(line), null);
  assert.equal(parseRow("prose, not a row"), null);
});

test("rowForRun publishes the failing check ids", async () => {
  const probe = makeProbe({ runtimeAvailable: false });
  const row = rowForRun(await runContract(probe, { ...OPTS, elapsed: () => 0 }));
  assert.equal(row.result, "fail");
  assert.equal(row.checkedAt, "2026-09-09");
  assert.ok(row.failing.includes("hook-ctx-fields-present"));
});

test("upsertRow replaces the row for a version instead of appending a second", () => {
  const doc = `# OpenClaw compatibility matrix\n\n${MATRIX_HEADER}\n`;
  const first = upsertRow(doc, {
    ocVersion: "2026.9.3", evolveVersion: "0.1.0", result: "fail",
    failing: ["subagent-reentry-guarded"], checkedAt: "2026-09-09",
  });
  const second = upsertRow(first, {
    ocVersion: "2026.9.3", evolveVersion: "0.1.0", result: "pass",
    failing: [], checkedAt: "2026-09-10",
  });
  const rows = second.split("\n").map(parseRow).filter(Boolean);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].result, "pass");
  assert.equal(rows[0].checkedAt, "2026-09-10");
});

test("upsertRow keeps distinct versions and preserves publication order", () => {
  let doc = `# OpenClaw compatibility matrix\n\n${MATRIX_HEADER}\n`;
  doc = upsertRow(doc, { ocVersion: "2026.9.2", evolveVersion: "0.1.0", result: "pass", failing: [], checkedAt: "2026-09-09" });
  doc = upsertRow(doc, { ocVersion: "2026.9.3", evolveVersion: "0.1.0", result: "fail", failing: ["x"], checkedAt: "2026-09-10" });
  const rows = doc.split("\n").map(parseRow).filter(Boolean);
  assert.deepEqual(rows.map((r) => r.ocVersion), ["2026.9.2", "2026.9.3"]);
});

test("upsertRow creates the table when the document has none", () => {
  const doc = upsertRow("# OpenClaw compatibility matrix\n\nSome prose.\n", {
    ocVersion: "2026.9.2", evolveVersion: "0.1.0", result: "pass", failing: [], checkedAt: "2026-09-09",
  });
  assert.ok(doc.includes(MATRIX_HEADER));
  assert.equal(doc.split("\n").map(parseRow).filter(Boolean).length, 1);
});

// ── read-only guardrail ─────────────────────────────────────────────────────

test("the probe refuses a write-capable invocation", () => {
  assert.doesNotThrow(() => assertReadOnlyArgs(["doctor", "--json"]));
  for (const flag of ["--fix", "--write", "--apply", "--repair", "--migrate", "-f"]) {
    assert.throws(() => assertReadOnlyArgs(["doctor", flag]), /read-only by construction/);
  }
});

test("a machine with no OpenClaw skips every CLI check and passes nothing", async () => {
  // Regression from this module's own first dry run: without cliAvailable,
  // "command not found" came back from every CLI check and THREE of them
  // reported PASS — "we could not tell" wearing validation's clothes. Every
  // CLI-dependent check must skip, and the run must not be ok.
  const run = await runContract(makeProbe({ cliAvailable: false, runtimeAvailable: false }), {
    ...OPTS, elapsed: () => 0,
  });
  assert.equal(run.ok, false);
  const byId = Object.fromEntries(run.results.map((r) => [r.id, r.status]));
  for (const id of [
    "silent-token-stays-silent",
    "evolve-config-keys-accepted",
    "plugin-install-flags-accepted",
    "doctor-preserves-model-refs",
    "channel-plugin-versions-match",
    "gateway-status-deep-surface",
  ]) {
    assert.equal(byId[id], "skip", `${id} must skip when the CLI cannot be executed`);
  }
});

test("cliUnavailable separates 'no binary' from 'the binary said no'", () => {
  assert.equal(cliUnavailable({ code: 127, stdout: "", stderr: "openclaw: command not found" }), true);
  assert.equal(cliUnavailable({ code: 1, stdout: "", stderr: "spawnSync openclaw ENOENT" }), true);
  assert.equal(cliUnavailable({ code: 1, stdout: "", stderr: "unknown command 'gateway'" }), false);
  assert.equal(cliUnavailable({ code: 0, stdout: "2026.9.2", stderr: "" }), false);
});

test("resolveOcInstall returns null when there is nothing to test against", () => {
  assert.equal(resolveOcInstall({ prefix: "/nonexistent/prefix", env: {} }), null);
  assert.equal(resolveOcInstall({ env: {} }), null);
});

test("resolveOcInstall handles both npm prefix layouts", () => {
  // Regression: CI installed OpenClaw with `npm install --prefix` (which
  // writes node_modules/, not lib/node_modules/) and the resolver reported
  // "no OpenClaw to test against" seconds later. Both layouts are real —
  // `-g --prefix` is the pod's side-by-side runtime shape, plain `--prefix`
  // is CI's, since it needs no root.
  const root = mkdtempSync(join(tmpdir(), "oc-prefix-"));

  const globalStyle = join(root, "global");
  mkdirSync(join(globalStyle, "lib", "node_modules", "openclaw"), { recursive: true });
  mkdirSync(join(globalStyle, "bin"), { recursive: true });
  writeFileSync(join(globalStyle, "bin", "openclaw"), "#!/bin/sh\n");
  assert.deepEqual(resolveOcInstall({ prefix: globalStyle, env: {} }), {
    bin: join(globalStyle, "bin", "openclaw"),
    packageRoot: join(globalStyle, "lib", "node_modules", "openclaw"),
  });

  const localStyle = join(root, "local");
  mkdirSync(join(localStyle, "node_modules", "openclaw"), { recursive: true });
  mkdirSync(join(localStyle, "node_modules", ".bin"), { recursive: true });
  writeFileSync(join(localStyle, "node_modules", ".bin", "openclaw"), "#!/bin/sh\n");
  assert.deepEqual(resolveOcInstall({ prefix: localStyle, env: {} }), {
    bin: join(localStyle, "node_modules", ".bin", "openclaw"),
    packageRoot: join(localStyle, "node_modules", "openclaw"),
  });

  rmSync(root, { recursive: true, force: true });
});

// ── the published file ──────────────────────────────────────────────────────

test("internal/oc-compatibility.md carries the header this module writes", () => {
  // The doc is the publication of these rows; if the two formats drift, the
  // nightly's --matrix upsert silently appends a second table.
  const path = new URL("../../../internal/oc-compatibility.md", import.meta.url);
  const doc = readFileSync(path, "utf8");
  assert.ok(doc.includes(MATRIX_HEADER), "the matrix table header does not match matrix.ts");
  // Every data row in the file must parse — a hand-edited row that doesn't
  // is a row the upsert will duplicate rather than replace.
  for (const line of doc.split("\n")) {
    if (!line.trim().startsWith("| ")) continue;
    if (line.includes("OpenClaw | Evolve plugin")) continue;
    const parsed = parseRow(line);
    // Rows in the *checks* table are prose, not matrix rows; only assert on
    // lines whose third cell is a verdict.
    const third = line.split("|")[3]?.trim();
    if (third === "pass" || third === "fail") assert.ok(parsed, `unparseable matrix row: ${line}`);
  }
});
