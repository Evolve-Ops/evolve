# PWA Phase 0 §4.1 — HTTPS on the LAN Sub-Spec

**Date:** 2026-05-18
**Status:** Resolved (open questions closed 2026-05-18)
**Parent:** [spec-pwa-2026-05-18.md](spec-pwa-2026-05-18.md) §4.1
**Owner:** Evolve admin server + setup wizard

---

## 1. Why this sub-spec

Service workers — the foundation of PWA installability — require HTTPS or localhost. Today's admin server runs plain HTTP on `127.0.0.1:5050`, reachable from laptops/phones over Tailscale at `http://<host>.<tailnet>.ts.net:5050`. That works for the existing web UI but blocks PWA install.

This sub-spec specifies the move to HTTPS: the proxy/TLS choice, the cert path, the setup-wizard integration, the migration story for existing pods, and the scheme-heuristic fix surfaced by PR #1259.

---

## 2. Current state (from audit, 2026-05-18)

### Server bind
- Entry point: `packages/admin/evolve_admin/web/run.py` → `app.run(host=args.host, port=args.port)`.
- Default args: `--host 127.0.0.1 --port 5050`.
- LaunchDaemon plist (production: `ai.evolve.evolve.admin-ui`) passes the same `127.0.0.1:5050`.
- **No TLS, no `ssl_context`, no gunicorn, no reverse proxy.** Plain Flask dev server.

### Reverse proxy / cert infrastructure
- **Nothing.** No Caddy, no nginx, no `tailscale serve`, no `tailscale cert`, no certbot, no acme client. Fresh ground.

### `adminBaseUrl` plumbing (post-#1259)
- Read by `evolve_admin.config.resolve_admin_base_url()` with the precedence env-override → `network.adminBaseUrl` → derived `http://<hostname>:5050`.
- Written today **only by the Google OAuth setup flow** (`evo/wizard/engine.py:3039`). The main `setup_wizard.run_fresh_wizard()` does not write it.
- Validator (`_validate_admin_base_url`) accepts both `http://` and `https://`.

### Scheme-heuristic code smell (the #1259 finding)
Two functions decide scheme based on host shape:

```python
# handover.py:371
scheme = "https" if "." in host and ":" not in host else "http"

# audit_poller.py:899-900
scheme = "https" if "." in host and ":" not in host else "http"
```

Both extract `host:port` from `resolve_admin_base_url()` then **re-guess the scheme** instead of preserving the original. Consequence: an explicit `https://pod.local:5050` in `adminBaseUrl` silently downgrades to `http://pod.local:5050` in handover links and investigation-trail links.

### Setup wizard's Tailscale awareness
- Already prompts for `tailscale_hostname` at `setup_wizard.py:1176–1180` (for the MCP bridge URL).
- Stores it under `network.mcp_bridge.tailscale_hostname`.
- **Never linked to `adminBaseUrl`.** Wizard doesn't currently know "this pod is on Tailscale and the admin URL should be https://."

### Privilege model
- Setup wizard runs as `sudo evolve-admin setup`.
- Admin server runs as `evolve` user.
- Tailscale daemon on macOS runs as root after install; the `tailscale` CLI talks to it via a privileged socket. Most `tailscale` subcommands work for the user-shell that's logged in (or under `sudo` for setup-time use).

---

## 3. Key decisions

### 3.1 TLS terminator: `tailscale serve` vs Caddy

| Option | What it does | Pros | Cons |
|---|---|---|---|
| **`tailscale serve` (recommend)** | Built-in Tailscale feature; binds 443 on the tailnet IP, terminates TLS with a Tailscale-issued LetsEncrypt cert, proxies to `127.0.0.1:5050` | Single command; auto-renewing cert; no separate package install; one fewer process to monitor | Tightly Tailscale-coupled; harder to migrate off Tailscale later |
| **Caddy + `tailscale cert`** | Run Caddy locally as a reverse proxy; obtain cert via `tailscale cert <host>` | Portable across TLS sources; mature config language; can serve multiple hostnames | Requires installing Caddy via Homebrew; manual cert-renewal cron; one more daemon to keep alive |
| **Cloudflare Tunnel / ngrok** | Reverse tunnel via a SaaS relay | No need for Tailscale on phone | Public exposure; SaaS dependency; not the "private pod" story |

