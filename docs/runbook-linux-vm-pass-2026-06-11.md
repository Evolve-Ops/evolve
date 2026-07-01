# Runbook — real-Ubuntu-VM manual pass (Linux-port GA residual), 2026-06-11

**Who runs this:** the pod operator, on an Apple-Silicon Mac, using a
disposable [Multipass](https://canonical.com/multipass) Ubuntu 24.04 VM.

**What this pass is — and is not.** Roadmap 8.3
([roadmap-80-to-100-2026-06-09.md](roadmap-80-to-100-2026-06-09.md)) tracks a
GA residual called "real-Ubuntu-VM manual pass (incl. masked-ACE kernel
question + full wizard)". The *full-wizard* half waits on the wizard
remainder being ported; **this runbook covers the early half only**, three
things:

1. **(a) Run the existing `linux-e2e` harness on a real stock Ubuntu VM** —
   the same seven-step harness that is green on GitHub's `ubuntu-24.04`
   runners ([design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md)
   §9), but on an unmodified Ubuntu image instead of GitHub's customized
   runner image.
2. **(b) Human-eyeball the rendered sudoers under stock `visudo`** — the
   escaped-colon mask-repair grants (`m\:\:rwX`) were W5A's open syntax
   risk; a human reads the lines, not just the parser.
3. **(c) Settle the masked-ACE kernel-enforcement question** — on the GH
   runner, a named-user ACE whose mask was zeroed still permitted traverse,
   contrary to POSIX.1e; the harness currently *observes* rather than
   *asserts* kernel denial (`test_ubuntu_e2e.py` step 4). A stock-kernel
   data point decides whether the harness can tighten.

Ground truth for everything below:
[design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) §9
("Running it locally" + guard semantics) and the harness itself,
`packages/admin/tests/e2e_linux/test_ubuntu_e2e.py`.

---

## 1. VM setup

> **DISPOSABLE VM ONLY.** The harness mutates the host: it creates user
> accounts (`evolve`, `e2ebot`, group `evolve-bots`), installs
> `/etc/sudoers.d/evolve`, and writes real units under
> `/etc/systemd/system` (plus `/opt/evolve-e2e` and `/var/lib/evolve`).
> Cleanup runs even on failure (module-scoped finalizer in the harness),
> but never point this at a machine you care about.

On the Mac:

```bash
brew install multipass        # skip if `multipass version` already works
multipass launch 24.04 --name evolve-e2e --cpus 4 --memory 8G --disk 20G
multipass shell evolve-e2e
```

The default `ubuntu` account inside the VM has passwordless sudo, which the
harness requires (it drives everything through `sudo -n`).

**Teardown when the whole pass is done** (after §6's findings are recorded
and any artifacts copied off the VM):

```bash
multipass delete --purge evolve-e2e
```

---

## 2. Getting the repo into the VM

> **Durable pod vs. this disposable VM — pick the right path.** This runbook
> sets up a *throwaway* VM for the harness pass, so the tarball below is the
> right call **here**. But the same tarball on a **durable pod** (a VPS, a
> production box — anything non-ephemeral) is a trap: a tarball has no `.git`,
> no remote, and no credential, so the `ai.evolve.evolve.repo-puller` daemon
> can never `git pull`. The box freezes on install-day code and the only
> signal is a quiet "repo-puller stale" hint. (This is the `evolve-vsp-pod`
> freeze, 2026-06.) **For a durable pod, use §2a (clone-with-credentials);
> for this disposable harness VM, use §2b (tarball).**

### 2a. Durable pod (VPS / production) — clone-with-credentials (recommended for non-ephemeral hosts)

Make the deploy checkout a **real clone owned by the `evolve` user**,
authenticated by a **read-only deploy key**. The invariant that makes
auto-update "just work" afterward: **ONE credential, owned by `evolve`** — so
the bootstrap key IS the puller key. Cloning as root or via `gh` puts the
credential in the wrong home and the puller can't reuse it.

