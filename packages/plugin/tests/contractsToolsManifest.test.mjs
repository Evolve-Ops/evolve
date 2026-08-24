/**
 * Tests for openclaw.plugin.json's contracts.tools declaration.
 *
 * OC 2026.5.27+ requires plugins to pre-declare every tool name they
 * register with `api.registerTool(...)` under `contracts.tools` in the
 * manifest. If a tool isn't declared, OC silently rejects the
 * registration and the gateway error log spams:
 *
 *   [gateway] [plugins] plugin must declare contracts.tools before
 *   registering agent tools (plugin=evolve, source=.../dist/index.js)
 *
 * The tools then don't exist at runtime — bots can't `defer()`,
 * `session.set_tier(...)`, `record_application(...)`, etc. Silent
 * functional regression across the entire pod.
 *
 * This test pins the manifest list against the actual register-tool
 * call sites in source. Adding a new tool requires updating both —
 * forgetting one trips the test before the silent breakage ships.
 *
 * Run from packages/plugin:
 *   node --test tests/contractsToolsManifest.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const PLUGIN_DIR = path.dirname(path.dirname(__filename));
const MANIFEST = path.join(PLUGIN_DIR, "openclaw.plugin.json");
const TOOLS_DIR = path.join(PLUGIN_DIR, "src", "tools");

function readManifestTools() {
  const raw = fs.readFileSync(MANIFEST, "utf8");
  const manifest = JSON.parse(raw);
  const tools = manifest?.contracts?.tools;
  assert.ok(
    Array.isArray(tools),
    "openclaw.plugin.json must declare contracts.tools as an array " +
      "(OC 2026.5.27+ silently rejects undeclared tool registrations)",
  );
  return tools;
}

function collectSourceToolNames() {
  // Walk packages/plugin/src/tools/*.ts looking for `name: "<tool_name>"`
  // inside the factory's returned object literal. The convention is
  // `createXxxToolFactory(...)` → returns `{ name: "<tool_name>", ... }`
  // — each file has exactly one such name per tool. We accept either
  // single or double quotes and ignore commented-out lines.
  const names = new Set();
  const NAME_RE = /^\s*name:\s*["']([a-z_][a-z0-9_.]*)["']/gm;
  for (const file of fs.readdirSync(TOOLS_DIR)) {
    if (!file.endsWith(".ts")) continue;
    const src = fs.readFileSync(path.join(TOOLS_DIR, file), "utf8");
    // Strip line-comments so test patterns inside `//` don't pollute.
    const stripped = src.replace(/^\s*\/\/.*$/gm, "");
    let m;
    while ((m = NAME_RE.exec(stripped)) !== null) {
      names.add(m[1]);
    }
  }
  return names;
}

test("contracts.tools is declared in openclaw.plugin.json", () => {
  const tools = readManifestTools();
  assert.ok(tools.length > 0, "contracts.tools must list at least one tool");
});

test("every tool registered in src/tools/*.ts is declared in contracts.tools", () => {
  const declared = new Set(readManifestTools());
  const source = collectSourceToolNames();

  const undeclared = [...source].filter((n) => !declared.has(n));
  assert.equal(
    undeclared.length, 0,
    `These tools exist in src/tools/ but are missing from ` +
      `openclaw.plugin.json#contracts.tools — OC will silently reject ` +
      `their registration at gateway startup:\n  ${undeclared.sort().join("\n  ")}\n` +
      `Add them to the manifest's contracts.tools array.`,
  );
});

test("every declared tool in contracts.tools has a matching source registration", () => {
  // Bidirectional check: a name listed in the manifest but not actually
  // registered means we either renamed/deleted a tool and forgot to
  // update the manifest, or copy-pasted an entry. Both worth catching.
  const declared = readManifestTools();
  const source = collectSourceToolNames();

  const orphaned = declared.filter((n) => !source.has(n));
  assert.equal(
    orphaned.length, 0,
    `These tools are declared in openclaw.plugin.json#contracts.tools ` +
      `but no corresponding factory registers them in src/tools/:\n  ${orphaned.sort().join("\n  ")}\n` +
      `Either remove them from the manifest or restore the factory.`,
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// Preflight: every declared tool name must match Anthropic's regex.
//
// INCIDENT (2026-05-28): Anthropic tightened server-side validation on tool
// names to enforce ^[a-zA-Z0-9_-]{1,128}$. PR #1742 declared 14 plugin
// tools in contracts.tools; one — "session.set_tier" — contained a dot.
// As soon as the manifest fix actually surfaced these tools to the model,
// every Anthropic-routed turn started failing pod-wide with:
//
//   tools.<n>.custom.name: String should match pattern ^[a-zA-Z0-9_-]{1,128}$
//
// Three bots on three channels all broke at the same point because they
// share a plugin. This test would have caught it before merge.
// ─────────────────────────────────────────────────────────────────────────────

const ANTHROPIC_TOOL_NAME_REGEX = /^[a-zA-Z0-9_-]{1,128}$/;

test("every declared tool name matches Anthropic's tool-name regex", () => {
  const declared = readManifestTools();
  const offenders = declared.filter((n) => !ANTHROPIC_TOOL_NAME_REGEX.test(n));
  assert.equal(
    offenders.length, 0,
    `These tool names will be rejected by Anthropic's Messages API:\n` +
      `  ${offenders.join("\n  ")}\n\n` +
      `Anthropic enforces ^[a-zA-Z0-9_-]{1,128}$ on tool names. Dots, slashes, ` +
      `spaces, and Unicode dashes are all rejected. Rename the tool ` +
      `(use _ instead of .) in BOTH packages/plugin/openclaw.plugin.json AND ` +
      `the corresponding src/tools/<Name>Tool.ts file. xAI / OpenAI don't ` +
      `enforce this — a bot with non-Anthropic fallback may appear fine ` +
      `but is silently falling back at ~10x cost. Bots with Anthropic-only ` +
      `fallback (e.g. team_bot_c) fail with "Something went wrong".`,
  );
});
