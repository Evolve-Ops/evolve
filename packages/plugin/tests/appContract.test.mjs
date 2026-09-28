/**
 * Tests for the app-contract v1 registration gate (apps/contract/).
 *
 * WHAT THESE PIN:
 *   * **Every shipped `board` verb is on the contract** — BOARD_VERBS and the
 *     mirrored rows agree, so the real registration in index.ts goes through.
 *   * **A verb without a row is refused at registration** — not registered,
 *     logged at error level, and the log names the row it needs.
 *   * **A row of the wrong kind does not count** — a daemon endpoint's name is
 *     not a tool verb's row.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/appContract.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { BOARD_VERBS } from "../dist/tools/BoardTool.js";
import {
  APP_CONTRACT_ROWS, CONTRACT_VERSION, contractRow,
} from "../dist/apps/contract/rows.js";
import {
  ContractRowMissing, registerAppTool, requireToolVerbRows,
} from "../dist/apps/contract/register.js";

function fakeApi() {
  const registered = [];
  return { registered, registerTool: (f) => registered.push(f) };
}

function fakeLogger() {
  const errors = [];
  return { errors, error: (m) => errors.push(m) };
}

test("every board verb carries a v1 tool_verb row", () => {
  assert.equal(CONTRACT_VERSION, "v1");
  for (const verb of BOARD_VERBS) {
    const row = contractRow(`board.${verb}`);
    assert.ok(row, `board.${verb} has no row`);
    assert.equal(row.kind, "tool_verb");
    assert.equal(row.version, "v1");
  }
  assert.doesNotThrow(() => requireToolVerbRows("board", BOARD_VERBS));
});

test("the board tool registers when all its verbs are on the contract", () => {
  const api = fakeApi();
  const log = fakeLogger();
  const factory = () => ({ name: "board" });
  assert.equal(registerAppTool(api, "board", BOARD_VERBS, factory, log), true);
  assert.deepEqual(api.registered, [factory]);
  assert.deepEqual(log.errors, []);
});

test("a verb with no row is refused at registration, naming the row it needs", () => {
  const api = fakeApi();
  const log = fakeLogger();
  const ok = registerAppTool(api, "board", [...BOARD_VERBS, "archive"], () => ({}), log);
  assert.equal(ok, false);
  assert.deepEqual(api.registered, [], "a refused tool must not be registered");
  assert.equal(log.errors.length, 1);
  assert.match(log.errors[0], /tool 'board' refused/);
  assert.match(log.errors[0], /tool_verb 'board\.archive' has no contract row/);
  assert.match(log.errors[0], /app_contract\.py/);
  assert.throws(() => requireToolVerbRows("board", ["archive"]), ContractRowMissing);
});

test("a row of another kind does not satisfy a tool verb", () => {
  // `tracker.propose` is a (queued) daemon_endpoint row, not a tool verb.
  assert.equal(contractRow("tracker.propose")?.kind, "daemon_endpoint");
  assert.throws(() => requireToolVerbRows("tracker", ["propose"]), ContractRowMissing);
});

test("row names are unique and every row names a service 1..12", () => {
  const names = APP_CONTRACT_ROWS.map((r) => r.name);
  assert.equal(new Set(names).size, names.length);
  for (const r of APP_CONTRACT_ROWS) {
    assert.ok(r.service >= 1 && r.service <= 12, `${r.name}: service ${r.service}`);
  }
});