The private-repo chicken-and-egg (you can't clone without the key, and the key
lives on the box you're setting up) resolves by doing the **key BEFORE the
clone**:

1. **Create the `evolve` service user** (the setup wizard also does this; a
   from-scratch clone needs it first):
   ```bash
   sudo useradd -m -r evolve        # Linux; -r = system account
   ```
2. **Generate the deploy key AS `evolve`** (exactly what
   `repo_puller.ensure_deploy_key()` would generate — doing it by hand here
   just lets you register the key before the first clone):
   ```bash
   sudo -u evolve ssh-keygen -t ed25519 -N '' \
     -f /home/evolve/.ssh/evolve-repo -C "evolve-repo deploy key ($(hostname))"
   sudo -u evolve cat /home/evolve/.ssh/evolve-repo.pub
   ```
3. **Add the pubkey to GitHub as a READ-ONLY deploy key:** repo → Settings →
   Deploy keys → Add deploy key. Paste the `.pub` line, title it for the pod
   (e.g. `evolve repo-puller (<hostname>)`). **Leave "Allow write access"
   UNCHECKED** — the puller only ever pulls.
4. **Point `evolve`'s SSH at the key** so the clone uses it:
   ```bash
   sudo -u evolve tee -a /home/evolve/.ssh/config >/dev/null <<'EOF'
   Host github.com
       HostName github.com
       User git
       IdentityFile ~/.ssh/evolve-repo
       IdentitiesOnly yes
   EOF
   sudo -u evolve chmod 600 /home/evolve/.ssh/config
   ```
5. **Clone AS `evolve`** into the platform deploy-checkout path — `/var/lib/evolve/repo`
   on Linux (the LINUX profile's `deploy_checkout_default`; macOS uses
   `/Users/Shared/evolve-repo`). Derive it from `repo_puller.DEFAULT_REPO` if
   in doubt; don't hardcode it elsewhere. **Clone as `evolve`, NOT as root and
   NOT via `gh`:**
   ```bash
   sudo install -d -o evolve -g evolve /var/lib/evolve
   sudo -u evolve git clone git@github.com:<org>/evolve.git /var/lib/evolve/repo
   #                                     ^^^^^ whatever fork this pod tracks (e.g. evolve-ops/evolve)
   ```
6. **Verify before running setup** (this is the §`pre-install-checklist.md`
   GitHub-access gate):
   ```bash
   sudo -u evolve ssh -T git@github.com   # "Hi <org>/evolve! You've successfully authenticated…"
   sudo evolve-admin repo-pull            # first pull fast-forwards (or "up to date")
   ```

The setup wizard's **Repo access** step (Step 14) re-verifies all of this and
records `pod.repo_url` into `network.json`. If you bootstrapped as `evolve`
above, that step is a **silent no-op** ("just works"). If you skipped it, the
wizard prints the same read-only-deploy-key walkthrough and warns — *loudly* —
that the pod will not stay current until you finish it.

### 2b. Ephemeral local VM only (this harness pass) — tarball over `multipass transfer`

**For the disposable VM in this runbook**, the tarball is the right call — no
GitHub credentials ever enter a VM you're about to run permission experiments
on, and the harness doesn't need `.git` (the uv workspace has no VCS-based
versioning; `uv.lock` is committed). **Do not use this on a durable pod** — it
produces a non-updating box (see the callout above and §2a).

On the Mac, from the dev checkout:

```bash
cd ~/GitHub/evolve
git fetch origin
git archive --format=tar.gz -o /tmp/evolve-src.tar.gz origin/main
multipass transfer /tmp/evolve-src.tar.gz evolve-e2e:/home/ubuntu/evolve-src.tar.gz
```

Inside the VM:

```bash
mkdir -p ~/evolve && tar -xzf ~/evolve-src.tar.gz -C ~/evolve
```

*(Alternative for the disposable VM: install `gh`, `gh auth login`, `gh repo
clone` — fine too, but it parks a GitHub token inside a VM you're about to do
permission experiments on; prefer the tarball. For a durable pod use §2a's
read-only deploy key, never a personal `gh` token.)*

Then the dependency install, per design-doc §9:

```bash
sudo apt-get update
sudo apt-get install -y python3-pip acl
pip install uv
export PATH="$HOME/.local/bin:$PATH"
cd ~/evolve && uv sync --locked
```

**Known wrinkle vs the §9 block:** stock Ubuntu 24.04 enforces PEP 668, so
a bare `pip install uv` may refuse with `error: externally-managed-
environment` (the GH runner image is more permissive). If it does, use
either:

```bash
pip install --user --break-system-packages uv
# or the standalone installer:
curl -LsSf https://astral.sh/uv/install.sh | sh
```

`uv sync --locked` resolves the committed `uv.lock` for both workspace
packages (`packages/admin`, `packages/analyzer`) plus the dev group
(pytest); it will fetch a managed Python if needed (packages require
≥3.10; noble ships 3.12 — either is fine).

---

## 3. Run the harness

The harness guard (`E2E_ENABLED` at the top of
`packages/admin/tests/e2e_linux/test_ubuntu_e2e.py`) requires ALL of:

- a **Linux** host (`sys.platform`),
- **`CI`** set in the environment,
- **`EVOLVE_PLATFORM=linux`** (the 8.3 experimental opt-in),
- **`EVOLVE_NUM_SHARDS` unset** (never inside a sharded suite run).

