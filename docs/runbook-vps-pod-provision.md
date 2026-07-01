# Runbook — provisioning an Evolve pod on a cloud VPS

**Who runs this:** an Evolve operator (or a prospective customer) standing up a
**second** pod — one that isn't your personal Mac. A test pod, or a
project-/team-specific production pod, on a cloud VPS.

**What you'll have at the end:** a hardened Ubuntu 24.04 host, reachable only
over SSH, with the prerequisites staged and the Evolve setup wizard ready to
run. The provider steps and the security posture are the load-bearing parts of
this runbook and they are final; the install step (§6) is a documented stub
that the first real install will fill in.

**Companion docs:**
[remote-operator-access.md](remote-operator-access.md) (how you reach the admin
UI from another machine — the per-client-OS tunnel commands live there),
[design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md) §2 / §8.2 (the
Add-a-pod flow and the install-step seam this runbook is the manual version of),
and [design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) (the
Linux/VPS port that makes a non-Mac pod host possible).

---

## 1. Purpose — when to use this

Use this when you want an Evolve pod running somewhere other than your personal
Mac:

- A **test pod** you can break without touching your real one.
- A **project- or team-specific production pod** — a dedicated host for one
  team's bots, kept separate from your main pod's blast radius.
- An **always-on pod** that doesn't depend on your laptop being awake and on
  your home network.

**Know what this architecture is — and isn't.** Evolve is
**sovereign-pod + hub-federation**, not failover or high availability. Each pod
is a complete, self-governing Evolve on one host; a second pod is a *second
sovereign*, not a replica of the first. The "hub" is a links-only switcher (a
`peers` list in `network.json` plus a chevron in the sidebar) that lets one
browser hop between the pods it's already paired with — it transports no
credentials and proxies no data
([design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md) §3). The only
thing that *crosses* pods is a **deposit-only** read surface (a digest one pod
can publish to another, §4 of that design) — never command, never control. If
pod A dies, pod B keeps running and the switcher row to A simply goes dead.
Nothing fails over, because nothing was ever shared. Don't reach for this
expecting a redundant pair; reach for it when you want a *second, independent*
Evolve.

> **The provider is in your trust base.** On a VPS the hosting provider has
> console and rescue access, which is root-equivalent. That's the same posture
> every self-hosted product carries, and it's your hosting choice to make — but
> a VPS pod is not identical to a box in your closet. Pick a provider you're
> willing to trust with root.

---

## 2. Pick a provider — the trio

Any provider that accepts **cloud-init user-data**, lets you attach an **SSH
public key**, and offers a **cloud firewall** will work — that's the universal
trio, and every step in §4–§5 transfers across all three providers below
unchanged. The differences are geography, price, and how polished the console
is. Pick by what you actually need:

| Provider | Pick it when | The honest one-liner |
|---|---|---|
| **DigitalOcean** *(mainstream US default)* | You're a US operator and you want the smoothest, best-documented path. | The path of least resistance for US users — clean console, US data centers everywhere, and the provider the upstream OpenClaw VPS docs walk through. Mid-priced. |
| **Vultr** | You need a region the others don't have (Asia-Pacific, Middle East, South America, more EU cities). | Widest geographic reach — **33 cloud regions** (as of 2026-06). Priced similarly to DigitalOcean; the reason to choose it is *where*, not *how much*. |
| **Hetzner** | You're in or near the EU and price is the deciding factor. | By far the cheapest — **roughly 6–7× less** than DigitalOcean/Vultr — but EU-centric. Its cheap CX/CAX tiers are **EU-only**; US presence is just Ashburn, VA and Hillsboro, OR. A US operator wanting US data residency pays more or looks elsewhere. |

*(Pricing and region counts are **as of 2026-06 — verify current numbers at
signup.** Providers re-price; Hetzner raised cloud prices up to ~37% in April
2026, for instance.)*

**The decision in one breath:** US and want it easy → DigitalOcean. Need an
unusual geography → Vultr. In the EU and counting dollars → Hetzner. When in
doubt, DigitalOcean — the rest of this runbook is written DigitalOcean-first,
with the two Vultr/Hetzner deltas called out inline.

---

## 3. Sizing

