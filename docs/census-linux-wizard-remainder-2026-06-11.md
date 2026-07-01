# Census: setup-wizard macOS remainder under the Linux gate (Phase 8.3 / L3)

**Status:** census / planning input for round-2 GA "wizard-port" waves ·
**Date:** 2026-06-11 · **Roadmap:** Phase 8.3 (L3)
([design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md))

Scope: `packages/admin/evolve_admin/setup_wizard.py`'s interactive `run_setup`
flow (18 numbered steps) — what still does macOS-only things despite the
`platform_profile` / `get_isolation()` / `get_scheduler()` / `perms` seams and
the `EVOLVE_PLATFORM=linux` gate (`_resolve_platform_gate`,
[setup_wizard.py:3510](../packages/admin/evolve_admin/setup_wizard.py)) already
existing. Every claim is anchored to `setup_wizard.py:LINE` (or the helper
module it delegates to).

## 1. Summary

**The "5 steps" assumption in the brief is wrong in both directions — say so
plainly.** The Linux gating already landed (W5A) is *more* extensive than 5
steps: the platform gate, the sudoers writer (both command tables, incl. the
`setfacl` mask-repair grants), the prerequisites step (NodeSource/apt/ACL
tools), the npm-prefix logic, account creation (Steps 9/10 via
`get_isolation()`), and all path/daemon-dir defaults already branch through
`platform_profile`. But the macOS *remainder* is also concentrated in
**fewer** than 5 truly-blocking steps — most of the 18 steps are already
NEUTRAL or PORTED.

Of the 18 steps:

| Class | Count | Steps |
|---|---|---|
| **NEUTRAL** (Linux-safe as-is, or already via seams) | 11 | 1, 2, 3, 4, 9, 10, 11, 12, 13, 14, 16 |
| **PORTED** (already branches for Linux behind the gate) | 2 | 5, 8 |
| **TODO** (macOS-shaped, broken/dead under the gate) | 5 | 6, 7, 15, 17, 18 |

**Headline:** the GA wizard-port cost is **one real adapter (host-power /
`host_power.py`), one seam-routing fix (`mcp_service.py` off the direct
`LaunchdScheduler` onto `get_scheduler()`), one topology decision (HTTPS/PWA
Step 18 under the SSH-tunnel model), two copy/posture-doc steps (6, 7), and a
cleanup sweep of ~6 hardcoded `/Users/Shared/...` and `/Users/{u}/...`
f-string literals that are macOS-only but currently *latent* (they sit in the
network.json builder and the dead existing-primary branch, not on the hot
path).** None of the TODO steps requires a *new runtime seam* — they consume
existing ones (`host_power` gets a platform backend like the other adapters;
`mcp_service` already imports `JobSpec`/`render_launchd_plist`, it just needs
to go through `get_scheduler()` instead of `LaunchdScheduler` directly). Steps
17 and 18 additionally carry a **judgment call** (§4 open questions): a
headless VPS may not run them at all.

## 2. Per-step census

Line refs are `setup_wizard.py:LINE` unless another module is named.

