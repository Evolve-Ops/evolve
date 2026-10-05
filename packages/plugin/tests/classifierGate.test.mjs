/**
 * Tests for the classifier gate — the ONE switch every Evolve-owned
 * model call reads (D-OH2 / D-OH5,
 * internal/decision-evolve-overhead-2026-09-07.md).
 *
 * The contract under test:
 *   - default OFF: a pod that configures nothing makes no Evolve model calls;
 *   - fail CLOSED: an unreadable or unparseable network.json means off.
 *     The gates this replaced all failed OPEN, which meant the way to turn
 *     four model calls per turn ON was to break the config;
 *   - per-bot overrides win over the pod value in both directions;
 *   - the sample rate is read from config, honoured, and deterministic per
 *     (bot, session) so a session is in or out for its whole life.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/classifierGate.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  readClassifierGate,
  shouldSampleSession,
  ClassifierGateReader,
  DEFAULT_SAMPLE_RATE,
  DEFAULT_SAMPLE_RATE_WITH_PRIOR,
} from "../dist/observer/classifierGate.js";

const BOT = "team-bot-a";

function tmpDir(network) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "classifier-gate-"));
  if (network !== undefined) {
    fs.writeFileSync(
      path.join(dir, "network.json"),
      typeof network === "string" ? network : JSON.stringify(network),
    );
  }
  return dir;
}

// ── Default off, fail closed ────────────────────────────────────────────────

test("gate: a pod with no network.json is OFF", () => {
  const g = readClassifierGate(tmpDir(), BOT);
  assert.equal(g.enabled, false);
  assert.equal(g.source, "unreadable");
});

test("gate: a pod with an empty network.json is OFF", () => {
  const g = readClassifierGate(tmpDir({}), BOT);
  assert.equal(g.enabled, false, "the pod default is off, not on");
  assert.equal(g.source, "config");
});

test("gate: an unparseable network.json fails CLOSED", () => {
  // The inversion that matters. Every gate this replaced failed OPEN, so
  // a broken config enabled the machinery on exactly the pod least able
  // to afford it.
  const g = readClassifierGate(tmpDir("{ not json at all"), BOT);
  assert.equal(g.enabled, false);
  assert.equal(g.source, "unreadable");
});

test("gate: a nonexistent shared dir fails CLOSED", () => {
  const g = readClassifierGate("/nonexistent/path/for/this/test", BOT);
  assert.equal(g.enabled, false);
});

// ── Turning it on ───────────────────────────────────────────────────────────

test("gate: pod-level enabled turns it on", () => {
  const g = readClassifierGate(
    tmpDir({ cascade: { classifiers: { enabled: true } } }), BOT,
  );
  assert.equal(g.enabled, true);
  assert.equal(g.source, "config");
});

test("gate: per-bot ON beats pod OFF", () => {
  const g = readClassifierGate(
    tmpDir({
      cascade: { classifiers: { enabled: false } },
      bots: { [BOT]: { classifiers: { enabled: true } } },
    }),
    BOT,
  );
  assert.equal(g.enabled, true);
});

test("gate: per-bot OFF beats pod ON", () => {
  const g = readClassifierGate(
    tmpDir({
      cascade: { classifiers: { enabled: true } },
      bots: { [BOT]: { classifiers: { enabled: false } } },
    }),
    BOT,
  );
  assert.equal(g.enabled, false);
});

test("gate: another bot's setting does not leak", () => {
  const g = readClassifierGate(
    tmpDir({ bots: { "team-bot-b": { classifiers: { enabled: true } } } }), BOT,
  );
  assert.equal(g.enabled, false);
});

// ── Sample rate ─────────────────────────────────────────────────────────────

test("sample rate: defaults are 10%, and 5% for a bot with a confident prior", () => {
  assert.equal(readClassifierGate(tmpDir({}), BOT, false).sampleRate, DEFAULT_SAMPLE_RATE);
  assert.equal(
    readClassifierGate(tmpDir({}), BOT, true).sampleRate,
    DEFAULT_SAMPLE_RATE_WITH_PRIOR,
  );
});

test("sample rate: comes from network.json when set", () => {
  const dir = tmpDir({
    cascade: {
      classifiers: { enabled: true, sample_rate: 0.25, sample_rate_with_prior: 0.02 },
    },
  });
  assert.equal(readClassifierGate(dir, BOT, false).sampleRate, 0.25);
  assert.equal(readClassifierGate(dir, BOT, true).sampleRate, 0.02);
});

test("sample rate: a per-bot rate beats the pod rate", () => {
  const dir = tmpDir({
    cascade: { classifiers: { sample_rate: 0.25 } },
    bots: { [BOT]: { classifiers: { sample_rate: 0.5 } } },
  });
  assert.equal(readClassifierGate(dir, BOT).sampleRate, 0.5);
});

test("sample rate: out-of-range values clamp rather than throw", () => {
  assert.equal(
    readClassifierGate(tmpDir({ cascade: { classifiers: { sample_rate: -3 } } }), BOT)
      .sampleRate,
    0,
  );
  assert.equal(
    readClassifierGate(tmpDir({ cascade: { classifiers: { sample_rate: 99 } } }), BOT)
      .sampleRate,
    1,
  );
  assert.equal(
    readClassifierGate(
      tmpDir({ cascade: { classifiers: { sample_rate: "half" } } }), BOT,
    ).sampleRate,
    DEFAULT_SAMPLE_RATE,
    "junk falls back to the default rather than sampling everything",
  );
});

// ── Sampling ────────────────────────────────────────────────────────────────

test("sampling: rate 0 samples nothing, rate 1 samples everything", () => {
  assert.equal(shouldSampleSession(BOT, "s-1", 0), false);
  assert.equal(shouldSampleSession(BOT, "s-1", 1), true);
});

test("sampling: the same session always gets the same answer", () => {
  for (const id of ["a", "b", "c", "session-with-a-long-id"]) {
    const first = shouldSampleSession(BOT, id, 0.5);
    for (let i = 0; i < 5; i++) {
      assert.equal(shouldSampleSession(BOT, id, 0.5), first);
    }
  }
});

test("sampling: the observed rate is close to the configured one", () => {
  const N = 4000;
  let hits = 0;
  for (let i = 0; i < N; i++) {
    if (shouldSampleSession(BOT, `session-${i}`, 0.1)) hits++;
  }
  const observed = hits / N;
  assert.ok(
    observed > 0.08 && observed < 0.12,
    `expected ~10% sampled, got ${(observed * 100).toFixed(1)}%`,
  );
});

test("sampling: missing bot or session ids sample nothing", () => {
  assert.equal(shouldSampleSession("", "s", 1), false);
  assert.equal(shouldSampleSession(BOT, "", 1), false);
});

// ── The TTL reader ──────────────────────────────────────────────────────────

test("reader: caches, and invalidate() picks up a config change", () => {
  const dir = tmpDir({ cascade: { classifiers: { enabled: false } } });
  const reader = new ClassifierGateReader(dir, BOT);
  assert.equal(reader.read().enabled, false);

  fs.writeFileSync(
    path.join(dir, "network.json"),
    JSON.stringify({ cascade: { classifiers: { enabled: true } } }),
  );
  assert.equal(reader.read().enabled, false, "still cached within the TTL");
  reader.invalidate();
  assert.equal(reader.read().enabled, true);
});

test("reader: a change in hasConfidentPrior bypasses the cache", () => {
  // The flag selects the default sample rate, so a bot that gains a
  // prior overnight must see the new rate without a gateway restart.
  const reader = new ClassifierGateReader(tmpDir({}), BOT);
  assert.equal(reader.read(false).sampleRate, DEFAULT_SAMPLE_RATE);
  assert.equal(reader.read(true).sampleRate, DEFAULT_SAMPLE_RATE_WITH_PRIOR);
});
