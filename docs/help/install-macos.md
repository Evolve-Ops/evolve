---
title: "Install Evolve on macOS"
slug: install-macos
audience: public
last_reviewed: 2026-08-26
concepts:
  - install-macos
  - installation
  - adopt-existing-bots
  - openclaw-versions
  - ssh-tunnel
  - remote-access
  - tailscale
ui_surface: null
related_specs: []
---

# Install Evolve on macOS

End-to-end setup on a dedicated Mac — a Mac mini or a retired Apple-Silicon
MacBook. This is the simplest way to run Evolve.

Before you start, make sure you've gathered the prerequisites and keys from the
**[Installing Evolve](installation.md)** hub. You need, at minimum, an LLM
provider key (Anthropic, OpenAI, Google, xAI, or an OpenAI-compatible
endpoint), a Telegram bot token, and your Telegram user ID.

**What the Mac needs:**

- macOS 14 (Sonoma) or later — Apple Silicon recommended
- [Homebrew](https://brew.sh) — installing it also installs the Xcode Command
  Line Tools (which provide `git`)
- Python 3.10+ — **the Python that ships with macOS is 3.9, which is too old.**
  `brew install python@3.12`
- Node.js 20+ — `brew install node`. Make it **22.13+** if you run OpenClaw
  2026.7 or newer, or Evolve can't read OpenClaw's schedule store and per-app
  cost stays empty for scheduled work (see
  [Installing Evolve](installation.md), *Which OpenClaw versions Evolve
  supports*)
- OpenClaw **2026.6.11 or newer**, if it's already on the machine — below that
  floor Evolve's tool-approval gate silently never fires. Tested through
  2026.7.1. The wizard installs OpenClaw for you if it isn't there
- `sudo` access on an admin account dedicated to operating the pod (this account
  should *not* run a bot)
- Remote Login enabled (System Settings → General → Sharing → Remote Login) so
  you can reach the pod over SSH

---

## Step 1 — Clone the repo and install the admin CLI

Clone the repository to the shared deploy path, create a virtual environment, and
install the command-line tool. Install the analyzer package **before** the admin
package (admin depends on it), in compat-editable mode so that modules added by
later updates import without a reinstall:

```bash
# 1. Clone to the deploy path (a real clone — see "Installing Evolve", Step 1)
sudo git clone https://github.com/evolve-ops/evolve /Users/Shared/evolve-repo

# 2. Create the virtual environment with a Python ≥3.10 (NOT the system
#    /usr/bin/python3, which is 3.9 and will fail the next step)
sudo $(brew --prefix)/bin/python3.12 -m venv /Users/Shared/evolve-venv

# 3. Install the analyzer first, then the admin CLI
sudo /Users/Shared/evolve-venv/bin/pip install -e \
  /Users/Shared/evolve-repo/packages/analyzer/ --config-settings editable_mode=compat
sudo /Users/Shared/evolve-venv/bin/pip install -e \
  /Users/Shared/evolve-repo/packages/admin/

# 4. Put evolve-admin on your PATH (the venv install alone does not) and confirm
sudo mkdir -p /usr/local/bin
sudo ln -sf /Users/Shared/evolve-venv/bin/evolve-admin /usr/local/bin/evolve-admin
evolve-admin --help
```

---

## Step 2 — Run the setup wizard

```bash
sudo evolve-admin setup --fresh
```

This is the command **whether or not the Mac already has OpenClaw bots on it**.
`--fresh` doesn't mean "wipe" — it means "set up everything Evolve needs on this
machine." If bots are already here, the wizard finds them and offers to adopt
them; if this is a bare Mac, it creates them. If you already run bots, read
[I already run OpenClaw bots](#i-already-run-openclaw-bots) below before you
start — it's the same command, but the prompts land differently.

The wizard is **idempotent and re-runnable** — every step checks before it acts,
so it's safe to re-run after fixing something. Along the way it:

1. Asks for your pod name, bot roster, and gateway ports
2. Checks prerequisites (Python, Node, OpenClaw) and installs OpenClaw if missing
3. Creates a macOS user account for each bot that doesn't already have one
4. Writes an OpenClaw configuration for each bot that doesn't already have one
5. Asks for your LLM provider key, Telegram bot token, and alert chat ID
6. Sets up the shared directory that holds pod state
7. Deploys the Evolve plugin and background jobs to every bot
8. Verifies the pod can pull its own updates (see note below)
9. Starts each bot's gateway and verifies it responds

> **About the auto-update check:** if you cloned the public repo over HTTPS
> (the command above), the wizard verifies that anonymous pulls work and moves
> on. A **deploy key** is only needed when the pod pulls from a *private*
> origin (e.g. your own fork) — in that case the wizard prints a one-time
> GitHub registration walkthrough; you can also re-run it later with
> `sudo evolve-admin repo-pull --setup-key`.

### I already run OpenClaw bots

This is the path for a Mac that has been running OpenClaw for a while — one
bot or six, with real history behind them. **You are adopting, not
re-installing.** The wizard skips anything that already exists:

| Already on the Mac | What the wizard does with it |
|---|---|
| OpenClaw itself | Leaves it — it only installs OpenClaw when it's missing |
| A macOS account a bot runs under | Leaves it |
| A bot's `openclaw.json` | Leaves it. Your models, provider keys, tools and channel config are not rewritten |
| A running gateway | Reuses the port it reads from that bot's config |

Two things *are* added: the Evolve plugin inside each adopted bot's OpenClaw
config, and a new `evo` bot on its own account, which is how you talk to Evolve
itself. `evo` sits alongside your bots — it does not take one of them over.

Step by step:

1. **See what the wizard will see.** It looks for an `openclaw.json` under each
   home directory, so this is the same list:

   ```bash
   sudo sh -c 'ls -d /Users/*/.openclaw/openclaw.json'
   ```

   A bot that doesn't show up here won't be offered for adoption. (Nothing is
   lost — you can add it afterwards with `sudo evolve-admin add-bot`.)

2. **Run the wizard** — `sudo evolve-admin setup --fresh`, exactly as above.

3. **Step 2 of the wizard, "Bot roster."** It prints the installs it found, each
   with the gateway port read from that bot's config and a suggested bot ID
   taken from the bot's gateway service. Enter `a` to adopt them all, or the
   numbers of the ones you want, or `n` for none.

4. **Name each one — and match the account.** For every install you adopt the
   wizard asks `Bot ID for user=<account>` and offers a default taken from that
   bot's gateway service label. **Give it the account name shown in the prompt.**
   Usually the default already is that name, and Enter is enough. Any Bot ID
   that isn't an existing account makes the wizard create a brand-new macOS
   account under that name instead of adopting the bot you meant.

5. **Answer the rest.** Pod name, the dedicated-host acknowledgment, an alert
   channel for Evolve's own messages, and a provider key for the new `evo` bot —
   for that last one, the wizard offers to reuse a key it already found on the
   machine, so you usually don't need a new one. Your existing bots keep using
   their own keys either way.

6. **Let the deploy finish.** For each adopted bot Evolve adds its plugin,
   creates an `evolve/` folder in that bot's workspace, registers it in the pod
   config, installs the background jobs, and **runs its first app-discovery
   scan** — reading the bot's workspace, its schedules and its standing
   instructions to work out which of them look like applications. That's why the
   Discovered queue has content the first time you open it.

7. **Go to [Quick Start](quick-start.md)** and read the *adopted bots that were
   already there* part of Day 0. The first thing worth doing is opening
   Apps → Discovered.

On a pod with several busy bots the first scans take a few minutes in total, and
if a batch runs long the remaining bots are **deferred, not skipped** — a later
deploy picks them up, a weekly sweep covers them regardless, and you can always
run one now from **Apps → Discovered → Sync all bots**.

### If your Mac is a laptop

The wizard handles laptops, but two settings are worth understanding:

- **Sleep** — the wizard detects that a MacBook ships set to sleep on AC power
  and offers to disable it. Accept the prompt. To run reliably with the lid
  closed, keep it on AC power with an external display attached, or run
  `sudo pmset disablesleep 1` (the lid-closed-on-a-shelf setup).
- **FileVault** — your call. **On** gives at-rest encryption but pauses the pod
  at the pre-boot password screen after a power-loss reboot until someone unlocks
  it. **Off** means unattended reboots come all the way back up on their own. On
  a physically secure shelf, either is reasonable.

---

## Step 3 — Verify the install and pair your browser

The admin dashboard is **already running as a background service** after the
wizard — don't start a second one; just open it and pair:

```bash
# Pod health summary
evolve-admin status

# Get a pairing code (the dashboard asks for it on first visit)
sudo evolve-admin pair

# Open the dashboard
open http://127.0.0.1:5050
```

Device pairing is on by default: the first time a browser opens the dashboard
it redirects to a pairing page, and you enter the 6-digit code that
`sudo evolve-admin pair` prints. Each device pairs once.

Then send your bot a Telegram message — you should get a reply. The first
improvement recommendations appear after about a week of data; until then the
engine reports "insufficient data," which is expected.

If you adopted existing bots, go to **Apps → Discovered** next. Evolve scanned
each bot as it deployed, and that page is where what it found is waiting.

---

## Accessing the dashboard

The admin dashboard runs at **`http://127.0.0.1:5050`** on the Mac and is bound
to loopback — it is never exposed to the network directly.

To reach it from your laptop, use one of these:

**SSH tunnel (quickest).** Forward the port over SSH and open the dashboard
locally. The same command works from a macOS, Linux, or Windows 10+ client
(Windows ships the OpenSSH client):

```bash
ssh -N -L 5050:127.0.0.1:5050 pod-admin-user@<pod-host>
# then open http://127.0.0.1:5050
```

For a tunnel that auto-reconnects across reboots and network drops, install the
CLI on your laptop instead. `evolve-admin` is **not on PyPI** — install it from
a clone of this repo:

```bash
git clone https://github.com/evolve-ops/evolve ~/evolve
pip install --user -e ~/evolve/packages/analyzer/ --config-settings editable_mode=compat
pip install --user -e ~/evolve/packages/admin/
evolve-admin connect --host <pod-host>
```

**Tailscale (recommended for everyday use).** Tailscale gives you a private
network to your pod with no port-forwarding, and it's the prerequisite for
installing the dashboard as an app on your phone. Once Tailscale is running on
the pod, enabling HTTPS is one command:

```bash
sudo evolve-admin enable-https
```

This publishes the dashboard at a `https://…ts.net` URL reachable only by your
own devices. See **[Install the app on your phone or desktop](pwa-install.md)**
for the phone-install steps that build on this.
