# Design: Phase 2 security hardening — admin-server auth (2.1) + static-gate hardening (2.3)

**Status:** design / awaiting decisions · **Date:** 2026-06-09 · **Roadmap:** Phase 2

These are the two design-heavy Phase 2 items. Both are real, both involve a genuine
product/risk tradeoff (not a mechanical fix), so this doc lays out options +
recommendation and surfaces the decision for the operator. See also
[docs/threat-model.md](threat-model.md) (item 2.4).

---

## 2.1 — Admin-server authentication

### Current state (verified)
- The Flask admin server binds `127.0.0.1` only ("never expose externally",
  `web/server.py:3`, `web/run.py:23`).
- `_before_request` performs **no authentication** — it only starts a timer. Every
  privileged route (deploy, config write, proposal approve, secret read) is reachable
  by anything that can connect to `127.0.0.1:5050`.
- Operators reach it remotely via an SSH tunnel over Tailscale; the PWA runs on
  phone/tablet/laptop; the model today is "UI access **is** the authorization layer."

### Threat
The single-tenant assumption (threat-model §2) says no *other human users* on the host.
But it does **not** cover a **compromised local process** — a malicious npm/pip dep in a
bot's workspace, a prompt-injected bot agent, or the `evo` agent itself — any of which
can `curl 127.0.0.1:5050` and get root-equivalent pod control + all secrets. Loopback
binding does not defend against same-host processes.

### Options
| Option | Defends local-process? | Persona friction | Notes |
|--------|:--:|:--:|-------|
| **A. Device-pairing token** | ✅ | low (pair each device once, like a Chromecast) | One-time code from `evolve-admin pair` / setup; long-lived per-device cookie; checked in `_before_request`. Fits the multi-device PWA model. |
| **B. Shared password/login** | ✅ | medium (login form, password mgmt) | Simpler to build; worse PWA UX; one secret to leak. |
| **C. Tailscale identity** (`tailscale whois`) | ❌ | none | Zero-friction, but a *local* process still hits loopback unauthenticated — doesn't address the actual threat. Good as a *second* factor for remote access, not the primary. |
| **D. mTLS / client certs** | ✅ | high | Too much friction for the "installs Plex" persona. |

### Recommendation
**Option A (device-pairing token)**, layered behind the existing loopback binding
(defense in depth). It's the only low-friction option that addresses the
compromised-local-process threat and fits the PWA-on-many-devices model. Pairing code is
shown once in the CLI; each device stores a signed token; `_before_request` rejects
unauthenticated requests with 401 (with a pairing-needed page for browsers).

### The decision for you
**Is the compromised-local-process threat in scope for v1, or do we accept loopback +
single-tenant as sufficient and spend the effort elsewhere?** If in scope → build A
(~3–4 days incl. the pairing CLI + PWA pairing flow + tests). If not → document the
acceptance in the threat model and defer.

---

## 2.3 — Static review-gate hardening

### Current state (verified)
- `review.py::evaluate_proposal` keys every rule on the **self-declared**
  `proposal.get("type")` (`review.py:267`); `_applies_to(rule, type)` gates whether a
  rule even runs (`review.py:324`).
- Rules are **regex `re.search`** over specific dict keys — path / value / target /
  content (`review.py:341-371`), defined in `security_rules.json` (`auto_reject`,
  `auto_flag`, `risk_scoring`).
- The real sandbox (Forge isolated-instance validation) was **removed April 2026**.

### Threat
An LLM-authored (or malicious) proposal can evade the gate two ways:
1. **Type-spoofing** — the dangerous-content rules only `applies_to: ["script_change"]`.
   A proposal that carries executable content under a different `type` (e.g.
   `config_change`, `workflow_change`) skips them entirely.
2. **Regex blind spots** — the network/exec patterns match literal `requests.get` /
   `curl` / `\bsudo\b`, but miss `httpx`, `socket`, `os.system("cur"+"l")`,
   `__import__`, base64-decoded payloads, etc.

### Options
| Option | Closes type-spoof | Closes obfuscation | Cost |
|--------|:--:|:--:|------|
| **A. AST-level checks** for any code-bearing proposal | — | ✅ (catches socket/os.system/exec/eval/`__import__`/base64-decode regardless of spelling) | medium |
| **B. Type-agnostic content rules** | ✅ | — | low |
| **C. Reintroduce sandbox** validation for high-risk classes | ✅ | ✅ | high |
| **D. Capability model** (proposals declare what they touch; applier enforces) | ✅ | ✅ | very high (redesign) |

### Recommendation
**B + A together**, plus a **red-team test suite** of crafted proposals that previously
passed. B closes the spoofing gap (apply content rules to every proposal that carries
executable content, not just `type==script_change`). A adds AST analysis so obfuscated
dangerous calls are caught regardless of spelling. This is a bounded, high-leverage
hardening that doesn't require standing the sandbox back up. Defer C (sandbox) until
proposal risk actually increases; note D as the long-term direction.

### The decision for you
**How far for v1: the bounded regex→AST + type-agnostic hardening (B+A, recommended,
~3–5 days), or reintroduce the isolated-instance sandbox (C, ~1.5–2 weeks)?** Given the
autonomous-apply blast radius, I recommend at least B+A now.

---

## Suggested sequencing
1. **2.3 B+A first** — it directly protects the autonomous-apply loop (the highest blast
   radius), and it's pure backend + tests (no UX surface). Dispatchable to a careful
   agent once the depth is chosen.
2. **2.1 A second** — touches the CLI + PWA pairing flow + every route; bigger surface,
   wants a canary. Best done deliberately on Fable.

Both are gated on the two decisions above.
