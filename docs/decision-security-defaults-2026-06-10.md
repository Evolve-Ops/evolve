# Decision memo: security defaults (Phase 2 residuals 2.6–2.11)

**Status:** decided · **Date:** 2026-06-10 · **Owner:** pod-admin ·
**Roadmap:** [roadmap-80-to-100-2026-06-09.md](roadmap-80-to-100-2026-06-09.md) Phase 2 residuals ·
**Prior art:** [design-phase2-security-hardening-2026-06-09.md](design-phase2-security-hardening-2026-06-09.md),
[threat-model.md](threat-model.md)

The 2026-06-10 audit found Phase 2's ✅ overstated: the pairing layer shipped but
**opt-in**, the Fernet vault shipped but the GitHub PAT is still plaintext, and the
forge install gate is a regex denylist in front of `shell=True`. Each residual row
turns on a product decision about **defaults**. This memo records those decisions
so the implementation PRs (one per row) execute against a written rationale instead
of re-litigating it per-PR. The standing bar: the product claims *"vigilant by
default, friendly by design"* — a default that ships open fails the first half; a
default that locks out the Plex-installer persona fails the second.

---

## D1 (row 2.6) — Admin auth: ON by default, explicit recorded opt-out

**Decision: auth is enforced by default. Fresh pods boot enforcing. Setup forces
the first pairing. Opting out requires an explicit CLI command that records the
acceptance.**

Today `is_auth_enabled()` returns False until the operator runs `evolve-admin
pair` — a fresh pod's control plane (deploy, config write, proposal approve) is
open to any local process. That is the single most quotable diligence finding:
*"the safety product ships its control plane open."*

| Option | Plex-installer friction | Closes the finding |
|---|---|---|
| Keep opt-in, document harder | none | ✗ — the default is the finding |
| **Default-on, forced first pairing, recorded opt-out** | one pairing code per device, shown by the wizard at setup | ✓ |
| Default-on, no opt-out | same, plus dead-end for legitimate kiosk/dev setups | ✓ but brittle |

**Friction analysis for the Plex-installer persona.** Pairing is a pattern this
persona already performs: Plex itself requires a `plex.tv/link` code claim at
setup; Chromecast, smart TVs, and 2FA apps all use the same enter-this-code shape.
The cost is ~30 seconds per device, once (the device cookie is long-lived). The
wizard prints the code at the moment the operator is already looking at a terminal
they just ran setup from, so the happy path adds **zero extra context switches**.
This is well inside the persona's tolerance; an open control plane is not inside
the product's claims.

**Mechanics decided:**

- `is_auth_enabled()` semantics invert: enforced **unless** an explicit opt-out
  marker exists (`{shared_dir}/keystore/admin-auth.disabled`, JSON:
  `{disabled_at, by, reason}`). Key-absent + no marker = enforced (requests 401 /
  redirect to `/pair`; the operator runs `evolve-admin pair` to mint the key and
  get a code). Deleting the key no longer silently disables auth — that was a
  fail-open escape hatch; the escape hatch is now the recorded opt-out.
- `evolve-admin auth disable --accept-risk "<reason>"` writes the marker;
  `evolve-admin auth enable` removes it. The opt-out acceptance is documented in
  threat-model.md §6.1 (it shifts the boundary back to "loopback + single-tenant").
- The setup wizard ends by generating the key and printing the pairing code + URL,
  so first-run is *forced pairing*, not lockout-discovery.
- **Upgrade behavior:** existing unpaired pods begin enforcing on the deploy after
  this lands. The UI redirects to `/pair`; the operator runs
  `sudo evolve-admin pair` over SSH (`ssh pod-admin-user@<pod-host>`). This is
  deliberate fail-closed; the alternative (grandfather existing pods open) would
  leave the one production pod as the one open pod.

**Load-bearing consequence found during recon:** `admin_auth.py`'s docstring
claims internal components don't use the HTTP API — that is **stale**. Evo's
HTTP-routed tools (`action_keys.py`, `action_plugin.py`, wizard engine/handlers)
call the TCP API via `resolve_admin_base_url()`. Default-on auth would 401 them.
Decision: those tools route through the existing peer-authenticated unix socket
(`evo/admin_client.try_daemon_call`) **first**, with TCP as the migration-window
fallback — completing more of Phase E.3 instead of inventing a parallel service
token whose file ACL would just re-create the trust question. Requests arriving
over the unix socket are exempt from the device-cookie gate (they carry stronger
auth: kernel-verified peer uid). Read-only liveness probes (`/api/health`) join
the exempt list so `health.py` / `diagnose/probes.py` keep working.

**What would change this:** a headless/kiosk deployment story where no terminal
exists at setup time. That topology doesn't exist today; if it appears, the
opt-out marker is the documented path.

---

## D2 (row 2.8) — Secrets at rest: file-vault (Fernet) is acceptable; Keychain stays opportunistic; plaintext is not acceptable

**Decision: Keychain-mandatory is rejected. The Fernet file vault is the accepted
floor for daemon-held secrets. Plaintext persistence (`network.json::github.pat`)
is removed.**

