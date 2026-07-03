/**
 * Tests for AppScriptRegistry's coverage-file WRITE path — specifically the
 * file mode contract.
 *
 * The coverage file's consumer is the ADMIN SERVER running as the `evolve`
 * user, not the bot user this plugin runs as. The gateway process runs with a
 * restrictive umask, so an implicit-mode write mints the file 0600 bot-owned
 * — unreadable by the admin reader, whose conservative default then keeps the
 * "May misreport failures" badge on for every app fleet-wide (verified live
 * 2026-07-01). The contract under test: the file is EXPLICITLY 0644 on every
 * write, regardless of umask, stale tmp files, or a legacy 0600 dest.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/appIntegrity.coverageWrite.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  AppScriptRegistry,
  COVERAGE_FILE_MODE,
} from "../dist/integrity/appScriptRegistry.js";

const logger = { info() {}, warn() {}, error() {}, debug() {} };

/**
 * A registry whose bot id can't exist on this machine: the manifests dir
 * lookup fails, so refresh() takes the setEmpty() path — which still writes
 * the coverage file (empty covered set). That exercises the real write path
 * with no /Users fixtures needed.
 */
function registryFor(sharedDir, botId) {
  return new AppScriptRegistry({ botId, sharedDir, logger });
}

function withUmask(mask, fn) {
  const prev = process.umask(mask);
  try {
    return fn();
  } finally {
    process.umask(prev);
  }
}

function tmpShared() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "evolve-cov-"));
}

test("coverage file is 0644 even under a restrictive umask", () => {
  const sharedDir = tmpShared();
  const botId = `covbot-none-${process.pid}`;
  try {
    withUmask(0o077, () => {
      // the live-pod condition that minted 0600
      registryFor(sharedDir, botId).coveredApps();
    });
    const dest = path.join(sharedDir, botId, "app-integrity-coverage.json");
    const st = fs.statSync(dest);
    assert.equal(st.mode & 0o777, COVERAGE_FILE_MODE);
    assert.equal(COVERAGE_FILE_MODE, 0o644);
    // A plugin-minted parent dir must be traversable too — umask 077 would
    // otherwise make it 0700 and defeat the 0644 file inside.
    assert.equal(fs.statSync(path.join(sharedDir, botId)).mode & 0o777, 0o755);
    // Sanity: it's the real payload shape.
    const payload = JSON.parse(fs.readFileSync(dest, "utf8"));
    assert.equal(payload.version, 1);
    assert.equal(payload.bot_id, botId);
    assert.deepEqual(payload.covered_apps, []);
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("rewrite replaces a legacy 0600 dest and a stale 0600 tmp with 0644", () => {
  const sharedDir = tmpShared();
  const botId = `covbot-none-${process.pid}`;
  const dir = path.join(sharedDir, botId);
  const dest = path.join(dir, "app-integrity-coverage.json");
  try {
    // The fleet's pre-fix state: dest already exists at 0600, and a stale
    // 0600 tmp lingers (writeFileSync's mode option is IGNORED for an
    // existing file — only the explicit chmod fixes this).
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(dest, "{}", { mode: 0o600 });
    fs.chmodSync(dest, 0o600);
    fs.writeFileSync(dest + ".tmp", "stale", { mode: 0o600 });
    fs.chmodSync(dest + ".tmp", 0o600);

    withUmask(0o077, () => {
      // Fresh instance ⇒ in-memory de-dup key is empty ⇒ first refresh
      // rewrites the file (the same shape as a gateway restart on the pod).
      registryFor(sharedDir, botId).coveredApps();
    });

    assert.equal(fs.statSync(dest).mode & 0o777, COVERAGE_FILE_MODE);
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});
