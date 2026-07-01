# Plugin install trust — signed bypass + declared affordances

**Status:** Draft. Pre-implementation; design review before any code beyond [PR #2293](https://github.com/evolve-ops/evolve/pull/2293) lands.

**Date:** 2026-06-06.

**Origin:** Pod-wide deploy failure 2026-06-06 15:10 — OC 2026.6.1's new install-time dangerous-code scanner blocked the evolve plugin on all 9 bots because `dist/observer/TurnObserver.js:1539` calls `spawn("python3", ...)` (intentional subagent-transport hop). [PR #2293](https://github.com/evolve-ops/evolve/pull/2293) restored deploys by passing `--dangerously-force-unsafe-install` unconditionally. That's a working tourniquet, not a durable answer: it bypasses the scanner for *any* file under `/Users/Shared/evolve-plugin/dist/`, regardless of provenance.

**Adjacent:**

- [`packages/admin/evolve_admin/deploy.py:install_oc_plugin`](../packages/admin/evolve_admin/deploy.py) — the install path that now passes the bypass flag.
- [`packages/plugin/openclaw.plugin.json`](../packages/plugin/openclaw.plugin.json) — manifest extension point.
- `/opt/homebrew/lib/node_modules/openclaw/dist/scanner-CCQg3MsL.js` — OC's hardcoded pattern set (`dangerous-exec`, `dangerous-eval`, `pipe-to-shell`, etc., all severity `critical`, no allowlist).
- [docs/spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md) — runtime exec-policy. Complementary layer; this spec is about *install-time* trust.
- [docs/spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md) — same pattern (privileged operation gated by signed boundary) one tier up.
- Project posture: push upstream when OC ships a parallel capability; the affordance manifest in §2 was the upstream half of this design (now mooted by OC #89516).
- Project posture: review the OC releases page on every upgrade; the new install scanner is exactly the class of feature that rule catches.

---

## Problem

Three forces collide:

1. **OC ships a scanner with no allowlist.** OC 2026.6.1's `install-security-scan` runtime hardcodes patterns (`spawn|exec|execSync|execFile`, `eval|new Function`, `curl|wget | sh`, `chmod 777`, base64-of-length-200+, …) and either blocks the install or accepts `--dangerously-force-unsafe-install` as a blanket bypass. No manifest field, no per-pattern allowlist, no per-file exemption.
2. **Evolve's plugin legitimately needs one of those patterns.** `TurnObserver.spawn("python3", ...)` is the subagent-transport hop — the load-bearing path for evolve helpers (signal store, profile storage, observation pipeline). Replacing it means re-architecting either the transport (admin-daemon unix-socket, like evo's privileged-ops API) or the helpers (port to JS). Both are larger projects; neither is on the critical path.
3. **The blanket bypass we shipped trusts the *path* `/Users/Shared/evolve-plugin/`, not the *content*.** Anything writable to that directory by the `evolve` user — a stray rsync, a tampered local checkout on the dev mini, a future bug in the puller — installs into every bot with the scanner disabled. The bypass is a category, not a check.

Forward-looking: the gallery / third-party app surface inherits all three problems if apps ever ship as OC plugin bundles. We need a model that distinguishes "code we built and signed" from "code we ran the scanner against" from "code that declared what it needs and got operator approval."

---

## Principle

**Plugin install trust comes from one of three sources, in order of preference:**

1. **The scanner found nothing.** Default path. No bypass needed. Most third-party plugins should land here.
2. **The manifest declared the affordances and the operator approved.** Each `critical` finding maps to a `declaredAffordances[]` entry naming the pattern, file, and rationale. Operator sees the declarations at install time; install proceeds if every critical is declared, fails closed if not. This is the upstream-OC half of the design.
3. **The plugin's content matches a digest the operator pre-installed and the operator opted into bypass.** Signed bypass: build-time digest of `dist/` recorded in the manifest, recomputed at install, bypass only on match. This is the near-term Evolve half — closes the path-trust gap that PR #2293 leaves open, while we work tier 2 upstream.

Critically, signed bypass is **only for plugins built from this repo**. It is not a mechanism we extend to gallery apps or third-party plugins; those go through tier 1 or tier 2. The signed-bypass key material (digest + comparison) lives in `evolve_admin`, not in OC; it's an Evolve-side check that runs before we hand the install off to OC with the bypass flag.

---

## Architecture

### 1. Signed bypass — the Evolve-side near-term fix

**Manifest extension** (`packages/plugin/openclaw.plugin.json`):

```json
{
  "id": "evolve",
  "main": "dist/index.js",
  ...
  "x-evolve-trust": {
    "distDigest": "sha256:<hex>",
    "digestAlgorithm": "sha256-canonical-v1",
    "builtAt": "2026-06-06T15:00:00Z",
    "builtFromCommit": "e64649ce"
  }
}
```

The `x-` prefix keeps the field out of OC's manifest schema (and out of any OC config-validate gates). It's Evolve-private metadata.

**Digest algorithm** (`sha256-canonical-v1`): canonical traversal of `dist/`, sorted by relative path, each file's content hashed, then a final hash over `path + ":" + filehash + "\n"` joined lines. Stable across machines, independent of mtimes / inode order.

**Build-time stamping** — extend `build_plugin()` in `evolve_admin.deploy`:

1. `tsc` (existing).
2. Compute digest over `packages/plugin/dist/`.
3. Read `packages/plugin/openclaw.plugin.json`.
4. Write back with `x-evolve-trust.distDigest` set to the new digest, `builtAt = utcnow().isoformat()`, `builtFromCommit = git rev-parse HEAD`.
5. Stage the updated manifest into `/Users/Shared/evolve-plugin/openclaw.plugin.json` alongside `dist/`.

The manifest's digest field is *output*, not input. Operators never write it by hand; the build computes it. Re-runs of `build_plugin()` are idempotent on a clean tree (same source → same digest → same manifest), so this doesn't churn `git status` unless the plugin source actually changed.

**Install-time verification** — new helper `verify_evolve_plugin_signature(plugin_dir: Path) -> tuple[bool, str]`:

1. Read `openclaw.plugin.json` from `plugin_dir`.
2. Extract `x-evolve-trust.distDigest`.
3. Recompute the digest over `plugin_dir / "dist"`.
4. Compare. Return `(True, "")` on match, `(False, "digest mismatch: manifest=… computed=…")` otherwise.

In `install_oc_plugin` (today's bypass site):

```python
ok, msg = verify_evolve_plugin_signature(PLUGIN_INSTALL_DIR)
if ok:
    cmd = [..., "--dangerously-force-unsafe-install", "-l", str(PLUGIN_INSTALL_DIR)]
else:
    # Refuse to bypass; surface a clear error. Either re-run `build_plugin()`
    # or investigate the divergence — a mismatched digest means dist/ has been
    # touched since the last build.
    raise RuntimeError(
        f"Evolve plugin digest verification failed: {msg}. "
        f"Re-run `sudo evolve-admin upgrade` to rebuild, or investigate "
        f"what wrote to {PLUGIN_INSTALL_DIR}/dist/ since the last build."
    )
```

**Failure surface:** when the digest mismatches, we fail closed. The deploy stops with a clear actionable error. We do NOT fall back to installing without the bypass (OC will reject) and do NOT pass the bypass flag anyway (defeats the point). The recovery is "rebuild the plugin" — one command — which is what an operator does after a code change anyway.

**Signal:** add `plugin_digest_mismatch` Signal (producer `deploy`, severity `error`, signature `(plugin_id, observed_digest, manifest_digest)`) so a mismatch caught during deploy surfaces in Alerts even when the operator missed the CLI error. Auto-archives when the next deploy succeeds.

### 2. Declared affordances — the upstream-OC half

**Proposed OC manifest extension** (`openclaw.plugin.json`):

```json
{
  "declaredAffordances": [
    {
      "pattern": "child_process",
      "ruleId": "dangerous-exec",
      "file": "dist/observer/TurnObserver.js",
      "rationale": "Subagent transport — spawn python3 to invoke evolve helpers (signal store, profile storage). Audited; see docs/spec-plugin-install-trust-2026-06-06.md."
    }
  ]
}
```

Fields:
- `pattern` — matches the scanner's `requiresContext` regex (e.g. `child_process`, `eval`, `network`).
- `ruleId` — the scanner's rule identifier (`dangerous-exec`, `dangerous-eval`, …) so OC can scope the exemption tightly.
- `file` — relative path; substring match against `finding.file`. Wildcards (`dist/**/*.js`) deferred to v2.
- `rationale` — operator-readable. Shown at install time.

**Scanner integration** (OC side, what we ask upstream for):

1. Run the existing scanner.
2. For each `critical` finding, check if any `declaredAffordances[]` entry covers `(ruleId, file)`. A match means the operator-and-author-acknowledged this pattern.
3. Covered findings drop to severity `declared` (new); they appear in the install record's audit log but don't block.
4. Uncovered findings retain `critical` and block as today.
5. CLI install prompt shows declared affordances + their rationales; operator sees what they're approving. `--quiet` accepts them without prompt (for automation); `--review` prompts even when none.

**Pull-request shape:** small (~200 LOC in `install-security-scan.runtime`), additive (no behavior change for plugins without declarations), back-compat (older OC ignores the unknown field). Easy upstream sell.

**Evolve's manifest gets the affordance once OC ships this:**

```json
{
  "id": "evolve",
  ...
  "declaredAffordances": [
    {
      "pattern": "child_process",
      "ruleId": "dangerous-exec",
      "file": "dist/observer/TurnObserver.js",
      "rationale": "Subagent transport — see docs/spec-plugin-install-trust-2026-06-06.md"
    }
  ],
  "x-evolve-trust": { ... }  // signed bypass kept as defense-in-depth
}
```

The two mechanisms compose: declared affordances let the scanner accept the install without bypass; the signed digest still gets recorded and verified so a tampered `dist/` is caught even when the manifest *would* let it through.

### 3. Per-app affordance contract — for the gallery, follow-on

Each gallery app's `manifest.yaml` (already extant per [spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md)) extends with the same `declaredAffordances` shape:

```yaml
declaredAffordances:
  - pattern: child_process
    file: scripts/sync_inbox.py
    rationale: Spawns gmail CLI helper. Audited line 47.
```

At app install (`evolve_admin.applications.install_helpers`):

1. Static check: scan the app bundle for OC's scanner patterns.
2. Compare findings to the app's `declaredAffordances`.
3. Show operator the declared set; refuse-to-install on undeclared criticals.
4. Stamp the approved affordances into the installed wrapper as a `# evolve-managed:` header (we already do similar provenance stamping).
5. Future workspace audit re-checks that on-disk wrappers haven't drifted from declared affordances → drift Signal.

This piggy-backs on existing `evolve-managed` markers and existing audit machinery. No new substrate.

**Note on scope:** workspace files (the launchd wrappers and HEARTBEAT.md sections we generate today) are NOT plugin-installed and never see OC's plugin scanner. The per-app affordance contract exists for *operator visibility and audit*, not to satisfy OC's installer. If OC ever extends scanning to workspace artifacts, the same declarations satisfy that scanner too.

---

## Migration plan

### Phase A — Signed bypass (Evolve-side, ~1 day)

1. Add `_canonical_dist_digest(dist_dir: Path) -> str` to `evolve_admin.deploy`.
2. Extend `build_plugin()` to stamp `x-evolve-trust` into `openclaw.plugin.json` post-`tsc`.
3. Add `verify_evolve_plugin_signature(plugin_dir: Path)` and call it in `install_oc_plugin` before passing `--dangerously-force-unsafe-install`.
4. Add `plugin_digest_mismatch` Signal type.
5. Test: build → install (pass), tamper `dist/index.js` post-build → install (fail with clear error), rebuild → install (pass).
6. Land as one PR; back-compat with PR #2293's blanket bypass via a feature flag that defaults on for one release, then off.

Acceptance: a tampered `/Users/Shared/evolve-plugin/dist/index.js` deploy aborts before the OC bypass; rebuilding restores the deploy.

### Phase B — File the OC upstream issue + draft PR (parallel to A, ~1 week)

1. Open issue against `openclaw/openclaw` summarizing the design from §2 (declared affordances, ruleId + file matching, severity downgrade, CLI prompt).
2. If response is positive, draft the PR against `install-security-scan.runtime`. Small surface; should be reviewable.
3. Track upstream status in [`docs/upstream-tracking.md`](upstream-tracking.md) per the `upstream-issue-watcher` daemon convention.
4. When merged: add `declaredAffordances` to `packages/plugin/openclaw.plugin.json`; keep signed bypass as belt-and-suspenders.

Acceptance: OC issue filed within a week; PR drafted whether or not OC accepts. If OC rejects (unlikely — pattern is clean and additive), document the fallback (continue signed bypass indefinitely).

### Phase C — Per-app affordance contract (gallery, after the first non-trivial third-party app)

1. Extend gallery `manifest.yaml` schema with `declaredAffordances[]`.
2. Add static-scan step to `evolve_admin.applications.install_helpers.install_app`.
3. Stamp declarations into generated wrappers' `# evolve-managed:` header.
4. Add workspace-audit drift check (compare wrapper-stamped affordances to source app's declarations) → reuses existing audit infrastructure.
5. Admin UI surface: per-app card shows declared affordances; install confirmation modal lists them.

Acceptance: a gallery app declaring `child_process` installs cleanly with operator confirmation; a gallery app that uses `child_process` without declaring it fails to install with a clear "this app uses X but didn't declare it" error.

Trigger for Phase C is the first non-trivial third-party app, not a date. Until then, our own apps don't ship gallery-style and the affordance check would be over-engineering.

---

## What this does NOT do

- **Replace OC's scanner.** We rely on OC for the actual pattern detection. Our additions are signature + declared-allowlist; the rule set itself stays upstream.
- **Replace runtime exec policy.** [spec-app-derived-permissions-2026-05-24.md](spec-app-derived-permissions-2026-05-24.md) handles per-bot runtime exec gating; this spec is install-time only. The two layers are orthogonal: a plugin can pass install scanning *and* still have its at-runtime exec calls denied by the bot's exec policy.
- **Cover workspace files.** OC's scanner doesn't see them. Per-app affordance declarations exist for our own audit, not because anyone else is checking.
- **Replace the marker-comment system.** `<!-- evolve-managed -->` markers stay; they're inert metadata, load-bearing for refuse-to-clobber, never scanner-relevant.
- **Sign third-party plugins.** Signed bypass is Evolve-built-only. Third-party trust comes through tier 1 (clean scan) or tier 2 (declared affordances).

---

## Open questions

1. **Where does the digest comparison happen if the operator builds the plugin themselves vs. installs from a release tarball?** Build-time digest computation works for both. The manifest committed to git would carry a stale digest from the last build; that's OK as long as `build_plugin()` runs on every deploy (it does). Consider: should the committed manifest carry *any* digest? A null value with a build-time fill would prevent stale-digest confusion; downside is git-status churn on every build. Leaning toward null-in-git, populated-at-build.

2. **What about plugins we install via `oc_neutralize.install_plugin` (upstream @openclaw/* packages)?** They're installed without `-l` (npm spec, not local path). The bypass flag isn't passed there today and shouldn't be — those plugins should pass the scanner cleanly (they're authored against the scanner). If an @openclaw/* plugin ever fails the scanner, that's an upstream bug to file, not something we work around.

3. **Should signed bypass extend to `evolve-plugin/` updates pushed via `repo-puller`?** The puller runs every 15 min; if it lands new `dist/` files between deploys, the digest won't match until the next `build_plugin()` runs. Decision: the puller pulls source; deploy rebuilds `dist/`. The digest only matters at deploy time, not at pull time. Document this; consider a `plugin_dist_stale` Signal if the source commit is newer than the digest's `builtFromCommit`.

4. **CLI UX for the upstream `declaredAffordances` prompt:** should we propose a `--yes-affordances` flag for automation, or rely on the existing `--quiet`? Defer to OC's CLI conventions when drafting the PR.

5. **Tooling for third-party plugin authors:** should we ship a `verify-affordances` CLI that scans a plugin and tells the author what to declare? Useful but not blocking; defer to after upstream lands.

---

## Success criteria

This design has worked when:

1. **A tampered `/Users/Shared/evolve-plugin/dist/`** aborts the deploy with a clear error before bypassing the OC scanner. PR #2293's blanket-trust window is closed.
2. **The OC scanner accepts the evolve plugin cleanly** (no `--dangerously-force-unsafe-install` needed) once the upstream `declaredAffordances` field ships and we populate it.
3. **A gallery app that needs a dangerous pattern** can ship by declaring it; the operator sees the declaration before install; undeclared uses fail closed.
4. **The next OC release that tightens the scanner** (new pattern, e.g. flagging `fs.writeFile` to absolute paths) doesn't cause a pod-wide outage — our affordances are declared, mismatch surfaces clearly, response is "rebuild" not "investigate the pod from cold."
5. **Operators can audit what dangerous patterns each plugin uses** by reading the manifest's `declaredAffordances` list — no source-diving required.