**Baseline: 4 vCPU / 8 GB RAM, Ubuntu 24.04 LTS.** That's the recommendation,
not a starting point you'll grow out of. The 8 GB matters: Evolve builds plugins
with `npm` and `uv` at deploy time, and those builds plus a handful of running
bots will sit on top of the OS. 8 GB keeps that headroom comfortably off the
OOM-killer line; 4 GB does not once a few bots are live.

**Ubuntu 24.04 LTS (Noble Numbat)** specifically — it's the distro the Linux
port pins and tests against
([design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) §1). Don't
substitute another distro or release; the wizard's prerequisite probe and the
systemd/ACL adapters are validated against 24.04.

**Budget floor: 4 GB / 2 vCPU** is acceptable for a *light test pod* — one or
two quiet bots, no heavy plugin churn. Treat it as a floor, not a default. If
the box starts OOM-ing during plugin builds, that's the signal to move up to the
8 GB baseline.

Concrete prices, **as of 2026-06 — verify at signup:**

| Provider | Baseline (8 GB / 4 vCPU) | Budget floor (4 GB / 2 vCPU) |
|---|---|---|
| **DigitalOcean** | Basic, 8 GB / 4 vCPU / 160 GB SSD — **$48/mo** | Basic, 4 GB / 2 vCPU / 80 GB SSD — **$24/mo** |
| **Hetzner** *(EU)* | CX33, 4 vCPU / 8 GB / 80 GB — **~€6.49/mo** | A smaller CX tier, EU-only |
| **Vultr** | 8 GB plan — **~$48/mo** | 4 GB plan — proportionally less |

The Hetzner column is why the price-sensitive EU operator picks it — an order of
magnitude under the US providers for the same baseline — at the cost of EU-only
data residency on those tiers.

---

## 4. Provision — DigitalOcean-first

The flow is the same everywhere: create the account, register your SSH **public**
key, then create the server choosing Ubuntu 24.04 + the 8 GB tier + **SSH-key
auth**. DigitalOcean's exact labels are below; the Vultr/Hetzner deltas are
called out as notes.

### 4.1 Create the account