**Recommend `tailscale serve` as the default.** The audience is already on Tailscale (it's the path to the admin UI today), the user memory's "vigilant by default, friendly by design" pillar argues for fewer moving parts, and the renewal cron is gone for free. Caddy stays as a documented escape hatch for the rare user who wants to leave Tailscale.

### 3.2 URL shape after HTTPS lands

| | Today | After §4.1 |
|---|---|---|
| `adminBaseUrl` | `http://<host>:5050` (when set) or derived | `https://<host>.<tailnet>.ts.net` (no port — default 443) |
| Admin server bind | `127.0.0.1:5050` | unchanged — still loopback; `tailscale serve` proxies to it |
| External reachability | Tailscale routing to OS port 5050 | Tailscale `serve` on port 443 of the tailnet IP |

The admin server's own bind doesn't change. `tailscale serve` does TLS termination + reverse proxy in front of the loopback Flask server. No changes to `run.py`, `service.py`, or the plist.

### 3.3 Scheme-heuristic fix

**Replace the host-shape guessing with scheme-preservation.** Concretely:

1. Change `handover.pod_host()` and `audit_poller._pod_host_for_dashboard()` to return `(scheme, host)` tuples — both pulled from `resolve_admin_base_url()` via `urllib.parse.urlsplit`.
2. The two call sites (`build_handover_url` and `_build_investigation_trail_url`) consume the tuple directly: `f"{scheme}://{host}/handover/{token}"`.
3. Delete the `"https" if "." in host and ":" not in host else "http"` heuristic.
4. Existing operator overrides (`pod.public_host`, `tunnel.remote_host`) keep working but now require an explicit scheme — or fall back to `adminBaseUrl`'s scheme as a default if they're plain hostnames. Document this clearly.

This is a small, contained change. Lands first because it's correctness-not-feature work and unblocks the rest.

### 3.4 Setup-wizard integration

Add a new wizard phase: **HTTPS setup**.

Decision tree at the relevant wizard step:

```
Is Tailscale installed and signed in?
├── No → "We recommend running Evolve over Tailscale. Skip HTTPS for now;
│         you can re-run `sudo evolve-admin enable-https` later."
│         (Leaves adminBaseUrl as http://<host>:5050; server stays HTTP.)
└── Yes → Detect tailnet hostname via `tailscale status --json`
    │
    ├── Is HTTPS-cert provisioning enabled in tailscale admin console?
    │   ├── No → Show the one-line instructions to enable it
    │   │       (link to login.tailscale.com/admin/dns)
    │   │       Wait for confirmation, then re-check.
    │   └── Yes → continue
    │
    └── Run `tailscale serve --bg --https=443 http://127.0.0.1:5050`
        Write adminBaseUrl = "https://<host>.<tailnet>.ts.net" to network.json
        Verify reachability via a fetch from the admin server itself.
```

The "HTTPS cert provisioning" toggle in Tailscale's admin console is a real one-time per-tailnet step — needs operator action in a browser. Worth surfacing once and clearly; not something the wizard can do for them.

### 3.5 Existing-pod migration

Existing pods on HTTP keep working. To opt in:

```
sudo evolve-admin enable-https
```

Same logic as the wizard phase (decision tree above). Idempotent — re-running just re-validates and re-writes `adminBaseUrl` if needed.

### 3.6 Disable / rollback path

```
sudo evolve-admin disable-https
```

Reverts: `tailscale serve --https=443 off`, rewrites `adminBaseUrl` to `http://<host>:5050`. Useful for debugging and as a documented escape hatch for the rare user who hits a Tailscale cert provisioning issue.

### 3.7 What does NOT change

- Admin server bind stays `127.0.0.1:5050`.
- LaunchDaemon plist unchanged.
- All HTTP loopback patterns (the intentionally-not-migrated list from PR #1259) stay HTTP.
- Member-bot gateways unchanged.
- The `mcp_bridge.url` *value* is rewritten by `enable-https` (now `https://<host>/sse`), but the underlying MCP service does not need code changes — it's already served by the admin server at the same loopback port that `tailscale serve` proxies.

### 3.8 Atomicity in `enable-https`

The command does several distinct things — start `tailscale serve`, rewrite `adminBaseUrl`, rewrite `mcp_bridge.url`, verify reachability. If a later step fails after an earlier one succeeded, the pod ends up in a half-configured state.

Required behavior:

1. **Stage all changes in memory first** — compute the new `network.json` contents and the `tailscale serve` invocation, but don't apply yet.
2. **`tailscale serve` runs last** among the side-effects.
3. **`network.json` writes atomically** (temp file + rename, the existing CLAUDE.md pattern).
4. **Post-apply verification fetch.** Hit the new HTTPS URL from the admin server itself; expect 200.
5. **Rollback on verification failure.** Revert `network.json` (atomic rewrite) and run `tailscale serve --https=443 off` to clear the proxy config.
6. **Idempotent retry.** Re-running after partial failure picks up where it left off; no manual cleanup expected.

Same shape applies to `disable-https` in reverse.

---

## 4. Phased delivery

| Phase | Work | Estimate |
|---|---|---|
| **4.1.a Scheme-heuristic fix** | Replace the host-shape heuristic with scheme-preservation in `handover.py` + `audit_poller.py`; update unit tests | **1 day** |
| **4.1.b `enable-https` command** | New CLI subcommand: detect Tailscale state, run `tailscale serve`, update `adminBaseUrl`, verify; idempotent. Plus `disable-https` counterpart | **1–2 days** |
| **4.1.c Setup-wizard integration** | New phase in `run_fresh_wizard()` that offers HTTPS and runs the same `enable-https` logic for new pods | **1 day** |
| **4.1.d Docs + verification** | Operator docs (when to use HTTPS, what the Tailscale admin-console toggle is, how to rollback); end-to-end run on the test pod | **0.5 day** |

**Total: 3.5–4.5 days.** Matches main spec's 3–5 day estimate.

**Sequencing:** 4.1.a lands first (independent, fixes existing bug). 4.1.b and 4.1.c can run in parallel after 4.1.a since they share logic. 4.1.d after both.

---

## 5. Resolved decisions

All eight original open questions are closed. Decisions below.

**5.1 TLS terminator: `tailscale serve` (not Caddy).**
Single command, no separate cert-renewal cron, no extra daemon. The audience is already on Tailscale; the simplicity wins. Caddy stays as a documented escape hatch in §3.1 only — not wired into the wizard.

**5.2 Port 443.**
Standard HTTPS port matches what browsers expect. No port suffix in the visible URL; users don't have to type `:8443` anywhere.

**5.3 Old Tailscale versions — detect + refuse with upgrade message.**
`tailscale serve` shipped in v1.44 (~3 years stable). `enable-https` runs `tailscale version` first; on too-old, prints "Please upgrade Tailscale to v1.44+ (`brew upgrade tailscale`)" and exits cleanly. No automatic upgrade attempted.

**5.4 Admin-console HTTPS-provisioning toggle: defer, don't block, plus a persistent admin-UI banner.**
- The wizard should not pause for a browser-side click; that breaks the one-shell-session model.
- Skip HTTPS in the wizard if the toggle isn't enabled; pod stays on HTTP; web UI keeps working; only PWA install is blocked.
- Show a persistent banner in the admin UI while the pod is on HTTP: *"Pod is on HTTP — enable Tailscale HTTPS to install on mobile. [Show me how]"* Links to docs covering the admin-console toggle + the `enable-https` command. This is the "won't be forgotten" half.

**5.5 MCP bridge URL — rewrite to HTTPS in the same `enable-https` step.**
`tailscale serve --https=443 http://127.0.0.1:5050` proxies the whole admin server at the loopback port, so the `/sse` MCP path is automatically covered. The URL flips from `http://<host>:5050/sse` to `https://<host>/sse`. Same change reversed in `disable-https`. No MCP-side code changes needed.

**5.6 `tailscale serve --bg` for persistence.**
`--bg` registers the proxy with the Tailscale daemon, which already persists across reboots via Tailscale's own launchd job. No separate launchd plist needed on Evolve's side.

**5.7 Non-Tailscale pods — document only; no Caddy automation in v1.**
The Evolve onboarding assumes Tailscale (it's how the admin UI is reached remotely). LAN-only pods can still hit `http://<host>:5050` from a laptop on the same network — they just can't install the PWA on a phone. Acceptable v1 tradeoff. Document this in operator-facing docs: *"PWA install requires HTTPS; install Tailscale on the mini or configure Caddy yourself (see Caddy escape-hatch docs)."* Caddy automation in the wizard is a v2 if anyone asks.

**5.8 Test-pod migration order.**
Get `enable-https` working end-to-end on the test pod (team-bot-a-mini) before 4.1.c (wizard integration) lands. That verifies the command against real Tailscale-serve state rather than mocks. After 4.1.c lands, the next fresh-bot setup is the integration proof.

---

## 6. Out of scope

- **Caddy automation.** Documented in §3.1 as the alternative, but not wired into the wizard. Operator who wants Caddy installs it themselves.
- **Public reachability via Tailscale Funnel.** The parent PWA spec §3.4 covers it as opt-in v2.
- **Multi-host certs / wildcard certs.** Each pod has one hostname; this is a per-pod single-cert setup.
- **HSTS / HPKP / strict CSP.** Worth doing but each its own design decision; out of scope here.
- **TLS 1.3 / cipher tuning.** Defaults from `tailscale serve` are fine.
- **Mutual TLS (client certs) for admin access.** Authorization stays at the admin-UI layer; mTLS is a future hardening pass.
- **Replacing the loopback bind with a Unix socket.** Worth considering for performance/security but not necessary for HTTPS to work.

---

## 7. Acceptance criteria

For Phase 0 §4.1 to be "done":

- A pod on Tailscale, after `sudo evolve-admin enable-https`, is reachable at `https://<host>.<tailnet>.ts.net` (default 443) with a valid LetsEncrypt cert.
- `adminBaseUrl` in `network.json` is the HTTPS URL above.
- Handover links and investigation-trail links emit HTTPS URLs (scheme-heuristic fix lands first; this is automatic after 4.1.a).
- Service workers register successfully when the PWA work (Phase 1) lands — verified by Playwright on WebKit + Chromium (the test infrastructure from PR #1265).
- `disable-https` cleanly reverts.
- Existing pods that don't run `enable-https` continue to work unchanged on HTTP.
- The test pod runs over HTTPS end-to-end for at least 24 hours without operator intervention; cert renewal at next Tailscale poll succeeds.

---

## 8. Why this shape

- **`tailscale serve` over Caddy** — fewer moving parts, no separate cert-renewal cron, no extra daemon to monitor. The Plex-test audience already trusts Tailscale.
- **Loopback bind unchanged** — minimizes blast radius. The Flask server stays exactly as it is; TLS is added in front, not inside.
- **Scheme-heuristic fix lands first** — independent of HTTPS work, fixes a real existing bug, unblocks every URL emitter from doing the wrong thing.
- **Opt-in for existing pods** — no surprise migrations; operator runs one command when ready.
- **New pods get HTTPS by default** — first-time setup is the right place to ask the question; doesn't require a follow-on chore.
- **`enable-https` / `disable-https` symmetry** — idempotent, reversible operations are healthier than one-way doors.