Why not Keychain-mandatory: the admin daemon is a headless LaunchDaemon running as
the `evolve` user with no GUI session — the macOS Keychain is routinely locked or
unavailable in that context (this is why the vault fallback exists at all). A
Keychain-mandatory policy would either break the daemon's secret access on every
boot or push operators toward keychain-unlock hacks worse than the vault.

What the Fernet vault honestly buys, given the machine key (`.machine-key`, 0640,
`evolve`-owned) sits on the same disk:

- **It does not** protect against a root-capable local attacker — they can read
  the machine key. Single-tenant assumption (threat-model §2) carries this, as it
  already does for `auth-profiles.json`.
- **It does** protect against the leak classes that actually occur: secrets in
  greppable config files, secrets swept into git-pushed backups (the
  public-repo-guard incident class), secrets in `/api/*` payload responses, and
  secrets in casual `cat network.json` over an operator's shoulder. It is also
  tamper-evident (Fernet is authenticated encryption).

This is recorded in threat-model.md §6.2 with exactly that framing — accuracy
over optimism. **What would change this:** Apple shipping a sanctioned headless
keychain story, or pods moving to hardware with a usable secure element.

---

## D3 (row 2.9) — Forge install exec: argv-vector + interpreter allowlist; denylist demoted to belt-and-braces; sandbox deferred

**Decision: parse `install_cfg.command` with `shlex`, refuse anything that needs a
shell, exec as an argv vector (`shell=False`) with the first token checked against
an interpreter/tool allowlist. The `_DENYLIST` regexes stay as a second layer.
A seatbelt sandbox is deferred.**

The denylist-in-front-of-`shell=True` shape is exactly the "shapeable by an
LLM-authored input" pattern that Phase 2.3 was built to kill in the review gate —
`$(…)`, `` ` `` , `${IFS}`, and base64-pipe shapes walk through regexes. Removing
the shell removes the entire bypass *class*: metacharacters become literal argv
bytes.

Evidence the allowlist is cheap: all 67 real `install_cfg.command` strings across
the gallery follow `[interpreter] [workspace path] [subcommand/args]` —
`python3`/`/usr/bin/python3`, `/bin/bash` + script, `pip`/`npm`, `cat`, simple
file utilities. Zero use pipes, substitution, or chaining. An LLM that genuinely
needs a compound command can emit a `.sh` script as a forge artifact (which goes
through script review) and invoke it — the *command string* never needs shell
features.

Sandbox (seatbelt / `sandbox-exec`) is deferred: deprecated-but-functional Apple
surface, high integration cost, and the argv change already removes the injection
class. Revisit if forge ever runs unapproved commands. `gallery.py`'s
`requirements.system[].check` probe (the other manifest-supplied `shell=True`)
gets the same argv treatment in the same PR — same class, same fix.

---

## D4 (row 2.7) — CSRF/Origin: scoped to browser-credentialed requests; state-changing GETs become POSTs

**Decision: three layers, enforced in `before_request`, exempting unix-socket
transport (peer-auth'd, not cookie-auth'd):**

1. **Host-header validation** on all requests (allowlist: loopback forms + the
   `adminBaseUrl` host) — kills DNS-rebinding, the one way a remote page can
   reach a loopback server.
2. **Origin/Referer same-origin check** on mutating methods when the header is
   present.
3. **Double-submit CSRF token** (readable cookie + `X-CSRF-Token` header added in
   the central `core/api.js` wrapper) required on mutating methods for
   cookie-authenticated requests. CSRF is an ambient-credential attack; requests
   that carry no device cookie have no ambient authority to ride, so token
   enforcement is scoped to where the cookie is the credential.

The two pairing-poll GETs (`/api/skills/install/{whatsapp,signal}/pair/<id>`)
write `openclaw.json` and kickstart gateways on a GET — they become POSTs (the
SPA poller is ours; no third-party caller exists). The Slack OAuth callback GET
stays: recon verified it already checks the `state` parameter against a
server-stored entry and rejects unknown/expired state; it gains a regression test
rather than a redesign.

---

## D5 (row 2.11) — peer_auth trust: `("evo",)` with a pre-separation fallback

**Decision: `DEFAULT_TRUSTED_USERS` narrows from `("evolve", "evo")` to
`("evo",)`. If the `evo` macOS account does not exist (pre-separation pod), the
resolver falls back to trusting `evolve` — preserving the migration window only
where the migration hasn't happened.**

Evo account separation shipped 2026-05-30; the dual-trust window was explicitly
temporary per `peer_auth.py`'s own decommission note. Keeping `evolve` trusted on
a separated pod means the admin daemon's own uid can call its own
member-tier-scoped routes — harmless today, but it's exactly the kind of
"harmless residual trust" that widens silently. threat-model.md §6.4 (which still
says the separation phases are "pending") is reconciled in the same PR.

---

## Sequencing & process

One PR per roadmap row, in this order: **2.10 → 2.11 → 2.8 → 2.9 → 2.7 → 2.6**
(mechanical sweeps first, the auth-default flip last so every supporting exemption
— socket transport, health probe, evo tool reroute — is already on main when
enforcement lands). Every PR ships with tests; security-behavior changes get the
two-pass review (build-agent self-review + independent reviewer). threat-model.md
sections are updated in the PR that changes the behavior they describe
(§6.1 → 2.6, §6.2 → 2.8, §6.4 → 2.11, §6.5 → 2.9).
