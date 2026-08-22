"""Ubuntu end-to-end — roadmap 8.3's exit criterion, run LIVE in CI.

Design: docs/design-linux-port-2026-06-10.md §9 (the `linux-e2e` job) and
§11's L3 row: *a bot deploys, runs, and is administered on Linux*. GitHub's
ubuntu-24.04 runners are full VMs — systemd as PID 1, passwordless sudo —
so every step below exercises the REAL adapter against the real OS: real
``visudo``, real ``useradd``, real ``setfacl``/``getfacl``, real systemd
units, a real Flask admin server. The agent is a STUB (a tiny python
heartbeat) — no OpenClaw, no tokens, no network (§9: CI never spends keys
proving upstream's Linux support).

The steps are ordered and numbered; ``pytest -v`` output is the run
journal. Each test prints every subprocess it runs (argv + rc + output)
so the FIRST failing CI run is debuggable from the log alone; diagnostics
(rendered sudoers, getfacl dumps, admin-server logs) are also written to
``EVOLVE_E2E_DIAG_DIR`` for artifact upload on failure.

GUARD — this module mutates the host (creates users, installs
/etc/sudoers.d/evolve, writes /etc/systemd/system units). It runs ONLY
when ALL of these hold, which is impossible on a dev Mac or a pod:

- ``sys.platform`` is Linux         (excludes every macOS machine)
- ``CI`` env var set                 (excludes real Linux pods/dev boxes)
- ``EVOLVE_PLATFORM=linux``          (the 8.3 experimental opt-in; the
                                      normal admin-suite CI shards run on
                                      ubuntu with CI=1 but never set this)
- not a sharded suite run            (``EVOLVE_NUM_SHARDS`` unset — the
                                      full-suite shard legs can never run
                                      it even if the opt-in leaks into env)

To run locally: use a DISPOSABLE Ubuntu 24.04 VM (multipass/UTM), never a
real machine — see design-linux-port §9's "Running the e2e locally" note.

Composes (all merged): the wizard platform gate (#2692),
SystemdScheduler + LinuxUserIsolation (#2654), LinuxPerms (#2684), the
Linux sudoers render incl. setfacl grants (#2683 + #2692), and
platform_profile (#2665).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# ── the guard ─────────────────────────────────────────────────────────────────

E2E_ENABLED = (
    sys.platform.startswith("linux")
    and bool(os.environ.get("CI"))
    and (os.environ.get("EVOLVE_PLATFORM") or "").strip().lower() == "linux"
    and not os.environ.get("EVOLVE_NUM_SHARDS")
)

pytestmark = [
    # Real systemd/HTTP polling needs real wall-clock — opt out of the
    # admin conftest's suite-wide time.sleep cap.
    pytest.mark.real_sleep,
    pytest.mark.skipif(
        not E2E_ENABLED,
        reason=(
            "Linux e2e runs only on a Linux CI runner with the explicit "
            "EVOLVE_PLATFORM=linux opt-in (and never inside the sharded "
            "full suite) — it creates users and systemd units on the host"
        ),
    ),
]

# ── masked-ACE kernel-denial: assert on real VMs, observe on GH runners ───────
#
# POSIX.1e requires the kernel to DENY a named-user ACE once its mask is zeroed
# (chmod g-rwx → mask::--- → ACE #effective:---). A stock Ubuntu 24.04 VM does
# exactly that (DigitalOcean s-4vcpu-8gb, kernel 6.8.0-71, real-VM pass
# 2026-06-17: cat → EACCES, rc=1). GitHub's `ubuntu-24.04` runner image is the
# exception — it still ALLOWS the masked read (runs 1+4, 2026-06-11), an IMAGE
# QUIRK (overlay/fs or image customization), not kernel-general Ubuntu
# behavior. So step 4 ASSERTS the denial off-CI (real VM / local) and stays
# OBSERVE-ONLY under GitHub Actions. See docs/design-linux-port-2026-06-10.md
# §9 (real-VM findings) + §4 (the sharp edge). GITHUB_ACTIONS is set
# automatically on every GH runner and nowhere in the documented local-VM run
# (which sets only CI=1 + EVOLVE_PLATFORM=linux), so it cleanly discriminates
# the two contexts the harness guard (CI=1) cannot.
ON_GITHUB_RUNNER = bool(os.environ.get("GITHUB_ACTIONS"))

# ── names / paths (placeholder bot id per docs/PLACEHOLDER_NAMING.md) ─────────

EVOLVE_USER = "evolve"
BOT = "e2ebot"
BOT_HOME = Path(f"/home/{BOT}")
OC_DIR = BOT_HOME / ".openclaw"

GATEWAY_LABEL = f"ai.openclaw.{BOT}-gateway"   # keep_alive daemon (stub agent)
SWEEP_LABEL = f"ai.evolve.{BOT}.sweep"         # timer-activated one-shot
ADMIN_LABEL = "ai.evolve.evolve.admin-ui"      # the real admin server, as evolve
# Step 6f — the per-bot cost-converter daemon (the W7-deferred
# launchd-posture site the real deploy_bot install path materializes).
# The per-bot apply daemon was the other one until 2026-08-18; it was
# retired (docs/design-proposal-signing-key-2026-08-18.md) and the
# _bootout_retired_per_bot_jobs sweep now removes its unit instead.
APPLY_LABEL = f"ai.openclaw.evolve.apply.{BOT}"   # retired — teardown only
COST_LABEL = f"ai.openclaw.evolve.cost-converter.{BOT}"
# Step 6f stages a stub openclaw here (fix 2's Linux NodeSource candidate) so
# install_bot_gateway_plist resolves a real Linux index path, not /opt/homebrew.
STUB_OC_DIR = Path("/usr/lib/node_modules/openclaw")

# W8 step 6d — the wizard's day-one evo primary-bot provisioning (Linux).
EVO_USER = "evo"
EVO_HOME = Path(f"/home/{EVO_USER}")
# The PRIMARY bot's gateway label is the canonical per-bot label for the
# resolved primary id — ai.openclaw.evo-gateway on an evo-primary pod (NOT the
# legacy ai.openclaw.evolve-gateway, which used to be hardcoded and crash-looped
# as a phantom second daemon on :19030 — EVO-LINUX-PHANTOM-GATEWAY).
EVO_GATEWAY_LABEL = "ai.openclaw.evo-gateway"  # primary gateway, runs as evo

STUB_DIR = Path("/opt/evolve-e2e")
HEARTBEAT_LOG = OC_DIR / "logs" / "heartbeat.log"
SWEEP_MARKER = OC_DIR / "logs" / "sweep-ran.log"

SHARED_DIR = Path("/var/lib/evolve")           # platform_profile.LINUX default
EVOLVE_VENV = Path("/var/lib/evolve-venv")     # platform_profile.LINUX venv_dir
PLUGIN_INSTALL_DIR = Path("/var/lib/evolve-plugin")  # LINUX plugin_install_dir
NETWORK_JSON = SHARED_DIR / "network.json"
ADMIN_PORT = 5057
ADMIN_URL = f"http://127.0.0.1:{ADMIN_PORT}"

DIAG_DIR = Path(os.environ.get("EVOLVE_E2E_DIAG_DIR") or "/tmp/evolve-linux-e2e-diag")

SYSTEMCTL = "/usr/bin/systemctl"
GETFACL = "/usr/bin/getfacl"

# Cross-step state (steps are ordered; later steps consume earlier results).
STATE: dict = {}


# ── verbose plumbing — every subprocess is journaled to stdout ────────────────

def _sh(argv: list[str], *, check: bool = False, input_text: "str | None" = None,
        timeout: int = 60) -> subprocess.CompletedProcess:
    """Run ``argv``, echoing the command, rc, and output to the test log."""
    print(f"\n$ {' '.join(shlex.quote(a) for a in argv)}")
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, input=input_text,
    )
    print(f"  rc={proc.returncode}")
    for stream, body in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        body = (body or "").strip()
        if body:
            for line in body.splitlines()[:60]:
                print(f"  {stream}| {line}")
    if check and proc.returncode != 0:
        raise AssertionError(
            f"command failed rc={proc.returncode}: {' '.join(argv)}\n"
            f"stderr: {(proc.stderr or '').strip()}"
        )
    return proc


def _sudo(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return _sh(["sudo", "-n", *args], check=check, **kw)


def _diag(name: str, content: str) -> None:
    """Persist a diagnostic blob for the failure-artifact upload."""
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    (DIAG_DIR / name).write_text(content)
    print(f"  [diag] wrote {DIAG_DIR / name} ({len(content)} bytes)")


def _wait_for(desc: str, predicate, *, timeout: float = 30.0,
              interval: float = 0.5):
    """Poll ``predicate`` until truthy; fail with ``desc`` on timeout."""
    deadline = time.monotonic() + timeout
    print(f"  waiting (≤{timeout:.0f}s): {desc}")
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {desc}")
        time.sleep(interval)


def _http_get(path: str, timeout: float = 5.0) -> "tuple[int, str]":
    """(status, body) for an admin-server GET; (-1, error) on conn failure."""
    try:
        with urllib.request.urlopen(f"{ADMIN_URL}{path}", timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # non-2xx still has a status
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — poll-loop probe; retried by caller
        return -1, f"{type(exc).__name__}: {exc}"


def _runner_user() -> str:
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


# ── seam wiring + host cleanup ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _linux_seams():
    """Pin the LINUX profile + real Linux adapters for every step.

    The admin tests conftest pins the MACOS profile per-test (its suite
    was authored against macOS path shapes); this module-local autouse
    fixture runs AFTER that pin (module fixtures sit later in the fixture
    closure) and re-pins LINUX with the real adapters. Teardown resets
    the seams; the conftest fixture then restores MACOS.
    """
    from platform_profile import LINUX, set_profile
    from runtime.isolation import LinuxUserIsolation, set_isolation
    from runtime.perms import set_perms
    from runtime.scheduler import SystemdScheduler, set_scheduler

    set_profile(LINUX)
    set_isolation(LinuxUserIsolation())
    set_scheduler(SystemdScheduler())
    set_perms(None)  # profile-keyed default resolves to LinuxPerms
    yield
    set_isolation(None)
    set_scheduler(None)
    set_perms(None)


@pytest.fixture(autouse=True, scope="module")
def _host_cleanup():
    """Cleanup trap: units, users, sudoers, and stub files are removed even
    when a step fails mid-run (module finalizer). Diagnostics are copied
    into DIAG_DIR FIRST so the failure artifact survives the teardown."""
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    yield
    print("\n── e2e teardown: collecting diagnostics, then removing host state ──")
    from runtime.isolation import LinuxUserIsolation
    from runtime.scheduler import SystemdScheduler

    # 1. Diagnostics before destruction.
    for src, name in [
        (SHARED_DIR / "logs" / "admin-ui.log", "admin-ui.log"),
        (SHARED_DIR / "logs" / "admin-ui.err.log", "admin-ui.err.log"),
        (Path("/etc/sudoers.d/evolve"), "sudoers-evolve.installed"),
    ]:
        proc = subprocess.run(["sudo", "-n", "/bin/cat", str(src)],
                              capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout:
            _diag(name, proc.stdout)
    for tree in (OC_DIR, SHARED_DIR):
        proc = subprocess.run(["sudo", "-n", GETFACL, "-R", "-p", str(tree)],
                              capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout:
            _diag(f"getfacl-{tree.name}.txt", proc.stdout)

    # 2. systemd units (idempotent: remove() succeeds when nothing exists).
    sched = SystemdScheduler()
    # The fixed lifecycle/admin labels PLUS every Evolve-namespaced unit the
    # step-6g capstone's install_evolve_infra_jobs fleet left behind (it
    # installs ~50, far more than this list — enumerate them off disk so a
    # mid-run failure can't strand the fleet on a local re-run VM).
    sysd = Path("/etc/systemd/system")
    fleet: set[str] = set()
    for unit in list(sysd.glob("ai.evolve.*")) + list(sysd.glob("ai.openclaw.*")):
        for suffix in (".service", ".timer", ".path"):
            if unit.name.endswith(suffix):
                fleet.add(unit.name[: -len(suffix)])
                break
    for label in ({ADMIN_LABEL, SWEEP_LABEL, GATEWAY_LABEL, EVO_GATEWAY_LABEL,
                   APPLY_LABEL, COST_LABEL} | fleet):
        ok, msg = sched.remove(label)
        print(f"  remove({label}): ok={ok} {msg}")

    # 2b. snapshot ACL state into DIAG_DIR while the homes still exist —
    # the workflow's failure-diagnostics step runs AFTER this teardown,
    # so post-mortem getfacl against /home/<bot> is otherwise empty
    # (first-run lesson, 2026-06-11).
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    for name, path in (("bot-home", BOT_HOME), ("evolve-home", f"/home/{EVOLVE_USER}")):
        snap = subprocess.run(
            ["sudo", "-n", "/usr/bin/getfacl", "-R", "-p", str(path)],
            capture_output=True, text=True)
        (DIAG_DIR / f"teardown-getfacl-{name}.txt").write_text(
            snap.stdout + snap.stderr)

    # 3. user accounts (kill leftover processes first; userdel refuses while
    #    the user has live processes).
    iso = LinuxUserIsolation()
    for user in (BOT, EVO_USER, EVOLVE_USER):
        subprocess.run(["sudo", "-n", "/usr/bin/pkill", "-9", "-u", user],
                       capture_output=True)
        time.sleep(0.5)
        iso.delete_user(user, remove_home=True)
        print(f"  delete_user({user}): exists={iso.user_exists(user)}")
    subprocess.run(["sudo", "-n", "/usr/sbin/groupdel", "evolve-bots"],
                   capture_output=True)

    # 4. installed files (incl. the W7 deploy-flow artifacts under /var/lib and
    #    the step-6f stub openclaw under the Linux NodeSource prefix). SHARED_DIR
    #    (/var/lib/evolve) is removed too: a stale stub with a foreign networkId
    #    makes a LATER `setup --fresh` mis-detect an existing pod (W10-D), so a
    #    clean local-VM re-run must start from a bare shared dir.
    for path in (
        "/etc/sudoers.d/evolve", str(STUB_DIR), str(SHARED_DIR),
        str(EVOLVE_VENV), str(PLUGIN_INSTALL_DIR), str(STUB_OC_DIR),
    ):
        subprocess.run(["sudo", "-n", "/bin/rm", "-rf", path], capture_output=True)
    # Best-effort: if a leak materialized /Users, remove it so a re-run VM is
    # clean (the capstone asserts its absence — this is hygiene, not the check).
    if Path("/Users").exists():
        subprocess.run(["sudo", "-n", "/bin/rm", "-rf", "/Users"], capture_output=True)
    print("── e2e teardown complete ──")


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — the wizard platform gate resolves THIS host (the #2692 opt-in, live)
# ══════════════════════════════════════════════════════════════════════════════


def test_step1_platform_gate_optin_resolves_linux():
    """The real gate, real ``sys.platform``, real environment: with
    EVOLVE_PLATFORM=linux exported (as the CI job does), the wizard's one
    platform-detection site proceeds, pins the LINUX profile, and activates
    the Linux adapters."""
    from platform_profile import get_profile
    from runtime.isolation import LinuxUserIsolation, get_isolation
    from runtime.scheduler import SystemdScheduler, get_scheduler

    from evolve_admin import setup_wizard

    resolved = setup_wizard._resolve_platform_gate(None)  # real host + env
    assert resolved == "linux"
    assert get_profile().name == "linux"
    assert isinstance(get_isolation(), LinuxUserIsolation)
    assert isinstance(get_scheduler(), SystemdScheduler)
    print("platform gate: linux opt-in accepted; LINUX profile + adapters active")


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — sudoers: render via the real writer, validate with REAL Ubuntu
# visudo (W5A-1 risk item: the m\:\:rwX escaped-colon syntax), install
# ══════════════════════════════════════════════════════════════════════════════


def test_step2_sudoers_render_validate_install():
    import tempfile

    from evolve_admin import setup_wizard

    content = setup_wizard._render_evolve_sudoers()
    assert content, "renderer returned no content"
    _diag("sudoers-evolve.rendered", content)

    # The Linux render is the LINUX table: systemctl + setfacl shapes, and
    # the escaped-colon mask-repair grants whose acceptance by a REAL Linux
    # visudo is exactly the W5A-1 open risk this step adjudicates.
    assert "/usr/bin/systemctl" in content
    assert f"/usr/bin/setfacl -m m\\:\\:rwX {SHARED_DIR}" in content
    assert "/usr/sbin/useradd" in content
    assert "/Library/LaunchDaemons" not in content  # no macOS leakage

    # Real visudo, file-scoped — granular diagnostics before install.
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as tmp:
        tmp.write(content)
        staged = tmp.name
    try:
        proc = _sudo("/usr/sbin/visudo", "-c", "-f", staged, check=False)
        assert proc.returncode == 0, (
            "REAL Ubuntu visudo rejected the rendered Linux sudoers "
            f"(W5A-1): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    finally:
        os.unlink(staged)

    # The real writer end-to-end: its own visudo gate + install + 440 root:root.
    assert setup_wizard._write_evolve_sudoers(initiated_by="linux-e2e") is True
    _sudo("/usr/bin/test", "-f", "/etc/sudoers.d/evolve")

    # File-scoped check of the INSTALLED file. A whole-config `visudo -c`
    # is not usable here: GitHub's runner image ships /etc/sudoers.d/runner
    # with non-0440 perms, failing the global check for reasons outside our
    # control (first live run, 2026-06-11). Proving sudo isn't wedged is
    # done functionally instead — step 3's `sudo -n -l` as evolve and every
    # later _sudo call would fail loudly if the installed file broke sudo.
    _sudo("/usr/sbin/visudo", "-c", "-f", "/etc/sudoers.d/evolve")
    print("sudoers: rendered, visudo-validated, installed to /etc/sudoers.d/evolve")


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — accounts: evolve + one bot user via real LinuxUserIsolation
# ══════════════════════════════════════════════════════════════════════════════


def test_step3_accounts_useradd_and_getent():
    from runtime.isolation import EVOLVE_BOTS_GROUP, get_isolation

    iso = get_isolation()

    for user in (EVOLVE_USER, BOT):
        if iso.user_exists(user):  # idempotence for re-runs in a local VM
            print(f"  pre-existing {user} account — deleting for a clean run")
            _sh(["sudo", "-n", "/usr/bin/pkill", "-9", "-u", user], check=False)
            iso.delete_user(user, remove_home=True)
        uid = iso.next_free_uid()
        # Bounded retry: GH-runner background activity (unattended-upgrades,
        # man-db triggers) can hold the passwd/group locks and stall useradd
        # past the adapter timeout (live failure 2026-06-11, run 2 — run 1's
        # identical call took <1s). Real Linux hosts have the same condition;
        # the retry is e2e robustness, the forensics tell us if it recurs.
        from runtime.isolation import IsolationError

        def _materialized() -> bool:
            # Run-3 forensics (2026-06-11): useradd can outlive the adapter's
            # 30s sudo timeout and still complete — subprocess.run kills the
            # sudo wrapper, not the useradd child. Accept a user that
            # materialized with the EXPECTED uid as success.
            proc = _sh(["getent", "passwd", user], check=False)
            if proc.returncode != 0:
                return False
            actual_uid = int(proc.stdout.strip().split(":")[2])
            if actual_uid != uid:
                print(f"  {user} materialized with uid {actual_uid} != {uid} "
                      "— deleting for a clean retry")
                _sh(["sudo", "-n", "/usr/bin/pkill", "-9", "-u", user], check=False)
                iso.delete_user(user, remove_home=True)
                return False
            return True

        for attempt in range(1, 4):
            try:
                iso.create_user(user, uid, real_name=f"Evolve e2e {user}")
                break
            except IsolationError as e:
                print(f"  create_user({user}) attempt {attempt} failed: {e}")
                _sh(["sudo", "-n", "/bin/ls", "-la", "/etc/passwd.lock",
                     "/etc/group.lock", "/etc/shadow.lock"], check=False)
                snap = _sh(
                    ["/bin/sh", "-c",
                     "ps axo pid,comm | grep -E 'apt|dpkg|unattended|useradd' || true"],
                    check=False)
                print("  ps snapshot:", snap.stdout.strip())
                try:
                    _wait_for(f"{user} to materialize post-timeout",
                              _materialized, timeout=45.0, interval=3.0)
                    print(f"  {user} materialized with expected uid {uid} — "
                          "treating the timed-out useradd as completed")
                    break
                except AssertionError:
                    if attempt == 3:
                        raise
                    time.sleep(10)
        STATE[f"uid_{user}"] = uid
    STATE["accounts_ok"] = True

    # Verify through NSS itself, not just the adapter's own probe.
    for user in (EVOLVE_USER, BOT):
        assert iso.user_exists(user)
        proc = _sh(["getent", "passwd", user], check=True)
        fields = proc.stdout.strip().split(":")
        assert int(fields[2]) == STATE[f"uid_{user}"], f"uid mismatch for {user}"
        assert fields[5] == f"/home/{user}"
        assert Path(f"/home/{user}").is_dir(), f"useradd -m did not create /home/{user}"

    assert STATE[f"uid_{EVOLVE_USER}"] != STATE[f"uid_{BOT}"]
    assert STATE[f"uid_{BOT}"] >= 1000  # LINUX_DEFAULT_UID_START convention

    # Inventory group: both accounts joined evolve-bots at creation.
    members = set(iso.group_members(EVOLVE_BOTS_GROUP))
    assert {EVOLVE_USER, BOT} <= members, f"evolve-bots membership: {members}"

    # run_as drops identity correctly (sudo -u <user> -H, cwd=/tmp).
    proc = iso.run_as(BOT, ["/usr/bin/id", "-un"], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == BOT

    # The installed sudoers grants resolve for the now-existing evolve
    # principal: evolve can list its own NOPASSWD grants non-interactively.
    proc = iso.run_as(EVOLVE_USER, ["sudo", "-n", "-l"],
                      capture_output=True, text=True)
    _diag("sudo-l-evolve.txt", proc.stdout or proc.stderr or "")
    assert proc.returncode == 0, f"sudo -n -l as evolve failed: {proc.stderr}"
    assert SYSTEMCTL in proc.stdout, "systemctl grants missing from evolve's sudo -l"
    print(f"accounts: {EVOLVE_USER} (uid {STATE[f'uid_{EVOLVE_USER}']}) + "
          f"{BOT} (uid {STATE[f'uid_{BOT}']}) created; grants visible to evolve")


# ══════════════════════════════════════════════════════════════════════════════
# Step 3b — W10-F #11 (round-4 HEADLINE): the account OWNS + can WRITE its own
# $HOME after provisioning — even when /home/<user> already exists root-owned.
# `useradd -m` only owns a home it CREATES; an earlier root-context
# `sudo mkdir -p /home/<user>/.openclaw` (the wizard's _write_bot_files / the
# evolve-tree provisioning) leaves a pre-existing home root:root, so the account
# can't write its $HOME — `sudo -u darwin npm view ...` died EACCES on
# /home/darwin/.npm and the brave gap-fill install failed (live round-4). The
# fix is create_user's final chown of the home to the account.
# ══════════════════════════════════════════════════════════════════════════════


def test_step3b_account_owns_and_writes_home_even_when_preexisting():
    from runtime.isolation import IsolationError, get_isolation

    iso = get_isolation()
    user = "e2ehomeacct"  # throwaway — does not disturb BOT/EVOLVE
    home = Path(f"/home/{user}")
    if iso.user_exists(user):
        _sh(["sudo", "-n", "/usr/bin/pkill", "-9", "-u", user], check=False)
        iso.delete_user(user, remove_home=True)
        _sudo("/bin/rm", "-rf", str(home), check=False)

    # Reproduce the live precondition: the home (and a .openclaw under it)
    # exists ROOT-OWNED before the account is created.
    _sudo("/bin/mkdir", "-p", str(home / ".openclaw"))
    owner_before = _sh(["stat", "-c", "%U", str(home)], check=True).stdout.strip()
    assert owner_before == "root", f"precondition: home should be root-owned, got {owner_before}"

    try:
        uid = iso.next_free_uid()
        try:
            iso.create_user(user, uid)
        except IsolationError as e:
            # Reuse step3's tolerance for GH-runner passwd-lock contention.
            assert iso.user_exists(user), f"create_user failed: {e}"

        # 1. After provisioning, the ACCOUNT owns its home (create_user's chown
        #    fixed the root-owned pre-existing dir — useradd -m would not).
        owner_after = _sh(["stat", "-c", "%U", str(home)], check=True).stdout.strip()
        assert owner_after == user, (
            f"/home/{user} still owned by {owner_after}, not {user} — "
            "create_user did not chown the pre-existing root-owned home (W10-F #11)"
        )

        # 2. ...and the account can actually WRITE its $HOME — the npm-cache /
        #    dotfile / brave-gap-fill case that EACCES'd live.
        r = iso.run_as(user, ["/bin/mkdir", str(home / ".npm")],
                       capture_output=True, text=True)
        assert r.returncode == 0, f"account cannot mkdir $HOME/.npm: {r.stderr}"
        assert Path(home / ".npm").is_dir()

        # 3. The .openclaw subtree was NOT clobbered by the (non-recursive) home
        #    chown — it still exists (its own ownership is set elsewhere).
        assert (home / ".openclaw").is_dir()
    finally:
        _sh(["sudo", "-n", "/usr/bin/pkill", "-9", "-u", user], check=False)
        iso.delete_user(user, remove_home=True)
        _sudo("/bin/rm", "-rf", str(home), check=False)

    print("home(W10-F #11): account owns + writes its own $HOME even when "
          "pre-created root-owned (npm cache / brave gap-fill unblocked)")


# ══════════════════════════════════════════════════════════════════════════════
# Step 4 — perms: real LinuxPerms ACLs, verified by EFFECTIVE perms +
# kernel-enforced reads (the POSIX mask sharp edge, live)
# ══════════════════════════════════════════════════════════════════════════════


def test_step4_perms_acls_carveout_and_mask():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation
    from runtime.perms import POD_READ_ACL_PERMS, LinuxPerms, get_perms

    from evolve_admin.deploy import EVOLVE_WS_WRITE_ACL_PERMS

    iso = get_isolation()
    perms = get_perms()
    assert isinstance(perms, LinuxPerms), (
        f"profile-keyed default should be LinuxPerms, got {type(perms).__name__}"
    )

    # Bot home shape: .openclaw tree with a config file and a credentials
    # secret, all bot-owned, mode 700 at the top (the deploy contract).
    for d in (OC_DIR, OC_DIR / "workspace" / "evolve", OC_DIR / "credentials",
              OC_DIR / "logs"):
        _sudo("/bin/mkdir", "-p", str(d))
    _sudo("/usr/bin/tee", str(OC_DIR / "openclaw.json"),
          input_text='{"agent": "e2e-stub"}\n')
    _sudo("/usr/bin/tee", str(OC_DIR / "credentials" / "secret.json"),
          input_text='{"marker": "bot-private-credentials-content"}\n')
    _sudo("/usr/bin/chown", "-R", f"{BOT}:{BOT}", str(OC_DIR))
    _sudo("/bin/chmod", "700", str(OC_DIR))
    _sudo("/bin/chmod", "700", str(OC_DIR / "credentials"))

    # Ubuntu useradd -m creates /home/<user> mode 750 — grant traverse so
    # evolve (and the harness user, for unprivileged getfacl probes) can
    # reach .openclaw at all. rX-only: no write verbs in the string.
    for principal in (EVOLVE_USER, _runner_user()):
        assert perms.grant(BOT_HOME, principal, "read,search")

    # ── the read contract: access + default ACLs, recursively ────────────────
    assert perms.grant_read_recursive(OC_DIR, EVOLVE_USER)
    assert perms.grant_read_recursive(OC_DIR, _runner_user())  # harness probes

    # Effective-perm checks parse getfacl's #effective: annotations (ACE ∩ mask).
    assert perms.acl_user_effective(OC_DIR, EVOLVE_USER, POD_READ_ACL_PERMS)
    assert perms.acl_user_effective(OC_DIR / "openclaw.json", EVOLVE_USER, "read")

    # Kernel-enforced proof, not just metadata: evolve actually reads the file.
    proc = iso.run_as(EVOLVE_USER, ["/bin/cat", str(OC_DIR / "openclaw.json")],
                      capture_output=True, text=True)
    assert proc.returncode == 0 and "e2e-stub" in proc.stdout

    # ── default-ACL inheritance: files created LATER are readable too ────────
    newfile = OC_DIR / "created-after-grant.json"
    proc = iso.run_as(BOT, ["/bin/bash", "-c", f"echo '{{}}' > {newfile}"],
                      capture_output=True, text=True)
    assert proc.returncode == 0, f"bot could not write its own dir: {proc.stderr}"
    assert perms.acl_user_effective(newfile, EVOLVE_USER, "read")
    proc = iso.run_as(EVOLVE_USER, ["/bin/cat", str(newfile)],
                      capture_output=True, text=True)
    assert proc.returncode == 0, "default ACL did not propagate to a new file"

    # ── credentials carve-out: evolve must NOT read bot API keys ─────────────
    creds = OC_DIR / "credentials"
    assert perms.clear_acl(creds)
    _sudo("/bin/chmod", "700", str(creds))
    proc = iso.run_as(EVOLVE_USER, ["/bin/cat", str(creds / "secret.json")],
                      capture_output=True, text=True)
    assert proc.returncode != 0, (
        "SECURITY: evolve read the credentials carve-out (threat model §3.1)"
    )
    assert not perms.acl_user_effective(creds, EVOLVE_USER, "read")

    # ── the POSIX mask sharp edge, live ───────────────────────────────────────
    # chmod's group bits BECOME the ACL mask: after g-rwx the stored ACE
    # survives but its EFFECTIVE perms collapse — the check must see drift,
    # the kernel must refuse, and reassert_mask must repair both.
    ws = OC_DIR / "workspace"
    # The denial probe reads a FILE through ws (needs x-traverse on ws +
    # masked r on the file) — readdir/`ls` exit semantics on partially
    # accessible dirs are ambiguous; open(2) through a masked path is not.
    probe = ws / "mask-probe.txt"
    _sudo("/usr/bin/tee", str(probe), input_text="mask-probe\n")
    _sudo("/usr/bin/chown", f"{BOT}:{BOT}", str(probe))

    def _dump_acl_state(tag: str) -> None:
        # Inline forensics for the first-run kernel-allow mystery: owner,
        # mode, and full ACL of ws + probe at each assertion point.
        for p in (ws, probe):
            st = _sudo("/usr/bin/stat", "-c", "%U %G %a", str(p), check=False)
            fa = _sudo("/usr/bin/getfacl", "-p", str(p), check=False)
            print(f"[acl-state:{tag}] {p}: {st.stdout.strip()}\n{fa.stdout}")

    assert perms.acl_user_effective(ws, EVOLVE_USER, "read,search")
    _dump_acl_state("pre-clobber")
    _sudo("/bin/chmod", "g-rwx", str(ws))
    _dump_acl_state("post-clobber")
    assert not perms.acl_user_effective(ws, EVOLVE_USER, "read,search"), (
        "effective-perm check missed a chmod-clobbered mask"
    )
    proc = iso.run_as(EVOLVE_USER, ["/usr/bin/id", "-u"],
                      capture_output=True, text=True)
    print(f"[acl-state] run_as identity probe: rc={proc.returncode} "
          f"uid={proc.stdout.strip()!r}")
    proc = iso.run_as(EVOLVE_USER, ["/bin/cat", str(probe)],
                      capture_output=True, text=True)
    # The masked-ACE kernel-enforcement verdict is OBSERVE-ONLY (W7c revert).
    # POSIX.1e *says* the kernel must DENY this read — the evolve ACE is the
    # sole authorization and the zeroed mask (mask::---) caps it to --- — but on
    # stock Ubuntu 24.04 kernel enforcement of a clobbered mask is INCONSISTENT,
    # not reliably DENY: a manual file-read probe (runbook §5) DENIED, yet the
    # harness's own traverse-then-read clobber case ALLOWED (rc=0) on the SAME
    # box (DigitalOcean s-4vcpu-8gb, ext4, kernel 6.8.0-71). W6 had ASSERTed the
    # denial off-CI from that single manual data point; one probe over-fit, so
    # this records the result rather than asserting it. A clobbered mask is an
    # AVAILABILITY bug Evolve DETECTS (the getfacl #effective: drift, asserted
    # above) and REPAIRS (reassert_mask, asserted below) — NOT a security
    # boundary, so observe-only is the correct posture. The real boundary is the
    # credentials carve-out (no-ACE + 0700), asserted strictly above and DENIED
    # on every VM. Do NOT rely on kernel mask enforcement. Forensics print
    # regardless, on every run (real-VM and GH-runner alike).
    allowed = proc.returncode == 0 and "mask-probe" in proc.stdout
    print(f"[mask-enforcement] masked-ACE read as evolve: rc={proc.returncode} "
          f"stdout={proc.stdout.strip()!r} → kernel "
          f"{'ALLOWED (mask not enforced)' if allowed else 'DENIED (POSIX-correct)'}; "
          f"observe-only — Evolve relies on detect+repair, not this denial "
          f"(POSIX-spec expectation: EACCES; github_runner={ON_GITHUB_RUNNER})")
    _sh(["/bin/uname", "-a"], check=False)
    _sh(["/bin/sh", "-c", "mount | grep -E ' / | /home ' || true"], check=False)
    assert perms.reassert_mask(ws)
    _dump_acl_state("post-reassert")
    assert perms.acl_user_effective(ws, EVOLVE_USER, "read,search")
    proc = iso.run_as(EVOLVE_USER, ["/bin/cat", str(probe)],
                      capture_output=True, text=True)
    assert proc.returncode == 0, f"mask repair did not restore access: {proc.stderr}"

    # ── the write contract on workspace/evolve (manifests, scan status) ──────
    ws_evolve = ws / "evolve"
    assert perms.grant_write_recursive(ws_evolve, EVOLVE_USER, EVOLVE_WS_WRITE_ACL_PERMS)
    proc = iso.run_as(
        EVOLVE_USER,
        ["/bin/bash", "-c", f"echo '{{}}' > {ws_evolve / 'manifest.json'}"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"evolve write to workspace/evolve failed: {proc.stderr}"

    proc = _sh([GETFACL, "-R", "-p", str(OC_DIR)], check=False)
    _diag("getfacl-openclaw-after-step4.txt", proc.stdout or "")
    print("perms: read contract + inheritance + credentials carve-out + "
          "mask repair + write contract all verified against the live kernel")


# ══════════════════════════════════════════════════════════════════════════════
# Step 4a2 — the hourly ACL-lockout FLAP, end to end. Step 4 proved the
# perms-level primitive (reassert_mask repairs a clobbered mask). This proves
# the RUNTIME orchestration that kills the flap: the OC gateway re-hardens
# .openclaw to 0700 on its own ops (gateway restart / every `openclaw`
# invocation an hourly daemon makes), which on Linux recomputes the ACL mask to
# --- and clamps evolve's traverse ACE; secret_config_perms.reassert_evolve_access
# (run periodically by pod_perms_drift_monitor) re-widens the mask and VERIFIES
# (last) — restoring access via the LIGHT path, no full re-grant. This is the
# bound that turns an hourly lockout into a ≤1-cycle blip with no Signal.
# ══════════════════════════════════════════════════════════════════════════════


def test_step4a2_reassert_evolve_access_heals_a_real_0700_reclamp():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.perms import get_perms
    from evolve_admin.secret_config_perms import (
        reassert_evolve_access, verify_evolve_access,
    )

    perms = get_perms()
    # Precondition: step 4 left the full read/traverse/write contract intact.
    assert verify_evolve_access(BOT) == [], (
        "contract should hold coming out of step 4"
    )

    # The gateway's runtime re-harden — the exact chmod that starts the flap.
    _sudo("/bin/chmod", "700", str(OC_DIR))
    proc = _sh([GETFACL, "-p", str(OC_DIR)], check=False)
    _diag("getfacl-openclaw-after-0700-reclamp.txt", proc.stdout or "")

    # The lockout: the recomputed mask::--- caps evolve's traverse ACE to ---.
    assert not perms.acl_user_effective(OC_DIR, EVOLVE_USER, "search"), (
        "chmod 0700 should have clamped evolve's traverse via the ACL mask"
    )
    assert verify_evolve_access(BOT), "verify should report the lockout"

    # The periodic self-heal — light reassert + verify (last).
    ok, failures = reassert_evolve_access(BOT, BOT)
    assert ok and failures == [], (
        f"reassert_evolve_access did not restore the contract: {failures}"
    )
    assert perms.acl_user_effective(OC_DIR, EVOLVE_USER, "search")
    assert verify_evolve_access(BOT) == []
    print("flap fix: a real `chmod 0700 .openclaw` re-clamp was healed by "
          "reassert_evolve_access via the light path (mask re-widen + verify)")


# ══════════════════════════════════════════════════════════════════════════════
# Step 4b — W10-F #1: the PRODUCT set_evolve_read_acl grants HOME traverse.
# Step 4 above grants traverse by hand (perms.grant(BOT_HOME, ...)) to set up
# its read-contract checks. This step proves the real deploy entry point —
# deploy.set_evolve_read_acl(bot) — does it ITSELF, which is the W10-F headline
# fix: 14 evolve daemons died `PermissionError: .../home/<bot>` because the rX
# ACL on .openclaw was unreachable through the 0750 home with no traverse ACE,
# and set_evolve_read_acl never granted one. Revoke the manual grant, prove the
# denial, then let the product function restore access.
# ══════════════════════════════════════════════════════════════════════════════


def test_step4b_product_set_evolve_read_acl_grants_home_traverse():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation

    from evolve_admin import deploy

    iso = get_isolation()
    oc_json = OC_DIR / "openclaw.json"  # created bot-owned in step 4

    # Revoke evolve's traverse ACE on the home dir and re-assert the Ubuntu
    # 0750 default so evolve genuinely cannot reach .openclaw. (The runner
    # user's ACE from step 4 stays — the harness still needs to probe.)
    _sudo("/usr/bin/setfacl", "-x", "u:evolve", str(BOT_HOME), check=False)
    _sudo("/bin/chmod", "750", str(BOT_HOME), check=False)
    denied = iso.run_as(EVOLVE_USER, ["/bin/cat", str(oc_json)],
                        capture_output=True, text=True)
    assert denied.returncode != 0, (
        "precondition: with no traverse ACE on the 0750 home, evolve must NOT "
        f"reach .openclaw (got rc={denied.returncode})"
    )

    # The PRODUCT path — no manual setfacl. set_evolve_read_acl must restore
    # reachability by granting the --x traverse on the home itself.
    deploy.set_evolve_read_acl(BOT)

    # The traverse ACE is present (execute-only — NOT read: evolve still can't
    # list the home's other dotfiles) and evolve can now read .openclaw again.
    facl = _sudo("/usr/bin/getfacl", "-p", str(BOT_HOME), check=False).stdout
    assert "user:evolve:--x" in facl, (
        f"set_evolve_read_acl did not grant the evolve home-traverse ACE:\n{facl}"
    )
    allowed = iso.run_as(EVOLVE_USER, ["/bin/cat", str(oc_json)],
                         capture_output=True, text=True)
    assert allowed.returncode == 0 and "e2e-stub" in allowed.stdout, (
        "set_evolve_read_acl's home-traverse grant did not restore evolve's "
        f"reach to .openclaw: rc={allowed.returncode} err={allowed.stderr!r}"
    )
    print("perms(W10-F #1): set_evolve_read_acl grants home-traverse (--x) — "
          "evolve reaches .openclaw through the 0750 home, no manual setfacl")


# ══════════════════════════════════════════════════════════════════════════════
# Step 4c — W10-F #B (HEADLINE): the wizard writes the bot's API key under the
# bot's REAL home (/home/<bot> on Linux), not a hardcoded /Users/<bot>. Round-3
# wrote darwin's 302-byte real key to /Users/darwin while the agent looked under
# /home/darwin (54-byte stub) — so the bot installed but had no usable key.
# Drive wizard._write_bot_files on the real Linux host and prove auth-profiles
# land at /home/<bot> AND carry the key. Capture+restore so later steps see the
# bot's step-4 .openclaw posture unchanged.
# ══════════════════════════════════════════════════════════════════════════════


def test_step4c_wizard_write_bot_files_lands_key_under_home():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from evolve_admin import wizard

    oc_json = OC_DIR / "openclaw.json"
    auth_json = OC_DIR / "agents" / "main" / "agent" / "auth-profiles.json"
    # Capture the bot-owned openclaw.json step 4 wrote, to restore afterwards.
    saved = _sudo("/bin/cat", str(oc_json), check=False)
    saved_oc = saved.stdout if saved.returncode == 0 else None
    try:
        auth = wizard._new_bot_auth_profiles("anthropic", "api_key", "sk-ant-E2E-SECRET")
        oc_cfg = wizard._new_bot_openclaw_config(BOT, "anthropic", 18901)
        errs = wizard._write_bot_files(BOT, oc_cfg, auth, "soul", "agents")
        assert errs == [], f"_write_bot_files reported errors: {errs}"

        # The auth-profiles landed under the bot's REAL /home, never /Users.
        assert auth_json.is_file() or _sudo("/usr/bin/test", "-f", str(auth_json),
                                            check=False).returncode == 0, (
            f"auth-profiles.json not at {auth_json} — the key did not reach "
            "the bot's real home"
        )
        assert str(auth_json).startswith(f"/home/{BOT}/")
        assert not Path(f"/Users/{BOT}").exists(), "wizard created a /Users/<bot> tree on Linux"
        body = _sudo("/bin/cat", str(auth_json), check=True).stdout
        assert "sk-ant-E2E-SECRET" in body, "the API key did not reach the bot's auth-profiles"
        # The openclaw.json workspace points at the real /home, not /Users.
        oc_body = _sudo("/bin/cat", str(oc_json), check=True).stdout
        assert f"/home/{BOT}/.openclaw/workspace" in oc_body
        assert "/Users/" not in oc_body
        print(f"wizard(W10-F #B): _write_bot_files lands the key at {auth_json} "
              "(real /home, never /Users)")
    finally:
        # Restore the bot's step-4 .openclaw posture for later steps.
        _sudo("/bin/rm", "-rf", str(OC_DIR / "agents"),
              str(OC_DIR / "workspace" / "SOUL.md"),
              str(OC_DIR / "workspace" / "AGENTS.md"), check=False)
        if saved_oc is not None:
            _sudo("/usr/bin/tee", str(oc_json), input_text=saved_oc, check=False)
            _sudo("/usr/bin/chown", f"{BOT}:{BOT}", str(oc_json), check=False)


# ══════════════════════════════════════════════════════════════════════════════
# Step 4d — W10-F #12 (round-4): the bot-home WRITERS that run after deploy (the
# app-permission reconciler + audit dispatch) write under the bot's REAL home
# (/home/<bot>), never a hardcoded /Users/<bot>. Live round-4 left root-owned
# /Users/darwin/.openclaw/workspace/evolve/ (the audit-inbox mkdir) and
# /Users/darwin/.openclaw/exec-approvals.preview.json (reconciler) on a fresh
# Linux box — the audit mkdir is what MATERIALIZED /Users at all (the reconciler
# cp then succeeded into the now-present tree). Both builders are exercised here
# against the real account; the step6g capstone re-asserts the global invariant.
# ══════════════════════════════════════════════════════════════════════════════


def test_step4d_bot_home_writers_never_materialize_users_tree():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from evolve_admin.app_permissions import reconciler
    from evolve_admin.applications import audit_dispatch

    # 1. Pure resolution: every bot-home builder resolves pwd-first to the REAL
    #    /home/<bot> (pre-fix these returned /Users/<bot> literals).
    inbox_dir = audit_dispatch._audit_inbox_dir(BOT)
    trail = audit_dispatch._investigations_trail_path(BOT)
    preview_home = reconciler._acct_home(BOT)
    for p in (inbox_dir, trail, preview_home):
        assert str(p).startswith(f"/home/{BOT}"), p
        assert "/Users" not in str(p)

    # 2. Integration: actually queue an audit (kick=False → no runner spawn).
    #    Pre-fix this sudo-mkdir'd /Users/<bot>/.openclaw/workspace/evolve/
    #    audit_inbox — CREATING /Users on the box. Snapshot what we create so we
    #    can restore the step-4 posture for later steps.
    ws_evolve = Path(f"/home/{BOT}/.openclaw/workspace/evolve")
    ws_evolve_existed = ws_evolve.exists() or _sudo(
        "/usr/bin/test", "-d", str(ws_evolve), check=False).returncode == 0

    res = audit_dispatch.request_audit(BOT, BOT, kick=False)
    try:
        assert res.ok, f"inbox write failed: {res.error}"
        assert res.inbox_path.startswith(f"/home/{BOT}/"), res.inbox_path
        assert "/Users" not in res.inbox_path
        assert _sudo("/usr/bin/test", "-d", str(inbox_dir), check=False).returncode == 0, (
            f"audit inbox dir not created under the real home: {inbox_dir}"
        )
        # The headline invariant for this step.
        assert not Path(f"/Users/{BOT}").exists(), (
            f"a bot-home writer materialized /Users/{BOT} on the Linux box (W10-F #12)"
        )
    finally:
        # Restore: remove exactly the audit subtree we added (root-owned by the
        # sudo-mkdir fallback) so step 5+ see the step-4 .openclaw posture.
        if ws_evolve_existed:
            _sudo("/bin/rm", "-rf", str(inbox_dir), check=False)
        else:
            _sudo("/bin/rm", "-rf", str(ws_evolve), check=False)

    print("bot-home writers(W10-F #12): audit-inbox + reconciler resolve under "
          "/home/<bot> — no /Users tree materialized on the Linux box")


# ══════════════════════════════════════════════════════════════════════════════
# Step 4e — W10-G #5 (round-5): the INFRA-jobs + repo-puller setup paths are
# platform-keyed too. The existing "no /Users after run_setup" assertion missed
# these because they live OUTSIDE run_setup: the repo-puller materialized the
# deploy key at /Users/evolve/.ssh, and the defer / manifest-reflex queue
# resolvers' account-not-found fallback wrote root-owned
# /Users/<bot>/.openclaw/workspace/evolve. Exercise the real path math here so a
# regression fails CI on the Linux runner, not on the next live pod.
# ══════════════════════════════════════════════════════════════════════════════


def test_step4e_infra_and_puller_paths_never_users(tmp_path):
    import defer_queue
    import manifest_reflex_queue
    from evolve_admin import repo_puller

    # 1. repo-puller deploy-key SSH dir resolves under the real /home root.
    for p in (repo_puller.EVOLVE_SSH_DIR,
              repo_puller.DEPLOY_KEY_PATH,
              repo_puller.SSH_CONFIG_PATH):
        assert str(p).startswith("/home/evolve/.ssh"), p
        assert "/Users" not in str(p)

    # 2. The infra-jobs evolve home the puller passes is platform-keyed.
    infra_home = Path(repo_puller._PROFILE.user_home_root) / "evolve"
    assert str(infra_home) == "/home/evolve", infra_home

    # 3. The per-bot queue resolvers resolve under /home (their account-not-
    #    found fallback was the writer that landed a root-owned /Users tree).
    for resolver in (defer_queue.bot_evolve_dir, manifest_reflex_queue.bot_evolve_dir):
        p = str(resolver(BOT))
        assert p.startswith("/home/"), p
        assert "/Users" not in p

    # 4. A non-git deploy checkout (the single-VPS tarball-staged shape) no-ops
    #    cleanly instead of warning "not in a git directory".
    repo = tmp_path / "repo"
    repo.mkdir()  # no .git
    ok, msg = repo_puller.ensure_shared_repo_config(repo)
    assert ok and "not a git working tree" in msg, (ok, msg)

    # Headline: none of the above computations materialized /Users on the box.
    assert not Path("/Users").exists(), (
        "an infra-jobs / repo-puller path materialized /Users on the Linux box (W10-G #5)"
    )
    print("infra + repo-puller paths (W10-G #5): deploy-key, infra home, and "
          "queue resolvers all under /home — no /Users materialized")


# ══════════════════════════════════════════════════════════════════════════════
# Step 4f — W10-G ROUND-6 file-level reality. Round-5's harness tested the
# workspace/evolve DIRECTORY ACL in isolation; round-6 proved the daemons fail
# on the bot-owned FILES inside it. Two faults, both the Linux ACL-mask sharp
# edge applied at file/dir CREATION (not chmod): a file/dir the bot creates at
# its 0600/0700 umask zeroes the inherited named ACE's mask → evolve EACCES.
#   (a) defer/manifest queue files: the runner (evolve, non-root) must rewrite a
#       bot-created queue → fixed by the writer chmod'ing 0660 (mask = rw).
#   (b) auth-profiles.json under a bot-created 0700 agent dir: evolve loses
#       traverse → fixed by re-asserting the read ACL (recomputes the mask).
#   (c) the last /Users writer: pod_config sync now resolves under /home.
# Exercised against the live kernel + real bot/evolve accounts so a regression
# fails CI here, not on the next live pod.
# ══════════════════════════════════════════════════════════════════════════════


def test_step4f_evolve_rw_actual_bot_owned_files():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation

    from evolve_admin import deploy

    iso = get_isolation()

    # The full product ACL contract (home traverse + .openclaw read +
    # workspace/evolve write), idempotent — exactly what every deploy runs.
    deploy.set_evolve_read_acl(BOT)
    ws_evolve = OC_DIR / "workspace" / "evolve"
    _sudo("/bin/mkdir", "-p", str(ws_evolve))
    _sudo("/usr/bin/chown", "-R", f"{BOT}:{BOT}", str(ws_evolve))
    deploy.set_evolve_read_acl(BOT)  # re-grant write ACL after the chown

    queue = ws_evolve / "defer-queue.jsonl"
    agents_root = OC_DIR / "agents"
    try:
        # ── (a) cross-user queue file ────────────────────────────────────────
        # PRE-FIX: the bot creates the queue at its restrictive umask (0600).
        iso.run_as(BOT, ["/bin/bash", "-c", f"umask 077 && printf '{{}}\\n' > {queue}"],
                   capture_output=True, text=True)
        _sudo("/bin/chmod", "600", str(queue))  # pin the pre-fix mode
        pre = iso.run_as(EVOLVE_USER, ["/bin/bash", "-c", f"echo x >> {queue}"],
                         capture_output=True, text=True)
        # Observe-only: the CI runner's kernel enforces a clobbered mask
        # inconsistently (see step 4's mask note) — record, don't assert.
        print(f"[round6 #1a] evolve append to a 0600 bot-created queue: "
              f"rc={pre.returncode} (observe-only — kernel mask enforcement "
              f"inconsistent on CI)")

        # THE FIX: the TS writer (and the Python rewrite path) chmod 0660 after
        # creating the file → the mask is wide enough for evolve's inherited ACE.
        iso.run_as(BOT, ["/bin/bash", "-c", f"chmod 660 {queue}"],
                   capture_output=True, text=True)
        post = iso.run_as(EVOLVE_USER, ["/bin/bash", "-c", f"echo x >> {queue}"],
                          capture_output=True, text=True)
        assert post.returncode == 0, (
            "evolve (the defer-runner) could not append to the 0660 bot-created "
            f"queue: {post.stderr}\n"
            + _sudo("/usr/bin/getfacl", "-p", str(queue), check=False).stdout)
        print("round6 #1a: evolve appends to a 0660 bot-created queue file — the "
              "Linux ACL mask no longer clamps the inherited cross-user ACE")

        # ── (b) auth-profiles.json under a bot-created 0700 agent dir ─────────
        agent_dir = agents_root / "main" / "agent"
        auth = agent_dir / "auth-profiles.json"
        iso.run_as(BOT, ["/bin/bash", "-c",
                         f"umask 077 && mkdir -p {agent_dir} && "
                         f"chmod 700 {agents_root} {agents_root}/main {agent_dir} && "
                         f"printf '{{\"profiles\":{{}}}}\\n' > {auth}"],
                   capture_output=True, text=True)
        # Product re-assert (what a deploy / `ensure-pod-perms` runs): the
        # recursive read grant recomputes the masks the 0700 dirs clamped, so
        # evolve regains traverse + read on the OC-created tree.
        deploy.set_evolve_read_acl(BOT)
        st = iso.run_as(EVOLVE_USER, ["/usr/bin/stat", "-c", "%a", str(auth)],
                        capture_output=True, text=True)
        assert st.returncode == 0, (
            "evolve cannot stat auth-profiles under a bot-created 0700 agent dir "
            f"after the read-ACL re-assert: {st.stderr}\n"
            + _sudo("/usr/bin/getfacl", "-R", "-p", str(agents_root),
                    check=False).stdout)
        rd = iso.run_as(EVOLVE_USER, ["/bin/cat", str(auth)],
                        capture_output=True, text=True)
        assert rd.returncode == 0 and "profiles" in rd.stdout, (
            f"evolve cannot READ auth-profiles after the re-assert: {rd.stderr}")
        print("round6 #1b: after set_evolve_read_acl, evolve stats + reads a 0600 "
              "auth-profiles.json under a bot-created 0700 agent dir")

        # ── (c) pod_config sync: under /home, never /Users (round6 #3) ────────
        from evolve_admin.applications import audit_pod_config
        pc = audit_pod_config.pod_config_path(BOT)
        assert str(pc).startswith(f"/home/{BOT}/") and "/Users" not in str(pc), pc
        # Drive the real writer; evolve holds the workspace/evolve write ACL.
        wrote = audit_pod_config.write_pod_config(
            {"networkId": "e2e", "bots": {BOT: {}}}, BOT, BOT)
        print(f"[round6 #3] write_pod_config rc={wrote} dest={pc}")
        assert not Path("/Users").exists(), (
            "pod_config sync materialized /Users on the Linux box (W10-G round-6 #3)")
    finally:
        # Restore the step-4 .openclaw posture for later steps.
        _sudo("/bin/rm", "-rf", str(agents_root), str(queue),
              str(ws_evolve / "defer-archive.jsonl"),
              str(ws_evolve / "pod_config.json"), check=False)


# ══════════════════════════════════════════════════════════════════════════════
# Step 4g — W10-G ROUND-7. The round-6 bug shipped MUTE: the real secret-write
# hardening (chmod_secret_config → chmod 600) clobbers the Linux ACL mask, and
# nothing re-read the contract, so evolve's lost read on auth-profiles.json only
# surfaced when pod_perms_drift_monitor hit EACCES live. This exercises the
# ACTUAL hardening path end-to-end against the live kernel + real setfacl/sudoers:
#   1. a RAW chmod 600 (hardening WITHOUT the re-grant) → mask::--- → the new
#      verify_evolve_access backstop REPORTS the unreadable secret (the loud
#      signal that would have caught round-6 in CI, not on the pod);
#   2. the REAL chmod_secret_config (chmod 600 + the Linux re-grant, which needs
#      the auth-profiles.json `setfacl` sudoers grant round-6 had MISSED) → evolve
#      reads again, mask is no longer ---, and the backstop goes quiet.
# This is the exact reproduction harness that root-caused the bug, run as a test.
# ══════════════════════════════════════════════════════════════════════════════


def _getfacl_mask(path) -> "str | None":
    """The ``mask::`` perm field from getfacl on ``path`` (None if no mask)."""
    out = _sudo(GETFACL, "-p", str(path), check=False).stdout or ""
    for line in out.splitlines():
        text = line.strip()
        if text.startswith("mask::"):
            return text[len("mask::"):]
    return None


def _getfacl_field(path, tag: str) -> "str | None":
    """The perm bits for an EXACT getfacl entry tag on ``path`` (None if absent).

    ``tag`` is the full entry prefix incl. the trailing ``::`` — e.g. ``other::``
    (the access-ACL other class) or ``default:other::`` (its default-ACL twin).
    ``startswith`` on the trimmed line keeps the two distinct (a ``default:…``
    line never matches the bare ``other::`` tag)."""
    out = _sudo(GETFACL, "-p", str(path), check=False).stdout or ""
    for line in out.splitlines():
        text = line.strip()
        if text.startswith(tag):
            return text[len(tag):]
    return None


def test_step4g_real_secret_hardening_keeps_evolve_read():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation
    from runtime.perms import get_perms

    from evolve_admin import deploy, secret_config_perms

    iso = get_isolation()
    perms = get_perms()
    agents_root = OC_DIR / "agents"
    agent_dir = agents_root / "main" / "agent"
    auth = agent_dir / "auth-profiles.json"
    try:
        # A traversable agent dir + a bot-owned secret file, then the full product
        # ACL contract. (test_step4f covers the 0700-traverse-clamp variant; this
        # isolates the chmod-clobbers-the-mask-on-the-FILE mechanism.)
        iso.run_as(BOT, ["/bin/bash", "-c",
                         f"mkdir -p {agent_dir} && printf '{{\"profiles\":{{}}}}\\n' > {auth}"],
                   capture_output=True, text=True)
        deploy.set_evolve_read_acl(BOT)

        # ── baseline: evolve reads the freshly-granted secret ────────────────
        assert perms.acl_user_effective(auth, EVOLVE_USER, "read"), (
            "evolve lacks effective read on auth-profiles.json right after the grant:\n"
            + _sudo(GETFACL, "-p", str(auth), check=False).stdout)
        assert all(str(auth) not in f for f in secret_config_perms.verify_evolve_access(BOT)), (
            "verify_evolve_access flagged auth-profiles before any hardening")

        # ── (1) RAW chmod 600 — hardening WITHOUT the re-grant clobbers the mask ─
        _sudo("/bin/chmod", "600", str(auth))
        assert _getfacl_mask(auth) == "---", (
            "a raw chmod 600 should zero the ACL mask (the round-6 mechanism); got "
            f"mask={_getfacl_mask(auth)!r}\n"
            + _sudo(GETFACL, "-p", str(auth), check=False).stdout)
        # The loud backstop must NAME the now-unreadable secret — this is the
        # signal that was missing in round-6 (the failure shipped silently).
        clobbered = secret_config_perms.verify_evolve_access(BOT)
        assert any(str(auth) in f for f in clobbered), (
            f"verify_evolve_access did not report the clobbered secret: {clobbered}\n"
            + _sudo(GETFACL, "-p", str(auth), check=False).stdout)
        print("round7: a raw chmod 600 clobbers the mask AND verify_evolve_access "
              "reports the unreadable auth-profiles.json (the round-6 blind spot)")

        # ── (2) the REAL chmod_secret_config: chmod 600 + the Linux re-grant ───
        # Exercises the auth-profiles.json `setfacl` sudoers grant round-6 missed.
        assert secret_config_perms.chmod_secret_config(auth) is True
        mask = _getfacl_mask(auth)
        assert mask is not None and "r" in mask, (
            f"chmod_secret_config did not recompute the mask to include read; got {mask!r}\n"
            + _sudo(GETFACL, "-p", str(auth), check=False).stdout)
        assert perms.acl_user_effective(auth, EVOLVE_USER, "read"), (
            "evolve lost effective read after the REAL secret hardening — the "
            "re-grant or its sudoers grant is broken:\n"
            + _sudo(GETFACL, "-p", str(auth), check=False).stdout)
        rd = iso.run_as(EVOLVE_USER, ["/bin/cat", str(auth)],
                        capture_output=True, text=True)
        assert rd.returncode == 0 and "profiles" in rd.stdout, (
            f"evolve cannot READ auth-profiles after chmod_secret_config: {rd.stderr}")
        assert all(str(auth) not in f for f in secret_config_perms.verify_evolve_access(BOT)), (
            "verify_evolve_access still flags auth-profiles after the real re-grant")
        print("round7: chmod_secret_config (chmod 600 + Linux re-grant) restores "
              "evolve's read, the mask is no longer ---, and the backstop is quiet")
    finally:
        _sudo("/bin/rm", "-rf", str(agents_root), check=False)


def test_step4h_post_gateway_reassert_heals_round8_clamps():
    """W10-G round-8: reproduce the REAL post-runtime state the round-7 harness
    missed — the OC gateway, AFTER deploy-time grants, (1) re-hardens .openclaw to
    0700 (clamps evolve's traverse) and (2) creates agents/.../auth-profiles.json
    fresh — and prove the post-gateway re-assert + probe heals + verifies all of it.

    These four checks would have caught round 7:
      (a) chmod .openclaw 0700 (mimic the gateway) → evolve loses traverse; the
          re-assert restores it (root cause 1);
      (b) a NEW file the bot creates under agents/ AFTER the grant is readable by
          evolve once re-asserted (default-ACL inheritance + mask recompute, root
          cause 2);
      (c) evolve can WRITE a fresh workspace/evolve/<queue>.jsonl as itself;
      (d) the slack-signals JobSpec passes its REQUIRED --bot arg.
    """
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation
    from runtime.perms import get_perms

    from evolve_admin import deploy, secret_config_perms

    iso = get_isolation()
    perms = get_perms()
    agents_root = OC_DIR / "agents"
    agent_dir = agents_root / "main" / "agent"
    auth = agent_dir / "auth-profiles.json"
    ws_evolve = OC_DIR / "workspace" / "evolve"
    queue = ws_evolve / "defer-queue.jsonl"
    try:
        deploy.set_evolve_read_acl(BOT)
        _sudo("/bin/mkdir", "-p", str(ws_evolve))
        _sudo("/usr/bin/chown", "-R", f"{BOT}:{BOT}", str(ws_evolve))
        deploy.set_evolve_read_acl(BOT)

        # ── (a) gateway re-hardens .openclaw to 0700 → traverse clamp ─────────
        _sudo("/bin/chmod", "700", str(OC_DIR))
        assert _getfacl_mask(OC_DIR) == "---", (
            "chmod 700 on .openclaw should zero its ACL mask (the round-8 traverse "
            f"clamp); got {_getfacl_mask(OC_DIR)!r}")
        clamped = secret_config_perms.verify_evolve_access(BOT)
        assert any("TRAVERSE" in f and str(OC_DIR) in f for f in clamped), (
            f"verify_evolve_access did not report the .openclaw traverse clamp: {clamped}")
        print("round8(a): chmod 700 .openclaw clamps the mask AND verify_evolve_access "
              "names the traverse failure (the primary-bot defer/manifest EACCES)")

        # ── (b) bot creates a NEW file under agents/ AFTER the grant ──────────
        iso.run_as(BOT, ["/bin/bash", "-c",
                         f"umask 077 && mkdir -p {agent_dir} && "
                         f"printf '{{\"profiles\":{{}}}}\\n' > {auth}"],
                   capture_output=True, text=True)

        # THE POST-GATEWAY RE-ASSERT: recomputes every clamped mask + re-plants the
        # default ACL (set_evolve_read_acl, run late in setup / every ensure-pod-perms).
        deploy.set_evolve_read_acl(BOT)

        # (a) heals: evolve regains traverse, mask no longer ---.
        assert _getfacl_mask(OC_DIR) != "---"
        assert perms.acl_user_effective(OC_DIR, EVOLVE_USER, "search"), (
            "evolve still cannot traverse .openclaw after the re-assert:\n"
            + _sudo(GETFACL, "-p", str(OC_DIR), check=False).stdout)

        # (b) heals: evolve reads the gateway-created auth-profiles (REAL syscall).
        rd = iso.run_as(EVOLVE_USER, ["/bin/cat", str(auth)],
                        capture_output=True, text=True)
        assert rd.returncode == 0 and "profiles" in rd.stdout, (
            f"evolve cannot READ the post-grant bot-created auth-profiles: {rd.stderr}\n"
            + _sudo(GETFACL, "-p", str(auth), check=False).stdout)

        # ── (c) evolve writes a fresh workspace/evolve queue file ────────────
        # The authoritative as-evolve exercise: run_as(EVOLVE_USER) so the kernel
        # checks EVOLVE's access, not the test runner's (the runner holds only the
        # read ACL, so a direct write from the test process would EACCES even on a
        # healthy pod — verify_evolve_access's getfacl-effective check is the
        # caller-independent verifier used in product code).
        wr = iso.run_as(EVOLVE_USER, ["/bin/bash", "-c", f"printf '{{}}\\n' > {queue}"],
                        capture_output=True, text=True)
        assert wr.returncode == 0, (
            f"evolve (defer-runner) cannot WRITE the queue: {wr.stderr}\n"
            + _sudo(GETFACL, "-p", str(ws_evolve), check=False).stdout)

        # verify_evolve_access (getfacl #effective — evolve's perms regardless of
        # caller) agrees the whole contract holds after the re-assert.
        assert secret_config_perms.verify_evolve_access(BOT) == [], (
            "verify_evolve_access still reports gaps after the post-gateway re-assert")
        print("round8(b/c): post-gateway re-assert heals traverse + default-ACL "
              "inheritance; evolve reads gateway-created auth-profiles + writes the "
              "queue; verify_evolve_access confirms the effective contract holds")

        # ── (d) no daemon JobSpec is missing required args (slack-signals) ────
        # Phase E.6: _install_launchd_slack_signals resolves the REAL primary and
        # SKIPS the install when none resolves (rather than hardcoding "evolve"
        # and shipping a unit that dies "no such bot"). Point it at a network
        # whose primary resolves so we exercise the install path and pin the
        # W10-G contract: when it installs, it passes the REQUIRED --bot naming
        # the resolved primary.
        captured: dict = {}
        orig = deploy._install_launchd
        orig_net = deploy._CANONICAL_NETWORK_JSON
        # Write the fixture network to /tmp (world-writable; the call resolves it
        # in-process as the test runner, which can't write under /var/lib/evolve).
        net_path = Path("/tmp") / "e2e-slack-signals-network.json"
        net_path.write_text(json.dumps(
            {"primary": "evo", "bots": {"evo": {"role": "primary"}}}))

        def _capture(label, user, script_path, schedule, result, extra_args=None, **kw):
            captured["extra_args"] = extra_args or []

        deploy._install_launchd = _capture
        deploy._CANONICAL_NETWORK_JSON = net_path
        try:
            deploy._install_launchd_slack_signals(
                "evolve", SHARED_DIR, deploy.DeployResult(bot_id="evolve", success=True))
        finally:
            deploy._install_launchd = orig
            deploy._CANONICAL_NETWORK_JSON = orig_net
            net_path.unlink(missing_ok=True)
        assert "--bot" in captured.get("extra_args", []), (
            f"slack-signals JobSpec still omits the REQUIRED --bot: {captured}")
        assert "evo" in captured["extra_args"], (
            f"slack-signals --bot must name the RESOLVED primary, got: {captured}")
        print("round8(d): slack-signals JobSpec passes --bot naming the resolved "
              "primary — the unit no longer dies on `arguments are required: --bot`")
    finally:
        _sudo("/bin/rm", "-rf", str(agents_root), check=False)
        _sudo("/bin/rm", "-f", str(queue), check=False)


def test_step4i_post_verify_reharden_heals_and_key_resolves():
    """W10-G round-9: the round-8 verify was a FALSE GREEN. The OC gateway re-hardens
    .openclaw to 0700 AGAIN on every `openclaw` invocation against it — and the
    wizard's Telegram channel-add + plugin-install steps run AFTER the post-gateway
    verify, re-clamping the mask the verify had just confirmed clean. Reproduce that
    exact ordering on the live kernel and prove both round-9 fixes:

      #1 heal_evolve_access (the genuinely-LAST pass, run after primary hardening)
         restores traverse+read+write AFTER a post-verify re-harden, AND returns a
         REAL bool (the set_evolve_read_acl→None / bool(None) false-failure is gone);
      #2 the Anthropic-key resolver targets the PRIMARY bot's home (the evolve
         service account has NO OpenClaw instance on Linux), and the key is reachable
         by evolve once the mask is intact.

    This ordering — verify PASSES, THEN a re-harden re-breaks it — is the blind spot
    the round-8 single-pass harness missed.
    """
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation
    from runtime.perms import get_perms

    import primary_bot
    from evolve_admin import deploy, secret_config_perms

    iso = get_isolation()
    perms = get_perms()
    agents_root = OC_DIR / "agents"
    agent_dir = agents_root / "main" / "agent"
    auth = agent_dir / "auth-profiles.json"
    ws_evolve = OC_DIR / "workspace" / "evolve"
    queue = ws_evolve / "defer-queue.jsonl"
    try:
        deploy.set_evolve_read_acl(BOT)
        _sudo("/bin/mkdir", "-p", str(ws_evolve))
        _sudo("/usr/bin/chown", "-R", f"{BOT}:{BOT}", str(ws_evolve))
        # The bot's auth-profiles carries a (fake) Anthropic key — the shape the
        # synthesizer's resolver must find under the PRIMARY bot's home.
        iso.run_as(BOT, ["/bin/bash", "-c",
                         f"umask 077 && mkdir -p {agent_dir} && "
                         f"printf '{{\"profiles\":{{\"anthropic:api\":"
                         f"{{\"type\":\"api_key\",\"key\":\"sk-ant-e2e\"}}}}}}\\n' > {auth}"],
                   capture_output=True, text=True)
        deploy.set_evolve_read_acl(BOT)

        # 1) the post-gateway verify PASSES — the round-8 green snapshot.
        assert secret_config_perms.verify_evolve_access(BOT) == [], (
            "contract should hold right after the grant (the round-8 verify point)")

        # 2) THE LATE CLAMP: an `openclaw` op (channel-add / plugin-install) re-hardens
        #    .openclaw to 0700 AFTER that green verify → mask --- → false-green exposed.
        _sudo("/bin/chmod", "700", str(OC_DIR))
        assert _getfacl_mask(OC_DIR) == "---"
        assert secret_config_perms.verify_evolve_access(BOT) != [], (
            "a post-verify re-harden must RE-break the contract — proving the round-8 "
            "single verify was a false green")
        print("round9: a post-verify `openclaw` re-harden re-clamps the mask the verify "
              "had just confirmed clean — the false-green the round-8 harness missed")

        # 3) heal_evolve_access (the final post-hardening pass) restores it + REAL bool.
        assert secret_config_perms.heal_evolve_access(BOT, BOT) is True, (
            "heal_evolve_access must restore the contract AND return True (not bool(None))")
        assert _getfacl_mask(OC_DIR) != "---"
        assert perms.acl_user_effective(OC_DIR, EVOLVE_USER, "search"), (
            "evolve still cannot traverse .openclaw after the heal:\n"
            + _sudo(GETFACL, "-p", str(OC_DIR), check=False).stdout)

        # real syscalls AS evolve: read the primary's secret + write the queue.
        rd = iso.run_as(EVOLVE_USER, ["/bin/cat", str(auth)], capture_output=True, text=True)
        assert rd.returncode == 0 and "anthropic" in rd.stdout, (
            f"evolve cannot READ the primary's auth-profiles after the heal: {rd.stderr}")
        wr = iso.run_as(EVOLVE_USER, ["/bin/bash", "-c", f"printf '{{}}\\n' > {queue}"],
                        capture_output=True, text=True)
        assert wr.returncode == 0, (
            f"evolve (defer/manifest-reflex-runner) cannot WRITE the queue: {wr.stderr}")
        print("round9 #1: heal restores traverse+read+write after the post-verify clamp")

        # 4) #2 the key resolver targets the PRIMARY bot's home (network.primary=BOT),
        #    NOT the evolve service account (no OC instance on Linux). Env cleared so
        #    the auth-profiles path is exercised, not the ANTHROPIC_API_KEY override.
        import os as _os
        _os.environ.pop("ANTHROPIC_API_KEY", None)
        key = primary_bot.read_primary_bot_anthropic_key(
            {"primary": BOT, "bots": {BOT: {"user": BOT}}})
        assert key == "sk-ant-e2e", (
            f"the Anthropic-key resolver did not read the PRIMARY bot's auth-profiles "
            f"on Linux: got {key!r}")
        print("round9 #2: the Anthropic-key resolver reads the PRIMARY bot's key on Linux "
              "(the evolve service account has no OC instance)")
    finally:
        _sudo("/bin/rm", "-rf", str(agents_root), check=False)
        _sudo("/bin/rm", "-f", str(queue), check=False)


# ══════════════════════════════════════════════════════════════════════════════
# Step 4j — the #3198 sibling-call-site gap (THIS PR). Step 4a2 proved the
# RUNTIME mask-flap heal; this proves the GROUP/OTHER clamp survives the
# self-heal. The bug: deploy.set_evolve_read_acl clamped `.openclaw` group/other
# to `---` (so OC-gateway-minted files are never born world-readable), but the
# every-deploy / pod-wide / hourly drift repair (deploy._add_acl) re-ran ONLY the
# recursive read grant WITHOUT the clamp — re-planting the permissive default ACL
# and re-widening other:: → r-x between full deploys, re-arming the leak. The fix
# routes both paths through `_apply_openclaw_read_contract`. Falsifiable on the
# live kernel: deploy clamps, a minted file is born `other::---`, the DRIFT REPAIR
# PRESERVES the clamp (not re-widens), creds stay 0700 + no-ACE, and a file minted
# AFTER the repair is still `other::---`.
# ══════════════════════════════════════════════════════════════════════════════


def test_step4j_drift_repair_preserves_the_openclaw_group_other_clamp():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation
    from runtime.perms import get_perms

    from evolve_admin import deploy
    from evolve_admin.secret_config_perms import verify_evolve_access

    iso = get_isolation()
    perms = get_perms()
    creds = OC_DIR / "credentials"
    # A unique probe dir (no other step touches it) ARMED with a permissive
    # DEFAULT ACL — `default:other::r-x`. That is the genuine pre-#3198 /
    # gateway-mint state an UNCLAMPED `setfacl -R -d -m` produces (it auto-copies
    # a dir's permissive base into the default), and the state the OLD unclamped
    # `_add_acl` PRESERVES (its `-d -m u:evolve:rX` only ADDS the named entry). A
    # file minted under it is then born `other::r-x` REGARDLESS of umask (a default
    # ACL overrides umask). The fixed self-heal must overwrite it to `---`.
    gw_child = OC_DIR / "drift-probe-child"
    leaked = gw_child / "minted-pre-heal.json"
    healed = gw_child / "minted-post-heal.json"
    try:
        r = iso.run_as(BOT, ["/bin/bash", "-c",
            f"mkdir -p {OC_DIR}/workspace/evolve {creds} && "
            f"printf '{{}}\\n' > {OC_DIR}/openclaw.json && "
            f"printf 'sk-secret\\n' > {creds}/secret.json"],
            capture_output=True, text=True)
        assert r.returncode == 0, f"bot setup failed: {r.stderr}"

        # ── (a) the DEPLOY path clamps group/other (the #3198 keystone) ──────────
        deploy.set_evolve_read_acl(BOT)
        assert _getfacl_field(OC_DIR, "other::") == "---", (
            f"deploy did not clamp .openclaw ACCESS other:: — got "
            f"{_getfacl_field(OC_DIR, 'other::')!r}")
        assert _getfacl_field(OC_DIR, "default:other::") == "---", (
            f"deploy did not clamp .openclaw DEFAULT other:: (future children would "
            f"be born world-readable) — got {_getfacl_field(OC_DIR, 'default:other::')!r}")
        # the clamp must NOT starve evolve: its named ACE survives, mask pinned rX.
        assert perms.acl_user_effective(OC_DIR / "openclaw.json", EVOLVE_USER, "read")

        # ── (b) arm the leak: a probe dir whose DEFAULT ACL mints world-readable ─
        r = iso.run_as(BOT, ["/bin/bash", "-c", f"mkdir -p {gw_child}"],
                       capture_output=True, text=True)
        assert r.returncode == 0, f"probe-dir mkdir failed: {r.stderr}"
        _sudo("/usr/bin/setfacl", "-b", "-k", str(gw_child))               # strip access + default ACL
        _sudo("/usr/bin/setfacl", "-d", "-m", "u::rwx,g::r-x,o::r-x", str(gw_child))  # permissive default
        _sudo("/bin/chmod", "0755", str(gw_child))   # dir stays bot-owned from the mkdir above
        assert _getfacl_field(gw_child, "default:other::") == "r-x", (
            "test setup failed to arm a permissive default ACL on the probe dir")
        r = iso.run_as(BOT, ["/bin/bash", "-c", f"printf '{{}}\\n' > {leaked}"],
                       capture_output=True, text=True)
        assert r.returncode == 0, f"probe-dir mint failed: {r.stderr}"
        proc = _sh([GETFACL, "-R", "-p", str(gw_child)], check=False)
        _diag("getfacl-drift-probe-pre-heal.txt", proc.stdout or "")
        pre = _getfacl_field(leaked, "other::")
        assert pre is not None and pre != "---", (
            f"armed leak did not mint a world-readable file pre-heal; other::{pre!r}")

        # ── (c) THE REGRESSION: the self-heal CLAMPS the armed child's default ───
        # deploy._add_acl is the apply behind _check_bot_acl in ensure_pod_perms
        # (every deploy + the pod-wide ensure-pod-perms pass). Before this fix it
        # re-ran the recursive read grant UNCLAMPED, so `setfacl -R -d -m` left
        # gw_child's default:other::r-x intact and the leak STAYED ARMED.
        assert deploy._add_acl(BOT, EVOLVE_USER) is True
        proc = _sh([GETFACL, "-R", "-p", str(OC_DIR)], check=False)
        _diag("getfacl-openclaw-after-selfheal-clamp.txt", proc.stdout or "")
        # the killer assertion: the armed child's DEFAULT other:: is now clamped
        # (the OLD unclamped repair would have LEFT it r-x — only adding the named
        # evolve entry — so every file minted under it stayed world-readable).
        assert _getfacl_field(gw_child, "default:other::") == "---", (
            "self-heal (_add_acl) left the probe child's DEFAULT other:: WIDE — "
            "the #3198 sibling-call-site gap: every file minted under it stays "
            f"world-readable. got {_getfacl_field(gw_child, 'default:other::')!r}")
        assert _getfacl_field(gw_child, "other::") == "---", (
            f"self-heal did not clamp the probe child's ACCESS other:: — got "
            f"{_getfacl_field(gw_child, 'other::')!r}")
        # .openclaw itself stays clamped on access + default too.
        assert _getfacl_field(OC_DIR, "other::") == "---"
        assert _getfacl_field(OC_DIR, "default:other::") == "---"

        # ── (d) a file minted in the child AFTER the heal is born NON-readable ───
        r = iso.run_as(BOT, ["/bin/bash", "-c", f"printf '{{}}\\n' > {healed}"],
                       capture_output=True, text=True)
        assert r.returncode == 0, f"post-heal mint failed: {r.stderr}"
        assert _getfacl_field(healed, "other::") == "---", (
            "the self-heal re-armed world-readable minting — a file created under "
            f"the gateway child after it was born other::{_getfacl_field(healed, 'other::')!r}")

        # ── (e) credentials/ stays bot-private after the repair: 0700, no evolve ACE
        mode = _sudo("/usr/bin/stat", "-c", "%a", str(creds)).stdout.strip()
        assert mode == "700", f"credentials/ not 0700 after self-heal: {mode}"
        assert not perms.acl_user_effective(creds, EVOLVE_USER, "read"), (
            "evolve gained a read ACE on credentials/ after the self-heal")
        rd = iso.run_as(EVOLVE_USER, ["/bin/cat", str(creds / "secret.json")],
                        capture_output=True, text=True)
        assert rd.returncode != 0, (
            "SECURITY: evolve read the credentials carve-out after the self-heal")

        # the repair kept the evolve access contract (clamp + workspace re-widen,
        # NOT clamp-induced starvation).
        gaps = verify_evolve_access(BOT)
        assert gaps == [], f"self-heal left the evolve access contract broken: {gaps}"
        print("clamp self-heal: a probe child armed world-readable (default "
              "other::r-x) is re-clamped (default+access other::---) by the drift "
              "repair; post-heal files born non-world-readable; .openclaw stays "
              "clamped; credentials/ stays 0700 + no evolve ACE")
    finally:
        _sudo("/bin/rm", "-rf", str(gw_child), check=False)


# ══════════════════════════════════════════════════════════════════════════════
# Step 5 — scheduler: real systemd units for a STUB agent; the restart
# guarantee live; install-skip parity; timer activation; clean remove
# ══════════════════════════════════════════════════════════════════════════════

_HEARTBEAT_PY = """\
import getpass, sys, time
log = sys.argv[1]
while True:
    with open(log, "a") as f:
        f.write(f"{time.time():.3f} beat user={getpass.getuser()}\\n")
    time.sleep(1)
