# Runbook — fresh Evolve install on a Linux/VPS (end-to-end)

**Who runs this:** the pod operator, standing up a brand-new Evolve pod on a
dedicated, always-on Linux VPS (Ubuntu/Debian, merged-`/usr`).

**What this is.** The single end-to-end path from a bare VPS to a running pod,
written *after* the Linux port (W10-A…G) reached GA and a fresh install runs
clean. It ties together the pieces that live in separate docs and — more
importantly — collects the **Linux-specific landmines** that passed the wizard
but broke later during the port, plus the **one known-unresolved gap** so an
operator knows what to verify rather than discovering it in production.

**What this is NOT.** Not the macOS path (the wizard is byte-identical there and
needs none of the FHS/ACL caveats below). Not the disposable-VM harness pass —
that's [runbook-linux-vm-pass-2026-06-11.md](runbook-linux-vm-pass-2026-06-11.md),
which this runbook reuses for the credentialed-clone bootstrap (§2a there).

**Ground truth** for the path/owner facts below: the LINUX profile in
`packages/analyzer/platform_profile.py`, the deploy spec
[spec-deploy-meta-2026-06-14.md](spec-deploy-meta-2026-06-14.md), and a live
Ubuntu 24.04 pod (Python 3.12, Node 24, OpenClaw 2026.6.x, daemons as systemd
units) confirmed 2026-06-23.

---

## The Linux layout (don't fight it)

Every path below is the LINUX `PlatformProfile` default. Derive from the profile
in code; never hardcode a sibling.

```
/var/lib/evolve            shared_dir        (evolve:root, sticky 1777)
/var/lib/evolve/repo       deploy checkout   (read-only to daemons; owned evolve:staff)
/var/lib/evolve-venv       venv whose python has evolve_admin + analyzer installed
/var/lib/evolve-plugin     built OpenClaw plugin (openclaw loads the plugin from here)
/etc/systemd/system        daemon units      (systemctl — NOT launchctl)
/home/<acct>               bot homes         (NEVER /Users/<acct> — the #1 port bug)
```

Two macOS→Linux divergences that bite if assumed away:

- **The deploy checkout is a _child_ of shared_dir** (`/var/lib/evolve/repo`),
  whereas on macOS it's a sibling (`/Users/Shared/evolve-repo`). Any recursive
  pass over shared_dir touches the checkout. See §4.2.
- **`chown` is `/usr/bin/chown`**, `lsof` is `/usr/bin/lsof` (macOS uses
  `/usr/sbin`), and the admin group is **`root`** (gid 0), not `wheel`. The
  profile encodes all of this; sudoers is rendered from it.

---

## Phase 0 — Host prerequisites

| Requirement | Why it matters |
|---|---|
| Dedicated, **always-on** VPS (Ubuntu 24.04 LTS recommended) | The dedicated-host + power-posture wizard steps assume the box won't sleep/reboot under the gateways. |
| **root or full-sudo** login | The wizard creates users, writes systemd units, installs sudoers drop-ins. |
| **`acl` package installed** (`apt-get install -y acl`) | `setfacl`/`getfacl` ARE the Linux permission model — there is no macOS ACL fallback. Not present on minimal Ubuntu; **without it every ACL grant silently no-ops.** |
| Python ≥3.10 (3.12 fine), Node ≥20, git, `ca-certificates` | Runtime + plugin build + deploy pull. |
| Inbound 22 only | The admin UI binds locally (5050 / 19099) — reach it over an SSH tunnel. **Do not expose it publicly.** |

PEP 668 wrinkle: stock Ubuntu 24.04 refuses a bare `pip install uv`
(`externally-managed-environment`). Use the standalone installer
(`curl -LsSf https://astral.sh/uv/install.sh | sh`) or
`pip install --user --break-system-packages uv`.

---

## Phase 1 — Stage the code + Python env