| Step | Class | macOS mechanism + line refs | Linux shape | Size | Blocker |
|---|---|---|---|---|---|
| 1 Pod identity | NEUTRAL | Pure prompts (pod name, timezone): 4504–4518. No platform primitives. | None. | — | — |
| 2 Bot roster | NEUTRAL | OC-candidate discovery + `BotSpec` building: 4520–4676. `find_oc_candidates()` reads ports/users; comments say "macOS user" (4597, 4886) but the value is just the Unix login, POSIX-portable. | None (cosmetic comment only). | — | — |
| 3 Security config | NEUTRAL | `_run_security_config_step` (4680): backup-repo URL prompt + keystore. No platform primitive in the prompt path. | None. | — | — |
| 4 Admin user | NEUTRAL | `getpass.getuser()` default (4686–4692). POSIX-portable. On a VPS the operator account is `ubuntu`/`root` per design §1; the prompt already accepts any login. | None. | — | — |
| 5 Prerequisites | **PORTED** | `_check_prerequisites` (4697; def 3620) is fully profile-keyed: `get_profile().name` drives macOS-ver vs `_linux_release_prereq` (3663), Homebrew vs `/usr/bin` PATH (3636), Node 20 (brew) vs `_LINUX_NODE_MIN` (NodeSource) (3692), Apple-Silicon row macOS-only (3670), and the POSIX ACL-tools (`setfacl`/`getfacl`) prereq is Linux-only (3728–3735). NodeSource hint at 3574–3582. | Done. Validate in the e2e harness (already does). | — | — |
| 6 Host power & sleep | **TODO** | `_run_power_posture_step` (4722; def 3809) is hardwired macOS via `host_power.py`: `pmset -g custom` parse (host_power.py:75,108), `pmset disablesleep` (host_power.py:112), `pmset -c sleep 0 displaysleep 0` set (host_power.py:120), `sysctl hw.model` (host_power.py:44). No platform branch anywhere in `host_power.py` (`_PMSET`/`_SYSCTL` literals, host_power.py:26–27). Operator copy hardcodes "Mac"/"this Mac" (3829, 3842, 3866–3871). Note: prereq Step 5 *also* imports `host_power.is_apple_silicon` (3671) but only inside the `if macos` branch, so it's never reached on Linux. | New platform backend behind `host_power` (same adapter pattern as scheduler/isolation): Linux uses `systemd-logind`/`systemctl`'s sleep targets — but a headless VPS *cannot sleep*, so the Linux backend is mostly "report: not applicable, daemons always-on" + suppress the whole pmset offer. Cheapest correct shape: gate the step on `get_profile().name == "macos"`; on Linux render a one-line "always-on host (no sleep management needed)" `_skip`. Portable-laptop-Linux (home server lid) is the only real Linux sleep case → `systemctl mask sleep.target suspend.target` as the analogue, optional. | **M** | None hard. Topology: VPS never sleeps, so the *minimum* is hide-on-Linux (S); the laptop-Linux analogue is the M. |
| 7 Dedicated-host ack | **TODO** | `_run_dedication_ack_step` (4727; def 3876): informed-operator single-tenant ack; "records, never blocks". Need to read its body for Mac-specific copy, but structurally it's a prompt + a `host` dict field — the threat-model §2 single-tenant assumption is platform-neutral (design §1 carries it over verbatim). Likely the only macOS-shape is operator-facing wording ("dedicated Mac"). | Copy-only: parameterize the wording by `get_profile()` (the wizard already has the `_account_noun` idiom at 4750). The ack *content* gains the design §1 SSH-operator-on-VPS framing ("anyone who can SSH in is the operator"). No code-structure change. | **S** | None. Pure copy + the §1 threat-model paragraph (which is an L3 deliverable anyway). |
| 8 Install OpenClaw | **PORTED** (with one latent literal) | `_install_openclaw_npm` (4743; def 3936) is profile-keyed: `--prefix`/Homebrew-layout logic is macOS-only and "must NOT render on Linux" (3940–3975), NodeSource npm uses its default global prefix. **Latent:** the "already installed" probe at 4734–4736 hardcodes a Homebrew fallback PATH `shutil.which("openclaw", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin")` — harmless on Linux (the plain `shutil.which("openclaw")` finds NodeSource's first) but cosmetically macOS. | Essentially done. Optional: drop the Homebrew fallback PATH from the 4734 probe behind the profile, or leave it (it's a no-op on Linux). | **S** (optional) | None. |
| 9 Create bot accounts | NEUTRAL | Delegates to `_create_bot_account` (4753) → `get_isolation()` (def at 3020/3028/3047). Account-noun already parameterized ("macOS" vs "Linux", 4749–4750). The isolation seam owns `useradd`/`dscl`. | None — already via the seam. | — | — |
| 10 OC per-bot | NEUTRAL | Delegates to `_setup_oc_for_bot` (4761). Per-bot OC config write + ACL grant; the ACL/sudoers side is profile-keyed (setfacl grants at 2480–2494). Any residual `/Users/{bot}` literals inside the helper are the L2 path sweep's job, not L3 wizard-specific. | None at the wizard layer. | — | — |
| 11 Configure Telegram | NEUTRAL | Telegram BotFather token prompts + `_test_telegram_token` (4766–4820). Channel-agnostic network calls. **iMessage is not offered here** (design §8) — this step is Telegram-only, so no dead macOS-only affordance to hide. | None. (iMessage hiding, design §8, lives in the channel catalog / admin UI, not this step.) | — | — |
| 12 Security alert channel | NEUTRAL | Second Telegram bot token prompt (4822–4848). Same as Step 11. | None. | — | — |
| 13 Shared directory | NEUTRAL | `_ask(..., DEFAULT_SHARED_DIR)` (4852) → `DEFAULT_SHARED_DIR = CANONICAL_SHARED_DIR` (config.py:22), which is platform-keyed via `evolve_config`/`platform_profile` (config.py:17–22) → `/var/lib/evolve` on Linux. `setup_shared` (installer.py:34) creates the tree. The default is already correct under the gate. | None (default resolves via profile). Verify `setup_shared`'s chown/ACL path is profile-keyed in the L2 perms sweep — but that's not wizard-specific. | — | — |
| 14 Deploy Evolve | NEUTRAL (with latent literals) | Builds `network` dict + `save_network` + `_deploy_evolve` (4867–5003). Deploy goes through the seam stack. **Latent macOS literals in the dict:** `"rulesFile": "/Users/Shared/evolve/security_rules.json"` (4967) is a hardcoded macOS path, not `shared_dir`-derived; comment "Record macOS user" (4886). Also the early re-run probes hardcode `Path("/Users/Shared/evolve")` (4435) and `/Users/Shared/evolve/network.json` (4462). These are macOS-only but *latent* — on Linux `net_path` is already the Linux canonical, so the 4435/4462 probes just no-op (wrong-but-harmless: they'd miss a Linux repair-mode detection). The 4967 `rulesFile` literal is a real defect: it writes a `/Users/Shared/...` path into a Linux pod's network.json. | L2 path-sweep cleanup: derive `rulesFile` from `shared_dir` (5 chars), and route the 4435/4462 re-run probes through `DEFAULT_SHARED_DIR`. None blocks a *fresh* Linux install; 4967 produces a wrong stored path (the security_rules consumer would miss it). | **S** | None hard; 4967 is a correctness bug under the gate. |
| 15 Provision primary + sudoers | **TODO** | Mostly seam-routed: `_provision_evo_oc` (5051), `_provision_evo_account` (5066) / `_perform_evo_cutover` (5080), `_write_sudoers`/`_write_evolve_sudoers` (5130/5134, profile-keyed setfacl tables), `install_evolve_infra_jobs` (5179, scheduler seam). **macOS-shaped remainders:** (a) the keystore chmod uses `sudo /bin/chmod 600` (5040–5047) — fine via /usr-merge; (b) the **dead existing-primary branch** (5087–5125) hardcodes `Path("/Users/evolve/.openclaw…")` (5102–5106), `sudo /bin/mkdir` (5107), and `chown -R evolve:staff /Users/evolve/.openclaw` (5108–5111) — the `staff` group and `/Users/` are macOS-only; (c) Phase-E.2 evo-cutover comments/paths reference `/Users/evolve/` → `/Users/evo/` (5057–5085). The cutover helpers themselves need a Linux audit (the evo-account-separation ACL dance, design §4). | The hot path (dedicated branch) is seam-routed and largely works once L1/L2 land. The **dead existing-primary branch** (5087–5125) should be deleted (it's documented as dead code, kept "one release" per the 4664 comment) rather than ported — that erases the `/Users/evolve` + `staff`-group literals for free. The **evo-cutover** path needs the design-§4/§9 Linux audit (does a *fresh* Linux pod even need a staged cutover, or provision `evo` from day one? — §12 Q9 of the design doc). | **M** | Soft: depends on L2 (`perms.py` evo write-ACL) being done. The dead-branch deletion is independent and S. |
| 16 Verify | NEUTRAL | `verify_plugin_live(bot, port)` HTTP probe to `localhost:port` (5226–5247). Platform-neutral (loopback HTTP). | None. | — | — |
| 17 Claude Desktop / MCP Bridge | **TODO** (+judgment call) | `mcp_service.install` (5315) → **`mcp_service.py` imports `LaunchdScheduler` + `render_launchd_plist` DIRECTLY** (mcp_service.py:47), bypassing `get_scheduler()`; hardcodes `PLIST_PATH = /Library/LaunchDaemons/{LABEL}.plist` (mcp_service.py:58) and legacy `~/Library/LaunchAgents/` sweep (mcp_service.py:166–177); operator copy points at `~/Library/Application Support/Claude/claude_desktop_config.json` (5328) — an operator-*client* path. Wizard side is otherwise neutral (Tailscale hostname prompt, 5262). | Route `mcp_service` through `get_scheduler()` (it already builds a `JobSpec`, mcp_service.py:119 — the seam absorbs launchd vs systemd). The Claude-Desktop config path (5328) is the operator's *laptop*, platform-agnostic per design §1 ("operator client matrix") — leave as a printed hint. **Judgment call:** does a headless VPS pod even offer Step 17? The bridge binds `0.0.0.0:5051` for remote Claude Desktop, so it's *more* relevant on a VPS, not less — but it depends on Tailscale being present (the prompt already skips when blank). Keep the step; port the service installer. | **M** | The `mcp_service` direct-`LaunchdScheduler` coupling is the one place a non-wizard module bypasses the scheduler seam — it's an L1/L2 seam-discipline fix as much as L3. |
| 18 HTTPS on the LAN (PWA) | **TODO** (+judgment call) | `_run_https_phase` (5347; def 4300) → `enable_https_if_possible` (https_setup.py). Tailscale-cert based (platform-neutral mechanism), but the whole step assumes a **LAN/PWA-on-phones topology** (4300–4348, spec-pwa-phase0-https). Under design §1 the Linux/VPS model is **SSH-tunnel-to-loopback, admin UI bound to 127.0.0.1, no public bind** — "HTTPS on the LAN" is a different topology. Also the post-summary copy (5398–5408) hardcodes `mini` as the hostname default and an `ssh -L 5050:localhost:5050 mini` one-liner. | **Topology decision, not a port.** On a VPS the recommended path is the SSH tunnel (design §1) — Step 18's Tailscale-HTTPS path is *optional/orthogonal* (Tailscale clients exist for all OSes, design §1 lists it as a "nice middle ground"). Likely shape: keep the step (Tailscale HTTPS still works on Linux), but the wizard summary copy must derive the SSH target / hostname from `resolve_pod_context()` instead of hardcoding `mini` (5395–5404), and the §1 SSH-tunnel instructions become the primary path with per-client-OS variants (design §1, §12 Q11). | **S–M** | None hard (Tailscale path is platform-neutral). The decision is *product/topology*: is LAN-HTTPS even a VPS concern? → open question for the coordinator. |

### Cross-cutting latent literals (not a step, but L3/L2 scope)

These are macOS-only path literals inside `run_setup`'s body that are *latent*
(off the fresh-install hot path) — they don't block a Linux install but write
wrong values or skip Linux-side logic. Grouped here so a single sweep clears
them:

- `_shared_dir_early = Path("/Users/Shared/evolve")` — 4435 (re-run detection)
- `Path("/Users/Shared/evolve/network.json").exists()` — 4462 (pre-v0.3 repair probe)
- `"rulesFile": "/Users/Shared/evolve/security_rules.json"` — 4967 (**writes a macOS path into a Linux network.json — real defect**)
- existing-primary dead branch `/Users/evolve/.openclaw…` + `evolve:staff` — 5102–5111
- `mini` hostname default + `ssh -L … mini` summary copy — 5395–5404 (should come from `resolve_pod_context()`)

## 3. Proposed wave grouping (round-2 GA wizard-port)

Clustered by **shared seam / blast-radius**, each wave one-PR-sized. Flow-rule
applied: sized by test-blast-radius, ratchet/lint changes kept separate from
behavioral migrations, and the seam-discipline fix (W2) split from the
copy/topology work (W4) even though both touch "Step 17/18", because they fail
differently.

| Wave | Contents | Steps | Seam / dependency | Size | Notes |
|---|---|---|---|---|---|
| **W1 — host-power backend** | Add a platform backend to `host_power.py` (mirrors the scheduler/isolation adapter pattern): macOS = today's `pmset`/`sysctl` verbatim; Linux = "always-on, no sleep management" report (+ optional `systemctl mask sleep.target` for laptop-Linux). Gate `_run_power_posture_step` (3809) on profile; parameterize Step 7 dedication-ack copy (3884–3915) by `get_profile()` + fold in the design §1 SSH-operator framing. | 6, 7 | Self-contained; no dependency on L1/L2. Golden-test the Linux "skip" path. | **M** | Independent of the adapter work — can land first or in parallel. Steps 6+7 share "host-posture copy" blast-radius. |
| **W2 — mcp_service onto the scheduler seam** | Reroute `mcp_service.py` from direct `LaunchdScheduler`/`render_launchd_plist` (mcp_service.py:47,58) onto `get_scheduler()` (it already builds a `JobSpec`, line 119). Generalize the `/Library/LaunchDaemons` + `~/Library/LaunchAgents` paths via `platform_profile.daemon_dir`. | 17 (installer) | **Depends on L1** (`SystemdScheduler` must exist). This is a seam-discipline fix — it's the last module bypassing `get_scheduler()`; arguably belongs with L1's "zero direct scheduler call sites" gate, not L3. | **M** | Split from W4 (the Step-17/18 *copy*) — this is a behavioral migration with its own e2e (install the bridge unit on Ubuntu), W4 is docs/copy. |
| **W3 — wizard path-literal sweep** | Clear the cross-cutting latent literals (§2 box): 4435/4462 re-run probes → `DEFAULT_SHARED_DIR`; 4967 `rulesFile` → `shared_dir`-derived; **delete** the dead existing-primary branch (5087–5125, removes `/Users/evolve` + `staff`). Add to / ride the L2 `/Users/` ratchet lint. | 14, 15 | **With/after L2** (the `platform_profile` path sweep + ratchet lint). Same lines/discipline as L2 §6. | **S** | Bundle into L2's literal sweep rather than a standalone PR if L2 is still open (less churn, per design §6 "touch each site once"). Ratchet-lint change stays separate from the literal edits per flow-rule. |
| **W4 — topology copy + SSH-operator docs** | Step 18 (5347) + summary copy (5395–5408): derive SSH target/hostname from `resolve_pod_context()` (no hardcoded `mini`); make the SSH-tunnel the primary "open the admin UI" path with per-client-OS variants (design §1, §12 Q11); decide whether Step 18's Tailscale-HTTPS even renders on a headless VPS. Threat-model §1.1 SSH-operator variant + §2 note (the 8.3 exit criterion's doc deliverable). | 18 | **Depends on the §1/§12-Q11 product decision** (see open questions). Pure copy + docs once decided. | **S–M** | Docs-heavy; the only code is the `resolve_pod_context()` substitution in summary copy. Pairs with the L3 operator-client docs deliverable. |
| **W5 — evo-account-separation Linux audit** | Audit `_provision_evo_account` / `_perform_evo_cutover` (5066/5080) + the evo write-ACL dance (design §4) on Linux. Decide §12-Q9 (provision `evo` from day one vs staged cutover). | 15 (evo cutover) | **Depends on L2** (`perms.py` evo write-ACL backend). | **M** | Could merge into W3 if the team prefers one Step-15 PR, but the ACL audit has a different blast-radius (the proposal/signal store EACCES path, CLAUDE.md "post-evo-account-separation exception") than the literal cleanup — keep separate. |

**Wave count: 5.** W1 and W2/W3/W5 can proceed in parallel once their
dependencies (L1 for W2, L2 for W3/W5) land; W4 is gated on a product decision,
not code. The two genuinely-new pieces of work are **W1** (host_power backend)
and **W2** (the mcp_service seam reroute) — everything else is copy, literal
cleanup, or an ACL audit of already-seam-routed code.

## 4. Open questions for the coordinator / operator

1. **Does a headless VPS run Step 17 (MCP Bridge) at all?** The bridge binds
   `0.0.0.0:5051` for remote Claude Desktop, so it's arguably *more* useful on
   a VPS — but it's Tailscale-gated and the wizard already skips on a blank
   hostname. Keep-and-port (recommended, W2) vs hide-on-Linux? The `mcp_service`
   seam reroute is worth doing regardless (it's the last scheduler-seam
   bypass), so this only affects whether the *wizard prompt* renders.

2. **Does a headless VPS run Step 18 (HTTPS-on-LAN / PWA) at all?** Design §1
   says the VPS model is SSH-tunnel-to-loopback with no public bind; "HTTPS on
   the LAN for PWA-on-phones" is a different topology (LAN + mDNS + Tailscale
   cert). Options: (a) keep it — Tailscale HTTPS works on Linux and a VPS *can*
   serve a phone PWA over Tailscale; (b) hide on Linux and make the SSH tunnel
   the only documented path; (c) keep but re-frame copy around Tailscale-only
   (no "LAN"). This is the one true topology judgment call in the census.

