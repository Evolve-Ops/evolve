# CLAUDE.md — Evolve Admin Dev Guidelines

> **Installing Evolve rather than developing it?** Everything below is for
> people (and agents) working on the Evolve *codebase* — its warnings about the
> deploy checkout, SSH conventions, and file-access patterns apply to a
> maintainer's setup, not to a machine you are setting up for the first time.
> For an install, ignore this file and start at
> [docs/help/installation.md](docs/help/installation.md); if you are an AI
> coding agent assisting an install, read
> [docs/help/install-with-an-agent.md](docs/help/install-with-an-agent.md).

## Runtime Context

The admin server (`ai.evolve.evolve.admin-ui` LaunchDaemon) runs as the **`evolve`** user,
not as the admin user (`pod-admin`). All file access and subprocess calls must be written
with this in mind.

---

## Where to do dev work — NOT in `/Users/Shared/evolve-repo`

Two checkouts exist on the deploy-host, and they have different roles:

- **`/Users/Shared/evolve-repo`** — the **deploy checkout**. The `ai.evolve.evolve.repo-puller`
  LaunchDaemon `git pull --ff-only`s this every 15 min. Every running daemon (admin-ui,
  heal, verify, the gateways via plugin code) loads from here. **Treat it as read-only.**
  Never run Claude Code, Cursor, or Claude Desktop with Cowork attached against this path —
  any file you create lands as untracked and the next pull will wedge on
  "untracked working tree files would be overwritten by merge". The puller now sweeps
  identical-to-origin files into a quarantine dir and retries (`_handle_untracked_conflict`
  in `repo_puller.py`), but that's recovery for accidents — don't rely on it.
  When `pod.release.mode = "canary"` (network.json; spec
  [internal/spec-state-store-and-deploy-resilience-2026-06-10.md](internal/spec-state-store-and-deploy-resilience-2026-06-10.md)),
  this checkout follows the **release pointer** (`{shared_dir}/release.json`, mirrored
  by the local `evolve-stable` tag), not origin tip: candidates from origin/main are
  gated in per-candidate worktrees under `/Users/Shared/evolve-staging/` (static
  checks + canary-bot soak) before the fleet moves. `sudo evolve-admin release
  status|rollback|pin|promote` is the operator surface; one-command undo is
  `sudo evolve-admin release rollback`. Out-of-band `git reset` on this checkout
  gets repaired back to the pointer — don't fight it; use `release pin`/`rollback`.

- **Your laptop's clone** (e.g. `~/GitHub/evolve`) — the **dev checkout**. Make all
  branches, commits, and PRs from here. Use `git worktree add` for parallel changes.

If you need to investigate something *on the deploy-host* (file state, daemon behaviour), prefer
read-only commands via SSH (see SSH conventions below). If you genuinely need an editable
working tree on the deploy-host, `git worktree add /Users/Shared/evolve-investigation main` —
worktrees share `.git` but have a separate working tree, so a pull on the deploy checkout
won't collide with your edits.

---

## Where a new doc goes — `docs/` is PUBLIC, `internal/` is not

The repo has two doc trees and the split is physical, not a matter of judgment
per file (PD-3c; `internal/spec-public-2026-06-18.md` §4.1):

| Tree | Holds | Ships to `evolve-ops/evolve`? |
|---|---|---|
| `docs/` | **public product docs only** — architecture, configuration, the `help/` corpus, `system/`, `schemas/`, `skills/`, `reference/`, `gitpages/`, `principle-*` | **yes** |
| `internal/` | **everything else** — specs, designs, decisions, roadmaps, audits, build briefs, work orders, strategy, incidents, forensics, the META apparatus, EDR | **no** — denylisted wholesale as `internal/**` |

**Writing a spec, design note, decision record, audit, build brief, work order,
roadmap, incident, or any dated internal document? It goes in `internal/`.** It
needs no manifest entry — `internal/**` already covers it. Put one in `docs/`
and CI fails: `tools/check-public-manifest` rejects any tracked `docs/` path
that is not in `docs/public-manifest.yaml`'s `public_docs:` allowlist.

Adding a genuinely new *product* doc under `docs/` means adding a `public_docs:`
glob — an explicit, reviewable act, because that glob publishes the file. When
unsure, `internal/`: private→public later costs nothing, the other direction is
the 2026-08-15 leak.