The invariant that makes auto-update "just work": **one read-only deploy key,
owned by `evolve`, used for both the first clone and the puller.** Cloning as
root or via a personal `gh` token puts the credential in the wrong home and the
puller can't reuse it.

**Do the full credentialed-clone bootstrap from
[runbook-linux-vm-pass-2026-06-11.md §2a](runbook-linux-vm-pass-2026-06-11.md)** —
it is the authoritative, copy-pasteable sequence:

1. `useradd -m -r evolve` (the wizard also does this; a from-scratch clone needs
   it first).
2. Generate the deploy key **as `evolve`** (`/home/evolve/.ssh/evolve-repo`) —
   exactly what `repo_puller.ensure_deploy_key()` would generate.
3. Add the `.pub` to GitHub as a **READ-ONLY** deploy key ("Allow write access"
   UNCHECKED).
4. Point `evolve`'s `~/.ssh/config` at the key.
5. Clone **as `evolve`** into `/var/lib/evolve/repo` (the profile's
   `deploy_checkout_default`).
6. Verify: `sudo -u evolve ssh -T git@github.com` then `sudo evolve-admin repo-pull`.

Then create the venv and install both workspace packages, plus build the plugin:

```bash
cd /var/lib/evolve/repo
uv sync --locked            # resolves uv.lock for packages/admin + packages/analyzer + dev group
# Plugin build → /var/lib/evolve-plugin :  npm ci   (NEVER npm install — see §4.3)
```

> **Compat-editable is mandatory.** The analyzer is a real package since 6.1 and
> the daemons import it as installed. A non-editable install — or a path that
> lets the stdlib `profile` module shadow the analyzer package — breaks imports
> at runtime, not install time. The interpreter every `sudo` subprocess uses
> must be `/var/lib/evolve-venv/bin/python3`.

---

## Phase 2 — Run the wizard

```bash
EVOLVE_PLATFORM=linux /var/lib/evolve-venv/bin/evolve-admin setup --fresh
```

`sys.platform` already selects the LINUX profile on a Linux box;
`EVOLVE_PLATFORM=linux` is belt-and-suspenders (and what CI/e2e uses). The
wizard is idempotent — re-running it after a fix is safe.

The real 17-step flow (`setup_wizard.py`), with the Linux-relevant notes:

| Step | What it does | Note |
|---|---|---|
| 1 | Welcome · pod name · timezone | — |
| **2** | **Bot roster** — claim discovered OpenClaw installs, or create your first member bot | Creating a bot asks for the LLM key, messaging channel, and **your Telegram chat ID**. |
| 3 | Security config (optional workspace-backup repo) | The generated deploy key is **optional** — only for git-backed workspace backup / auto-update. |
| 4 | Admin user identity | Typically a sudo user or root on a VPS. |
| 5 | Prerequisites check | Verifies node / openclaw / python / **acl**. |
| 6 | Host power & sleep | Offers the no-sleep fix; never hard-blocks. |
| 7 | Dedicated-host acknowledgment | — |
| 8 | Install OpenClaw (if missing) | via npm. |
| **9** | **Create bot accounts** | `useradd` under `/home/<acct>`. |
| 10 | Set up OpenClaw per bot | Writes each bot's `/home/<bot>/.openclaw/openclaw.json` (0600). |
| **11** | **Configure Evolve alerts** | **Defaults the chat ID to the Step-2 value** — just press Enter (round-10). Editable for a separate infra-alert chat. |
| 12 | Set up shared directory | Creates `/var/lib/evolve` + the ACL tree. |
| 13 | Deploy Evolve | Installs the `repo-puller` + per-bot plumbing. |
| **14** | **Provision primary bot OC instance** | Creates the `evolve` service account **and** the `evo` primary bot **day-one onto `/home/evo`** (no macOS-style E.2.b cutover). Installs all infra systemd units + first-party apps + sudoers. |
| 15 | Verify | Probes gateways. |
| 16 | Claude Desktop / MCP bridge (optional) | — |
| 17 | HTTPS on the LAN (PWA-ready) | The repo-puller deploy-key prompt here is **optional** (round-10) — framed as a next step, not "✗ broken". |

