/**
 * The shared turn row carries the speaker's resolved role, so
 * spend_attribution can tell a person working the bot from a group
 * participant, a blocked sender, or no speaker at all (hold-fix-4810).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.speakerRole.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { TurnObserver } from "../dist/observer/TurnObserver.js";
import { captureSender, _resetForTests } from "../dist/util/senderRegistry.js";

const BOT = "bot-a";

function harness(roster) {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-speakerrole-"));
  fs.mkdirSync(path.join(shared, "rosters"), { recursive: true });
  fs.writeFileSync(path.join(shared, "rosters", `${BOT}.json`), JSON.stringify({ identities: roster }));
  const logger = { debug() {}, info() {}, warn() {}, error() {} };
  const observer = new TurnObserver({
    botId: BOT, role: "member", networkId: "n", sharedDir: shared, tier: "full",
    capabilities: { observer: true }, tierClassification: "session",
    enableLLMSummarization: false, minTurns: 1, keywordConfidenceThreshold: 0.7,
  }, logger, undefined);
  const write = (ctx) => {
    observer.writeTurnToShared("s1", { model: "m", provider: "p", source: "human", channel: "telegram" }, undefined, ctx);
    const dir = path.join(shared, BOT, "turns");
    const f = fs.readdirSync(dir)[0];
    const rows = fs.readFileSync(path.join(dir, f), "utf8").trim().split("\n").map((l) => JSON.parse(l));
    return rows[rows.length - 1];
  };
  return { write };
}

test("turn row stamps the resolved role; unknown speaker resolves to participant", () => {
  _resetForTests();
  const h = harness({ "telegram:1": { role: "primary_user" }, "telegram:3": { role: "blocked" } });
  captureSender("run-owner", { senderId: "1", platform: "telegram" });
  captureSender("run-newcomer", { senderId: "2", platform: "telegram" });
  captureSender("run-blocked", { senderId: "3", platform: "telegram" });
  assert.equal(h.write({ runId: "run-owner", sessionKey: "agent:main:telegram:direct:1" }).speaker_role, "primary_user");
  assert.equal(h.write({ runId: "run-newcomer", sessionKey: "agent:main:telegram:direct:2" }).speaker_role, "participant");
  assert.equal(h.write({ runId: "run-blocked", sessionKey: "agent:main:telegram:direct:3" }).speaker_role, "blocked");
});

test("a turn with no captured sender (cron, heartbeat) carries no role", () => {
  _resetForTests();
  const h = harness({});
  assert.equal(h.write({ runId: "run-none", sessionKey: "agent:main:cron:1" }).speaker_role, null);
  assert.equal(h.write(undefined).speaker_role, null);
});
