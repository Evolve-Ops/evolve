# Research: platform expansion — where the OpenClaw install base actually is

**Status:** research complete / decision recorded · **Date:** 2026-06-10 ·
**Roadmap:** Phase 8.1 ([roadmap-80-to-100-2026-06-09.md](roadmap-80-to-100-2026-06-09.md))

Evolve today assumes one dedicated Mac mini: launchd for scheduling, `dscl`
per-bot macOS users for isolation, macOS `chmod +a` ACLs for cross-user reads,
macOS-specific sudoers, and `/Users/...` paths throughout. The question this
doc answers: **should we expand supported hardware, and in what order?**

**Decision bar (from roadmap Phase 8):** expand only to where the OpenClaw
install base demonstrably is — not for platform completeness.

**The answer in one paragraph:** the install base concentrates in two clusters —
Mac hardware (Mac mini foremost) and **Linux VPS/cloud** — with Windows and
Raspberry Pi as real but minority segments. So: (a) widen macOS support from
"a Mac mini" to **any dedicated, always-on Apple-Silicon Mac** now (~1 session;
shared personal Macs stay unsupported — the threat model's single-tenant
assumption is load-bearing and a personal MacBook violates it); (b) **go on
Linux**, Ubuntu LTS first, gated on the 4.3 C Scheduler/Isolation seams —
that's where the second-largest user cluster is and the seams were designed for
exactly this; (c) **defer Windows** explicitly — native is a poor structural
fit and the demonstrated user base is small; WSL2 inherits the Linux port
later as a best-effort path.

---

## 1. Install-base findings (external research, 2026-06-10)

Per the project verification norm (AI-generated blog summaries of OpenClaw
have been unreliable before — verify against release notes/docs/source), every
claim below traces to official docs, the upstream repo, or first-person
community posts — not third-party blog roundups. Full source list in §7.

