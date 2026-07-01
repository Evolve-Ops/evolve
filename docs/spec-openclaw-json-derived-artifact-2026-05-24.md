# Bot-side `openclaw.json` as Derived Artifact

**Status:** Phases 1, 2, 3a–3c shipped 2026-05-25. Phase 3d (L2 cutover) + Phase 4 (admin UI) + Phase 5 (expires_at enforcement) remain. See §12 for the live phased-rollout state.

**Date:** 2026-05-24, last revision 2026-05-25.

**Triggered by:** PR [#1525](https://github.com/evolve-ops/evolve/pull/1525) (2026-05-24). Schema removed `reportingEnabled` from the evolve plugin's `configSchema`; six of seven bots had the (now-disallowed) key baked into their `openclaw.json` from earlier deploys; OC's `additionalProperties: false` rejected the configs on next reload; gateways failed to start; evo went silent in Chat. The schema migration was correct — the on-disk migration was missing.

## 1. Mental model

Each bot's `~<bot>/.openclaw/openclaw.json` is treated as a **materialized projection** of declared inputs, not a source of truth. On every deploy it is regenerated from:

```
openclaw.json  =  merge(
    schema_defaults,                                ← code (plugin + admin)
    pod_defaults from {shared_dir}/network.json,    ← pod-wide authored
    per_bot_overrides from
        {shared_dir}/sandbox/overrides/<bot>.json,  ← per-bot authored
    bot_identity                                    ← still on the bot side
)
```

Three of those four are durable inputs. The fourth (`bot_identity`) is the authored, secret-bearing portion (channel tokens, gateway tokens, plugin allow-list) — it has no shipped default and cannot be "regenerated." Identity stays on the bot side, and deploy is forbidden from rewriting it.

The result: any field the schema layer can produce a value for is **rebuilt** every deploy. Removed-from-schema fields disappear cleanly (the #1525 case). Renamed fields get rewritten under the new name. Added-with-default fields appear without operator action. Per-bot tweaks (Security-Bot's `primary=haiku`, etc.) live in the overrides file — durable, audit-able, and indifferent to redeploys.

## 2. The design problem this resolves

Today's `ensure_plugin_config` ([packages/admin/evolve_admin/deploy.py:1203](packages/admin/evolve_admin/deploy.py:1203)) is **gap-fill**: it `setdefault`s missing keys from the `_PLUGIN_CONFIG_DEFAULTS` registry ([deploy.py:65](packages/admin/evolve_admin/deploy.py:65)) but never overwrites existing values. That preserves per-bot direct edits (good), but it cannot:

- **Strip** a key that the schema has since removed (the #1525 class — *every* schema-tightening leaves silent landmines until the next bot-by-bot redeploy).
- **Rename** a key (operator-set value lives on under the old name).
- **Tell the operator** which keys are intentionally divergent vs accumulated cruft.
- **Re-derive** a key whose default changed in code (operator-implicit values stay stuck at the old default).

The opposite extreme — whole-file rewrite from a registry — would solve all four but destroys legitimate per-bot tweaks (Security-Bot's haiku pin, the safeguards on team-bot-c/security-bot, future operator decisions). The user's framing names this exactly: *"We can't have a design where a redeploy tramples anything. Redeploys will happen all the time."*

The resolution is to **make redeploys idempotent by construction**: deploy reads declared inputs and writes the output, with no path by which a redeploy can lose an operator's intent — because that intent is expressed in the inputs, not in the output file.

## 3. Audience

- **Operators:** edit per-bot overrides through the admin UI (or, in v1, by hand-editing one JSON file under `{shared_dir}/sandbox/overrides/`). Never edit `/Users/<bot>/.openclaw/openclaw.json` directly — those edits will be overwritten on the next deploy.
- **RSI / L2 appliers:** write to the overrides file, not to bot-side `openclaw.json`. Provenance is recorded as `set_by = rsi:<proposal_id>`.
- **Plugin / admin authors:** removing or renaming a Policy-tier key is now a safe operation. Adding one only requires updating the `_PLUGIN_CONFIG_DEFAULTS` registry.

## 4. Three categories of state in `openclaw.json`

Inherits the taxonomy from [docs/spec-config-sandbox-2026-05-09.md §3](docs/spec-config-sandbox-2026-05-09.md). Each top-level key in `openclaw.json` falls into exactly one of:

| Category | Source of truth | Behavior on deploy |
|---|---|---|
| **Schema defaults** (Policy) | `_PLUGIN_CONFIG_DEFAULTS` + plugin `configSchema` | Re-derived every deploy. Stripped if removed from schema. |
| **Pod defaults** (Policy) | `{shared_dir}/network.json` | Re-derived every deploy from network.json. |
| **Per-bot overrides** (Policy) | `{shared_dir}/sandbox/overrides/<bot_id>.json` | Re-derived every deploy from this file. Schema-validated. |
| **Identity** (non-cascading) | The on-disk `openclaw.json` itself | Preserved verbatim. Never rewritten by deploy. |

**Policy keys** — the regenerable ones — are exactly the keys enumerated in [spec-config-sandbox §5.5](docs/spec-config-sandbox-2026-05-09.md) under "evolve-relevant keys": `plugins.entries.evolve.config.*`, `agents.defaults.model.*`, `agents.defaults.bootstrapMaxChars`, etc. Authoritative list lives in `evolve_admin/config_sandbox/schema.py` (already shipped via PR #893).

**Identity keys** — preserved verbatim — include: `channels.telegram.botToken`, `gateway.auth.token`, `plugins.allow`, `plugins.entries.*.enabled` for non-evolve plugins, `mcp.servers.*`, `meta.*`, `agents.list`, `auth.order`. Anything containing a secret or a deploy-specific identifier.

A key may not span categories. If it must (a hypothetical example: a Policy default that bakes in a secret), the spec refuses it — split the key.

## 5. On-disk layout

```
{shared_dir}/sandbox/
├── overrides/
│   ├── <bot_id>.json          # per-bot Policy overrides (NEW; this spec)
│   └── …
├── provenance.json            # who/when/why for every override (exists; PR #893)
└── (sandbox-read API already shipped; PR #893)
```

`overrides/<bot_id>.json` shape:

```json
{
  "schema_version": 1,
  "bot_id": "security-bot",
  "overrides": {
    "agents.defaults.model.primary": {
      "value": "anthropic/claude-haiku-4-5",
      "set_by": "operator",
      "set_at": "2026-05-21T14:02:11Z",
      "note": "Workaround for OC#84825 isHeartbeat-lost; revert when upstream lands.",
      "expires_at": "2026-08-01",
      "needs_review": false
    },
    "plugins.entries.evolve.config.tier": {
      "value": "monitor",
      "set_by": "rsi:proposal_a1b2c3",
      "set_at": "2026-05-23T09:14:00Z",
      "note": null,
      "expires_at": null,
      "needs_review": false
    },
    "agents.defaults.bootstrapMaxChars": {
      "value": 60000,
      "set_by": "auto:drift_2026-05-24T18:00:00Z",
      "set_at": "2026-05-24T18:00:00Z",
      "note": "Auto-recorded from ad-hoc edit; review.",
      "expires_at": null,
      "needs_review": true
    }
  }
}
```

- **Keys** are ConfigPatch dotted paths (existing convention from `config_sandbox/schema.py`), scoped to `openclaw.json`.
- **`value`** must validate against the plugin schema for that key (otherwise we re-create the original bug class in a new file).
- **`set_by`** is `operator | rsi:<proposal_id> | wizard | migration:<id>`.
- **`set_at`** is ISO timestamp.
- **`note`** is freeform operator context (shown in the admin UI).
- **`expires_at`** (optional) is when the override auto-lapses back to the default. Security-Bot's haiku pin is the archetype — "until OC#84825 lands"; without it the override survives forever even after the underlying bug is fixed.
- **`needs_review`** (default false) is set true by auto-promotion and Migration A. The Customizations page highlights `needs_review = true` entries and offers Accept / Annotate / Revert. Clearing the flag is part of operator review.

Files are owned by the `evolve` user, written via temp-file + atomic rename. No `/tmp` staging needed — `{shared_dir}/sandbox/` already has the right ACL.

## 6. Regeneration pipeline (deploy-side)

`ensure_plugin_config` is renamed `materialize_openclaw_json` and rewritten to:

```
1. Read current openclaw.json (preserve Identity sections verbatim).
2. Compute the would-be Policy slice from inputs:
     a. schema_defaults    — from _PLUGIN_CONFIG_DEFAULTS + plugin configSchema
     b. pod_defaults       — from network.json (per-bot section first, then pod)
     c. per_bot_overrides  — from sandbox/overrides/<bot_id>.json
3. Diff the would-be Policy slice against the current on-disk Policy slice.
   For each key where current ≠ derived AND the current value isn't already
   represented as an override:
     a. If the current value is schema-valid: auto-promote it to an override
        with set_by = "auto:drift_<deploy_id>", note = "Auto-recorded from
        ad-hoc edit; review.", needs_review = true. Goto 2 (re-derive with
        the new override in place — converges in one extra pass).
     b. If the current value is schema-invalid: skip auto-promotion, leave
        drift in place (it'll fail OC validation in step 6), file an Alert
        Signal flagging the invalid edit.
4. Run the migration/repair passes that already exist (agents.main → agents.defaults,
   model normalization, thinkingDefault enforcement, etc.) — but only against the
   Policy slice.
5. Strip every Policy-positioned key from the current file (the slice we own
   is now authoritatively derived from inputs); compose Identity + derived
   Policy into the final document.
6. Validate the result with `openclaw config validate` (existing safe_write_bot_config).
7. Write through the existing /tmp + sudo /bin/cp path.
8. Kickstart the gateway (existing).
9. After write, emit one batched Signal per deploy: "N auto-promoted overrides
   on bot X await review." (Skipped if N == 0.) Signal resolves when the
   operator acks each override on the Customizations page.
```

Step 3 is the load-bearing addition: any ad-hoc edit becomes a `needs_review = true` override before the regeneration runs, so the materialized output preserves it. Redeploys never lose an edit; they just *record* it in the durable layer and queue it for async review. Operators are never blocked.

Step 5's "strip" is what fixes the #1525 class — once Policy keys must come from inputs (defaults + overrides) and overrides are schema-validated on write, any Policy key removed from the schema disappears from openclaw.json on next deploy.

The list of "what counts as Policy" comes from a single new registry — call it `POLICY_KEY_PATHS` — derived from `config_sandbox/schema.py`. Every key declared there is Policy by definition. Every key *not* declared there is Identity (preserved verbatim). This collapses two sources of authority into one and means adding a new Policy key requires only a schema entry.

**Async review of auto-promoted overrides.** A `needs_review = true` override behaves identically to any other override at materialization time (it's in the inputs, so the output reflects it). The flag affects only how the admin UI renders the entry: highlighted in the Customizations page with three actions —
- **Accept** → `set_by = "operator"`, `needs_review = false`, the override is now first-class.
- **Annotate** → operator adds `note:` and optional `expires_at:`, then accepts.
- **Revert** → override deleted, next deploy materializes the schema default.

The single batched Signal from step 9 is the operator's notification surface; it sweep-resolves when no overrides on the bot still carry `needs_review = true`.

## 7. L2 applier change

Today `UpdatePermissionConfigApplier` ([packages/arbiter/appliers/permissions.py:119](packages/arbiter/appliers/permissions.py:119)) writes directly to bot-side `openclaw.json` via `/tmp` + `sudo /bin/cp` + gateway kickstart. After this spec lands it writes to `{shared_dir}/sandbox/overrides/<bot_id>.json` instead, with `set_by = rsi:<proposal_id>`. The deploy pipeline (step 6) then materializes the change into `openclaw.json` on the next deploy — or the applier can call `materialize_openclaw_json` directly for the affected bot if the change must take effect immediately (still the common case).

This removes the need for `_ALLOWED_FIELDS` allow-listing inside the applier (the schema already enforces "what can be set") and dissolves the L1/L2 asymmetry the auth_drift_filler incident exposed: appliers no longer need to know that bot-side `openclaw.json` is a special ACL'd file, because they no longer write to it.

## 8. Post-pull validation (independent of layering)

Even with regeneration in place, a Signal that catches "openclaw.json validation fails for bot X" is valuable defense-in-depth — for the case where someone has hand-edited a bot's file directly, or a future schema tightening lands without a deploy. Add a post-pull hook in [packages/admin/evolve_admin/repo_puller.py](packages/admin/evolve_admin/repo_puller.py) (insertion point near line 818 per the research):

```
for bot in network.bots:
    result = run([oc_bin, "config", "validate", "--json"],
                 env={"OPENCLAW_CONFIG_PATH": bot.openclaw_json_path})
    if result.returncode != 0:
        signals.observe(
            producer="openclaw_config_validator",
            signature=f"openclaw_invalid:{bot.id}",
            severity="error",
            ...
        )
```

Surfaces in the Alerts page as a `firing` Signal until the next pull validates clean (sweep_resolve). Recommend-only; no auto-redeploy (per memory note about Phase 1 scope).

## 9. Migration plan

Two cutovers are needed; they can land in separate PRs.

**Migration A — populate overrides from current state (read-only):**

A one-shot script reconciles each bot's current `openclaw.json` against `(schema_defaults + network.json defaults)`, writes the diff into `sandbox/overrides/<bot_id>.json` with `set_by = "migration:openclaw_derived_2026_05_24"`, `needs_review = true`. After this runs, the overrides file is the authoritative record of every per-bot deviation in the pod — Security-Bot's haiku pin lands as one entry, the safeguards on team-bot-c/security-bot land as entries, and so on. **No deploy behavior changes yet.** This is purely making the implicit explicit.

Schema-removed keys (the #1525 class — `reportingEnabled: true`) are *not* written to overrides; they're skipped, since they wouldn't validate against the current schema. They get dropped silently on the first regeneration deploy (Migration B).

All other migration entries are flagged `needs_review = true`. The admin UI surfaces a "review your overrides" prompt; the operator either accepts (becomes `set_by = "operator"`), annotates with rationale + optional `expires_at`, or reverts. After review, the overrides file is clean and Migration B can proceed.

**Migration B — flip deploy to regeneration mode:**

After Migration A is reviewed and accepted, `ensure_plugin_config` is rewritten to `materialize_openclaw_json` per §6. The first redeploy of each bot rebuilds its `openclaw.json` from the inputs. The overrides file (now authoritative) plus the schema defaults plus network.json defaults produce a clean output that matches the prior state for everything the operator chose to keep, and drops everything they marked as cruft.

**Rollback:** Migration B is the irreversible step. The original `openclaw.json` from before the flip is preserved (existing `safe_write_bot_config` writes `.json.bak`); the rollback procedure is `cp .json.bak openclaw.json` and revert deploy.py.

## 10. What does NOT change

- The on-disk path of `openclaw.json` (`/Users/<bot>/.openclaw/openclaw.json`).
- The OC plugin's read path. From the gateway's perspective the file is identical, just rebuilt by a different mechanism.
- ACL setup, sudoers rules, `safe_write_bot_config`'s validation+backup+sudo-cp pattern. All of those continue to gate the final write.
- The existing config-sandbox read API ([packages/admin/evolve_admin/config_sandbox/api.py](packages/admin/evolve_admin/config_sandbox/api.py)) — `customizations()` already walks the right stores; it gains the new overrides file as the bot-side backing store and otherwise works unchanged.
- The set of writers identified in the research (setup_wizard, `_write_oc_json`, `evo/tools/deploy_integration.py`, slack `rewrite_openclaw_json_channel_keys`) all route through `materialize_openclaw_json` or stop writing entirely (the slack channel rewrite becomes "write to overrides for the channels portion" — but channels are Identity per §4, so it stays on the direct-write path; see decision §11.3).

## 11. Decisions to confirm before mechanism work

1. **Drift handling: auto-promote to override.** Spec proposes that when deploy detects an ad-hoc edit to a Policy key (current on-disk value ≠ what inputs would produce, and not already an override), the value is auto-promoted to an override with `set_by = "auto:drift_<deploy_id>"`, `needs_review = true`. Redeploys never block; the operator reviews async on the Customizations page. Schema-invalid edits are *not* auto-promoted (they'd just recreate the bug class inside the overrides file) — they file a Signal and get reverted next deploy. **Confirmed by user 2026-05-24.**

2. **Overrides-file format.** The annotated shape in §5 (value + set_by + set_at + note + expires_at + schema_version) carries enough metadata for the admin UI without becoming JSON-Schema-shaped. Alternative: minimal `{key: value}` map, with provenance entirely in `sandbox/provenance.json`. My lean: **annotated form per §5** — keeping the metadata with the value makes the file self-explanatory in `cat`, and provenance.json becomes the cross-cutting audit log (already does for #893).

3. **Identity boundary cases.** A few keys are ambiguous: `gateway.bind` (loopback vs network — Policy by my read; operators don't tune it), `agents.defaults.workspace` (deploy-specific path — Identity), `channels.telegram.streaming.mode` (Policy — has a default we'd tune), `commands.native` / `commands.nativeSkills` (Policy — defaults to "auto"). Want a quick pass with the user to ratify the §4 boundary before encoding `POLICY_KEY_PATHS`.

4. **Migration A's discovery step.** Resolved by §11.1: schema-invalid keys (the #1525 class — `reportingEnabled` everywhere) are skipped at migration time (they can't validate into the overrides file) and stripped on first regeneration; everything else lands in overrides with `needs_review = true` for async operator review. No synchronous decisions required during migration.

5. **`expires_at` enforcement.** When the timestamp passes, what happens? Three options: (a) override silently lapses, value reverts to default; (b) firing Signal until operator extends or removes the override; (c) both — Signal at T-7d, lapse at T. My lean: **(c)**, modeled on cert-expiry tooling.

6. **L2 applier cutover timing.** Switch L2 to write-to-overrides as part of Phase 3 (deploy regen) or as a separate Phase 5? My lean: **same PR as Phase 3** — they're conceptually the same change ("the durable layer for Policy keys is the overrides file, period"), and shipping them separately means RSI proposals would briefly need to re-target.

## 12. Phased rollout (live status)

Smallest PRs first; each ships independently and is safe to revert.

1. **Phase 1 — post-pull validation Signal. ✅ SHIPPED 2026-05-25.** `repo_puller.pull()` now invokes `openclaw_config_validator.validate_all_bots()` every tick (no-op pulls included), emits a `producer="openclaw_config_validator"` Signal per failing bot, sweep-resolves recovered ones. Catches the #1525 class regardless of whether the rest of this spec lands.

2. **Phase 2 — overrides file writeable. ✅ SHIPPED 2026-05-25.** `config_sandbox.overrides` exposes `write_override` / `read_bot_overrides` / `delete_override` / `mark_reviewed` / `iter_all_overrides`. Schema-validates on write. Atomic temp+rename with fcntl-locked read-modify-write. Refuses to clobber malformed or future-`schema_version` files. Mirrors provenance entries. File at `{shared_dir}/sandbox/overrides/<bot_id>.json`.

3. **Phase 3 — materializer + Migration A. ✅ NARROW SCOPE SHIPPED 2026-05-25.** Scope intentionally narrowed to the `plugins.entries.evolve.config.*` block (where the #1525 incident lived):
   - **3a:** Schema `target_path` strings fixed to match real openclaw.json layout (`plugins.entries.evolve.config.X` not `plugins.evolve.X`); added `agents.defaults.model.primary` entry.
   - **3b:** `evolve_admin.openclaw_materializer.materialize_evolve_plugin_config()` regenerates the evolve plugin config slice from `(identity from network.json) + (defaults registry) + (per-bot overrides)`. Stale-key prune, drift auto-promotion, override consumption. Wired into `deploy.ensure_plugin_config`.
   - **3c:** `evolve-admin openclaw-migrate-overrides` CLI subcommand reconciles each bot's current state into the overrides file. Idempotent; `--dry-run` available; surfaces schema-invalid drift loudly in the summary.
   - **3d:** L2 applier cutover. **DEFERRED.** `UpdatePermissionConfigApplier` writes permission-posture fields (`tools.exec.*`, `tools.web.*`, `commands.*`), which aren't currently in the sandbox schema. Cutting it over requires either widening the materialized slice to cover permission-posture or building a parallel applier track. Its own design pass.

4. **Phase 4 — admin UI for overrides. NOT STARTED.** Per-bot "Customizations" page that lists current overrides, lets the operator edit / delete / annotate / set `expires_at`. Reuses the read API from PR #893. (~200 lines.)

5. **Phase 5 — `expires_at` enforcement. NOT STARTED.** Daily cron checks overrides files; emits Signals on impending or actual expiry; deploy automatically prunes lapsed entries on next run.

## 13. Cross-references

- Triggering incident: PR [#1525](https://github.com/evolve-ops/evolve/pull/1525) ([4390d135](commit 4390d135)). Also today's broken-evo investigation (this conversation).
- Builds on: [docs/spec-config-sandbox-2026-05-09.md](docs/spec-config-sandbox-2026-05-09.md) (the cascade taxonomy + read API + provenance store).
- Aligns with: [memory: L1/L2 applier separation](https://github.com/evolve-ops/evolve/pull/1430) and [memory: generators must consider intent](feedback_generators_consider_intent.md) — overrides are *the* place a generator records "intent."
- Supersedes: the deferred per-install sandbox design ([memory: evolve_sandbox_design](project_evolve_sandbox_design.md)) for the bot-side openclaw.json portion.
- Adjacent: [docs/spec-config-intent-system-2026-05-21.md](docs/spec-config-intent-system-2026-05-21.md) — that spec's `config_intent` annotation system answers the *why* question; this spec's overrides file answers the *what* question. Both can coexist; an override entry's `note:` is one way to satisfy the config-intent system's "investigate before propose" requirement.
