/**
 * Tests for apps/scheduledAttribution — AL-1.2's before_agent_run join of a
 * cron-driven turn to its app (docs/build-AL-1.2-scheduled-attribution.md).
 *
 * Lane A: gateway session key ``cron:<job.id>`` → job name via the bot's own
 * ``~/.openclaw/cron/jobs.json`` fixture → app_id via
 * ``{shared}/{bot}/app-cron-map.json`` → recordScheduled (source
 * "oc_cron_map"). Lane B: ``{shared}/{bot}/app-runs/<sessionId>.json`` claim
 * → recordScheduled (source "claim_file"), claim consumed, stale claims
 * swept after 24h. No match at any step → nothing recorded, never a guess.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/scheduledAttribution.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import { createRequire } from "node:module";
import * as os from "node:os";
import * as path from "node:path";

import {
  captureScheduledAttribution,
  cronJobIdFromSessionKey,
  SOURCE_OC_CRON_MAP,
  SOURCE_CLAIM_FILE,
  CLAIM_DIR_NAME,
  CRON_MAP_FILENAME,
  _resetScheduledAttributionForTests,
} from "../dist/apps/scheduledAttribution.js";
import { resolveForTurn, _resetForTests } from "../dist/apps/AppAttribution.js";


const NONE = {
  app_id: null,
  app_attribution: "none",
  app_confidence: null,
  app_attribution_source: null,
};

const JOB_ID = "0b6a5c9e-1111-4222-8333-444455556666";
const SESSION_ID = "865675cf-02a8-4f00-8c81-6d598835914d";

/** Build a {sharedDir, homeDir} fixture pair: jobs.json in the fake bot home,
 *  app-cron-map.json in the fake shared dir. */
function makeFixture({ jobs, map } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-sched-"));
  const homeDir = path.join(root, "home");
  const sharedDir = path.join(root, "shared");
  fs.mkdirSync(path.join(homeDir, ".openclaw", "cron"), { recursive: true });
  fs.mkdirSync(path.join(sharedDir, "team_bot_a"), { recursive: true });
  if (jobs !== undefined) {
    fs.writeFileSync(
      path.join(homeDir, ".openclaw", "cron", "jobs.json"),
      JSON.stringify(jobs),
    );
  }
  if (map !== undefined) {
    fs.writeFileSync(
      path.join(sharedDir, "team_bot_a", CRON_MAP_FILENAME),
      JSON.stringify(map),
    );
  }
  return { root, homeDir, sharedDir };
}

function opts(fx) {
  return { sharedDir: fx.sharedDir, botId: "team_bot_a", homeDir: fx.homeDir };
}

// The session-key shape the live gateway (OC 2026.7) threads through the
// hook ctx for a cron run — observed on the canary proof. The bare
// `cron:<job.id>` form is covered by the extraction tests below.
const CRON_CTX = {
  sessionId: SESSION_ID,
  sessionKey: `agent:main:cron:${JOB_ID}:run:${SESSION_ID}`,
};


// ── Session-key → job-id extraction ──────────────────────────────────────────

test("cronJobIdFromSessionKey handles live, bare, and hostile key shapes", () => {
  // Live OC 2026.7 shape.
  assert.equal(
    cronJobIdFromSessionKey(`agent:main:cron:${JOB_ID}:run:abc-123`),
    JOB_ID,
  );
  // Bare store-internal shape (AL-0.4).
  assert.equal(cronJobIdFromSessionKey(`cron:${JOB_ID}`), JOB_ID);
  // SQLite-migration ids START with "cron" — must not confuse token scan.
  const migrated = "cron-migrated-0-e212cdb73962a9f2";
  assert.equal(
    cronJobIdFromSessionKey(`agent:main:cron:${migrated}:run:abc`),
    migrated,
  );
  // Non-cron keys resolve nothing.
  assert.equal(cronJobIdFromSessionKey("telegram:12345"), null);
  assert.equal(cronJobIdFromSessionKey(`agent:main:${migrated}:run:abc`), null);
  assert.equal(cronJobIdFromSessionKey("cron:"), null);
  assert.equal(cronJobIdFromSessionKey(""), null);
});

