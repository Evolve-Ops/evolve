# Safe automatic sudoers install — CI-signed render + root verifier

Status: proposed (design only; no code)
Date: 2026-06-16
Aspect: META:deploy (with META:substrate security review)
Related: Option B (PR #2759), drift detection (PR #2951), the §4b grant that
started this (PR #2948)

## Motivation

`sudo evolve-admin refresh-sudoers` is **manual by design**. The `evolve`
service user is deliberately barred from installing `/etc/sudoers.d/evolve`
(**Option B, #2759**): a self-grant is a direct privilege-escalation path. We
do not automate it the naive way (a root daemon that renders + installs from
the deploy checkout) because **that is a root-escalation vector**:

- The deploy checkout `/Users/Shared/evolve-repo` is **evolve-writable** (the
  repo-puller pulls it as `evolve`; `setup_wizard.py` is `-rw-rw-r-- evolve`).
- `visudo` validates **syntax, not content**.

So a compromised `evolve` could rewrite `_render_evolve_sudoers()` to emit
`evolve ALL=(ALL) NOPASSWD: ALL`, the root daemon would install it, and the
attacker has root. What root buys beyond a compromised `evolve` (verified on
the reference pod): the operator's personal SSH key (`/Users/pod-admin-user/.ssh/
id_ed25519`, `0600 pod-admin-user` — `evolve` provably cannot read it), pivot via
`pod-admin-user ALL=(ALL) NOPASSWD: ALL`, unrecoverable root persistence that survives
the evolve-account remediation, and the ability to silently disable the audit /
verify / warden daemons. Option B is the line between a **contained, recoverable
Evolve-pod breach** and **operator-identity theft + undetectable root**. It is
worth keeping.

But manual install has a real operability cost: a future update that adds a
grant + dependent code can break silently if the operator does not know to
refresh. #2951 closed most of that (accurate, self-resolving, immediate-to-chat
drift Signal, fired at the next tick AND at promote-time via the hooks
subprocess). This spec is for operators who want install to be **truly
automatic** without weakening Option B.

## Goal & non-goals

- **Goal:** `/etc/sudoers.d/evolve` is installed automatically on update, while
  preserving the invariant that a compromised `evolve` cannot get an
  attacker-chosen sudoers file installed.
- **Non-goal:** removing the one-time operator bootstrap (a root action to
  install the verifier daemon + pin the public key). That is unavoidable and
  acceptable — the operator already does a root setup once.
- **Non-goal:** trusting `visudo` for safety. It is a syntax check only and
  stays as a belt-and-suspenders step, never the security boundary.

## Core idea

Authenticate the sudoers **content** as coming from a trusted source that
`evolve` cannot influence: **CI signs the canonical render; a root verifier on
the pod installs only content whose signature validates against a pinned public
key.** A tampered checkout fails verification → no install. `evolve` can neither
forge the signature (no private key) nor swap the pinned key (root-owned).

```
  CI (trusted, evolve cannot influence)          Pod (root verifier, evolve-immutable)
  ┌───────────────────────────────────┐          ┌─────────────────────────────────────┐
  │ render canonical sudoers (per      │          │ trigger (evolve MAY trigger — it     │
  │ platform), normalize env-specific  │  repo /  │ supplies no content)                 │
  │ bits, hash, SIGN hash with CI key  │ release  │   ↓                                  │
  │   → ship signature + normalized    │ ───────► │ verify signature vs PINNED pubkey    │
  │     template as release artifact   │ artifact │   reject if invalid → no install     │
  └───────────────────────────────────┘          │   ↓ valid                            │
            private key: CI secret only           │ resolve template → visudo → install  │
            public key: pinned on pod (root)       │   /etc/sudoers.d/evolve + marker      │
                                                   └─────────────────────────────────────┘
```

## Components

### 1. Trust anchor (keypair)
- **Private signing key**: lives only in CI secrets (GitHub Actions). Never on
  any pod, never in the repo.
- **Public verify key**: pinned on each pod, **root-owned, evolve-immutable**
  (e.g. `/usr/local/share/evolve/sudoers-verify.pub`, `0644 root:wheel`).
  Installed once during operator setup (`sudo evolve-admin setup` /
  `install-infra-jobs`). Rotating it is a deliberate root action.

### 2. CI signing step (release/tag time)
- Render `_render_evolve_sudoers()` for each platform profile (macOS, Linux).
- **Normalize** the one environment-specific input — the runtime-discovered
  `openclaw` path — to a placeholder token (`@@OPENCLAW_BIN@@`). Everything
  else in the render is deterministic (proven today by the byte-identity
  goldens in `test_sudoers_platform_profile.py`).
- Hash the normalized template; sign the hash. Commit
  `release-artifacts/sudoers/<platform>.sudoers.tmpl` + `.sig` to the repo (or
  attach to the GitHub release). The template is human-readable and diffable.

### 3. Pod root verifier-installer (new, evolve-immutable)
- A minimal root-owned launchd daemon (`ai.evolve.evolve.sudoers-verifier`,
  `UserName` root). **Its code is the crux**: it must NOT run from the
  evolve-writable deploy checkout. Install a self-contained copy to a root-owned
  path at operator-setup time; it is updated only through that same trusted root
  channel (see Open Questions on self-update).
- On trigger:
  1. Read `release-artifacts/sudoers/<platform>.sudoers.tmpl` + `.sig` from the
     deploy checkout.
  2. **Verify** the signature against the pinned public key. **Reject** on
     mismatch (a tampered checkout fails here) — emit a high-severity Signal and
     stop.
  3. Resolve the template: substitute the local `openclaw` path into
     `@@OPENCLAW_BIN@@`, **after validating it against a strict allowlist**
     (absolute path, `^/[A-Za-z0-9_./-]+/openclaw$`, must exist + be executable)
     so the substitution itself can't inject sudoers syntax.
  4. `visudo -c` the result (belt-and-suspenders), install
     `/etc/sudoers.d/evolve` (`0440 root:wheel`), write the
     `{shared_dir}/state/sudoers-installed.sha256` marker (#2951) so the puller
     drift check resolves.
- **Trigger** may be evolve-influenced (it supplies no content): e.g. WatchPaths
  on the deploy checkout's artifact, or a request marker the promote hooks /
  puller write. Worst case a compromised evolve triggers re-install of the same
  verified content — no escalation.

## Why this preserves Option B

| Attacker capability (compromised `evolve`) | Outcome |
|---|---|
| Rewrite `_render_evolve_sudoers()` / the template in the checkout | Signature no longer matches → verifier **rejects** → no install |
| Forge a signature | Needs the CI private key — not on the pod |
| Swap the pinned public key | Root-owned, evolve cannot write it |
| Trigger the verifier repeatedly | Re-installs the **same verified** content — no escalation |
| Tamper with the verifier's own code | Verifier runs from a root-owned copy, not the checkout (see Open Questions) |

The only way to get a malicious sudoers installed is to compromise CI or the
root-owned verifier/key — i.e. the boundary moves from "evolve" to "CI + pod
root", which is exactly the trusted set we already rely on for releases.

## Residual risks & mitigations

- **CI key compromise** → attacker can sign a malicious sudoers. Mitigate:
  store in CI secret manager (hardware-backed where available), restrict who can
  run the signing workflow, require it run only on protected/tag refs, rotate
  the key, log every signature. This is the same trust we already place in CI to
  ship code that runs as `evolve`.
- **openclaw-path substitution as an injection surface** → mitigated by the
  strict path allowlist before substitution (step 3.3). Alternative: sign
  per-platform **fully-resolved** renders for the known openclaw install
  locations, removing substitution entirely.
- **Verifier-code immutability** → the hardest part; see Open Questions.
- **Reproducible render drift** → if the pod's render ever diverges from the
  CI-signed template (a non-determinism we missed), the verifier rejects and
  the pod stays on the last good sudoers + fires the drift Signal. Fail-closed,
  not fail-broken.

## Phasing

- **Phase 0 (done):** #2948 (the grant), #2951 (drift detection: accurate,
  self-resolving, immediate-to-chat, fired at promote via the hooks subprocess).
  This already removes *silent* breakage — the interim Path-1.
- **Phase 1:** CI signing only. Produce + commit the signed template artifact.
  No pod behavior change; validate the artifact is reproducible against the
  goldens.
- **Phase 2:** Pod verifier in **observe-only** mode — verify + log what it
  WOULD install, never install. Soak across a few real grant-changing releases.
- **Phase 3:** Enable auto-install. Keep #2951's drift Signal as the backstop
  (it now resolves automatically the moment the verifier installs).

## Alternatives considered

- **Content-policy allowlist (Path-3):** root installer renders from the
  checkout but refuses to install unless every grant matches a strict policy (no
  `(ALL)` runas beyond a documented set, no `NOPASSWD: ALL`, command paths
  constrained). No keys to manage, but a policy/parser bug = root, and sudoers
  grammar (Cmnd_Alias, Runas_Alias, negation, env_keep) is gnarly. Signing is a
  cleaner trust model than content-policing.
- **Keep manual + #2951 (Path-1, current):** no automation; relies on the
  immediate operator-chat alert + self-resolving Signal. Zero new attack
  surface. Sufficient for "never silent"; chosen as the interim and a fine
  permanent answer if the team prefers no key management.

## Open questions

1. **Verifier-code immutability.** The verifier must not execute evolve-writable
   code. Options: (a) a tiny self-contained root-owned script snapshotted at
   setup; (b) the verifier binary itself is CI-signed and self-verifies before
   running; (c) ship the verifier as a separately-packaged, root-installed unit
   outside the deploy checkout. (a) is simplest; (b) is most robust but
   bootstrap-heavy.
2. **Key management & rotation.** Who holds the CI private key; rotation cadence;
   how a pod learns a rotated public key (a root action, presumably bundled into
   `evolve-admin upgrade`).
3. **Template vs fully-resolved renders.** Normalize the openclaw path (one
   template) vs sign per-platform fully-resolved renders (no substitution
   surface, but N artifacts).
4. **Trigger.** WatchPaths on the artifact vs a request marker from the promote
   hooks. The trigger is untrusted by design, so this is an efficiency choice.
5. **Is it worth it?** #2951 already removes silent breakage. Phase 1–3 add CI
   signing infra + a root daemon + key management to remove the *last manual
   command*. Decide whether that command is painful enough to justify the
   ongoing key-management burden, or whether the immediate-chat nudge is enough.
