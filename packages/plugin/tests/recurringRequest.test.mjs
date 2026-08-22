/**
 * Tests for recurringRequest — conversation-only evidence, in-bot half.
 * Design: docs/design-app-spec-and-discovery-2026-08-15.md §7.1a.
 *
 * Pins:
 *   1. The shared cross-language fixture — outputs AND vocabularies. The
 *      Python twin (evolve_admin.applications.conversation_recurrence)
 *      asserts against the same file, so drift fails on the side that
 *      drifted rather than silently halving the detector's recall.
 *   2. The three fail-closed gates: per-bot DNT flag, per-identity
 *      do_not_track / blocked, and a resolved human requester.
 *   3. The privacy contract — no digit, no content, from any input.
 *
 * HOME is not read by this module, but the fixture dirs are real temp
 * dirs so a stray laptop ~/.openclaw can never satisfy a config read.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/recurringRequest.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  normalizeRequestLabel,
  composeLabel,
  sanitizeTag,
  firstUserTurn,
  MAX_SCAN_CHARS,
  MAX_TAG_CHARS,
  VOLATILE_TOKENS,
  STOPWORDS,
  SENTINEL_TEXTS,
  SYNTHETIC_PREFIXES,
  localHour,
  buildRecurringRequest,
  isRecurringRequestEnabled,
  isRequesterExcluded,
  _resetRecurringRequestCaches,
  MAX_LABEL_TOKENS,
  MIN_LABEL_TOKENS,
  MAX_LABEL_TAGS,
} from "../dist/observer/recurringRequest.js";

const FIXTURE = JSON.parse(
  fs.readFileSync(
    new URL("./fixtures/recurring-request-labels.json", import.meta.url),
    "utf8",
  ),
);

// ── 1. the shared cross-language pin ─────────────────────────────────────────

test("fixture pins the label constants", () => {
  assert.equal(MAX_LABEL_TOKENS, FIXTURE.constants.max_label_tokens);
  assert.equal(MIN_LABEL_TOKENS, FIXTURE.constants.min_label_tokens);
  assert.equal(MAX_LABEL_TAGS, FIXTURE.constants.max_label_tags);
  assert.equal(MAX_SCAN_CHARS, FIXTURE.constants.max_scan_chars);
  assert.equal(MAX_TAG_CHARS, FIXTURE.constants.max_tag_chars);
});

// THE cross-language contract. Probing fixture tokens through the
// normalizer (below) proves this side drops everything the fixture lists,
// but NOT that this side lists nothing extra — a TS-only stopword would
// pass every probe while the two implementations genuinely disagreed.
// Set equality closes that direction, matching what the Python twin asserts.
test("this implementation's vocabularies equal the fixture exactly", () => {
  const v = FIXTURE.vocabulary;
  assert.deepEqual([...VOLATILE_TOKENS].sort(), v.volatile_tokens);
  assert.deepEqual([...STOPWORDS].sort(), v.stopwords);
  assert.deepEqual([...SENTINEL_TEXTS].sort(), v.sentinel_texts);
  assert.deepEqual([...SYNTHETIC_PREFIXES].sort(), v.synthetic_prefixes);
});

test("every fixture normalize case matches", () => {
  assert.ok(FIXTURE.normalize_cases.length >= 20, "fixture must not shrink");
  for (const c of FIXTURE.normalize_cases) {
    assert.equal(
      normalizeRequestLabel(c.input),
      c.expected,
      `case: ${c.name}`,
    );
  }
});

test("every fixture compose case matches", () => {
  for (const c of FIXTURE.compose_cases) {
    assert.equal(composeLabel(c.head, c.tags), c.expected, `case: ${c.name}`);
  }
});

// The vocabularies are the actual cross-language contract: identical code
// with different word lists produces different keys for the same habit.
// Probing every listed token through the normalizer proves this side
// carries the same list without exporting the sets.
test("every fixture volatile token is dropped by this implementation", () => {
  for (const tok of FIXTURE.vocabulary.volatile_tokens) {
    if (tok.length < 3) continue; // dropped by the length floor regardless
    assert.equal(
      normalizeRequestLabel(`${tok} revenue digest`),
      "revenue digest",
      `volatile token not dropped: ${tok}`,
    );
  }
});

test("every fixture stopword is dropped by this implementation", () => {
  for (const tok of FIXTURE.vocabulary.stopwords) {
    if (tok.length < 3) continue;
    assert.equal(
      normalizeRequestLabel(`${tok} revenue digest`),
      "revenue digest",
      `stopword not dropped: ${tok}`,
    );
  }
});

test("every fixture synthetic prefix is refused", () => {
  for (const p of FIXTURE.vocabulary.synthetic_prefixes) {
    assert.equal(normalizeRequestLabel(`${p}:x] send the revenue digest`), null, p);
  }
});

test("every fixture sentinel text is refused", () => {
  for (const s of FIXTURE.vocabulary.sentinel_texts) {
    assert.equal(normalizeRequestLabel(s.toUpperCase()), null, s);
  }
});

// ── 2. privacy contract ──────────────────────────────────────────────────────

test("no digit from any input can reach a label", () => {
  const probes = [
    "send account 4111111111111111 summary report",
    "call me on 555-0134 about the revenue digest",
    "user42 wants the revenue digest",
    "revenue digest for Q3 2026 at 09:30",
  ];
  for (const p of probes) {
    const label = normalizeRequestLabel(p);
    // Assert the probe KEYS first — guarded only by a null check, a
    // regression to all-null would turn this green while asserting nothing.
    assert.notEqual(label, null, `probe stopped keying, test would be vacuous: ${p}`);
    assert.ok(!/\d/.test(label), `digit leaked: ${label}`);
  }
});

test("no digit reaches the COMPOSED label either", () => {
  const label = composeLabel("morning summary", ["invoice-4111111111111111", "email"]);
  assert.ok(!/\d/.test(label), `digit leaked via tag: ${label}`);
});

test("composed label length is bounded however long the tags", () => {
  const label = composeLabel("morning summary", ["x".repeat(5000), "y".repeat(5000)]);
  assert.ok(label.length <= "morning summary".length + 2 + 2 * (MAX_TAG_CHARS + 3), label);
});

test("sanitizeTag rejects content-free input", () => {
  assert.equal(sanitizeTag(""), null);
  assert.equal(sanitizeTag(null), null);
  assert.equal(sanitizeTag("2026"), null);
  assert.equal(sanitizeTag("  --  "), null);
  assert.equal(sanitizeTag("Health-Nutrition"), "health-nutrition");
});

test("a label never exceeds the token cap however long the input", () => {
  const label = normalizeRequestLabel(
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima",
  );
  assert.equal(label.split(" ").length, MAX_LABEL_TOKENS);
});

// ── 3. localHour ─────────────────────────────────────────────────────────────

test("localHour renders the pod-local hour, not UTC", () => {
  // 2026-08-19T16:30Z is 09:30 in Los Angeles (PDT, UTC-7).
  const at = new Date("2026-08-19T16:30:00Z");
  assert.equal(localHour(at, "America/Los_Angeles"), 9);
  assert.equal(localHour(at, "UTC"), 16);
});

test("localHour falls back to UTC on an unusable timezone", () => {
  const at = new Date("2026-08-19T16:30:00Z");
  assert.equal(localHour(at, "Not/AZone"), 16);
});

test("localHour uses a 24h cycle with no 24 for midnight", () => {
  const at = new Date("2026-08-19T00:15:00Z");
  assert.equal(localHour(at, "UTC"), 0);
});

// ── 4. firstUserTurn ─────────────────────────────────────────────────────────

test("firstUserTurn returns the whole first user TURN, not just its text", () => {
  // The requester must come from the same turn as the ask — returning
  // only the text is what allowed the last speaker's identity to be
  // attached to the first speaker's request.
  const turns = [
    { userMessage: "   ", requester: { platform: "telegram", senderId: "1" } },
    { userMessage: "morning summary", requester: { platform: "telegram", senderId: "2" } },
    { userMessage: "thanks", requester: { platform: "telegram", senderId: "3" } },
  ];
  const got = firstUserTurn(turns);
  assert.equal(got.userMessage, "morning summary");
  assert.equal(got.requester.senderId, "2");

  assert.equal(firstUserTurn([]), null);
  assert.equal(firstUserTurn([{ userMessage: 42 }]), null);
});

// ── 5. the gates ─────────────────────────────────────────────────────────────

function tmpShared(network, overlay) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-rr-"));
  if (network !== undefined) {
    fs.writeFileSync(path.join(dir, "network.json"), JSON.stringify(network));
  }
  if (overlay !== undefined) {
    fs.mkdirSync(path.join(dir, "rosters"), { recursive: true });
    fs.writeFileSync(path.join(dir, "rosters", "bot-a.json"), JSON.stringify(overlay));
  }
  _resetRecurringRequestCaches();
  return dir;
}

test("per-bot DNT: default on, only an explicit false disables", () => {
  let dir = tmpShared({ bots: { "bot-a": {} } });
  assert.equal(isRecurringRequestEnabled(dir, "bot-a"), true);

  dir = tmpShared({ bots: { "bot-a": { recurringRequestSignal: false } } });
  assert.equal(isRecurringRequestEnabled(dir, "bot-a"), false);

  // A truthy-but-wrong value must NOT disable — a typo can never silently
  // switch observation off.
  for (const bogus of ["false", 0, null, "off"]) {
    dir = tmpShared({ bots: { "bot-a": { recurringRequestSignal: bogus } } });
    assert.equal(
      isRecurringRequestEnabled(dir, "bot-a"),
      true,
      `bogus value disabled the signal: ${JSON.stringify(bogus)}`,
    );
  }
});

test("per-bot DNT fails open when network.json is missing or corrupt", () => {
  let dir = tmpShared(undefined);
  assert.equal(isRecurringRequestEnabled(dir, "bot-a"), true);

  dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-rr-"));
  fs.writeFileSync(path.join(dir, "network.json"), "{not json");
  _resetRecurringRequestCaches();
  assert.equal(isRecurringRequestEnabled(dir, "bot-a"), true);
});

test("per-identity do_not_track and blocked both exclude", () => {
  let dir = tmpShared({}, { do_not_track: { "telegram:99": { at: "x" } } });
  assert.equal(isRequesterExcluded(dir, "bot-a", "telegram", "99"), true);
  assert.equal(isRequesterExcluded(dir, "bot-a", "telegram", "77"), false);
  // Wrong platform must not match — the key is platform-scoped.
  assert.equal(isRequesterExcluded(dir, "bot-a", "slack", "99"), false);

  dir = tmpShared({}, { blocked: { "telegram:99": { reason: "spam" } } });
  assert.equal(isRequesterExcluded(dir, "bot-a", "telegram", "99"), true);

  dir = tmpShared({}, {});
  assert.equal(isRequesterExcluded(dir, "bot-a", "telegram", "99"), false);

  // No overlay at all → no exclusions (fresh bot).
  dir = tmpShared({});
  assert.equal(isRequesterExcluded(dir, "bot-a", "telegram", "99"), false);
});

// ── 6. buildRecurringRequest end to end ──────────────────────────────────────

const BASE = {
  requestText: "Can you give me my morning summary please?",
  appTags: ["email", "calendar"],
  requester: { platform: "telegram", senderId: "99" },
  at: new Date("2026-08-19T16:30:00Z"),
  timezone: "America/Los_Angeles",
};

test("builds the design's own {label, requester, hour} shape", () => {
  const dir = tmpShared({ bots: { "bot-a": {} } });
  const got = buildRecurringRequest({ ...BASE, sharedDir: dir, botId: "bot-a" });
  assert.deepEqual(got, {
    label: "morning summary: calendar + email",
    requester: "telegram:99",
    hour: 9,
  });
  // Exactly three keys — nothing else may ride along into the shared file.
  assert.deepEqual(Object.keys(got).sort(), ["hour", "label", "requester"]);
});

test("no requester → no row (heartbeat, cron tick, daemon turn)", () => {
  const dir = tmpShared({ bots: { "bot-a": {} } });
  for (const requester of [
    null,
    undefined,
    { platform: null, senderId: "99" },
    { platform: "telegram", senderId: null },
    { platform: "", senderId: "" },
  ]) {
    assert.equal(
      buildRecurringRequest({ ...BASE, requester, sharedDir: dir, botId: "bot-a" }),
      null,
      `requester ${JSON.stringify(requester)} produced a row`,
    );
  }
});

test("per-bot DNT off → no row", () => {
  const dir = tmpShared({ bots: { "bot-a": { recurringRequestSignal: false } } });
  assert.equal(
    buildRecurringRequest({ ...BASE, sharedDir: dir, botId: "bot-a" }),
    null,
  );
});

test("excluded requester → no row", () => {
  const dir = tmpShared(
    { bots: { "bot-a": {} } },
    { do_not_track: { "telegram:99": {} } },
  );
  assert.equal(
    buildRecurringRequest({ ...BASE, sharedDir: dir, botId: "bot-a" }),
    null,
  );
});

test("unkeyable request → no row even with a good requester", () => {
  const dir = tmpShared({ bots: { "bot-a": {} } });
  for (const requestText of ["thanks!", "[cron:x] tick", "HEARTBEAT_OK", ""]) {
    assert.equal(
      buildRecurringRequest({ ...BASE, requestText, sharedDir: dir, botId: "bot-a" }),
      null,
      `requestText ${JSON.stringify(requestText)} produced a row`,
    );
  }
});

test("gates are skipped when no sharedDir/botId is supplied", () => {
  const got = buildRecurringRequest({ ...BASE });
  assert.equal(got.requester, "telegram:99");
});
