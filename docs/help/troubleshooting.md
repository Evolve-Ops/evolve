---
title: "Troubleshooting"
slug: troubleshooting
audience: public
last_reviewed: 2026-07-28
concepts:
  - troubleshooting
  - logs
ui_surface: null
related_specs: []
---

# Troubleshooting

Start with the two commands that diagnose most things:

```bash
evolve-admin status              # pod summary: bots live? versions in sync?
sudo evolve-admin health         # scan OC instances, permissions, services
sudo evolve-admin health --fix   # ...and apply the non-privileged fixes
```

`health` exits 0 when everything passes (warnings allowed) and 2 when at
least one check fails, so it also works in scripts.

---

## "The wizard failed at step N"

The setup wizard is **idempotent** — every step checks before it acts, so the
fix for almost any wizard failure is: fix the underlying problem, then re-run
`sudo evolve-admin setup --fresh` and let it fast-forward through the steps
that already succeeded.

The 18 steps, so you can name where it died:

1. Pod identity
2. Bot roster
3. Security configuration
4. Admin user
5. Check prerequisites
6. Host power & sleep
7. Dedicated-host acknowledgment
8. Install OpenClaw
9. Create bot accounts
10. Set up OpenClaw per bot
11. Configure Evolve alerts
12. Set up shared directory
13. Deploy Evolve
14. Repo access (auto-update)
15. Provision primary bot OC instance
16. Verify
17. Claude Desktop integration (optional)
18. HTTPS on the LAN (for PWA install on phones)

The steps that fail most often, and the first thing to check:

- **Step 5 (prerequisites)** — `python3 --version` and `node --version`.
  Evolve needs Python 3.10+ (macOS ships 3.9) and Node 20+; on a Mac,
  `brew install python@3.12 node`.
- **Step 8 (install OpenClaw)** — `which openclaw`. If the npm install
  failed, check `npm --version` works and that npm's global bin dir is on
  `PATH`, install OpenClaw manually, then re-run the wizard (it detects the
  existing install and skips).
- **Step 14 (repo access)** — `sudo evolve-admin repo-pull` tests a pull
  directly; `sudo evolve-admin repo-pull --setup-key` re-prints the deploy
  key and the GitHub registration walkthrough (only needed for private
  origins). The wizard never blocks on this step — the puller install
  bootstraps the key as a backstop.
- **Step 16 (verify — gateway not responding)** — gateways can take a minute
  to start, especially the primary. If one stays down:
  `tail -50 /Users/<bot>/.openclaw/logs/gateway.err.log`, then
  `sudo evolve-admin health`.

---

## Where logs live

| What | Where |
|------|-------|
| Admin structured log (CLI + server) | `~/.evolve/logs/evolve-admin.log` (the `evolve` user's home on a pod) |
| Admin server daemon stdout/stderr | `/Users/evolve/.openclaw/logs/evolve-admin-ui.log` and `/Users/Shared/evolve/logs/evolve-admin-ui.err.log` |
| Per-bot gateway | `/Users/<bot>/.openclaw/logs/gateway.log` and `gateway.err.log` |
| Pod-wide background jobs | `{shared_dir}/logs/` (e.g. `/Users/Shared/evolve/logs/audit.log`, `better_engine.log`) |

On Linux pods, substitute the platform paths (`/var/lib/evolve/…`,
`/home/<bot>/…`).

---

## Common issues

**Metrics aren't being written.** Usually shared-dir permissions:
`sudo evolve-admin setup-shared` recreates the shared directory with correct
ownership and modes.

**Plugin not loading.**
`tail -50 /Users/<bot>/.openclaw/logs/gateway.err.log | grep evolve`, then
reinstall with `sudo evolve-admin deploy <bot-id>`.

**No proposals after a week.** Check that observation data exists:
`ls /Users/Shared/evolve/annotations/<bot-id>/` and
`ls /Users/Shared/evolve/metrics/`. Empty annotations means the plugin isn't
running — `sudo evolve-admin deploy <bot-id>`. (Less than a week of data is
normal: the engine reports "insufficient data" until then.)

**Proposals exist on disk but not in the dashboard.** Look at the rejected
pile for an auto-rejection reason:
`python3 -m json.tool /Users/Shared/evolve/proposals/rejected/<latest>.json | grep rejection_reason`.

---

## Pairing problems (locked out of the dashboard)

Device pairing is on by default. If no paired browser can reach the UI, get a
fresh code **on the pod itself**:

```bash
sudo evolve-admin pair           # prints a 6-digit code, valid a few minutes
evolve-admin auth status         # is pairing enforced? key present?
```

The sudo matters: the pairing key is owned by the `evolve` daemon user, and
minting or reading it without root would leave the daemon locked out. As a
last resort on a genuinely single-tenant box,
`evolve-admin auth disable --accept-risk "<why>"` records an opt-out and stops
enforcement (`auth enable` restores it).

## Port 5050 already in use

That is almost always a good sign: the admin UI is **already running as a
background service** (the `ai.evolve.evolve.admin-ui` daemon). Don't run
`evolve-admin serve` on the pod — just open `http://127.0.0.1:5050`. If you
installed the server as a user-level service instead, `evolve-admin service
status` / `service restart` manage it.

---

## Where to look in the dashboard

- **Errors page** — deduplicated runtime errors from the admin UI, background
  jobs, and gateways, with a one-click path to report a bug upstream. See
  [Help: Errors Page](errors.md).
- **Alerts page** — the pod's monitors (health, cost, audit, watchdog) file
  Signals here; a firing Signal is the "something needs attention" view.
