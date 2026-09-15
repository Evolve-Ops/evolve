---
title: "Installing with an AI agent"
slug: install-with-an-agent
audience: public
last_reviewed: 2026-07-29
concepts:
  - install-agent
  - installation
ui_surface: null
related_specs: []
---

# Installing with an AI agent

If you use Claude Code (or a similar AI coding agent), it can do most of the
mechanical work of an Evolve install for you — and get you unstuck when
something doesn't match the guide. This page tells you *and your agent* how to
split the work so the install goes smoothly.

**Point your agent at three documents:** this page, the
**[Installing Evolve](installation.md)** hub (the authority on prerequisites
and install order), and your platform guide —
**[macOS](install-macos.md)** or **[Linux / VPS](install-linux-vsp.md)**.

> **Note for the agent:** if you are running inside a clone of the Evolve
> repository, the repo's `CLAUDE.md` may auto-load into your context. That file
> is written for people *developing* Evolve — its warnings about the deploy
> checkout, dev worktrees, and pod daemons describe an already-running pod, not
> the machine you are installing onto. For an install, follow
> `docs/help/installation.md` and the platform guide, not `CLAUDE.md`.

---

## What the agent can do unattended

Everything in the platform guide's **Step 1** and the verification afterward is
ordinary shell work an agent handles well:

- Check prerequisites — platform, Python ≥ 3.10, Node ≥ 20, git, Homebrew
  (macOS) — and install what's missing
- Clone the repo to the deploy path (a real `git clone`, never a ZIP — the hub
  explains why)
- Build the bootstrap environment the platform guide describes — on macOS the
  venv plus the two `pip install -e` commands in order (analyzer first, then
  admin); on Linux `uv sync --locked` in the checkout
- Put `evolve-admin` on the PATH the way the platform guide says (macOS:
  symlink into `/usr/local/bin`) and confirm the documented wizard command runs
  with `--help`
- **After the wizard**: verify the pod with `evolve-admin status` and
  `curl -s http://localhost:5050/api/health`, and read logs to diagnose
  anything that looks off

---

## What needs a human at the terminal

Some steps can't be delegated, by design:

- **`sudo` password prompts.** Most install commands run under `sudo`; the
  password prompt is yours to answer.
- **The wizard's secret prompts.** The setup wizard asks for your LLM provider
  key and Telegram bot token with echo turned off (like a password prompt) —
  you paste and *nothing appears on screen*; a blank-looking line is normal.
  Just paste and press Enter.
- **GUI and phone steps.** macOS System Settings → General → Sharing → Remote
  Login; creating your bot with **@BotFather** and getting your user ID from
  **@userinfobot** in Telegram on your phone; grabbing your API key from the
  LLM provider's console.

---

## The recommended division of labor

1. **Agent:** run the platform guide's Step 1 (prereqs, clone, venv, installs,
   symlink) and confirm `evolve-admin --help` works.
2. **Human:** open your own terminal and run `sudo evolve-admin setup --fresh`
   yourself, answering its prompts interactively.
3. **Agent:** verify with `evolve-admin status` and
   `curl -s http://localhost:5050/api/health`, and troubleshoot from logs if
   the checks fail.

### Why the agent must not drive the wizard through a pipe

The wizard is built for an interactive terminal, and two behaviors make piping
input into it actively dangerous:

- **Secrets are read from the terminal, not stdin.** The key/token prompts use
  a no-echo terminal read (Python's `getpass`), so text piped on stdin never
  reaches them.
- **EOF exits 0, silently.** Every wizard prompt treats end-of-input the same
  as Ctrl-C: it prints a blank line and exits with status **0**. A wizard fed
  from a pipe or a closed stdin can stop partway through the install *while
  reporting success to the calling process*.

So: never judge the wizard by its exit code. An agent (or a human) confirms a
successful install only by checking `evolve-admin status` and the
`/api/health` endpoint. (The wizard does have a `--non-interactive` mode with
`--bots-manifest`, but the manifest carries no secrets — bot IDs, ports, and
roles only — so it cannot complete a fresh install that needs your API key and
bot token. The interactive wizard is the supported path.)

---

## A prompt to give your agent

Paste this into Claude Code (or your agent of choice) on the machine you're
setting up:

```text
Help me install Evolve on this machine. Read docs/help/installation.md,
docs/help/install-with-an-agent.md, and the platform guide for this OS
(docs/help/install-macos.md or docs/help/install-linux-vsp.md) in the Evolve
repo, then do Step 1 for me: check prerequisites, clone the repo to the
documented deploy path, create the venv, install the analyzer and admin
packages, and symlink evolve-admin onto the PATH. Stop before the setup
wizard — I will run `sudo evolve-admin setup --fresh` myself in my own
terminal, because it prompts for secrets and needs a real TTY. Do not pipe
input into the wizard or run it non-interactively. When I tell you the wizard
finished, verify the install with `evolve-admin status` and
`curl -s http://localhost:5050/api/health`, and help me debug from logs if
anything fails.
```

---

## Warnings

- **After the install, don't point your agent's dev work at the deploy
  checkout** (`/Users/Shared/evolve-repo` on macOS, `/var/lib/evolve/repo` on
  Linux). The pod pulls updates into that checkout on a schedule, and stray
  files an agent creates there can wedge the update. The repo's `CLAUDE.md`
  covers this rule for developers — if you want to hack on Evolve, use a
  separate clone.
- **Never paste API keys or bot tokens into the agent chat.** They would land
  in the conversation transcript. Type them into the wizard's own no-echo
  prompts, in your own terminal.
