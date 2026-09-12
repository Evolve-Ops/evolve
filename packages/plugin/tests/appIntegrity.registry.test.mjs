/**
 * Tests for the app-integrity recognition core — collectScriptPaths,
 * lookupCommand, tokenize, isExecutablePosition, coverage.
 *
 * Auditor-grade recognition bounds:
 *   • a known app script is matched only at a PATH boundary + EXECUTABLE
 *     position; a non-app command that merely mentions an app path is NOT
 *     matched (`echo /…/foo.py`, `cat /…/foo.py`);
 *   • data files (non-executable extensions) are never treated as app scripts;
 *   • two apps sharing one script both resolve (to the same outcome);
 *   • interpreter / env-assignment / sudo wrappers don't defeat recognition.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/appIntegrity.registry.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  collectScriptPaths,
  knownScriptsForManifest,
  coverageOf,
  lookupCommand,
  tokenize,
  isExecutablePosition,
  stripQuotes,
  toAbs,
  toWorkspaceRel,
} from "../dist/integrity/appScriptRegistry.js";

const WS = "/Users/testbot/.openclaw/workspace";
const SCRIPT_ABS = WS + "/scripts/research.py";

function manifest(overrides = {}) {
  return {
    id: "atlas-research",
    realized_files: [
      { logical_name: "research", path: SCRIPT_ABS, file_id: "f-00000000@2026.06.30-1.0", marker_state: "OWNED" },
      // a DATA file — must never be treated as a runnable script:
      { logical_name: "data", path: WS + "/data/store.json", file_id: "f-00000001@2026.06.30-1.0", marker_state: "OWNED" },
    ],
    ...overrides,
  };
}

// ── collectScriptPaths ───────────────────────────────────────────────────────

test("collectScriptPaths picks executable realized files, skips data files", () => {
  const paths = collectScriptPaths(manifest(), WS);
  assert.deepEqual(paths, [SCRIPT_ABS]);
});

test("collectScriptPaths resolves workspace-relative trigger scripts", () => {
  const m = {
    id: "x",
    event_triggers: [{ id: "t1", invocation: { script: "scripts/t.py", stdout_protocol: "raw_text" } }],
  };
  assert.deepEqual(collectScriptPaths(m, WS), [WS + "/scripts/t.py"]);
});

test("collectScriptPaths strips a leading workspace/ prefix to one canonical abs", () => {
  const m = {
    id: "x",
    event_triggers: [
      { id: "a", invocation: { script: "workspace/scripts/t.py" } },
      { id: "b", invocation: { script: "scripts/t.py" } },
    ],
  };
  assert.deepEqual(collectScriptPaths(m, WS), [WS + "/scripts/t.py"]);
});

test("collectScriptPaths reads interface_contract.cli path tokens", () => {
  const m = {
    id: "x",
    interface_contract: { cli: [{ command: "python3 scripts/cli_tool.py --flag" }] },
  };
  assert.deepEqual(collectScriptPaths(m, WS), [WS + "/scripts/cli_tool.py"]);
});

test("collectScriptPaths is total on garbage manifests", () => {
  assert.deepEqual(collectScriptPaths(null, WS), []);
  assert.deepEqual(collectScriptPaths(42, WS), []);
  assert.deepEqual(collectScriptPaths({}, WS), []);
  assert.deepEqual(collectScriptPaths({ realized_files: "nope" }, WS), []);
});

// ── toAbs / toWorkspaceRel ───────────────────────────────────────────────────

test("toAbs/toWorkspaceRel round-trip; bare basenames produce no rel", () => {
  assert.equal(toAbs("scripts/x.py", WS), WS + "/scripts/x.py");
  assert.equal(toAbs(SCRIPT_ABS, WS), SCRIPT_ABS);
  assert.equal(toWorkspaceRel(SCRIPT_ABS, WS), "scripts/research.py");
  // A file directly under workspace root → no dir separator → no rel match key.
  assert.equal(toWorkspaceRel(WS + "/top.py", WS), "");
  // Outside the workspace → no rel.
  assert.equal(toWorkspaceRel("/etc/passwd", WS), "");
});

// ── lookupCommand: recognition + bounds ──────────────────────────────────────

const scripts = knownScriptsForManifest(manifest(), WS);

test("matches an absolute app-script path run by an interpreter", () => {
  const m = lookupCommand(`python3 ${SCRIPT_ABS} --q hello`, scripts);
  assert.ok(m);
  assert.equal(m.appId, "atlas-research");
  assert.equal(m.script, SCRIPT_ABS);
});

test("matches a workspace-relative invocation", () => {
  assert.ok(lookupCommand("python3 scripts/research.py", scripts));
});

test("matches a chained command (after && / | )", () => {
  assert.ok(lookupCommand(`cd /tmp && python3 ${SCRIPT_ABS}`, scripts));
  assert.ok(lookupCommand(`echo hi | python3 ${SCRIPT_ABS}`, scripts));
});

test("matches through env-assignment + sudo wrappers", () => {
  assert.ok(lookupCommand(`FOO=bar python3 ${SCRIPT_ABS}`, scripts));
  assert.ok(lookupCommand(`sudo -u evolve python3 ${SCRIPT_ABS}`, scripts));
});

test("matches direct ./script execution", () => {
  assert.ok(lookupCommand(`${SCRIPT_ABS} --go`, scripts));
});

test("does NOT match a non-executable mention (echo / cat the path)", () => {
  assert.equal(lookupCommand(`echo ${SCRIPT_ABS}`, scripts), null);
  assert.equal(lookupCommand(`cat ${SCRIPT_ABS}`, scripts), null);
  assert.equal(lookupCommand(`ls -la ${SCRIPT_ABS}`, scripts), null);
});

test("does NOT match a bare basename collision", () => {
  // A different research.py NOT under the app's workspace path.
  assert.equal(lookupCommand("python3 /other/place/research.py", scripts), null);
  assert.equal(lookupCommand("python3 research.py", scripts), null);
});

test("does NOT match a data file even if executed", () => {
  // store.json isn't a script ext → never in the known set.
  assert.equal(lookupCommand(`python3 ${WS}/data/store.json`, scripts), null);
});

test("empty / unknown commands ⇒ null", () => {
  assert.equal(lookupCommand("", scripts), null);
  assert.equal(lookupCommand("ls -la", scripts), null);
  assert.equal(lookupCommand("python3 some_other.py", scripts), null);
});

test("two apps sharing one script both resolve (same outcome)", () => {
  const shared = [
    { appId: "appA", absPath: SCRIPT_ABS, relPath: "scripts/research.py" },
    { appId: "appB", absPath: SCRIPT_ABS, relPath: "scripts/research.py" },
  ];
  const m = lookupCommand(`python3 ${SCRIPT_ABS}`, shared);
  assert.ok(m);
  assert.equal(m.script, SCRIPT_ABS);
  assert.ok(["appA", "appB"].includes(m.appId));
});

// ── coverageOf ───────────────────────────────────────────────────────────────

test("coverageOf groups scripts per app, sorted", () => {
  const cov = coverageOf([
    { appId: "b", absPath: "/b2.py", relPath: "" },
    { appId: "a", absPath: "/a1.py", relPath: "" },
    { appId: "b", absPath: "/b1.py", relPath: "" },
  ]);
  assert.deepEqual(cov, [
    { appId: "a", scripts: ["/a1.py"] },
    { appId: "b", scripts: ["/b1.py", "/b2.py"] },
  ]);
});

// ── tokenize / isExecutablePosition / stripQuotes ────────────────────────────

test("tokenize keeps operators standalone and respects quotes", () => {
  assert.deepEqual(tokenize("a && b | c"), ["a", "&&", "b", "|", "c"]);
  assert.deepEqual(tokenize(`python3 "with space.py"`), ["python3", "with space.py"]);
  assert.deepEqual(tokenize("a;b"), ["a", ";", "b"]);
});

test("isExecutablePosition: index 0 and post-interpreter/operator true; args false", () => {
  const t = tokenize(`echo ${SCRIPT_ABS} && python3 ${SCRIPT_ABS}`);
  // ["echo", abs, "&&", "python3", abs]
  assert.equal(isExecutablePosition(t, 0), true); // echo
  assert.equal(isExecutablePosition(t, 1), false); // abs as echo arg
  assert.equal(isExecutablePosition(t, 4), true); // abs after python3
});

test("stripQuotes removes matched wrapping quotes only", () => {
  assert.equal(stripQuotes(`"x"`), "x");
  assert.equal(stripQuotes("'y'"), "y");
  assert.equal(stripQuotes(`"mismatch'`), `"mismatch'`);
  assert.equal(stripQuotes("plain"), "plain");
});