Anything else and every test skips. So, inside the VM:

```bash
cd ~/evolve/packages/admin
CI=1 EVOLVE_PLATFORM=linux \
  uv run --no-sync python -m pytest tests/e2e_linux/ -v -s
```

(`-s` streams the harness's subprocess journal — every argv + rc + output —
so a failure is debuggable from the terminal scrollback alone. CI adds
`-rA --tb=long -p no:cacheprovider`; add them if you want byte-comparable
output.)

**What green looks like:** seven ordered tests pass, in order —

1. `test_step1_platform_gate_optin_resolves_linux` — gate + LINUX profile +
   real adapters active
2. `test_step2_sudoers_render_validate_install` — render, **real `visudo
   -c -f`**, install to `/etc/sudoers.d/evolve`
3. `test_step3_accounts_useradd_and_getent` — real `useradd` for `evolve` +
   `e2ebot`; grants visible via `sudo -n -l`
4. `test_step4_perms_acls_carveout_and_mask` — ACL read contract,
   inheritance, credentials carve-out denial, the POSIX-mask sharp edge
   (detect → repair)
5. `test_step5_scheduler_systemd_stub_agent` — real systemd units, restart
   guarantee (fresh MainPID), timer fire, clean remove
6. `test_step6_admin_server_runs_and_answers_as_evolve` — real Flask admin
   server as a `User=evolve` systemd unit answering `/`, `/api/health`,
   `/api/status`
7. `test_step7_write_completion_sentinel` — writes `COMPLETED` into the
   diag dir

followed by the teardown journal (`── e2e teardown … ──`) removing users,
units, and `/etc/sudoers.d/evolve`.

**Diagnostics** land in `EVOLVE_E2E_DIAG_DIR`, default
**`/tmp/evolve-linux-e2e-diag`** (the harness default; CI overrides it to
`$RUNNER_TEMP/linux-e2e-diag`). Expect at least: `sudoers-evolve.rendered`,
`sudoers-evolve.installed` (teardown copy of the live file),
`sudo-l-evolve.txt`, `getfacl-*` dumps, `teardown-getfacl-*.txt`, admin-ui
logs, and `COMPLETED`. The diag dir survives the harness teardown — §4
reads from it.