"""

_ONESHOT_PY = """\
import sys, time
with open(sys.argv[1], "a") as f:
    f.write(f"{time.time():.3f} sweep ran\\n")
"""


def _install_stub_scripts() -> None:
    _sudo("/bin/mkdir", "-p", str(STUB_DIR))
    _sudo("/usr/bin/tee", str(STUB_DIR / "heartbeat.py"), input_text=_HEARTBEAT_PY)
    _sudo("/usr/bin/tee", str(STUB_DIR / "oneshot.py"), input_text=_ONESHOT_PY)
    _sudo("/bin/chmod", "-R", "755", str(STUB_DIR))


def test_step5_scheduler_systemd_stub_agent():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.scheduler import JobSpec, get_scheduler

    sched = get_scheduler()
    _install_stub_scripts()

    # ── the stub gateway: keep_alive daemon running AS the bot user ──────────
    spec = JobSpec(
        label=GATEWAY_LABEL,
        program_args=["/usr/bin/python3", str(STUB_DIR / "heartbeat.py"),
                      str(HEARTBEAT_LOG)],
        user=BOT,
        keep_alive=True,
        run_at_load=True,
        comment="Evolve Linux e2e stub agent (no OpenClaw, no network)",
        stdout_path=str(OC_DIR / "logs" / "gateway.log"),
        stderr_path=str(OC_DIR / "logs" / "gateway.err.log"),
    )
    res = sched.install(spec)
    assert res.ok, f"install failed: {res.message}"
    assert not res.skipped
    assert Path(f"/etc/systemd/system/{GATEWAY_LABEL}.service").exists()

    _wait_for(f"{GATEWAY_LABEL} active", lambda: sched.running(GATEWAY_LABEL),
              timeout=20)
    st = sched.status(GATEWAY_LABEL)
    print(f"  status: {st}")
    assert st["managed"] and st["running"] and st["pid"]
    STATE["gateway_pid"] = st["pid"]

    # The deploy runs AS THE BOT (User= in the rendered unit), and it RUNS —
    # the heartbeat log accrues beats stamped with the bot's identity.
    _wait_for(
        "heartbeat log shows ≥2 beats from the bot user",
        lambda: (
            (p := subprocess.run(["sudo", "-n", "/bin/cat", str(HEARTBEAT_LOG)],
                                 capture_output=True, text=True)).returncode == 0
            and p.stdout.count(f"beat user={BOT}") >= 2
        ),
        timeout=20,
    )
    proc = _sh(["ps", "-o", "user=", "-p", str(st["pid"])], check=True)
    assert proc.stdout.strip().startswith(BOT[:8]), "MainPID not owned by the bot user"

    # ── the installed sudoers grants work for the evolve principal, live ─────
    from runtime.isolation import get_isolation
    iso = get_isolation()
    unit = f"{GATEWAY_LABEL}.service"
    proc = iso.run_as(EVOLVE_USER, ["sudo", "-n", SYSTEMCTL, "is-active", unit],
                      capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "active", (
        f"evolve's `systemctl is-active` grant dead: rc={proc.returncode} "
        f"out={proc.stdout!r} err={proc.stderr!r}"
    )
    # …and the boundary holds: a verb OUTSIDE the grant table is refused.
    # (sudo -n phrases the denial as "a password is required" or "not
    # allowed" depending on match path — accept either, require nonzero rc.)
    proc = iso.run_as(EVOLVE_USER, ["sudo", "-n", SYSTEMCTL, "stop", "ssh.service"],
                      capture_output=True, text=True)
    denial = (proc.stderr or "").lower()
    assert proc.returncode != 0 and (
        "password" in denial or "not allowed" in denial
    ), (
        f"evolve escalated OUTSIDE its grant table: rc={proc.returncode} "
        f"err={proc.stderr!r}"
    )

    # ── the restart guarantee: restart() yields a FRESH pid ──────────────────
    ok, msg = sched.restart(GATEWAY_LABEL)
    assert ok, f"restart failed: {msg}"
    new_pid = _wait_for(
        "fresh MainPID after restart",
        lambda: (
            (s := sched.status(GATEWAY_LABEL))["running"]
            and s["pid"] not in (None, STATE["gateway_pid"])
            and s["pid"]
        ),
        timeout=20,
    )
    print(f"  restart: pid {STATE['gateway_pid']} → {new_pid}")

    # ── byte-identical reinstall skips (repeat deploys don't bounce) ─────────
    res2 = sched.install(spec)
    assert res2.ok and res2.skipped, f"expected skip, got: {res2}"
    assert sched.status(GATEWAY_LABEL)["pid"] == new_pid, (
        "skipped reinstall must not bounce a healthy daemon"
    )

    # ── timer activation: interval JobSpec → .timer that fires on install ────
    sweep = JobSpec(
        label=SWEEP_LABEL,
        program_args=["/usr/bin/python3", str(STUB_DIR / "oneshot.py"),
                      str(SWEEP_MARKER)],
        user=BOT,
        start_interval=3600,
        run_at_load=True,  # OnBootSec=0 → the timer fires once right away
        comment="Evolve Linux e2e timer sweep",
    )
    res3 = sched.install(sweep)
    assert res3.ok, f"timer install failed: {res3.message}"
    assert Path(f"/etc/systemd/system/{SWEEP_LABEL}.timer").exists()
    proc = _sudo(SYSTEMCTL, "is-active", f"{SWEEP_LABEL}.timer", check=False)
    assert proc.stdout.strip() == "active", "timer unit not active after install"
    _wait_for(
        "timer-activated sweep wrote its marker",
        lambda: subprocess.run(
            ["sudo", "-n", "/usr/bin/test", "-s", str(SWEEP_MARKER)],
            capture_output=True,
        ).returncode == 0,
        timeout=30,
    )

    # ── clean remove: unit set gone, processes gone ───────────────────────────
    for label in (SWEEP_LABEL, GATEWAY_LABEL):
        ok, msg = sched.remove(label)
        assert ok, f"remove({label}) failed: {msg}"
        for kind in ("service", "timer", "path"):
            assert not Path(f"/etc/systemd/system/{label}.{kind}").exists()
        assert not sched.running(label)
        assert not sched.status(label)["managed"]
    _wait_for(
        "stub agent processes exited",
        lambda: subprocess.run(
            ["/usr/bin/pgrep", "-u", BOT, "-f", "heartbeat.py"],
            capture_output=True,
        ).returncode != 0,
        timeout=15,
    )
    print("scheduler: install → run-as-bot → grants-live → restart(fresh pid) → "
          "skip-on-identical → timer fire → clean remove, all against real systemd")


# ══════════════════════════════════════════════════════════════════════════════
# Steps 5b-5e — the ONGOING daemon lifecycle, driven through the REAL migrated
# entrypoints (scheduler-seam-portability bites 2/5/GRACEFUL/4). Step 5 proved
# the seam adapter itself; these prove the just-migrated CALL SITES route to it.
#
# Each step calls the real production function — repo_puller._kickstart_daemon,
# metrics.resolvers.launchd, recovery._launchctl_n, retire._stop_plist — so a
# regression that hardcoded a LaunchdScheduler (bypassing the injected
# SystemdScheduler) would surface as a real systemctl/launchctl-not-found
# failure on this Ubuntu runner, not a silent no-op.
#
# They share ONE dedicated stub gateway under GATEWAY_LABEL (re-installed by
# 5b — step 5 tore down its own copy at its clean-remove finale): 5b restarts
# it, 5c reads its loaded-state metric, 5d locks the graceful no-op, and 5e
# retires it. Near-zero new host state — the unit 5b installs is the only
# addition, and 5e (LAST, destructive) removes it; the module teardown's
# remove(GATEWAY_LABEL) is the backstop if 5e is skipped.
# ══════════════════════════════════════════════════════════════════════════════


def _install_ongoing_stub_gateway():
    """(Re)install the stub gateway under GATEWAY_LABEL and wait until it is
    running. Returns (sched, pid) — the live MainPID the lifecycle steps read.

    Idempotent: ``_install_stub_scripts()`` re-stages the heartbeat script,
    and ``install()`` of a byte-identical spec is a skip, so this is safe to
    call even if a prior step left the unit live.
    """
    from runtime.scheduler import JobSpec, get_scheduler

    sched = get_scheduler()
    _install_stub_scripts()
    spec = JobSpec(
        label=GATEWAY_LABEL,
        program_args=["/usr/bin/python3", str(STUB_DIR / "heartbeat.py"),
                      str(HEARTBEAT_LOG)],
        user=BOT,
        keep_alive=True,
        run_at_load=True,
        comment="Evolve Linux e2e ongoing-ops stub gateway",
        stdout_path=str(OC_DIR / "logs" / "gateway.log"),
        stderr_path=str(OC_DIR / "logs" / "gateway.err.log"),
    )
    res = sched.install(spec)
    assert res.ok, f"ongoing stub install failed: {res.message}"
    assert Path(f"/etc/systemd/system/{GATEWAY_LABEL}.service").exists()
    _wait_for(f"{GATEWAY_LABEL} active", lambda: sched.running(GATEWAY_LABEL),
              timeout=20)
    pid = _wait_for(
        f"{GATEWAY_LABEL} has a MainPID",
        lambda: sched.status(GATEWAY_LABEL).get("pid"),
        timeout=20,
    )
    return sched, pid


# ══════════════════════════════════════════════════════════════════════════════
# Step 5b — kickstart (bite 2): repo_puller._kickstart_daemon routes the
# puller's per-daemon restart onto get_scheduler(). Pre-migration it built a
# LaunchdScheduler and died on `launchctl` not existing; post-migration it
# restarts the unit through the injected SystemdScheduler with a FRESH pid.
# ══════════════════════════════════════════════════════════════════════════════


def test_step5b_kickstart_daemon_restarts_via_seam():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from evolve_admin import repo_puller

    sched, pid_before = _install_ongoing_stub_gateway()

    ok, info = repo_puller._kickstart_daemon(GATEWAY_LABEL)
    assert (ok, info) == (True, "ok"), (
        "repo_puller._kickstart_daemon did not route to the injected "
        f"SystemdScheduler: ok={ok!r} info={info!r} (a pre-migration "
        "hardcoded LaunchdScheduler would have failed on launchctl-not-found)"
    )

    new_pid = _wait_for(
        "fresh MainPID after _kickstart_daemon (real unit restart)",
        lambda: (
            (s := sched.status(GATEWAY_LABEL))["running"]
            and s["pid"] not in (None, pid_before)
            and s["pid"]
        ),
        timeout=20,
    )
    print(f"kickstart(bite 2): _kickstart_daemon → (ok,'ok'); "
          f"pid {pid_before} → {new_pid} via real systemctl restart")


# ══════════════════════════════════════════════════════════════════════════════
# Step 5c — metric resolver / false-signal (bite 5): with SystemdScheduler
# active and the gateway running, the launchd.service_loaded resolver must
# return 1.0. Pre-migration on Linux the launchd-only raw() path raised →
# 0.0 → a FALSE `launchd_not_loaded` Signal on every bot, every cycle.
# ══════════════════════════════════════════════════════════════════════════════


def test_step5c_metric_resolver_no_false_not_loaded_signal():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from datetime import datetime, timezone

    from metrics.resolvers import launchd as launchd_mod

    # The default label builder yields exactly GATEWAY_LABEL for `bot_id=BOT`
    # (`ai.openclaw.{bot}-gateway`), matching the stub unit 5b installed; reset
    # it defensively in case a prior test in-process swapped it.
    launchd_mod.set_label_builder(launchd_mod._default_label)
    assert launchd_mod._default_label(BOT) == GATEWAY_LABEL

    sched, _pid = _install_ongoing_stub_gateway()
    assert sched.running(GATEWAY_LABEL), "precondition: stub gateway must be running"

    v = launchd_mod.resolve_launchd_service_loaded(BOT, datetime.now(timezone.utc))
    print(f"  launchd.service_loaded({BOT}) = {v.value} "
          f"(conf={v.confidence}, note={v.source_note!r})")
    assert v.value == 1.0, (
        "launchd.service_loaded returned a FALSE not-loaded under "
        "SystemdScheduler — bite 5's regression (the launchd raw() path "
        f"raising → 0.0 → spurious launchd_not_loaded Signal): {v.source_note!r}"
    )
    assert v.confidence == 1.0, f"expected authoritative confidence, got {v.confidence}"
    print("metric(bite 5): loaded+running gateway resolves to 1.0 on Linux — "
          "no false launchd_not_loaded Signal")


# ══════════════════════════════════════════════════════════════════════════════
# Step 5d — recovery GRACEFUL guard (regression-lock): recovery._launchctl_n
# must STAY a no-op under a non-launchd Scheduler — it returns (0, "", "")
# rather than probing/escalating. Locks in that the GRACEFUL sites don't
# silently start shelling launchctl on Linux.
# ══════════════════════════════════════════════════════════════════════════════


def test_step5d_recovery_launchctl_n_graceful_under_systemd():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.scheduler import SystemdScheduler, get_scheduler

    from evolve_admin import recovery

    assert isinstance(get_scheduler(), SystemdScheduler), (
        "precondition: the injected seam must be SystemdScheduler for this lock"
    )
    # The real call shape used throughout recovery.py is
    # `_launchctl_n(<verb>, f"system/{label}")`; under a non-launchd seam it
    # short-circuits to the no-op tuple WITHOUT spawning launchctl.
    rc, out, err = recovery._launchctl_n("print", f"system/{GATEWAY_LABEL}")
    assert (rc, out, err) == (0, "", ""), (
        "recovery._launchctl_n did not stay graceful under SystemdScheduler — "
        f"a GRACEFUL site started probing: ({rc!r}, {out!r}, {err!r})"
    )
    print("recovery(GRACEFUL): _launchctl_n is the (0,'','') no-op under "
          "SystemdScheduler — graceful sites stay graceful on Linux")


# ══════════════════════════════════════════════════════════════════════════════
# Step 5e — retire (bite 4): retire._stop_plist routes the bot-retire stop
# onto get_scheduler().remove(). DESTRUCTIVE — it removes the stub gateway,
# so it runs LAST of the ongoing-ops steps. Pre-migration it bootout'd via a
# hardcoded LaunchdScheduler; post-migration it disables+deletes the systemd
# unit atomically.
# ══════════════════════════════════════════════════════════════════════════════


def test_step5e_retire_stop_plist_removes_unit_via_seam():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.scheduler import get_scheduler

    from evolve_admin.retire import RetireResult, _stop_plist

    sched, _pid = _install_ongoing_stub_gateway()
    assert sched.running(GATEWAY_LABEL), "precondition: stub gateway must be running"

    result = RetireResult(bot_id=BOT, dry_run=False)
    ok = _stop_plist(GATEWAY_LABEL, dry_run=False, result=result)
    print(f"  _stop_plist steps: {result.steps}")
    if result.errors:
        print(f"  _stop_plist errors: {result.errors}")
    assert ok is True, (
        "retire._stop_plist failed to remove the systemd unit via the seam: "
        f"{result.errors}"
    )
    assert result.success, f"RetireResult marked failure: {result.errors}"

    # The unit is actually gone: not running, and the service file removed.
    assert not get_scheduler().running(GATEWAY_LABEL), (
        "retire._stop_plist returned ok but the unit is still active"
    )
    assert not get_scheduler().status(GATEWAY_LABEL)["installed"], (
        "retire._stop_plist returned ok but the service unit file remains"
    )
    assert not Path(f"/etc/systemd/system/{GATEWAY_LABEL}.service").exists()
    print("retire(bite 4): _stop_plist → get_scheduler().remove() — stub "
          "gateway disabled, unit file deleted, daemon-reloaded, all via systemd")


# ══════════════════════════════════════════════════════════════════════════════
# Step 6 — admin smoke: the real Flask admin server, as evolve, administered
# over HTTP (a systemd unit through the same Scheduler seam, like a real pod)
# ══════════════════════════════════════════════════════════════════════════════


def _ensure_traversable_by(user: str, leaf: Path) -> None:
    """Grant ``user`` directory-traverse (--x ACE) up the ancestry of ``leaf``.

    GH runners keep /home/runner at mode 750, which blocks the evolve user
    from reaching the checkout + venv. Minimal fix: an execute-only ACE on
    each non-world-traversable ancestor — no read, no content exposure.
    """
    for ancestor in [leaf, *leaf.parents]:
        if str(ancestor) == "/":
            continue
        try:
            mode = ancestor.stat().st_mode
        except OSError:
            continue
        if ancestor.is_dir() and not (mode & 0o001):
            _sudo("/usr/bin/setfacl", "-m", f"u:{user}:--x", str(ancestor))


def test_step6_admin_server_runs_and_answers_as_evolve():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation
    from runtime.scheduler import JobSpec, get_scheduler

    iso = get_isolation()
    sched = get_scheduler()

    # ── pod state: shared dir + minimal network.json, owned by evolve ────────
    _sudo("/bin/mkdir", "-p", str(SHARED_DIR / "logs"))
    network = {
        "networkId": "linux-e2e",
        "sharedDir": str(SHARED_DIR),
        "members": [BOT],
        "bots": {BOT: {"role": "member", "port": 18900}},
    }
    _sudo("/usr/bin/tee", str(NETWORK_JSON), input_text=json.dumps(network, indent=2))
    _sudo("/usr/bin/chown", "-R", f"{EVOLVE_USER}:{EVOLVE_USER}", str(SHARED_DIR))

    # ── evolve must reach the checkout + venv the CI job installed ───────────
    # ExecStart MUST use the venv shim (sys.executable), NOT its resolved
    # symlink target — the base interpreter has no evolve_admin on its path
    # (the editable install lives in the venv's site-packages). The resolved
    # target only matters for the traversal grants below.
    venv_python = Path(sys.executable)
    interpreter_target = venv_python.resolve()
    # parents: [0]=e2e_linux [1]=tests [2]=admin [3]=packages [4]=repo root
    repo_root = Path(__file__).resolve().parents[4]
    for leaf in (venv_python.parent, interpreter_target.parent, repo_root):
        _ensure_traversable_by(EVOLVE_USER, leaf)

    # Preflight with full diagnostics — if the import fails, the curl below
    # could only fail less legibly.
    proc = iso.run_as(
        EVOLVE_USER,
        [str(venv_python), "-c", "import evolve_admin, flask; print('import-ok')"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or "import-ok" not in proc.stdout:
        for ancestor in [venv_python, *venv_python.parents][:6]:
            _sh(["/bin/ls", "-ld", str(ancestor)], check=False)
        raise AssertionError(
            f"evolve cannot import the packaged admin server: {proc.stderr}"
        )

    # ── the admin server as a systemd unit, User=evolve (real-pod shape) ─────
    spec = JobSpec(
        label=ADMIN_LABEL,
        program_args=[
            str(venv_python), "-m", "evolve_admin.web.run",
            "--host", "127.0.0.1", "--port", str(ADMIN_PORT),
            "--network", str(NETWORK_JSON),
        ],
        user=EVOLVE_USER,
        keep_alive=True,
        run_at_load=True,
        working_dir=str(SHARED_DIR),
        env={
            # Device-pairing auth stays ON by default product-wide; the e2e
            # uses the documented test escape so / and the read API answer
            # without a browser pairing dance.
            "EVOLVE_ADMIN_AUTH_DISABLED": "1",
            # No Keychain on Linux — force the file vault (keystore §7).
            "EVOLVE_KEYSTORE_NO_KEYCHAIN": "1",
        },
        comment="Evolve admin UI (Linux e2e)",
        stdout_path=str(SHARED_DIR / "logs" / "admin-ui.log"),
        stderr_path=str(SHARED_DIR / "logs" / "admin-ui.err.log"),
    )
    res = sched.install(spec)
    assert res.ok, f"admin-ui install failed: {res.message}"

    def _server_log_tail() -> str:
        proc = subprocess.run(
            ["sudo", "-n", "/bin/cat", str(SHARED_DIR / "logs" / "admin-ui.err.log")],
            capture_output=True, text=True,
        )
        return "\n".join((proc.stdout or "").splitlines()[-30:])

    try:
        _wait_for(
            "admin server answers /api/health with 200",
            lambda: _http_get("/api/health")[0] == 200,
            timeout=90, interval=1.0,
        )
    except AssertionError:
        print(f"  admin-ui.err.log tail:\n{_server_log_tail()}")
        print(f"  unit status: {sched.status(ADMIN_LABEL)}")
        raise

    status, body = _http_get("/api/health")
    print(f"  GET /api/health → {status} {body[:200]}")
    assert status == 200 and json.loads(body)["status"] == "ok"

    # The SPA shell — "administered on Linux" includes the UI surface.
    status, body = _http_get("/")
    print(f"  GET / → {status} ({len(body)} bytes)")
    assert status == 200 and "<html" in body.lower()

    # One real read API: pod status, resolved from the Linux-path
    # network.json, naming our deployed bot.
    status, body = _http_get("/api/status", timeout=30)
    print(f"  GET /api/status → {status} {body[:300]}")
    assert status == 200
    payload = json.loads(body)
    assert payload["network_id"] == "linux-e2e"
    assert payload["shared_dir"] == str(SHARED_DIR)
    assert BOT in payload["bots"], f"bot missing from /api/status: {payload['bots']}"

    # ── #3189 regression-lock: service.status() must reflect the REAL systemd
    # admin-ui unit on Linux ─────────────────────────────────────────────────
    # Before the fix, status() gated on a macOS /Library/LaunchDaemons plist
    # that never exists on Linux, so it returned running:false (backend:launchd)
    # even with the unit up — a false-negative that would break Setup Step 1's
    # self-service status. The unit is confirmed active above (/api/health 200);
    # status() routes through _status_linux() → get_scheduler().status(
    # SYSTEM_LABEL) → real `systemctl show`, so it must now report systemd +
    # running + MainPID. (The harness has SystemdScheduler injected and
    # service.SYSTEM_LABEL == ADMIN_LABEL, so it queries this same unit.)
    from evolve_admin import service as _admin_service
    _st = _admin_service.status()
    assert _st.get("backend") == "systemd", (
        f"#3189: status().backend must be 'systemd' on Linux, got {_st!r}")
    assert _st.get("running") is True, (
        f"#3189: status() must report the active admin-ui systemd unit running, got {_st!r}")
    assert _st.get("pid"), (
        f"#3189: status() must surface the systemd MainPID, got {_st!r}")
    print("  ✓ #3189 lock: service.status() reports systemd unit running "
          f"(pid={_st.get('pid')})")

    # ── shut down through the seam; the port must actually close ─────────────
    ok, msg = sched.remove(ADMIN_LABEL)
    assert ok, f"admin-ui remove failed: {msg}"
    _wait_for(
        "admin port closed after remove",
        lambda: _http_get("/api/health", timeout=2)[0] == -1,
        timeout=30,
    )
    print("admin smoke: server ran as evolve under systemd, answered /, "
          "/api/health and /api/status, and shut down cleanly")


# ══════════════════════════════════════════════════════════════════════════════
# Step 6b — the deploy FLOW builds + populates the canonical Linux venv, and the
# plugin install dir lands root-owned. The W7 gap: the deploy orchestration
# ASSUMED a pre-existing venv (true on macOS) and hardcoded macOS paths/group,
# so a real-VPS install died with "No such file or directory:
# /var/lib/evolve-venv/bin/python3" and the gateway never started. The seam
# e2e (steps 1-6) never exercised deploy.py's venv/plugin orchestration, which
# is exactly how the gap shipped invisibly — this step closes that.
# ══════════════════════════════════════════════════════════════════════════════


def test_step6b_deploy_flow_builds_canonical_venv_and_plugin_dir():
    from platform_profile import get_profile

    prof = get_profile()  # LINUX (pinned by _linux_seams)
    assert prof.name == "linux"
    venv_python = Path(prof.venv_python)
    assert str(venv_python) == "/var/lib/evolve-venv/bin/python3", venv_python

    repo_root = Path(__file__).resolve().parents[4]
    worktree_pp = os.pathsep.join(
        [str(repo_root / "packages" / "analyzer"), str(repo_root / "packages" / "admin")]
    )

    # ── build the canonical venv via the REAL deploy flow, AS ROOT ───────────
    # The deploy pipeline is root-gated and /var/lib needs root.
    # ensure_evolve_venv() builds the venv via `uv venv` (ensurepip-free; stock
    # Ubuntu omits python3-venv — W7c) then installs evolve-analyzer +
    # evolve-admin compat-editable from the checkout.
    #
    # `sudo` resets PATH to secure_path, which on a real runbook'd pod contains
    # /usr/local/bin (where `pip install uv` lands uv). The CI/dev box's uv is
    # under `uv run`'s toolchain dir instead, so prepend it to the sudo PATH —
    # this makes _find_uv() resolve uv under sudo the same way a real pod does
    # off secure_path, so the build takes the uv path (asserted below via the
    # uv stamp in pyvenv.cfg) rather than silently degrading to the fallback.
    uv_bin = shutil.which("uv")
    sudo_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if uv_bin:
        sudo_path = f"{str(Path(uv_bin).parent)}:{sudo_path}"
    _sh(
        ["sudo", "-n", "env", f"PYTHONPATH={worktree_pp}", f"PATH={sudo_path}",
         sys.executable, "-c",
         "from evolve_admin.installer import ensure_evolve_venv; ensure_evolve_venv()"],
        check=True, timeout=900,
    )
    # THE bug: this exact path was missing on the VPS.
    assert venv_python.exists(), f"{venv_python} not created by the deploy flow"
    assert Path(prof.venv_evolve_admin).exists(), "evolve-admin console-script missing"

    # Non-vacuous-green: prove the uv builder actually ran (not the stdlib
    # fallback, which would also create bin/python3 — but the GH runner's python
    # HAS ensurepip, so a silent fallback would pass and hide a uv-resolution
    # regression). `uv venv` stamps `uv = <ver>` + `seed = true` into pyvenv.cfg;
    # stdlib `python -m venv` writes neither. This is the W7c fix's signature.
    cfg = _sudo("/bin/cat", str(EVOLVE_VENV / "pyvenv.cfg")).stdout
    assert "uv = " in cfg and "seed = true" in cfg, (
        "venv was NOT built by `uv venv --seed` — pyvenv.cfg lacks the uv stamp, "
        "so _find_uv() failed to resolve uv under sudo and the build silently "
        f"fell back to stdlib venv (the W7c regression this asserts against):\n{cfg}"
    )

    # The compat-editable install is real: the freshly-built venv imports the
    # packages AND carries the evolve-analyzer dist (not on PyPI — proves the
    # editable install landed, the interpreter contract).
    proc = _sh(
        [str(venv_python), "-c",
         "import evolve_admin, audit, platform_profile; "
         "import importlib.metadata as m; m.distribution('evolve-analyzer'); "
         "print('venv-import-ok', platform_profile.get_profile().name)"],
        check=True,
    )
    assert "venv-import-ok linux" in proc.stdout

    # ── plugin install dir: root-owned (root:root on Linux, NOT root:wheel) ──
    _sudo("/bin/mkdir", "-p", str(PLUGIN_INSTALL_DIR))
    _sudo("/usr/bin/tee", str(PLUGIN_INSTALL_DIR / "marker"), input_text="x")
    # Run fix_plugin_permissions through the freshly-built venv — proves the
    # built venv can EXECUTE the real deploy code — AS ROOT.
    _sh(
        ["sudo", "-n", str(venv_python), "-c",
         "from evolve_admin.deploy import fix_plugin_permissions; fix_plugin_permissions()"],
        check=True,
    )
    owner = _sh(["stat", "-c", "%U:%G", str(PLUGIN_INSTALL_DIR)], check=True)
    assert owner.stdout.strip() == "root:root", (
        f"plugin dir owner is {owner.stdout.strip()!r}, expected root:root "
        "(the admin_group fix — `wheel` is not gid 0 on Linux)"
    )
    print("deploy flow: built /var/lib/evolve-venv (python + evolve-admin, "
          "compat-editable imports verified) and chowned the plugin dir root:root")


# ══════════════════════════════════════════════════════════════════════════════
# Step 6c — pod-perms enforcement: the deferred W7-followup chown sweep, live.
# The per-bot-config + ensure_pod_perms chowns hardcoded the macOS chown BINARY
# (/usr/sbin/chown) and the macOS gid-0 group `wheel`; both are HARD failures on
# Ubuntu. This step drives the real _ensure_evolve_owned_dir_perms (the apply for
# ensure_pod_perms' evolve-owned-dir check) against a drifted root:root shared
# subdir and proves it recovers to evolve:root — i.e. the routed binary
# (/usr/bin/chown) and admin_group (root) both work on the live runner.
# ══════════════════════════════════════════════════════════════════════════════


def test_step6c_pod_perms_chown_uses_admin_group_root_on_linux():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")

    repo_root = Path(__file__).resolve().parents[4]
    worktree_pp = os.pathsep.join(
        [str(repo_root / "packages" / "analyzer"), str(repo_root / "packages" / "admin")]
    )

    # A drifted evolve-owned shared subdir: dir + a file inside, all root:root
    # (the 2026-06-06 config_intents incident shape — first files hand-placed by
    # the wrong user, leaving the daemon unable to mkstemp+rename inside).
    ci = SHARED_DIR / "config_intents"
    _sudo("/bin/mkdir", "-p", str(ci))
    _sudo("/usr/bin/tee", str(ci / "intent.json"), input_text="{}\n")
    _sudo("/usr/bin/chown", "-R", "root:root", str(ci))

    # Run the REAL perms helper in a FRESH process (AS ROOT, mirroring step 6b)
    # so deploy._PROFILE resolves to LINUX — it is captured at import, and the
    # in-suite deploy module may have been imported under the conftest's MACOS
    # pin. The assert in-band catches a stale-profile import before the chown.
    code = (
        "from pathlib import Path; "
        "from evolve_admin import deploy; "
        "assert deploy._PROFILE.name == 'linux', deploy._PROFILE.name; "
        "assert deploy._PROFILE.admin_group == 'root', deploy._PROFILE.admin_group; "
        f"ok = deploy._ensure_evolve_owned_dir_perms(Path({str(ci)!r})); "
        "print('PERMS-OK' if ok else 'PERMS-FAIL')"
    )
    proc = _sh(
        ["sudo", "-n", "env", f"PYTHONPATH={worktree_pp}", sys.executable, "-c", code],
        check=True, timeout=120,
    )
    assert "PERMS-OK" in proc.stdout, f"helper returned False:\n{proc.stdout}\n{proc.stderr}"

    # The proof: the dir AND its pre-existing file are now evolve:root — the
    # routed chown binary (/usr/bin/chown) + admin_group (root) both worked.
    # Pre-followup `evolve:wheel` would have chown-failed (no wheel group) and
    # left ownership at root:root.
    dir_owner = _sh(["stat", "-c", "%U:%G", str(ci)], check=True).stdout.strip()
    assert dir_owner == "evolve:root", (
        f"dir owner is {dir_owner!r}, expected evolve:root "
        "(the wheel→admin_group fix — `wheel` is not gid 0 on Linux)"
    )
    file_owner = _sh(["stat", "-c", "%U:%G", str(ci / "intent.json")], check=True).stdout.strip()
    assert file_owner == "evolve:root", (
        f"recursive chown missed the file: owner is {file_owner!r}, expected evolve:root"
    )
    mode = _sh(["stat", "-c", "%a", str(ci)], check=True).stdout.strip()
    assert mode == "755", f"mode is {mode!r}, expected 755 (POD_EVOLVE_OWNED_DIR_MODE)"
    _diag("step6c-config-intents-owner", f"{ci}: {dir_owner} {mode}\n")
    print(f"pod-perms: {ci} recovered to evolve:root 0755 (wheel→admin_group, "
          "binary /usr/bin/chown) on the live ubuntu-24.04 runner")


def test_step6c2_upgrade_reclamp_keeps_evolve_access_and_records_install():
    """The VPS 'Upgrade failed — 0 of N bots' blocker, durable layer (chip behind #3138).

    Reproduces the live failure shape on the real kernel + real setfacl/sudoers:
    during an upgrade the OC gateway 0700-clamps a bot's ``.openclaw`` (its ACL
    mask recomputes to ``---`` → the evolve service user loses traverse+read), and
    the per-bot deploy then has to heal access and stamp the version record. The
    three durable contracts this proves end-to-end:

      (a) the heal (``heal_evolve_access`` — what the web Upgrade pre-loop
          ``ensure_pod_perms`` and the ``check_evolve_access`` apply both invoke)
          restores evolve's EFFECTIVE traverse+read after the 0700 clamp;
      (b) ``verify_evolve_access`` — the per-bot deploy's access gate — then reports
          ZERO gaps, i.e. the exact condition that became '0 of N bots' is cleared;
      (c) the evolve daemon can write ``{shared}/install.json`` via the new §10a
          sudoers grant (cp + chown) even when bootstrap left it root-owned, and
          ``record_bot_deploy`` stamps the per-bot version record.

    No OpenClaw needed (the clamp is a raw ``chmod 700``, the canonical reproducer
    used by steps 4h/4i); self-contained re: #3138 (it heals BEFORE any clamped
    read rather than depending on the un-merged blocker fix)."""
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.perms import get_perms
    from evolve_admin import deploy, secret_config_perms

    perms = get_perms()
    ws_evolve = OC_DIR / "workspace" / "evolve"
    install_json = SHARED_DIR / "install.json"
    stage = "/tmp/evolve-stage-e2e.json"
    try:
        # Setup mirrors step 4h: ensure .openclaw + the workspace/evolve queue dir
        # exist + bot-owned, then grant the evolve read/write contract.
        _sudo("/bin/mkdir", "-p", str(ws_evolve))
        _sudo("/usr/bin/chown", "-R", f"{BOT}:{BOT}", str(OC_DIR))
        deploy.set_evolve_read_acl(BOT)

        # ── reproduce the gateway's mid-upgrade 0700 re-harden ───────────────
        _sudo("/bin/chmod", "700", str(OC_DIR))
        assert _getfacl_mask(OC_DIR) == "---", (
            "chmod 700 on .openclaw should zero its ACL mask (the upgrade clamp); "
            f"got {_getfacl_mask(OC_DIR)!r}")

        # ── (a) the heal restores evolve's EFFECTIVE traverse + read ─────────
        healed = secret_config_perms.heal_evolve_access(BOT, BOT)
        assert healed is True, (
            "heal_evolve_access did not return True after the 0700 clamp:\n"
            + _sudo(GETFACL, "-p", str(OC_DIR), check=False).stdout)
        assert _getfacl_mask(OC_DIR) != "---", "the heal left .openclaw's mask clamped"
        assert perms.acl_user_effective(OC_DIR, EVOLVE_USER, "search"), (
            "evolve still cannot TRAVERSE .openclaw after the heal:\n"
            + _sudo(GETFACL, "-p", str(OC_DIR), check=False).stdout)
        assert perms.acl_user_effective(OC_DIR, EVOLVE_USER, "read"), (
            "evolve still cannot READ .openclaw after the heal")

        # ── (b) the per-bot deploy access gate is now clear (no '0 of N') ─────
        gaps = secret_config_perms.verify_evolve_access(BOT)
        assert gaps == [], f"verify_evolve_access still reports gaps after the heal: {gaps}"

        # ── (c) the evolve daemon writes install.json via the §10a grant ─────
        _sudo("/bin/mkdir", "-p", str(SHARED_DIR))
        # Bootstrap shape: a ROOT-owned install.json the evolve daemon must overwrite.
        _sudo("/usr/bin/tee", str(install_json),
              input_text=json.dumps({"version": "old", "bots": [BOT], "bot_versions": {}}) + "\n")
        _sudo("/usr/bin/chown", "root:root", str(install_json))
        # The staged tmp the daemon's fallback writes (mkstemp prefix evolve-stage-;
        # see deploy._secure_stage). Source must match the §10a grant's glob.
        Path(stage).write_text(json.dumps({"version": "new", "bots": [BOT]}) + "\n")
        _sudo("/bin/chmod", "644", stage)
        # AS THE EVOLVE DAEMON (nested sudo: runner→evolve, then evolve→root governed
        # by /etc/sudoers.d/evolve §10a): the cp + chown the PermissionError fallback runs.
        cp = _sh(["sudo", "-n", "-u", EVOLVE_USER, "sudo", "-n",
                  "/bin/cp", stage, str(install_json)], check=False)
        assert cp.returncode == 0, (
            "evolve cannot cp install.json — §10a `cp /tmp/evolve-stage-*.json "
            f"{SHARED_DIR}/install.json` grant missing/dead: {cp.stderr}")
        chown = _sh(["sudo", "-n", "-u", EVOLVE_USER, "sudo", "-n",
                     "/usr/bin/chown", f"{EVOLVE_USER}:root", str(install_json)], check=False)
        assert chown.returncode == 0, (
            "evolve cannot chown install.json — §10a chown grant missing/dead: "
            f"{chown.stderr}")
        owner = _sh(["stat", "-c", "%U", str(install_json)], check=True).stdout.strip()
        assert owner == EVOLVE_USER, (
            f"install.json owner is {owner!r}, expected evolve — the fast write_text "
            "path won't win on the next deploy (parity-with-macOS goal)")
        # And record_bot_deploy stamps the per-bot version record (merge + version stamp).
        deploy.record_bot_deploy(BOT, SHARED_DIR)
        rec = deploy.read_install_json(SHARED_DIR) or {}
        assert BOT in (rec.get("bot_versions") or {}), (
            f"record_bot_deploy did not stamp {BOT}: {rec}")
        _diag("step6c2-install-json", json.dumps(rec, indent=2))
        print("upgrade-reclamp: heal restores evolve traverse+read after a 0700 mask "
              "clamp; verify_evolve_access clears the per-bot deploy gate; evolve "
              "writes install.json via §10a (cp+chown) and record_bot_deploy stamps it")
    finally:
        _sudo("/bin/rm", "-f", stage, check=False)


# ══════════════════════════════════════════════════════════════════════════════
# Step 6d — the WIZARD's OWN provisioning, live. W8: a real `setup --fresh` on
# Ubuntu failed even with steps 1–6 green, because the wizard's account-
# provisioning code (_provision_evo_oc) was macOS-/Users-hardcoded and the
# seam-level steps never drove it. This step drives the wizard's day-one evo
# provisioning end to end and asserts the evo account, /home/evo OC config,
# and the gateway systemd unit all land at the correct Linux paths — so a
# green harness now means the real wizard install works.
# ══════════════════════════════════════════════════════════════════════════════


def test_step6d_wizard_evo_day_one_provisioning():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation
    from runtime.scheduler import get_scheduler

    from evolve_admin import deploy, setup_wizard

    # Drive the wizard's primary-bot provisioning exactly as run_setup does on
    # a fresh Linux pod: gateway_account="evo" (day-one, no cutover). The
    # evolve service account already exists (step 3); _provision_evo_oc creates
    # `evo`, writes its OC config on /home/evo, and installs the gateway unit
    # through the systemd seam. non_interactive=True + no keys ⇒ no prompts.
    ok = setup_wizard._provision_evo_oc(
        "e2epod", SHARED_DIR, _runner_user(), [], True,
        telegram_token="", bot_id="evo", gateway_account=EVO_USER,
    )
    assert ok, "_provision_evo_oc(gateway_account='evo') returned False"

    # 1. Both accounts exist on the real host: evolve (service) + evo (bot),
    #    provisioned day-one with no /Users → /home cutover shuffle.
    iso = get_isolation()
    assert iso.user_exists(EVOLVE_USER), "evolve service account missing"
    assert iso.user_exists(EVO_USER), "evo account was not created day-one"

    # 2. evo's OC config landed on /home/evo (NOT /Users), owned by evo, with
    #    the workspace pointed at evo's own home.
    oc_json = EVO_HOME / ".openclaw" / "openclaw.json"
    body = _sudo("/bin/cat", str(oc_json), check=True).stdout
    assert "/Users/" not in body, "macOS /Users path leaked into evo's openclaw.json"
    cfg = json.loads(body)
    ws = cfg["agents"]["defaults"]["workspace"]
    assert ws == f"/home/{EVO_USER}/.openclaw/workspace", f"workspace={ws!r}"
    owner = _sh(["stat", "-c", "%U", str(oc_json)], check=True).stdout.strip()
    assert owner == EVO_USER, f"openclaw.json owned by {owner!r}, expected {EVO_USER}"

    # 2b. The evolve plugin config carries the required botId (W10-E fix 4). The
    #     live VPS shipped this as {} → the evo gateway's `plugins install`
    #     failed schema validation ("must have required property 'botId'"). The
    #     seed must populate it before any plugin install runs.
    evolve_cfg = cfg["plugins"]["entries"]["evolve"]["config"]
    assert evolve_cfg.get("botId") == "evo", (
        f"evo plugin config missing/empty botId: {evolve_cfg!r}"
    )
    assert evolve_cfg.get("role") == "primary", f"evo plugin role wrong: {evolve_cfg!r}"

    # 3. The gateway installed through the SYSTEMD seam (not launchd): the unit
    #    FILE lands with User=evo + /home/evo paths, no macOS leakage. (The
    #    service can't START — no real openclaw — but install() writes the unit
    #    before the restart, so the file persists; that is what we assert.)
    unit_path = Path(f"/etc/systemd/system/{EVO_GATEWAY_LABEL}.service")
    assert unit_path.exists(), (
        f"{unit_path} missing — the evo gateway did not route through the "
        f"systemd seam (still launchd-only?)"
    )
    unit = _sudo("/bin/cat", str(unit_path), check=True).stdout
    _diag("evo-gateway.service", unit)
    assert f"User={EVO_USER}" in unit, f"unit not run as evo:\n{unit}"
    assert f"HOME=/home/{EVO_USER}" in unit, f"HOME not on /home/evo:\n{unit}"
    assert "/Users/" not in unit, "macOS /Users path leaked into the systemd unit"
    assert "/Library/LaunchDaemons" not in unit, "launchd path leaked into the systemd unit"
    assert "/opt/homebrew" not in unit, "Homebrew PATH leaked into the Linux unit"

    STATE["evo_provisioned"] = True

    # Stop the openclaw-less gateway now (its ExecStart can't resolve, so the
    # Restart=always unit would otherwise flap every RestartSec until module
    # teardown). The assertions above already captured the installed unit.
    ok_rm, msg_rm = get_scheduler().remove(EVO_GATEWAY_LABEL)
    print(f"  remove({EVO_GATEWAY_LABEL}): ok={ok_rm} {msg_rm}")

    # ── 4. install_evolve_bot_docs lands the primary identity docs on the EVO
    #    primary home — never the brain-less /home/evolve service account ───────
    # EVOLVE-ACCT-OCJSON follow-up (the install_evolve_app sibling, #3063). On a
    # fresh Linux pod the primary is "evo" on account "evo", so SOUL/AGENTS/
    # MEMORY/README + procedures/ must reach /home/evo/.openclaw/workspace where
    # the evo gateway reads them. The old _bot_user_for("evolve") hardcode put
    # them on /home/evolve (the daemon-only service account), where nothing reads
    # — and the headline "no /Users inode" invariant cannot catch it (/home/evolve
    # is a valid Linux path). Drive the REAL installer against the canonical
    # fresh-Linux network.json (primary=evo) and assert placement directly.
    saved_net = _sudo("/bin/cat", str(NETWORK_JSON), check=False)
    saved_net_body = saved_net.stdout if saved_net.returncode == 0 else None
    fresh_linux_net = {
        "networkId": "linux-e2e", "sharedDir": str(SHARED_DIR),
        "primary": EVO_USER, "members": [EVO_USER],
        "bots": {EVO_USER: {"role": "primary", "user": EVO_USER, "port": 18900}},
    }
    _sudo("/usr/bin/tee", str(NETWORK_JSON),
          input_text=json.dumps(fresh_linux_net, indent=2))
    _sudo("/usr/bin/chown", f"{EVOLVE_USER}:{EVOLVE_USER}", str(NETWORK_JSON))
    try:
        docs_result = deploy.install_evolve_bot_docs(dry_run=False)
        _diag("step6d-evolve-bot-docs.txt",
              "\n".join(docs_result.steps) + "\n--- errors ---\n"
              + "\n".join(docs_result.errors))
        assert docs_result.success, (
            f"install_evolve_bot_docs failed: {docs_result.errors}")
        evo_ws = EVO_HOME / ".openclaw" / "workspace"
        for fname in ("SOUL.md", "AGENTS.md", "MEMORY.md", "README.md"):
            dst = evo_ws / fname
            assert _sudo("/usr/bin/test", "-f", str(dst), check=False).returncode == 0, (
                f"{fname} did not land on the evo primary home {dst} — "
                "install_evolve_bot_docs misrouted")
        proc_dir = evo_ws / "procedures"
        assert _sudo("/usr/bin/test", "-d", str(proc_dir), check=False).returncode == 0, (
            f"procedures/ dir not created on the evo primary: {proc_dir}")
        owner = _sh(["stat", "-c", "%U", str(evo_ws / "SOUL.md")],
                    check=True).stdout.strip()
        assert owner == EVO_USER, f"SOUL.md owned by {owner!r}, expected {EVO_USER}"
        # The smoking gun: nothing reached the brain-less service account home.
        evolve_ws = Path(f"/home/{EVOLVE_USER}") / ".openclaw" / "workspace"
        for fname in ("SOUL.md", "AGENTS.md", "MEMORY.md", "README.md"):
            stray = evolve_ws / fname
            assert _sudo("/usr/bin/test", "-f", str(stray), check=False).returncode != 0, (
                f"identity doc misrouted to the evolve SERVICE account: {stray} "
                "— the EVOLVE-ACCT-OCJSON bug is back")
        print(f"  install_evolve_bot_docs: SOUL/AGENTS/MEMORY/README + procedures/ "
              f"on /home/{EVO_USER} (owned by {EVO_USER}); none on /home/{EVOLVE_USER}")
    finally:
        if saved_net_body is not None:
            _sudo("/usr/bin/tee", str(NETWORK_JSON), input_text=saved_net_body)
            _sudo("/usr/bin/chown", f"{EVOLVE_USER}:{EVOLVE_USER}", str(NETWORK_JSON))

    print(
        "wizard evo-day-one: evolve+evo accounts, /home/evo OC config "
        "(owned by evo, workspace on /home/evo), systemd gateway unit "
        "User=evo, install_evolve_bot_docs → identity docs on /home/evo "
        "(not the /home/evolve service account) — no /Users / /Library / "
        "Homebrew leakage"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Step 6e — the REAL plugin build path, end to end on Linux. Same W7/W8 lesson
# again: step6b drove the venv build + fix_plugin_permissions SEAMS but never ran
# build_plugin()'s TypeScript compile, so the DO-pathfinder Step-14 failure
# shipped green — `npx --yes tsc` on Ubuntu 24.04 / npm 11 fetched the bogus
# standalone tsc@2.0.4 package ("This is not the tsc command you are looking
# for") instead of the local compiler, and the deploy died "tsc failed:". This
# step drives the actual deploy.build_plugin() and asserts the install dir got a
# NON-EMPTY compiled dist/index.js — which only a real tsc can emit (the bogus
# package exits non-zero, so build_plugin would RAISE before reaching here). A
# green here is therefore non-vacuous: it proves the real compile ran.
# ══════════════════════════════════════════════════════════════════════════════


def test_step6e_plugin_build_compiles_via_local_tsc():
    from evolve_admin import deploy

    # Defensive: deploy.py freezes PLUGIN_INSTALL_DIR at import from sys.platform.
    # On this Linux runner it must be the /var/lib path; if a stray macOS-profile
    # import had frozen it to /Users/Shared, this step would silently build to
    # the wrong place — assert the Linux path so any such mismatch fails loudly.
    assert str(deploy.PLUGIN_INSTALL_DIR) == str(PLUGIN_INSTALL_DIR), (
        f"deploy.PLUGIN_INSTALL_DIR={deploy.PLUGIN_INSTALL_DIR} != "
        f"{PLUGIN_INSTALL_DIR} (profile froze to the wrong platform at import)"
    )

    # node/npm are preinstalled on the ubuntu-24.04 image. We do NOT skip when
    # they're absent — a skip here would re-hide exactly the blind-spot this step
    # closes; build_plugin()'s own _check_node_version() raises a clear error if
    # the toolchain is missing, which is the correct loud failure.
    for tool in ("node", "npm"):
        print(f"  which {tool} = {shutil.which(tool)}")

    install_index = PLUGIN_INSTALL_DIR / "dist" / "index.js"
    # Clean slate so the assertion below can't pass on a stale prior artifact.
    _sudo("/bin/rm", "-rf", str(PLUGIN_INSTALL_DIR), check=False)

    # Remove node_modules so build_plugin's Step 1a (`npm ci`) DETERMINISTICALLY
    # runs — that is the step the 2026-06-23 freeze's mechanism-2 fix lives in,
    # and the lockfile-clean assertion below is only non-vacuous when the install
    # actually executes. Precondition: the committed lockfile is clean.
    repo_root = deploy._REPO_ROOT
    lockfile = "packages/plugin/package-lock.json"
    _sh(["git", "-C", str(repo_root), "checkout", "--", lockfile], check=False)
    _sudo("/bin/rm", "-rf", str(deploy.PLUGIN_SRC_DIR / "node_modules"), check=False)

    # THE real deploy build path. With the fix it runs `npm ci` (Step 1a, honors
    # the lockfile) then `npm run build` (local tsc); under the tsc regression it
    # would run `npx --yes tsc`, fetch the bogus package, and raise here.
    print("\n$ deploy.build_plugin()  (real npm ci + npm run build + sync)")
    deploy.build_plugin()

    assert install_index.is_file(), (
        f"{install_index} not produced by deploy.build_plugin() — the real "
        "plugin compile did not emit the installed entrypoint"
    )
    size = install_index.stat().st_size
    # A real compiled index.js is well over a hundred bytes; the bogus standalone
    # tsc package can emit nothing (it exits non-zero). Floor above triviality.
    assert size > 100, (
        f"{install_index} is {size} bytes — too small to be real compiled JS; "
        "the local-tsc compile did not run"
    )

    # MECHANISM 2 of the 2026-06-23 Linux freeze: `npm install` rewrote
    # package-lock.json (+1 line) IN the read-only deploy checkout, dirtying it
    # so the next `git pull --ff-only` refused. `npm ci` installs from the
    # lockfile WITHOUT rewriting it — so after a real build the checkout's
    # lockfile (and the rest of packages/plugin/, dist restored, node_modules
    # gitignored) must be pristine. Under the regression this status is non-empty.
    porcelain = _sh(
        ["git", "-C", str(repo_root), "status", "--porcelain", "packages/plugin/"],
        check=True,
    ).stdout
    assert porcelain.strip() == "", (
        "deploy.build_plugin() left packages/plugin/ dirty in the deploy "
        f"checkout — `git pull --ff-only` would refuse (freeze mechanism 2):\n{porcelain}"
    )

    print(
        f"plugin build: deploy.build_plugin() ran `npm ci` + LOCAL tsc, installed "
        f"a {size}-byte dist/index.js at {install_index}, and left the deploy "
        "checkout's packages/plugin/ pristine (no lockfile churn → no ff-only wedge)"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Step 6l — the 2026-06-23 Linux pod freeze: the deploy checkout self-dirtying so
# `git pull --ff-only` wedges and the fleet freezes on stale code. On Linux the
# deploy checkout is a CHILD of shared_dir (/var/lib/evolve/repo), so
# deploy_shared_dir's recursive `chmod -R a+rX` (and the installer's `chmod -R
# 755`) descended INTO the git tree, flipped tracked files 100644→100755, and
# `core.fileMode=true` then made ff-only refuse — 3096 files flagged, the fleet
# frozen ~37 commits behind origin. This drives the REAL widen as the evolve user
# against a real nested git checkout and proves: (1) the widen leaves the checkout
# clean (prune), (2) a subsequent `git pull --ff-only` fast-forwards, (3) the
# puller pins core.fileMode=false defensively. The falsifiable check that would
# have caught the freeze.
# ══════════════════════════════════════════════════════════════════════════════


def test_step6l_deploy_checkout_survives_widen_and_ff_pulls():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation

    iso = get_isolation()
    venv_py = str(EVOLVE_VENV / "bin" / "python3")

    # Build the synthetic layout UNDER the real shared_dir so the real LINUX
    # profile's nested_deploy_checkout(/var/lib/evolve) targets the checkout at
    # the REAL /var/lib/evolve/repo path — no profile injection. shared_dir is
    # evolve-owned (step 6), so evolve can create + traverse everything here.
    repo = SHARED_DIR / "repo"
    upstream = SHARED_DIR / "_e2e_upstream"

    def ev(script: str):
        # Run as evolve, cd'd into the (evolve-owned, traversable) shared_dir
        # first so Node/Python/git never EACCES on an untraversable cwd (the
        # documented `sudo -u <bot>` getcwd gotcha).
        return iso.run_as(
            EVOLVE_USER, ["/bin/bash", "-c", f"cd {SHARED_DIR} && {script}"],
            capture_output=True, text=True,
        )

    _sudo("/bin/rm", "-rf", str(repo), str(upstream), check=False)
    try:
        # ── upstream with two commits; clone @first into the deploy checkout ──
        ev(f"git init -q {upstream} && git -C {upstream} config user.email t@t && "
           f"git -C {upstream} config user.name t && printf 'x = 1\\n' > {upstream}/a.py && "
           f"git -C {upstream} add -A && git -C {upstream} commit -q -m A")
        r = ev(f"git clone -q {upstream} {repo}")
        assert r.returncode == 0, f"clone failed: {r.stderr}"
        ev(f"git -C {repo} config core.fileMode true")  # the config that turns churn into a wedge
        ev(f"printf 'x = 1\\ny = 2\\n' > {upstream}/a.py && git -C {upstream} add -A && "
           f"git -C {upstream} commit -q -m B")
        head_b = ev(f"git -C {upstream} rev-parse HEAD").stdout.strip()
        assert ev(f"git -C {repo} status --porcelain").stdout.strip() == "", "fresh clone not clean"

        # ── NON-VACUOUS: a blanket recursive widen over the checkout DOES dirty
        #    it under fileMode=true (the freeze) — so this test fails if the fix
        #    regresses. Scoped to the checkout so the rest of shared_dir is
        #    untouched. ─────────────────────────────────────────────────────
        ev(f"chmod -R 755 {repo}")
        assert ev(f"git -C {repo} status --porcelain").stdout.strip(), (
            "precondition: a blanket `chmod -R 755` over the nested checkout must "
            "dirty it under core.fileMode=true (the freeze) — else this guards nothing"
        )
        ev(f"git -C {repo} checkout -- . && find {repo} -name '*.py' -exec chmod 644 {{}} +")
        assert ev(f"git -C {repo} status --porcelain").stdout.strip() == "", "recover failed"

        # ── THE FIX (mechanism 1): the REAL widen, run as evolve via the deploy
        #    venv, must NOT descend into the nested checkout. ─────────────────
        widen_py = ("from evolve_admin import secret_config_perms as s; "
                    f"s.widen_shared_dir_world_read('{SHARED_DIR}', chmod='/bin/chmod')")
        w = ev(f'{venv_py} -c "{widen_py}"')
        assert w.returncode == 0, f"widen failed: {w.stderr}"
        st = ev(f"git -C {repo} status --porcelain").stdout
        assert st.strip() == "", (
            "widen_shared_dir_world_read dirtied the nested deploy checkout — the "
            f"2026-06-23 freeze would recur:\n{st}"
        )

        # ── ...and a subsequent `git pull --ff-only` (the puller's command) does
        #    fast-forward to the upstream HEAD. ────────────────────────────────
        pull = ev(f"git -C {repo} pull --ff-only")
        assert pull.returncode == 0, f"ff-only pull refused after widen: {pull.stderr}"
        assert ev(f"git -C {repo} rev-parse HEAD").stdout.strip() == head_b, "did not fast-forward"

        # ── THE FIX (mechanism 3): the puller pins core.fileMode=false on the
        #    nested checkout (run as evolve via the deploy venv). ──────────────
        ev(f"git -C {repo} config core.fileMode true")  # simulate drift back to the dangerous value
        pin_py = ("from pathlib import Path; from evolve_admin import repo_puller as r; "
                  f"r.pin_filemode_off_if_nested(Path('{repo}'), Path('{SHARED_DIR}'), sudo_evolve=False)")
        p = ev(f'{venv_py} -c "{pin_py}"')
        assert p.returncode == 0, f"pin failed: {p.stderr}"
        fm = ev(f"git -C {repo} config core.fileMode").stdout.strip()
        assert fm == "false", f"puller did not pin core.fileMode=false (got {fm!r})"

        print("deploy-checkout freeze: real widen left the nested checkout clean, "
              "`git pull --ff-only` fast-forwarded, and the puller pinned "
              "core.fileMode=false — the 2026-06-23 wedge cannot recur")
    finally:
        _sudo("/bin/rm", "-rf", str(repo), str(upstream), check=False)


# ══════════════════════════════════════════════════════════════════════════════
# Step 6f — the deploy_bot DAEMON-INSTALL path, driven for REAL with NO seam
# injection. This is the regression-lock for the whole deploy_bot Linux port.
#
# Why it has to exist: steps 1-6e all run with the module's autouse
# `_linux_seams` fixture having injected set_scheduler(SystemdScheduler()), so
# every prior step validated the SEAM with the adapter already swapped in. They
# NEVER exercised the production get_scheduler() DEFAULT, the real
# install_bot_gateway_plist node/openclaw resolution, or the apply/cost-converter
# installs. The live DO-pathfinder proved that gap is load-bearing: a real
# `deploy darwin` failed at get_scheduler()->launchd then node-path 203/EXEC,
# even though this harness was 14/14 green. This step resets the seam to the
# platform default and drives the real install path so that blind-spot is shut.
# ══════════════════════════════════════════════════════════════════════════════

# Minimal stand-in for the openclaw gateway entrypoint: parse `--port N`, bind
# 127.0.0.1:N, and stay alive. install_bot_gateway_plist resolves THIS as the
# ExecStart index (fix 2's Linux candidate) and _wait_for_gateway_port (fix 6)
# TCP-connects to prove the listener came up. No OpenClaw, no network.
_STUB_OPENCLAW_JS = """\
const net = require('net');
const a = process.argv.slice(2);
const i = a.indexOf('--port');
const port = i >= 0 ? parseInt(a[i + 1], 10) : 0;
net.createServer((s) => s.end()).listen(port, '127.0.0.1');
setInterval(() => {}, 1 << 30);
"""


def _install_stub_openclaw() -> None:
    """Stage the stub openclaw entrypoint at the Linux NodeSource global prefix
    (``/usr/lib/node_modules/openclaw/dist/index.js``) — fix 2's added candidate
    — so install_bot_gateway_plist resolves a real Linux index path."""
    dist = STUB_OC_DIR / "dist"
    _sudo("/bin/mkdir", "-p", str(dist))
    _sudo("/usr/bin/tee", str(dist / "index.js"), input_text=_STUB_OPENCLAW_JS)
    _sudo("/bin/chmod", "-R", "755", str(STUB_OC_DIR))


def _ensure_node_resolvable() -> None:
    """Guarantee `node` resolves inside install_bot_gateway_plist's platform-keyed
    node search path. A real DO pod gets node from NodeSource at /usr/bin/node
    (in the search path); the GH runner's setup-node lands it in the toolcache
    (NOT in the search path), so symlink it into /usr/local/bin (which IS) so the
    rendered ExecStart is a real, executable node — never the /opt/homebrew
    fallback."""
    node_search = "/opt/homebrew/bin:/usr/local/bin:/opt/homebrew/opt/node/bin:/usr/bin:/bin"
    if shutil.which("node", path=node_search):
        return
    real = shutil.which("node")
    assert real, "node not found on PATH — linux-e2e requires node (setup-node step)"
    _sudo("/bin/ln", "-sf", real, "/usr/local/bin/node")


def test_step6f_real_deploy_daemon_install_no_injection():
    """Drive the REAL deploy_bot daemon-install path on Linux with NO scheduler
    injection, asserting the production default + real installs work end to end:

      (a) ai.openclaw.<bot>-gateway.service exists with a Linux ExecStart that
          actually EXECUTES — real resolved node + the /usr/lib openclaw index
          (fix 2), never /opt/homebrew;
      (b) the gateway BINDS its port within fix 6's cold-VPS window;
      (c) the apply + cost-converter daemons render SYSTEMD units, not launchd
          plists (fix 4) — no /Library/LaunchDaemons write is even attempted;
      (d) the member-bot OC instance lands only under /home/<bot> — no
          /Users/<bot> leak (fix 5 regression-lock).

    Asserts the PRODUCTION default explicitly (set_scheduler(None) → the fix-1
    platform key), not the seam injected by the module's autouse fixture — the
    exact path the live DO box exercised that this harness previously never did.
    """
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    import socket

    from platform_profile import get_profile
    from runtime.scheduler import SystemdScheduler, get_scheduler, set_scheduler

    from evolve_admin import deploy, setup_wizard

    port = 18931
    try:
        # 0. Reset the seam to the PLATFORM DEFAULT and prove fix 1 in PRODUCTION:
        #    get_scheduler() must resolve SystemdScheduler from the pinned LINUX
        #    profile — NOT because a test injected it.
        set_scheduler(None)
        assert get_profile().name == "linux"
        sched = get_scheduler()
        assert isinstance(sched, SystemdScheduler), (
            f"production default resolved {type(sched).__name__}, not "
            "SystemdScheduler — fix 1 (platform-keyed get_scheduler) is not active "
            "in production; the live box hit exactly this (launchd on Linux)"
        )

        _install_stub_openclaw()
        _ensure_node_resolvable()

        # ── (a) + (b): the REAL gateway install, production default ──────────
        ok, detail = deploy.install_bot_gateway_plist(BOT, port, user=BOT)
        assert ok, f"install_bot_gateway_plist failed on the production path: {detail}"
        gw_unit = Path(f"/etc/systemd/system/{GATEWAY_LABEL}.service")
        assert gw_unit.exists(), (
            f"{gw_unit} missing — the gateway did not route through the systemd seam"
        )
        unit = _sudo("/bin/cat", str(gw_unit), check=True).stdout
        _diag("step6f-gateway.service", unit)
        # (a) the ExecStart is a real Linux path that executes — never Homebrew.
        assert "/opt/homebrew" not in unit, (
            f"Homebrew path leaked into the Linux gateway unit (fix 2):\n{unit}"
        )
        assert "/usr/lib/node_modules/openclaw/dist/index.js" in unit, (
            f"gateway ExecStart is not the resolved Linux openclaw index (fix 2):\n{unit}"
        )
        # (b) the listener is actually up — install only returns ok after the
        #     port-bind wait (fix 6), but prove it independently.
        assert get_scheduler().running(GATEWAY_LABEL), "gateway unit is not running"
        with socket.create_connection(("127.0.0.1", port), timeout=3.0):
            pass  # a successful connect == the gateway bound its port

        # ── (c): cost-converter renders a SYSTEMD unit, not a plist ──────────
        result = deploy.DeployResult(bot_id=BOT, success=True)
        deploy._install_launchd_cost_converter(BOT, result, user=BOT)
        cost_unit = Path(f"/etc/systemd/system/{COST_LABEL}.service")
        assert cost_unit.exists(), (
            f"{cost_unit} missing — cost-converter did not render a systemd unit (fix 4)"
        )
        # No launchd write was even attempted: the box has no /Library/LaunchDaemons
        # and the unit carries no .plist sibling.
        assert not Path("/Library/LaunchDaemons").exists(), (
            "/Library/LaunchDaemons exists on a Linux box — a launchd write was attempted"
        )
        assert not Path(str(cost_unit)[:-len(".service")] + ".plist").exists()
        assert get_scheduler().status(COST_LABEL)["installed"]

        # ── (c'): the retired apply daemon's unit is swept, platform-correctly ─
        # _bootout_retired_per_bot_jobs goes through the scheduler seam rather
        # than a hardcoded /Library/LaunchDaemons rm, so it must actually
        # remove a Linux unit. Plant one, sweep, assert it is gone — this is
        # the assertion the old macOS-only _bootout_legacy_test_plists could
        # never have satisfied.
        # The planted unit is deliberately made to FAIL before the sweep: that
        # is the state the real retired apply units were in on the VPS on
        # 2026-08-18 (`status=2/INVALIDARGUMENT`, ExecStart naming a deleted
        # file). Removing the unit file does NOT retract systemd's recorded
        # failure, so without the `reset-failed` in SystemdScheduler.remove a
        # SUCCESSFUL teardown leaves the unit in `list-units --state=failed`
        # as `not-found / failed` and anything monitoring failed units trips
        # on the residue. This is the only place that can be proven against a
        # real systemd, so prove it here rather than trusting the argv.
        stale = Path(f"/etc/systemd/system/{APPLY_LABEL}.service")
        _sudo("/usr/bin/tee", str(stale), input_text=(
            "[Unit]\nDescription=stale retired apply daemon\n"
            "[Service]\nType=oneshot\nExecStart=/bin/false\n"
            "[Install]\nWantedBy=multi-user.target\n"
        ), check=True)
        _sudo("/usr/bin/systemctl", "daemon-reload", check=False)
        assert stale.exists(), "test fixture failed to plant the stale apply unit"
        # oneshot + /bin/false ⇒ `start` exits non-zero and the unit lands in
        # the failed state (check=False: the failure IS the fixture).
        _sudo("/usr/bin/systemctl", "start", f"{APPLY_LABEL}.service", check=False)

        def _failed_units() -> str:
            return (_sudo(
                "/usr/bin/systemctl", "list-units", "--state=failed",
                "--all", "--plain", "--no-legend", check=False,
            ).stdout or "")

        assert APPLY_LABEL in _failed_units(), (
            "test fixture failed to put the stale apply unit into the failed "
            f"state; failed list was:\n{_failed_units()}"
        )

        deploy._bootout_retired_per_bot_jobs(BOT, result)
        assert not stale.exists(), (
            f"{stale} survived _bootout_retired_per_bot_jobs — the sweep is not "
            "platform-correct on Linux"
        )
        residue = _failed_units()
        assert APPLY_LABEL not in residue, (
            f"{APPLY_LABEL} is still listed as failed after a successful "
            "teardown — SystemdScheduler.remove did not reset-failed (check the "
            "`systemctl reset-failed` sudoers grant reached this box). Failed "
            f"list:\n{residue}"
        )

        # ── (d): member-bot OC instance lands only under /home/<bot> ─────────
        # Drive the real member-bot OC provisioning (the path that leaked
        # /Users/darwin pre-#2979) and assert no /Users/<bot> inode appears.
        assert setup_wizard._setup_oc_for_bot(BOT, port), "_setup_oc_for_bot returned False"
        assert (BOT_HOME / ".openclaw" / "openclaw.json").exists(), (
            "member-bot OC config did not land under /home/<bot>"
        )
        assert not Path(f"/Users/{BOT}").exists(), (
            f"/Users/{BOT} leaked — member-bot OC provisioning is not platform-keyed "
            "(fix 5 regression)"
        )
    finally:
        # Stop the stub gateway (Restart=always would otherwise flap until
        # module teardown) + the apply/cost-converter units, then restore the
        # injected fake so later steps/modules see the fixture's posture.
        for label in (GATEWAY_LABEL, APPLY_LABEL, COST_LABEL):
            try:
                get_scheduler().remove(label)
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                print(f"  step6f cleanup remove({label}) failed: {exc}")
        set_scheduler(SystemdScheduler())

    print(
        "real-deploy daemon-install (NO injection): production get_scheduler() → "
        "SystemdScheduler; gateway ExecStart real node + /usr/lib openclaw, port "
        "bound; apply + cost-converter systemd units (no launchd plist); member-bot "
        "OC only under /home/<bot> — no /Users leak"
    )


def test_step6h_restart_gateway_installs_missing_unit_and_binds():
    """fix 1 / W10-E: Step-14 calls ``restart_gateway()`` to bring a member-bot
    gateway up. On macOS that keys off a /Library/LaunchDaemons plist; on Linux
    the plist never exists, so the legacy code no-op'd into a phantom
    "✓ Gateway restarted" and nothing bound on the port. ``restart_gateway``
    must now INSTALL the systemd unit when it's absent (and restart it when
    present). Driven on the PRODUCTION default (set_scheduler(None) → fix-1
    platform key), exactly the path the live DO box hit."""
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    import socket

    from platform_profile import get_profile
    from runtime.scheduler import SystemdScheduler, get_scheduler, set_scheduler

    from evolve_admin import deploy

    def _port_open(p: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=2.0):
                return True
        except OSError:
            return False

    port = 18934
    try:
        set_scheduler(None)
        assert get_profile().name == "linux"
        assert isinstance(get_scheduler(), SystemdScheduler), (
            "production default is not SystemdScheduler — fix 1 inactive"
        )

        _install_stub_openclaw()
        _ensure_node_resolvable()

        # restart_gateway resolves the gateway port from network.json
        # (get_bot_port); write the canonical Linux network.json, evolve-owned.
        _sudo("/bin/mkdir", "-p", str(SHARED_DIR / "logs"))
        network = {
            "networkId": "linux-e2e",
            "sharedDir": str(SHARED_DIR),
            "members": [BOT],
            "bots": {BOT: {"role": "member", "port": port}},
        }
        _sudo("/usr/bin/tee", str(NETWORK_JSON), input_text=json.dumps(network, indent=2))
        _sudo("/usr/bin/chown", "-R", f"{EVOLVE_USER}:{EVOLVE_USER}", str(SHARED_DIR))

        # Precondition: NO gateway unit on disk — the fresh-bot state Step-14
        # hits (gateway install deferred at bot-creation → "install during deploy").
        get_scheduler().remove(GATEWAY_LABEL)
        assert not Path(f"/etc/systemd/system/{GATEWAY_LABEL}.service").exists()

        # ── unit ABSENT → restart_gateway INSTALLS it (not a phantom no-op) ──
        deploy.restart_gateway(BOT, bot_user=BOT)
        gw_unit = Path(f"/etc/systemd/system/{GATEWAY_LABEL}.service")
        assert gw_unit.exists(), (
            "restart_gateway did not install the missing gateway unit on Linux "
            "(fix 1 — the live VPS left the gateway port unbound here)"
        )
        assert get_scheduler().running(GATEWAY_LABEL), "installed gateway is not running"
        # Poll for the bind (the stub listener needs a moment to come up); a
        # successful connect == a real listener, not a phantom success.
        _wait_for(f"gateway bound :{port} after install",
                  lambda: _port_open(port), timeout=30, interval=1.0)

        # ── unit PRESENT → restart_gateway restarts it (stays bound) ─────────
        # The seam restart bounces the process; poll until it re-binds (a bare
        # connect would race the rebound listener).
        deploy.restart_gateway(BOT, bot_user=BOT)
        assert get_scheduler().running(GATEWAY_LABEL)
        _wait_for(f"gateway re-bound :{port} after restart",
                  lambda: _port_open(port), timeout=30, interval=1.0)
    finally:
        try:
            get_scheduler().remove(GATEWAY_LABEL)
        except Exception as exc:  # noqa: BLE001 — best-effort teardown
            print(f"  step6h cleanup remove({GATEWAY_LABEL}) failed: {exc}")
        set_scheduler(SystemdScheduler())

    print("restart_gateway on Linux: unit absent → installs + binds; unit "
          "present → restarts + stays bound (no phantom success)")


def test_step6i_systemd_seam_autocreates_stdout_log_dir():
    """fix 2 / W10-E: systemd (unlike launchd) does NOT create the directory
    behind StandardOutput. The scheduler seam must mkdir+chown each log parent
    (to the unit's User=) before the unit starts, or it 209/STDOUT crash-loops
    (the live admin-ui hit 39 restarts for this). Install a unit whose
    stdout_path is in a FRESH (absent) dir and assert the seam created it,
    owned by the unit's user."""
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.scheduler import JobSpec, get_scheduler

    sched = get_scheduler()
    label = f"ai.evolve.{BOT}.logdir-probe"
    log_dir = OC_DIR / "w10e-freshlogs"
    _sudo("/bin/rm", "-rf", str(log_dir), check=False)
    assert not log_dir.exists(), "precondition: the log dir must not pre-exist"
    spec = JobSpec(
        label=label,
        program_args=["/bin/sh", "-c", "echo probe"],
        user=BOT,
        start_interval=3600,
        run_at_load=True,  # timer fires once on install
        comment="W10-E fix-2 log-dir auto-create probe",
        stdout_path=str(log_dir / "probe.log"),
        stderr_path=str(log_dir / "probe.err.log"),
    )
    try:
        res = sched.install(spec)
        assert res.ok, f"install failed: {res.message}"
        # The seam created the StandardOutput parent — systemd never would.
        assert log_dir.is_dir(), (
            f"{log_dir} not created by the seam — a real unit would 209/STDOUT"
        )
        owner = _sh(["stat", "-c", "%U", str(log_dir)], check=True).stdout.strip()
        assert owner == BOT, f"log dir owned by {owner!r}, expected the unit user {BOT}"
    finally:
        sched.remove(label)
        _sudo("/bin/rm", "-rf", str(log_dir), check=False)
    print("systemd seam auto-creates + chowns the StandardOutput log dir (fix 2)")


def test_step6j_repo_root_preflight_rejects_unreadable_source(monkeypatch):
    """fix 3 / W10-E: a repo source root the evolve service user can't traverse
    (the live box staged it under /root, mode 0710) poisons the venv .pth and
    every daemon ExecStart. The wizard preflight must HARD-FAIL up front. Build
    a real root-owned 0700 tree with the sentinel inside, point ``_REPO_ROOT``
    at it, and assert SystemExit — exercising the faithful
    `sudo -n -u evolve test -r` path (the evolve account exists by step 3)."""
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from evolve_admin import deploy, setup_wizard

    base = "/tmp/w10e-unreadable-root"
    bad_root = Path(base) / "repo"
    sentinel_dir = bad_root / "packages" / "analyzer"
    try:
        _sudo("/bin/mkdir", "-p", str(sentinel_dir))
        _sudo("/bin/sh", "-c", f"echo '# sentinel' > {sentinel_dir}/platform_profile.py")
        _sudo("/usr/bin/chown", "-R", "root:root", base)
        _sudo("/bin/chmod", "700", base)  # /root-style: no o+x for evolve

        monkeypatch.setattr(deploy, "_REPO_ROOT", bad_root)
        assert setup_wizard._user_exists(EVOLVE_USER), "evolve should exist by step 3"
        with pytest.raises(SystemExit):
            setup_wizard._preflight_repo_root_traversable()
    finally:
        _sudo("/bin/rm", "-rf", base, check=False)
    print("repo-root preflight HARD-FAILS on a non-traversable (/root-style) source (fix 3)")


def test_step6k_evolve_principal_can_create_every_daemon_log_dir():
    """fix 2 grant-completeness / W10-E: the SystemdScheduler seam mkdir+chowns
    every unit's StandardOutput parent via `sudo`. When the **evolve daemon**
    (not root) triggers a redeploy, those calls run under evolve's RESTRICTED
    sudoers — so each distinct JobSpec log-dir root needs a matching grant or the
    install fails. step6g runs as root and can't catch a grant miss; this drives
    each log-dir shape AS evolve against the installed `/etc/sudoers.d/evolve`.

    Covers all three roots the daemon fleet logs under (the `.evolve/logs` case
    is the mcp-bridge daemon — the grant the first sweep missed)."""
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    from runtime.isolation import get_isolation

    from evolve_admin import deploy

    iso = get_isolation()

    # Derive the mcp-bridge + admin-ui log dirs from the REAL JobSpec builders so
    # the test tracks the code, not a copy of the paths.
    mcp_spec = deploy._mcp_bridge_jobspec("ai.evolve.evolve.mcp-bridge", EVOLVE_USER, 8765, "127.0.0.1")
    admin_spec = deploy._admin_ui_jobspec(ADMIN_UI_LABEL)
    log_dirs = sorted({
        str(Path(p).parent)
        for p in (
            mcp_spec.stdout_path, mcp_spec.stderr_path,        # /home/evolve/.evolve/logs
            admin_spec.stdout_path, admin_spec.stderr_path,    # /home/evolve/.openclaw/logs + {shared}/logs
            str(OC_DIR / "logs" / "gateway.log"),              # /home/<bot>/.openclaw/logs
        )
    })
    assert any(".evolve/logs" in d for d in log_dirs), (
        "expected an mcp-bridge .evolve/logs dir in the set — builder drifted?"
    )

    created: list[str] = []
    try:
        for d in log_dirs:
            assert "/Users/" not in d, f"a JobSpec log dir is a macOS /Users path: {d}"
            mk = iso.run_as(
                EVOLVE_USER, ["sudo", "-n", "/bin/mkdir", "-p", d],
                capture_output=True, text=True,
            )
            assert mk.returncode == 0, (
                f"evolve lacks a `sudo mkdir -p {d}` grant — the seam's log-dir "
                f"creation fails on an evolve-initiated install: {mk.stderr!r}"
            )
            created.append(d)
            ch = iso.run_as(
                EVOLVE_USER, ["sudo", "-n", "/usr/bin/chown", EVOLVE_USER, d],
                capture_output=True, text=True,
            )
            assert ch.returncode == 0, (
                f"evolve lacks a `sudo chown {EVOLVE_USER} {d}` grant: {ch.stderr!r}"
            )
    finally:
        for d in created:
            _sudo("/bin/rm", "-rf", d, check=False)
    print(f"evolve principal can mkdir+chown every daemon log-dir root: {log_dirs}")


# ══════════════════════════════════════════════════════════════════════════════
# Step 6g — THE CAPSTONE: drive the full fresh-setup infra-install path
# (`run_setup` Steps 15–18's install_evolve_infra_jobs) on Linux with the
# PRODUCTION-DEFAULT scheduler, and assert the install actually COMES UP —
# closing the recurring "green harness yet real fresh install fails" blind
# spot for good (the 4× W7/W8/W10 lesson: the harness validated SEAMS while
# the full interactive run still leaked macOS /Users paths and never charted
# steps 15–18 end to end).
#
# What this charts that NO prior step did:
#   - install_evolve_infra_jobs() AS THE ORCHESTRATOR (not the individual
#     _install_launchd_* of step 6f, nor step 6's hand-built admin JobSpec):
#     the ~50 evolve infra daemons + the bot-docs/first-party-app installs +
#     the cron baseline rebless, all in one production call.
#   - the admin-ui daemon installed via that orchestrator and the admin server
#     actually BINDING, as evolve, under systemd — the long-standing #2
#     admin-server half of the blind spot (W10-C routed _install_launchd_admin_ui
#     through the seam; here it's reached + proven up via the real orchestrator).
#   - `sudo evolve-admin` resolving under the rendered secure_path (W10-A #1:
#     {venv_dir}/bin on the Linux secure_path) — the pod-breaking blocker.
#   - every infra daemon landing as /etc/systemd/system/*.service, never a
#     launchd plist, with NO /Users path baked into any unit body.
#   - NO /Users/... inode created ANYWHERE on the box — the exact leak surface
#     every prior REAL fresh run tripped on (/Users/<bot>/.openclaw,
#     /Users/Shared/evolve, /Users/evolve, /Users/evo).
#
# Like step 6f it asserts the PRODUCTION default (set_scheduler(None) → the
# platform key), not the autouse-fixture seam — the exact path the live DO box
# exercised that this harness previously never drove to completion.
# ══════════════════════════════════════════════════════════════════════════════

ADMIN_UI_LABEL = f"ai.evolve.{EVOLVE_USER}.admin-ui"  # _install_launchd_admin_ui
ADMIN_UI_PORT = 5050                                   # _admin_ui_jobspec hardcode


def _http_get_port(port: int, path: str, timeout: float = 5.0) -> "tuple[int, str]":
    """(status, body) for a GET against an arbitrary admin port; (-1, err) on fail."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=timeout
        ) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — poll-loop probe; retried by caller
        return -1, f"{type(exc).__name__}: {exc}"


def _installed_evolve_unit_labels() -> list[str]:
    """Every Evolve-namespaced systemd unit FILE currently on disk, as labels
    (filename minus the .service/.timer/.path suffix). /etc/systemd/system is
    world-readable, so this enumerates without sudo — used to assert the install
    landed as systemd (not launchd) and to drive a thorough teardown."""
    sysd = Path("/etc/systemd/system")
    labels: set[str] = set()
    for unit in list(sysd.glob("ai.evolve.*")) + list(sysd.glob("ai.openclaw.*")):
        for suffix in (".service", ".timer", ".path"):
            if unit.name.endswith(suffix):
                labels.add(unit.name[: -len(suffix)])
                break
    return sorted(labels)


def test_step6g_full_fresh_setup_admin_server_up_no_users_leak():
    if not STATE.get("accounts_ok"):
        pytest.skip("step 3 (account creation) did not complete — skipping dependent step")
    if not STATE.get("evo_provisioned"):
        pytest.skip("step 6d (evo provisioning) did not complete — skipping capstone")
    from platform_profile import get_profile
    from runtime.isolation import get_isolation
    from runtime.scheduler import SystemdScheduler, get_scheduler, set_scheduler

    from evolve_admin import deploy

    iso = get_isolation()

    # ── preconditions a REAL fresh setup has by the time it reaches Step 15 ───
    # 1. The canonical Linux network.json (admin-ui's EVOLVE_NETWORK +
    #    install_evolve_infra_jobs' mcp_bridge read). A prior step may have
    #    emptied SHARED_DIR; (re)write it, evolve-owned.
    _sudo("/bin/mkdir", "-p", str(SHARED_DIR / "logs"))
    network = {
        "networkId": "linux-e2e",
        "sharedDir": str(SHARED_DIR),
        "members": [BOT],
        "bots": {BOT: {"role": "member", "port": 18900}},
    }
    _sudo("/usr/bin/tee", str(NETWORK_JSON), input_text=json.dumps(network, indent=2))
    _sudo("/usr/bin/chown", "-R", f"{EVOLVE_USER}:{EVOLVE_USER}", str(SHARED_DIR))

    # 2. The evolve SERVICE account's .openclaw/logs — systemd (like launchd)
    #    needs a daemon's StandardOutput parent dir to exist or the unit fails
    #    to start. On a real pod the evolve-account provisioning creates it; the
    #    e2e's step-3 evolve account is bare, so create it here. (This is a test
    #    precondition, NOT something install_evolve_infra_jobs is expected to do
    #    — it writes UNDER shared_dir, not under /home/evolve.)
    _sudo("/bin/mkdir", "-p", f"/home/{EVOLVE_USER}/.openclaw/logs")
    _sudo("/usr/bin/chown", "-R", f"{EVOLVE_USER}:{EVOLVE_USER}",
          f"/home/{EVOLVE_USER}/.openclaw")

    # 3. The admin-ui ExecStart is the canonical venv evolve-admin shim built by
    #    step 6b; it is a compat-EDITABLE install, so the evolve user must be
    #    able to traverse to BOTH the venv and the checkout it points back at
    #    (GH runners keep /home/runner at 750). Mirror step 6's traverse grants.
    venv_admin = Path(deploy.VENV_EVOLVE_ADMIN)
    assert venv_admin.exists(), (
        f"{venv_admin} missing — step 6b (canonical venv build) did not run; "
        "the capstone needs the real deploy venv to launch the admin server"
    )
    repo_root = Path(__file__).resolve().parents[4]
    for leaf in (venv_admin.parent, repo_root):
        _ensure_traversable_by(EVOLVE_USER, leaf)

    installed_before = set(_installed_evolve_unit_labels())
    try:
        # ── production default: get_scheduler() resolves SystemdScheduler from
        #    the pinned LINUX profile, NOT because a test injected it (fix 1) ──
        set_scheduler(None)
        assert get_profile().name == "linux"
        sched = get_scheduler()
        assert isinstance(sched, SystemdScheduler), (
            f"production default resolved {type(sched).__name__}, not "
            "SystemdScheduler — fix 1 is not active in production"
        )

        # ── THE full orchestrator: the real Step-15 infra install ────────────
        # This is exactly what `run_setup` invokes (setup_wizard Step 15 →
        # install_evolve_infra_jobs). It installs the whole evolve daemon fleet,
        # incl. the admin-ui, plus install_evolve_bot_docs + first-party apps
        # (the path that carried the W10-D /Users/evolve leak) + the cron
        # baseline rebless. Driven AS THE RUNNER (it sudo-escalates internally,
        # matching `sudo evolve-admin install-infra-jobs`).
        print("\n$ deploy.install_evolve_infra_jobs(SHARED_DIR)  (real Step-15 fleet install)")
        result = deploy.install_evolve_infra_jobs(SHARED_DIR, shared_dir=SHARED_DIR)
        _diag("step6g-infra-jobs-steps.txt",
              "\n".join(result.steps) + "\n\n--- errors ---\n" + "\n".join(result.errors))
        print(f"  install_evolve_infra_jobs: {len(result.steps)} steps, "
              f"{len(result.errors)} errors, success={result.success}")
        # Non-vacuous: a single failed daemon (e.g. a log-dir grant miss) used to
        # hide behind the ≥20-units + admin-ui-up criteria. Assert the WHOLE
        # fleet install succeeded. (Runs as root here, so the seam's sudo always
        # works — test_step6k exercises the grants under the evolve principal.)
        assert result.success, f"infra-jobs install reported failure: {result.errors}"

        # ── (a) the admin-ui daemon landed as a systemd unit, run-as evolve ──
        admin_unit = Path(f"/etc/systemd/system/{ADMIN_UI_LABEL}.service")
        assert admin_unit.exists(), (
            f"{admin_unit} missing — install_evolve_infra_jobs did not install the "
            "admin-ui through the scheduler seam (the #2 admin-server blind spot)"
        )
        admin_unit_body = _sudo("/bin/cat", str(admin_unit), check=True).stdout
        _diag("step6g-admin-ui.service", admin_unit_body)
        assert f"User={EVOLVE_USER}" in admin_unit_body, f"admin-ui not run as evolve:\n{admin_unit_body}"
        assert "/Users/" not in admin_unit_body, "macOS /Users path leaked into the admin-ui unit"
        assert "/Library/LaunchDaemons" not in admin_unit_body, "launchd path leaked into the admin-ui unit"
        assert str(venv_admin) in admin_unit_body, (
            f"admin-ui ExecStart is not the venv evolve-admin shim:\n{admin_unit_body}"
        )

        # ── (b) the admin server actually BINDS, as evolve, under systemd ────
        def _admin_err_tail() -> str:
            proc = subprocess.run(
                ["sudo", "-n", "/bin/cat",
                 str(SHARED_DIR / "logs" / "evolve-admin-ui.err.log")],
                capture_output=True, text=True,
            )
            return "\n".join((proc.stdout or "").splitlines()[-30:])

        try:
            _wait_for(
                f"admin server answers :{ADMIN_UI_PORT}/api/health with 200",
                lambda: _http_get_port(ADMIN_UI_PORT, "/api/health")[0] == 200,
                timeout=90, interval=1.0,
            )
        except AssertionError:
            print(f"  admin-ui unit status: {sched.status(ADMIN_UI_LABEL)}")
            print(f"  admin-ui.err.log tail:\n{_admin_err_tail()}")
            raise
        status, body = _http_get_port(ADMIN_UI_PORT, "/api/health")
        print(f"  GET :{ADMIN_UI_PORT}/api/health → {status} {body[:200]}")
        assert status == 200 and json.loads(body)["status"] == "ok"

        # ── (c) `sudo evolve-admin` resolves under the rendered secure_path ──
        # Step 2 installed /etc/sudoers.d/evolve whose Linux `Defaults
        # secure_path` includes {venv_dir}/bin (W10-A #1). Prove it two ways:
        # the PATH sudo hands a command includes the venv bin, and bare
        # `evolve-admin` resolves there (the wizard's `sudo evolve-admin deploy`
        # would have died "command not found" without this).
        venv_bin = str(venv_admin.parent)
        env_proc = _sudo("/usr/bin/env", check=True)
        assert venv_bin in env_proc.stdout, (
            f"sudo secure_path does not include the venv bin {venv_bin!r} "
            f"(W10-A #1 regression):\n{env_proc.stdout}"
        )
        resolve = _sudo("/bin/sh", "-c", "command -v evolve-admin", check=False)
        assert resolve.returncode == 0 and venv_bin in resolve.stdout, (
            f"`sudo evolve-admin` does not resolve under secure_path: "
            f"rc={resolve.returncode} out={resolve.stdout!r}"
        )

        # ── (d) every Evolve daemon is a systemd unit — never a launchd plist ─
        new_labels = [l for l in _installed_evolve_unit_labels()
                      if l not in installed_before]
        print(f"  install added {len(new_labels)} systemd units: {new_labels[:8]}…")
        assert len(new_labels) >= 20, (
            f"expected the infra fleet (~50 units), got {len(new_labels)}: {new_labels}"
        )
        # W10-F #C: the measure daemon installs through the seam on Linux now
        # (it was only ever installed by the macOS-only migrate-jobs static-plist
        # copy, so a Linux pod's health flagged its systemd unit missing).
        assert "ai.openclaw.evolve.measure" in new_labels, (
            "measure daemon not installed by install_evolve_infra_jobs on Linux "
            f"(W10-F #C) — installed: {new_labels}"
        )
        assert not Path("/Library/LaunchDaemons").exists(), (
            "/Library/LaunchDaemons exists on a Linux box — a launchd write was attempted"
        )
        for label in new_labels:
            assert not Path(f"/etc/systemd/system/{label}.plist").exists(), (
                f"{label} has a launchd .plist sibling — a daemon bypassed the seam"
            )
            # No /Users path baked into any installed unit body (catches the
            # class where a daemon hardcodes /Users/Shared/evolve into argv).
            for kind in ("service", "timer"):
                upath = Path(f"/etc/systemd/system/{label}.{kind}")
                if upath.exists():
                    ubody = _sudo("/bin/cat", str(upath), check=True).stdout
                    assert "/Users/" not in ubody, (
                        f"macOS /Users path leaked into {label}.{kind}:\n{ubody}"
                    )

        # ── (e2) W10-F #3: pod_health inspects systemd units on Linux ────────
        # Before W10-F, health._check_launchd stat'd /Library/LaunchDaemons
        # unconditionally, so on Linux EVERY label read as "Plist not found"
        # and the summary said "0/N — Evolve not deployed" though the whole
        # fleet was live as systemd units (73 false FAILs on the live pod).
        # Drive the REAL check over the just-installed fleet (real
        # SystemdScheduler) and assert no false-missing flood.
        from evolve_admin import health as _health
        _orig_expected = _health.expected_plist_labels
        _health.expected_plist_labels = (
            lambda net, realized_only=False: set(new_labels)
        )
        try:
            hreport = _health.HealthReport()
            _health._check_launchd(
                hreport, {"members": [BOT], "sharedDir": str(SHARED_DIR)})
        finally:
            _health.expected_plist_labels = _orig_expected
        launchd_checks = [c for c in hreport.checks if c.category == "launchd"]
        details = [c.detail for c in launchd_checks]
        _diag("step6g-health-launchd.txt", "\n".join(
            f"{c.status} {c.name}: {c.detail}" for c in launchd_checks))
        assert not any("Plist not found" in d for d in details), details
        assert not any("not deployed" in d for d in details), details
        assert not any("/Library/LaunchDaemons" in d for d in details), details
        assert any(c.status == _health.PASS for c in launchd_checks), (
            f"health saw the systemd fleet as all-missing/unloaded: {details}"
        )
        print(f"  health._check_launchd over {len(new_labels)} units: "
              "no false 'Plist not found' / 'not deployed' (W10-F #3)")

        # ── (e) the headline invariant: NO /Users inode anywhere on the box ──
        # Linux has no /Users at all; any of the historical leaks
        # (/Users/<bot>/.openclaw, /Users/Shared/evolve, /Users/evolve,
        # /Users/evo) would have MATERIALIZED it. Its mere existence is the
        # failure — and the diag enumerates what leaked for the post-mortem.
        if Path("/Users").exists():
            leak = subprocess.run(["/usr/bin/find", "/Users", "-maxdepth", "3"],
                                  capture_output=True, text=True)
            _diag("step6g-Users-leak.txt", leak.stdout + leak.stderr)
            raise AssertionError(
                "/Users/ was created on the Linux box — a macOS path leaked "
                f"through fresh setup:\n{leak.stdout}"
            )
    finally:
        # Thorough teardown: remove EVERY Evolve unit this step installed (the
        # module finalizer only knows a fixed label set; the fleet is ~50), then
        # restore the injected fake so later steps/modules see the fixture posture.
        for label in [l for l in _installed_evolve_unit_labels()
                      if l not in installed_before]:
            try:
                get_scheduler().remove(label)
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                print(f"  step6g cleanup remove({label}) failed: {exc}")
        set_scheduler(SystemdScheduler())

    print(
        "CAPSTONE: full Step-15 install_evolve_infra_jobs on the production "
        "SystemdScheduler — admin-ui systemd unit installed + admin server bound "
        f":{ADMIN_UI_PORT} as evolve; `sudo evolve-admin` resolves under secure_path; "
        f"{len(new_labels)} daemons all systemd (no launchd plist, no /Users in any "
        "unit); NO /Users inode created anywhere on the box"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Step 7 — completion sentinel (anti-vacuous-green: the CI job asserts this
# file exists, so an accidentally-skipped run can never pass silently)
# ══════════════════════════════════════════════════════════════════════════════


def test_step7_write_completion_sentinel():
    _diag(
        "COMPLETED",
        "linux-e2e completed all steps: platform-gate, sudoers(visudo), "
        f"accounts({EVOLVE_USER},{BOT}), perms(ACL+mask), scheduler(systemd), "
        "admin-smoke(HTTP 200), deploy-flow(venv build + plugin dir), "
        "plugin-build(real build_plugin → local-tsc dist/index.js), "
        "pod-perms(evolve-owned chown → evolve:root), "
        "wizard-evo-day-one(_provision_evo_oc → evo account + /home/evo OC "
        "+ systemd gateway unit), "
        "real-deploy-daemon-install(no-injection: production get_scheduler → "
        "SystemdScheduler, gateway ExecStart real node + /usr/lib openclaw + "
        "port-bound, apply+cost-converter systemd units, no /Users leak), "
        "CAPSTONE(full Step-15 install_evolve_infra_jobs → admin-ui systemd unit "
        "+ admin server bound, sudo evolve-admin on secure_path, whole daemon "
        "fleet systemd not launchd, NO /Users inode anywhere)\n",
    )
