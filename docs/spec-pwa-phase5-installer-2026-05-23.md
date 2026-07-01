# PWA Phase 5 — In-PWA Setup Wizard Sub-Spec

**Date:** 2026-05-23
**Status:** Resolved (open questions closed by recommended defaults)
**Parent:** [spec-pwa-2026-05-18.md](spec-pwa-2026-05-18.md) §9
**Owner:** Evolve admin server + new bootstrap installer

---

## 1. Why this sub-spec

The parent PWA spec deferred Phase 5 (setup wizard inside the PWA) because Phase 3's terminal removed the immediate pain of "I have to SSH to my mini to run anything." That logic held while Evolve was a private tool. Two things have changed:

1. **The PWA shipped.** Day-1 install-on-phone is now a real, polished experience. The weakest link in the user journey is the moment *before* the PWA exists — first-run setup on a fresh mini, which still requires SSH + terminal commands + a multi-minute interactive CLI wizard.
2. **Public launch is being prepared** (per the public-launch cleanup project). First-impression UX is now load-bearing in a way it wasn't before. A Plex-test-capable operator who buys a Mac mini, installs Evolve, and walks away with a working pod in 15 minutes is the goal.

Today's gap: even after running `sudo evolve-admin setup`, the operator has been through ~30 questions in a terminal. That's the gap this phase closes.

---

## 2. Current state

### How setup works today (post-§4.1.c)

1. Operator gets a Mac mini, installs macOS, creates the `pod-admin` user account.
2. Operator installs Tailscale on the mini and signs in.
3. Operator SSHes from their laptop to the mini as `pod-admin`.
4. Operator clones the Evolve repo to `/Users/Shared/evolve-repo` and installs dependencies.
5. Operator runs `sudo evolve-admin setup` — a multi-step interactive terminal wizard:
   - Pod identity (`networkId`, display name)
   - Primary user (operator name, Telegram chat ID for alerts)
   - Tailscale hostname detection
   - HTTPS setup via Tailscale serve (from §4.1.c)
   - First bot creation (name, integrations)
   - Sysadmin alert subscription defaults
6. Wizard creates the `evolve` service user, installs LaunchDaemons, configures sudoers, deploys the first bot.
7. Admin server starts on `:5050` (or `:443` via `tailscale serve`).
8. Operator opens the URL in a browser and proceeds.

Steps 3–5 are the weakest UX surface. Terminal flow runs ~30 prompts; one typo or connection drop resets state; no visual feedback; intimidating for the target audience.

### What the PWA already enables

- HTTPS-by-default on a per-pod URL with a valid Let's Encrypt cert via Tailscale serve (§4.1)
- Installable PWA on every device (Phase 1)
- Embedded mini terminal (Phase 3) — covers "I want to run one command on the mini" without re-SSH

The terminal closes the post-setup remote-management gap. Phase 5 closes the pre-setup gap.

---

## 3. Architecture choice

Three options were on the table:

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **A: Standalone installer binary** | Separate Python/Rust binary, ships independently, hosts its own setup web server during install, exits when handoff complete | Cleanly separate from running admin server; can ship via Homebrew | New artifact to build/sign/distribute; duplicates a lot of `evolve-admin` logic |
| **B (recommended): Integrate into `sudo evolve-admin setup`** | Same CLI command operators already run; when invoked without `--terminal`, spawns a local web server hosting the wizard SPA, prints a URL the operator opens in a laptop browser | Reuses existing entry point; no new artifact; CLI fallback is one flag away; minimal new packaging | The `evolve-admin` package now has to know how to host a web wizard pre-install — adds dependency weight to the bootstrap |
| **C: Hybrid** | Ship a one-line bootstrap script that fetches Evolve, then runs (B) | Best end-user UX (one curl + bash from a fresh macOS) | Two artifacts (bootstrap script + integrated wizard) |

**Recommendation: C** — Option B's integrated wizard plus a tiny one-line bootstrap script. The bootstrap solves "fresh macOS → ready to setup"; the integrated wizard solves "ready to setup → running pod."

