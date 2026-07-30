---
title: "Install Evolve on Linux (Ubuntu / VPS)"
slug: install-linux-vsp
audience: public
last_reviewed: 2026-07-29
concepts:
  - install-linux
  - installation
  - vps
  - ssh-tunnel
  - remote-access
ui_surface: null
related_specs: []
---

# Install Evolve on Linux (Ubuntu / VPS)

End-to-end setup on a dedicated, always-on Linux box — a cloud VPS or a
home server running Ubuntu/Debian. The setup wizard is the same as on macOS, but
Linux has a handful of platform-specific landmines that pass the wizard and only
bite later. This guide calls each one out so you can verify it now rather than
discover it in production.

Gather your prerequisites and keys from the **[Installing Evolve](installation.md)**
hub first.

---

## The Linux layout (don't fight it)

Evolve uses fixed paths on Linux. The most common single mistake is assuming the
macOS layout — bot homes live under `/home/<bot>`, **never** `/Users/<bot>`.

| Path | What it is |
|---|---|
| `/var/lib/evolve` | Shared directory — pod-wide state |
| `/var/lib/evolve/repo` | The deploy checkout (a **child** of the shared directory — this matters) |
| `/var/lib/evolve-venv` | The virtual environment whose Python runs Evolve |
| `/var/lib/evolve-plugin` | The built OpenClaw plugin |
| `/etc/systemd/system` | Daemon units — managed with `systemctl`, not `launchctl` |
| `/home/<bot>` | Bot home directories |

---

## Phase 0 — Host prerequisites

| Requirement | Why it matters |
|---|---|
| Dedicated, **always-on** VPS (Ubuntu 24.04 LTS recommended) | The wizard assumes the box won't sleep or reboot out from under the gateways. |
| **root or full-sudo** login | The wizard creates users, writes systemd units, and installs sudoers drop-ins. |
| **The `acl` package** (`sudo apt-get install -y acl`) | `setfacl`/`getfacl` *are* the Linux permission model. It isn't on minimal Ubuntu, and **without it every permission grant silently does nothing.** Install it first. |
| Python ≥3.10, Node **≥24** (NodeSource), git, `ca-certificates` | Runtime, plugin build, and update pulls. The wizard's prereq check enforces Node 24+ on Linux. |
| **Only inbound port 22 open** | The admin UI binds to localhost — reach it over an SSH tunnel (see the end of this guide). **Do not expose it publicly.** |

> On stock Ubuntu 24.04, a bare `pip install` is refused with
> `externally-managed-environment`. Install `uv` with the standalone installer
> (`curl -LsSf https://astral.sh/uv/install.sh | sh`) rather than fighting pip.

---

## Phase 1 — Clone the repo (as the `evolve` user) and bootstrap the CLI

On Linux the deploy checkout must be a real clone **owned by a dedicated
`evolve` service account** — the account the auto-updater later pulls as.
Cloning as root, or into your own home, puts the checkout in the wrong place
and the updater can't use it.

```bash
# 1. Create the service account and the shared directory
sudo useradd -m -r evolve
sudo mkdir -p /var/lib/evolve
sudo chown evolve /var/lib/evolve

# 2. Clone the public repo over HTTPS, as evolve
sudo -u evolve git clone https://github.com/evolve-ops/evolve /var/lib/evolve/repo
```

Anonymous HTTPS pulls work for the public repo, so **no deploy key is needed**.
If your pod pulls from a *private* origin (e.g. your own fork), clone over SSH
instead and register a **read-only deploy key**: generate an SSH key as
`evolve` (e.g. `/home/evolve/.ssh/evolve-repo`), add the public key on GitHub
as a deploy key with "Allow write access" **unchecked**, point `evolve`'s
`~/.ssh/config` at it, and verify with
`sudo -u evolve ssh -T git@github.com` ("successfully authenticated"). The
setup wizard re-checks this and prints the walkthrough if it's missing.

Then bootstrap a Python environment just good enough to run the wizard
(the wizard builds the *real* runtime venv itself in Phase 2):

```bash
# uv, via the standalone installer — stock Ubuntu's pip is externally-managed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Bootstrap env at /var/lib/evolve/repo/.venv (gitignored, so it can't dirty
# the checkout)
cd /var/lib/evolve/repo
sudo $HOME/.local/bin/uv sync --locked
```

You do **not** build the plugin by hand — the wizard's deploy step compiles it
into `/var/lib/evolve-plugin`. (If you ever build manually in the checkout, use
`npm ci`, never `npm install` — see landmine 3.)

---

## Phase 2 — Run the setup wizard

Run the wizard from the bootstrap venv you just built:

```bash
sudo /var/lib/evolve/repo/.venv/bin/evolve-admin setup --fresh
```

