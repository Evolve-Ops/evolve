/**
 * Tests for resolveAnalyzerDir — the EVO-LINUX-PLUGIN-PATH fix.
 *
 * The plugin spawns the standalone analyzer scripts (session_surface.py,
 * task_extractor.py) under {repoRoot}/packages/analyzer. Pre-fix it derived
 * the dir from dirname(sharedDir)+"evolve-repo" — a macOS sibling-layout
 * assumption that is WRONG on Linux:
 *
 *   macOS  sharedDir=/Users/Shared/evolve → /Users/Shared/evolve-repo  ✓ (real checkout)
 *   Linux  sharedDir=/var/lib/evolve      → /var/lib/evolve-repo       ✗ (checkout is /var/lib/evolve/repo)
 *
 * Live evolve-vsp-pod: "/usr/bin/python3: can't open file
 * '/var/lib/evolve-repo/packages/analyzer/session_surface.py'" and the
 * per-turn capability block never rendered. The fix carries the deploy
 * checkout explicitly via config.repoRoot (injected by deploy.py from
 * platform_profile.deploy_checkout_default) and falls back to the legacy
 * derivation only when repoRoot is absent (not-yet-redeployed bots).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.resolveAnalyzerDir.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { resolveAnalyzerDir } from "../dist/observer/TurnObserver.js";

// ── repoRoot present (post-redeploy — the fix) ──────────────────────────────

test("macOS repoRoot → /Users/Shared/evolve-repo/packages/analyzer", () => {
  assert.equal(
    resolveAnalyzerDir({ repoRoot: "/Users/Shared/evolve-repo", sharedDir: "/Users/Shared/evolve" }),
    "/Users/Shared/evolve-repo/packages/analyzer",
  );
});

test("Linux repoRoot → /var/lib/evolve/repo/packages/analyzer (NOT the broken sibling)", () => {
  const dir = resolveAnalyzerDir({ repoRoot: "/var/lib/evolve/repo", sharedDir: "/var/lib/evolve" });
  assert.equal(dir, "/var/lib/evolve/repo/packages/analyzer");
  // The exact regression path the live pod hit.
  assert.notEqual(dir, "/var/lib/evolve-repo/packages/analyzer");
});

test("repoRoot is honored even if sharedDir would derive elsewhere", () => {
  // An operator-overridden sharedDir must not pull the analyzer dir off the
  // explicit repoRoot.
  assert.equal(
    resolveAnalyzerDir({ repoRoot: "/opt/custom/checkout", sharedDir: "/mnt/data/evolve" }),
    "/opt/custom/checkout/packages/analyzer",
  );
});

// ── repoRoot absent (back-compat — not-yet-redeployed bots) ─────────────────

test("no repoRoot, macOS sharedDir → byte-identical to legacy derivation", () => {
  // The macOS path MUST stay /Users/Shared/evolve-repo even on the fallback
  // path — pre-fix bots keep working until they're redeployed.
  assert.equal(
    resolveAnalyzerDir({ sharedDir: "/Users/Shared/evolve" }),
    "/Users/Shared/evolve-repo/packages/analyzer",
  );
});

test("no repoRoot, Linux sharedDir → legacy (still-broken) sibling derivation", () => {
  // Documents the back-compat fallback faithfully: without repoRoot the only
  // information available is sharedDir, so the legacy (wrong-on-Linux) path is
  // what we get. The fix is to inject repoRoot — which deploy.py now does.
  assert.equal(
    resolveAnalyzerDir({ sharedDir: "/var/lib/evolve" }),
    "/var/lib/evolve-repo/packages/analyzer",
  );
});

test("empty/whitespace repoRoot falls back to the legacy derivation", () => {
  assert.equal(
    resolveAnalyzerDir({ repoRoot: "  ", sharedDir: "/Users/Shared/evolve" }),
    "/Users/Shared/evolve-repo/packages/analyzer",
  );
});
