---
title: "Install Evolve on macOS"
slug: install-macos
audience: public
last_reviewed: 2026-07-29
concepts:
  - install-macos
  - installation
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
- Node.js 20+ — `brew install node`
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

The wizard takes you from a bare Mac to a running pod in one pass. It is
**idempotent and re-runnable** — every step checks before it acts, so it's safe
to re-run after fixing something. Along the way it:

1. Asks for your pod name, bot roster, and gateway ports
2. Checks prerequisites (Python, Node, OpenClaw) and installs OpenClaw if missing
3. Creates a macOS user account per bot
4. Writes each bot's OpenClaw configuration
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

Use `--fresh` for a brand-new machine (it creates the bot accounts). If you
already have OpenClaw installed and just want to add Evolve to existing bots, run
`sudo evolve-admin setup` without `--fresh`.

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