The wizard is the same idempotent 18-step flow as on macOS — it creates bot
accounts under `/home/<bot>`, asks for your LLM provider key, Telegram token, and
chat ID, sets up the shared directory and its permissions, installs the systemd
units, and provisions the primary bot. During its deploy step it **builds the
canonical runtime venv at `/var/lib/evolve-venv`** (the interpreter every
daemon uses) and shims `evolve-admin` onto `/usr/local/bin` — so after setup
completes, plain `evolve-admin …` works from any shell.

After it finishes, the admin server and the MCP bridge run continuously; **every
other unit is timer-triggered**, so seeing many `ai.evolve.*` units listed as
`inactive (dead)` is normal — they fire on a schedule, not continuously.

---

## Phase 3 — Verify the Linux landmines

These five checks each correspond to a real failure that passes the wizard but
breaks later. A genuinely clean install has none of them lurking — run each one.

> ⚠️ **1. ACL mask clobber (the big one).** On Linux, `chmod` recalculates the
> POSIX ACL mask. Any hardening `chmod` on a `.openclaw` directory can clamp the
> `evolve` account's access to nothing — silently, pod-wide. Verify `evolve`
> still has effective read/traverse on every bot:
>
> ```bash
> getfacl /home/*/.openclaw 2>/dev/null | grep -E 'user:evolve|mask'
> #  want:  user:evolve:r-x  and  mask::r-x
> #  bad:   #effective:---   or   mask::---
> ```

> ⚠️ **2. Nested deploy checkout → frozen fleet.** Because `/var/lib/evolve/repo`
> sits *inside* the shared directory, a recursive permission pass over the shared
> directory can flip the checkout's git file modes and wedge the next update
> pull — freezing your pod on stale code. Pin the guard and confirm the checkout
> is clean:
>
> ```bash
> git -C /var/lib/evolve/repo config core.fileMode      # expect: false
> sudo -u evolve git -C /var/lib/evolve/repo status      # expect: clean
> ```

> ⚠️ **3. `npm ci`, never `npm install`.** Building the plugin with
> `npm install` rewrites `package-lock.json`, which dirties the deploy checkout
> and wedges the next update pull — the same freeze as landmine 2, different
> trigger. Always use `npm ci`.

> ⚠️ **4. Secrets re-widened by a recursive permission pass.** A recursive
> `chmod -R a+rX` over the shared directory makes every secret world-readable
> (this is how an API key once leaked). The tree is re-tightened automatically,
> but confirm nothing token-bearing is world-readable:
>
> ```bash
> sudo find /var/lib/evolve -name '*.json' -perm -o+r \
>   \( -name 'openclaw.json' -o -name 'auth-profiles.json' -o -path '*keystore*' \)
> #  expect: no output
> ```

> ⚠️ **5. Python 3.12 changes how missing files report.** On Python 3.12, a file
> existence check under a locked-down (`0700`) parent *raises* an error instead of
> returning "not found" as it did on 3.11. This is already guarded in current
> code, but it's the failure family to recognize if a daemon dies with a bare
> permission error from a file probe.

---

## Phase 4 — Known unresolved gap (read before declaring "done")

Be honest with yourself about one current limitation on Linux:

> 🚧 **The `evolve` service account has no `openclaw.json` on Linux.**
>
> On Linux the bot gateway runs under a separate `evo` account, so the `evolve`
> service account is provisioned without its own OpenClaw config file. The
> consequence, confirmed on a live box: the built-in **security CVE-scan app is
> quarantined and cannot run**. The wizard correctly reports this as *deferred*
> rather than failed — it does not crash the install.
>
> This is a known limitation tracked by the maintainers; **it is not yet fixed in
> code.** Until it is, a Linux pod either runs the CVE scan dark, or you apply one
> manual remediation: provision `/home/evolve/.openclaw/openclaw.json` (a headless
> config), set it `evolve`-owned and `0600`, re-assert the `evolve` read-ACL (see
> landmine 1), then re-run `sudo evolve-admin install-infra-jobs`.

Detect the gap:

```bash
ls -l /home/evolve/.openclaw/openclaw.json 2>&1   # "No such file" on a current Linux pod
```

macOS is unaffected — there the `evolve` account *is* the gateway account, so the
file exists.

---

## Accessing the dashboard over an SSH tunnel

The admin UI binds to localhost on the VPS and is never exposed publicly. Reach
it by forwarding the port over your existing SSH connection:

```bash
ssh -N -L 5050:127.0.0.1:5050 <user>@<vps-host>
# then open http://127.0.0.1:5050 in your browser
```

The first visit asks for a **pairing code** — run `sudo evolve-admin pair` on
the VPS and enter the 6-digit code it prints. Each device pairs once.

Once it's up, walk the Alerts / Reports page to confirm there are no firing
signals. For a private, persistent path from your laptop and phone — and to
install the dashboard as an app — set up [Tailscale and HTTPS](pwa-install.md).