**On failure, capture (mirrors CI's failure-diagnostics step):**

- the full pytest output (the `-s` journal — first failing subprocess is
  usually the answer),
- the entire `/tmp/evolve-linux-e2e-diag/` directory,
- `sudo journalctl --no-pager -n 400 -u 'ai.openclaw.*' -u 'ai.evolve.*'`,
- `sudo ls -la /etc/systemd/system/`, `uname -a`.

Copy out with `multipass transfer -r evolve-e2e:/tmp/evolve-linux-e2e-diag /tmp/`
(run on the Mac) before any teardown.

---

## 4. Visudo / mask-grant eyeball

Step 2 already ran the parser; this section is the *human* read of the one
construct a parser pass doesn't fully vouch for: the escaped-colon
mask-repair grants (W5A's open syntax risk).

```bash
DIAG=/tmp/evolve-linux-e2e-diag
sudo visudo -c -f "$DIAG/sudoers-evolve.rendered"     # expect: "… parsed OK"
less "$DIAG/sudoers-evolve.rendered"
```

Find the block headed **`9b2. ACL-mask repair (Linux only — macOS has no
POSIX mask)`**. The four lines to read (rendered by
`_render_evolve_sudoers()` in
`packages/admin/evolve_admin/setup_wizard.py`):

```
evolve ALL=(root) NOPASSWD: /usr/bin/setfacl -m m\:\:rwX /var/lib/evolve
evolve ALL=(root) NOPASSWD: /usr/bin/setfacl -m m\:\:rwX /var/lib/evolve/*
evolve ALL=(root) NOPASSWD: /usr/bin/setfacl -m m\:\:rwX /var/lib/evolve-plugin
evolve ALL=(root) NOPASSWD: /usr/bin/setfacl -m m\:\:rwX /var/lib/evolve-plugin/*
```

**What "correct" means** — the grant must match what the runtime actually
invokes. The runtime side is `LinuxPerms.reassert_mask` in
`packages/analyzer/runtime/perms.py`: it calls
`self._setfacl(["-m", "m::rwX", str(path)])` (and, in the recursive form,
the same shape once per masked path — it **never** runs `setfacl -R`), and
`_setfacl` prepends `sudo` + `/usr/bin/setfacl` (the `SETFACL` constant,
from `platform_profile`). So the exec-time argv is exactly:

```
sudo /usr/bin/setfacl -m m::rwX <path>
```

Check, line by line:

1. **visudo verdict** — `visudo -c -f` exits 0 on both the `.rendered` and
   `.installed` copies.
2. **The escape is exactly `m\:\:rwX`** — one backslash before *each*
   colon, nothing else escaped in the token. After sudoers unescaping that
   is the literal `m::rwX` the runtime passes. (Colons are escaped because
   visudo rejects bare `:` in argument patterns.)
3. **Command path is `/usr/bin/setfacl`** — identical to the runtime
   constant; a `/bin/setfacl` vs `/usr/bin/setfacl` mismatch would make
   sudo demand a password silently.
4. **Flags are `-m m\:\:rwX` with no `-R`** — matching `reassert_mask`'s
   contract (per-path repair only; an `-R` here would be a wider grant than
   the code ever uses).
5. **Path patterns cover the call sites** — `reassert_mask` runs under the
   `evolve` principal against shared-store subdirs after mode normalization
   and per-masked-path under the plugin install dir (the 9b2 comment block
   says exactly this). `{shared}` + `{shared}/*` and `{plugin_dir}` +
   `{plugin_dir}/*` are one-level globs; flag in §6 if you spot any
   repair site deeper than one level below those roots.

**Optional but recommended — live exec-time match probe.** The harness
validates the grant *syntactically* (visudo) and lists it (`sudo -n -l` as
`evolve`), but `reassert_mask` itself runs in step 4 with the harness
user's unrestricted sudo against paths under `/home/e2ebot` — so the
escaped pattern is never *matched at exec time* under the `evolve`
principal by the harness. Two commands close that (run after the harness;
its teardown removed the sudoers file and users):

```bash
DIAG=/tmp/evolve-linux-e2e-diag
sudo install -m 440 -o root -g root "$DIAG/sudoers-evolve.rendered" /etc/sudoers.d/evolve
sudo useradd -m evolve 2>/dev/null || true
sudo mkdir -p /var/lib/evolve
sudo setfacl -m u:evolve:rX /var/lib/evolve   # give the dir a real ACL mask

# positive: escaped pattern must match the literal runtime argv
sudo -u evolve -H bash -c 'cd /tmp && sudo -n /usr/bin/setfacl -m m::rwX /var/lib/evolve && echo GRANT-MATCH-OK'

# negative: same verb outside the path patterns must be refused
sudo -u evolve -H bash -c 'cd /tmp && sudo -n /usr/bin/setfacl -m m::rwX /etc; echo rc=$?'
```

Expect `GRANT-MATCH-OK` from the first and a refusal (nonzero rc,
"a password is required" / "not allowed") from the second. The VM is
disposable, so no cleanup needed beyond §1's teardown.

---

## 5. Masked-ACE kernel test

**Background.** POSIX.1e ACL evaluation caps every named-user ACE by the
ACL *mask*; `chmod` on group bits silently rewrites that mask. So after
`chmod g-rwx` on a directory carrying a `user:<x>:rX` ACE, the stored ACE
survives but its effective perms collapse to `---` — and the kernel is
supposed to **deny** that user traverse/read. On the GitHub `ubuntu-24.04`
runner VM it did not: with a textbook clobbered state (`mask::---`, ACE
`#effective:---`, uid verified) the masked-ACE traverse was still
**allowed** (harness runs 1+4, 2026-06-11). The credentials carve-out
denial (no-ACE + 0700) *was* correctly enforced on the same VM — the
security boundary Evolve relies on held; the anomaly is specifically the
mask. The harness therefore asserts detect (`acl_user_effective` sees the
drift) and repair (`reassert_mask` restores access), but only *observes*
the kernel-denial step (see the `OBSERVED, NOT ASSERTED` comment in
`test_step4_perms_acls_carveout_and_mask`). This section gets the stock-
kernel data point.

> **W7c update (2026-06-17) — observe-only is permanent; do NOT tighten.**
> The stock-Ubuntu data point came back **inconsistent**, not a clean DENY:
> the *manual* probe below (masking the *file's own* read ACE) **DENIED**,
> but the *harness's* own clobber case (`test_step4`, masking a *directory's*
> traverse ACE then reading a file beneath it) **ALLOWED** (`rc=0`) on the
> **same box** (ext4, kernel 6.8.0-71). W6 briefly tightened the harness to
> assert the denial off-CI from the manual DENY; **W7c reverted it** to
> observe-only on every run. So treat the "DENY ⇒ tighten into a strict
> assertion" branch below as **superseded**: a single manual DENY is not
> enough — the traverse path is unreliable, kernel mask enforcement must not
> be relied on, and the step stays observe-only. Still record the result.

Copy-paste, inside the VM (as `ubuntu`):

```bash
# ── setup: a probe principal + an ACL'd dir holding a file ──────────────
sudo useradd -m -s /bin/bash maskprobe
sudo mkdir -p /home/acl-lab/inner
echo secret | sudo tee /home/acl-lab/inner/file.txt >/dev/null
sudo chmod 700 /home/acl-lab /home/acl-lab/inner
sudo setfacl -m u:maskprobe:rX /home/acl-lab /home/acl-lab/inner
sudo setfacl -m u:maskprobe:r  /home/acl-lab/inner/file.txt

# ── baseline: the ACE works before the clobber ──────────────────────────
sudo -u maskprobe -H bash -c 'cd /tmp && cat /home/acl-lab/inner/file.txt'
# expect: secret

# ── zero the mask exactly like the sharp edge: chmod on group bits ──────
sudo chmod g-rwx /home/acl-lab/inner

# ── record the ACL state (keep this output) ─────────────────────────────
getfacl -p /home/acl-lab/inner
# expect: user:maskprobe:r-x  #effective:---   and   mask::---

# ── the question: does the kernel enforce the zeroed mask? ──────────────
sudo -u maskprobe -H bash -c 'cd /tmp && cat /home/acl-lab/inner/file.txt; echo rc=$?'
sudo -u maskprobe -H bash -c 'cd /tmp && ls /home/acl-lab/inner; echo rc=$?'

# ── environment fingerprint (keep this output too) ──────────────────────
uname -a
findmnt -no FSTYPE,SOURCE /home /
```

Record for each probe: **allow vs deny** (output + `rc=`), the full
`getfacl` block **including the `#effective:` lines**, and `uname -a`.

**Expected POSIX-correct result: DENY** — `cat` fails with `Permission
denied` (rc=1), because traverse through `inner` is authorized only by the
maskprobe ACE, which the zeroed mask caps to `---`.

**What each outcome means:**

- **DENY on stock Ubuntu** ⇒ the GH-runner allow is a **runner-image
  quirk** (overlay/fs or image customization), not Ubuntu behavior. The
  harness can tighten the observe-only print into a strict assertion for
  local-VM runs (and keep the observe posture, or environment-gate the
  assert, on GH runners). Evolve's detect+repair posture is unchanged —
  it just gains a kernel backstop it can rely on.
- **ALLOW on stock Ubuntu** ⇒ the behavior is **kernel-general** (at least
  for this kernel/fs combo), not a runner quirk. The harness's
  detect/repair-only posture *stays* — a clobbered mask remains an
  availability bug Evolve finds and fixes, never a security boundary — and
  [design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) gets
  an addendum recording that POSIX.1e mask enforcement must not be relied
  on (§4 sharp-edge + §9 observe-not-assert both reference this).

Either way the verdict, the `#effective:` evidence, and the kernel/fs
fingerprint go into §6.

---

## 6. Recording results

Fill this in and bring it back to the coordinator session. The findings
land as a **§9 addendum to
[design-linux-port-2026-06-10.md](design-linux-port-2026-06-10.md) in a
follow-up PR** (this runbook PR doesn't carry results).

```markdown
## Linux real-VM manual pass — findings (YYYY-MM-DD)

- VM: Multipass, Ubuntu 24.04.x (image build: …), Apple-Silicon host
- `uname -a`: …
- Filesystem (`findmnt -no FSTYPE /home`): …

### (a) Harness
- Result: PASS (7/7) / FAIL at step N
- Wall time: …
- Artifacts: pytest journal + /tmp/evolve-linux-e2e-diag/ archived at: …
- Deviations from the CI runs (if any): …

### (b) Visudo / mask-grant eyeball
- `visudo -c -f` on .rendered: OK / errors: …
- 9b2 lines read correct per runbook §4 checks 1–5: yes / no — notes: …
- Live grant-match probe (optional): GRANT-MATCH-OK yes/no; /etc refused yes/no

### (c) Masked-ACE kernel question
- Verdict: DENY (POSIX-correct — GH-runner allow is an image quirk)
          / ALLOW (kernel-general — posture stays, design doc gets addendum)
- Evidence: getfacl block w/ #effective lines, cat/ls rc values: …

### Follow-ups proposed
- Harness: tighten mask-denial observe → assert? yes / no / env-gated
- Design doc §9 addendum PR: …
- Anything blocking the remaining GA residuals (full wizard pass, …): …
```

Then tear down the VM (§1).
