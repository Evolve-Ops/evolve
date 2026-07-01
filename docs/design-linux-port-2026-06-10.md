# Design: Linux port (Phase 8.3) — Ubuntu 24.04 adapter set

**Status:** design / **awaiting discussion — SPEC ONLY, no build approved** ·
**Date:** 2026-06-10 · **Roadmap:** Phase 8.3
([roadmap-80-to-100-2026-06-09.md](roadmap-80-to-100-2026-06-09.md))

The GO decision and install-base evidence live in
[research-platform-expansion-2026-06-10.md](research-platform-expansion-2026-06-10.md)
§4b: Linux VPS/cloud is the co-#1 OpenClaw install cluster, Ubuntu 24.04 LTS is
the distro, and the port is ~5–8 sessions *on top of* the 4.3 C seams. This doc
turns that one-table translation map into per-decision design: each section
states a recommendation, the alternatives considered, and the trade-offs.
**Nothing here is built until the project owner has read this and the
open questions in §12 have been discussed.**

What the port builds on — and what it doesn't touch:

| Layer | Status | Port impact |
|---|---|---|
| `AgentRuntime` seam ([design-phase4.3-runtime-adapter-seam-2026-06-09.md](design-phase4.3-runtime-adapter-seam-2026-06-09.md)) | **Shipped** (PRs #2560/#2562/#2571) | **None.** OpenClaw is fully supported on Linux upstream; the `OpenClawRuntime` adapter works as-is. The agent runtime is not part of this port. |
| `Scheduler` / `IsolationProvider` seams ([design-phase4.3c-scheduler-isolation-seams-2026-06-10.md](design-phase4.3c-scheduler-isolation-seams-2026-06-10.md)) | **Scoped, NOT built** (S0 spec ready) | The port *is* the second adapter set for these interfaces. Hard gate: do not start before Track S S2 reaches **zero** direct `launchctl` call sites (§11). |
| Threat model ([threat-model.md](threat-model.md)) | Current | §1.1/§2 gain a "remote operator over SSH" variant (§1 below); single-tenant assumption carries over unchanged. |

---

## 1. Target topology — VPS first; home server rides along

**Recommendation: design for the dedicated Linux VPS (Hetzner-class) as the
primary topology; the home server/mini-PC case is the same code with a LAN
instead of a WAN in front of it.** Do not build any web-exposure path for the
admin UI in this port.

The research doc's evidence ordering is VPS/cloud co-#1, home Linux #3 — and
they differ only in *how the operator reaches loopback*, not in any code path.

### The remote-operator question

This is the one genuinely new thing about Linux topology. On the mini, the
operator's browser and the admin server share a machine; threat-model §2's
"loopback-as-authz" means reaching `127.0.0.1:8080` *is* the authentication.
On a VPS the operator is remote, so the model becomes:

> **SSH access to the box is the operator credential.** The admin UI stays
> bound to `127.0.0.1`; the operator reaches it via an SSH tunnel
> (`ssh -L 8080:127.0.0.1:8080 <host>`). Anyone who can SSH in is the
> operator — which is exactly the single-tenant assumption restated.

Upstream OpenClaw docs model precisely this access pattern for VPS installs,
so the target audience already lives this way.

| Alternative | Trade-offs |
|---|---|
| **SSH tunnel to loopback (recommended)** | Zero new attack surface; zero new code (one docs page + wizard copy). The threat model's §2 table carries over verbatim — arguably *stronger* on a VPS (no GUI apps, no other humans, no third-party desktop agents racing `/tmp`). Cost: operator UX is "run an ssh command first", which is fine for the Hetzner demographic and wrong for consumer polish — but consumer polish on a VPS is not this phase's job. |
| Bind `0.0.0.0` + Phase 2.6 pairing auth | Real browser-direct UX, but it makes 2.6/2.7 (pairing default-on, CSRF/Origin) **blocking prerequisites** of the port, couples two roadmap phases, and converts every Phase-2 residual into an internet-facing hole instead of a loopback subtlety. Rejected for the spike; revisit as a post-2.6 enhancement. |
| Tailscale/WireGuard as the blessed access path | Nice middle ground (still no public bind) and worth a docs mention, but blessing a third-party network dependency in the wizard is a product decision we don't need yet. SSH is already there. |

**Interplay with Phase 2.6:** unchanged in direction, raised in value. Pairing
auth protects against the *local* process that isn't the operator; on a VPS it
additionally protects against SSH-key sprawl (a second human with SSH access to
a box someone treats as shared). The research doc's note stands: 2.6 becomes
more valuable on Linux, not less — but it is **not a gate** for the spike,
because the spike's topology assumption (dedicated, single-tenant) is the same
one the macOS product ships under today. Threat-model §1.1 gets the SSH-variant
paragraph as part of this port (it's in the 8.3 exit criterion).

**Operator account model on a fresh VPS:** there is no `pod-admin` macOS
account; there's `root` (or a sudo-capable default user like `ubuntu`). The
mapping: *operator = any sudo-capable login account*; the wizard (run via
`sudo evolve-admin setup`) creates the service accounts (`evolve`, `evo`,
per-bot users) exactly as on macOS. `resolve_pod_context()`'s SSH-target
derivation (`network.json::pod.ssh_target` → derived → empty) already models
"operator is remote"; Linux makes the middle branch the common case instead of
the exception.

**Operator client matrix — distinct from the pod-host matrix.** This port is
about the pod *host* (macOS today, Ubuntu 24.04 after 8.3). The operator's
*client* machine is a different axis and is platform-agnostic by construction:
the admin UI is a browser app, so any modern browser on macOS, Windows, or
Linux is a supported operator client. The Windows-deferral decision (research
doc §4c) is about Windows as a pod host and says nothing about Windows
operators — a Surface administering a Linux VPS pod is a fully supported
topology, and it means **Evolve never requires owning Apple hardware at all**
(worth stating in launch copy). What this costs in practice: tunnel
instructions written per client OS (Windows 10+ ships the OpenSSH client, so
`ssh -L 8080:127.0.0.1:8080 user@host` works verbatim in PowerShell; Tailscale
clients exist for all three), operator-facing docs/copy that never assume a
Mac on the client end of the tunnel, and one manual UI-verification pass in a
Windows browser (Edge/Chrome) — the UI has only ever been used from macOS
browsers; the risk for a Flask-served SPA is low (theme rendering, fonts,
scrollbars) but the check is cheap. L3 carries the docs + verification pass
(§12 Q11).

---

## 2. Isolation — per-bot Linux users, not containers

**Recommendation: `LinuxUserIsolation` — per-bot Unix users via
`useradd -m` / `userdel -r` / `getent passwd`, a direct translation of the
macOS per-bot-user model.** Containers stay a possible *future* second
IsolationProvider; the seam permits them, nothing requires them.

This is the IsolationProvider interface from the 4.3 C doc with the easy
adapter: `create` → `useradd -m -s /bin/bash <user>` (regular UID range, plus
a dedicated `evolve-bots` group for inventory), `delete` →
`userdel -r <user>`, `resolve` → `getent passwd` (note `bot_home()` /
`get_bot_user()` already resolve via `pwd.getpwnam`, which is POSIX-portable
today), `run_as` → `sudo -H -u <user> …` unchanged.

| | Per-bot Linux users (recommended) | Containers (podman, rootless) |
|---|---|---|
| Fit with shipped model | **1:1.** Same accounts (`evolve`, `evo`, bot users), same sudoers shape, same ACL story, same threat-model §1.3 tier table. One mental model across both platforms. | A second, different model to keep in parity forever. Threat-model §1.3/§1.4 would need a container column; sudoers and cross-user ACLs don't map (cross-container file grants mean volume gymnastics). |
| Confinement strength | OS user isolation — exactly what we have on macOS. Bots share a kernel and a filesystem view. | **Genuinely better**: namespaces, cgroup resource limits, image-pinned dependencies, seccomp. This is the honest argument *for* containers. |
| Weight | `useradd` is instant and dependency-free. | Image build/refresh lifecycle, registry or local builds, OpenClaw + Node inside the image, volume-mounting `{shared_dir}` with correct ACL behavior across the boundary, podman version drift. Heavier to build and to operate. |
| Shared-state plumbing | `{shared_dir}` ACLs (§4) work directly. | Every `{shared_dir}` contract (signal store, proposals, evo write-ACL) crosses a mount boundary; the evo-account-separation ACL dance would need re-derivation. |
| Install-base pull | The research doc records: nothing in the install base demands containers before plain users work. Docker ranks as an install *method* for OpenClaw itself, not a demand for per-bot container isolation. | Cross-cutting Docker familiarity exists (843 issues), so a container provider would not be alien — later. |

**Trade-off owned explicitly:** we are choosing parity-and-simplicity over
stronger confinement. If a future threat-model revision (e.g. hostile-local-
process work for shared Macs, Phase 2 residuals) raises the confinement bar,
`ContainerIsolation` is an additive adapter behind the same interface — that's
what the seam is for. Building it *now* would double the port's surface for a
segment that hasn't asked.

Details that differ from macOS and need deciding in code review, not after:
UID range (regular ≥1000, matching macOS's regular-user behavior, so file
listings look sane), shell (`/bin/bash` to match macOS run-as semantics — not
`nologin`, since `run_as` and OpenClaw exec rely on a working shell), and
`createhomedir` having no analogue (`useradd -m` covers it; one less ritual).

---

## 3. Scheduler — `JobSpec` → systemd units + timers

**Recommendation: `SystemdScheduler` renders each `JobSpec` to a system-domain
unit in `/etc/systemd/system/`, with a service+timer pair for interval/calendar
jobs and a plain service for KeepAlive daemons. Keep the reverse-DNS labels
verbatim as unit names. Keep file logs (`StandardOutput=append:`) for v1, not
journald.**

The `JobSpec` dataclass (4.3 C S0,
[design-phase4.3c-S0-plist-consolidation.md](design-phase4.3c-S0-plist-consolidation.md))
was designed for exactly this rendering. The field map:

| `JobSpec` field | launchd today | systemd rendering |
|---|---|---|
| `label` | plist `Label`, e.g. `ai.evolve.evolve.admin-ui` | Unit name verbatim: `ai.evolve.evolve.admin-ui.service` (+ `.timer`). Dots are legal in unit names; the orphan sweep's prefix match (`ai.evolve.*`) keeps working with `systemctl list-units 'ai.evolve.*'`. |
| `program_args` | `ProgramArguments` | `ExecStart=` (full-path argv — discipline already enforced) |
| `user` | `UserName` | `User=` (see "system vs user units" below) |
| `keep_alive: True` | `KeepAlive: true` | `Restart=always` + `RestartSec=10` (matches launchd's default 10 s throttle) |
| `keep_alive: {SuccessfulExit: False}` | conditional KeepAlive | `Restart=on-failure` |
| `run_at_load` | `RunAtLoad` | KeepAlive daemons: `systemctl enable --now` (`WantedBy=multi-user.target`). Timer jobs: `OnBootSec=` on the timer (+ `Persistent=true` where a missed calendar run should fire on boot — decide per-job, it's the launchd wake-coalescing analogue). |
| `start_interval` | `StartInterval` (every N s while loaded; skips if still running) | Timer with `OnUnitActiveSec=Ns` + `OnBootSec=Ns`. Near-identical semantics: a firing while the service is active is a no-op, like launchd. Drift nuance: `OnUnitActiveSec` measures from last *activation*, so long-running jobs drift by run duration; launchd holds wall-clock cadence. None of our interval jobs are cadence-critical at that precision (15-min puller, hourly prunes), but it's a behavior change to note in review. |
| `start_calendar` | `StartCalendarInterval` dicts | `OnCalendar=` expressions (`{Hour: 7, Minute: 30}` → `*-*-* 07:30:00`; `Weekday` → `Mon..`/`Sun` prefix). Mechanical dict→string translation, table-driven. |
| `working_dir` / `env` | `WorkingDirectory` / `EnvironmentVariables` | `WorkingDirectory=` / `Environment=` lines |
| `stdout_path` / `stderr_path` | `StandardOutPath` / `StandardErrorPath` | `StandardOutput=append:<path>` / `StandardError=append:<path>` (needs systemd ≥ 240; Ubuntu 24.04 ships 255) |
| `jitter_seconds` | bash-`sleep` wrapper (emitter #2's hack) | `RandomizedDelaySec=` on the timer — **native and better**; the Linux renderer should drop the sleep wrapper, not reproduce it |
| `extra` (launchd-only escape hatch) | passthrough | **Renders nothing; logs loudly.** The S0 rule applies doubly here: if a job needs a key the `JobSpec` can't express, model the field, don't smuggle XML/ini. |

Verb map for the `Scheduler` interface: `install` → write unit file(s) +
`systemctl daemon-reload` + `enable --now` · `remove` → `disable --now` + rm
files + `daemon-reload` · `restart` (launchd `kickstart -k`) → `systemctl
restart` · `status` (launchd `print`) → `systemctl show` (parse
`ActiveState`/`MainPID`/`ExecMainStatus`) · `running` → `is-active` · `kill` →
`systemctl kill`. The pair-ness (service+timer) lives entirely inside the
adapter: `install`/`remove` treat the pair atomically, and the label↔unit-name
mapping must be deterministic in both directions so the orphan sweep
(`find_orphaned_plists` generalized) can reconcile.

Decisions inside this recommendation, with alternatives:

- **System units with `User=`, not per-user systemd instances.** Per-user
  units (`systemctl --user` + `loginctl enable-linger <bot>`) are the upstream
  OpenClaw docs' pattern for *single-user* installs, and they'd isolate unit
  state per bot. Rejected: our model is one admin plane managing N bots — all
  launchd jobs today are *system-domain* LaunchDaemons with `UserName`, the
  admin daemon (running as `evolve`) must install/restart/inspect *other*
  users' jobs (per-user units would require sudo-as-each-bot, which the
  sudoers model deliberately forbids), and linger adds a per-bot stateful
  toggle that's one more thing to drift. System units with `User=` is the
  1:1 translation.
- **File logs for v1, journald later (maybe).** journald is the native
  citizen (`journalctl -u`, rotation for free) — but every log *reader* in
  the codebase (health checks, error_reporter, log tails in the admin UI)
  reads files at known paths from the plists. `StandardOutput=append:`
  preserves all of that unchanged, keeps macOS/Linux behavior identical, and
  costs only "you also get journald copies by default" (set
  `LogRateLimitIntervalSec`/journald caps if duplication bothers us).
  Switching readers to journald would be a both-platform refactor for zero
  port-critical gain. Revisit post-parity (§12 Q2).
- **Unit naming: keep `ai.evolve.*` reverse-DNS.** Alternative — rename to
  Linux-conventional `evolve-<job>.service`. Rejected for the spike: every
  label literal, the orphan sweep, monitoring allowlists, and operator docs
  use the labels; renaming is gratuitous churn across both platforms. A
  Linux purist can `systemctl list-units 'ai.evolve.*'` just fine. (§12 Q1
  if anyone feels strongly.)

---

## 4. ACLs — POSIX `setfacl` + default ACLs, behind a new `perms.py`

**Recommendation: introduce `perms.py` — a small platform-dispatching
abstraction over the ~6 ACL entry points currently open-coding `chmod +a` in
`deploy.py` (`set_evolve_read_acl`, `ensure_pod_perms`,
`fix_shared_dir_permissions`, `_ensure_evo_write_acl`,
`_ensure_evolve_owned_dir_perms`, `fix_plugin_permissions`, plus their
check/apply helpers). macOS backend = today's `chmod +a/-N` verbatim; Linux
backend = `setfacl` access + default ACLs.**

This is the only surface in the port with no existing seam *and* no 4.3 C
track — the research doc flagged it ("semi-funneled; no abstraction"). The
abstraction is justified the same way `JobSpec` was: 6 call sites, 2 renderers.

The semantic translation:

| macOS mechanism | POSIX equivalent | Notes |
|---|---|---|
| `chmod +a "user:evolve allow list,search,readattr,readextattr,readsecurity,file_inherit,directory_inherit"` | `setfacl -R -m u:evolve:rX <dir>` (existing tree) **+** `setfacl -R -d -m u:evolve:rX <dir>` (default ACL = inheritance for new files) | **Verb collapse is honest:** macOS's fine-grained verbs (list/search/readattr/readextattr/readsecurity) are all facets of "read + traverse"; POSIX `r` on files and `rX` (execute-only-if-directory) is what we actually use them *for*. `readsecurity` has no POSIX analogue and needs none — POSIX ACLs are readable by anyone who can reach the file. |
| evo write grant (`read,write,delete,append,…` + inherit) | `setfacl -R -m u:evo:rwX` + matching `-d` default ACL | macOS `delete` ≈ POSIX write-on-containing-directory; the `rwX` grant on the dir covers create/delete/rename within it, which is exactly what `os.replace` in the proposal/signal stores needs (the evo-account-separation EACCES fix carries over). |
| Negative carve-outs: `chmod -N <path>` + `chmod 700` (credentials/, profile `.md`s) | `setfacl -b <path>` (strip access ACL) + `setfacl -k <path>` (strip default ACL, dirs) + `chmod 700/600` | `perms.py` needs an explicit `clear_acl(path)` primitive — the carve-outs are load-bearing (they're what keeps `evolve` out of bot API keys, threat-model §3.1). |
| ACE-presence drift check (`_acl_user_present`, parses `ls -le`) | parse `getfacl` — and check **effective** perms, not just ACE presence | See the mask caveat below; this is the one place the Linux check must be *stronger* than a literal translation. |

### The POSIX ACL mask caveat (the sharp edge — read this one)

POSIX ACLs have no macOS analogue for this: once a file has a named-user ACE,
the **group permission bits become the ACL mask**, and the mask caps every
named-user/group entry's *effective* permissions. Two concrete consequences:

1. **Any later `chmod` that touches group bits silently disables the ACL.**
   `chmod 700` on `.openclaw/` after `setfacl` zeroes the mask → evolve's `r`
   ACE remains visible in `getfacl` but grants nothing. On macOS, mode bits
   and ACLs are orthogonal; on Linux they interlock. Every code path (and
   habit) that "tightens" modes can break the read contract without any
   command failing.
2. **Mode-assertion code lies on ACL'd files.** `auth-profiles.json` is
   asserted 0600 in security audits; an ACL'd file *displays* the mask in the
   group triad (`-rw-r-----+`), so naive `stat.st_mode == 0o600` checks will
   false-positive drift — and "fixing" them with `chmod 600` triggers
   consequence 1.

Mitigations, all inside `perms.py` so call sites stay ignorant: the check
primitive parses `getfacl`'s `#effective:` annotations (ACE ∩ mask), the apply
primitive re-asserts the mask (`setfacl -m m::rX`) after any mode change, and
the existing pod-perms drift monitor (installed by
`_install_launchd_pod_perms_drift_monitor`, which itself becomes a `JobSpec`)
covers regressions operationally. Mode-assertion sites get a `perms.py` helper
(`effective_mode()`) instead of raw `stat`.

**Kernel-enforcement note (W7c correction, 2026-06-17):** POSIX.1e *says* the
kernel must *deny* a named-user ACE whose mask has been zeroed — but on stock
Ubuntu 24.04 that enforcement is **inconsistent**, not a dependable DENY. On one
box (DigitalOcean `s-4vcpu-8gb`, ext4, kernel 6.8.0-71) a manual file-read probe
(runbook §5) was DENIED, yet the e2e harness's own traverse-then-read clobber
case ALLOWED the read (`rc=0`) on that *same* box — see the §9 real-VM findings.
W6 had briefly tightened the harness to **assert** the denial off-CI from that
single manual DENY; **W7c reverts that** — the masked-ACE step is now
**observe-only on every run** (records allow-vs-deny, never fails). The likely
reason the two cases diverge: the manual probe masks the *file's own* read ACE,
while the harness masks a *directory's* traverse ACE and then opens a file
beneath it — `getfacl` reports the clobbered `#effective:---` consistently in
both, but live kernel enforcement of that mask on the traverse path is not
reliable. **Do not rely on kernel mask enforcement.** Evolve's posture is
unchanged: the credentials carve-out (no-ACE + `chmod 700`) is the security
boundary (DENIED on every VM), and a clobbered mask is an availability bug that
**detect** (`getfacl` `#effective:` drift) + **repair** (`setfacl -m m::rX`)
own — both asserted strictly in the harness.

One more inheritance nuance: POSIX default ACLs apply at *creation* in the
directory — a file `mv`'d/renamed in from elsewhere keeps its source ACL.
Our stores already create temp files in the destination directory before
`os.replace` (so they inherit), and `/tmp`-staging uses `sudo cp` (creates
fresh → inherits). The rule to enforce in review: **create-in-place or copy,
never rename across directory boundaries into an ACL'd tree.**

| Alternative to POSIX ACLs | Trade-offs |
|---|---|
| Group-based access (put `evolve` in each bot's group, setgid dirs) | No mask sharp-edge, ancient and boring — but groups can't express the carve-outs (credentials/ must exclude evolve while the parent grants it), can't do per-user write grants like evo's without a group-per-pair explosion, and umask discipline across OpenClaw + our writers is unenforceable. The macOS model is named-user ACEs; groups can't translate it. |
| NFSv4 ACLs (richacl) | Verb-granularity parity with macOS, but not enabled on stock Ubuntu ext4 — non-starter for "default Hetzner image". |
| Sudoers-only access (no ACLs; every read via `sudo /bin/cat`) | This is the pre-`set_evolve_read_acl` world the ACL work explicitly retired — it widens sudoers grants (threat-model §2's *most* sensitive table row) to cover every read path. Regression, rejected. |

---

## 5. sudoers — same files, fewer quirks; one writer, two command tables

**Recommendation: keep the `/etc/sudoers.d/evolve` + `/etc/sudoers.d/evolve-admin`
structure verbatim; parameterize `_write_evolve_sudoers()` (setup_wizard.py,
mirrored in cli.py) by the platform_profile's command table; keep writing in
the macOS-safe strictest-common-denominator syntax.**

What carries over and what changes:

- **Full paths carry over thanks to Ubuntu's /usr-merge.** `/bin/cat`,
  `/bin/chmod`, `/bin/cp` all resolve on Ubuntu 24.04 (as symlinks into
  `/usr/bin`). The full-path discipline (CLAUDE.md's macOS path table) was the
  hard part and it's already done.
- **A few commands differ per-OS regardless:** `launchctl` → `systemctl`,
  `/usr/sbin/chown` → `/usr/bin/chown`, `dscl`/`sysadminctl` → `useradd`/
  `userdel`. These grants are derived from what the adapters invoke, so the
  sudoers writer should consume its command list **from the platform_profile
  table the adapters also use** — one source of truth, no drift between "what
  evolve may sudo" and "what the adapter runs".
- **macOS visudo quirks don't apply on Linux** (trailing `/*` accepted, no
  escaped-dot weirdness) — but the recommendation is to keep the macOS-safe
  subset anyway, so a rule rendered for either platform validates on both and
  the writer stays one code path. Cost: we forgo Linux-only sudoers
  conveniences nobody is asking for. `visudo -c -f <tmpfile>` validation
  before install stays mandatory on both.

| Alternative | Trade-offs |
|---|---|
| Per-platform sudoers writers (fork the function) | Lets each platform use idiomatic syntax; doubles the most security-sensitive string-emitting code in the repo and invites drift between the two. Rejected — this is exactly the 6-emitters-of-plist-XML mistake in miniature. |
| Drop sudoers on Linux, run admin daemon as root | Simpler grants, catastrophic blast-radius regression; the entire §1.3 tier table exists to avoid this. Not seriously on the table; recorded for completeness. |

---

## 6. Paths — a `platform_profile` module; do it with Phase 6.2

**Recommendation: one frozen-per-OS `platform_profile` dataclass that owns
every platform-divergent path and command location; migrate path *construction*
sites onto it; sequence the sweep after (or with) Phase 6.2's util dedup.**

The measured shape of the problem (research doc §2): `/Users/` appears in 576
py files, but ~600 call sites already go through `bot_home()`/`get_bot_user()`
— which resolve via `pwd.getpwnam` first and are **already POSIX-portable**.
The real debt is ~230 direct f-string `/Users/{...}` literals, plus the
hardcoded singletons. The profile:

| Concern | macOS value | Linux value (proposed) | Notes |
|---|---|---|---|
| User home root (construction only) | `/Users/{u}` | `/home/{u}` | Only for *creating* users and the f-string literal cleanup; *resolution* stays `pwd.getpwnam` (never reintroduce path math — auto-memory: bot_id ≠ account name). |
| Shared dir default | `/Users/Shared/evolve` | `/var/lib/evolve` | FHS-correct for service state. Alternative `/home/Shared/evolve` shim: zero translation but un-idiomatic and confuses every Linux admin who looks; alternative `/opt/evolve`: conventionally for *software*, not state. `{shared_dir}` is already config-resolved nearly everywhere, so this is a default, not a migration. |
| Deploy checkout | `/Users/Shared/evolve-repo` | `/var/lib/evolve/repo` (or sibling `/var/lib/evolve-repo`) | Same read-only + repo-puller model. §12 Q4. |
| Daemon dir | `/Library/LaunchDaemons` | `/etc/systemd/system` | Consumed only by the two Scheduler adapters after S2 — that's the gate paying off. |
| Command paths | `/bin/cat`, `/usr/sbin/chown`, `/bin/launchctl`… | `/bin/cat`, `/usr/bin/chown`, `/usr/bin/systemctl`… | Single table feeding subprocess calls *and* the sudoers writer (§5). |
| Runtime install | Homebrew (`/opt/homebrew` / `/usr/local`) | apt + NodeSource (Node 24 per upstream recommendation) | Wizard-only concern (L3); no Homebrew analogues needed at runtime. |

| Alternative | Trade-offs |
|---|---|
| **`platform_profile` module (recommended)** | One place to audit; adapters and wizard consume it; a third platform later is one more dataclass. Cost: one new module everyone must learn to use instead of writing the literal — needs a ratchet lint (forbid new `/Users/` literals outside the profile + tests) or it erodes like the plist emitters did. |
| `sys.platform` checks at each divergent site | No new abstraction, but ~230+ scattered conditionals is precisely the diffuse-coupling disease 4.3 exists to cure. Rejected. |
| Config-file indirection (paths in `network.json`) | Maximum flexibility nobody asked for; turns constants into runtime state that can drift per-pod and break the "fresh install must work" invariant. Rejected. |

**Phase 6 interplay — the "do together?" question.** Phase 6.2 deduplicates
the shared primitives, including **16 copies of `_bot_home`** (several carrying
the bot_id≠account bug class). Sweeping paths *before* 6.2 means migrating 16
copies and re-migrating after the dedup; sweeping *after* means touching each
site once. Recommendation: **6.2 (at minimum the `_bot_home`/path-helpers
slice) lands before or with the portability sweep; full 6.1 packaging is
desirable-not-gating.** The sweep and 6.2 touch the same lines — bundling them
is less total churn, at the cost of a bigger combined diff to review (§12 Q5).

### W7 — deploy/installer layer port (as-built, 2026-06-17)

L1–L3 ported the **seams** (scheduler/isolation/perms/platform_profile) and the
wizard, but the `linux-e2e` harness drove those seams *directly* and never
exercised `deploy.py`'s plugin-install + venv-management orchestration — so the
gap below shipped invisibly. A live install on a real Ubuntu VPS
(`evolve-vsp-pod`) cleared the wizard (W1–W3) and then died at the deploy step:

1. `installer.setup_shared` chowned the shared dir `root:wheel` — `wheel` is the
   macOS gid-0 group; on Linux gid 0 is `root` and `wheel` isn't gid 0 (and
   doesn't exist by default on Debian/Ubuntu). → chown failed on a fresh box.
2. `deploy.py` hardcoded `/Users/Shared/evolve-{venv,plugin}` and the venv
   `python3`/`evolve-admin` binaries. → "No such file or directory" on Linux;
   the gateway never started.
3. The deploy flow ASSUMED the canonical venv pre-existed (macOS bootstrap
   builds it); on a fresh Linux box nothing built it.

Closed in two PRs:
- **platform_profile** gained `admin_group` (wheel/root), `venv_dir`,
  `plugin_install_dir`, `venv_evolve_admin`; deploy/installer route through them
  (no `sys.platform` at call sites).
- **`installer.ensure_evolve_venv()`** builds the canonical venv on a fresh
  Linux pod (analyzer-first, compat-editable — §8.2 venv contract made
  concrete), wired into `reinstall_evolve_admin` (the deploy funnel).
- **linux-e2e** gained step 6b: the deploy flow builds `/var/lib/evolve-venv`,
  the built venv imports the packaged code, and the plugin dir chowns
  `root:root` — so the gap can't silently return.
- macOS byte-identity preserved (both profiles pin to the pre-W7 literals).

**W7c follow-up (2026-06-17, the deploy-extended real-VM run).** Two issues the
real-Ubuntu deploy-flow run surfaced after W7 merged:
1. **`ensure_evolve_venv` died on stock Ubuntu:** it built the venv with
   `python3 -m venv`, but Ubuntu ships `venv`/`ensurepip` in the separate
   `python3-venv` apt package (absent by default) → "ensurepip is not
   available". Fixed by building the venv via **`uv venv --clear --seed`**
   (`_find_uv()` resolves uv across `sudo`'s reset PATH) — uv creates
   virtualenvs without `ensurepip`, and is already the project's venv tool;
   `--seed` populates pip so the compat-editable installs below are unchanged,
   and `--clear` keeps the build idempotent on a deploy retry (we only reach it
   when `venv_python` is absent, so any dir still at `venv_dir` is partial/stray
   state to wipe — matching stdlib `venv`'s in-place self-heal). Falls back to
   stdlib `venv` when uv is absent (a host with `python3-venv` but no uv). macOS
   is untouched (the venv pre-exists → early return; both profile literals
   unchanged). The `release_manager._ensure_staging_venv` canary path had the
   *same* `python3 -m venv` shape — **now fixed** (W7c-followup): it imports the
   same `installer._find_uv()` and builds via `uv venv --clear --seed --python
   <deploy interpreter>` (preserving the `_maybe_as_evolve(...)` re-exec and
   `cwd=staging`), falling back to stdlib `venv` when uv is absent. macOS stays
   byte-identical (staging venvs already build there; the resolver just finds
   Homebrew's uv or falls back). Pre-GA-acceptable until then because no Linux
   pod runs canary mode yet; it MUST be in place before Linux canary goes live.
2. **W6's masked-ACE assertion reverted** — see the §4 kernel-enforcement note
   and the §9 (c) correction.

**Audit of OTHER macOS-isms** in `deploy.py`/`installer.py` (beyond the two
reproduced failures):

- **None** un-seamed: no `launchctl`/`dscl`/`pmset`/`sysctl`/`security`(Keychain)
  calls outside the scheduler/isolation seams (the §3 / 4.3C work covers them).
- **`chown` binary path** (`/usr/sbin/chown` macOS vs `/usr/bin/chown` Linux):
  routed at the venv/plugin-flow sites (`_SUDO_CMD_PATHS`, `build_plugin`) +
  `lsof`. **~25 inline `/usr/sbin/chown` literals remain** in the per-bot config
  + `ensure_pod_perms`/`ensure_bot_perms` paths — **deferred (W7-followup)**.
- **`wheel`/`staff` group literals** in those ~25 chowns: `staff` exists on
  Ubuntu (gid 50) so `bot:staff` chowns are benign-but-non-idiomatic, but the
  `evolve:wheel`/`bot:wheel` ones are **hard failures** on Ubuntu (no `wheel`
  group) → must become `:{admin_group}` (root). They run in the
  perms-enforcement path of a full *bot* deploy. **Deferred.**
- **macOS ACL syntax** `chmod +a` / `-N` (`build_plugin` dist restore;
  `ensure_pod_perms` repo-pkg grant): macOS-only; on Linux they no-op/fail
  silently (`capture_output`, no `check`) → should route through the `perms`
  seam (`setfacl`). **Deferred.**
- **Homebrew PATH** (`/opt/homebrew/bin:/usr/local/bin:…` in `build_plugin` and
  the daemon JobSpec env): functionally **harmless** on Linux (the macOS dirs
  are simply absent from PATH; `/usr/bin:/bin` find Node) — the only live edge
  is the `brew install node` hint in the Node-missing error string. **Deferred
  (cosmetic).**
- **macOS-posture code** (launchd plist rendering, `~/Library/LaunchAgents`,
  `plistlib` reads in `restart_gateway`): gated OUT on Linux by the scheduler
  seam (SystemdScheduler renders units) — not a Linux runtime hazard.

**W7-followup bite — as-built (2026-06-16):** the deferred chown/ACL sweep
shipped. What was done, per category:

- **`chown` binary** — every inline `"/usr/sbin/chown"` literal in `deploy.py`
  (25, incl. one commented example) now routes through `_PROFILE.chown`
  (`/usr/sbin/chown` macOS / `/usr/bin/chown` Linux). Uniform: a `grep
  '"/usr/sbin/chown"' deploy.py` is now empty. Byte-identical on macOS.
- **`wheel` group** — every chown *command* site (`evolve:wheel`,
  `bot:wheel`, `root:wheel`, `{user}:wheel`, and the `_check_dir_owner`
  fix_description) now routes `wheel → _PROFILE.admin_group` (`wheel` macOS /
  `root` Linux). The marquee hard-failures (`evolve:wheel` in
  `_ensure_evolve_owned_dir_perms` / `_ensure_evo_write_acl` / proposal-lifecycle
  / network.json; `bot:wheel` in `ensure_bot_perms`/`fix_shared_dir_permissions`;
  `{user}:wheel` in `_set_dir_owner`) are all routed. Remaining `wheel` strings
  in the file are comments/docstrings + the one `_check_evo_write_acl`
  fix_description that narrates the macOS `chmod +a` ritual (only renders
  post-account-separation, a macOS-only Phase E feature where no `evo` user
  exists on Linux yet — `_check_evo_write_acl` returns the skip path there).
- **`staff` group** — deliberately LEFT literal (binary routed, group kept).
  `staff` exists on Ubuntu (gid 50) so `<bot>:staff` chowns succeed there.
  Resolving the account's real primary group per-OS is a follow-up; routing it
  now risks macOS byte-identity (the evolve user's primary group on the mini is
  not guaranteed `staff`). A module-level note in `deploy.py` records this.
- **macOS ACL ops** — `chmod -R -N` (build_plugin dist-restore) → the perms
  seam's new `clear_acl(path, recursive=True)` (macOS `chmod -R -N`; Linux
  `setfacl -R -b/-k`). `chmod +a` / `-R +a` (`deploy_shared_dir` repo-pkg read
  grant) → `get_perms().grant_read_recursive(pkgs, EVOLVE_SERVICE_USER)` — the
  ACE string was byte-identical to the seam's read contract. Both byte-identical
  on macOS.
- **Cellar check** — `_check_cellar_perms` now has an explicit non-macOS gate
  (returns an OK/skip `_PermCheck`) so the Homebrew Cellar check + its
  `_apply_cellar_perms` chmod are never reached on Linux, rather than relying on
  the incidental `/opt/homebrew/Cellar` absence.
- **Node-missing hint** — `_check_node_version` now emits a platform-appropriate
  install hint (`brew install node` macOS; NodeSource/apt on Linux, reusing the
  setup wizard's canonical `_LINUX_NODE_HINT` via a lazy import so the version /
  channel never drifts from the wizard's prereq row).
- **macOS-launchd-posture chowns** (`_write_plist`, `_install_launchd`,
  `install_staged_plists`, `_install_launchd_better_engine`): binary + `root:wheel`
  group routed for uniformity, but these functions remain macOS-only by
  construction (they render to `LAUNCHD_DIR=/Library/LaunchDaemons` and bootstrap
  via launchctl; on Linux the scheduler seam renders systemd units instead, and
  the function would fail at the `cp` to `LAUNCHD_DIR` before reaching the chown).
  The launchd→systemd daemon-install port is separate seam work (§3 / 4.3C), not
  this perms bite.

Proven on the live `ubuntu-24.04` runner by `test_step6c` in the linux-e2e:
a drifted `root:root` evolve-owned shared subdir is recovered to **`evolve:root`**
(0755) by the real `_ensure_evolve_owned_dir_perms` — the routed binary
(`/usr/bin/chown`) + `admin_group` (`root`) both work. Both-profile unit tests
(`test_deploy_chown_group_routing.py`) pin the macOS↔Linux divergence in-process;
the plist/sudoers/perms golden suites confirm macOS byte-identity.

### W10-E — first real interactive `setup --fresh` residuals (as-built, 2026-06-19)

The W10-D capstone harness (`test_step6g`) passed green, yet the **first real
operator-interactive** `EVOLVE_PLATFORM=linux evolve-admin setup --fresh
--platform linux` on a live DigitalOcean Ubuntu 24.04 VPS hit a cascade — the
recurring "green harness yet real install fails" lesson. Findings + fixes:
diag at [diag-w10e-fresh-install-residuals-2026-06-19.md](diag-w10e-fresh-install-residuals-2026-06-19.md).

**§8.2 source-location contract — now formalized (the long-deferred
"path+ownership still to formalize" item).** The box staged the source at
`/root/evolve`; `/root` is mode `0710`, so the `evolve`/`evo`/bot users cannot
traverse it. Because `deploy.py::_REPO_ROOT` is baked into the venv editable
`.pth` AND ~50 daemon `ExecStart=`/`ANALYZER_DIR` paths, the admin UI
crash-looped and the whole evolve fleet failed with ModuleNotFoundError. The
contract:

- **Canonical deploy checkout = `/var/lib/evolve/repo`**
  (`platform_profile.LINUX.deploy_checkout_default`). **NOT `/root/…`.**
- The source root and every ancestor MUST be traversable by the `evolve` service
  user — either world-traversable (`0755`) or `evolve`-owned. The repo-puller
  follows the same read-only model.
- The wizard enforces this with a Linux preflight
  (`setup_wizard._preflight_repo_root_traversable`, run right after the platform
  gate): when `evolve` exists it does the faithful `sudo -n -u evolve test -r`
  on a deep source file; on a true fresh install it scans every ancestor for the
  `o+x` traverse bit. Either way it HARD-FAILS early with the staging remediation
  rather than producing a broken install. Runbook
  [runbook-vps-pod-provision.md](runbook-vps-pod-provision.md) §6 step 2 carries
  the operator-facing version.

**The rest of the W10-E cascade** (each platform-keyed; macOS byte-identical):

- **Step-14 gateway install.** `restart_gateway` keyed off the macOS
  `/Library/LaunchDaemons/<label>.plist`, which never exists on Linux → a
  near-total no-op that falsely printed "✓ Gateway restarted" while nothing bound
  the port. Now routes through the systemd seam: restart when the unit is
  installed, **install it if absent** (`install_bot_gateway_plist`), else raise.
- **systemd log dirs.** launchd auto-creates the dir behind
  `StandardOutput`/`StandardError`; **systemd does not** (admin-ui hit 39
  restarts, `209/STDOUT`). The `SystemdScheduler` seam now `mkdir -p` + chowns
  every unit's log-parent (to its `User=`) before `systemctl`.
- **evo primary `botId`.** The primary's evolve-plugin config could reach
  `openclaw plugins install` as `{}` ("must have required property 'botId'"): the
  telegram branch in `_evolve_openclaw_config` replaced the whole `plugins` block
  (now merges), and the evo install path skipped the pre-install
  `ensure_plugin_config` that the member-bot path runs (now mirrored).
- **Residual `/Users` + macOS-path leaks** the W7/W8 sweep missed:
  `repo_puller.py` chown binary + repo/shared defaults; `APPLY_LOCK_TEMPLATE`;
  the manifest-spec/schema + help-index docs roots (→ `_REPO_ROOT/docs`);
  `cwd="/Users/Shared"` subprocess defaults (→ `_PROFILE.scratch_dir`, which IS
  `/Users/Shared` on macOS); the dead `thresholds.json` writer (referenced a
  removed `DEFAULT_THRESHOLDS`/`_meta` shape and wrote to a location deploy
  actively deletes — removed; defaults ship in code); and the setup-complete
  recovery banner's `launchctl kickstart` hints (→ `systemctl restart` on Linux).

linux-e2e gained `test_step6h` (restart_gateway installs+binds a missing
gateway), `test_step6i` (the seam auto-creates the StandardOutput log dir),
`test_step6j` (the repo-root preflight rejects a `/root`-style source), and a
`botId` assertion in `test_step6d`; non-gated unit tests pin the log-dir
mkdir/chown, the telegram-merge, and the preflight branches. macOS goldens
byte-identical.

---

## 7. Keystore — file-vault v1; systemd-creds folded into Phase 2.2

**Recommendation: ship the port on the existing file-vault fallback with
honest labeling; make the Keychain probe fail fast on Linux; do *not* build a
Linux-native secret store in this port — fold that into Phase 2.2.**

The key fact (threat-model §6.2, keystore.py): the file-vault (XOR + machine
key, explicitly labeled "not cryptographically strong") is **already the
common path on macOS** — the headless `evolve` LaunchDaemon has no Keychain
session, so `KeychainUnavailable` → file-vault is what production pods do
today. A Linux pod on the file-vault is therefore *equal* security to a macOS
pod, not a downgrade. The port's only code change: `_store_value`'s Keychain
attempt should short-circuit on Linux (no `security` CLI) instead of
discovering its absence via subprocess failure, and the status/docs strings
must say "file vault (machine-key)" honestly rather than implying Keychain.

| Alternative | Trade-offs |
|---|---|
| **File-vault v1 (recommended)** | Zero new code; parity with today's real posture; keeps the port's security review surface small. Cost: the diligence finding "secrets not strongly encrypted at rest" (§6.2) now applies to two platforms — acceptable only because Phase 2.2 is already the tracked fix for both. |
| `systemd-creds` now | The *right* eventual Linux answer for a headless server (TPM2-sealed where hardware allows, no session daemon needed, fits the LoadCredential= unit model). But it's per-unit plumbing across every service that reads secrets — that's Phase 2.2's redesign (make the strong store mandatory), not a port task. Doing it Linux-only would fork the keystore model across platforms. |
| `libsecret`/gnome-keyring | Wrong topology — needs a user session/keyring daemon; headless VPS is exactly where it doesn't work. Rejected. |

Tie-in recorded for Phase 2.2: when 2.2 makes the strong store mandatory, the
per-platform strong backends are macOS Keychain and `systemd-creds`, behind
the same `_store_value`/`_retrieve_value` seam that exists today.

---

## 8. Channel matrix honesty — iMessage is macOS-only

**Recommendation: the channel matrix (wizard + admin UI + docs) must be
platform-aware: a Linux pod never renders an iMessage affordance.** Upstream
requires a macOS host for the iMessage channel; this is a hard constraint, not
a missing feature.

This is the product-defaults rule applied (auto-memory: fresh install must not
render dead affordances): showing iMessage on a Linux pod and failing at
connect time is the dead-affordance anti-pattern. Implementation shape: the
channel catalog gains a `platforms:` field consumed wherever channels are
offered; everything else (Slack, Telegram, Discord, etc.) is platform-neutral.

Alternatives: (a) show-but-disable with an explanatory tooltip — defensible
UX for discoverability ("this exists, needs a Mac"), and a docs page noting
upstream's SSH-wrapped-Mac-relay answer covers the curious without UI
clutter; (b) build/bless the Mac-relay path — out of scope, it's upstream's
mechanism and a second host contradicts the single-machine threat model (§7
out-of-scope list). Recommendation is hide-on-Linux + one docs paragraph;
weak-opinion territory (§12 Q6).

---

## 9. CI — one ubuntu-runner end-to-end job; feature-gated product

**Recommendation: a GitHub Actions `ubuntu-24.04` job that exercises the real
adapters end-to-end with one bot; the Linux product path itself ships behind
an explicit experimental gate so macOS pods are untouched.**

Why the GH runner directly (alternatives: nested VM via qemu/vagrant — slower,
flakier, more to maintain; systemd-in-docker — notorious pain, rejected):
GitHub's ubuntu runners are full VMs with systemd as PID 1 and passwordless
sudo, which is everything the adapters need — `useradd` a bot user, `setfacl`,
install real units under `/etc/systemd/system`, run them. The job is the
Linux analogue of the 8.3 exit criterion: *a bot deploys, runs, and is
administered on Linux* — using `FakeRuntime` or a stub agent process where the
OpenClaw gateway would be (the agent runtime is out of scope per §0; CI
shouldn't spend tokens or keys proving upstream's Linux support).

Tiering, consistent with the 2-core CI economics (auto-memory: shard, don't
xdist): unit tests of the renderers (JobSpec→unit-file golden tests, mirroring
S0's plist golden tests; `getfacl` parse fixtures) run in the normal suite on
every PR; the full e2e job is one new runner, path-filtered to the adapter/
profile/wizard surfaces.

**Feature gating:** today `setup_wizard.py:3069` hard-fails non-macOS — that
check *stays*, becoming the gate: Linux proceeds only under an explicit opt-in
(e.g. `EVOLVE_PLATFORM=linux` env or `--platform linux` wizard flag) until
parity is declared. The gate is platform *detection*, not scattered
conditionals — code paths select adapters via `get_scheduler()` /
`get_isolation()` / `platform_profile`, so "gated" means the wizard refuses,
not that modules branch. macOS pods see zero behavior change from any Linux
PR; that's the gate's proof obligation (the existing macOS CI suite is the
regression net).

### The `linux-e2e` job, as built (W5B — 8.3's exit criterion)

The job lives in `.github/workflows/ci.yml` (`linux-e2e`, `runs-on:
ubuntu-24.04`); the harness is
`packages/admin/tests/e2e_linux/test_ubuntu_e2e.py` — seven ordered pytest
steps whose `-v` output is the run journal. What each step proves, live
against the real OS:

1. **Platform gate** — `_resolve_platform_gate()` with the real
   `sys.platform` + `EVOLVE_PLATFORM=linux` proceeds, pins the LINUX
   profile, activates `LinuxUserIsolation` + `SystemdScheduler`.
2. **Sudoers** — `_render_evolve_sudoers()` (LINUX command table) passes a
   REAL Ubuntu `visudo -c -f` — including the `m\:\:rwX` escaped-colon
   mask-repair grants that were W5A's open syntax risk — and
   `_write_evolve_sudoers()` installs it without breaking the host config
   (`visudo -c` whole-config check).
3. **Accounts** — real `useradd` via `LinuxUserIsolation`: `evolve` + one
   stub bot, verified through `getent` (uid, home), `evolve-bots`
   inventory-group membership, `run_as` identity, and `sudo -n -l` as
   `evolve` showing the installed grants resolve for the principal.
4. **Perms** — real `LinuxPerms`: recursive read grant (access + default
   ACLs) verified by *effective* perms (`getfacl` `#effective:` parsing)
   AND kernel-enforced reads as `evolve`; default-ACL inheritance onto a
   file created later by the bot; the `credentials/` carve-out (`evolve`
   cannot read bot keys — threat model §3.1); the POSIX mask sharp edge
   end-to-end (`chmod g-rwx` silently disables the ACE → check sees drift
   [**asserted**], kernel mask enforcement of the masked read **observe-only**
   on every run per the W7c correction — inconsistent on stock Ubuntu, see the
   findings below → `reassert_mask` repairs both [**asserted**]); the
   `workspace/evolve` write contract.
5. **Scheduler** — real systemd units via `SystemdScheduler` for a STUB
   agent (python heartbeat, `User=<bot>` — no OpenClaw, no tokens):
   install → active with the bot's uid; the restart *guarantee* (fresh
   MainPID after `restart()`); the byte-identical install skip (repeat
   deploys don't bounce); a timer JobSpec firing on install
   (`OnBootSec=0`); clean `remove()` (unit files gone, processes gone).
   Plus live grant probes: `evolve` can `sudo -n systemctl is-active` the
   gateway unit, and is REFUSED a verb outside its grant table.
6. **Admin smoke** — the real Flask admin server, installed through the
   same Scheduler seam as a systemd unit with `User=evolve` (the real-pod
   shape), reading a Linux-path `network.json` under `/var/lib/evolve`;
   `GET /`, `/api/health`, and `/api/status` (naming the deployed bot) all
   return 200; clean shutdown through the seam.
7. **Sentinel** — writes `COMPLETED` into the diag dir; the workflow fails
   if it's missing, so an accidentally all-skipped run can't pass green.

**Guard semantics** (the harness refuses to run anywhere else): all of
*Linux host* AND *`CI` env set* AND *`EVOLVE_PLATFORM=linux`* AND *not a
sharded suite run* (`EVOLVE_NUM_SHARDS` unset). A dev Mac and every pod
fail the first condition; a real Linux pod fails the second; the regular
admin CI shards fail the third and fourth. The job is path-filtered (the
`changes` job's `linux_e2e` output: seam adapters, `platform_profile`,
`evolve_config`, wizard/deploy/keystore, the perms/sudoers/systemd test
families, the harness, the workflow) and always runs on main pushes.

**Running it locally:** use a DISPOSABLE Ubuntu 24.04 VM (multipass, UTM,
…) — the harness creates users, installs `/etc/sudoers.d/evolve`, and
writes `/etc/systemd/system` units (cleanup runs even on failure, but
don't point it at a machine you care about):

```bash
sudo apt-get install -y python3-pip acl && pip install uv
git clone <repo> && cd evolve && uv sync --locked
cd packages/admin
CI=1 EVOLVE_PLATFORM=linux \
  uv run --no-sync python -m pytest tests/e2e_linux/ -v -s
```

Diagnostics (rendered sudoers, getfacl dumps, admin-server logs) land in
`EVOLVE_E2E_DIAG_DIR` (default `/tmp/evolve-linux-e2e-diag`); in CI they
upload as the `linux-e2e-diagnostics` artifact on failure.

### Scheduler-seam portability — as-built addendum (2026-06-13)

The §3 scheduler seam shipped, but a post-spike coordinator audit found
`get_scheduler()` is **not** profile-dispatching — Linux works only because
the platform gate injects `set_scheduler(SystemdScheduler())` process-wide.
**~9 daemon/operator call sites still constructed `LaunchdScheduler(...)`
directly in module-globals**, bypassing that injection → they invoked
`launchctl` on a Linux pod (e.g. repo-puller's every-15-min kickstart; the
RSI resolver fired a false `launchd_not_loaded` Signal per bot per cycle).
The argv=0 gate never caught it — it bans raw launchctl *argv*, not adapter
*construction*. Closed in 9 PRs (audit `docs/audit-scheduler-seam-portability-2026-06-11.md`;
bites #2733/#2737/#2743/#2738/#2740/#2752/#2755):
- **Per-call `timeout`** added to the `Scheduler` Protocol verbs (#2733) so
  posture-divergent sites can route through the singleton.
- Sites migrated to `get_scheduler()` (clean swap where posture allows) or the
  **guarded-derive** pattern (`isinstance(get_scheduler(), LaunchdScheduler)`
  → posture-customized adapter on macOS, injected adapter on systemd) where
  `use_sudo=False`/`sudo_non_interactive`/run-as-bot posture is load-bearing;
  `gui/<uid>`-domain ops (no systemd analogue) platform-gate-out on Linux.
- **linux-e2e extended** (#2757) to drive kickstart/metric/recovery/retire
  through their real entrypoints under `SystemdScheduler` — **green**, so the
  daemon *lifecycle* (not just first-deploy) is proven administered on Linux.
- **`tools/scheduler-factory-lint`** (#2800, AST, CI `--strict`, no baseline)
  now bans adapter construction outside an allowlist (the seam + the platform
  gate + the verified guarded-derive accessors + tests), so this can't regress.

### Linux real-VM manual pass — findings (2026-06-17)

The `linux-e2e` harness runs green on GitHub's `ubuntu-24.04` runners every
main push, but those runners are a *customized* image. The GA residual
"real-Ubuntu-VM manual pass" (roadmap 8.3) asks for the same harness on an
unmodified stock Ubuntu host, plus a human visudo eyeball and a stock-kernel
answer to the masked-ACE question. This records the **early half** of that
residual (the full-wizard half waits on the wizard remainder being ported);
procedure in [runbook-linux-vm-pass-2026-06-11.md](runbook-linux-vm-pass-2026-06-11.md).

- **VM / env:** DigitalOcean droplet `evolve-vsp-pod`, Basic `s-4vcpu-8gb`,
  region SFO3, **Ubuntu 24.04.3 LTS**, kernel **6.8.0-71-generic**, `x86_64`
  (a real cloud VPS — §1's co-#1 install cluster — not the runbook's
  Apple-Silicon Multipass default; the un-customized stock image is the point).

#### (a) Harness
- **PASS — 11/11** in ~16s: the seven ordered steps (`test_step1`..`step7`)
  plus the four ongoing-lifecycle steps (`step5b`/`5c`/`5d`/`5e`). Real
  `_resolve_platform_gate` → LINUX profile + adapters; real `useradd` for
  `evolve` + `e2ebot`; real `setfacl`/`getfacl` effective-perm + carve-out
  checks; real systemd start/kickstart/retire through the scheduler seam; the
  real Flask admin server answering `/`, `/api/health`, `/api/status` as
  `User=evolve`; the completion sentinel.
- **Teardown clean** — no leftover `evolve`/`e2ebot` users and no residual
  `/etc/sudoers.d/evolve` after the module finalizer ran.
- **Deviations from CI:** none functional. One nit surfaced — a
  `DeprecationWarning: datetime.datetime.utcnow() is deprecated` from
  `setup_wizard.py:270` (the audit-log timestamp); fixed in this PR's first
  commit (timezone-aware `datetime.now(timezone.utc)`, byte-identical `…Z`).

#### (b) Visudo / mask-grant eyeball
- Real `visudo -c` accepted the rendered Linux sudoers, **OK** — including the
  escaped-colon `m\:\:rwX` mask-repair grants (W5A's open syntax risk), which
  install and parse on a stock host exactly as on the runner image.

#### (c) Masked-ACE kernel question — **INCONSISTENT** (W7c correction)
- With a textbook clobbered state (`chmod g-rwx` → `mask::---`, ACE
  `#effective:---`), the result is **not a dependable DENY** on stock Ubuntu
  24.04:
  - the **manual file-read probe** (runbook §5: read `secret` as the
    `maskprobe` user, whose *file's own* read ACE is masked) was **DENIED**
    (`cat: Permission denied`, `rc=1`);
  - but the **harness's own clobber case** (`test_step4`: mask the *directory*
    `workspace`'s traverse ACE, then `cat` a file beneath it as `evolve`)
    **ALLOWED** the read (`rc=0`) on the **same box** (ext4, kernel 6.8.0-71).
- **W7c correction of the earlier conclusion.** The 2026-06-11 record had read
  the manual DENY as proof that the GitHub-runner *allow* was a runner-image
  quirk and that real hosts enforce the mask. The same-box harness ALLOW
  disproves that: stock-Ubuntu mask enforcement is **condition-dependent**, not
  POSIX-reliable. `getfacl` reports the clobbered `#effective:---` consistently
  in both cases — only **live kernel enforcement** diverges, and only the
  *traverse-through-a-masked-directory* path is unreliable (the masked *file's
  own* read ACE denied). Plausible cause: the kernel evaluates the named-user
  entry against the mask differently when the masked entry authorizes traverse
  on a path component vs. read on the leaf; not chased further because the fix
  is the revert regardless.
- **Posture (unchanged, now correctly stated):** Evolve never relied on mask
  enforcement as a security boundary — the credentials carve-out (no-ACE +
  `chmod 700`) is the boundary (DENIED on every VM) and a clobbered mask is an
  availability bug that **detect+repair** owns. **Do not rely on kernel mask
  enforcement.** There is **no backstop** to lean on.

#### Follow-ups taken
- **W6 (superseded):** briefly tightened the masked-ACE step in
  `test_step4_perms_acls_carveout_and_mask` to **assert** the denial off-CI
  (env-gated on `GITHUB_ACTIONS`) from the single manual DENY.
- **W7c (current):** **reverted** that to **observe-only on every run** — the
  step records allow-vs-deny and never fails — because the same-box harness
  ALLOW shows the assertion was over-fit to one probe. The detection
  (`getfacl` `#effective:` drift) and repair (`reassert_mask`) around it stay
  **asserted strictly**; the forensic prints stay either way.
- This satisfies the **early half** of the §9 GA residual "real-Ubuntu-VM
  manual pass". Still open for the residual's late half: the full-wizard pass
  (waits on the wizard remainder being ported) and the broader parity/soak GA
  un-gating evidence package.

---

## 10. Non-goals

Recorded so scope creep has to argue with a list:

- **Windows** — deferred with a recorded revisit trigger (research doc §4c:
  upstream ships first-party Windows *service* tooling AND >15% of
  first-person hardware mentions). WSL2 inherits this port as best-effort
  *documentation* later; no Windows-native adapters, no code.
- **Shared / multi-tenant Linux boxes** — same break as shared Macs
  (threat-model §2); out of scope until the Phase 2 residuals + threat-model
  rewrite, and even then a product decision.
- **Raspberry Pi as a supported target** — the Ubuntu ARM64 build leaves Pi
  *unblocked-but-untested* (upstream ARM regressions noted in the research
  doc §1); we don't test it, document it, or block on it.
- **Containers as isolation** — §2; a possible future second
  IsolationProvider, not part of this port.
- **Non-Ubuntu distros** — Debian-family will likely work (same systemd, same
  ACL tooling) but only Ubuntu 24.04 LTS is tested/supported; the real
  contract is "systemd ≥ 255 + POSIX ACLs + /usr-merge" (§12 Q3).
- **iMessage on Linux** (§8) — upstream constraint; document, don't build.
- **Web-exposed admin UI / built-in reverse proxy** — §1; SSH tunnel only
  until Phase 2.6/2.7 change the calculus.
- **journald migration for log readers** — §3; file logs keep parity.
- **macOS pods adopting any Linux-motivated behavior change** — parity means
  the launchd adapter's rendered output stays golden-test-identical
  throughout.

---

## 11. Sequencing, build gates, and estimates

**Hard gates — no build before both:**

1. **The project owner reads this spec and the §12 questions get answers**
   (the 8.1 decision record: Linux is GO *spec-first, discuss before build*).
2. **4.3 C is done** — specifically Track S **S2 at zero non-test `launchctl`
   call sites** (the same `git grep` gate Phase B used) and Track I migrated.
   The 4.3 C doc's warning is the reason verbatim: starting the port against a
   half-migrated scheduler forks behavior between direct callers and the
   adapter. Current state: S0 spec ready, nothing built; Track I ~1 session,
   Track S 2–3 sessions.

**Steps after the gates (≈5–8 sessions, matching the research doc §4b):**

| Step | Contents | Sessions | Depends on |
|---|---|---|---|
| L1 — adapters | `SystemdScheduler` (replaces the Phase-D stub) + `LinuxUserIsolation`; unit-file golden tests mirroring S0's plist tests; label↔unit mapping + orphan-sweep generalization | 1–2 | gates |
| L2 — portability sweep | `platform_profile` module + f-string literal migration (with/after Phase 6.2's `_bot_home` dedup, §6); `perms.py` with both backends + effective-perms checks (§4); sudoers writer parameterization (§5); keystore fail-fast + labeling (§7); ratchet lint for new `/Users/` literals | 2–3 | L1 |
| L3 — wizard + e2e | Wizard Linux path behind the gate (apt/NodeSource prereqs, service-account creation, `systemd` install flow); channel-matrix platform awareness (§8); one bot end-to-end on an Ubuntu VM; the CI job (§9); threat-model §1.1 SSH-operator variant + §2 note; operator-client docs (per-OS tunnel instructions) + Windows-browser UI pass (§1, §12 Q11) | 2–3 | L2 |

Each step is its own PR-sized session chain with the standard discipline
(`git log origin/main..` at start and before `gh pr create`; pyflakes baseline;
canary rules don't apply until a Linux pod exists to canary). The 8.3 exit
criterion is L3's: *a bot deploys, runs, and is administered on Linux in a
VM/CI — even if feature-gated*. Full parity (docs, soak, GA un-gating) is
explicitly beyond 8.3 and gets its own decision after the spike soaks.

---

## 12. Open questions for discussion

1. **Unit naming** (§3): keep `ai.evolve.*` reverse-DNS labels verbatim as
   systemd unit names (recommended — zero churn), or take the port as the
   one chance to rename to Linux-conventional `evolve-*` names everywhere?
2. **journald** (§3): file logs for v1 is the recommendation — but do we ever
   *want* the journald migration as a later both-platform refactor, or is
   file-logs-forever the actual position? (Affects whether log-reader code is
   worth abstracting now.)
3. **Distro contract** (§10): is the supported statement "Ubuntu 24.04 LTS"
   (recommended — one tested image) or the capability contract
   ("systemd ≥ 255 + POSIX ACLs"), which implicitly blesses Debian 12 and
   invites support requests we haven't tested for?
4. **Filesystem layout** (§6): `/var/lib/evolve` + `/var/lib/evolve/repo` —
   agree, or prefer the repo as a sibling (`/var/lib/evolve-repo`) /
   somewhere else entirely? (Only the defaults; both are config-resolved.)
5. **Phase 6 coupling** (§6): bundle the path sweep with Phase 6.2's
   `_bot_home`/util dedup in the same session chain (recommended — same lines
   touched once), or keep 8.3 and 6.2 strictly separate at the cost of
   re-touching ~16 sites?
6. **iMessage presentation** (§8): hide on Linux (recommended) vs
   show-disabled-with-explanation in the channel matrix?
7. **Pairing auth timing** (§1): the spike ships SSH-tunnel-only; should
   Phase 2.6 (auth default-on) be a prerequisite for *un-gating* Linux GA,
   even though it isn't one for the spike?
8. **Isolation ambition** (§2): is per-bot Linux users settled, or do you
   want a `ContainerIsolation` design sketch (not build) alongside L1 so the
   two can be compared concretely before the port hardens the user-based
   model into a second platform's worth of precedent?
9. **The `evo` account on Linux** (§2/§4): the evo-account-separation ACL
   shape translates cleanly per §4 — but should the Linux wizard provision
   `evo` from day one (recommended — no pre-separation legacy on a fresh
   platform), or mirror macOS's staged cutover for consistency?
10. **Session budget** (§11): 5–8 sessions is the research doc's estimate on
    top of 4.3 C's remaining 3–4. Is ~8–12 total sessions of runway for this
    acceptable now, or does it slot after the Phase 2 residuals / Phase 6
    items in priority order?
11. **Operator client bar** (§1): the Windows-browser verification pass — is
    it part of L3's exit criteria (recommended — it's cheap and it's what
    makes the "no Apple hardware required" claim honest), or a post-spike
    docs task? And does the channel/setup copy get per-client-OS tunnel
    instructions in L3 or at GA un-gating?