**There is no official platform telemetry.** ClawHub telemetry counts skill
installs only — no OS breakdown
([docs.openclaw.ai/clawhub/telemetry](https://docs.openclaw.ai/clawhub/telemetry)).
Ranking below is triangulated from two Ask HN user threads (~570 combined
comments), indexed Discord threads, official docs emphasis, and GitHub issue
volume. Treat shares as directional, not measured.

| Rank | Platform | Confidence | Evidence |
|------|----------|------------|----------|
| 1 | **macOS** (Mac mini foremost; spare MacBooks) | HIGH it's #1 or co-#1 | Mac mini is the community reference hardware (Show HN "$640 Mac mini for a week", [HN 46895546](https://news.ycombinator.com/item?id=46895546)); competitor pitches frame themselves as "no Mac mini" ([HN 47292587](https://news.ycombinator.com/item?id=47292587)); Ask HN [47783940](https://news.ycombinator.com/item?id=47783940) hardware tally was Mac-heavy; the **iMessage channel requires a macOS host** ([docs](https://docs.openclaw.ai/channels/imessage)) — a structural pull factor. Caveat: a first-person Discord post argues "Mac mini for OpenClaw is an influencer meme" — social visibility likely overstates Mac mini share vs. quiet VPS installs. |
| 2 | **Linux VPS / cloud VM** (Hetzner, Fly.io, GCP, Azure) | HIGH (co-#1 plausible) | The *earlier* Ask HN thread ([46838946](https://news.ycombinator.com/item?id=46838946)) had VPS/cloud as the **single largest** category; official docs ship dedicated Hetzner/Fly.io/GCP/Azure install guides ([docs.openclaw.ai/platforms](https://docs.openclaw.ai/platforms)); a managed-hosting vendor ecosystem exists (awesome-openclaw). |
| 3 | **Linux home server / mini PC / old laptop** | MEDIUM | Recurring in both HN threads (home servers, NUC, Proxmox, spare laptops); 283 GitHub issues mention systemd. |
| 4 | **Docker/containers** (cross-cuts 2–3) | MEDIUM-HIGH as a method | Official ghcr.io images + bundled compose; 843 issues mention Docker. |
| 5 | **Windows** (native Hub + WSL2) | MEDIUM — minority, growing | Only 3–4 mentions across ~570 HN comments. Upstream is investing (native WinUI Hub app, recent); WSL2 still documented as "the most Linux-compatible Gateway runtime on Windows". 1,369 Windows-titled issues reflect dev work + pain, not share. |
| 6 | **Raspberry Pi / ARM SBC** | HIGH it's real; LOW share | Official install guide with a Pi model matrix ([docs](https://docs.openclaw.ai/install/raspberry-pi)); first-person "Pi 5 works beautifully" (Discord); ARM is supported upstream but regression-prone (OOM #45440, CPU-spin #79418, opus compile #22983). |

Upstream platform posture: docs are deliberately OS-agnostic ("Any OS gateway",
Node 24 recommended). Install-order emphasis: macOS/Linux curl installer first,
then Windows (PowerShell / native Hub / WSL2), then Docker/Nix/npm
([getting-started](https://docs.openclaw.ai/start/getting-started)). Linux
Gateway is "fully supported today" with user-level systemd units documented
([platforms/linux](https://docs.openclaw.ai/platforms/linux)).

**Implication for Evolve:** the two demonstrable clusters are (1) the segment
we already serve — dedicated Mac hardware — and (2) Linux boxes, both cloud
VPS and home servers. Windows and Pi are real but small. The expansion order
writes itself; the only judgment calls are scope (shared Macs?) and gating
(seams first).

---

## 2. What the port actually costs — coupling, measured

Re-measured in this session (2026-06-10, this worktree; counts are `git grep`
over `*.py`):

| Surface | Measure | Funneled? |
|---------|---------|-----------|
| Agent runtime (OpenClaw CLI) | **0** importers of `oc_cli` outside the seam | ✅ **Shipped** — `AgentRuntime` seam, PRs #2560/#2562/#2571 (Phases A/B/D-partial) |
| Scheduler (launchd) | 78 non-test py files contain `launchctl` (136 incl. tests); the 4.3 C design's stricter "subprocess call sites" measure is 55 files; **6 independent plist-XML emitters** | ❌ No funnel yet — 4.3 C Track S builds it (S0 spec ready: [design-phase4.3c-S0-plist-consolidation.md](design-phase4.3c-S0-plist-consolidation.md)) |
| Isolation (`dscl`/`sysadminctl`) | 11 non-test files; create/delete semi-funneled in `provisioning.py` | ⚠️ Mostly — 4.3 C Track I (~1 session) |
| Paths | `/Users/` appears in 576 py files; ~230 direct f-string `/Users/{...}` literals vs ~600 calls through `bot_home()`/`get_bot_user()` (which resolve via `pwd.getpwnam` first — already POSIX-portable); `/Users/Shared/evolve` + `/Library/LaunchDaemons` hardcoded | ⚠️ Mixed — user homes mostly abstracted; shared/system paths are not |
| macOS ACLs (`chmod +a`) | ~150 references, but application is concentrated in a handful of `deploy.py` functions (`set_evolve_read_acl`, `ensure_pod_perms`, …) | ⚠️ Semi-funneled; **no abstraction** |
| Secrets (Keychain) | All `security` CLI use in `keystore.py`; file-vault fallback already exists (and is already the *common* path — the headless `evolve` LaunchDaemon has no Keychain session, threat-model §6.2) | ✅ Funneled |
| Host checks | `setup_wizard.py:3069` hard-fails non-macOS; macOS 14 floor is soft ("recommended"); **no Mac-mini model check anywhere**; Apple-Silicon/Intel handled via dual Homebrew paths (`/opt/homebrew` 89 refs, `/usr/local` 36); npm install hardcodes `--prefix=/opt/homebrew` (`setup_wizard.py:3180`) | ✅ Funneled |
| Power/sleep | **Zero** `pmset`/`caffeinate` in core code. Every daemon (repo-puller 15-min pull, watchdogs, heartbeats, signal-subscriber 1 Hz) silently assumes the host never sleeps | ❌ Implicit everywhere; cheap to gate at the wizard |

Two readings of the same table:

- **The pessimistic count is real** (~78 launchctl files, 576 `/Users` files)
  — a naive "port it" is a rewrite.
- **The funnel program already exists** — 4.3 C is precisely the
  consolidate-then-seam-then-migrate plan for the two unfunneled surfaces, with
  estimates already scoped (Track I ~1 session; Track S 2–3 sessions). The
  port cost is "finish 4.3 C, then write one adapter set", not "touch 600
  files per platform".

---

## 3. Threat-model implications

[threat-model.md](threat-model.md) §2: **"Evolve assumes no other untrusted
local users exist on the host"** — load-bearing for four controls (sudoers
wildcard grants, `/tmp` staging, keystore POSIX file perms, loopback-as-authz
on the admin server).

| Topology | Single-tenant assumption | Verdict |
|----------|--------------------------|---------|
| Dedicated Mac mini (today) | Holds by construction | Supported |
| **Dedicated** Apple-Silicon Mac of any kind (Studio, iMac, spare MacBook used as a server) | Holds — "dedicated" is the property that matters, not the chassis | **Supportable now**; needs a power/sleep prereq, not a security change |
| **Shared personal Mac** (someone's daily-driver MacBook) | **Broken.** One human, but dozens of third-party apps/agents run as that user; any of them can hit the unauthenticated loopback admin server, race `/tmp`, or read the keystore if it escalates. §2's "when the assumption breaks" clause applies verbatim | **Not supportable** until Phase 2 residuals land (2.6 pairing-auth default-on, 2.7 CSRF/Origin) **and** threat-model §2 is rewritten for a hostile-local-process model — sudoers wildcards and loopback trust both need rework, not just auth |
| Dedicated Linux VPS | Holds by construction — arguably *better* than a home Mac (no GUI apps, no other humans). New wrinkle: the operator is **remote**, so loopback-as-authz means access via SSH tunnel (upstream OpenClaw docs already model exactly this). Pairing auth (2.6) becomes more valuable, not less | Supportable post-port; threat model §1.1 needs a "remote operator over SSH" variant |
| Shared Linux box / multi-tenant | Same break as shared Mac | Out of scope |
| Windows native | Different isolation primitives entirely (no sudoers, icacls, service accounts) — the threat model would need a ground-up rewrite | Deferred (§4c) |

**The honest framing for docs/marketing:** Evolve's security model is built on
*dedicated hardware*, and that constraint is a feature (it is what makes the
"vigilant by default" claim cheap to keep). Expansion should widen *which*
dedicated box you can use — not quietly drop the word "dedicated".

---

## 4. Per-platform evaluation

### (a) All Apple computers — **GO now, scoped to "dedicated, always-on, Apple Silicon"** (~1 session)

What actually assumes a *mini* today: **nothing**. No hardware-model check
exists; the wizard runs on any Mac ≥ macOS 14 (soft floor). The real gaps are:

1. **Power/sleep** — the entire daemon fleet assumes always-on. launchd
   `StartInterval` jobs don't fire during sleep (they coalesce on wake);
   KeepAlive gateways are suspended and bots go dark with the lid. A MacBook
   that sleeps breaks the product promise silently.
   **Fix:** a wizard prereq step — detect battery hardware (`pmset -g batt` /
   model string), then require + offer to set "never sleep on AC"
   (`pmset -c sleep 0 displaysleep 0`), warn that lid-closed operation needs
   power + (display or `pmset disablesleep 1`), and record the choice. Plus a
   `host_health` signal when uptime gaps indicate the host slept.
2. **Threat model** — per §3: *dedicated* spare MacBook = fine; *shared
   personal* MacBook = explicitly unsupported. The wizard copy and README
   should say "a Mac that's dedicated to this job" rather than "Mac mini".
3. **Intel** — works today (dual Homebrew paths throughout), but macOS 26
   (Tahoe) is announced as the final Intel macOS release; recommend
   Apple Silicon in docs, keep Intel working on a best-effort basis, don't
   spend new effort on it. (The hardcoded `npm --prefix=/opt/homebrew` at
   `setup_wizard.py:3180` actually *breaks* fresh Intel installs — fix or
   document it when touching 8.2.)

**Effort:** ~1 session (wizard prereq + pmset step + copy/docs + threat-model
§2 paragraph + a MacBook test install). Independent of 4.3 C — can ship any
time. **Install-base justification:** spare MacBooks appear directly in the
HN tallies; this is serving existing demand, not chasing a platform.

### (b) Linux — **GO, gated on 4.3 C; Ubuntu LTS first** (~5–8 sessions on top of 4.3 C)

**Why go:** Linux VPS/cloud is the co-#1 cluster (and #3 home-Linux rides the
same port). OpenClaw itself is fully supported on Linux with systemd units
documented upstream — the *agent runtime* needs no change at all (the
AgentRuntime seam's OpenClaw adapter works as-is; only the OS-facing seams
swap). This is also the only expansion that opens a genuinely new market:
"$5/month Hetzner box" users who will never buy a Mac.

**Why gated:** without 4.3 C there is no Scheduler/Isolation interface to
write a Linux adapter *against* — the port would be a 78-file rewrite. With
C done it's two adapter classes plus a portability sweep.

**Which distro:** **Ubuntu 24.04 LTS** — the default image on the VPS
providers the community actually names (Hetzner foremost), the largest
support surface, and `/usr`-merged so the sudoers full-path discipline
(`/bin/cat` etc.) carries over unchanged.

**Translation map** (the adapter spec, in miniature):

| macOS primitive | Linux equivalent | Fit |
|---|---|---|
| launchd plist + `launchctl bootstrap/bootout/kickstart -k/print/list` | systemd unit (+ timer); `systemctl enable --now / disable --now / restart / show / list-units` | Clean — `JobSpec` (4.3 C S0) was designed for this: `start_interval`→`OnUnitActiveSec`, `start_calendar`→`OnCalendar`, `keep_alive`→`Restart=always|on-failure`, `user`→`User=`, stdout/stderr→journal or `StandardOutput=append:` |
| `dscl . create` / `sysadminctl -addUser` / `createhomedir` / `dscl -delete` | `useradd -m` / `userdel -r` / `getent passwd` | Clean — *simpler* than macOS; IsolationProvider's `MacOSIsolation` is the hard one, `LinuxUserIsolation` is the easy one |
| macOS ACLs: `chmod +a "user:evolve allow list,search,readattr,… file_inherit,directory_inherit"` | POSIX ACLs: `setfacl -R -m u:evolve:rX` + **default** ACLs `setfacl -d -m u:evolve:rX` for inheritance | Workable — POSIX default-ACLs give the same inheritance semantics; macOS's fine-grained verbs (list/search/readattr/readsecurity) collapse to `rX`, which is what we actually use them for. Needs a small `perms.py` abstraction over the ~6 `deploy.py` ACL functions; watch the POSIX ACL mask interaction |
| `/etc/sudoers.d/*` with macOS visudo quirks (no trailing `/*`, no escaped dots) | Same files, fewer quirks — Linux visudo accepts the patterns macOS rejects | Clean; keep full paths (valid on Ubuntu via /usr-merge) |
| Keychain via `security` CLI | None needed initially — the file-vault fallback is *already* the headless-daemon path on macOS (threat-model §6.2). Later: systemd-creds or libsecret, folded into Phase 2.2 | Clean (honestly-labeled weak vault either way) |
| `/Users/{user}`, `/Users/Shared/evolve`, `/Library/LaunchDaemons` | `/home/{user}`, `/var/lib/evolve` (or `/home/Shared` shim), `/etc/systemd/system` | Mechanical but diffuse — wants a `platform_profile` module; overlaps Phase 6 packaging, do them together |

**Known channel caveat:** iMessage requires a macOS host upstream — a Linux
pod cannot offer it (document; an SSH-wrapped Mac relay is upstream's answer).
**Bonus:** an Ubuntu ARM64 build covers Raspberry Pi ~for free; treat Pi as
untested-but-unblocked (upstream ARM regressions noted in §1), not a target.

**Effort, keyed to the 4.3 C plan:**

| Step | Sessions | Depends on |
|---|---|---|
| 4.3 C Track I (isolation seam, macOS adapter) | ~1 | — (scoped) |
| 4.3 C Track S (S0 plist consolidation → S1 interface → S2 migrate 55 sites → S3 prove) | 2–3 | — (S0 spec ready) |
| `SystemdScheduler` + `LinuxUserIsolation` real adapters | 1–2 | C done |
| Portability sweep: `platform_profile` paths, `perms.py` ACL abstraction, sudoers writer variant, keystore no-op-Keychain path | 2–3 | adapters |
| Wizard/deploy Linux path + Ubuntu VM end-to-end (one bot deploys, runs, is administered) + CI job | 2–3 | sweep |

≈ **5–8 sessions beyond 4.3 C** to the roadmap-8.3 spike bar ("one bot
end-to-end on Ubuntu, feature-gated"), with full parity (docs, soak,
channel-matrix honesty) beyond that. This is consistent with the 4.3 C doc's
warning: the seams are the cheap part; **don't start the spike before S2
reaches zero direct `launchctl` call sites** or the port forks the behavior.

### (c) Windows — **explicit defer** (record the decision, do nothing)

- **Demonstrated demand is small:** 3–4 mentions across ~570 first-person HN
  comments. Upstream's native push (WinUI Hub) is recent and aimed at
  *desktop* users — not the dedicated-host topology Evolve is.
- **Native is a structural mismatch:** no sudoers, no POSIX ACLs, no per-bot
  Unix users — IsolationProvider/Scheduler/perms would each need a from-scratch
  Windows model (service accounts, icacls, Task Scheduler), and threat-model §2
  has no Windows analogue. This is a different product, not an adapter.
- **WSL2 is the escape hatch, and it's free later:** WSL2 runs systemd, so the
  Linux port *is* the Windows story — minus the always-on guarantee (WSL VM
  lifecycle is tied to the host session; needs explicit keep-alive config).
  After 8.3 ships, document WSL2 as community-supported/best-effort. Do not
  build Windows-native adapters.

Revisit trigger: upstream ships first-party Windows *service* tooling **and**
Windows shows up materially in install-base signals (say, >15% of first-person
hardware mentions).

---

## 5. Recommended sequencing

1. **Now, independent of everything:** 8.2 — widen to "any dedicated,
   always-on Apple-Silicon Mac" (~1 session: wizard power/sleep prereq, Intel
   npm-prefix fix-or-document, copy, threat-model §2 paragraph). Cheap,
   serves demonstrated demand, zero security change.
2. **Continue 4.3 C as planned** (Track I then Track S, S0 already spec'd) —
   it was already justified on testability grounds; the Linux decision adds
   the strategic payoff.
3. **After C: 8.3 Linux spike** on Ubuntu 24.04 LTS (~5–8 sessions): systemd +
   useradd adapters, platform-profile sweep, one bot end-to-end in a VM,
   feature-gated. Threat-model gets a "remote operator over SSH" variant.
4. **Windows: deferred, recorded** (this doc). WSL2 inherits the Linux port
   as best-effort documentation later. No code.
5. **Not doing:** shared personal Macs (until Phase 2 residuals + threat-model
   rewrite — and even then it's a product decision, not just a hardening
   task); Raspberry Pi as a supported target (unblocked by the ARM64 Linux
   port, untested otherwise); Docker-as-isolation (a possible second
   IsolationProvider someday — the seam permits it; nothing in the install
   base demands it before plain Linux users work).

---

## 6. What this research did *not* establish

- **Actual platform shares.** No official telemetry exists; HN/Discord tallies
  are self-selected samples of people who post. The Mac-mini-vs-VPS ordering
  could be inverted in the quiet majority; it does not change the
  recommendation (both clusters clear the bar; nothing else comes close).
- Issue-title counts (Windows 1,369 vs macOS 1,307 …) measure friction +
  dev activity, **not** install share — cited only as secondary corroboration.
- Blog claims (enterprise-share percentages, "M4 handles AI models
  effortlessly") were found only in secondary sources and are **unverified**;
  none were used in the recommendation.

## 7. Sources

**Primary — official docs/repo:**
[docs.openclaw.ai](https://docs.openclaw.ai/) ·
[getting-started](https://docs.openclaw.ai/start/getting-started) ·
[platforms](https://docs.openclaw.ai/platforms) ·
[platforms/linux](https://docs.openclaw.ai/platforms/linux) ·
[platforms/windows](https://docs.openclaw.ai/platforms/windows) ·
[install](https://docs.openclaw.ai/install) ·
[install/raspberry-pi](https://docs.openclaw.ai/install/raspberry-pi) ·
[install/docker](https://docs.openclaw.ai/install/docker) ·
[channels/imessage](https://docs.openclaw.ai/channels/imessage) ·
[clawhub/telemetry](https://docs.openclaw.ai/clawhub/telemetry) ·
[github.com/openclaw/openclaw](https://github.com/openclaw/openclaw) +
[releases](https://github.com/openclaw/openclaw/releases) ·
issue-title counts via GitHub search 2026-06-10 (Windows 1369, macOS 1307,
Docker 843, systemd 283, Linux 256, launchd 189, WSL 72, VPS 70, RPi 41,
Hetzner 16, Mac mini 8); key issues #75, #44406, #45440, #79418, #22983,
#47222, #84151, #56285.

**Primary — first-person community:**
Ask HN [47783940](https://news.ycombinator.com/item?id=47783940) (Mac-heavy
tally) · Ask HN [46838946](https://news.ycombinator.com/item?id=46838946)
(VPS-heavy tally) · Show HN
[46895546](https://news.ycombinator.com/item?id=46895546) (Mac mini week-1
report) · [47292587](https://news.ycombinator.com/item?id=47292587) ("no Mac
mini" hosted pitch) · [47337249](https://news.ycombinator.com/item?id=47337249)
(Klaus VM) · Discord via answeroverflow: Pi 5 first-person, "VPS vs Mac Mini
vs Mini PC?", Hetzner CX23 sizing, Mac-mini-meme pushback ·
[awesome-openclaw](https://github.com/vincentkoc/awesome-openclaw)
(hosting-vendor ecosystem).

**Secondary (not relied on):** TechRadar/Medium/GEEKOM hardware posts,
fatjoe/getpanto stats pages, anakin.ai guides — flagged unverified per the
AI-blog-summaries verification norm.

**Internal:** [design-phase4.3-runtime-adapter-seam-2026-06-09.md](design-phase4.3-runtime-adapter-seam-2026-06-09.md) ·
[design-phase4.3c-scheduler-isolation-seams-2026-06-10.md](design-phase4.3c-scheduler-isolation-seams-2026-06-10.md) ·
[design-phase4.3c-S0-plist-consolidation.md](design-phase4.3c-S0-plist-consolidation.md) ·
[threat-model.md](threat-model.md) · coupling counts re-measured in this
worktree 2026-06-10.