Nothing a running pod reads moved. `docs/system/**` (injected into bot session
prompts), `docs/help/**`, `docs/schemas/`, `docs/skills/`, `docs/reference/`,
`docs/policy/`, `docs/fixtures/`, `docs/atlas-app-manifests/` and
`docs/gitpages/` are load-bearing paths in deployed pods — never rename them.

## SSH to the deploy-host

The deploy-host's admin account is **`pod-admin-user`**, not `dev` (the laptop account). This matters
for every command Claude generates:

| Audience | Write |
|----------|-------|
| Commands Claude runs itself via Bash | `ssh deploy-host …` — the harness has an SSH `Host deploy-host` alias that resolves the username automatically |
| Copy-paste commands handed to **this maintainer** | `ssh deploy-host …` — their `~/.ssh/config` pins `User pod-admin-user` plus a `HostName` and `IdentityFile` under `Host deploy-host`, so the bare alias resolves. This is the form they asked for (2026-08-28); do not expand it to `user@host`. |
| Error messages, generated docs, or anything a **different** operator's shell will run | explicit `user@host` — never assume someone else's `~/.ssh/config` has a matching `Host` block. Derive the target rather than hardcoding (see below). |

> **Caveat on the `deploy-host` alias:** it is pinned to the host's `.local` (LAN)
> name on purpose, because the bare name resolves via Tailscale MagicDNS and
> `ssh deploy-host` dies with a silent timeout whenever the tailnet node key lapses —
> even though sshd is fine (2026-08-28 incident). The trade-off is that the
> alias is LAN-only; off-network use needs the tailnet FQDN **and** a live
> node key.

The same distinction holds for any code that emits operator-facing strings: derive the SSH
target from `network.json::pod.ssh_target` (operator override) → `admin_user@<host from
adminBaseUrl>` (auto-derived) → empty string (operator is at the deploy box). The
`evolve_admin.config.resolve_pod_context()` helper does this; use it instead of hardcoding.