After this, `ai.evolve.evolve.admin-ui.service` and `mcp-bridge` run long; every
other unit is **timer-triggered**, so seeing dozens of `ai.evolve.*` units
`inactive (dead)` is **normal** — they fire on schedule, not continuously.

---

## Phase 3 — Account model (so the perms make sense)

| Account | Home | Role |
|---|---|---|
| `evolve` | `/home/evolve` | **Service account.** Runs the admin UI, all infra daemons/timers, and the repo-puller. Owns shared_dir and the deploy checkout. |
| `evo` | `/home/evo` | **Primary bot.** Its own unprivileged gateway (`/home/evo/.openclaw/openclaw.json`). Runs the RSI/assistant. Provisioned day-one on Linux. |
| `<member bots>` | `/home/<bot>` | The bots you created in Step 2. |

`evolve` reaches each bot's `.openclaw/` via a **`setfacl` read grant** (not
`sudo -u <bot>` — that grant does not exist; see CLAUDE.md "File Access
Pattern"). The shared `proposals/` + `signals/` trees carry an inherited ACL
granting `evo` read/write/delete/append so its MCP tools can move state files.

---

## Phase 4 — Linux landmines (verify these explicitly)

These passed the wizard but broke *later* during the port. A genuinely clean
install has none of them lurking.

### 4.1 ACL mask clobber — the big one
Linux `chmod` **recalculates the POSIX ACL mask.** Any `chmod 0700` on a
`.openclaw` dir resets `mask::---`, which clamps `user:evolve:r-x` →
`#effective:---` — and the `evolve` service user silently loses read/traverse
**pod-wide** (and silently, at harden time). Every place that hardens a dir must
re-assert `setfacl -m m::rX` *after* the chmod and verify it **last**. This
regressed specifically on the web *upgrade* path after being fixed at install.

```bash
# evolve must remain EFFECTIVE r-x on every bot's .openclaw:
getfacl /home/*/.openclaw 2>/dev/null | grep -E 'user:evolve|mask'
#   want: user:evolve:r-x   and   mask::r-x   (NOT  #effective:---  /  mask::---)
```

### 4.2 Deploy checkout nested under shared_dir → frozen fleet
Because `/var/lib/evolve/repo` is a **child** of shared_dir, a recursive
`chmod -R` / `a+rX` over shared_dir flips the checkout's git file modes
`100644 → 100755`; `git pull --ff-only` then wedges and the whole fleet freezes
on stale code. Guards: `git config core.fileMode false` on the checkout, and
exclude the nested checkout from recursive shared-dir passes
(`platform_profile.is_within` / `nested_deploy_checkout`).

```bash
sudo -u evolve git -C /var/lib/evolve/repo status            # expect: clean
git -C /var/lib/evolve/repo config core.fileMode             # expect: false
# (running as root needs:  git config --global --add safe.directory /var/lib/evolve/repo)
```

### 4.3 `npm install` dirties the checkout
The plugin build must use **`npm ci`**, not `npm install` — `npm install`
rewrites `package-lock.json`, which dirties the deploy checkout and wedges the
next ff-only pull (same failure as 4.2, different trigger).

### 4.4 Secrets re-widened by recursive `a+rX`
`chmod -R a+rX {shared_dir}` makes **every** 0600 file world-readable (this is
how a Google DwD key leaked). The tree is re-tightened afterward
(`tighten_shared_secret_tree` + the pod-wide self-heal), but verify nothing
token-bearing is world-readable:

```bash
sudo find /var/lib/evolve -name '*.json' -perm -o+r \
     \( -name 'openclaw.json' -o -name 'auth-profiles.json' -o -path '*keystore*' \)
#   expect: no output
```

