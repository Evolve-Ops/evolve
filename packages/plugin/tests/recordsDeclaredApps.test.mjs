/**
 * D-AD8: the `records` tool is shown only for apps the bot's manifests declare
 * with a `store:`. Run from packages/plugin after `npm run build`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { createRecordsToolFactory, declaredStoreApps } from "../dist/tools/records.js";

function pod() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "records-declared-"));
  const ws = path.join(root, "ws");
  const shared = path.join(root, "shared");
  fs.mkdirSync(path.join(ws, "manifests"), { recursive: true });
  fs.mkdirSync(path.join(shared, "apps", "specs"), { recursive: true });
  const manifest = (id) =>
    fs.writeFileSync(path.join(ws, "manifests", `${id}.json`), JSON.stringify({ id, name: id }));
  const spec = (id, body) =>
    fs.writeFileSync(path.join(shared, "apps", "specs", `${id}.json`), JSON.stringify(body));
  return { ws, shared, manifest, spec };
}

test("a bot with no manifests, or none with a store, declares nothing", () => {
  const p = pod();
  assert.deepEqual(declaredStoreApps(p.shared, "b", p.ws), []);
  p.manifest("plain-app");
  p.spec("plain-app", { app_id: "plain-app" });
  assert.deepEqual(declaredStoreApps(p.shared, "b", p.ws), []);
});

test("only declared apps with a store are listed; platform ids never are", () => {
  const p = pod();
  p.manifest("collection-tracker");
  p.spec("collection-tracker", { app_id: "collection-tracker", store: { tables: {} } });
  p.spec("not-mine", { app_id: "not-mine", store: { tables: {} } });
  p.manifest("evolve.directory");
  p.spec("evolve.directory", { store: { tables: {} } });
  assert.deepEqual(declaredStoreApps(p.shared, "b", p.ws), ["collection-tracker"]);
});

test("the description names the declared apps", () => {
  const tool = createRecordsToolFactory(
    { sharedDir: "/x", botId: "b", declaredApps: ["collection-tracker"] },
    { warn() {}, error() {} },
  )({});
  assert.match(tool.description, /collection-tracker/);
});