**`sudo -H -u <bot> openclaw …` over SSH — `cd /Users/<bot>` (or `/tmp`) first.** SSH
lands you in `/Users/pod-admin-user/`, which the bot user can't traverse. `openclaw` is a Node
binary, and Node calls `uv_cwd()` during startup — that hits EACCES on pod-admin-user's home
and the CLI dies before producing any output (or worse, with cryptic libuv noise). Same
shape as the `sudo -u evolve python3` gotcha already documented in memory (Python's
`sys.path[0]` instead of Node's `uv_cwd`). Always change to a directory the bot can read
before invoking. Code inside `deploy.py` already gets this right by passing `cwd=bot_home`
to `subprocess.run`; the rule is for ad-hoc operator commands over SSH.

---

## File Access Pattern

### Reads — use direct reads, not `sudo -u <bot>`

The `evolve` user gets macOS ACL read access to every bot's `.openclaw/` directory
via `set_evolve_read_acl(bot_id)` in `deploy.py`. This is called during every bot
deploy and fresh setup.

**Do this:**
```python
try:
    text = Path(f"/Users/{bot_id}/.openclaw/openclaw.json").read_text()
except PermissionError:
    # Fallback: sudo /bin/cat as root (sudoers grant)
    r = subprocess.run(["sudo", "/bin/cat", path], capture_output=True, text=True)
    text = r.stdout if r.returncode == 0 else None
```

**Never do this** — `evolve` cannot `sudo -u <bot_id>`:
```python
# BROKEN: evolve user has no 'sudo -u <bot_id>' grant
subprocess.run(["sudo", "-u", "<bot_id>", "cat", path], ...)
```

### Writes — /tmp staging + sudo /bin/cp

Bot config files (`openclaw.json`, `auth-profiles.json`) are owned by the bot user.
Write them via `/tmp` staging to avoid permission issues.

**Do this:**
```python
import tempfile, os
fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".json")
with os.fdopen(fd, "w") as f:
    f.write(content)
subprocess.run(["sudo", "/bin/cp", tmp, dest_path], check=True, capture_output=True)
subprocess.run(["sudo", "/usr/sbin/chown", f"{bot_user}:staff", dest_path], check=True, capture_output=True)
# Token-bearing files (openclaw.json, auth-profiles.json) MUST be 0600.
# `cp` (no -p) takes the dest mode from the staged tmp's mode or, when the
# dest already exists, PRESERVES the dest's current mode — so a file that
# ever became 0644 stays 0644. Enforce 0600 explicitly:
from evolve_admin.secret_config_perms import chmod_secret_config
chmod_secret_config(dest_path)   # sudo /bin/chmod 600 — preserves the evolve read ACL
os.unlink(tmp)
```

**Never do this:**
```python
# BROKEN: evolve user cannot sudo as bot users
subprocess.run(["sudo", "-u", "<bot_id>", "tee", path], input=content, ...)
# BROKEN: chmod 644 on a token-bearing file is world-readable on a multi-user box
subprocess.run(["sudo", "/bin/chmod", "644", oc_json], ...)
```

`secret_config_perms` is the single home for the 0600 contract: the write
helper above (`chmod_secret_config`) and the deploy-time self-heal
(`check_bot_secret_modes`, wired into `ensure_pod_perms`) both live there.
A new token-bearing file type needs a `chmod 600` sudoers grant in
`_render_evolve_sudoers` and an entry in `BOT_SECRET_CONFIG_RELPATHS`.

The bot's `workspace/evolve/` directory has write ACL for `evolve`, so manifests
and scan-status files there can be written directly without sudo.

---

## macOS Paths

| Command | Correct path | Wrong path |
|---------|-------------|------------|
| `cat`   | `/bin/cat`  | `/usr/bin/cat` (doesn't exist) |
| `launchctl` | `/bin/launchctl` | `launchctl` (no full path) |
| `chown` | `/usr/sbin/chown` | `/bin/chown` |
| `chmod` | `/bin/chmod` | `/usr/bin/chmod` |
| `mkdir` | `/bin/mkdir` | `/usr/bin/mkdir` |

Always use full paths in sudoers and subprocess calls.

---

## sudoers Rules

- **`/etc/sudoers.d/evolve`** — grants for the `evolve` service user (root-level commands)
- **`/etc/sudoers.d/evolve-admin`** — grants for `pod-admin` to sudo as bot users (CLI use only)
- Written by `_write_evolve_sudoers()` in `setup_wizard.py`; the content comes from `_render_evolve_sudoers()` there (single source of truth — `cli.py` imports the renderer, it does not keep a copy)
- Always validate with `visudo -c -f <tmpfile>` before installing
- No backslashes before `.` in paths (e.g., `.openclaw`, not `\.openclaw`)
- No trailing `/*` wildcard — macOS visudo rejects it

---

## ACL Setup

`set_evolve_read_acl(bot_id)` in `deploy.py`:
- Sets macOS ACL inheritance on `/Users/{bot}/.openclaw/` (read for all files)
- Sets read+write ACL on `/Users/{bot}/.openclaw/workspace/evolve/` (manifests, scan status)
- Called during `deploy_bot()` and `_setup_oc_for_bot()` — safe to call repeatedly

After calling this, all direct `Path.read_text()` calls on `.openclaw/` files work
without sudo. The `sudo /bin/cat` fallback handles bots not yet deployed through
the new path.

**Linux group/other contract (two pieces; macOS no-ops both — its mode bits
aren't entangled with a default-ACL mask):**
- **Bot-private clamp** — the `.openclaw` read grant passes
  `restrict_group_other=True` (`LinuxPerms.grant_read_recursive`), clamping the
  real `group::`/`other::` to `---` (access + default ACL) and pinning the dir
  mask at `r-x`. Without it, `setfacl -d` copies the dir's permissive base
  entries into the default, so every file the OC gateway mints is born genuinely
  `group::r-x`/`other::r-x` — a cross-bot read leak that #3190 deliberately does
  NOT suppress. After the clamp, `st_mode`'s group triad is purely the mask, the
  real `group::`/`other::` are `---`, and #3190's `acl_masked_owner_only` proves
  the OC "group/world-readable" finding a mask artifact. **The clamp is the
  keystone that makes the merged #3190 suppression effective on real files.**
- **Workspace shared-channel exception** — `workspace/evolve`,
  `workspace/manifests`, `evolve-backup` get `share_group_other_read=True`
  (`grant_write_recursive`), re-widening `group::r-x`/`other::r-x` past the clamp
  because the BOT reads evolve-written files there it does not own and has no
  named ACE on (manifests, the defer/audit queues, `rec-hints.json`). This MUST
  run after the bot-private clamp. The narrow drift-apply
  (`secret_config_perms._reassert_evolve_read_acl`) deliberately does NOT clamp —
  it would starve those reads until the next full deploy.

---

## Signal store (alerts/observation layer)

Spec: [internal/spec-alerts-signal-store-2026-05-07.md](internal/spec-alerts-signal-store-2026-05-07.md).

The unified observation store. Every monitor (pod_report, audit, watchdog,
host_health, error_reporter, integration_probe, pod_health, security_warden,
test_runner) writes Signals here; the admin UI's Alerts
page reads them. Distinct from the Proposal store — generators write
Proposals, monitors write Signals, and `Proposal.motivating_signals[]`
links one to the other.

```
{shared_dir}/signals/
├── firing/<id>.json               # state = firing
├── snoozed/<id>.json              # state = snoozed
├── archived/<id>.json             # state ∈ resolved | dismissed (90-day retention)
├── feedback.jsonl                 # rejected-proposal feedback (signal-tuning input)
└── log/<YYYY-MM-DD>.jsonl         # append-only state-change log (1-year retention)
```

The state field on each Signal JSON is authoritative; the subdir is the
physical index. State transitions go through
`signals.state_machine.transition` (valid edges: firing → snoozed |
resolved | dismissed; snoozed → firing | resolved | dismissed; resolved
→ firing only via observe() re-open within window).

Producer entrypoint is `signals.store.observe()` (find-or-create with
signature dedup + reopen window). Signature lookup is a directory scan
inside `signals.store` (`find_active_by_signature`) — the spec's
`signature_index.json` was never implemented; never read a
`signals/signature_index.json` file (that dead read is how the
sudoers-drift auto-resolve gate silently no-opped until 2026-07-29). Comprehensive sweep-style monitors
(pod_report, audit, host_health, integration_probe, pod_health) call
`signals.store.sweep_resolve(producer=..., kept_signatures=...)` at the
end of a run so cleared conditions auto-archive.

Atomic writes via temp-file + rename, owned by the `evolve` user — no
sudo or `/tmp` staging needed (the dir is under `{shared_dir}` which has
the right ACL).

Retention is enforced by `python3 -m signals.retention --shared-dir
{shared_dir}` (90-day archive prune + 1-year log roll). Idempotent;
safe to schedule on a daily cron.

Backfill of historical watchdog JSONL:
`python3 -m signals.backfill --shared-dir {shared_dir}` mirrors past
WatchdogEvents into the Signal store as resolved/historical for the
History tab.

### Event-driven generator dispatch (signal-subscriber)

Spec: [internal/spec-signal-subscriber-2026-05-31.md](internal/spec-signal-subscriber-2026-05-31.md).

The `ai.evolve.evolve.signal-subscriber` LaunchDaemon (long-running
KeepAlive, runs as `evolve`, installed by `sudo evolve-admin
install-infra-jobs`) watches `{shared_dir}/signals/firing/` at 1 Hz.
When a Signal lands whose `type` matches any active generator's
charter `subscribes_to: [<type>, ...]` field, the daemon invokes
that generator's observe() path via `generator_runner.run_one_generator`
within seconds (5s upper bound).

To subscribe a generator: add `subscribes_to:` to the charter.yaml,
bump the fingerprint via `tools/bump_charter_fingerprints.py`. No
code change in the generator itself.

The daily generator_runner sweep stays — it is the safety net for
daemon downtime and unsubscribed generators. Arbiter dedup ensures
a duplicate Proposal from both paths merges into one.

Per-(generator, signal_id) ledger at
`{shared_dir}/signal_subscribers/ledger.jsonl` prevents re-dispatch
across daemon restarts; one-week retention, pruned hourly.

Disable with `sudo /bin/launchctl bootout system/ai.evolve.evolve.signal-subscriber`
— the daily sweep continues to handle subscribed generators as the
backstop.

---

## Arbiter (L1-L6) on-disk layout

The RSI arbiter lives entirely under `{shared_dir}` (typically `/Users/Shared/evolve`).
Nothing is kept per-bot; everything is pod-wide state.

```
{shared_dir}/
├── proposals/
│   ├── pending/<id>.json      ← draft / pending / approved_* / failed_flagged
│   ├── snoozed/<id>.json      ← deferred by user or snooze-wake daemon
│   ├── applied/<id>.json      ← applier ran; verify daemon owns these next
│   └── archived/<id>.json     ← terminal (succeeded / failed_* / rejected / dismissed)
├── generators/<generator_id>.json   ← per-generator GeneratorRecord (track record, status, config)
├── profiles/<bot_id>.md              ← YAML frontmatter (weights) + Markdown body
├── observations/<bot_id>/<YYYY-MM-DD>.jsonl   ← (noun × verb × mood × engagement) tuples
├── watchdog/<YYYY-MM-DD>.jsonl                ← WatchdogEvent records (pod-wide)
└── calibration/snapshots/<id>.json            ← signal / generator / user snapshots
```

The **status field on the proposal JSON is authoritative**; the subdir is a physical
index for efficient iteration. Use `arbiter.store` helpers (`iter_proposals`,
`find_proposal`, `move_proposal`, `write_proposal`) rather than reading the JSON
directly — they handle subdir routing by status and atomic rewrites.

Charters ship in code at `packages/analyzer/generators/<id>/charter.yaml`. They
are immutable at runtime; the registry enforces this via fingerprint check
against the stored `GeneratorRecord.charter_fingerprint`.

The admin server (evolve user) has ACL read on `{shared_dir}/` already, so reads
are plain `Path.read_text()`. Writes go through `arbiter.store.write_proposal` /
`registry.update_*` / `profile.storage.save_profile` — each uses temp-file + rename
for atomicity. No /tmp staging + sudo needed; most of `{shared_dir}` is owned by
the `evolve` user.

**Post-evo-account-separation exception (spec at [internal/spec-evo-account-separation-2026-05-25.md](internal/spec-evo-account-separation-2026-05-25.md)):**
`{shared_dir}/proposals/` and `{shared_dir}/signals/` stay owned by `evolve:wheel`
but also carry an inherited ACL granting the `evo` macOS user
`read,write,delete,append` (the same shape as the evolve-side workspace ACL,
inverted in user direction). The invariant is enforced by
`ensure_pod_perms()` on every deploy (see `_check_evo_write_acl` in `deploy.py`)
and re-asserted by `_evo_cutover_apply_acl` during Phase E.2.b. The fix is a
no-op until the `evo` macOS user exists, so pre-separation pods are unaffected.

**Since 7.1 C2 (2026-08-25) the ACL no longer carries any tool WRITE:** evo's
proposal/signal MCP tools (`action.proposal.*`, `action.signal.*`,
`action.dispatch.acknowledge`) route every store write through the admin
daemon's `/api/arbiter/proposals/<id>/*` and `/api/signals/<id>/*` endpoints
over the unix socket, **fail closed** — daemon unreachable ⇒ the tool refuses
with an operator-legible error and writes nothing (`require_daemon_call` /
`DAEMON_REQUIRED_REFUSAL` in `evo/admin_client.py`; no direct-write fallback
exists, since a fallback would stay inducible by killing the socket). The
arbiter/signal stores have ONE writer: the admin daemon. Gateway-side READS
(pod_state.*, tool validates) still use the store APIs directly and still
need the ACL; retiring the ACL itself is the 7.1 C follow-up.

---

## Admin UI style — **read [docs/style-guide.md](docs/style-guide.md) before touching `packages/admin/evolve_admin/web/**`**

Every visual change in the admin SPA must comply with `docs/style-guide.md`. The
guide is authoritative — it covers theme parity (dark + light), the type/spacing/
radius scales, component conventions (buttons, forms, modals, drawers, badges,
expand/collapse), and the UI principles (data-shape input widths, one primary
per surface, semantic vs decorative color).

**The five highest-violation rules — keep these in working memory:**

1. **Form input widths follow DATA SHAPE, not column width** (§9.2). Add
   `class="input-w-sm"` (80px) / `-md` (160px) / `-lg` (320px) / `-xl` (480px) /
   `-text` (600px, textarea) to every new `<input>` and `<select>`. The global
   `input { width: 100% }` rule is a safety net, not the design — every new
   input/select gets an explicit width class.
2. **No new hex colors in component CSS** (§2 / §3). Reference token vars
   (`var(--bg2)`, `var(--text)`, `var(--accent)`, etc.). If you need a new shade,
   add it as a dark/light pair to the `:root` + `[data-theme="light"]` blocks
   in `base.css` first.
3. **No new font sizes outside the scale** (§4). Don't introduce 0.83, 0.84,
   0.86, 0.88 — they're off-scale; round to the canonical 0.85.
4. **Shadows go through `var(--shadow-*)` tokens** (§7), not inline
   `rgba(0,0,0,…)` — tokens carry both-theme parity for free.
5. **Expand/collapse triangles use `.expand-icon`** (§9.13), not Unicode glyphs
   (▸ ▾ ▼ ▶ ⟩). 14×14 SVG chevron, rotates 90° on `.is-open`.

**Before opening a PR:** toggle the theme button in the sidebar footer and
verify both modes render correctly. There is no CI gate for theme parity — if
you break light theme silently, no one will notice until an operator toggles.

**Lint check:** `tools/ui-style-lint <changed-files>` runs the same checks the
pre-commit hook does. The lint is **hybrid-severity**:
- **Block** (causes non-zero exit, fails commit): off-scale fonts, inline
  `rgba(0,0,0,…)` shadows, Unicode expand-triangle characters in expand
  contexts. These are high-confidence rules where false positives are rare.
- **Warn** (prints but doesn't fail): `<input>` / `<select>` without an
  explicit width-utility class or width style. The heuristic can miss
  legitimate-but-unusual patterns, so warning-only avoids friction for
  edge cases.
- `--strict` flips warn rules to block (used in CI). `--full` lints whole
  files even in `--staged` mode (used for "how much legacy debt is left?"
  audits).

---

## Preflight — run `tools/preflight` before EVERY push

CI (`.github/workflows/ci.yml` + `browser-smoke.yml`) is ~18 jobs. Many are
deterministic lints/ratchets that already run fine on macOS, but pushing blind
means the red only surfaces async minutes later. **`tools/preflight` is the
single "run what CI runs" command — run it before every push.** It mirrors
ci.yml's `changes` job path-gating (python / any_python / web / plugin /
linux_e2e / edr — the SAME grep predicates, kept byte-for-byte in sync) and
invokes each reproducible gate exactly as its CI job does, so a local PASS means
the matching CI job will pass.

```
tools/preflight            # changed-only (default): the cheap env-free lints/ratchets
                           #   (except-pass, ruff/pyright-baseline, sudo-grant, store-access,
                           #   platform-path, openclaw-eacces, scheduler-factory,
                           #   signal-protection, file-size/context-budget/quarantine ratchets, public-manifest,
                           #   help-coverage, scrub-guard, oc-channel, ui-style-lint) + targeted tests
                           # NOTE: the publish-scrub-suites gate is path-gated to the
                           #   publish surface and takes ~4m when it fires (real git
                           #   force-push round-trips) — that is expected, not a hang.
tools/preflight --full     # also run the heavy admin/analyzer/edr suites; provision uv/npm env as needed
tools/preflight --all-paths  # ignore the diff; run every gate (fail-safe sweep)
tools/preflight --no-setup   # never provision (uv sync / npm ci); env-gated gates SKIP if env absent
tools/preflight -v           # stream each gate's output live
```

Output is a compact `gate → CI job → PASS/FAIL/SKIP` table; exit is non-zero if
any gate failed (the failing gate's output is dumped so you can fix immediately).
The env-free lints/ratchets always run. The uv-based gates (ruff, pyright,
python tests) and node-based gates (eslint, plugin tsc/test) run when their
toolchain env is already present, else SKIP with a note (`--full` provisions
them). **What it can't reproduce:** `linux-e2e` (Linux-only) and the full ~12k
quarantined suites by default — when the diff would trigger linux-e2e, preflight
prints a note to push and watch the check.

**Belt-and-suspenders hook (optional, per-clone).** `tools/hooks/pre-push`
runs `tools/preflight` and blocks the push on a red gate. The stock
`.git/hooks/` is not version-controlled, so wire it up once per clone by
copying it into the active hooks dir — the primary contract is still that chips
invoke `tools/preflight` explicitly:

```
cp tools/hooks/pre-push "$(git rev-parse --git-path hooks)/pre-push" && chmod +x "$(git rev-parse --git-path hooks)/pre-push"
```

(`git rev-parse --git-path hooks` resolves to whatever your active hooks dir is,
honoring any `core.hooksPath`. Don't point `core.hooksPath` at `tools/hooks` —
that dir holds the META session-hook helpers, not a `pre-commit`, so it would
silence the `.githooks/pre-commit` lints.)

Bypass the hook for a genuine emergency with `git push --no-verify` (or
`PREFLIGHT_SKIP=1` when preflight already ran upstream in the same job).
