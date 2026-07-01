# Design: the multi-pod Evolve story — second installs, hub vs aggregation

**Status:** design / **awaiting discussion — SPEC ONLY, no build approved** ·
**Date:** 2026-06-11 · **Context:** raised while Phase 8.3
([design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md)) is in
execution — the Linux port is what makes a *second* pod (a Hetzner-class VPS
next to the mini) a near-term reality instead of a hypothetical.
**Updated same day** with first-pass owner feedback: "Pods" confirmed as the
user-facing word (definition recorded in §1), and the hub upgraded from a
switching layer to the long-term home for cross-pod *sharing* — apps, config
profiles, possibly credentials and user identity (growth path in §5; v1 scope
unchanged: switch + aggregate first). **Owner decision, second pass:** hub
over aggregation is settled, and not as a deferral — the expectation is that
aggregation never gets built; the *enhanced hub* (§5 + §6's federation shape)
is the better way to address everything aggregation would have accomplished.
**Third pass:** Q2 (bootstrap depth) resolved — a thorough set of
instructions is all v1 needs; the mini-driven remote-wizard variant is
recorded as a deferred enhancement (§2).

Two questions from the project owner, answered as decisions with alternatives:

1. Should the admin UI offer a "set up another Evolve on a VPS" flow, and at
   what depth (checklist+script vs provider-API provisioning vs docs-only)?
2. The sticky one: one Evolve managing bots **across** hosts (aggregation) vs
   a **hub** that switches between sovereign per-environment Evolves — and,
   honestly examined, whether the product pull (Diana-style cross-bot
   synthesis) eventually demands real aggregation, and what the minimal
   cross-pod *read* surface is that doesn't break pod sovereignty.

What this builds on — and the prior art it has to reckon with:

| Layer | Status | Bearing on this design |
|---|---|---|
| Linux port ([design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md)) | In execution (8.3) | The precondition. §1's remote-operator model (admin UI loopback-only, SSH tunnel or tailnet as the access path) is the access story every multi-pod decision composes with. A "second pod on a VPS" *is* a Linux pod — nothing in §2 below ships before 8.3. |
| Threat model ([threat-model.md](threat-model.md)) | Current | §7 scopes explicitly to single-machine pods: *"Multi-machine topologies add network-policy and identity-federation questions not addressed here."* §2's single-tenant assumption, the sudoers/ACL model (§1.3), and loopback-as-authz + pairing auth (§6.1, default-on since roadmap 2.6) are all **per-host**. Any design that crosses hosts must either stay outside these invariants or trigger the ground-up rewrite §7 warns about. |
| Multi-instance aggregation design ([design/multi-instance-aggregation.md](design/multi-instance-aggregation.md), 2026-05-05) | Proposed, never approved or built (`network.json::multiInstance` has zero implementation hits) | Prior art for exactly this territory, scoped to integration-state sweeps for a ~12–100-pod fleet operator. Its two foundational decisions — **A: push from pod to center, never pull; B: pod-local is source of truth, center is a read-only cache** — survive contact with the current threat model and are adopted by §4 below. Its scope (a new `evolve-central` service, fleet-sweep UI) is re-cut for the household scale that's actually arriving first. §4 records what supersedes what. |
| Manifest v7 cross-pod sharing ([spec-manifest-v7-2026-05-20.md](spec-manifest-v7-2026-05-20.md) §9.2) | Spec'd; within-pod sharing shipped, cross-pod in Slice 3 | The existing cross-pod precedent, and it's instructive: **artifact-shaped**. A Spec file moves between pods by export/import with mandatory re-review on arrival; no live connection, no shared identity, no network trust. §4's read surface follows the same grain. |
| PWA ([spec-pwa-2026-05-18.md](spec-pwa-2026-05-18.md)) | Shipped | Recorded non-goal: *"Generic multi-pod evolve.app PWA — each pod owns its own PWA install; no cross-pod chooser."* §3's hub respects this (the switcher is navigation between per-pod origins, not a merged PWA). |
| Install-base research ([research-platform-expansion-2026-06-10.md](research-platform-expansion-2026-06-10.md)) | Decision recorded | Linux VPS/cloud is the co-#1 OpenClaw cluster; Hetzner is the named community favorite. The mini+VPS pair is the canonical second-install shape. |

---

## 1. Who actually has two pods — and when

Worth thirty seconds of honesty before designing for it, because the two
personas pull in different directions:

- **The household operator (Marcus/Diana-adjacent).** One pod today. The
  realistic second pod is a VPS for always-on/headless workloads next to a
  mini that exists for iMessage and local data — or a pod at a second home.
  Scale: 2–3 pods, ever. What they need: easy second install, one place to
  see "are my pods okay", and ideally `evo, how are things?` answering across
  pods. They will never want fleet sweeps.
- **The service operator (Carla persona; the 2026-05-05 doc's audience).**
  Runs pods for clients. Scale: 5–50. What they need: the instance-summary /
  sweep views the old design drew, and eventually cross-pod *operations*.
  This is a real future audience but **not the one arriving with 8.3** — the
  first multi-pod operators will be us and household-scale early adopters.

The Diana persona's celebrated conversion moment (`evo, what's on this
week?`) is **within-pod** synthesis — one evo across many bots on one
machine. Her compartmentalization need (health never sees ventures) is an
argument for many bots in *one* pod, not many pods. Multi-pod Diana exists
(household pod + foundation pod), but she is one step removed from the
persona as written. This matters for §4: the product pull toward cross-pod
synthesis is real but smaller and later than it first appears, and it is a
*read* pull, not a control pull.

**What "pod" names (resolved, owner feedback 2026-06-11):** "Pods" is
confirmed as the user-facing word. Definition: *a designated group of
OpenClaw bots unified by some common element* — usually a host (the mini, a
VPS, a Linux box), possibly a purpose. The purpose reading implies a
variant worth flagging honestly: **multiple pods on the same hardware**.
Today pod == host by construction — one `{shared_dir}`, one `evolve`/`evo`
account pair, one admin port, one `network.json` per machine. Two pods on
one box is a *namespacing and packaging* question (suffixed service
accounts, distinct ports/shared-dirs/job labels), not a security redesign —
same-host pods share a kernel, so inter-pod isolation reduces to the
per-bot-user isolation that already exists — but nothing supports it today,
and multi-bot-in-one-pod compartmentalization already covers much of the
"different purposes" need at far lower cost. Recorded as an open question
(Q13), not designed here.

Decision frame used throughout: **ship for the household operator now;
leave the service operator a clean upgrade path; never foreclose it.**

---

## 2. Second-install provisioning — guided checklist + generated bootstrap; no provider APIs

**Recommendation: (a) — the admin UI (and docs) offer an "Add a pod" flow
that interviews the operator briefly, then emits a provider-neutral
cloud-init/bootstrap script plus a step-by-step checklist the operator
pastes into their provider's create-server page. Evolve never holds
provider credentials or touches payment. Full provider-API provisioning
(b) is rejected with recorded revisit triggers; docs-only (c) is the floor
that 8.3 L3 ships anyway, and (a) is a thin increment on top of it.**

### The safety posture, stated as the design constraint

Evolve's identity is approval-first and credential-minimal: the operator
approves every change; secrets the platform doesn't strictly need, it
doesn't hold. A provider API token (Hetzner's is project-scoped and can
create *and delete* servers, i.e. spend money and destroy data) is the
single most consequential credential an operator could hand us — custody of
it buys convenience on a flow the operator runs **once or twice a year**.
That trade fails on its face. Account creation, payment, and server
lifecycle stay in the operator's hands at the provider's own console; Evolve
supplies everything that makes those five minutes foolproof.

### What the flow does

1. **Interview (one screen):** which provider (Hetzner first-class, "other
   cloud-init provider" generic), region/size guidance (community sizing:
   Hetzner CX-class shared vCPU is the named reference), the operator's SSH
   **public** key (offer to read `~/.ssh/*.pub` paste), and a pod name.
2. **Emit two artifacts:**
   - A **cloud-init user-data file** that does the boring, deterministic
     prep: create the operator login account with the supplied public key
     (password auth off), `apt` prereqs, Node 24 via NodeSource (per
     upstream recommendation), clone the Evolve repo **pinned to the
     current release** (the `evolve-stable` semantics from the deploy
     pipeline, not origin tip), and drop a message-of-the-day with the
     single next command.
   - A **checklist** rendered next to it: create the account → add payment
     (their console, their card) → create server → paste user-data → wait
     for boot → `ssh <user>@<ip>` → `sudo evolve-admin setup`.
3. **Hand off to the wizard.** The script bootstraps **to the wizard, not
   past it.** The setup wizard collects things that must not be defaulted —
   the dedication acknowledgment is a recorded informed choice
   (threat-model §2), channel/consent decisions are the operator's, and
   pairing mints the admin-auth key. A "fully provisioned, already running"
   pod would mean we answered those questions for them.
4. **Close the loop with §3:** the final checklist step is "add this pod to
   your switcher" — the Add-a-pod flow is also the hub's registration path.

What the script must **never** contain: secrets of any kind. The SSH public
key is public; the new pod mints its own signing/admin-auth keys during
setup; pairing happens operator-side afterward.

One provider note for the threat model (one paragraph, not a section): on a
VPS, the hosting provider is in the trust base — console/rescue access is
root. That is the operator's hosting choice and it's the same posture every
self-hosted product carries; the docs say it plainly rather than implying a
VPS pod is identical to a box in your closet. (8.3's threat-model §1.1
SSH-variant paragraph is the natural place for one added sentence.)

| Alternative | Trade-offs |
|---|---|
| **(a) Checklist + generated cloud-init (recommended)** | No credential custody, no payment path, no per-provider integration treadmill — cloud-init is the lingua franca (Hetzner, DigitalOcean, Vultr, OVH all accept user-data verbatim), so one generator covers the field with provider-specific *copy* only. Failure modes are legible: if boot prep fails, the operator is looking at their provider's console, not filing an Evolve bug. Cost: the operator does five manual minutes at the provider console, and we maintain the script against distro/provider drift (cheap: it's ~40 lines targeting Ubuntu 24.04, which 8.3 already pins). |
| (a+) Same, plus an optional `hcloud` CLI variant | For operators who already use Hetzner's CLI: emit the equivalent `hcloud server create --user-data-from-file …` command to run **on their machine with their token**. Custody stays with the operator; UX approaches one-command. Worth including as a copy-paste variant in the checklist — it's zero new mechanism, just a second rendering of the same artifact. Not the primary path because it assumes a CLI the Plex-test audience won't have. |
| (b) Provider-API provisioning (Hetzner first) | The best demo ("click → pod exists in 90 s") and the community favorite provider has a clean API. Rejected: token custody (create/delete/spend authority) directly contradicts the stated posture; payment implications make Evolve the thing that spends the operator's money; provisioning failures, quota errors, and provider ToS churn become *our* support surface; and it starts a per-provider treadmill (Hetzner → DO → Vultr → …) for a once-a-year flow. **Revisit triggers:** a managed/hosted Evolve offering ever becomes a product (different custody model entirely), or the service-operator segment (§1) materializes at a scale where pods are created weekly, not yearly. |
| (c) Docs-only | The floor, and 8.3 L3's docs deliverable already covers most of it (VPS install guide, tunnel instructions). Costs nothing but leaves the operator hand-assembling cloud-init or typing apt commands over SSH — exactly the "weekend sysadmin" experience Evolve exists to remove. Ship it first (it's free), then layer (a) on top. |
| (a++) Mini-driven remote wizard (considered in discussion, deferred) | The existing pod's UI/CLI drives the *remote* wizard over SSH as the operator's agent — the operator never leaves the mini's screen, but every recorded choice (dedication-ack, consent, pairing) is still answered by the operator, just relayed; the SSH key is the operator's own credential, so this stays on the locally-adjudicated side of the §4 cliff (the operator commands, not pod A's authority). Deferred per owner decision (2026-06-11): **a thorough set of instructions is all v1 needs.** Revisit if real second installs show the checklist isn't enough — the upgrade is additive on the same generated artifacts. |

**Resolved (owner, 2026-06-11) — v1 depth is (a) with the instructions done
properly.** "Thorough" is the product bar, not filler: per-provider copy
(Hetzner first-class), per-client-OS SSH instructions (macOS/Windows/Linux —
the same matrix 8.3 L3's operator-client docs commit to), the exact paste
targets in the provider console, what a successful boot looks like, and the
verbatim `ssh` + `sudo evolve-admin setup` + pair + add-to-switcher steps.
The checklist *is* the v1 feature; the (a++) remote-driven wizard is the
recorded later enhancement, not the plan.

**Where the flow lives:** both. The generator is one template + one form; render
it in the admin UI (the natural home — your first pod helps birth your second,
and the UI already has the wizard muscle) *and* as a public docs page for the
operator whose first pod will *be* the VPS. The admin-UI placement also makes
the §3 hand-off (register the new pod in the switcher) seamless. (Open Q1.)

**Sequencing:** strictly after 8.3 L3 (there is nothing to provision before a
Linux pod can run). Effort ≈ 1 session for the generator + checklist UI +
docs page; the script body is the §8.2 **pre-wizard prep** (not the wizard's
install steps — that correction is load-bearing).

### 2.1 Concrete M1 artifacts (build detail)

Grounded in the §8.2 seam: cloud-init does the pre-wizard prep; the wizard owns
the install. Three artifacts plus an optional CLI variant.

**(a) The cloud-init user-data template.** The generator fills five placeholders
— `<OPERATOR_USER>`, `<SSH_PUBKEY>` (the operator's public key, verbatim —
public, no secret), `<REPO_URL>` (read from the generating pod's
`network.json::pod.repo_url`, `config.py:733` — never hand-typed), `<PINNED_SHA>`
(the generating pod's current commit, per §8.2's "embed at generation time"),
`<POD_NAME>` — and emits:

```yaml
#cloud-config
# Evolve pod bootstrap — pre-wizard prep only. Contains NO secrets.
# Hands off to `sudo evolve-admin setup`; it never answers the wizard's questions.
users:
  - name: <OPERATOR_USER>
    groups: [sudo]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']      # operator's own login; tighten post-setup if desired
    ssh_authorized_keys:
      - <SSH_PUBKEY>
ssh_pwauth: false                          # key-only; password auth off
package_update: true
packages:
  - git
  - acl                                    # setfacl/getfacl — the wizard probes for these
  - curl
  - ca-certificates
  - build-essential
runcmd:
  # Node.js 24 via NodeSource (the prereq the wizard probes but does not install)
  - [ bash, -c, "curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && apt-get install -y nodejs" ]
  # uv (the venv / interpreter contract — see [[analyzer-packaged-compat-editable]])
  - [ bash, -c, "curl -LsSf https://astral.sh/uv/install.sh | sh" ]
  # Clone the deploy checkout at the pinned commit, then build the venv so `evolve-admin` exists
  - [ bash, -c, "git clone <REPO_URL> /var/lib/evolve/repo" ]
  - [ bash, -c, "cd /var/lib/evolve/repo && git checkout <PINNED_SHA>" ]
  - [ bash, -c, "cd /var/lib/evolve/repo && ~/.local/bin/uv sync" ]
  # Expose the console script so `sudo evolve-admin setup` resolves (match the deploy checkout's exposure)
  - [ bash, -c, "ln -sf /var/lib/evolve/repo/.venv/bin/evolve-admin /usr/local/bin/evolve-admin" ]
  # MOTD: the single next command
  - [ bash, -c, "echo 'Evolve pre-staged. Next:  sudo evolve-admin setup' > /etc/motd" ]
```

Two grounded notes:
- **Repo cloneability / the no-secrets line.** The clone assumes a
  publicly-cloneable release repo — true at public launch (`palace-games/evolve`,
  [[project_repo_and_launch]]). For private/pre-launch testing the operator
  injects their own clone credential **out-of-band in their own SSH session**,
  never in the generated user-data — preserving the no-secrets invariant.
- **`evolve-admin` on root's PATH** is the seam's load-bearing detail:
  `sudo evolve-admin setup` must resolve to the venv entry point (the venv python
  *is* the interpreter contract — [[sudo-subprocess-interpreter-must-be-venv]]).
  The §8.2 seam test asserts `sudo evolve-admin --version` succeeds
  post-bootstrap. Confirm the exact exposure mechanism (symlink vs console-script
  install) against how the existing deploy checkout puts `evolve-admin` on PATH
  today, and match it rather than inventing.

**(b) The interview form (one screen).** Fields, with style-guide width classes
(§9.2):

| Field | Control | Width | Notes |
|---|---|---|---|
| Provider | `<select>` — Hetzner (recommended), DigitalOcean, Vultr, OVH, Other cloud-init | `input-w-md` | Drives per-provider checklist copy only; we provision nothing |
| Size/region guidance | read-only text per provider | — | Hetzner CX-class shared vCPU is the named reference; operator picks in their console |
| SSH public key | `<textarea>`, **paste** | `input-w-text` | Paste-only + format-validate (`ssh-ed25519`/`ssh-rsa`/`ecdsa-…`, single line). The "read `~/.ssh/*.pub`" convenience is **not** offered — the admin server runs as the `evolve` user and cannot read the operator's home (CLAUDE.md runtime context); the key is public, so paste is the honest path |
| Pod name | `<input>` | `input-w-md` | Becomes the new pod's `networkId` **and** the switcher peer `name`; validate non-empty, hostname-safe charset |

Output: the cloud-init file (copy/download) + the rendered checklist. Plus the
**§3 hand-off, sequenced honestly:** the new pod's `adminBaseUrl` is **not known
at generation time** (it boots later), so the form **pre-creates a draft peer**
`{name: <pod name>, adminBaseUrl: null}` and the checklist's final step is
*"setup prints your pod's address — paste it here to finish the switcher
entry,"* which fills in `adminBaseUrl`. No guessing a URL that doesn't exist yet.

**(c) The checklist (per-provider + per-client-OS).** Rendered beside the
artifacts:
1. **At the provider (Hetzner first-class):** create account → add payment
   (their console, their card) → create server: image **Ubuntu 24.04**, type
   **CX-class** → paste the user-data into the **"Cloud config"** field
   (DigitalOcean/Vultr/OVH label it "User data") → create.
2. **Confirm boot:** the server reaches the MOTD and cloud-init finished without
   error — the legible-failure posture (a prep error shows in the provider's
   console, §2).
3. **Reach the pod (per-client-OS SSH):** macOS/Linux native and **Windows 10+
   PowerShell** all run `ssh -L 5050:127.0.0.1:5050 <OPERATOR_USER>@<ip>`
   verbatim; Tailscale is the platform-agnostic alternative. **Reference, do not
   duplicate:** the basic command lives in `docs/getting-started.md` and the full
   tunnel wizard in `docs/help/maintenance.md`; the per-client-OS matrix is
   currently only *sketched* in design-linux-port §1/Q11 and is **not yet a
   standalone doc** — M0/M1 docs work consolidates it (dependency flagged).
4. **Run the wizard:** `sudo evolve-admin setup` → answer the operator-only
   questions (dedication-ack, consent, pairing) → pair the browser → **finish
   adding the pod to your switcher** (paste the printed `adminBaseUrl` into the
   draft peer from (b)).

**(d) hcloud CLI variant (Q3 = include).** For operators who have Hetzner's CLI,
a copy-paste rendering run **on their machine with their token** (custody stays
operator-side):

```
hcloud server create --name <POD_NAME> --type cx22 --image ubuntu-24.04 \
  --ssh-key <key-name> --user-data-from-file ./evolve-pod.yaml
```

Zero new mechanism — the same `evolve-pod.yaml` from (a), a second rendering of
the artifact.

---

## 3. Hub vs aggregation — hub-first; the switcher is nearly client-side

**Recommendation: build the hub — a pod switcher over N sovereign Evolves —
and do not build cross-host aggregation. The hub is a `peers` list in
`network.json` plus a switcher in the admin UI chrome; auth stays exactly
the per-pod pairing model (the browser already holds one device cookie per
pod origin — N pairings coexist naturally because cookies are per-origin).
Aggregation in the "one Evolve managing bots across hosts" sense is
rejected as a ground-up security redesign with no current demand; §4
defines the bounded read surface that answers the product pull instead,
and §6 records what real aggregation would cost so the rejection is
legible.**

Per owner feedback, the hub's ceiling is deliberately higher than its v1
floor: the switcher is the *entry point* of the pod-boundary surface, and
§5 records the growth path — apps, configuration profiles, credential
distribution, user identity, unified reports — all reachable from this
foundation without crossing the line drawn below. v1 scope stays
switch-and-aggregate.

### Why the line sits exactly here

Every load-bearing security invariant Evolve has is **per-host**:

| Invariant | Scope today | Under aggregation |
|---|---|---|
| Single-tenant assumption (threat-model §2) | One host, no untrusted local users | Becomes "N hosts and the network between them" — a different model, not an extension |
| Loopback-as-authz + pairing auth (§6.1, default-on per 2.6) | The admin server never leaves `127.0.0.1`; reaching it via SSH/tailnet *is* the operator credential | A controlling Evolve must reach *remote* admin surfaces with *stored* credentials — the exact thing the loopback design exists to avoid |
| sudoers + ACL + per-bot users (§1.3–1.4) | Kernel-enforced on one box; `evolve`'s narrow grants are meaningful because the OS adjudicates them | No kernel spans hosts. Cross-host "evolve may do X on pod B" must be reinvented in application code — identity, authn, authz, audit, all from scratch |
| Pod = blast-radius unit | A compromised pod is one machine | A controlling hub is a skeleton key to every pod it manages; compromise fans out |

The hub keeps all four because it adds **no server-side surface at all** on
any pod: it is the operator's browser visiting N loopback/tailnet origins it
was already allowed to visit, carrying N cookies it already holds.
Aggregation breaks all four at once. There is no half-step between them on
the *control* axis — which is why the productive half-step lives on the
*read* axis (§4).

### Hub design sketch

- **Registry:** `network.json::peers` — an operator-edited list of
  `{name, adminBaseUrl}` (the same `adminBaseUrl` every pod already
  publishes for its own PWA/tailnet access; `networkId` is the natural
  default name). Low-sensitivity data: names and URLs, no tokens. Every pod
  can carry the list, so **every pod is the hub** — there is no special
  node to keep alive, and a dead pod never strands the switcher. v1 keeps
  the lists manually maintained per pod (drift between two pods' peer lists
  is cosmetic); the §2 Add-a-pod flow appends to the list on the pod that
  ran it.
- **UI:** a pod switcher in the sidebar (current pod name + chevron →
  sibling links). Clicking a sibling navigates to its `adminBaseUrl` — a
  different origin, where that pod's own pairing gate applies. First visit
  to an unpaired sibling lands on its pairing screen; `sudo evolve-admin
  pair` over SSH to *that* pod is the recovery path, unchanged. The hub
  never transports, stores, or proxies credentials.
- **Liveness dots — v1 ships without them.** Showing a green/grey dot per
  sibling requires the browser on pod A's origin to read pod B's
  `/api/health` — a cross-origin fetch that fails without CORS headers.
  `/api/health` is already the auth-exempt liveness probe, and a CORS
  allow scoped to *that route only, for Origins in the peers list* leaks
  nothing beyond "up + version" — but it is the one place the hub would
  touch server code, so it's an explicit v1.1 option rather than a silent
  inclusion (Open Q5). v1 is links-only.
- **PWA:** unchanged — each pod owns its PWA install (the spec'd non-goal
  stands). The switcher renders inside each pod's UI; "switching" from a
  PWA is opening the sibling origin. That is **not** uniformly "a normal
  navigation": desktop Chrome/Edge keep you in the installed window (adding
  a thin URL/back chrome strip on the sibling origin), but **iOS standalone
  PWAs often bounce a cross-origin navigation out to Safari / an in-app
  browser**. A single installed PWA + the sidebar switcher **is** the
  cross-pod toggle — a second install is optional, not required — and a
  merged multi-pod PWA remains a non-goal. See "PWA switching — the
  single-install decision" below for the full settled decision.
  **Distinct surface — the per-pod browser PWA
  ([spec-pwa-2026-05-18.md](spec-pwa-2026-05-18.md)) is the single-pod
  everyday surface** (install *one* pod's dashboard as a desktop/phone app),
  and it is complementary to — not in conflict with — the native **Evolve
  Pods** app this design owns (M2.5; the native answer to the one-app question
  resolved below). The PWA spec's "no native shell" non-goal was scoped to that
  per-pod effort; this design adds the multi-pod native shell on top.
- **Version skew note:** with N pods each following its own release pointer
  ({shared_dir}/release.json, per the deploy-resilience spec), siblings can
  legitimately run different Evolve versions. The hub doesn't care (it's
  navigation), but it's the first place an operator will *see* skew —
  worth a version string on the switcher rows the day §4 digests exist
  (they carry `evolve_version` already in the 2026-05-05 snapshot shape).

| Alternative | Trade-offs |
|---|---|
| **Hub via `peers` in network.json (recommended)** | Sovereignty preserved by construction; ~1 session (config field, sidebar component, docs); works the day a second pod exists; degrades gracefully (a stale URL is a dead link, not an outage). Cost: no fleet view, no cross-pod data — that's §4's job, deliberately separated. |
| Browser-only hub (localStorage/bookmarks, no config field) | Even cheaper — arguably the operator's bookmark bar already is this. Rejected as the *product* answer: per-device, invisible to the Add-a-pod flow, and unsharable between the operator's laptop and phone. The peers field costs almost nothing more and gives the switcher a home both render from. |
| Hub as its own tiny app/site (a "pod chooser" page) | A new deployable with no pod to live on, or a static page holding URLs — worse than putting the same list in every pod's existing UI. Rejected. |
| Aggregation (one Evolve, N hosts) | Rejected for now — see §6 for the honest bill of materials and the revisit triggers, recorded so this is a decision, not a flinch. |

**Sequencing:** the hub is useful the moment a second pod exists; ~1 session,
independent of §4. Natural pairing: ship it in the same wave as §2's
Add-a-pod flow, post-8.3.

### PWA switching — the single-install decision

Settled with the operator (M2): **the single installed PWA plus the sidebar
switcher IS the cross-pod toggle.** Installing a second pod's PWA is
*optional* (an operator who lives in one pod and visits another occasionally
never needs it), not a requirement of the design.

**Cross-origin UX caveat — and why we lean into it rather than hide it.**
Each pod is a separate origin (required by the per-host security model above —
loopback/tailnet authz + per-origin pairing cookies), so navigating to a
sibling is a genuine cross-origin navigation, and browser behaviour varies:

- **Desktop Chrome/Edge (installed PWA):** stays in the PWA window but paints
  a thin URL/back chrome strip on the sibling origin — itself a visible
  "you've crossed to another pod" cue.
- **iOS standalone PWA:** frequently *bounces* a cross-origin navigation out
  to Safari or an in-app browser rather than keeping it in the standalone
  window. This is OS behaviour the switcher cannot override.

This variance is precisely why we **do not** try to fake a seamless tab bar
across pods — a faked bar would shatter the moment iOS kicked the operator out
to Safari. The honest design treats the cross-origin chrome (URL strip / Safari
hand-off) as the intentional **"viewing a sibling pod"** cue: because every pod
renders its own switcher as `EVOLVE OPS · <its-name>`, the identity label
visibly changes to the destination pod's name on arrival, so the operator
always sees which pod they are on — no cross-host call, no shared chrome, no
spoofing.

**A true unified native tab bar across pods would require a native shell
(Electron/Tauri)** — a standing non-goal (see `docs/spec-pwa-2026-05-18.md`
§12, ~line 502: "Native desktop shell (Tauri/Electron) … Not v1"). **Deferred.**
Revisit trigger: **3+ pods in regular rotation OR daily iOS-bounce friction**
reported by the operator — at which point the menubar-presence / native-shell
trade reopens and carries the cross-pod tab bar with it.

#### The one-app question, re-opened and resolved (owner, 2026-06-18)

The operator re-raised the obvious user-facing wish: *"a PWA is, from the
user's standpoint, just a place you go to manage your pods — can it be ONE
app that toggles among them, not one install per pod?"* The felt need,
pinned down: **one icon (not N), no iOS Safari-bounce on switch, and a truly
seamless tab feel** — explicitly **not** unified cross-pod push (which would
have dragged in centralized notification fan-in; it's out of scope).

The honest technical fork has exactly three live shapes, because **web origin
isolation is the same property that makes the hub secure** (loopback-as-authz
+ per-origin pairing cookie ⇒ zero new server surface). Any *web* app that
truly spans pods must break out of that isolation:

| Shape | Delivers the felt need? | Cost |
|---|---|---|
| **(a) Single web PWA at a neutral hub origin + client-held per-pod tokens** | Partly — one icon, no nav-bounce (you cross-origin *fetch*, never *navigate*); but it cannot deliver true *seamless tabs* across origins | Each pod adds **CORS + bearer-token auth**, which **permanently softens loopback-as-authz** (a token in the hub origin's `localStorage` is JS-readable and cross-origin-sent — an XSS at the hub origin commands *all* pods); **HTTPS on every pod** (mixed-content blocks an HTTPS shell calling `http://mini`); and the **neutral origin itself** is either a third-party static host (supply-chain skeleton key) or one pod (not neutral, single point of failure). iframes don't rescue it — cross-site cookies are blocked, so you're back to bearer tokens with worse UX. |
| **(b) Reverse-proxy hub** (`hub/podA`, `hub/podB`) | Yes, but | **Rejected** — the proxy holds credentials to command every pod = the skeleton-key node §6 forbids. Crosses the aggregation cliff. |
| **(c) Native shell** (Tauri / Capacitor) | **Yes — fully.** True native tabs; each webview keeps its **own origin's pairing cookie**, so the security model is *untouched* | A native build + **distribution** (easy on desktop; iOS means App Store / Apple Dev account or sideload). |
| **(d) Status quo: per-pod PWA + switcher** (shipped, M2) | Partly — "one place, toggle among pods" on desktop | iOS standalone PWA *bounces to Safari* on a cross-origin switch. Zero new surface. |

**Resolved: (c) is the long-term one-app answer; (d) holds until the trigger
fires; (a)/(b) rejected.** The reasoning, recorded so it isn't re-litigated:

- The felt need (one icon, no bounce, **seamless tabs**) is exactly what a
  native shell delivers *better* than any web option — and a native shell
  **preserves per-origin pairing** (each webview authenticates to its own pod
  as the browser does today), so we pay in *packaging*, not in *permanent
  auth-model softening*. For a security-first product that currency choice is
  the whole point.
- The web-hub (a)'s only edge over native is "web-only, sooner" — and that
  edge evaporates once *seamless tabs* is the bar, because a web PWA **cannot**
  truly deliver peer tabs across origins anyway. It would buy a lesser UX at
  the price of softening the model forever. Not worth it.
- Neither (a) nor (c) is worth building **at 2 pods today**: the shipped
  switcher (d) already gives "one place to manage your pods" on desktop, and
  the iOS bounce at 2 pods is a paper cut. So the standing decision is **keep
  (d) now, build (c) when the existing trigger fires** (3+ pods in rotation OR
  daily iOS-bounce friction). This *is* the §12 native-shell revisit path —
  now with an explicit "this is the chosen cross-pod one-app experience, and
  here is the felt need driving it," so the milestone arrives pre-justified.
- **The one thing that would reopen (a):** if the felt need ever narrows to
  *iOS, one icon, no bounce, and soon* — there, native-iOS is the most onerous
  path of all (App Store for a self-hosted personal product), and (a) becomes
  the pragmatic answer despite its architectural cost. Today's felt need
  (seamless tabs included) points the other way.

**Update (owner, 2026-06-18, second pass — building it):** with a real 2-pod setup imminent (mini + Linux VPS) and the need scoped to **desktop** (one Finder icon, a simple tab between pods, no interconnection), the native-shell trigger is **pulled forward to 2 pods, desktop-first** — the iOS App-Store cost that gated it does not apply to a desktop build. Toolchain: **Tauri** (chosen over Electron for the lighter binary; Electron's no-new-language edge was noted but not decisive). Built as milestone **M2.5** (§8), in parallel with the Linux port (separate desktop artifact, no deploy.py dependency). iOS remains a later, separate decision.

### 3.1 Concrete M2 artifacts (build detail)

**(a) The `peers` schema.** A **top-level** `network.json` field (a sibling of
`networkId`/`adminBaseUrl` — peers are pod *identity/topology*, not a
pod-internal setting; this resolves the top-level-vs-`pod.peers` question):

```json
"peers": [
  { "name": "home-mini",   "adminBaseUrl": "http://home-mini:5050" },
  { "name": "vps-hetzner", "adminBaseUrl": "https://vps.example.ts.net:5050" }
]
```

- Each entry is **`{name, adminBaseUrl}` only** — names + URLs, **no tokens**
  (the v1 invariant; M3 grows the entry, v1 does not). `name` defaults to the
  sibling's `networkId`; `adminBaseUrl` reuses the exact resolved shape of this
  pod's own (`resolve_admin_base_url`, `config.py:626` — scheme+host[:port], no
  trailing slash).
- **Where it's added:** `_default_network()` gets `"peers": []` (`config.py:166`);
  absent/empty ⇒ single-pod, no switcher dropdown. network.json is a bare dict
  (`config.py:80`), so there's nothing to extend beyond the default factory + a
  light validator (each entry has both keys; `adminBaseUrl` parses as an http(s)
  URL). No TypeScript mirror needed.
- **Maintained manually in v1** (the §7 non-goal: no live sync); the M1 flow
  appends the one entry it creates. Drift between two pods' lists is cosmetic.

**(b) The read endpoint.** The SPA can't read `network.json` directly, so add a
small **`GET /api/peers`** returning what the chrome needs in one shot:

```json
{ "current": { "name": "home-mini", "version": "2026.0611.2718" },
  "peers":   [ { "name": "vps-hetzner", "adminBaseUrl": "https://…:5050" } ] }
```

`current.name` fills the **pod-identity gap the chrome has today** — the sidebar
never displays `networkId`, only the logo + version string (`index.html:79–81`).
This is a **read** route behind the existing admin auth — emphatically **not**
the M3 deposit route (that's the first non-loopback *write*, reviewed
separately as auditor-grade).

**(c) Switcher placement — sidebar header, not footer.** Pod identity isn't
shown anywhere today and the footer is a cramped flex row (`index.html:126–151`,
theme toggle at 149). Recommendation: render the switcher in the **sidebar
header**, turning the anonymous logo block (`index.html:79–81`) into
**"EVOLVE OPS · `<current.name>` ▸"**. This fills the identity gap for *every*
operator (single-pod included) and gives the switcher room the footer lacks.
Handler is window-exported per the SPA lesson (`window._podSwitcherClick`,
mirroring `window._pwaInstallPromptClick` in `pwa-install.js:29` —
[[feedback-admin-spa-new-onclick-handlers-eslint]]); style-guide + both-theme
check; the chevron uses `.expand-icon`, not a Unicode glyph (style-guide §9.13).

**(d) The four switcher states:**

| State | Trigger | Render |
|---|---|---|
| **Single-pod** | `peers` empty | Static label "EVOLVE OPS · `<name>`" — no chevron, no dropdown. (Pure win: identity now visible.) |
| **Multi-pod** | `peers` non-empty | Label becomes a button + `.expand-icon`; click opens a dropdown of siblings by `name`. Current pod is marked (check/highlight), not a link. |
| **Sibling click** | operator picks a sibling | Browser navigates to its `adminBaseUrl` (a different origin). **Paired** (holds that origin's `evolve_device` cookie, `admin_auth.py:33`) ⇒ lands in its dashboard. **Unpaired** ⇒ that pod's `/pair` gate (the `before_request` redirect, `server.py:909–923`). The switcher transports **no** credential — N per-origin cookies coexist by construction. |
| **Stale / dead peer** | URL unreachable | Just a dead link — v1 runs **no liveness probe** (Q5 = links-only), so the switcher never blocks on a dead peer. A version / up-dot column is reserved for v1.1 (CORS dots) / M3 (digest-fed version + last-heard). |

---

## 4. The product pull, honestly — and the minimal cross-pod READ surface

The question behind the question: Diana's `evo, what's on this week?` is the
conversion moment, and a hub of sovereign pods cannot answer it *across*
pods — switching origins is operator UX, not synthesis. Does that pull
eventually demand real aggregation?

**Answer: no — it demands a bounded, push-based, artifact-shaped read
surface, which is a different and much smaller thing. The security cliff in
this design space is not between "hub" and "aggregation"; it is between
READ fan-in and WRITE/control fan-out** — refined in §5, once richer
sharing enters the picture, to: **between crossings the receiving pod
adjudicates itself and crossings where a remote party's authority
executes.** Everything on the read side can be built without touching a
single §3-table invariant. Remote *control* triggers §6. Naming that line
is this section's job.

### What actually needs to cross pods, by demand

1. **"Are my pods okay?" / alerting** — already solved with zero new
   surface: every pod's notifier messages the same operator on their
   channel (Signal/Telegram). The operator's phone is the aggregation
   point today. This is worth stating because it removes most of the
   urgency: a second pod is *not* invisible the day it ships.
2. **Cross-pod glance (the "Pods" page)** — sibling cards: health, firing
   signal counts, spend, last-heard-from. The hub's missing data layer.
3. **Cross-pod evo synthesis (the Diana pull)** — `evo, how are things
   across my pods?` answered conversationally on the operator's primary
   pod.

(2) and (3) share one mechanism:

### The pod digest — recommended shape

**A pod periodically exports a small, redacted, signed digest artifact and
pushes it to the operator's designated "front pod"; the front pod stores
received digests in `{shared_dir}/peers/<pod_id>/` where its UI renders
sibling cards and its evo reads them like any other local file.** Adopted
verbatim from the 2026-05-05 design: **decision A** (push from pod to
center — the sending pod stays loopback-only, nothing reaches *into* it)
and **decision B** (pod-local is source of truth; the receiver is a
read-only cache that never writes back). Re-cut from that design: the
receiver is the operator's front pod, not a new `evolve-central` service,
and the payload is an operator-facing digest, not an integration-probe
fleet snapshot.

- **Transport & auth:** HTTPS POST to the front pod's `adminBaseUrl` over
  the tailnet (the path `https-setup.md` already establishes), bearing a
  per-peer token the front pod mints (`evolve-admin peers add` prints it;
  the operator pastes it into the sending pod — same key-exchange ritual
  as pairing, deliberately). The token authorizes exactly one thing:
  *depositing a digest under that peer's id*. It reads nothing, controls
  nothing, and revoking it costs the attacker who steals it the ability
  to… submit status reports. This is the inverse of remote-credential
  custody: the stored credential's blast radius is a write-only mailbox.
- **Payload (privacy floor, operator-configurable upward):** pod name +
  `evolve_version` + timestamp; per-bot one-line status; firing-signal
  counts by severity (titles optional — Open Q7); spend rollup vs cap;
  last-briefing-delivered marker. **Excluded by construction:**
  conversation content, credentials or masked tokens, file paths, member
  PII. The export is opt-in per pod (configuring the push *is* the
  consent), with the standard wipe path on the receiver — consistent with
  the observation-features opt-out norm.
- **Evo synthesis falls out for free:** digests are files in the front
  pod's shared_dir, so evo answers cross-pod questions through its
  existing local-file reach — no remote query, no new tool surface, no
  cross-host identity. The Diana pull is satisfied by the same artifact
  the Pods page renders. This is the v7-§9.2 grain again: pods exchange
  *artifacts* they chose to export; the consumer treats them as local
  data with provenance.
- **Staleness is data, not gating** (kept from 2026-05-05): a silent pod's
  card shows last-heard-from age and goes grey; the receiver never infers
  "unreachable means gone."

| Alternative read surfaces | Trade-offs |
|---|---|
| **Digest push to front pod (recommended)** | Smallest thing that serves both the Pods page and evo synthesis; reuses pairing-style key exchange, tailnet transport, signal-store rendering idioms; every §3 invariant intact. Cost: one new authenticated route on the receiving admin server (its first non-loopback-originated write — narrowly scoped, token-gated, body-size-capped, schema-validated; this *is* new surface and should be reviewed as such), plus a push job on each sending pod. |
| Pull (front pod queries siblings' APIs) | Requires every pod to expose authenticated read APIs beyond loopback and the front pod to *hold credentials that read other pods* — the first step down the custody slope, rejected in 2026-05-05 and re-rejected here. Push inverts the credential so theft is harmless. |
| Channel-level only (each pod messages the operator; no digest) | Zero new code and already true today — but it can't render a Pods page, evo can't synthesize over it, and N pods × M messages is noise the operator must merge by eye. Keep as the floor (and the day-one answer), not the destination. |
| Separate `evolve-central` service (the 2026-05-05 topology) | Right shape for the Carla/fleet scale (observer ≠ any observed pod; blast-radius separation; an operator who isn't any pod's primary user). Wrong-sized for 2–3 household pods — a new always-on deployable with its own auth story, for which the front pod substitutes fine. **Disposition: the 2026-05-05 doc's decisions A/B and snapshot/retention thinking are absorbed here; its central-service topology and sweep UI are deferred to the fleet tier, to be revived if/when that audience materializes (its Phase-2/3 sections remain the best sketch of that tier).** |
| Shared store (both pods sync state via cloud bucket / syncthing) | Third-party custody of pod state, sync conflicts, and an always-on dependency — rejected in 2026-05-05 for the same reasons; nothing has changed. |

**What the mailbox refuses to grow into:** no remote approvals (you cannot
approve pod B's proposal from pod A's UI — the digest may *tell* you
approvals are pending; you switch origins via the hub to act, which is
precisely the sovereignty boundary doing its job), no remote config reads,
no query/fetch/exec verbs of any kind hiding in the deposit route. Each of
those is a §6 item. What the mailbox *is* allowed to grow into is §5's
typed-artifact path — richer deposits, every one applied through the
receiving pod's own local gates. The route handler should be written so
the distinction is structural: one verb (deposit a typed artifact), no
others.

**Sequencing:** build after the hub, when ≥2 real pods exist and the
channel-level floor has been felt as insufficient — not speculatively.
Estimate ≈ 2 sessions (export job + receiver route + Pods page cards + evo
prompt-surface note).

---

## 5. The hub growth path — typed artifacts over the mailbox

First-pass owner feedback (2026-06-11): the hub should be more than a
switching layer — it is the natural home for *sharing* across pods. Apps
are the obvious one; configuration profiles ("set Pod 2 up the way Pod 1
is" — default model tiers and the rest of the pod layer); perhaps
credentials, shared across pods the way they're provisioned across bots;
perhaps a sense of a "user" that persists across bots and pods; down the
line, unified reports across pods. Agreed — and the design already
contains the skeleton that makes this growable without crossing §6's line.
This section records the growth path so M2/M3 are built with it in mind.

**The unifying rule: everything that crosses a pod boundary is a typed
artifact deposited into the receiving pod's mailbox, and nothing takes
effect until the receiving pod's own machinery — operator approval scaled
to the artifact's sensitivity — applies it locally. Deposit is the only
remote verb; apply is always local.** That keeps every §3 invariant intact
while admitting arbitrarily rich sharing — it is the §4 cliff restated for
writes: what's safe isn't only reads, it's anything *locally adjudicated*;
what's forbidden is remote authority. It is also exactly the v7 §9.2 grain:
cross-pod app sharing was *already* specified as export → import →
mandatory local re-review; the hub gives that spec transport and UX
instead of file-download-and-upload.

| Artifact type | Carries | Local gate on arrival | Notes / risks owned |
|---|---|---|---|
| **Pod digest** (M3, §4) | Health, spend, signal rollup | None — read-only data, rendered as-is | The v1 artifact; proves the mailbox. |
| **App Spec** (M4) | A v7 Spec per [spec-manifest-v7 §9.2](spec-manifest-v7-2026-05-20.md) | **Mandatory Forge re-review** (already spec'd: *"external source — review carefully"*) | Closest to free: v7 Slice 3's cross-pod export/import gains the hub as transport; the digest can advertise "pod A shared 2 apps". No new trust decision — §9.2 made it. |
| **Configuration profile** (M4) | The pod-layer config serialized: default model tiers/rungs, policies, conduct, notification prefs | Operator review, then applied through the pod's **existing config writers** (schema validation, canary rules — the same path a local edit takes) | Layering stays code-defaults ← pod ← bot (the product-defaults norm); a profile is the pod layer *as an artifact*, not live sync. Post-import drift is legitimate divergence, not an error. |
| **Credential envelope** (M5 — appetite confirmed 2026-06-24: rotation-ledger-led) | An operator-initiated copy of a credential (e.g. the Anthropic key), encrypted to the receiving pod's public key | Operator approval on the receiver; lands via the normal key-provisioning path | This is **distribution, not custody**: no pod can read another's keys; the sender retains no authority over the copy. The real product win is **rotation fan-out** — a ledger of "key X lives on pods A/B/C" turns rotation from N manual sessions into one prompt per pod. Owned risk: the same key on N hosts widens its blast radius — but that is the operator's *existing manual practice* (paste the same key into each wizard) made safer, audited, and rotatable. |
| **User identity / profile** (M5+ — registry-only first, confirmed 2026-06-24) | (a) an operator-maintained registry of known users (@handles, the approved-users surface) so the same person is one principal across pods; (b) optionally, per-user profile data | (a) operator review; (b) consent-gated per the observation-features norm — per-user opt-out and a wipe path on **both** ends | (a) is small and concretely useful (approved-user lists stop being re-typed per pod). (b) is privacy-load-bearing — profile data crossing machines needs its own consent design before any build. Both are distinct from operator SSO, which stays a non-goal: N pairings *is* the sovereignty model. |
| **Unified reports** (M4+) | Nothing new — richer digest payloads, accumulated | None | The reports page is a renderer over stored digests; falls out of M3's storage with no new boundary. |

Two things this table deliberately cannot smuggle in. First, no artifact
type carries executable intent that bypasses a local gate — a config
profile applies through the same validated writers a local edit would, a
Spec goes through Forge review, a credential waits for the receiving
operator's click. Second, the mailbox never grows a second verb: query,
fetch, and exec stay structurally absent. If a future artifact seems to
need a remote *read* ("pull pod A's user registry"), the answer is always
that pod A *pushes* it — decision A, applied uniformly.

**Build implication for M3:** the deposit route should accept a typed
envelope from day one — `{type, schema_version, payload, signature}` —
even though v1 accepts only `type: digest` and rejects unknown types
loudly. The marginal cost is one field; retrofitting typing onto an
untyped digest route is exactly the churn the plist-consolidation lesson
exists to prevent.

### 5.1 Bot migration — the composite artifact (move a bot across pods/platforms)

The heaviest crossing of all: **moving a whole bot from one pod to another — the
mini→VPS case is also a macOS→Linux port of that bot's state.** It is not a new
verb; it is the §5 rule at full stretch — a *bot bundle* deposited into the
receiving pod's mailbox and applied entirely through that pod's own bot-deploy
machinery. It **composes** the lighter artifacts: the bot's apps are M4 App
Specs (Forge re-review on arrival), its integration tokens are M5 credential
envelopes, its `network.json::bots[bot_id]` slice is an M4 config-profile
fragment — plus two things only migration carries: the bot's **live OpenClaw
runtime state** (workspace, memory, auth/exec config) and a **channel cutover**
so the bot is live on exactly one pod at a time. Because it carries
conversation-derived state and live credentials, it is also the most sensitive
artifact in the taxonomy — the one the §4 digest deliberately refused to carry —
so it is encrypted end-to-end to the receiving pod and rides the same
auditor-grade deposit route, never anything looser.

**What 8.3 supplies, and why migration depends on its GA.** The receiving side
of a mini→VPS migration *is* the Linux bot-deploy path. 8.3 already built the
platform seams — `SystemdScheduler`, `LinuxUserIsolation`, `LinuxPerms`,
`platform_profile` (in `packages/analyzer/runtime/`) — and the Linux install
path is CI-green behind the `EVOLVE_PLATFORM=linux` gate. What remains is
`deploy.py`'s bot-deploy macOS-literal remainder, which 8.3's GA migration is
working through (the platform-path lint freezes it shrink-only). **Bot migration
therefore gates on 8.3 GA being thorough enough that `deploy_bot` fully
materialises a bot on Linux** — it is the first feature that exercises the Linux
*bot* path end to end, not just the install path. (This corrects a tempting
misread that "Evolve has no Linux support": the seams exist; the bot-deploy
remainder is the in-flight GA work.)

**The plan — the migration procedure.** Seven operator-driven steps; every
cross-pod hop is a deposit, every mutation is local, and exactly one pod runs the
bot's gateway at any instant:

1. **Initiate + export on pod A.** Operator picks "migrate `<bot>` → `<peer>`."
   Pod A serialises the bot into a bundle. The **export half largely exists**:
   `retire_bot()` already produces a complete, integrity-manifested archive
   (`.openclaw/` + per-bot metrics + a `network.json` snapshot + a sha256
   `MANIFEST.json`, under `{shared_dir}/archived-bots/…`). Migration reuses that
   serialiser — but **quiesces rather than retires** (see step 7): the bot is
   stopped and held, not yet archived-and-removed.
2. **Deposit to pod B's mailbox.** The bundle is encrypted to B's public key and
   POSTed to B's deposit route as `type: bot_bundle` — the same write-only
   mailbox M3 builds, body-size-capped and schema-validated. At rest on B it is
   ciphertext; B's operator has not yet approved anything.
3. **Apply on pod B (local gates).** Operator switches to B via the hub, sees
   "incoming bot migration: `<bot>` — review & apply," and approves. B unpacks
   the bundle into `bot_home/.openclaw/`, then runs its **own**
   `add_bot()` + `deploy_bot()` — the identical path a local add-bot uses, so
   account creation, the systemd gateway, ACLs, and `network.json` registration
   are all B's platform-correct machinery. Apps go through B's Forge re-review
   (M4); credentials land via B's key-provisioning path (M5, or are re-entered by
   the operator in v1 — below). The gap to build is exactly this **"materialise
   from bundle" entry point** — there is no import path today; the wizard only
   calls `add_bot()` from an interview.
4. **Verify on pod B.** B brings the bot up and confirms health (gateway live,
   plugins loaded, a self-check turn); `deploy_bot`'s `dry_run` supports a
   rehearsal first.
5. **Cutover.** *The genuinely new, genuinely risky part* (its own paragraph
   below). A's gateway is already drained (step 1); B's now starts. The bot is
   live on B.
6. **Acknowledge back to A.** B deposits a small `migration_ack` artifact to A's
   mailbox (deposit-only, no remote command). The operator switches back to A.
7. **Decommission on A, with a rollback window.** Only after B is verified does
   the operator let A run `retire_bot()` for real — which archives the held bot
   (the archive *is* the rollback source and the audit record) and removes it
   from A's roster. Until then, A retains the quiesced bot so a failed migration
   is a one-switch rollback (restart on A), not data loss.

**What moves vs. what stays.** Bot-*intrinsic* state moves; pod-*operational*
record stays:

| Moves with the bot (intrinsic) | Stays on pod A (operational record) |
|---|---|
| Identity (`bot_id`), `.openclaw/` (openclaw.json, workspace, memory, plugins, exec-approvals), the `bots[bot_id]` config slice, `profiles/<bot_id>.md` (the distilled learned profile), apps (as Specs), integration creds (v2) | Proposals referencing the bot (A's adjudication history), signals, the raw `observations/<bot_id>/` log — preserved in A's `retire_bot` archive for audit/rollback. The bot starts fresh on B's arbiter, seeded by its profile |

(Whether the raw observation log *also* moves is Q16 — the profile is its
distilled form, so moving the profile alone preserves "what the bot learned.")

**The cutover / channel-exclusivity discipline.** A bot's integration tokens live
in one place — `auth-profiles.json` (`agents/main/agent/`, the per-bot canonical
credential store). A Telegram/Slack bot token admits **exactly one active
gateway**: two gateways on the same token means duplicate replies and
`getUpdates`/socket races. So migration is a *planned cutover with brief
downtime*, never a live handoff: **drain A (stop its gateway, quiesce new turns)
→ snapshot → transfer → apply + verify on B → start B → only then retire A.** The
stop-A-to-start-B window is downtime the channels buffer (Telegram resumes from
the update offset); the invariant is that the token is bound to one gateway at
every instant. This is the one place the whole multi-pod design touches live
messaging continuity, and the place a careless migration does visible damage
(double-binding), so the orchestration must enforce single-binding structurally
(Q17).

**Platform translation (macOS→Linux), the owned risk.** The `.openclaw/` payload
is platform-neutral (JSON, relative paths) and ports cleanly; B re-derives
`/home/<bot>` via the `bot_home`/`get_bot_user` seam, supplies the systemd
gateway and Linux ACLs through 8.3, and rewrites the `bots[bot_id]` `user`/port
for its own host. **The real hazard is absolute macOS paths embedded *inside* the
bot's own config or workspace** (a `/Users/<bot>/…` literal a bot wrote into
`openclaw.json` or a workspace file) — the export must scan and relativise these,
and the verify step (4) must catch a bot that boots but can't find a re-rooted
path. This is migration's analogue of §8.2's seam test.

**v1 vs. v2 — credentials decide the gate.**
- **v1 (the brain moves; creds re-pasted):** the bundle carries everything
  *except* secrets; the operator re-authenticates the bot's integrations on B
  through the existing per-pod wizard (the "paste the token into each wizard"
  floor). Buildable on **M3 + M4 + 8.3 GA, without M5.** The cutover discipline
  is still mandatory.
- **v2 (creds ride along):** adds **M5 credential envelopes** so nothing is
  re-pasted — the bot arrives fully credentialed. The convenience upgrade, gated
  on M5 appetite (Q11).

| Alternative | Trade-offs |
|---|---|
| **Bot bundle over the mailbox (recommended)** | Reuses the `retire_bot` serialiser (export) and `add_bot`/`deploy_bot` (apply); encrypted, integrity-manifested, operator-gated on arrival; rollback via the retained-then-archived source. Cost: the new "materialise from bundle" entry, the cutover orchestration, and embedded-path relativisation. |
| Manual rebuild on B (scp `.openclaw/`, hand-run add-bot, re-auth) | The floor — roughly possible today over SSH. No new mechanism, but error-prone, no integrity/encryption, loses apps-as-Specs and easily double-binds the channel. Keep as the "you can already sort of do this" fallback, not the product. |
| Reuse the nightly git backup (`backupRepoUrl`) as transport | The workspace already pushes to a git remote; B could clone it. Partial (no creds, no `.openclaw` config, no apps-as-Specs) and puts a third-party remote in the path — usable as the *workspace-transfer leg inside* the bundle, not the whole answer. |
| **Continuous bot mirror / replication across pods** | **Rejected (non-goal).** Live cross-host bot-state sync is the distributed-systems + remote-write surface §6 rejects; migration is a one-time move with cutover, not replication. |

**Owned risks, named:** double-binding a channel (mitigated by the cutover
invariant); embedded absolute paths (mitigated by relativise-on-export + verify);
the bundle's sensitivity — it carries conversation-derived memory and (v2) live
creds across machines, so it is encrypted end-to-end, wiped on both ends after
apply, and for a **multi-user bot** raises a member-privacy question (their data
moved hosts — notify/consent? Q18).

**Sequencing → M6.** After M5 (it subsumes credential envelopes) and gated on
8.3 GA (the Linux bot path) + M3 (mailbox) + M4 (apps); v2 also on M5. Estimate
≈ 2–3 sessions (export adapter over `retire_bot` + the materialise-from-bundle
import + cutover orchestration + a "migrate" affordance on the Pods page).
Build-detail (the bundle schema field-by-field, the import UI) is deferred until
M6 is approached — consistent with this doc's discipline of not specifying a far
milestone from single-pod extrapolation.

---

## 6. What real aggregation would cost — recorded so the rejection stays legible

**Decision (owner, 2026-06-11): hub over aggregation is settled — and the
working expectation is that aggregation is never built.** The enhanced hub
(§5's typed artifacts now, the federation shape below if the fleet tier
ever demands cross-pod operations) is the chosen way to deliver everything
aggregation would have accomplished. This section stays in the doc not as a
live option but as the bill of materials — so that any future "wouldn't it
be easier to just manage them centrally" conversation starts from the real
price.

"One Evolve managing bots across mini + VPS" means the admin plane on host A
holds authority over host B. The bill of materials, enumerated once so
nobody re-derives it optimistically:

1. **Cross-host identity & authentication** between daemons — mTLS or
   tailnet-identity binding, key lifecycle, revocation. (New subsystem.)
2. **Remote credential custody** — the controlling pod stores write-capable
   credentials for every managed pod; it becomes the skeleton-key node the
   loopback design exists to prevent. (Direct contradiction of §6.1's
   architecture, not an extension of it.)
3. **A remote execution path** — today every privileged mutation is
   kernel-adjudicated on the host where it happens (sudoers, ACLs, peer-uid
   socket checks). A cross-host command must re-implement that adjudication
   in application code on the receiving end, then prove it equivalent.
4. **Threat-model rewrite** — §7's exclusion inverts; network attackers,
   identity federation, and partial-compromise (one pod owned, N pods
   reachable) all enter scope. Phase-2-scale security work as the *entry*
   price.
5. **Distributed-systems semantics** — partial failure, retries, version
   skew between controller and controlled (the release pointer makes skew
   *normal*), audit across hosts.
6. **Loss of the blast-radius unit** — "a pod is one machine" stops being
   true, which quietly invalidates the honest framing the security story is
   built on.

And the payoff for that price, today, is zero demanded features: §4 covers
the read pull, §5 covers the sharing pull, and per-pod
approval-by-switching covers the remaining write cases at household scale.

**If cross-pod *operations* are ever demanded** (the realistic trigger:
Carla-scale operators feeling per-pod clicking weekly — e.g. "apply this
config change on 12 pods"), the recorded direction is **federation, not
aggregation**: the 2026-05-05 doc's Phase-3 sketch — a coordinator fans an
*operation request* out to each pod's own admin surface, each pod applies
its **own** local safety checks and its own operator-visible audit trail,
and the coordinator gets per-pod accept/reject. Pods stay sovereign; the
coordinator is a convenience, not an authority. That keeps items 2/3/6 off
the bill even in the fleet future. Anything beyond that is a different
product (the old doc's MQ4 multi-tenant SaaS note stands).

---

## 7. Non-goals

Recorded so scope creep has to argue with a list:

- **Provider-API provisioning / holding provider or payment credentials**
  (§2) — revisit triggers recorded there.
- **Cross-host aggregation / remote control of one pod from another** (§6)
  — including remote proposal approval, remote config write, remote
  deploy. Federation sketch recorded for the fleet future; nothing built.
- **A new `evolve-central` service** (§4) — deferred to the fleet tier;
  the 2026-05-05 doc remains its best sketch.
- **Merged multi-pod PWA / cross-pod chooser inside one PWA install** —
  the PWA spec's non-goal stands; the hub is cross-origin navigation.
- **Cross-pod bot-to-bot communication** — bots on pod A talking to bots
  on pod B is a different feature with its own consent/privacy design;
  nothing here creates a channel for it (the mailbox is operator-plane).
- **Multi-tenancy** — one operator owns all pods in scope here; different
  operators sharing infrastructure is a different product.
- **Operator SSO / identity federation across pods** — N pairings is the
  model; deliberately, because each pairing is each pod's own sovereignty.
  (Distinct from §5's user-registry artifact, which is *end-user* data
  shared by operator choice, not operator authentication.)
- **Live config sync between pods** (peers lists included) — sharing is
  artifact-shaped import with local review (§5), never continuous sync;
  peers lists stay manually maintained in v1.
- **Multiple pods on one host** — flagged in §1 as a namespacing/packaging
  question (Q13); nothing here designs or builds it.
- **Continuous bot mirroring / live replication across pods** — bot migration
  (§5.1) is a one-time move with channel cutover, never live cross-host state
  sync; the latter is the remote-write surface §6 rejects.
- **Splitting one bot across pods** — a bot lives on exactly one pod at a time
  (the channel-exclusivity invariant, §5.1); a "bot spanning two hosts" is not a
  shape the design admits.

---

## 8. Sequencing

Everything here gates on 8.3 (no second-pod story before a Linux pod
exists), and none of it competes with 8.3's own wave plan:

| Step | Contents | Effort | Gate |
|---|---|---|---|
| M0 — docs floor | VPS install guide + tunnel/tailnet copy — this is 8.3 L3's existing deliverable, listed for completeness | in 8.3 | 8.3 L3 |
| M1 — Add-a-pod flow (§2) | Interview form + cloud-init/checklist generator (admin UI + public docs page); hcloud variant if Q3 says yes | ~1 session | 8.3 GA'd enough that the script's install path is stable |
| M2 — Hub (§3) | `peers` field + sidebar switcher + docs; liveness dots only if Q5 says yes | ~1 session | second pod exists (even a test one) |
| M2.5 — Desktop shell (§3) | Tauri macOS app: one Finder icon, native tab bar across pods (hidden at ≤1 pod), one webview per pod keeping its own pairing cookie, offline-tolerant; shell-local pod list, zero new pod surface | ~1–2 sessions | 2 pods in rotation, desktop-first (native-shell trigger pulled forward from 3+/iOS-friction, owner 2026-06-18) — **SHIPPED 2026-06-19, PR #3031** |
| M2.6 — Desktop signing / auto-update / distribution | Code-sign + notarize the `.app`; `tauri-plugin-updater` self-update feed (chrome-only capability); release channel + macOS CI build job. Decouples shell-*self* updates from manual rebuilds and lets the app leave the build machine. Spec: [spec-multipod-desktop-distribution-2026-06-19.md](spec-multipod-desktop-distribution-2026-06-19.md) | ~2–3 sessions | **DEFERRED** — build only when the app is handed to another machine, shell-update cadence annoys, or Evolve goes public. NOT needed for single-operator use. Ordinary Evolve updates never need a rebuild (the webview loads each pod's live UI). |
| M3 — Pod digest (§4) | Export job + **typed** deposit route (§5's envelope from day one) + Pods page cards + evo surface note | ~2 sessions | ≥2 real pods and felt insufficiency of the channel floor — **not speculative** |
| M4 — sharing artifacts (§5) | App Specs over the mailbox (the transport for v7 Slice 3's cross-pod export/import) + configuration profiles + reports page over accumulated digests | ~2 sessions | M3 mailbox proven; v7 Slice 3 for the Spec type |
| M5 — sensitive artifacts (§5) | Credential envelopes + rotation ledger; user registry (profile data excluded pending its own consent design) | own mini-spec each | appetite RESOLVED 2026-06-24 (Q11 rotation-ledger-led + Q12 registry-only-first) — opt-in security features, not defaults; still sequenced behind M3+M4 |
| M6 — bot migration (§5.1) | Bot-bundle export (over `retire_bot`) + materialise-from-bundle import + channel cutover + Pods-page "migrate" affordance; v1 re-pastes creds, v2 rides M5 envelopes | ~2–3 sessions | 8.3 GA (the Linux bot-deploy path) + M3 mailbox + M4 apps; v2 also M5 |
| M7+ — fleet tier | Sweep/instance views, evolve-central, federation design pass | own design doc | Carla-scale demand, observed not predicted |

M1+M2 are natural to ship together (~2 sessions total) as "the multi-pod
wave"; M3 waits for lived experience with two pods — building it before
feeling the gap would repeat the old doc's own warning against designing
Phase-2 UI from single-pod extrapolation. M4/M5 are the §5 growth path in
demand order: apps and profiles serve the second-install moment directly
("make Pod 2 like Pod 1"), credentials and identity wait for explicit
appetite.

**Status & corrected gating (owner, 2026-06-11): build held pending operator
bandwidth — this is a spec-thoroughness phase, not a build phase.** The
operator's stated real-world order is the prerequisite chain that gates
everything: (1) **VPS Evolve support reaches GA** — the Multi-platform aspect's
8.3 Linux build, *not* this aspect's; M1 only *wraps* it (see the install-step
seam, §8.1); (2) **stand up a real Evolve-on-VPS test pod** — the same VM 8.3 GA
needs for its real-Ubuntu-VM pass doubles as hub pod #2, so it is stood up once
and serves both; (3) **exercise the hub** managing the mini + VPS pair. One
gating refinement survives this: only **M1** truly depends on 8.3 (its
cloud-init body *is* 8.3's install steps) — **M2 is macOS admin-UI +
`network.json::peers` config that builds and reviews independently of 8.3**, and
needs the second pod only to be *exercised*, not to be *built*. The "M1+M2 ship
together" framing above is an availability convenience, not a build dependency.

### 8.1 Validation & proof — the 2-pod test environment (no CI analog)

Unlike 8.3 (whose `linux-e2e` CI job is its proof surface), the multi-pod story
has **no CI analog**: "a VPS pod boots from generated cloud-init" and "switch
between two sovereign pods, two pairings coexisting" can only be proven on a
**live two-pod setup**. The spec names the acceptance tests so the build, when
it happens, has a falsifiable bar — and so the test environment is provisioned
deliberately, not improvised.

**Shared-VM efficiency.** The VPS pod that 8.3 GA needs for its real-Ubuntu-VM
manual pass (linux-port GA item 1) *is* hub pod #2. One provider instance
(Hetzner CX-class, the community reference) serves both validations; stand it up
once, under the Multi-platform aspect's GA pass, and inherit it here.

**M1 acceptance (Add-a-pod → a second sovereign pod):**
1. Operator runs Add-a-pod on pod #1 (the mini) → the form emits a cloud-init
   user-data file + a checklist.
2. Operator pastes the user-data into a real provider's create-server page
   (Hetzner first-class) → the server boots.
3. Boot reaches the MOTD's single next-command; `ssh <operator>@<ip>` succeeds
   on the supplied public key (password auth off).
4. `sudo evolve-admin setup` runs the wizard **to completion** — confirming the
   bootstrap-to-wizard invariant (the script stops *at* the wizard, never past
   it; dedication-ack, consent, and pairing stay the operator's recorded
   choices).
5. Pod #2 is reachable on its own loopback/tailnet `adminBaseUrl`; pairing mints
   its admin-auth key.
   - **Pass:** a second sovereign pod exists, provisioned only from the
     generated artifacts + the operator's own console/SSH actions, with **no
     secret of any kind** having passed through the script.

**M2 acceptance (Hub → N pods navigable from one sidebar):**
1. Pod #2 is added to pod #1's `network.json::peers` (the Add-a-pod final step
   appends it).
2. The sidebar switcher on pod #1 shows pod #2.
3. Clicking pod #2 navigates to its origin → its own pairing gate appears (first
   visit, unpaired).
4. Pairing pod #2 in the browser lands in its UI; switching back to pod #1 is
   still paired — the two per-origin device cookies coexist, no re-pair.
5. A stale/dead peer renders a dead link, not an outage (graceful-degrade
   check).
   - **Pass:** N sovereign pods navigable from one sidebar, N pairings
     coexisting, zero credential transport between origins.

**What the live setup validates that no CI could:** real provider cloud-init
acceptance, real first-boot, real cross-origin pairing-cookie behaviour, and
real tailnet reachability between two pods.

**M3+ (digest) validation is deliberately deferred** — its proof plan is written
when M3 is approved (gated on ≥2 real pods *and* felt insufficiency of the
channel-level floor), not speculatively here.

### 8.2 The M1 ↔ 8.3 install-step seam — the precise contract

A correction the build must internalise, surfaced by reading the actual 8.3
wizard (`setup_wizard.py`): **M1's cloud-init body is NOT a copy of 8.3's
install steps.** The Linux wizard does the privileged, stateful work — bot
accounts, ACLs, sudoers, systemd units, plugin build, shared-dir — but
*interactively*, interleaved with prompts the operator must answer (pod name,
roster, dedication-ack, comms, pairing). cloud-init cannot and must not
replicate that; doing so would re-answer the very questions §2 insists stay the
operator's. The handoff is to the wizard, and the wizard owns the install.

What cloud-init actually owns is the **pre-wizard host prep** — the
deterministic part the wizard *assumes was already done* or only *probes for*:

1. **Operator login account** with the supplied public key (password auth off).
   The wizard never creates the human's account; it assumes you SSH'd in.
2. **Prerequisites the wizard probes but does not install** (the step-1 prereq
   probe, `setup_wizard.py` ≈ L3623–3754): `apt` base packages, **Node.js 24 via
   NodeSource**, npm, the **`acl`** package (setfacl/getfacl), and systemd
   (present on Ubuntu). The probe *warns* on missing Node/ACL and *hard-fails*
   on missing systemd — so a cloud-init that under-prepares produces a **legible
   failure at `sudo evolve-admin setup`, on the operator's own console** (exactly
   the §2 failure posture: the operator is looking at their provider, not filing
   an Evolve bug).
3. **The repo clone — and the venv install that makes `evolve-admin` exist** —
   and here is a genuine gap. The wizard does **not** clone the repo; it assumes
   the code already sits at the deploy-checkout path (Linux `/var/lib/evolve/repo`,
   `platform_profile.py`) and that `evolve-admin` is already installed into a
   venv (the CLI is *in* the repo — `sudo evolve-admin setup` cannot run
   otherwise; the venv python is the interpreter contract, per
   `[[analyzer-packaged-compat-editable]]` / `[[sudo-subprocess-interpreter-must-be-venv]]`).
   **Neither the wizard nor design-linux-port §6 documents this initial
   clone+install contract today** — M1 is the first feature that needs it, so
   M1's spec must define it: git remote URL, the pin (below), and the
   `uv sync` / compat-editable install that puts `evolve-admin` on PATH.
4. **MOTD** with the single next command (`sudo evolve-admin setup`).

**The seam as a testable contract (recommended).** The wizard's step-1
prerequisite probe is already a structured list of what the host must satisfy.
Make *that probe* the single source of truth and assert, in an M1 unit test,
that the generated cloud-init installs every hard prerequisite the probe
requires. Then "M1's body matches 8.3's needs" stops being prose and becomes a
test that fails the day 8.3 changes its prereqs — the drift-proofing the
plist-consolidation lesson calls for, applied to the seam. (Open for review.)

**The clone pin — reconciling Q4 with what exists.** Q4 resolved to "pin to the
release pointer, stable tag embedded at generation time." Reading the code, the
release-pointer / `evolve-stable` machinery is a **canary-mode (Phase 7)
deploy-resilience feature — default-off and per-pod-local**; a brand-new VPS pod
has no prior `release.json`, and the remote carries no `evolve-stable` tag to
clone (the default install path follows `origin/main` tip via the repo-puller;
version is CalVer derived from HEAD by `deploy.py::_compute_version`). So for M1
the honest mechanism is: **cloud-init clones the repo and checks out the exact
commit captured at generation time** — the generating pod embeds the SHA/tag it
is itself running — which *is* the "embed at generation time" intent of Q4,
grounded in a git ref rather than a release pointer the fresh pod does not yet
have. The new pod adopts its own release pointer (or follows `origin/main` per
its chosen `pod.release.mode`) *after* setup — a post-setup concern, not a
clone-time one. **Open for review:** confirm "embed the generation-time commit
SHA" as the concrete pin (recommended), and whether M1 should refuse to generate
when the generating pod's checkout is dirty or ahead of `origin` (so the
embedded SHA is always fetchable by the new pod).

---

## 9. Open questions for discussion

**Resolved (owner feedback, 2026-06-11):**

- **Naming** (was half of Q6): "Pods" is confirmed as the user-facing word
  — a designated group of OpenClaws unified by a common element (hardware
  platform or purpose). Definition recorded in §1.
- **Hub scope**: the hub is more than a switching layer — it is the
  long-term home for cross-pod sharing (apps, configuration profiles,
  possibly credentials and user identity, unified reports). Growth path
  recorded as §5; first iteration stays switch + aggregate, as recommended.
- **Hub vs aggregation**: settled, beyond the doc's "rejected for now" —
  the working expectation is aggregation is *never* built; the enhanced
  hub (§5 artifacts + §6 federation if fleet-scale operations ever arrive)
  is the better way to address what aggregation would have accomplished.
  §6 is retained as the recorded price, not as a live option.
- **Bootstrap depth** (was Q2): a thorough set of instructions is all v1
  needs — bootstrap-to-wizard stands, with the checklist quality as the
  v1 feature (per-provider, per-client-OS, exact steps; see §2). The
  mini-driven remote-wizard variant (§2 row a++) is recorded as a deferred
  enhancement, additive on the same artifacts if real installs show the
  checklist falling short.

**Resolved (META coordinator session, owner decisions 2026-06-11, M1/M2 batch):**

- **Q1 — Add-a-pod entry point**: admin UI interview form **and** public docs
  page (both), per recommendation. The UI placement is what makes the §3
  switcher-registration hand-off seamless.
- **Q4 — Script pinning**: pin the clone to the release pointer (not origin
  tip) **and embed the current stable tag at generation time** (reproducible/
  auditable; the operator regenerates for a fresh pin), per recommendation.
- **Q5 — Hub liveness dots**: **links-only v1**; the CORS-on-`/api/health`
  dots are deferred to v1.1 — keeps the hub's "no new server surface on any
  pod" property intact for M2.
- **Q6 — Peers list placement**: `network.json::peers` ({name, adminBaseUrl})
  on every pod, per recommendation — Add-a-pod appends to it; every pod can be
  the hub; names+URLs only, no tokens.
- **Q3 — hcloud CLI variant**: leaning **include** the `hcloud server create
  --user-data-from-file` rendering as a copy-paste checklist variant (zero new
  mechanism; custody stays operator-side). Confirm at M1 dispatch.
- **Q14 (creation-time hook only)**: M1's Add-a-pod form stays **minimal** — no
  "start from Pod 1's profile" affordance in v1; that arrives with the M4
  configuration-profile artifact. The rest of Q14 (profile contents) stays open
  for M4.

**Resolved (owner, 2026-06-18) — the "one app for all pods" experience:**

- **One-app PWA shape** (§3 "PWA switching"): the long-term cross-pod one-app
  experience is a **native shell** (Tauri/Capacitor — option (c)), *not* a
  web-hub PWA. The shipped per-pod-PWA + sidebar switcher (d) holds at 2 pods;
  the native shell is built when the existing revisit trigger fires (3+ pods in
  rotation OR daily iOS-bounce friction). Web-hub (a, single PWA at a neutral
  origin + client tokens) and reverse-proxy (b) are **rejected** — (a)
  permanently softens loopback-as-authz and stands up a neutral-origin
  skeleton-key for a UX it can't fully deliver; (b) crosses the aggregation
  cliff. Felt need recorded: one icon, no iOS bounce, seamless tabs (not
  unified push). Full reasoning + the lone (a)-reopen condition in §3. No build
  now; this resolves the re-opened decision, it does not lift the gate.

**Resolved (META coordinator session, owner decisions 2026-06-24) — M5 sensitive-artifact appetite:**

- **Q11 — Credential-envelope appetite** (§5): **yes, rotation-ledger-led.** The
  appetite gate is lifted. The headline feature is the **rotation ledger** — a
  record of which pods hold key X, turning rotation from N manual sessions into
  one prompt per pod; encrypted distribution is the *mechanism*, not the pitch.
  The distribution-not-custody invariant holds unchanged: the envelope is
  encrypted to the receiver's public key, the receiver's operator approves on
  arrival, and the sender retains no authority over the copy. The
  same-key-on-N-hosts blast radius is the operator's existing manual practice
  (paste the same key into each wizard) made audited and rotatable — not new
  exposure. Sequencing is unchanged: M5 still gates behind M3 (deposit mailbox)
  and M4 (apps) — not buildable today — and each artifact gets its own mini-spec
  when reached.
- **Q12 — User-identity scope** (§5): **registry-only first.** Build an
  operator-maintained registry of @handles/principals so the same person is one
  principal across pods and approved-user lists are not re-typed per pod.
  Cross-pod profile *data* is deferred to its own consent design (per-user
  opt-out and a wipe path on **both** ends). Operator SSO stays a non-goal — N
  pairings *is* the sovereignty model.

**Open:**

1. **Add-a-pod entry point** (§2): *resolved — admin UI form + docs page both
   (see above).* Number retained so later references stay stable.
2. **Bootstrap depth** (§2): *resolved — see above* (thorough instructions;
   bootstrap-to-wizard; remote-driven variant deferred). Number retained so
   later references stay stable.
3. **hcloud CLI variant** (§2): *leaning resolved — include the `hcloud server
   create` rendering as a checklist variant (see above); confirm at M1 dispatch.*
4. **Script pinning** (§2): *resolved — pin to the release pointer, embed the
   current stable tag at generation time (see above).* Number retained.
5. **Hub liveness dots** (§3): *resolved — links-only v1; CORS dots deferred
   to v1.1 (see above).* Number retained.
6. **Peers list placement** (§3): *resolved — `network.json::peers` on every
   pod (see above).* Number retained.
7. **Digest payload floor** (§4): are firing-signal *titles* and spend
   figures in the default payload, or counts-only by default with titles
   opt-in? (Titles are the useful part; they're also the leakiest.)
8. **Front-pod designation** (§4): is "the operator's primary/home pod
   receives digests" a config field on each sender (recommended), or should
   any pod be able to receive any peer's digest symmetrically (N×N instead
   of hub-and-spoke — more resilient, more tokens to manage)?
9. **The 2026-05-05 doc's disposition** (§4): agree to absorb decisions A/B
   here and mark that doc's central-service topology as the deferred fleet
   tier — or keep it independently alive as-is?
10. **Diana framing** (§1/§4): does multi-pod synthesis warrant a line in
    public positioning once M3 exists ("your pods report to one place"), or
    is multi-pod kept out of the pitch until the fleet tier is real?
11. **Credential-envelope appetite** (§5): *resolved 2026-06-24 — yes,
    rotation-ledger-led; the M5 appetite gate is lifted, with the rotation
    ledger as the lead feature and encrypted distribution as its mechanism;
    still sequenced behind M3+M4 (see above).* Number retained so later
    references stay stable.
12. **User-identity scope** (§5): *resolved 2026-06-24 — registry-only first;
    cross-pod profile data deferred to its own consent design (see above).*
    Number retained so later references stay stable.
13. **Pods on one host** (§1): is same-hardware multi-pod (purpose-split
    pods sharing a box) a real want, or does multi-bot-in-one-pod
    compartmentalization cover it? It's a namespacing/packaging lift
    (accounts, ports, shared-dirs, job labels), not a security redesign —
    but it's real work and would also need the wizard to learn "install a
    second pod here."
14. **Configuration-profile contents** (§5): what exactly is in the pod
    layer worth shipping as an artifact — model tiers/rungs, notification
    prefs, conduct, policies are the candidates; which are in v1 of the
    profile, and does the §2 Add-a-pod flow offer "start from Pod 1's
    profile" at creation time (the strongest version of "set up Pod 2 like
    Pod 1")?
15. **Bot migration v1 vs v2** (§5.1): is the first build v1 (brain moves,
    operator re-pastes integration creds on arrival) — buildable on M3+M4+8.3 GA
    — or do we wait for M5 so creds ride along (v2)? (Recommended: v1 first; v2
    is the convenience upgrade.)
16. **Migration: what moves** (§5.1): confirm bot-intrinsic state moves
    (identity, `.openclaw/`, apps, the `bots[bot_id]` slice, the profile) and the
    pod-operational record stays in A's retire-archive (proposals, signals) — and
    does the raw `observations/<bot_id>/` log move too, or is moving the distilled
    profile enough?
17. **Cutover orchestration** (§5.1): fully operator-sequenced (stop A, then
    apply+start B by hand), or assisted — A stamps a "drained" marker into the
    bundle and B structurally refuses to start the gateway until it sees it, with
    B's ack gating A's retire? (Either way deposit-only; the question is how much
    the tooling enforces single-binding vs. trusts the operator.)
18. **Multi-user bot privacy on migration** (§5.1): when a bot with members
    (e.g. a team bot) migrates, do its members get notified / asked to consent
    that their conversation-derived data moved hosts — the one artifact that
    carries such data across machines?
19. **Rollback window** (§5.1): how long does pod A retain the quiesced (stopped,
    not retired) bot before `retire_bot` archives it — and is that archive the
    canonical rollback source?