- **DigitalOcean:** sign up at [digitalocean.com](https://www.digitalocean.com);
  the console lives at [cloud.digitalocean.com](https://cloud.digitalocean.com).
- **Hetzner:** create an account at
  [accounts.hetzner.com](https://accounts.hetzner.com); the Cloud console is
  [console.hetzner.cloud](https://console.hetzner.cloud).
- **Vultr:** sign up and manage everything at
  [my.vultr.com](https://my.vultr.com).

Account creation and payment stay entirely on the provider's side — their
console, your card. Evolve never holds provider credentials or touches payment
([design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md) §2).

### 4.2 Register your SSH public key *first*

Add your SSH **public** key to the account **before** you create the server (or
paste it into the create-server form). All three providers have a "Security" /
"SSH Keys" area for this.

- The key is your `~/.ssh/id_ed25519.pub` (or `id_rsa.pub`) — the `.pub` file.
  It's public; pasting it is safe. **Never** paste a private key.
- If you don't have a keypair yet: `ssh-keygen -t ed25519 -C "evolve-pod"` on
  your client machine, then register the `.pub`.

This is what makes the next step able to choose key-only auth — there's a key on
file to authorize.

### 4.3 Create the server (Droplet)

On DigitalOcean, **Create → Droplets**, and choose:

| Field | Choose |
|---|---|
| **Region** | Nearest to you — for a US operator, **NYC** or **SFO**. (Vultr/Hetzner: pick the closest of their regions; remember Hetzner's cheap tiers are EU-only.) |
| **Image** | **Ubuntu 24.04 (LTS) x64** |
| **Droplet type** | **Basic** |
| **CPU options** | **8 GB RAM / 4 vCPU** (the §3 baseline) |
| **Authentication** | **SSH key** — select the key from 4.2. **Not** "Password". |

> **Authentication = SSH key, never password.** This is the single most
> important choice on this page. A password-auth server on the public internet
> is brute-forced within minutes; a key-only server is not. If the provider
> defaults this to "Password," change it.

Create the server and note its **public IP** — that's your `<pod-ip>` for the
rest of this runbook.

**Provider note (field names):** DigitalOcean and Hetzner call the cloud-init
field **"Cloud config" / user-data**; Vultr labels it **"User Data"**. They all
accept the same `#cloud-config` body verbatim.

### 4.4 cloud-init / user-data

Evolve's **"Add-a-pod" flow (M1, forthcoming)** will *generate* the user-data
for you — the deterministic host prep (operator account, prerequisites, repo
clone, the MOTD that points at the wizard), emitted from your existing pod so
your first pod helps birth your second
([design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md) §2.1).

**Until M1 ships, do the prep manually:** create the server *without* a
user-data blob, SSH in, and run the host prep yourself (see the §6 stub). The
manual prep and the generated cloud-init do exactly the same work — M1 just
automates the typing. Either way the handoff is identical: prep stages the host,
then `sudo evolve-admin setup` (the wizard) owns the actual install
([design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md) §8.2).

---

## 5. Security configuration — read this twice

This is the load-bearing section. Evolve's security model on a VPS rests on a
single idea: **the admin UI never touches the network, and SSH access to the box
*is* the operator credential**
([design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) §1;
threat-model §2). Everything below enforces that idea. Get this right and a VPS
pod is *stronger* than a desktop Mac (no GUI apps, no other humans, no
third-party agents racing `/tmp`). Get it wrong and you've put an admin panel on
the public internet.

### 5.1 SSH: keys only, root and passwords off

**First, the bootstrap reality.** A fresh **DigitalOcean** or **Hetzner**
droplet logs you in as **`root`** — there is no non-root operator account until
something creates one. cloud-init's `users:` block does this automatically
(design-multi-pod §2.1); on the manual path (§6) you start as `root` and create
a sudo-capable operator account with your public key as the **first** prep step.
From then on you SSH in as that operator account and `sudo` from there — never
as `root` directly, never with a password.

Then harden the SSH daemon. In `/etc/ssh/sshd_config` (set these by hand on the
manual path; cloud-init's `ssh_pwauth: false` covers the first one):

```
PasswordAuthentication no
PermitRootLogin no
```

Reload with `sudo systemctl reload ssh` (on Ubuntu the unit is `ssh`, not
`sshd`). Confirm you can still log in with your key in a *second* terminal
**before** you close the first — a locked-out root password plus a broken key is
how people brick a fresh box.

### 5.2 Cloud firewall: inbound SSH (22) only — nothing else, ever

Every provider offers a **cloud firewall** (a network-level rule set applied
*before* traffic reaches the host). Configure it as:

| Direction | Rule |
|---|---|
| **Inbound** | **TCP 22 (SSH) only.** Ideally **source-restricted to your own IP** (or your tailnet — see 5.4). |
| **Inbound** | **Everything else: deny.** |
| **Outbound** | Allow (the pod needs to reach LLM providers, channels, and `git`). |

> **There is NO inbound rule for 5050 or 5051. Ever.** The admin UI (5050) is
> reached over the SSH tunnel from §5.3, and the optional MCP bridge (5051) over
> your tailnet (§5.4) — never by a public firewall hole. If you ever find
> yourself adding an inbound rule for 5050 to "just reach the dashboard," stop —
> you're about to expose the admin panel to the internet. The tunnel is the
> access path, by design.

Set this up at the provider's firewall layer, not just `ufw` on the host — a
cloud firewall fails closed even if the host's firewall is misconfigured.

### 5.3 Reaching the admin UI — loopback + SSH tunnel

The admin server binds **`127.0.0.1:5050`** and is **never** exposed to the
network. You reach it by forwarding that loopback port to your client over SSH:

```bash
ssh -L 5050:127.0.0.1:5050 <operator>@<pod-ip>
```

…then open **`http://127.0.0.1:5050`** in your browser. This is
**loopback-as-authorization**: the admin server trusts whatever can reach its
loopback, and the *only* thing that can reach it is someone who already holds an
SSH key for the box. **Whoever can SSH in is the operator** — there's no
separate network-facing login to attack because there's no network-facing
surface at all.

The per-client-OS tunnel commands (macOS, Windows PowerShell, Linux), the
`tailscale serve` alternative, and the device-pairing step that gates the UI
once you reach it all live in
**[remote-operator-access.md](remote-operator-access.md)** — that's the
authoritative reference for the access side. Don't duplicate those commands;
follow them there.

### 5.4 Recommended hardening — Tailscale, and close 22

The strongest posture: install **Tailscale** on the pod host, join it to your
tailnet, and then **restrict SSH to the tailnet — close port 22 to the public
internet entirely** at the cloud firewall. Now the box has *no* publicly-reachable
port at all; you reach SSH (and, via `tailscale serve`, the admin UI) only from
devices on your tailnet. This collapses the remaining public attack surface to
zero. It's optional, but it's the recommendation for a production pod.

### 5.5 Single-tenant — don't co-tenant the box

A pod host is **dedicated to Evolve**. Don't run other services, other people's
accounts, or unrelated workloads on it. The entire threat model assumes a
single-tenant host (threat-model §2): the loopback-as-authz design, the per-bot
Unix users, the `evolve`/`evo` service-account sudoers grants — all of it is
sound *because* there's no untrusted second tenant racing for the same files. Put
something else on the box and you've quietly invalidated the assumption every
other control depends on. One pod, one host, one purpose.

---

## 6. Install Evolve on the box — STUB *(forthcoming)*

> **Forthcoming — to be filled from the first real install.** The steps below
> are the *shape* of the install, not verbatim commands. The exact clone+install
> contract is being finalized on a live pathfinder pass (the first real VPS
> install). **Do not treat the commands below as final**; the section will be
> replaced with the verified sequence once that pass completes.

Outline, once you've SSH'd into the host (as `root` on a fresh DO/Hetzner box):

0. **Create the operator account.** Add a non-root, sudo-capable login with your
   SSH **public** key (this is what cloud-init's `users:` block does for you —
   design-multi-pod §2.1). Re-connect as that account and confirm `sudo` works
   before going further; the wizard assumes you SSH'd in as the operator, not as
   `root`.
1. **Install prerequisites** (the wizard *probes* for these but does not install
   them — see
   [design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md) §8.2 and the
   wizard's step-1 probe):
   - **Node.js 24** via NodeSource (`deb.nodesource.com/setup_24.x`).
   - **`acl`** (`setfacl`/`getfacl` — the wizard uses POSIX ACLs for the
     `evolve`/`evo` cross-user grants).
   - **systemd** — already present on Ubuntu 24.04; the wizard hard-fails
     without it.
   - **`uv`** (the venv / interpreter contract — the wizard's Python must run in
     the project venv).
2. **Clone the repo at `/var/lib/evolve/repo`** (the canonical Linux deploy
   checkout — `platform_profile.LINUX.deploy_checkout_default`) and build the
   venv so that `evolve-admin` lands on PATH.

   > ⚠️ **Stage the source at `/var/lib/evolve/repo`, NOT under `/root`.**
   > `/root` is mode `0700`/`0710` on a fresh box, so the `evolve` service user
   > (and the bot users) cannot traverse into it. The source location is baked
   > into the venv editable `.pth` and into ~50 daemon `ExecStart=` paths
   > (`deploy.py::_REPO_ROOT`); an unreadable root makes every daemon die with
   > ModuleNotFoundError / EACCES and the admin UI crash-loop. The wizard now
   > runs a Linux preflight that HARD-FAILS early if `_REPO_ROOT` isn't
   > traversable by `evolve` — but stage it correctly and you'll never see it.
   > Clone as root, then `chown -R evolve:evolve /var/lib/evolve/repo` (or leave
   > it root-owned but world-traversable, `0755` on every ancestor).

   **This initial clone+install contract is the one genuine gap** the multi-pod
   design calls out — and the *path+ownership* half is now formalized here and in
   [design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) §8.2. The
   exact git URL, the commit pin, and the `uv sync` / compat-editable invocation
   that exposes the `evolve-admin` console script are being defined now.
   *Don't invent those commands here.*
3. **Run the wizard to completion:** `sudo evolve-admin setup`. The wizard owns
   the privileged, stateful install — bot accounts, ACLs, sudoers, systemd
   units, the plugin build, the shared directory — *interactively*, interleaving
   the operator-only questions that must not be defaulted (pod name, roster, the
   dedicated-host acknowledgment, channel/consent choices, and device pairing).
   The prep stages the host **up to** the wizard; the wizard does the rest. For
   the macOS-vs-Linux step-by-step of what the wizard still does under the Linux
   gate, see
   [census-linux-wizard-remainder-2026-06-11.md](census-linux-wizard-remainder-2026-06-11.md).

After setup finishes, pair your browser (the pairing code comes from
`sudo evolve-admin pair` over SSH — see
[remote-operator-access.md](remote-operator-access.md)) and, if this is your
second pod, add it to your switcher.

---

## 6b. Teardown — fully resetting a box between install rounds

If you are re-running a fresh install on a box that was already provisioned
(a pathfinder / soak loop), tear down **completely** first. A partial teardown
is worse than none: leftovers feed the next install dirty state. The round-3
W10-F pass hit exactly this — teardown removed `/home/*` but left a `/Users`
tree, a session log with the API key in cleartext, and a 372 MB stale source
tree, which between them fed a stale-key bug and a hygiene leak.

The wizard now HARD-FAILS on a Linux preflight if a `/Users` tree exists
(it is always cruft on Linux), so an incomplete teardown is caught up front
rather than silently poisoning the key scan. Clean the full set:

```bash
# Stop + remove all Evolve units (systemd)
sudo systemctl list-units --all 'ai.*.evolve.*' --no-legend --plain \
  | awk '{print $1}' | xargs -r -n1 sudo systemctl disable --now 2>/dev/null
sudo rm -f /etc/systemd/system/ai.openclaw.evolve.*.service \
           /etc/systemd/system/ai.evolve.evolve.*.service
sudo systemctl daemon-reload

# Remove every bot + service account home (Linux homes live under /home)
#   — replace the loop body with `userdel -r` per real bot account.
for u in evo evolve <bot1> <bot2>; do sudo userdel -r "$u" 2>/dev/null; done

# Cruft that survives a naive `rm -rf /home/*`:
sudo rm -rf /Users                  # macOS home root — ALWAYS cruft on Linux;
                                    #   a stale /Users/<bot>/.openclaw feeds the
                                    #   key scan a leftover API key (bug B).
sudo rm -f  /root/setup-session.log # contains the API key in CLEARTEXT if you
                                    #   teed the session; the wizard no longer
                                    #   echoes the key, but old logs persist.
sudo rm -rf /root/evolve /root/evolve.old   # stale staged source trees (372 MB)

# Evolve state, venv, plugin, deploy checkout, and per-pod quarantine
sudo rm -rf /var/lib/evolve /var/lib/evolve-repo /var/lib/evolve-venv \
            /var/lib/evolve-plugin /var/lib/evolve-quarantine

# sudoers grants
sudo rm -f /etc/sudoers.d/evolve /etc/sudoers.d/evolve-admin
```

Re-stage the source at `/var/lib/evolve/repo` (§6 step 2) and re-run
`sudo evolve-admin setup --fresh --platform linux`. The `/Users` preflight
confirms the box is genuinely clean before the install proceeds.

> **Secret hygiene:** never run `setup --fresh` under `script`/`tee` to a
> file you keep — and if you must, scrub it. The wizard reads API keys with
> terminal echo OFF (`getpass`) so live typing is not captured, but a tee'd
> stdout still records anything Evolve prints. Treat `/root/setup-session.log`
> as a secret and delete it as part of teardown.

---

## 7. What success looks like

You're done with provisioning when:

- [ ] The cloud firewall allows **inbound 22 only** (ideally source-restricted),
      and there is **no** inbound rule for 5050/5051.
- [ ] `ssh <operator>@<pod-ip>` works with your **key**; password and root login
      are off.
- [ ] `ssh -L 5050:127.0.0.1:5050 <operator>@<pod-ip>` plus
      `http://127.0.0.1:5050` reaches the admin UI's pairing screen — and that's
      the *only* way it's reachable.
- [ ] The box runs **nothing but Evolve** (single-tenant).
- [ ] (After §6) `sudo evolve-admin setup` has run to completion and your
      browser is paired.

---

## See also

- [remote-operator-access.md](remote-operator-access.md) — reaching the admin UI
  from any client OS (the tunnel commands, `tailscale serve`, pairing).
- [design-multi-pod-2026-06-11.md](design-multi-pod-2026-06-11.md) — the
  sovereign-pod + hub model (§3), the Add-a-pod provisioning flow (§2), and the
  install-step seam (§8.2) this runbook manually executes.
- [design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) — the
  Ubuntu 24.04 pod-host port: the SSH-tunnel access model (§1), per-bot Unix
  users, systemd scheduler, and POSIX ACLs.
- [census-linux-wizard-remainder-2026-06-11.md](census-linux-wizard-remainder-2026-06-11.md)
  — what `sudo evolve-admin setup` does, step by step, on a Linux host.