Concretely, the user-facing first-install becomes:

```
# In a Terminal window on the mini:
curl -fsSL https://evolve.app/install.sh | sudo bash
```

That script:
1. Verifies macOS + Tailscale prerequisites (refuses with clear message if missing)
2. Clones the Evolve repo to `/Users/Shared/evolve-repo`
3. Installs Python dependencies
4. Invokes `sudo evolve-admin setup` (the integrated wizard's entry point)

`evolve-admin setup` (default behavior, post-Phase-5) spawns the web wizard and prints:

```
Setup wizard is ready.
Open this URL in your laptop browser (must be on the same Tailscale tailnet):
  http://<host>.<tailnet>.ts.net:5051

Or run with --terminal to use the existing CLI wizard.
Press Ctrl-C to cancel.
```

Operator opens the URL on their laptop, walks through the web wizard, completes setup. The CLI command (running on the mini) handles handoff, restarts services, and exits.

---

## 4. Wizard steps mapped from CLI to web

Each current CLI prompt becomes a wizard page. Pages share a header (progress dots + back button) and content area. Targets ~6 steps, each ~30 seconds of operator attention.

| Step | Page title | What it collects | Default |
|---|---|---|---|
| 1 | **Welcome** | Nothing — just orientation: "You're setting up your Evolve pod. ~5 minutes." | — |
| 2 | **Name your pod** | `networkId` (URL slug) + display name (human-readable). Both editable later. | `networkId` defaults to hostname-derived; display name defaults to "<Hostname> Pod" |
| 3 | **Primary user** | Operator's name + Telegram chat ID (where alerts arrive) | Asks via QR code or "open Telegram → chat with @EvolveBot → forward chat ID" |
| 4 | **Network** | Confirms Tailscale hostname (auto-detected) + HTTPS enablement (per §4.1.c flow) | Auto-enable HTTPS if Tailscale toggle is on; defer with clear note otherwise |
| 5 | **First bot** | Name your first bot + pick its primary integration (Slack / Discord / Telegram / none) | Name suggestion based on pod display name; integration left blank (configurable later) |
| 6 | **Alerts** | Default subscriptions: which event types should fan to chat | Sensible defaults from the alert-subscriptions catalog (security on, cost daily-threshold on, etc.) |
| 7 | **Finish** | Summary of choices, "Create pod" button | — |

Page-level rules:
- Each page validates inline; "Next" disabled until valid
- Back button is always available (state persists across navigation)
- Skip is available for non-required steps (5, 6) — operator can configure these later
- Step 4 fails gracefully if Tailscale isn't ready; operator gets a "skip HTTPS for now" path that mirrors §4.1.c's defer-not-block behavior

---

## 5. Bootstrap script (`evolve.app/install.sh`)

A small, audit-able shell script — under 200 lines. Hosted at `evolve.app/install.sh` (or wherever the public domain ends up). The user runs it once on a fresh mini.

Responsibilities:
- Check macOS version (refuse pre-Sonoma)
- Check Tailscale installed + signed in (refuse with link to install instructions if not)
- Check `pod-admin` (or whatever user it's run as) has sudo
- Verify Python 3.12+ available; install via Homebrew if missing
- Verify Homebrew installed; install if missing (with explicit prompt)
- `git clone https://github.com/<org>/evolve.git /Users/Shared/evolve-repo`
- `cd /Users/Shared/evolve-repo && pip install -e packages/admin`
- `sudo evolve-admin setup` (which is now the integrated wizard)

Script is idempotent — re-running on a partially-installed system picks up where it left off. Each step prints a clear "✓ done" or "→ doing" so the operator can follow along.

**Audit-ability:** the install.sh is short enough that a tech-capable operator can read it in 30 seconds before piping to bash. Includes a `--dry-run` flag that prints what it would do without executing.

---

## 6. Handoff to main admin server

When the operator clicks "Create pod" on step 7:

1. Wizard backend writes all config files atomically (temp+rename per CLAUDE.md):
   - `network.json` with collected fields
   - `subscriptions.json` from alert defaults
   - Bot config skeleton at `/Users/<bot>/.openclaw/`
2. Wizard backend invokes existing `evolve-admin` post-setup logic:
   - Create `evolve` service user (if not exists)
   - Install LaunchDaemons (admin server, repo puller, etc.)
   - Configure sudoers
   - Deploy first bot
3. Wizard backend polls `https://<host>.<tailnet>.ts.net/api/health` until 200 (or timeout 60s)
4. On 200: wizard browser tab gets a JS redirect to `https://<host>.<tailnet>.ts.net/`
5. On timeout: wizard backend surfaces the most-recent log line + a "View setup logs" button that streams `/var/log/evolve-setup.log` (or wherever the bootstrap logs land)
6. The wizard server itself (on `:5051`) shuts down once handoff succeeds

The operator's experience: click "Create pod" → see "Setting up your pod…" with a progress indicator and live log tail → ~10–60 seconds later, browser redirects to the real admin UI in their pod.

---

## 7. CLI fallback (`--terminal`)

Keep `sudo evolve-admin setup --terminal` as the documented escape hatch for:
- Headless installs (CI, scripts, automated deployment)
- Sysadmins who genuinely prefer terminal flows
- Debugging when the web wizard itself misbehaves

The CLI wizard is the existing code path — don't delete it. The flag flips the default; `--terminal` opts out of the web flow.

In the future: if real telemetry shows nobody uses `--terminal` after launch, deprecate. Until then, keep.

---

## 8. Resolved decisions

All architectural questions closed below. No open questions.

**8.1 Architecture: Option C** — bootstrap script + integrated wizard via `evolve-admin setup`. See §3.

**8.2 HTTP vs HTTPS for the wizard server itself: HTTP.**
The wizard runs on `:5051` over the tailnet for ~10 minutes during setup. HTTPS would require a cert; cert requires Tailscale already set up; that's chicken-and-egg with step 4 ("Network") of the wizard itself. The tailnet's privacy is sufficient gate. Production traffic (post-handoff) is HTTPS via Tailscale serve.

**8.3 Port: 5051.** Sibling to `5050` (main admin). Avoid privileged ports.

**8.4 Authentication on the wizard server: none.**
Same logic as the admin UI's "auth is presumed" pillar (per memory). Whoever's on the tailnet and can reach the URL is presumed authorized for setup. Initial install is a single-shot, time-bounded operation; no per-page auth.

**8.5 Frontend tech: vanilla JS.** Matches the rest of the admin UI. No new framework dependency in the bootstrap path.

**8.6 Wizard state persistence: on-disk after each step.**
After each "Next" click, the wizard server writes a `/tmp/evolve-setup-state.json` (or in `/var/lib/evolve/`). Reload, refresh, or browser crash recovers to the most-recent completed step. On successful handoff, the state file is deleted.

**8.7 CLI fallback: keep `--terminal` flag.** See §7.

**8.8 Bot creation in step 5: minimal.**
The wizard only collects bot name + primary integration. Detailed bot config (deeper integrations, MEMORY.md, AGENTS.md tuning) happens in the running admin UI, not the setup wizard. Keeps the wizard short.

**8.9 Telegram chat-ID flow in step 3:**
Two paths, operator picks:
- **A: Forward chat ID** — instructions: "Open Telegram, message @userinfobot, copy your chat ID, paste here."
- **B: QR-code-from-bot** — operator scans a QR code with their phone that opens Telegram and starts a chat with the Evolve setup bot, which then pings the wizard with the operator's chat ID.

A is simpler to implement; B is friendlier. Build A in this phase; B as a follow-up if friction shows up in real installs.

**8.10 First-run discovery (how does the user know to open `:5051`?):**
The `evolve-admin setup` CLI command (running on the mini) prints the URL in its terminal output. The user is at that terminal because they just ran the install command, so they see the URL. No discovery problem.

For installs initiated remotely (e.g. via `ssh mini "sudo evolve-admin setup"` from the laptop), the URL prints to the SSH session and the operator copy-pastes into their laptop browser. Same flow.

---

## 9. Out of scope

- **Migrating an existing pod to a new mini.** That's a restore/migration tool, not setup.
- **Onboarding new bots to an already-running pod.** That's the conversational bot-creation wizard from the user memory (deferred until v1.1 substrate work) — different surface, different audience.
- **Re-running setup on an already-configured pod.** A separate `evolve-admin reconfigure` command would handle that; not in this phase.
- **Headless / unattended install** (no operator interaction at all). Possible via `evolve-admin setup --config <file>` reading a pre-baked config, but not in this phase.
- **Multi-pod federation.** Setup creates one pod; federation is a v2 product.
- **Bootstrap script for non-Mac platforms** (Linux, Windows). Mac mini is the target hardware per project memory.
- **Auto-update of the bootstrap script itself.** It's a one-time tool; if it has a bug, ship a new version at the same URL.

---

## 10. Phased delivery

| Phase | Work | Estimate |
|---|---|---|
| **5.a** | Wizard SPA scaffolding — pages 1–7 with placeholder content, navigation, state persistence | 3–4 days |
| **5.b** | Wire wizard pages to existing config-write logic (reuse what's in `setup_wizard.py` today) | 3–4 days |
| **5.c** | Bootstrap script `install.sh` + hosting at `evolve.app/install.sh` (or wherever the public domain ends up) | 1–2 days |
| **5.d** | Handoff flow: post-setup invocation, live log tail, browser redirect | 2 days |
| **5.e** | Cross-browser + cross-device verification (laptop browsers + iPad in landscape — operators might walk through setup on iPad) | 1 day |
| **5.f** | Docs: update README + getting-started with the new "fresh install" story | 1 day |

**Total: ~12–15 days.** Two weeks of focused work.

Sequencing: 5.a–5.b in parallel (different layers); 5.c–5.d after 5.b; 5.e + 5.f at the end.

---

## 11. Acceptance criteria

For Phase 5 to be "done":

- A fresh Mac mini with macOS + Tailscale installed can run `curl -fsSL https://evolve.app/install.sh | sudo bash` and be at a working PWA-installable pod within 15 minutes of operator attention.
- The wizard runs to completion in any common laptop browser (Chrome, Safari, Firefox) at desktop and tablet widths.
- The wizard is internally idempotent — refresh, browser crash, or SSH-session drop doesn't lose more than the current step's input.
- Errors at any step surface a clear "what went wrong" message + a "retry" path that doesn't require restarting from step 1.
- CLI fallback (`--terminal`) still works for headless/scripted use.
- The bootstrap script is auditable (under 200 lines, dry-run flag).

---

## 12. Why this shape

- **Bootstrap script + integrated wizard** is the right unit of work — addresses both the "I haven't downloaded Evolve yet" and "I've downloaded but not configured" pain points. Skipping the bootstrap leaves operators still typing git clone commands.
- **Reusing `evolve-admin setup` as the entry point** avoids the multi-binary distribution problem and the CLI fallback comes for free.
- **HTTP over tailnet** for the wizard is correct — HTTPS would require solving cert provisioning *before* configuring Tailscale, which is the chicken-egg. The tailnet's privacy is sufficient gate for a 10-minute, one-time setup flow.
- **Vanilla JS, no framework** keeps the bootstrap path light. A React/Vue dependency would 2x the install time and add a new audit surface.
- **State on disk after each step** matches what operators expect from modern installers (you can close the window and come back).
- **CLI fallback retained** because "deprecate everything immediately" is hostile to power users; real deprecation needs telemetry showing nobody uses it.
- **6 wizard steps, ~5 minutes** is the target. More steps = abandonment risk; fewer = pre-configuration steps spill into the admin UI's post-setup experience (which is fine for advanced tuning but should not be needed to get a working pod).
