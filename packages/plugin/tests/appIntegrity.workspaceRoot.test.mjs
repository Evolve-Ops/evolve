/**
 * Tests for the registry's workspace-root resolution — the S7 fix
 * (docs/audit-gallery-framework-2026-07-02.md §2).
 *
 * The registry used to build the workspace root as a "/Users/<botId>" literal.
 * On the Linux pod (bot homes under /home) that directory doesn't exist, so
 * the registry scanned nothing, covered_apps stayed empty forever, and the
 * admin's "May misreport failures" badge never cleared. The contract under
 * test: the root is derived from the PROCESS home (the plugin runs as the bot
 * user inside the gateway), identically on both pod shapes, with a
 * platform-keyed conventional fallback when no home is resolvable at all.
 *
 * os.homedir() reads $HOME at call time on POSIX, so the tests steer it by
 * setting process.env.HOME (Node's process.env writes through to the real
 * environment).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/appIntegrity.workspaceRoot.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  AppScriptRegistry,
  defaultWorkspaceRoot,
} from "../dist/integrity/appScriptRegistry.js";

const logger = { info() {}, warn() {}, error() {}, debug() {} };

function withHome(home, fn) {
  const prev = process.env.HOME;
  if (home === undefined) delete process.env.HOME;
  else process.env.HOME = home;
  try {
    return fn();
  } finally {
    if (prev === undefined) delete process.env.HOME;
    else process.env.HOME = prev;
  }
}

/** A Linux-pod-shaped bot home (…/home/<bot>) under a temp dir. */
function makeFakeHome(shape) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-wsroot-"));
  const home = path.join(base, ...shape);
  fs.mkdirSync(home, { recursive: true });
  return { base, home };
}

test("workspace root follows the process home, not a /Users literal (Linux shape)", () => {
  const { base, home } = makeFakeHome(["home", "linuxbot"]);
  try {
    withHome(home, () => {
      const root = defaultWorkspaceRoot("linuxbot");
      assert.equal(root, path.join(home, ".openclaw", "workspace"));
      assert.ok(!root.startsWith("/Users/"), `must not be /Users-shaped: ${root}`);
    });
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test("registry scans manifests under the home-derived root (fake Linux home)", () => {
  const { base, home } = makeFakeHome(["home", "linuxbot"]);
  const sharedDir = path.join(base, "shared");
  const workspace = path.join(home, ".openclaw", "workspace");
  const manifestsDir = path.join(workspace, "manifests");
  const script = path.join(workspace, "scripts", "job.py");
  try {
    fs.mkdirSync(manifestsDir, { recursive: true });
    fs.writeFileSync(
      path.join(manifestsDir, "p-abc.json"),
      JSON.stringify({ id: "atlas-research", realized_files: [{ path: script }] }),
    );
    withHome(home, () => {
      // No workspaceRoot override — this exercises the production default.
      const reg = new AppScriptRegistry({ botId: "linuxbot", sharedDir, logger });
      const covered = reg.coveredApps();
      assert.deepEqual(covered, [{ appId: "atlas-research", scripts: [script] }]);
      assert.deepEqual(reg.lookup(`python3 ${script}`), {
        appId: "atlas-research",
        script,
      });
    });
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test("macOS shape unchanged: home /Users/<bot> yields the pre-fix root", () => {
  // When the bot home IS /Users/<bot> (the macOS pod), the derived root must
  // equal what the old literal produced — no behavior change on macOS.
  withHome("/Users/macbot", () => {
    assert.equal(
      defaultWorkspaceRoot("macbot"),
      path.join("/Users", "macbot", ".openclaw", "workspace"),
    );
  });
});

test("no resolvable home falls back to the platform-conventional bot home", () => {
  // $HOME="" makes os.homedir() return "" on POSIX (env var present but
  // empty; no passwd fallback), forcing defaultWorkspaceRoot's last-resort
  // branch: the platform-keyed conventional home for the bot id.
  withHome("", () => {
    const root = defaultWorkspaceRoot("fbbot");
    const expectedHome =
      process.platform === "darwin" ? "/Users/fbbot" : "/home/fbbot";
    assert.equal(root, path.join(expectedHome, ".openclaw", "workspace"));
  });
});