### 4.5 Py3.12 `Path.exists()` raises under 0700 parents
On ≤3.11 `Path.exists()` returned `False` under an unreadable parent; on **3.12
it raises `EACCES`**. Code that probes file presence in privileged trees must
catch that, or the daemon crashes at harden time instead of degrading. Already
guarded — but it's the failure family to recognize if a daemon dies with a bare
`PermissionError` from an `.exists()`/`.stat()`.

---

## Phase 5 — KNOWN UNRESOLVED GAP (read before declaring done)

> ⚠️ **The `evolve` service account has no `openclaw.json` on Linux.**

Because the gateway diverged to `evo`, `_provision_evo_oc` provisions the
`evolve` account **dirs-only** — there is no `/home/evolve/.openclaw/openclaw.json`.
Consequence, confirmed on the live box: the first-party app **`security-cve-scan`
(`bot_id: evolve`) is quarantined** — `/home/evolve/.openclaw/cron/jobs-quarantine.json`,
`reason: "missing-payload"` — and cannot run, and the wizard correctly reports
the app's `required_tools` patch as *deferred* (not failing). macOS is
unaffected (there `evolve` IS the gateway account, so the file exists).

- **Status:** flagged as a META:platform W10 follow-up in
  [PR #3046](https://github.com/evolve-ops/evolve/pull/3046); **not yet fixed in code.**
- **Until it is fixed**, a "proper" Linux install has one manual remediation, or
  it accepts CVE-scan running dark. To de-quarantine: provision
  `/home/evolve/.openclaw/openclaw.json` (a headless config sufficient for
  isolated cron sessions), `evolve:staff`, `0600`, then re-assert the `evolve`
  read-ACL/mask (§4.1) and re-run `sudo evolve-admin install-infra-jobs`.

```bash
# Detect the gap:
ls -l /home/evolve/.openclaw/openclaw.json 2>&1            # "No such file" on a current Linux pod
cat /home/evolve/.openclaw/cron/jobs-quarantine.json 2>/dev/null | grep -o 'cve-scan[a-z-]*'
```

---

## Phase 6 — Verify it's actually healthy

```bash
systemctl --failed | grep -i evolve                         # expect: none
systemctl is-active ai.evolve.evolve.admin-ui.service ai.evolve.evolve.mcp-bridge.service
getfacl /home/evo/.openclaw | grep -E 'user:evolve|mask'    # evolve EFFECTIVE r-x  (§4.1)
sudo -u evolve git -C /var/lib/evolve/repo status           # clean; core.fileMode=false  (§4.2)
sudo find /var/lib/evolve -name '*.json' -perm -o+r -path '*keystore*'   # empty  (§4.4)
ls /home/evolve/.openclaw/openclaw.json                     # KNOWN GAP — see Phase 5
# Then tunnel the admin UI over SSH and walk the Alerts/Reports page for firing signals.
```

---

## Quick reference — the one-liner

> Bootstrap the read-only deploy key **as `evolve`** → clone to
> `/var/lib/evolve/repo` → `uv sync --locked` + `npm ci` →
> `EVOLVE_PLATFORM=linux evolve-admin setup --fresh` → then verify the four
> Linux divergences: **ACL masks (§4.1)**, **nested-checkout file modes (§4.2)**,
> **secret perms (§4.4)**, and **the `evolve`-account `openclaw.json` gap
> (Phase 5)**.

## Related docs
- [runbook-linux-vm-pass-2026-06-11.md](runbook-linux-vm-pass-2026-06-11.md) — credentialed-clone bootstrap (§2a) + masked-ACE kernel question + visudo eyeball.
- [design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) — the port design + the `linux-e2e` harness (§9).
- [spec-deploy-meta-2026-06-14.md](spec-deploy-meta-2026-06-14.md) — repo-puller / canary / promote / rollback.
- [spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md) — why `evolve` (service) and `evo` (primary bot) are distinct accounts.
- `CLAUDE.md` — the File Access / sudoers / ACL contracts these checks enforce.
