# Incident — app-cron launchd jobs can't find `openclaw` (exit 127, silent non-delivery)

**Date:** 2026-06-22
**Aspect:** META:apps
**Severity:** Pod-wide (every bot app-cron that shells out to `openclaw`)
**Status:** Generator fix + self-heal CLI in this PR; live re-heal operator-gated

---

## Summary

Atlas's "Daily Digest" cron fired daily at 07:00 but delivered nothing for weeks.
The launchd job is healthy and the helper script was substituted correctly (the
#2976 fix). The failure is one layer down: the digest's classifier shells out to
`openclaw`, which lives at **`/opt/homebrew/bin/openclaw`** (Apple Silicon
Homebrew), but launchd hands the job a **minimal PATH** —
`/usr/bin:/bin:/usr/sbin:/sbin` — that **excludes `/opt/homebrew/bin`**. So every
`openclaw` call returns **exit 127 (command not found)**, every article is
skipped, the digest is empty, and nothing posts. The wrapper still exits 0 on an
empty digest, so it looks healthy.

```
[atlas:oc_dispatch] openclaw exit=127 (command not found)
[atlas:classifier] dispatch failed: openclaw exit=127
DIGEST_EMPTY: 2026-06-22 all_skipped
```

This is the **same silent-failure family** as #2976 (then: unsubstituted
placeholders; now: `openclaw` off the launchd PATH), through a different door.

## Blast radius — pod-wide

Every installed **infra** LaunchDaemon (`ai.evolve.evolve.*`) sets
`EnvironmentVariables.PATH` (setup_wizard). Every installed **app cron**
(`ai.evolve.<bot>.<app>`, via `install_launchd_command_action`) did **not**:

| Plist | EnvironmentVariables |
|---|---|
| `ai.evolve.atlas.atlas-daily-digest` | **absent** |
| `ai.evolve.ledger.morning-briefing` | **absent** |
| `ai.evolve.ledger.note-taker` | **absent** |
| `ai.evolve.ledger.calendar-sync` | **absent** |
| `ai.evolve.evolve.*` (all infra) | present (`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`) |

Any app cron that shells out to `openclaw`/`node` is affected. `morning-briefing`
and `note-taker` are LLM apps that do exactly that.

## Root cause (file:line)

`install_launchd_command_action`
([`install_helpers.py`](packages/admin/evolve_admin/applications/install_helpers.py))
passed `env=None` to the plist renderer when the manifest action declared no
`install.env` → no `EnvironmentVariables` block → launchd's minimal default PATH.

## Fix (this PR)

1. **Generator** — `install_launchd_command_action` now always injects a PATH
   that finds openclaw/node (`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`,
   matching the infra jobs), merged with any app-supplied PATH
   (`_ensure_launchd_openclaw_path`). Every future install / re-forge is fixed.
2. **Self-heal** — `repair_app_cron_env_paths(members)` re-installs each
   installed launchd app-cron plist that lacks a PATH (idempotent: a plist that
   already has one is skipped). Exposed as `evolve-admin application
   repair-app-crons [--bot X] [--check]`.

## Operator remediation

```
sudo evolve-admin application repair-app-crons --check     # report fleet-wide
sudo evolve-admin application repair-app-crons             # heal all members
```
For atlas specifically this re-installs `ai.evolve.atlas.atlas-daily-digest` with
a working PATH; the next 07:00 run (or a manual `kickstart`) can then reach
openclaw. NOTE: atlas's scan also errors on a missing Anthropic key (separate OC
auth-profiles issue) — even with PATH fixed, the classifier needs a reachable key
to actually classify+deliver.

## Not in this PR (follow-ups)

- **Silent masking** — a digest where 100% of articles failed on a *tool error*
  exits 0 with no signal. A "tool-unavailable / all-skipped" run should fail
  loudly or raise an alert. This lives partly in the bot's own script and partly
  in the delivery monitor (the manifest's `outputs: []` / missing delivery
  contract leaves the monitor blind — the #2976/#2984 gap, still open for atlas).
- **Deploy auto-heal** — wiring `repair_app_cron_env_paths` into `deploy_bot`
  (deferred to avoid churn in the no-growth-capped deploy.py).
