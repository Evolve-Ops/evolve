/**
 * Tests for ToolProfiles — per-session-type tool registration (CE-2b).
 *
 * Pins:
 *   1. classifySessionKind agrees with the Python rule on the SHARED case
 *      table (packages/analyzer/tests/fixtures/session-kind-cases.json) — a
 *      rule change on one side reddens the other suite.
 *   2. Profile resolution: a scheduled/one-shot session gets the trimmed set,
 *      a user session the full set, an unclassified session the full set.
 *   3. A user session is NEVER trimmed (chip guardrail), for every tool.
 *   4. An out-of-profile tool keeps its name, sheds its schema, and refuses
 *      with text that names the profile — it is never absent.
 *   5. A refusal writes a ledger row; a broken ledger path never throws.
 *   6. The filter fails OPEN: a factory that throws, or a definition with no
 *      name, passes through untouched.
 *   7. The profile table matches the SHARED inventory fixture that the
 *      analyzer's before/after census proof is built from — a profile edit
 *      that lands in only one place reddens the other suite.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/toolProfiles.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  classifySessionKind,
  resolveToolProfile,
  profileAllows,
  applyToolProfile,
  installToolProfileFilter,
  refusalText,
  trimmedDescription,
  TOOL_PROFILES,
  FULL_PROFILE_ID,
  PROFILE_BY_KIND,
} from "../dist/tools/ToolProfiles.js";

const quiet = { warn: () => {}, info: () => {}, debug: () => {} };
const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(HERE, "..", "..", "analyzer", "tests", "fixtures");
const CASES = path.join(FIXTURES, "session-kind-cases.json");
const INVENTORY = path.join(FIXTURES, "tool-profile-inventory.json");

/** A realistic factory: a heavy description + a real parameter schema. */
function factoryFor(name) {
  return (ctx) => ({
    name,
    description: "D".repeat(900),
    parameters: { type: "object", properties: { q: { type: "string" } } },
    async execute() { return { content: [{ type: "text", text: `${name}:${ctx?.sessionKey ?? ""}` }] }; },
  });
}

const defChars = (def) =>
  JSON.stringify({ name: def.name, description: def.description, parameters: def.parameters }).length;

// ── 1. cross-language parity on the shared case table ────────────────────────
test("classifySessionKind matches the shared Python case table", () => {
  const doc = JSON.parse(fs.readFileSync(CASES, "utf8"));
  assert.ok(doc.cases.length >= 15, "case table should cover the real key shapes");
  for (const c of doc.cases) {
    const got = classifySessionKind(c.key, c.channel);
    assert.equal(got.kind, c.kind, `kind for ${JSON.stringify(c.key)}`);
    assert.equal(got.channel, c.expect_channel, `channel for ${JSON.stringify(c.key)}`);
  }
});

test("an absent session key classifies to the full-profile fallback", () => {
  for (const key of [undefined, null, ""]) {
    const { kind } = classifySessionKind(key);
    assert.equal(kind, "other");
    assert.equal(resolveToolProfile(kind).id, FULL_PROFILE_ID);
  }
});

// ── 2/3. profile resolution + the user guardrail ─────────────────────────────
test("scheduled and one-shot trim; user and subagent do not", () => {
  assert.equal(resolveToolProfile("scheduled").id, "no_live_speaker");
  assert.equal(resolveToolProfile("oneshot").id, "no_live_speaker");
  assert.equal(resolveToolProfile("evolve_internal").id, "evolve_dispatch");
  assert.equal(resolveToolProfile("user").id, FULL_PROFILE_ID);
  assert.equal(resolveToolProfile("subagent").id, FULL_PROFILE_ID);
  assert.equal(resolveToolProfile("unindexed").id, FULL_PROFILE_ID);
  assert.equal(resolveToolProfile("other").id, FULL_PROFILE_ID);
  // The guardrail, stated as a property rather than a spot check: no profile
  // reachable from a `user` session may withhold any tool.
  assert.equal(PROFILE_BY_KIND.user, undefined);
  for (const name of ["roster_block", "session_set_tier", "gmail_send", "anything_new"]) {
    assert.ok(profileAllows(resolveToolProfile("user"), name), name);
  }
});

test("a user session registers the full definition unchanged", () => {
  const cfg = { botId: "b", sharedDir: os.tmpdir() };
  const inner = factoryFor("roster_block");
  const wrapped = applyToolProfile(inner, cfg, quiet);
  const ctx = { sessionKey: "agent:main:telegram:direct:1", messageChannel: "telegram" };
  assert.deepEqual(wrapped(ctx).description, inner(ctx).description);
});

// ── 4. the trim: name kept, schema shed, refusal legible ─────────────────────
test("an out-of-profile tool keeps its name, sheds its schema, and refuses", async () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-tp-"));
  const cfg = { botId: "bot-x", sharedDir };
  const inner = factoryFor("roster_block");
  const wrapped = applyToolProfile(inner, cfg, quiet);
  const ctx = { sessionKey: "agent:main:cron:0000", sessionId: "sid-1" };

  const full = inner(ctx);
  const trimmed = wrapped(ctx);
  assert.equal(trimmed.name, "roster_block", "the tool is present, not absent");
  assert.ok(defChars(trimmed) * 3 < defChars(full),
    `trimmed definition should be far smaller: ${defChars(trimmed)} vs ${defChars(full)}`);
  assert.equal(trimmed.description, trimmedDescription(TOOL_PROFILES.no_live_speaker, "scheduled"));

  const out = await trimmed.execute("call-1", {});
  assert.equal(out.isError, true);
  const text = out.content[0].text;
  assert.equal(text, refusalText("roster_block", TOOL_PROFILES.no_live_speaker, "scheduled"));
  assert.ok(text.includes("no_live_speaker"), "the refusal names the profile");
  assert.ok(text.includes("roster_block"), "the refusal names the tool");
  assert.ok(text.includes("Nothing was done"), "the refusal says nothing happened");

  // 5. the refusal is recorded where the evolve-side monitor reads.
  const day = new Date().toISOString().slice(0, 10);
  const file = path.join(sharedDir, "bot-x", "turns", `tool-profile-refusals-${day}.jsonl`);
  const rows = fs.readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].tool_name, "roster_block");
  assert.equal(rows[0].profile, "no_live_speaker");
  assert.equal(rows[0].session_kind, "scheduled");
  assert.equal(rows[0].session_key, "agent:main:cron:0000");
  assert.equal(rows[0].session_id, "sid-1");
  fs.rmSync(sharedDir, { recursive: true, force: true });
});

