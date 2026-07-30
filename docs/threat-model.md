# Threat model

**Status:** current · **Last updated:** 2026-06-11 · **Owner:** pod-admin

This document records the trust model Evolve operates under, the assumptions
it depends on, the assets it protects, and the residual risks a reviewer or
operator should be aware of. It is written for internal use and for diligence
reviewers — accuracy over optimism.

---

## 1. Trust boundaries

### 1.1 The single host

All Evolve components run on one machine — the pod host (a macOS machine, the
"mini", today; an Ubuntu 24.04 host once the feature-gated Phase 8.3 Linux
port un-gates). There is no network boundary between the admin daemon, the bot
gateways, and the operator's browser. The entire security model rests on
OS-level process and user isolation.

**Remote operator over SSH (VPS / headless-host variant).** When the
operator's browser is not on the pod host — the normal case for a dedicated
Linux VPS, and an existing option for a headless Mac — the trust boundary does
not move: the admin server stays bound to `127.0.0.1`, and the operator
reaches it through an SSH tunnel (`ssh -L 5050:127.0.0.1:5050
pod-admin-user@<pod-host>`). **SSH access to the box is the operator
credential** — anyone who can SSH in is the operator, which is the
single-tenant assumption (§2) restated, not a new trust tier.
Loopback-as-authorization (§2's table) is unchanged by remoteness; no
web-exposure path is built or supported. The device-pairing auth that is on
by default (§6.1) still applies on top of the tunnel, so a second human with
SSH access to a box someone treats as shared must still pair before the admin
UI answers — the residual is that SSH-key sprawl on the host violates §2
regardless of what the UI does.

### 1.2 The operator

The person at the keyboard (`pod-admin-user` macOS account). Has `sudo` rights
via `/etc/sudoers.d/evolve-admin`, which enables `sudo evolve-admin <subcommand>`
for install/deploy/rotate operations. Implicitly trusted: when the admin UI
accepts a proposal approval, it treats every request that reaches the loopback
Flask server as the operator.

### 1.3 The three account tiers

| macOS user | Runs | Privilege |
|---|---|---|
| `pod-admin-user` | Admin shell, browser | `sudo` via `/etc/sudoers.d/evolve-admin` (deploy, keys rotate, etc.) |
| `evolve` | Admin daemon (Flask server, repo puller, reconciler, signal store) | Narrow `sudo` grants in `/etc/sudoers.d/evolve` (read/write bot configs, launchctl kickstart/bootout, openClaw CLI as bot users). Does **not** have broad root or bot-user `sudo`.|
| `evo` | Evo bot gateway (LLM session, MCP tools) | No `sudo`. No cross-bot ACL. Same reach as any other member-bot user. Privileged operations route through the admin daemon's HTTP API. |
| `<bot-user>` (one per bot) | Bot's OpenClaw gateway | No `sudo`. Own home + pod-wide `/Users/Shared/evolve/` only. |

`evolve` can read every bot's `.openclaw/` directory via macOS ACL inheritance
(`set_evolve_read_acl` in `deploy.py`). It cannot write to bot config files
directly — writes go through `/tmp` staging + `sudo /bin/cp` to the
bot-owned destination, matching the sudoers grants.

`evo` separated from the `evolve` account in Phase E.2.b
(see `docs/spec-evo-account-separation-2026-05-25.md`). Prior to that cutover,
evo's gateway shared the `evolve` account and inherited full admin-daemon
reach; after the cutover it does not.

### 1.4 What each tier cannot reach

| Tier | Cannot |
|---|---|
| `evo` | Read another bot's `.openclaw/` files directly (EACCES); call `launchctl`; write to `/etc/sudoers.d/`; read the admin daemon's process memory. |
| `<bot-user>` | Read another bot's home; read `evo`'s workspace; write to `/Users/Shared/evolve/proposals/` (read-only ACL for non-evo bots). |
| `evolve` daemon | Cannot `sudo -u <bot>` arbitrarily — only named commands at named paths listed in sudoers. Cannot run LLM sessions; cannot directly impersonate the operator in external integrations. |

---

## 2. The single-tenant assumption

**Evolve assumes no other untrusted local users exist on the host.**

This assumption is load-bearing. The following controls depend on it:

| Control | How the assumption is load-bearing |
|---|---|
| `/etc/sudoers.d/evolve` grants (e.g. `sudo /bin/cp /tmp/evolve-stage-*.json /Users/*/.openclaw/openclaw.json`) | The wildcard patterns are broad enough to be reachable by any local user who can write to `/tmp`. A hostile local user could pre-stage a malicious file and race the daemon. `_secure_stage()` (Phase 0) closes the most obvious race with `O_EXCL`+random suffix, but the grant surface itself is wide. |
| `/tmp` staging | `/tmp` is world-writable on macOS. Even with `mkstemp`, a local user with a PID oracle could attempt collisions in the brief window between `mkstemp` and `sudo cp`. Single-tenant assumption makes this window not exploitable in practice. |
| Keystore file perms | The HMAC signing key (`keystore/evolve-signing.key`, mode 0600, owned by `evolve`) and the machine XOR-encryption key (`.machine-key`, mode 0640) are protected by POSIX permissions, not encryption-at-rest. A root-capable local user could read them. |
| Admin server loopback | The Flask server binds to `127.0.0.1` only (`server.py` line 3: "Binds to 127.0.0.1 only — never expose externally"). Any process on the same machine can reach it. The "UI access is the authorization layer" model means there is no per-request authentication; operator identity is inferred from the fact that someone reached the loopback port. |

**When the assumption holds:** on a dedicated Mac running only Evolve and
its bots, with a single admin user, no guest accounts, and no third-party daemons
with shell access. This is the intended deployment topology.

**Scope: any dedicated, always-on Mac — the chassis is irrelevant.** A
retired MacBook on a shelf is exactly as isolated as a Mac mini; "dedicated"
is a property of how the machine is *used*, not of its hardware, and it is
the informed operator's choice to make and keep. The control for this is the
acknowledgment the setup wizard records (`network.json` → `host.dedication_ack`):
the wizard explains the four controls above, asks the operator to commit to
keeping the machine dedicated, and records the answer — it never hard-blocks.
The failure mode to watch is **drift**, not the initial decision: a "retired"
laptop quietly returning to daily personal use months after setup. A pod on a
machine that picks up other users or workloads should be re-homed first.
(Apple Silicon is the recommended architecture; Intel Macs are supported
best-effort — macOS 26 is the final Intel release.)

The assumption is **per-host and platform-neutral**: a dedicated Linux VPS —
one sudo-capable operator login, no other human users, nothing else deployed —
satisfies it exactly as a dedicated Mac does, and the four controls above
carry over unchanged under the Phase 8.3 port (arguably with less ambient
risk: no GUI apps, no third-party desktop agents racing `/tmp`).

**When the assumption breaks:** if a shared machine has other local users, or if
a remote-code-execution vulnerability in a bot gateway gives an attacker a shell
as any local user, the controls above weaken. See §6 for open items.

---

## 3. Assets

### 3.1 Secrets

| Asset | Location on disk | Protection |
|---|---|---|
| Bot LLM API keys (Anthropic, OpenAI, etc.) | `auth-profiles.json` in each bot's `.openclaw/agents/main/agent/`, mode 0600, owned by the bot user | POSIX permissions; `evolve` can read via sudoers `cat` grant; not encrypted at rest |
| GitHub PAT (self-backup + admin discovery) | Keystore (`github_pat`): macOS Keychain when usable, else `{shared_dir}/keystore/vault/github_pat.enc` (Fernet) | Moved out of plaintext `network.json::github.pat` 2026-06-10 (roadmap 2.8); a startup migration scrubs the legacy slot. See §6.2 for what the vault does and does not protect against. |
| Channel bot tokens (Slack, Telegram, etc.) | Per-bot `auth-profiles.json` or workspace `.env` | Same as API keys |
| HMAC signing key | `{shared_dir}/keystore/evolve-signing.key`, mode 0600, owned by `evolve` | POSIX permissions only; used to sign proposals/review stamps/forge results |
| Machine key (file-vault) | `{shared_dir}/keystore/.machine-key`, mode 0640, owned by `evolve` | Key for the Fernet file vault (the XOR scheme was replaced in roadmap 2.2). Protected by POSIX permissions only — see §6.2 for the honest framing of what this defends. |
| macOS Keychain entries | System/login keychain | macOS Keychain protection; strongest store available; used when Keychain is accessible |

### 3.2 The proposal pipeline

The generate → review → approve → apply → verify loop. A tampered proposal
could cause the applier to write a malicious bot config or execute an
LLM-authored script on an operator-approved click. Protected by:

- HMAC signatures on proposals, review stamps, and forge results (Phase 0 —
  `evolve_config.py::verify_proposal_sig` / `verify_review_stamp` /
  `verify_forge_result_sig`).
- Signature enforcement fails CLOSED once signing has ever been enabled
  (`SIGNING_ENFORCED_MARKER` outside the keystore directory — designed so
  key loss doesn't silently re-open the fail-open hole).
- The static review gate (`arbiter/routing.py::is_autonomous_eligible`):
  proposals with irreversible effects, wide blast radius, or no revert plan
  are routed to human approval rather than `approved_auto`.

### 3.3 Bot configs (`openclaw.json`, `auth-profiles.json`)

Bot configuration files are owned by the bot user (mode 0600/0644). The admin
daemon writes them via `/tmp` staging + `sudo /bin/cp`. A compromised `evolve`
account, or a hostile proposal that makes it through the review gate, could
modify a bot's config.

### 3.4 The autonomous-apply loop

See §4 for the detailed safety story.

---

## 4. The autonomous-apply safety story

The full lifecycle of an autonomous proposal:

```
Generator observes signal
    → writes Proposal to {shared_dir}/proposals/pending/
    → HMAC signed at creation (evolve_config.sign_proposal)

Arbiter routes proposal
    → is_autonomous_eligible() checks:
        reversibility == "auto"
        blast_radius in {"local", "bot"}
        touches no IRREVERSIBILITY_SURFACES
        claim present (something to verify against)
        revert_on_failure attached (revert plan required)
    → eligible  → status: approved_auto  (automated)
    → ineligible → status: pending, routed to human approval audience

Apply daemon (heal.py / verify/daemon.py)
    → re-verifies HMAC signature before applying
    → calls applier (config_patch / tier_adjustment / etc.)
    → applier writes bot config via safe_write_bot_config()
        (validates against OpenClaw schema before touching live config)

Verify daemon
    → re-reads proposal, re-verifies HMAC
    → resolves claim metric against real data
    → claim resolved correctly → status: succeeded
    → claim not met          → triggers revert_on_failure
    → revert succeeds        → status: failed_reverted
    → revert fails           → status: failed_revert_failed (operator alert)
```

**Human-gated steps:**
- All proposals with `reversibility != "auto"`, wide blast radius, or
  no revert plan go to human approval.
- `forge` (LLM-authored script execution) requires human approval regardless
  of other flags — it is an `IRREVERSIBILITY_SURFACE`.
- The operator approves via the admin UI; the approval itself is not
  additionally authenticated beyond loopback trust (see §6.1).

**What is fully automated:** config-patch and tier-adjustment proposals that
satisfy the `is_autonomous_eligible` criteria run without operator interaction
from generate through verify.

**What is not automated:** any proposal that writes a script, modifies system
plists, touches cross-bot state, or has blast_radius above "bot" requires an
explicit operator click.

---

## 5. Controls recently hardened (Phase 0, PR #2512)

Four controls added in the 2026-06-09 diligence review:

### 5.1 Fail-closed proposal signing (`evolve_config.py`)

Before: `verify_proposal_sig` returned `True` when the signing key was
absent — this meant a key loss (ACL hiccup, botched restore) would silently
re-open the fail-open hole on a pod that had previously been running with
signing enabled.

After: a `SIGNING_ENFORCED_MARKER` file lives _outside_ the keystore directory.
Once it is written (on first successful `_load_signing_key()` or
`generate_signing_key()`), all three verify functions (`verify_proposal_sig`,
`verify_review_stamp`, `verify_forge_result_sig`) fail CLOSED when the key is
missing. A genuinely fresh, never-keyed pod still bootstraps open.

### 5.2 Secret redaction on `/api/network` (`web/server.py::_redact_secrets`)

Before: `/api/network` returned the full `network.json` including `github.pat`,
channel `botToken` fields, and gateway auth tokens in plaintext.

After: a recursive redactor replaces any value whose key name matches a known
secret set (`pat`, `token`, `bottoken`, `apikey`, `secret`, etc.) with
`[REDACTED]`. Presence is preserved (truthy string) so the UI's set-vs-unset
logic still works. Reference-like keys (`tokenRef`, `token_slot`) are
deliberately excluded from the match set.

### 5.3 TOCTOU-safe `/tmp` staging (`deploy.py::_secure_stage`)

Before: temporary files used predictable names like
`/tmp/evolve-<bot_id>-<purpose>.json`. On a multi-user machine this is a
local TOCTOU/symlink privilege-escalation surface: `/tmp` is world-writable,
so an attacker could pre-create the path or swap it between the daemon's write
and the subsequent `sudo /bin/cp`.

After: `_secure_stage()` uses `tempfile.mkstemp()` with `O_EXCL` (unguessable
random suffix, exclusive creation). Applied at all 8 `sudo cp` call sites in
`deploy.py`. The single-tenant assumption still carries the residual risk
that even with a random name, a root-capable local process could inspect `/tmp`
and race the window; but the window is now narrow and the name is not guessable.

### 5.4 Least-privilege authz default (`evo/tools/mcp_server.py`)

Before: `_default_caller_identity()` returned `admin_ui` surface (highest
privilege tier) when `EVOLVE_CALLER_SURFACE` was absent or unrecognized. This
meant any unintended or misconfigured spawn path received admin-tier tool
access silently.

After: absent/unrecognized surface defaults to `cross_bot_member` (least
privilege). Every real spawn path sets `EVOLVE_CALLER_SURFACE` explicitly;
an unknown value is treated as an untrusted caller, not an admin one.

---

## 6. Known residual risks and open items

These items are explicitly unresolved as of 2026-06-09. They are documented
here to be honest rather than hidden.

### 6.1 Admin-server authentication — on by default (roadmap 2.1 + 2.6)

The Flask server binds loopback only, but loopback is not authentication: any
local process (a compromised bot dependency, a prompt-injected agent) can
`curl` it. The server now enforces **device-pairing auth by default**
(`web/admin_auth.py`, decision D1):

- `is_auth_enabled()` returns True unless the operator recorded an explicit
  opt-out marker (`{shared_dir}/keystore/admin-auth.disabled`). A fresh or
  upgraded pod enforces; the setup wizard mints the key and prints a pairing
  code so first-run is forced pairing, not lockout-discovery, and
  `sudo evolve-admin pair` is the SSH recovery path (it needs root — the
  admin-auth key is owned by the `evolve` daemon, mode 0600; the command
  refuses to run unprivileged rather than mint a key the daemon can't read).
- Browsers/PWA authenticate with a signed device cookie (`evolve_device`).
  evo's tool runtime authenticates differently: it calls the admin daemon over
  the **peer-authenticated unix socket**, and `_enforce_device_auth` exempts a
  socket request whose kernel peer-uid is trusted (the same check
  `require_trusted_peer` uses). A socket request from an untrusted uid is NOT
  exempt — it falls through to the cookie gate. evo's HTTP-routed tools route
  through `evo/admin_http.urlopen_admin` (socket-first, TCP fallback), so the
  flip doesn't break them.
- The read-only liveness probe (`/api/health`) and the pairing flow are
  exempt so a healthy enforcing daemon never looks down and pairing can
  bootstrap.

**Opt-out is a recorded acceptance, not a silent default.**
`evolve-admin auth disable --accept-risk "<reason>"` writes the marker (with
`by`/`reason`); `auth enable` removes it; `auth status` shows state. Opting out
reverts to "loopback + single-tenant is sufficient" — appropriate only on a
genuinely dedicated host (§2), and the reason should be noted here when used.

### 6.2 Secrets at rest — file vault accepted as the floor (decision D2, 2026-06-10)

Decision record (`docs/decision-security-defaults-2026-06-10.md` D2):
**Keychain-mandatory is rejected; the Fernet file vault is the accepted floor
for daemon-held secrets; plaintext persistence is not acceptable.** The admin
daemon is a headless LaunchDaemon running as `evolve` with no GUI session, so
the macOS Keychain is routinely locked/unavailable to it — a Keychain-mandatory
policy would break daemon secret access or push operators toward
keychain-unlock hacks worse than the vault. The Keychain is still used
opportunistically when usable; `EVOLVE_KEYSTORE_NO_KEYCHAIN` forces vault-only.

What the vault honestly provides, given the machine key
(`{shared_dir}/keystore/.machine-key`, mode 0640, `evolve`-owned) sits on the
same disk:

- It does **not** protect against a root-capable local attacker — they can
  read the machine key. The single-tenant assumption (§2) carries this, as it
  already does for `auth-profiles.json`.
- It **does** protect against the leak classes that actually occur: secrets in
  greppable config files, secrets swept into git-pushed backups (the
  public-repo-guard incident class), secrets in `/api/*` payload responses,
  and shoulder-surfed `cat network.json`. Fernet is authenticated encryption,
  so vault entries are also tamper-evident. (The legacy XOR vault was replaced
  by Fernet in roadmap 2.2; old XOR blobs are read once and re-encrypted on
  the next write.)

Current state per secret: `github.pat` migrated to the keystore 2026-06-10
(roadmap 2.8 — the onboarding wizard writes there, a startup migration scrubs
any legacy plaintext, and readers are keystore-first with a legacy fallback).
Bot API keys in `auth-profiles.json` remain plaintext-on-disk (mode 0600,
bot-user owned) — that file format is OpenClaw's, not Evolve's; encrypting it
requires upstream support.

**What would change D2:** Apple shipping a sanctioned headless-keychain story,
or pods moving to hardware with a usable secure element.

### 6.3 The static review gate is signature-based, not semantic

The autonomy gate (`is_autonomous_eligible`) checks reversibility, blast
radius, and a blocklist of irreversibility surfaces. These are metadata fields
on the proposal itself. A sophisticated LLM-authored proposal could potentially
be crafted to pass the gate by setting those fields to passing values while
encoding harmful intent in the `proposed_change` payload, which is only parsed
by the applier.

The static gate provides meaningful defense against accidental wide-blast
proposals and makes the review cost legible. It does not provide strong semantic
guarantees against adversarial proposal crafting.

**Open item:** Phase 2.3 in `docs/roadmap-80-to-100-2026-06-09.md` tracks
adding semantic checks (AST-level for script changes) and sandbox validation
for higher-risk proposal classes.

### 6.4 `evo` account separation — SHIPPED 2026-05-30 (closed)

All phases of the evo account separation are implemented and the production pod
cut over 2026-05-30: E.2.a (provision the `evo` macOS account), E.3 (re-plumb
C-bucket tools through the admin daemon's unix socket, peer-authenticated via
`getpeereid`), E.2.b (gateway plist flipped to the `evo` user), and E.4
(exec-policy carve-out removal, shipped 2026-05-25). On a cut-over pod evo's
gateway runs as the unprivileged `evo` user with no sudo and no cross-bot ACL;
privileged operations route through the admin daemon (see §1.3). Note the
cutover itself is an operator-run migration (`sudo evolve-admin
migrate-evo-account-cutover`): fresh installs provision the `evo` account at
setup but run the gateway as `evolve` until the operator cuts over. Spec:
`docs/spec-evo-account-separation-2026-05-25.md`.

The dual-trust migration window this left behind —
`peer_auth.DEFAULT_TRUSTED_USERS = ("evolve", "evo")`, which kept the admin
daemon's own uid trusted on the peer-authenticated socket — was decommissioned
2026-06-10 (roadmap 2.11). The default trust list now tracks the gateway's
actual runtime user (`network.json::bots.<primary>.user`, flipped atomically by
the cutover): cut-over pods trust only the `evo` peer; pods not yet cut over
(fresh installs included) trust `evolve`, which is what evo's gateway runs as
there. Resolution is per request, so the cutover flips trust without a daemon
restart.

### 6.5 Forge install command execution — argv-vector + allowlist (closed 2026-06-10)

LLM-authored `install_cfg.command` strings no longer reach a shell. The 2.5
denylist (regexes over the raw string) remains as belt-and-braces, but the
load-bearing control since roadmap 2.9 (decision D3,
`docs/decision-security-defaults-2026-06-10.md`) is structural:

- Commands are parsed with `shlex` into an **argv vector** and executed with
  `shell=False` — `$(…)`, backticks, `${IFS}`, pipes, and chaining are inert
  bytes, eliminating the regex-bypass class rather than enumerating it.
- Commands that *request* shell semantics (operators, evaluation markers,
  newlines) are refused pre-exec with an explanation; compound logic belongs
  in a script artifact (which goes through script review) invoked by a simple
  command.
- The first token must be an **allowlisted interpreter/tool** (python3, bash,
  pip, npm, simple file utilities — empirically a superset of every command in
  the gallery) or a file inside the bot's own workspace (`..` traversal out of
  the workspace is rejected).
- For an **allowlisted code interpreter** (python/bash/sh/node) the first
  argument must be a **script path, not an option flag**: `python3 -c
  "<payload>"` / `bash -c "<payload>"` are refused (a legitimate
  `<interpreter> <script>` never leads with a flag).
- For a **workspace-resident head**, the gate refuses any leading option flag
  outright — positional args only. The bot owns its workspace, so it could
  drop or symlink `${workspace}/python3` (or an alias `${workspace}/py`) and
  smuggle inline code via `-c` / joined `-cimport os` / `-W x -c …`.
  Enumerating interpreter flag spellings is the trap this design avoids, so
  the flag *shape* is refused wholesale; a workspace script that needs flags
  is invoked through an interpreter head (`/bin/bash ${workspace}/x.sh
  --flag`, the gallery's own convention), which routes the flags to the
  script rather than an inline-code switch.
- Together these make the interpreter allowlist a real boundary rather than
  cosmetic: there is no command shape that runs operator-invisible inline code
  through the gate.
- The same gate covers the gallery requirement probes
  (`requirements.system[].check`), the other manifest-supplied exec surface.

Residual (honest scope of the control):

- **It constrains *how* a command executes, not *what* a script does.** The
  gate guarantees the command invokes a reviewable script or a known tool; it
  does not read the invoked script's contents. Script-content review and the
  deferred seatbelt sandbox (per D3) are the next escalation.
- **`pip` / `npm` / `npx` are supply-chain primitives** (lifecycle scripts,
  `--index-url`, `git+` installs) that the gallery legitimately uses. The gate
  admits them by name; their package-source trust is gated only by the
  approval click. Constraining their flags would re-enter the enumerate-the-bad
  trap D3 abandoned — the sandbox is the right boundary when this matters.
- The **human-approval click** remains the authorization gate for *what* an
  install is allowed to do; the argv gate raises the floor on *how*.

### 6.6 Cross-site request forgery — addressed 2026-06-10 (roadmap 2.7)

The admin server is browser-facing, so an authenticated operator visiting a
malicious page was a CSRF surface: a cross-origin `POST` to
`http://127.0.0.1:5050` rides the `evolve_device` cookie. `SameSite=Lax`
blocked cross-*site* form/navigation POSTs but not all shapes, nor
DNS-rebinding. The defense (`web/csrf.py`, decision D4) adds, on mutating
methods for cookie-authenticated requests:

- **Origin/Referer same-origin check** (the load-bearing control; a rebound
  page's Origin ≠ the loopback Host).
- **Double-submit CSRF token** — a readable `evolve_csrf` cookie echoed in
  `X-CSRF-Token`; same-origin policy + CORS-preflight-on-custom-header mean
  only our own JS can produce it. The global `fetch` wrapper stamps it on
  every same-origin mutating request.
- **Host allowlist** (loopback + `adminBaseUrl` host + `trustedHosts`) for the
  rare Origin-less mutating request.

The two state-changing pairing-poll routes
(`/api/skills/install/{whatsapp,signal}/pair/<id>` — they write `openclaw.json`
and kickstart gateways) were converted from GET to POST so the side effect is
not reachable as a forgeable cross-origin `<img>`/`fetch` GET. The Slack OAuth
callback stays a GET (it is an OAuth redirect target) and is protected by its
server-stored, unguessable `state` parameter.

Unix-socket requests are exempt (kernel peer-uid auth, no ambient cookie). The
control is scoped to authenticated requests — an unpaired/open pod has no
session to forge.

---

## 7. Out of scope

- **Network-level threats:** Evolve does not expose any externally-reachable
  ports in its default configuration. External network attacks are out of scope
  for this model.
- **OpenClaw upstream:** the LLM gateway and its plugin SDK are third-party
  components. Vulnerabilities in OpenClaw itself are reported upstream.
- **macOS kernel / hypervisor attacks:** the model assumes the OS and hardware
  are uncompromised.
- **Multi-machine deployments:** this document describes single-machine pods
  only. Multi-machine topologies add network-policy and identity-federation
  questions not addressed here.
