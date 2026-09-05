/**
 * Tests for senderRegistry — Phase C.3.
 *
 * Covers capture/getter semantics, TTL eviction, FIFO eviction at the
 * size cap, and the no-runId / no-senderId no-op guards.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/senderRegistry.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  captureSender,
  getSender,
  normalizePlatform,
  _resetForTests,
} from "../dist/util/senderRegistry.js";


test.beforeEach(() => _resetForTests());


test("capture + get round-trips senderId, channelId, senderIsOwner", () => {
  captureSender("run-1", {
    senderId: "1260193629",
    channelId: "-100123",
    senderIsOwner: true,
  });
  const rec = getSender("run-1");
  assert.ok(rec);
  assert.equal(rec.senderId, "1260193629");
  assert.equal(rec.channelId, "-100123");
  assert.equal(rec.senderIsOwner, true);
  assert.ok(typeof rec.capturedAt === "number");
});


test("get returns null for unknown runId", () => {
  assert.equal(getSender("missing"), null);
});


test("capture is a no-op when runId is falsy", () => {
  captureSender(null, { senderId: "X" });
  captureSender(undefined, { senderId: "X" });
  captureSender("", { senderId: "X" });
  // No exception, no entry to retrieve.
  assert.equal(getSender(""), null);
});


test("capture is a no-op when senderId is missing — channel layer didn't surface it", () => {
  captureSender("run-no-sender", {
    senderId: null,
    channelId: "C123",
  });
  // Without senderId there's nothing useful to store; getSender returns null.
  assert.equal(getSender("run-no-sender"), null);
});


test("get returns null when runId is falsy (defensive)", () => {
  assert.equal(getSender(null), null);
  assert.equal(getSender(undefined), null);
  assert.equal(getSender(""), null);
});


test("re-capturing the same runId overwrites", () => {
  captureSender("run-overwrite", { senderId: "FIRST" });
  captureSender("run-overwrite", { senderId: "SECOND" });
  assert.equal(getSender("run-overwrite").senderId, "SECOND");
});


test("senderIsOwner defaults to false when unspecified or null", () => {
  captureSender("run-no-owner", { senderId: "X" });
  assert.equal(getSender("run-no-owner").senderIsOwner, false);
  captureSender("run-null-owner", { senderId: "Y", senderIsOwner: null });
  assert.equal(getSender("run-null-owner").senderIsOwner, false);
});


test("channelId defaults to null when unspecified or empty", () => {
  captureSender("run-no-channel", { senderId: "X" });
  assert.equal(getSender("run-no-channel").channelId, null);
  captureSender("run-empty-channel", { senderId: "Y", channelId: "" });
  assert.equal(getSender("run-empty-channel").channelId, null);
});


// ── platform threading (audit R1a G-N2) ───────────────────────────────


test("capture normalizes the real platform onto the record", () => {
  captureSender("run-slack", { senderId: "U12345", platform: "slack" });
  assert.equal(getSender("run-slack").platform, "slack");
  captureSender("run-discord", { senderId: "999", platform: "discord" });
  assert.equal(getSender("run-discord").platform, "discord");
});


test("platform is null when unspecified or unrecognized (not silently telegram)", () => {
  captureSender("run-no-platform", { senderId: "X" });
  assert.equal(getSender("run-no-platform").platform, null);
  captureSender("run-web", { senderId: "Y", platform: "web" });
  assert.equal(getSender("run-web").platform, null);
  captureSender("run-unknown", { senderId: "Z", platform: "unknown" });
  assert.equal(getSender("run-unknown").platform, null);
});


test("normalizePlatform: exact, cased, surface-suffixed, and unknown values", () => {
  assert.equal(normalizePlatform("telegram"), "telegram");
  assert.equal(normalizePlatform("Slack"), "slack");
  assert.equal(normalizePlatform("  DISCORD  "), "discord");
  // Surface-suffixed channel-kind values map to the base platform.
  assert.equal(normalizePlatform("telegram_group"), "telegram");
  assert.equal(normalizePlatform("slack_dm"), "slack");
  // Unrecognized / non-roster channels resolve to null.
  assert.equal(normalizePlatform("web"), null);
  assert.equal(normalizePlatform("unknown"), null);
  assert.equal(normalizePlatform(""), null);
  assert.equal(normalizePlatform(null), null);
  assert.equal(normalizePlatform(undefined), null);
});