3. **Step 6 host-power on Linux — skip entirely, or laptop-Linux analogue?**
   A VPS can't sleep, so the minimum is a one-line "always-on, N/A" skip (S).
   But a home-server Linux box (laptop lid, design §1's "home server rides
   along") *can* suspend — do we ship the `systemctl mask sleep.target` analogue
   (M) for that case, or document-only? (Mirrors design §12 Q3's distro-vs-
   capability framing: VPS-only vs any-Linux-host.)

4. **Step 15 evo account — provision from day one or staged cutover?** This is
   design §12 Q9, surfaced concretely by the census: the wizard's dedicated
   branch runs the full E.2.a/E.2.b cutover (5065–5086) on every fresh install.
   On a *fresh* Linux platform there's no pre-separation legacy to migrate from
   — provisioning `evo` directly (skipping the cutover dance) is simpler and
   removes the `/Users/evolve`→`/Users/evo` path-shuffle. Confirm before W5.

5. **Delete the dead existing-primary branch now (5087–5125)?** It's documented
   as dead ("effectively dead code now but kept for one release", 4664–4666) and
   it's the source of the `/Users/evolve` + `staff`-group macOS literals in
   Step 15. Deleting it is the cleanest port (W3) — but it's a behavioral
   deletion on the shared (macOS too) path, so it needs the "one release"
   grace-period check before removal. Is that release window elapsed?

6. **Should W2 (mcp_service seam reroute) be an L1 task, not L3?** It's the last
   module importing `LaunchdScheduler` directly instead of `get_scheduler()`
   (mcp_service.py:47). The 4.3-C "zero direct scheduler call sites" gate (design
   §11) arguably *should* catch it. If L1's scheduler gate is enforced by a
   `git grep` ratchet, `mcp_service.py` will trip it — pulling W2 forward into
   L1 rather than leaving it for the wizard-port wave.

7. **Operator-client / SSH-target copy (W4):** design §12 Q11 — is the
   per-client-OS tunnel-instruction copy + the `resolve_pod_context()`-derived
   hostname an L3 exit-criterion, or a post-spike docs task? The census found
   the hardcoded `mini` (5395) makes the current copy actively wrong on any
   non-mini host (macOS *or* Linux), so there's a case for fixing it independent
   of the Linux gate.

---

*Census method: per-step targeted reads of `run_setup` (no full-file read),
grep for macOS-divergent primitives (`launchctl`/`dscl`/`pmset`/`chmod +a`/
`/opt/homebrew`/`/Users/`/`/Library/LaunchDaemons`/Keychain/iMessage/Claude
Desktop), and seam-presence checks (`get_scheduler`/`get_isolation`/`perms`/
`platform_profile`). Ground truth over the brief's "5 steps" estimate: the
Linux gating already landed is broader than 5 steps (gate + sudoers + prereqs +
npm + accounts + path defaults), and the blocking macOS remainder is 5 steps
(6, 7, 15, 17, 18) of which only 2 (host_power backend, mcp_service seam) are
genuinely new work.*
