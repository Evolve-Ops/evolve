/**
 * Tests for PrefixHashLedger — context-observability Phase 0.
 *
 * Pins:
 *   1. buildPrefixHashRecord emits schema_version 1 with the spec's shape
 *      ({turn_id, prefix_sha256, appended_block_shas: {capabilities, digest,
 *      narrative, speaker}}).
 *   2. Absent/empty blocks record as null — NOT sha256("") — because block
 *      presence flapping is itself a churn signal the join must see.
 *   3. Identical bytes → identical hashes; one changed block changes only
 *      that block's sha (and the combined sha). This is the property the
 *      whole phase rests on: per-block attribution of prefix churn.
 *   4. The writer is dark by default (enabled: false → no file), and when
 *      enabled appends date-rolled JSONL beside the turns file.
 *   5. The writer never throws on an unwritable directory (EACCES posture).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/prefixHashLedger.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  buildPrefixHashRecord,
  prefixHashFileName,
  PrefixHashLedger,
  PREFIX_HASH_SCHEMA_VERSION,
} from "../dist/observer/PrefixHashLedger.js";

const FIXED_NOW = new Date("2026-07-31T12:00:00.000Z");
const quietLogger = { warn: () => {}, debug: () => {} };

function blocksInput(overrides = {}) {
  return {
    botId: "personal-bot",
    sessionId: "sess-1",
    turnId: "run-abc",
    path: "blocks",
    combined: "CAPS\n\nDIGEST\n\nSPEAKER",
    blocks: { capabilities: "CAPS", digest: "DIGEST", speaker: "SPEAKER" },
    now: FIXED_NOW,
    ...overrides,
  };
}

test("record shape matches the spec contract", () => {
  const rec = buildPrefixHashRecord(blocksInput());
  assert.equal(rec.schema_version, PREFIX_HASH_SCHEMA_VERSION);
  assert.equal(rec.type, "prefix_hash");
  assert.equal(rec.ts, FIXED_NOW.toISOString());
  assert.equal(rec.bot_id, "personal-bot");
  assert.equal(rec.session_id, "sess-1");
  assert.equal(rec.turn_id, "run-abc");
  assert.equal(rec.path, "blocks");
  assert.match(rec.prefix_sha256, /^[0-9a-f]{64}$/);
  assert.deepEqual(
    Object.keys(rec.appended_block_shas).sort(),
    ["capabilities", "cost_downgrade", "digest", "narrative", "speaker"],
  );
  assert.equal(rec.combined_chars, "CAPS\n\nDIGEST\n\nSPEAKER".length);
});

test("absent or empty blocks record as null, not sha256 of empty string", () => {
  const rec = buildPrefixHashRecord(blocksInput({ blocks: { capabilities: "CAPS", digest: "" } }));
  assert.match(rec.appended_block_shas.capabilities, /^[0-9a-f]{64}$/);
  assert.equal(rec.appended_block_shas.digest, null);
  assert.equal(rec.appended_block_shas.narrative, null);
  assert.equal(rec.appended_block_shas.speaker, null);
});

test("nothing-appended turns still produce a record with null prefix sha", () => {
  const rec = buildPrefixHashRecord(blocksInput({ combined: "", blocks: {} }));
  assert.equal(rec.prefix_sha256, null);
  assert.equal(rec.combined_chars, 0);
});

test("stable bytes hash identically; a changed block moves only its own sha", () => {
  const a = buildPrefixHashRecord(blocksInput());
  const b = buildPrefixHashRecord(blocksInput());
  assert.deepEqual(a, b);

  const c = buildPrefixHashRecord(
    blocksInput({
      combined: "CAPS\n\nDIGEST-v2\n\nSPEAKER",
      blocks: { capabilities: "CAPS", digest: "DIGEST-v2", speaker: "SPEAKER" },
    }),
  );
  assert.equal(c.appended_block_shas.capabilities, a.appended_block_shas.capabilities);
  assert.equal(c.appended_block_shas.speaker, a.appended_block_shas.speaker);
  assert.notEqual(c.appended_block_shas.digest, a.appended_block_shas.digest);
  assert.notEqual(c.prefix_sha256, a.prefix_sha256);
});

test("directive paths carry no block shas", () => {
  const rec = buildPrefixHashRecord({
    botId: "personal-bot",
    path: "stay_silent",
    combined: "[EVO] stay quiet",
    now: FIXED_NOW,
  });
  assert.equal(rec.path, "stay_silent");
  assert.match(rec.prefix_sha256, /^[0-9a-f]{64}$/);
  assert.deepEqual(rec.appended_block_shas, {
    capabilities: null,
    digest: null,
    narrative: null,
    speaker: null,
    cost_downgrade: null,
  });
});

test("disabled ledger writes nothing", () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-phl-"));
  const ledger = new PrefixHashLedger({
    enabled: false,
    sharedDir,
    botId: "personal-bot",
    logger: quietLogger,
  });
  ledger.record({ path: "blocks", combined: "X", blocks: {}, now: FIXED_NOW });
  assert.equal(fs.existsSync(path.join(sharedDir, "personal-bot", "turns")), false);
  fs.rmSync(sharedDir, { recursive: true, force: true });
});

test("enabled ledger appends date-rolled JSONL beside the turns file", () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-phl-"));
  const ledger = new PrefixHashLedger({
    enabled: true,
    sharedDir,
    botId: "personal-bot",
    logger: quietLogger,
  });
  ledger.record({ path: "blocks", combined: "X", blocks: { capabilities: "X" }, now: FIXED_NOW });
  ledger.record({ path: "blocks", combined: "X", blocks: { capabilities: "X" }, now: FIXED_NOW });

  const file = path.join(
    sharedDir, "personal-bot", "turns", prefixHashFileName(FIXED_NOW),
  );
  assert.equal(path.basename(file), "prefix-hashes-2026-07-31.jsonl");
  const lines = fs.readFileSync(file, "utf8").trim().split("\n");
  assert.equal(lines.length, 2);
  const rec = JSON.parse(lines[0]);
  assert.equal(rec.type, "prefix_hash");
  assert.equal(JSON.parse(lines[1]).prefix_sha256, rec.prefix_sha256);
  fs.rmSync(sharedDir, { recursive: true, force: true });
});

test("unwritable directory never throws — records are silently skipped", () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-phl-"));
  // Make a FILE where the bot dir should be, so mkdirSync fails (ENOTDIR /
  // EEXIST family — the posture is the same as EACCES: fail silent, stop).
  fs.writeFileSync(path.join(sharedDir, "personal-bot"), "not a dir");
  const ledger = new PrefixHashLedger({
    enabled: true,
    sharedDir,
    botId: "personal-bot",
    logger: quietLogger,
  });
  assert.doesNotThrow(() => {
    ledger.record({ path: "blocks", combined: "X", blocks: {}, now: FIXED_NOW });
    ledger.record({ path: "blocks", combined: "X", blocks: {}, now: FIXED_NOW });
  });
  fs.rmSync(sharedDir, { recursive: true, force: true });
});