test.beforeEach(() => {
  _resetForTests();
  _resetScheduledAttributionForTests();
});


// ── Lane A: OC-cron map join ─────────────────────────────────────────────────

test("cron turn resolves scheduled/oc_cron_map via jobs.json + app-cron-map", () => {
  const fx = makeFixture({
    jobs: { jobs: [{ id: JOB_ID, name: "daily-digest", schedule: "0 9 * * *" }] },
    map: { "daily-digest": "morning-briefing" },
  });
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), {
    app_id: "morning-briefing",
    app_attribution: "scheduled",
    app_confidence: 1.0,
    app_attribution_source: SOURCE_OC_CRON_MAP,
  });
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("jobs.json id→name resolution tolerates a bare-array shape", () => {
  const fx = makeFixture({
    jobs: [{ id: JOB_ID, name: "daily-digest" }],
    map: { "daily-digest": "morning-briefing" },
  });
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.equal(resolveForTurn("run-1", SESSION_ID).app_id, "morning-briefing");
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("no guessing: unknown job id records nothing", () => {
  const fx = makeFixture({
    jobs: { jobs: [{ id: "some-other-id", name: "daily-digest" }] },
    map: { "daily-digest": "morning-briefing" },
  });
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), NONE);
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("no guessing: job name absent from the map records nothing", () => {
  const fx = makeFixture({
    jobs: { jobs: [{ id: JOB_ID, name: "daily-digest" }] },
    map: { "some-other-cron": "other-app" },
  });
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), NONE);
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("no guessing: missing jobs.json / missing map / malformed files record nothing", () => {
  // Missing jobs.json entirely.
  let fx = makeFixture({ map: { "daily-digest": "morning-briefing" } });
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), NONE);
  fs.rmSync(fx.root, { recursive: true, force: true });

  // Missing map.
  _resetScheduledAttributionForTests();
  fx = makeFixture({ jobs: { jobs: [{ id: JOB_ID, name: "daily-digest" }] } });
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.deepEqual(resolveForTurn("run-2", SESSION_ID), NONE);
  fs.rmSync(fx.root, { recursive: true, force: true });

  // Malformed both.
  _resetScheduledAttributionForTests();
  fx = makeFixture();
  fs.writeFileSync(path.join(fx.homeDir, ".openclaw", "cron", "jobs.json"), "{nope");
  fs.writeFileSync(path.join(fx.sharedDir, "team_bot_a", CRON_MAP_FILENAME), "[]");
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.deepEqual(resolveForTurn("run-3", SESSION_ID), NONE);
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("non-cron session keys and missing session ids record nothing", () => {
  const fx = makeFixture({
    jobs: { jobs: [{ id: JOB_ID, name: "daily-digest" }] },
    map: { "daily-digest": "morning-briefing" },
  });
  // Ordinary channel session key.
  captureScheduledAttribution(
    { sessionId: SESSION_ID },
    { sessionId: SESSION_ID, sessionKey: "telegram:12345" },
    opts(fx),
  );
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), NONE);
  // Cron key but no usable session id (never stamp the KEY as the session).
  captureScheduledAttribution({}, { sessionKey: CRON_CTX.sessionKey }, opts(fx));
  assert.deepEqual(resolveForTurn("run-2", CRON_CTX.sessionKey), NONE);
  // Path-hostile session id is refused, not sanitized.
  captureScheduledAttribution(
    { sessionId: "../../etc/passwd" },
    { sessionKey: CRON_CTX.sessionKey },
    opts(fx),
  );
  assert.deepEqual(resolveForTurn("run-3", "../../etc/passwd"), NONE);
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("mtime cache: a map rewrite is picked up on the next turn", () => {
  const fx = makeFixture({
    jobs: { jobs: [{ id: JOB_ID, name: "daily-digest" }] },
    map: { "daily-digest": "old-app" },
  });
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.equal(resolveForTurn("run-1", SESSION_ID).app_id, "old-app");

  const mapPath = path.join(fx.sharedDir, "team_bot_a", CRON_MAP_FILENAME);
  fs.writeFileSync(mapPath, JSON.stringify({ "daily-digest": "new-app-longer-name" }));
  const bumped = new Date(Date.now() + 5000);
  fs.utimesSync(mapPath, bumped, bumped); // defeat same-ms mtime granularity
  const otherSession = "11111111-2222-4333-8444-555566667777";
  captureScheduledAttribution(
    { sessionId: otherSession },
    { sessionId: otherSession, sessionKey: `cron:${JOB_ID}` },
    opts(fx),
  );
  assert.equal(resolveForTurn("run-2", otherSession).app_id, "new-app-longer-name");
  fs.rmSync(fx.root, { recursive: true, force: true });
});


// ── Lane A via the SQLite cron store (current OC: jobs.json is import-once,
//    renamed .migrated at gateway start — the runtime source is
//    ~/.openclaw/state/openclaw.sqlite cron_jobs). node:sqlite needs
//    node ≥22.13; CI runs node 22, older local nodes skip. ──────────────────

const hasNodeSqlite = (() => {
  try {
    return !!createRequire(import.meta.url)("node:sqlite");
  } catch {
    return false;
  }
})();

function writeSqliteCronStore(homeDir, rows) {
  const { DatabaseSync } = createRequire(import.meta.url)("node:sqlite");
  const stateDir = path.join(homeDir, ".openclaw", "state");
  fs.mkdirSync(stateDir, { recursive: true });
  const db = new DatabaseSync(path.join(stateDir, "openclaw.sqlite"));
  db.exec("CREATE TABLE cron_jobs (store_key TEXT, job_id TEXT, name TEXT)");
  const ins = db.prepare("INSERT INTO cron_jobs VALUES (?, ?, ?)");
  for (const r of rows) ins.run("default", r.job_id, r.name);
  db.close();
}

test("cron turn resolves via the SQLite cron store when jobs.json is absent", { skip: !hasNodeSqlite && "node:sqlite unavailable (node <22.13)" }, () => {
  const fx = makeFixture({ map: { "daily-digest": "morning-briefing" } }); // no jobs.json
  writeSqliteCronStore(fx.homeDir, [{ job_id: JOB_ID, name: "daily-digest" }]);
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), {
    app_id: "morning-briefing",
    app_attribution: "scheduled",
    app_confidence: 1.0,
    app_attribution_source: SOURCE_OC_CRON_MAP,
  });
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("SQLite store with no matching job id records nothing", { skip: !hasNodeSqlite && "node:sqlite unavailable (node <22.13)" }, () => {
  const fx = makeFixture({ map: { "daily-digest": "morning-briefing" } });
  writeSqliteCronStore(fx.homeDir, [{ job_id: "some-other-id", name: "daily-digest" }]);
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), NONE);
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("jobs.json wins over the SQLite store when both exist (legacy OC)", { skip: !hasNodeSqlite && "node:sqlite unavailable (node <22.13)" }, () => {
  const fx = makeFixture({
    jobs: { jobs: [{ id: JOB_ID, name: "daily-digest" }] },
    map: { "daily-digest": "json-app", "sqlite-name": "sqlite-app" },
  });
  writeSqliteCronStore(fx.homeDir, [{ job_id: JOB_ID, name: "sqlite-name" }]);
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  assert.equal(resolveForTurn("run-1", SESSION_ID).app_id, "json-app");
  fs.rmSync(fx.root, { recursive: true, force: true });
});


// ── Lane B: claim-file join (AL-0.4 contract) ────────────────────────────────

test("claim file resolves scheduled/claim_file and is consumed", () => {
  const fx = makeFixture();
  const claimDir = path.join(fx.sharedDir, "team_bot_a", CLAIM_DIR_NAME);
  fs.mkdirSync(claimDir, { recursive: true });
  const claim = path.join(claimDir, `${SESSION_ID}.json`);
  fs.writeFileSync(claim, JSON.stringify({ app_id: "wrapped-app", label: "t", ts: "2026-08-15T00:00:00Z" }));

  captureScheduledAttribution({ sessionId: SESSION_ID }, { sessionId: SESSION_ID }, opts(fx));
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), {
    app_id: "wrapped-app",
    app_attribution: "scheduled",
    app_confidence: 1.0,
    app_attribution_source: SOURCE_CLAIM_FILE,
  });
  assert.equal(fs.existsSync(claim), false, "claim is single-use — consumed on read");
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("claim beats the cron map when both match (more specific wins)", () => {
  const fx = makeFixture({
    jobs: { jobs: [{ id: JOB_ID, name: "daily-digest" }] },
    map: { "daily-digest": "map-app" },
  });
  const claimDir = path.join(fx.sharedDir, "team_bot_a", CLAIM_DIR_NAME);
  fs.mkdirSync(claimDir, { recursive: true });
  fs.writeFileSync(
    path.join(claimDir, `${SESSION_ID}.json`),
    JSON.stringify({ app_id: "claim-app" }),
  );
  captureScheduledAttribution({ sessionId: SESSION_ID }, CRON_CTX, opts(fx));
  const r = resolveForTurn("run-1", SESSION_ID);
  assert.equal(r.app_id, "claim-app");
  assert.equal(r.app_attribution_source, SOURCE_CLAIM_FILE);
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("malformed / app_id-less claims record nothing but are still consumed", () => {
  const fx = makeFixture();
  const claimDir = path.join(fx.sharedDir, "team_bot_a", CLAIM_DIR_NAME);
  fs.mkdirSync(claimDir, { recursive: true });
  const claim = path.join(claimDir, `${SESSION_ID}.json`);
  fs.writeFileSync(claim, "{not json");
  captureScheduledAttribution({ sessionId: SESSION_ID }, { sessionId: SESSION_ID }, opts(fx));
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), NONE);
  assert.equal(fs.existsSync(claim), false, "broken claim consumed, not retried forever");
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("sweep removes claims older than 24h and keeps fresh ones", () => {
  const fx = makeFixture();
  const claimDir = path.join(fx.sharedDir, "team_bot_a", CLAIM_DIR_NAME);
  fs.mkdirSync(claimDir, { recursive: true });
  const stale = path.join(claimDir, "aaaaaaaa-0000-4000-8000-000000000000.json");
  const fresh = path.join(claimDir, "bbbbbbbb-0000-4000-8000-000000000000.json");
  fs.writeFileSync(stale, JSON.stringify({ app_id: "stale-app" }));
  fs.writeFileSync(fresh, JSON.stringify({ app_id: "fresh-app" }));
  const old = new Date(Date.now() - 25 * 60 * 60_000);
  fs.utimesSync(stale, old, old);

  // Any capture triggers the (rate-limited) sweep; this turn matches nothing.
  captureScheduledAttribution(
    { sessionId: "cccccccc-0000-4000-8000-000000000000" },
    { sessionKey: "telegram:99" },
    opts(fx),
  );
  assert.equal(fs.existsSync(stale), false, "25h-old claim swept");
  assert.equal(fs.existsSync(fresh), true, "fresh claim kept");
  fs.rmSync(fx.root, { recursive: true, force: true });
});

test("hostile inputs never throw", () => {
  const fx = makeFixture();
  captureScheduledAttribution(null, null, opts(fx));
  captureScheduledAttribution(42, "x", opts(fx));
  captureScheduledAttribution({}, { sessionKey: "cron:" }, opts(fx));
  captureScheduledAttribution(
    { sessionId: SESSION_ID },
    { sessionId: SESSION_ID, sessionKey: 17 },
    { sharedDir: "/nonexistent-root-zzz", botId: "team_bot_a" },
  );
  assert.deepEqual(resolveForTurn("run-1", SESSION_ID), NONE);
  fs.rmSync(fx.root, { recursive: true, force: true });
});