test("a tool nobody has added to the keep-list is trimmed, not silently kept", () => {
  // The forward-discipline direction, pinned so it stays a decision: a NEW
  // plugin tool is not carried by no_live_speaker until someone adds its name.
  // Not silent — the first background session that reaches for it is refused
  // by name and the refusal raises a tool_profile Signal.
  const cfg = { botId: "b", sharedDir: os.tmpdir() };
  const trimmed = applyToolProfile(factoryFor("brand_new_tool"), cfg, quiet)(
    { sessionKey: "agent:main:cron:0000" });
  assert.equal(trimmed.name, "brand_new_tool");
  assert.match(trimmed.description, /no_live_speaker/);
  // …and a user session still gets it in full.
  assert.equal(
    applyToolProfile(factoryFor("brand_new_tool"), cfg, quiet)(
      { sessionKey: "agent:main:telegram:direct:1" }).description.length, 900);
});

test("an in-profile tool is untouched on a trimmed session", () => {
  const cfg = { botId: "b", sharedDir: os.tmpdir() };
  const ctx = { sessionKey: "agent:main:cron:0000" };
  for (const name of ["defer", "gmail_list_messages", "expand_app"]) {
    const inner = factoryFor(name);
    assert.equal(applyToolProfile(inner, cfg, quiet)(ctx).description, inner(ctx).description, name);
  }
});

test("the evolve_dispatch profile keeps no Evolve tool at all", () => {
  const cfg = { botId: "b", sharedDir: os.tmpdir() };
  const ctx = { sessionKey: "agent:main:explicit:evolve:tier-classifier:1" };
  for (const name of ["defer", "gmail_list_messages", "pod_state"]) {
    const trimmed = applyToolProfile(factoryFor(name), cfg, quiet)(ctx);
    assert.match(trimmed.description, /evolve_dispatch/, name);
  }
});

test("a refusal on an unwritable ledger path still refuses and never throws", async () => {
  const sharedDir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-tp-"));
  fs.writeFileSync(path.join(sharedDir, "bot-x"), "a file blocks the mkdir");
  const wrapped = applyToolProfile(factoryFor("roster_block"), { botId: "bot-x", sharedDir }, quiet);
  const trimmed = wrapped({ sessionKey: "agent:main:cron:0000" });
  const out = await trimmed.execute("call-1", {});
  assert.equal(out.isError, true);
  assert.ok(out.content[0].text.includes("no_live_speaker"));
  fs.rmSync(sharedDir, { recursive: true, force: true });
});

// ── 6. fail-open ─────────────────────────────────────────────────────────────
test("the filter fails open on a throwing factory and on a nameless definition", () => {
  const cfg = { botId: "b", sharedDir: os.tmpdir() };
  const ctx = { sessionKey: "agent:main:cron:0000" };
  const boom = () => { throw new Error("needs live deps"); };
  assert.throws(() => applyToolProfile(boom, cfg, quiet)(ctx), /needs live deps/);
  const nameless = () => ({ description: "no name here" });
  assert.deepEqual(applyToolProfile(nameless, cfg, quiet)(ctx), { description: "no name here" });
});

test("installToolProfileFilter wraps registrations and passes extra args through", () => {
  const seen = [];
  const api = { registerTool: (f, extra) => { seen.push([f, extra]); return "reg-ok"; } };
  installToolProfileFilter(api, { botId: "b", sharedDir: os.tmpdir() }, quiet);
  assert.equal(api.registerTool(factoryFor("roster_block"), "extra"), "reg-ok");
  assert.equal(seen.length, 1);
  assert.equal(seen[0][1], "extra");
  // The registered factory is the WRAPPED one: it trims on a cron session.
  assert.match(seen[0][0]({ sessionKey: "agent:main:cron:0000" }).description, /no_live_speaker/);
});

// ── 7. the profile table matches the shared inventory fixture ────────────────
test("TOOL_PROFILES and PROFILE_BY_KIND match the shared inventory fixture", () => {
  const doc = JSON.parse(fs.readFileSync(INVENTORY, "utf8"));
  const declared = Object.fromEntries(
    Object.entries(TOOL_PROFILES).map(([id, p]) => [id, p.tools === "full" ? "full" : [...p.tools]]),
  );
  assert.deepEqual(declared, doc.profiles);
  assert.deepEqual({ ...PROFILE_BY_KIND }, doc.kind_profiles);
  // Every name a profile keeps must be a tool this pod actually registers —
  // a typo would silently trim the tool it meant to keep.
  const known = new Set(doc.tools.map((t) => t.name));
  for (const [id, tools] of Object.entries(doc.profiles)) {
    if (tools === "full") continue;
    for (const name of tools) assert.ok(known.has(name), `${id} keeps unknown tool ${name}`);
  }
});
