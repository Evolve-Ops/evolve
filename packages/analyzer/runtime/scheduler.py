"""Scheduler seam — JobSpec, the platform renderers, and the adapters.

Design: docs/design-phase4.3c-S0-plist-consolidation.md. Every launchd
plist evolve_admin writes is described as a :class:`JobSpec` and rendered
by :func:`render_launchd_plist` — never hand-format plist XML at a call
site. The renderer goes through :mod:`plistlib`, so escaping and value
typing (int vs string vs bool) are correct by construction; consumers
assert parity with parsed-dict equality, not byte equality.

The same :class:`JobSpec` renders to systemd units via
:func:`render_systemd_units` (Linux port L1,
docs/design-linux-port-2026-06-10.md §3); :class:`SystemdScheduler` is
the second real :class:`Scheduler` adapter. Golden tests:
packages/admin/tests/test_systemd_jobspec_golden.py — never hand-format
unit ini at a call site either.

Home: this module lives in the analyzer package (beside
``runtime.agent_runtime``) so analyzer-side S2 migrations never import
``evolve_admin`` — admin depends on analyzer, never the reverse.
``evolve_admin.runtime`` re-exports the public surface as a
compatibility shim; the singleton state lives here only.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import shlex
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple, Protocol, runtime_checkable

# Module-level, mirroring runtime.perms (which keys get_perms off the same
# profile): platform_profile is the analyzer-side sibling leaf, so this import
# stays inside the package and never reaches into evolve_admin.
from platform_profile import get_profile

_log = logging.getLogger(__name__)


# ── sudo escalation classification ───────────────────────────────────────────
# Moved here from infra_audit (PR #1579 origin) so the adapters can classify
# their own subprocess failures: status() callers need "sudo couldn't
# escalate" (tooling failure) kept distinguishable from "service not found"
# (authoritative finding) without dropping to raw(). infra_audit and
# oc_log_rotate import this via the evolve_admin.runtime shim.
_SUDO_ESCALATION_MARKERS = (
    "a terminal is required to read the password",
    "a password is required",
    "no tty present and no askpass program",
    "sudo: unknown user",
    "is not in the sudoers file",
)


def is_sudo_escalation_error(stderr: str) -> bool:
    """Did sudo fail to escalate, or did the inner command fail?

    Returns True only when the stderr looks like a sudo-side failure
    (missing NOPASSWD grant, no TTY, password required). False for any
    other failure shape — including the inner command's own diagnostics.
    """
    if not stderr:
        return False
    low = stderr.lower()
    return any(m in low for m in _SUDO_ESCALATION_MARKERS)


@dataclass
class JobSpec:
    """Declarative description of one launchd job (daemon or agent).

    Field semantics map 1:1 onto launchd keys except:

    - ``user``/``group_name`` → ``UserName``/``GroupName``. System-domain
      LaunchDaemons need these to run as a non-root user; user-domain
      LaunchAgents inherit the session user and may leave them unset.
    - ``jitter_seconds`` > 0 wraps the program in
      ``/bin/bash -c "sleep $((RANDOM % N)); exec <cmd>"`` so daemons that
      share a StartInterval boundary don't all fire in lockstep. macOS
      launchd does not jitter StartInterval, and same-interval installs
      across N bots fire synchronously — the resulting process-spawn storm
      has wedged the mini's userland before (sshd hung at banner exchange).
    - ``start_calendar`` accepts either a single dict (one calendar rule)
      or a list of dicts (multiple firing times) — launchd allows both
      shapes for ``StartCalendarInterval`` and they parse differently, so
      the spec preserves whichever the caller built.
    - ``extra`` is a migration-only escape hatch for launchd keys not yet
      modelled here. It logs a warning when used so leftover debt stays
      visible; if a key is load-bearing, model it as a real field instead.
    """

    label: str
    program_args: list[str]
    user: str | None = None
    group_name: str | None = None
    keep_alive: bool | dict = False      # bool or launchd {SuccessfulExit: …}
    run_at_load: bool = False
    start_interval: int | None = None    # seconds
    start_calendar: dict | list[dict] | None = None
    watch_paths: list[str] | None = None  # launchd WatchPaths — fire on file change
    working_dir: str | None = None
    env: dict[str, str] | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    jitter_seconds: int = 0
    comment: str | None = None
    throttle_interval: int | None = None  # seconds; launchd ThrottleInterval
    umask: int | None = None              # decimal int, e.g. 63 == 0o077
    # Soft open-file-descriptor cap for the job's process. launchd:
    # ``SoftResourceLimits.NumberOfFiles``; systemd: ``LimitNOFILE=``.
    # launchd's macOS default soft NOFILE is 256 — the admin daemon hit
    # EMFILE storms under werkzeug thread amplification (2026-07-28
    # incident) and permanently lost its unix-socket accept loop.
    soft_file_limit: int | None = None
    extra: dict | None = None             # migration-only escape hatch


def is_timer_activated_oneshot(spec: JobSpec) -> bool:
    """True when ``spec`` is a batch job the scheduler *starts on a schedule*
    and that exits — as opposed to a long-running supervised daemon.

    The discriminator lives here, at the JobSpec level, so launchd and systemd
    agree by construction: it is the spec-side twin of
    :meth:`SystemdScheduler._activation_units`, which asks the same question of
    the rendered unit set (a timer owns activation ⇒ the service does not).
    Deciding this inside one scheduler adapter would let the two platforms
    drift, and the pod fleet runs both (macOS mini + Linux VPS).

    Deliberately narrow — a spec qualifies only when ALL hold:

    - it carries a schedule (``start_interval`` or ``start_calendar``);
    - it is not supervised (``keep_alive is False`` — the same
      ``keep_alive is not False`` daemon test ``_activation_units`` uses, so a
      launchd ``{SuccessfulExit: …}`` dict counts as a daemon);
    - it is not path-activated (``watch_paths`` unset).

    Anything else — gateways, admin-ui, mcp-bridge, signal-subscriber,
    better-engine, WatchPaths jobs, run-once-at-load jobs — is NOT a timer
    oneshot and keeps every existing bounce/restart behaviour.

    ``run_at_load`` is intentionally NOT part of the test. It says "also run
    once when the job is loaded"; on a redeploy whose unit is byte-identical
    no load happens, so no run is owed. Treating ``run_at_load=True`` as
    daemon-shaped would leave most of the fleet's monitors force-run.
    """
    if spec.keep_alive is not False:
        return False
    if spec.watch_paths:
        return False
    return spec.start_interval is not None or spec.start_calendar is not None


def render_launchd_plist(spec: JobSpec) -> str:
    """Render ``spec`` to launchd plist XML.

    Pure — no filesystem or launchctl access. ``RunAtLoad`` is always
    emitted explicitly (even when False) so "intentionally off" and
    "forgot to set it" stay distinguishable during incident response —
    see pod-health-invariants.H7. Optional keys are omitted when unset,
    matching launchd's defaults.
    """
    if not spec.label:
        raise ValueError("JobSpec.label is required")
    if not spec.program_args:
        raise ValueError("JobSpec.program_args must be a non-empty list of strings")
    for arg in spec.program_args:
        if not isinstance(arg, str):
            raise ValueError(
                f"JobSpec.program_args entries must be strings, got {type(arg).__name__}"
            )

    program_args = list(spec.program_args)
    if spec.jitter_seconds and spec.jitter_seconds > 0:
        cmd = " ".join(shlex.quote(a) for a in program_args)
        program_args = [
            "/bin/bash",
            "-c",
            f"sleep $((RANDOM % {int(spec.jitter_seconds)})); exec {cmd}",
        ]

    payload: dict[str, object] = {"Label": spec.label}
    if spec.comment:
        payload["Comment"] = spec.comment
    payload["ProgramArguments"] = program_args
    if spec.user:
        payload["UserName"] = spec.user
    if spec.group_name:
        payload["GroupName"] = spec.group_name
    if spec.umask is not None:
        payload["Umask"] = int(spec.umask)
    if spec.working_dir:
        payload["WorkingDirectory"] = spec.working_dir
    payload["RunAtLoad"] = bool(spec.run_at_load)
    if spec.keep_alive is not False:
        payload["KeepAlive"] = spec.keep_alive
    if spec.throttle_interval is not None:
        payload["ThrottleInterval"] = int(spec.throttle_interval)
    if spec.start_interval is not None:
        payload["StartInterval"] = int(spec.start_interval)
    if spec.start_calendar is not None:
        payload["StartCalendarInterval"] = spec.start_calendar
    if spec.watch_paths:
        payload["WatchPaths"] = [str(p) for p in spec.watch_paths]
    if spec.stdout_path:
        payload["StandardOutPath"] = spec.stdout_path
    if spec.stderr_path:
        payload["StandardErrorPath"] = spec.stderr_path
    if spec.env:
        payload["EnvironmentVariables"] = {str(k): str(v) for k, v in spec.env.items()}
    if spec.soft_file_limit is not None:
        payload["SoftResourceLimits"] = {"NumberOfFiles": int(spec.soft_file_limit)}

    if spec.extra:
        _log.warning(
            "render_launchd_plist(%s): unmodelled launchd keys via extra=%s — "
            "model load-bearing keys as real JobSpec fields",
            spec.label, sorted(spec.extra),
        )
        payload.update(spec.extra)

    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


# ── S1: the Scheduler seam ───────────────────────────────────────────────────
#
# Design: docs/design-phase4.3c-scheduler-isolation-seams-2026-06-10.md.
# Mirrors the AgentRuntime seam (agent_runtime.py, this package): a Protocol,
# one real adapter, and a process-wide get/set singleton for test injection.
# S2 migrates the direct ``launchctl`` call sites onto ``get_scheduler()``;
# until then both paths coexist by design.
#
# ⚠️ ``restart`` (kickstart -k) and ``remove`` (bootout) are LIVE-TRAFFIC
# DESTRUCTIVE — they restart/stop running bot gateways. Tests must NEVER
# reach a real ``launchctl`` for mutating verbs: inject a fake ``runner``.


class InstallResult(NamedTuple):
    """Outcome of :meth:`Scheduler.install`.

    A NamedTuple so adapters and callers stay tuple-shaped (``res[0]`` is
    ``ok``, the whole result is truthy iff non-empty — always), but the
    idempotent-skip path is a FIELD, not a message substring: callers that
    need "did this actually (re)register the job?" key on ``res.skipped``,
    never on parsing ``res.message``.
    """

    ok: bool
    message: str
    skipped: bool = False  # True ⇒ byte-identical plist, no bounce performed


@runtime_checkable
class Scheduler(Protocol):
    """Manage recurring/long-running jobs, platform-neutrally.

    The launchd adapter is :class:`LaunchdScheduler`; a systemd/cron adapter
    implements the same closed surface (the 6 operations every launchctl
    call site in the codebase reduces to, plus ``running`` as a convenience
    over ``status``).
    """

    # ``timeout`` is an OPTIONAL per-call override of the adapter's
    # constructor timeout (None ⇒ the constructor value — byte-identical to
    # the pre-enrichment behavior). It lets daemon sites that need a bounded
    # timeout (e.g. repo-puller's sequential kickstarts) migrate onto the
    # process-wide ``get_scheduler()`` singleton, which is built with the
    # 30 s default and so cannot carry a per-site timeout any other way.
    def install(self, spec: JobSpec, *, timeout: "float | None" = None) -> "InstallResult": ...   # render + bootstrap/load
    def remove(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]": ...     # bootout/unload (+ rm plist)
    # disable/enable: PERSISTENT (reboot-surviving) pause + resume that leaves
    # the artifact on disk — a plain bootout/`disable --now` is not enough on
    # macOS because /Library/LaunchDaemons plists auto-load at the next boot.
    # The inverse pair for pause/archive ⇄ unpause/restore of a scheduled job.
    def disable(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]": ...    # persistent disable + stop now
    def enable(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]": ...     # clear disable + (re)start
    def restart(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]": ...    # kickstart -k
    def list(self, *, prefix: "str | None" = None, timeout: "float | None" = None) -> "list[str]": ...
    def status(self, label: str, *, timeout: "float | None" = None) -> dict: ...                   # {installed, managed, running, pid, last_exit, status_error}
    def running(self, label: str) -> bool: ...
    def supervision(self, label: str, *, timeout: "float | None" = None) -> dict: ...              # {n_restarts, sub_state, active_state, pid, last_exit, env, status_error}
    def kill(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]": ...
    def artifact_path(self, label: str) -> str: ...                                                # primary on-disk artifact install() writes for <label>


# argv → (returncode, stdout, stderr). The ONLY seam in LaunchdScheduler
# that may spawn a subprocess; tests inject a fake and no process is ever
# created. stdout is kept separate from stderr because the bootout-settle
# wait keys on EMPTY STDOUT from ``launchctl print`` (Apple's CLI returns
# rc=0 whether or not the service exists, so rc is not a usable signal —
# see deploy._wait_for_launchd_unload).
LaunchctlRunner = Callable[[list[str]], "tuple[int, str, str]"]


def _subprocess_runner(argv: list[str], timeout: float = 30.0) -> "tuple[int, str, str]":
    """Default runner — subprocess with the same guardrails as
    ``service.py::_launchctl`` (timeout, exception → (1, "", str(e)))."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # noqa: BLE001 — adapter boundary: report, don't raise
        return 1, "", str(e)


class LaunchdScheduler:
    """The launchd adapter — generalizes ``service.py::_launchctl`` semantics
    (bootstrap / bootout / kickstart -k / print / list) and the
    ``deploy.py::_write_plist`` install ritual (/tmp staging + sudo /bin/cp +
    chown root:wheel + chmod 644 + bootout-settle + bootstrap, with the
    byte-identical short-circuit so repeat installs don't bounce daemons).

    Defaults target the pod standard: system domain, /Library/LaunchDaemons,
    sudo (full binary paths per the macOS-paths table in CLAUDE.md). A
    user-domain LaunchAgent instance is ``LaunchdScheduler(domain=f"gui/{uid}",
    plist_dir=Path.home()/"Library/LaunchAgents", use_sudo=False)``.
    """

    def __init__(
        self,
        *,
        domain: str = "system",
        plist_dir: "Path | str" = "/Library/LaunchDaemons",
        use_sudo: bool = True,
        sudo_non_interactive: bool = False,
        runner: "LaunchctlRunner | None" = None,
        unload_timeout: float = 5.0,
        timeout: float = 30.0,
    ) -> None:
        self.domain = domain
        self.plist_dir = Path(plist_dir)
        self.use_sudo = use_sudo
        # sudo -n: fail fast instead of blocking on a password prompt.
        # Probes that run in a daemon context (no TTY, e.g. the infra audit)
        # rely on this to classify "sudo couldn't escalate" as a tooling
        # failure rather than hanging — see auto-memory:
        # feedback_distinguish_tooling_failure_from_findings.
        self.sudo_non_interactive = sudo_non_interactive
        # Constructor timeout: the runner's closed-over default. Kept as a
        # field so a per-call ``timeout=`` override can replace it for one
        # invocation (see ``_run``). When a custom ``runner`` is injected
        # (tests, the as-bot runner), the per-call override is ignored — the
        # runner is a 1-arg ``Callable[[list[str]], …]`` and owns its own
        # timeout posture; this preserves every existing fake-runner test.
        self._timeout = timeout
        self._custom_runner = runner is not None
        self._runner = runner or (lambda argv: _subprocess_runner(argv, timeout=timeout))
        self.unload_timeout = unload_timeout

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _run(self, argv: "list[str]", timeout: "float | None") -> "tuple[int, str, str]":
        """Single chokepoint for every subprocess the adapter spawns.

        ``timeout=None`` ⇒ the constructor value (byte-identical to the
        pre-enrichment behavior). A non-None override only takes effect on
        the DEFAULT subprocess runner; an injected custom runner is called
        1-arg exactly as before (backward compat — see ``__init__``)."""
        if timeout is not None and not self._custom_runner:
            return _subprocess_runner(argv, timeout=timeout)
        return self._runner(argv)

    def _launchctl(self, *args: str, timeout: "float | None" = None) -> "tuple[int, str]":
        """Run launchctl (sudo'd per config); (rc, combined stripped output)."""
        rc, out, err = self._launchctl_raw(*args, timeout=timeout)
        return rc, (out + err).strip()

    def _launchctl_raw(self, *args: str, timeout: "float | None" = None) -> "tuple[int, str, str]":
        if self.use_sudo:
            prefix = ["sudo", "-n"] if self.sudo_non_interactive else ["sudo"]
        else:
            prefix = []
        return self._run([*prefix, "/bin/launchctl", *args], timeout)

    def raw(self, *args: str) -> "tuple[int, str, str]":
        """Adapter-specific escape hatch: run a verbatim launchctl
        subcommand through the seam's runner; ``(rc, stdout, stderr)``.

        For launchd operations that don't map onto the closed verb surface
        behavior-preservingly: a bare ``bootout`` that must NOT delete the
        plist (:meth:`remove` would), ``bootstrap`` of a plist staged by the
        caller (:meth:`install` owns the write AND skips byte-identical
        plists — wrong for flows that must re-register an unloaded job),
        and domain-level ``print`` probes. S2-migration debt — prefer the
        verbs; each call site carries a comment saying why it can't.

        ``raw()`` deliberately takes NO per-call ``timeout`` — it stays
        launchd-only debt; the per-call timeout lives only on the closed
        Protocol verbs.
        """
        return self._launchctl_raw(*args)

    def _sudo(self, *args: str, timeout: "float | None" = None) -> "tuple[int, str]":
        rc, out, err = self._run(["sudo", *args], timeout)
        return rc, (out + err).strip()

    def _plist_path(self, label: str) -> Path:
        return self.plist_dir / f"{label}.plist"

    def artifact_path(self, label: str) -> str:
        """The plist :meth:`install` writes for ``label`` — what callers
        stamp as ``installed_artifact`` (manifest audit trail, orphan sweep,
        pod-health expected set)."""
        return str(self._plist_path(label))

    def _wait_for_unload(self, label: str, *, timeout: "float | None" = None) -> None:
        """Poll until launchd reports the label gone after ``bootout``.

        bootout returns before tear-down completes; an immediate bootstrap
        of the same label fails with "Service is being unloaded". Empty
        stdout from ``launchctl print`` is the gone-signal (rc is 0 in both
        states on macOS). Bounded so a stuck service can't wedge a deploy.
        """
        deadline = time.monotonic() + self.unload_timeout
        while True:
            _rc, out, _err = self._launchctl_raw("print", f"{self.domain}/{label}", timeout=timeout)
            if not out.strip():
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.1)

    # ── the six operations ───────────────────────────────────────────────────

    def install(self, spec: JobSpec, *, timeout: "float | None" = None) -> InstallResult:
        """Render ``spec``, write the plist, (re)register with launchd.

        Skips the write + bootout/bootstrap cycle when the on-disk plist is
        byte-identical — repeat installs must not bounce a healthy daemon
        (same contract as ``deploy._write_plist``). The skip is reported as
        ``InstallResult.skipped`` — callers that need a restart-happens
        guarantee key on that field.

        ``timeout`` optionally bounds each subprocess this install spawns
        (None ⇒ the constructor value).
        """
        content = render_launchd_plist(spec)
        dst = self._plist_path(spec.label)
        try:
            if dst.exists() and dst.read_text() == content:
                return InstallResult(
                    True, f"Up-to-date: {spec.label} (skipped reinstall)", skipped=True
                )
        except (OSError, PermissionError) as e:
            # Unreadable → fall through; the privileged write below wins.
            _log.debug("install(%s): cannot read existing plist (%s)", spec.label, e)

        if self.use_sudo:
            fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".plist")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                rc, out = self._sudo("/bin/cp", tmp, str(dst), timeout=timeout)
                if rc != 0:
                    return InstallResult(False, f"plist write failed: {out}")
            finally:
                try:
                    os.unlink(tmp)
                except OSError as e:
                    _log.debug("install(%s): staged tmp cleanup failed (%s)", spec.label, e)
            # launchd refuses non-root-owned / group-writable system plists —
            # surface ownership failures instead of letting bootstrap fail
            # with a less actionable error.
            rc, out = self._sudo("/usr/sbin/chown", "root:wheel", str(dst), timeout=timeout)
            if rc != 0:
                return InstallResult(False, f"plist chown failed: {out}")
            rc, out = self._sudo("/bin/chmod", "644", str(dst), timeout=timeout)
            if rc != 0:
                return InstallResult(False, f"plist chmod failed: {out}")
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(content)
            except OSError as e:
                return InstallResult(False, f"plist write failed: {e}")

        # Re-register: bootout is expected to fail when not loaded (fresh
        # install) — ignore its rc, then wait for the unload to settle.
        self._launchctl("bootout", f"{self.domain}/{spec.label}", timeout=timeout)
        self._wait_for_unload(spec.label, timeout=timeout)
        rc, out = self._launchctl("bootstrap", self.domain, str(dst), timeout=timeout)
        if rc != 0:
            return InstallResult(False, f"launchctl bootstrap failed (rc={rc}): {out}")
        return InstallResult(True, f"Installed: {spec.label}")

    def remove(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        """Bootout + delete the plist. Idempotent — removing a job that was
        never installed succeeds. ``timeout`` optionally bounds each
        subprocess (None ⇒ the constructor value)."""
        self._launchctl("bootout", f"{self.domain}/{label}", timeout=timeout)  # rc ignored: not-loaded is fine
        dst = self._plist_path(label)
        if not dst.exists():
            return True, f"Removed (no plist on disk): {label}"
        if self.use_sudo:
            rc, out = self._sudo("/bin/rm", str(dst), timeout=timeout)
            if rc != 0:
                return False, f"plist delete failed: {out}"
        else:
            try:
                dst.unlink()
            except OSError as e:
                return False, f"plist delete failed: {e}"
        return True, f"Removed: {label}"

    def disable(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        """Persistently pause the job: ``launchctl disable`` (writes the
        override DB so the plist does NOT auto-load at the next boot) + a
        ``bootout`` to stop the currently-loaded instance now.

        A plain ``bootout`` is not enough: a ``/Library/LaunchDaemons`` plist
        re-loads on reboot, so an "archived"/"paused" app would resume firing.
        The plist file is left on disk (unlike :meth:`remove`) — :meth:`enable`
        reverses it. Idempotent: disabling an already-disabled / not-loaded
        label succeeds. ``timeout`` optionally bounds each subprocess (None ⇒
        the constructor value)."""
        rc, out = self._launchctl("disable", f"{self.domain}/{label}", timeout=timeout)
        if rc != 0:
            return False, f"launchctl disable failed (rc={rc}): {out}"
        # Stop the running instance; not-loaded is fine (rc ignored, same as
        # the bootout in install()/remove()).
        self._launchctl("bootout", f"{self.domain}/{label}", timeout=timeout)
        return True, f"Disabled: {label}"

    def enable(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        """Resume a :meth:`disable`-d job: ``launchctl enable`` (clears the
        override DB — a disabled service refuses to bootstrap) + a clean
        reload (bootout-settle + ``bootstrap`` from the on-disk plist, the
        same re-register ritual :meth:`install` uses).

        ``enable`` alone does not start the job — the reload does. When no
        plist is on disk the override is still cleared and success is
        reported (nothing to bootstrap; an idempotent no-op). Idempotent:
        enabling an already-running label reloads it. ``timeout`` optionally
        bounds each subprocess (None ⇒ the constructor value)."""
        rc, out = self._launchctl("enable", f"{self.domain}/{label}", timeout=timeout)
        if rc != 0:
            return False, f"launchctl enable failed (rc={rc}): {out}"
        dst = self._plist_path(label)
        if not dst.exists():
            return True, f"Enabled (no plist on disk to load): {label}"
        # Clean reload: bootout is expected to fail when not loaded (the
        # common paused→resumed case) — ignore its rc, wait for the unload
        # to settle, then bootstrap the (now-enabled) plist.
        self._launchctl("bootout", f"{self.domain}/{label}", timeout=timeout)
        self._wait_for_unload(label, timeout=timeout)
        rc, out = self._launchctl("bootstrap", self.domain, str(dst), timeout=timeout)
        if rc != 0:
            return False, f"launchctl bootstrap failed (rc={rc}): {out}"
        return True, f"Enabled: {label}"

    def restart(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        """⚠️ Destructive: ``kickstart -k`` kills the running instance (live
        gateway traffic included) and starts a fresh one. ``timeout``
        optionally bounds the subprocess (None ⇒ the constructor value)."""
        rc, out = self._launchctl("kickstart", "-k", f"{self.domain}/{label}", timeout=timeout)
        return rc == 0, out

    def list(self, *, prefix: "str | None" = None, timeout: "float | None" = None) -> "list[str]":
        """Labels registered in this domain (``launchctl list``), optionally
        filtered by prefix. Empty list on failure. ``timeout`` optionally
        bounds the subprocess (None ⇒ the constructor value)."""
        rc, out, _err = self._launchctl_raw("list", timeout=timeout)
        if rc != 0:
            return []
        labels: list[str] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[0] == "PID":  # header / malformed
                continue
            labels.append(parts[-1].strip())
        if prefix is not None:
            labels = [lab for lab in labels if lab.startswith(prefix)]
        return labels

    def status(self, label: str, *, timeout: "float | None" = None) -> dict:
        """``launchctl list <label>`` → ``{installed, managed, running, pid,
        last_exit, status_error}`` (the shape ``service.py::status``
        established, plus the probe-failure tri-state).

        installed: plist file on disk · managed: registered with launchd ·
        running: has a PID right now · last_exit: LastExitStatus as a string.

        status_error keeps tooling failures distinguishable from findings
        (the PR #1579 tri-state, previously only reachable via raw()):
        ``None`` when the probe was authoritative (loaded OR confirmed
        not-found), ``"cannot_escalate"`` when sudo couldn't escalate
        non-interactively, ``"error"`` for any other probe failure. When
        it is non-None, ``managed=False`` means "unknown", not "absent".

        ``timeout`` optionally bounds the probe subprocess (None ⇒ the
        constructor value).
        """
        base: dict = {
            "installed": self._plist_path(label).exists(),
            "managed": False,
            "running": False,
            "pid": None,
            "last_exit": None,
            "status_error": None,
        }
        rc, out, err = self._launchctl_raw("list", label, timeout=timeout)
        if rc != 0:
            # launchctl returns 113 when the service is not found.
            if rc == 113 or "Could not find service" in err:
                return base
            base["status_error"] = (
                "cannot_escalate" if is_sudo_escalation_error(err.strip())
                else "error"
            )
            return base
        base["managed"] = True
        for line in out.splitlines():
            line = line.strip().rstrip(";")
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip().strip('"')
            v = v.strip().strip('"')
            if k == "PID":
                try:
                    base["pid"] = int(v)
                    base["running"] = True
                except ValueError:
                    _log.debug("status(%s): non-integer PID %r", label, v)
            elif k == "LastExitStatus":
                base["last_exit"] = v
        return base

    def running(self, label: str) -> bool:
        return bool(self.status(label)["running"])

    def supervision(self, label: str, *, timeout: "float | None" = None) -> dict:
        """Crash-loop telemetry, normalized to the cross-platform shape
        ``{n_restarts, sub_state, active_state, pid, last_exit, env,
        status_error}``.

        launchd has **no restart counter and no auto-restart substate** the way
        systemd does, so ``n_restarts`` / ``sub_state`` / ``active_state`` are
        ``None`` here — the gateway-supervision consumer falls back to PID-churn
        tracking on this platform (a stable PID means healthy; a PID that
        changes every tick means a unit that cannot stay up). ``pid`` /
        ``last_exit`` / ``status_error`` come from the same ``launchctl list``
        probe ``status()`` uses; ``env`` is read from the plist's
        ``EnvironmentVariables`` so the consumer can detect two units declaring
        the same gateway port."""
        st = self.status(label, timeout=timeout)
        env: "dict[str, str]" = {}
        try:
            data = plistlib.loads(self._plist_path(label).read_bytes())
            raw = data.get("EnvironmentVariables")
            if isinstance(raw, dict):
                env = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError) as exc:
            # plist absent/unreadable (e.g. a loaded orphan) — env stays {}.
            _log.debug("supervision(%s): plist env read failed: %s", label, exc)
        return {
            "n_restarts": None,
            "sub_state": None,
            "active_state": None,
            "pid": st.get("pid"),
            "last_exit": st.get("last_exit"),
            "env": env,
            "status_error": st.get("status_error"),
        }

    def kill(
        self, label: str, *, signal: str = "SIGTERM", timeout: "float | None" = None
    ) -> "tuple[bool, str]":
        """Signal the job's running process (default SIGTERM). The job stays
        registered; KeepAlive jobs will be respawned by launchd. ``timeout``
        optionally bounds the subprocess (None ⇒ the constructor value)."""
        rc, out = self._launchctl("kill", signal, f"{self.domain}/{label}", timeout=timeout)
        return rc == 0, out


# ── the swappability artifacts (S3) ──────────────────────────────────────────
#
# Phase-D pattern, like ``FakeRuntime`` for the AgentRuntime seam and
# ``FakeIsolation`` for Track I: an in-memory fake proves call paths run with
# zero subprocesses, and a second-platform stub proves the Protocol closes
# over a real port target. Proof tests:
# packages/admin/tests/test_scheduler_swappability.py.


class FakeScheduler:
    """In-memory adapter for tests — the full ``Scheduler`` verb surface,
    zero launchd / zero subprocess.

    ``jobs`` maps label → installed :class:`JobSpec`. Semantics mirror
    ``LaunchdScheduler`` wherever a call site can observe them:

    - ``install`` validates the spec through :func:`render_launchd_plist`
      (same ``ValueError`` contract) and short-circuits with
      ``InstallResult.skipped=True`` when the rendered content is identical
      to the installed job's — the byte-identical-plist contract, computed
      by the same renderer so the skip semantics can never drift.
    - a non-skipped ``install`` (re)registers the job; it starts running
      iff ``run_at_load``/``keep_alive`` say launchd would start it.
    - ``status`` returns exactly the ``LaunchdScheduler`` shape:
      ``{installed, managed, running, pid, last_exit, status_error}``;
      seed a probe tri-state via ``seed_job(..., status_error=...)`` or
      ``status_errors[label]``.
    - ``kill`` stops the process; ``keep_alive`` jobs respawn with a new
      pid, as launchd would.

    Deliberately does NOT implement ``raw()`` — that is the point.
    ``raw()`` runs verbatim launchctl subcommands no other platform's
    adapter can honor, so it is not on the ``Scheduler`` Protocol; a call
    path that needs it cannot run against this fake
    (``get_launchd_scheduler()`` raises) and is, by definition, S2
    migration debt. The frozen census in
    ``packages/admin/tests/test_launchctl_seam_gates.py`` is that worklist.

    Mutating calls record into ``self.calls``.
    """

    def __init__(self) -> None:
        self.jobs: "dict[str, JobSpec]" = {}
        self.pids: "dict[str, int | None]" = {}
        self.last_exits: "dict[str, str | None]" = {}
        self.status_errors: "dict[str, str | None]" = {}
        # Per-label supervision() overrides (n_restarts / sub_state /
        # active_state / env). Seeded by ``seed_supervision``; anything not
        # seeded falls back to defaults derived from the live pid/last_exit.
        self.supervisions: "dict[str, dict]" = {}
        self.calls: "list[tuple]" = []
        # Labels persistently disabled via disable() — a disabled job stays
        # installed (plist on disk) but is stopped and won't start on load
        # until enable() clears it, mirroring launchctl's override DB.
        self.disabled: "set[str]" = set()
        self._rendered: "dict[str, str]" = {}
        self._next_pid = 1000

    def seed_job(
        self, spec: JobSpec, *,
        running: "bool | None" = None, last_exit: "str | None" = None,
        status_error: "str | None" = None,
    ) -> None:
        """Register a pre-existing job without recording an install call
        (test arrangement; ``running=None`` = launchd's start-on-load rule)."""
        self.jobs[spec.label] = spec
        self._rendered[spec.label] = render_launchd_plist(spec)
        start = self._starts_on_load(spec) if running is None else running
        self.pids[spec.label] = self._alloc_pid() if start else None
        self.last_exits[spec.label] = last_exit
        self.status_errors[spec.label] = status_error

    def seed_supervision(
        self, label: str, *,
        n_restarts: "int | None" = None,
        sub_state: "str | None" = None,
        active_state: "str | None" = None,
        env: "dict | None" = None,
    ) -> None:
        """Arrange the values ``supervision(label)`` reports (test setup).

        Only the fields passed are pinned; ``pid`` / ``last_exit`` /
        ``status_error`` always reflect the live fake state so PID-churn tests
        can flip the pid via ``kill``/``install`` and watch supervision track
        it."""
        self.supervisions[label] = {
            "n_restarts": n_restarts,
            "sub_state": sub_state,
            "active_state": active_state,
            "env": dict(env or {}),
        }

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _alloc_pid(self) -> int:
        self._next_pid += 1
        return self._next_pid

    @staticmethod
    def _starts_on_load(spec: JobSpec) -> bool:
        return bool(spec.run_at_load) or spec.keep_alive is not False

    # ── the verb surface ─────────────────────────────────────────────────────

    def artifact_path(self, label: str) -> str:
        """Protocol parity: a launchd-shaped artifact path (the fake mirrors
        ``LaunchdScheduler`` semantics; nothing is written to disk)."""
        return f"/Library/LaunchDaemons/{label}.plist"

    # ``timeout`` is accepted (Protocol parity) and ignored — the fake
    # never spawns a subprocess, so there is nothing to bound.
    def install(self, spec: JobSpec, *, timeout: "float | None" = None) -> InstallResult:
        self.calls.append(("install", spec.label))
        content = render_launchd_plist(spec)  # same validation as the real adapter
        if self._rendered.get(spec.label) == content:
            return InstallResult(
                True, f"Up-to-date: {spec.label} (skipped reinstall)", skipped=True
            )
        self.jobs[spec.label] = spec
        self._rendered[spec.label] = content
        self.last_exits.setdefault(spec.label, None)
        self.pids[spec.label] = self._alloc_pid() if self._starts_on_load(spec) else None
        return InstallResult(True, f"Installed: {spec.label}")

    def remove(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        self.calls.append(("remove", label))
        existed = self.jobs.pop(label, None) is not None
        self._rendered.pop(label, None)
        self.pids.pop(label, None)
        self.last_exits.pop(label, None)
        self.disabled.discard(label)
        if not existed:
            return True, f"Removed (no plist on disk): {label}"  # idempotent
        return True, f"Removed: {label}"

    def disable(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        self.calls.append(("disable", label))
        if label not in self.jobs:
            return True, f"Disabled (no plist on disk): {label}"  # idempotent
        self.disabled.add(label)
        self.pids[label] = None  # bootout stops the running instance
        return True, f"Disabled: {label}"

    def enable(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        self.calls.append(("enable", label))
        if label not in self.jobs:
            return True, f"Enabled (no plist on disk to load): {label}"  # idempotent
        self.disabled.discard(label)
        # The reload starts it iff launchd's start-on-load rule would.
        self.pids[label] = self._alloc_pid() if self._starts_on_load(self.jobs[label]) else None
        return True, f"Enabled: {label}"

    def restart(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        self.calls.append(("restart", label))
        if label not in self.jobs:
            # kickstart of an unregistered label fails (launchctl rc=113)
            return False, f"Could not find service: {label}"
        self.pids[label] = self._alloc_pid()  # -k: kill + fresh instance
        return True, ""

    def list(self, *, prefix: "str | None" = None, timeout: "float | None" = None) -> "list[str]":
        labels = list(self.jobs)
        if prefix is not None:
            labels = [lab for lab in labels if lab.startswith(prefix)]
        return labels

    def status(self, label: str, *, timeout: "float | None" = None) -> dict:
        status_error = self.status_errors.get(label)
        if status_error:
            # Probe failure: like the real adapters, nothing else is knowable.
            return {
                "installed": label in self.jobs,
                "managed": False,
                "running": False,
                "pid": None,
                "last_exit": None,
                "status_error": status_error,
            }
        managed = label in self.jobs
        pid = self.pids.get(label)
        return {
            "installed": managed,  # the fake has no separate plist-on-disk axis
            "managed": managed,
            "running": pid is not None,
            "pid": pid,
            "last_exit": self.last_exits.get(label),
            "status_error": None,
        }

    def running(self, label: str) -> bool:
        return bool(self.status(label)["running"])

    def supervision(self, label: str, *, timeout: "float | None" = None) -> dict:
        seeded = self.supervisions.get(label, {})
        st = self.status(label)
        return {
            "n_restarts": seeded.get("n_restarts"),
            "sub_state": seeded.get("sub_state"),
            "active_state": seeded.get("active_state"),
            "pid": st.get("pid"),
            "last_exit": st.get("last_exit"),
            "env": dict(seeded.get("env") or {}),
            "status_error": st.get("status_error"),
        }

    def kill(
        self, label: str, *, signal: str = "SIGTERM", timeout: "float | None" = None
    ) -> "tuple[bool, str]":
        self.calls.append(("kill", label, signal))
        if label not in self.jobs:
            return False, f"Could not find service: {label}"
        if self.jobs[label].keep_alive is not False:
            self.pids[label] = self._alloc_pid()  # launchd respawns KeepAlive jobs
        else:
            self.pids[label] = None
        return True, ""


# ── the systemd adapter (Linux port L1 / W3A) ────────────────────────────────
#
# Design: docs/design-linux-port-2026-06-10.md §3 — the field-by-field
# JobSpec→systemd map. Each JobSpec renders to a system-domain unit set in
# /etc/systemd/system: always a ``.service``, plus a ``.timer`` for
# interval/calendar jobs and a ``.path`` for watch_paths jobs. Labels stay
# verbatim as unit names (``ai.evolve.*`` is legal systemd naming), so the
# label↔unit mapping is deterministic in both directions.

# Linux full paths (Ubuntu 24.04, /usr-merge). Note /usr/bin/chown — the
# macOS location is /usr/sbin/chown; see design doc §6's command table.
_SYSTEMCTL = "/usr/bin/systemctl"
_LINUX_CP = "/bin/cp"
_LINUX_CHOWN = "/usr/bin/chown"
_LINUX_CHMOD = "/bin/chmod"
_LINUX_RM = "/bin/rm"
_LINUX_MKDIR = "/bin/mkdir"

# systemd unit names: label + one of these suffixes. ``.service`` always
# exists; ``.timer``/``.path`` exist iff the JobSpec has a schedule / watch.
_UNIT_KINDS = (".service", ".timer", ".path")

# Conservative charset for unit names (systemd allows a superset; every
# real label is reverse-DNS). Rejecting here keeps rendered ini injectable
# tokens out of file names and systemctl argv.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:@_-]*$")

# launchd StartCalendarInterval Weekday: 0 and 7 are Sunday, 1 is Monday.
_WEEKDAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_CALENDAR_KEYS = {"Minute", "Hour", "Day", "Month", "Weekday"}

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def systemd_unit_name(label: str, kind: str = "service") -> str:
    """Deterministic label→unit-name mapping: the label verbatim plus the
    unit-type suffix (design §3 keeps ``ai.evolve.*`` names unchanged)."""
    return f"{label}.{kind}"


def label_from_unit_name(unit: str) -> "str | None":
    """Inverse mapping: strip a known unit suffix; ``None`` for unit types
    the adapter never renders (sockets, mounts, …)."""
    for suffix in _UNIT_KINDS:
        if unit.endswith(suffix):
            return unit[: -len(suffix)]
    return None


def _no_newline(field: str, value: str) -> str:
    """Reject newlines in any rendered value — a value containing
    ``\\n[Service]…`` would inject keys into the unit file (ini has no
    escaping for this, unlike plistlib which is safe by construction)."""
    if "\n" in value or "\r" in value:
        raise ValueError(f"JobSpec.{field} must not contain newlines: {value!r}")
    return value


def _specifier_safe(field: str, value: str) -> str:
    """Newline guard + ``%`` → ``%%`` for unit settings that undergo
    specifier expansion (Description, WorkingDirectory, StandardOutput,
    PathModified, …) — a literal ``%`` must never reach systemd unescaped."""
    return _no_newline(field, value).replace("%", "%%")


def _exec_token(arg: str) -> str:
    """Quote one argv token for a systemd ``ExecStart=`` line.

    systemd's Exec parsing differs from shell quoting: ``%`` is a specifier
    (escaped as ``%%``), ``$`` is variable expansion (literal via ``$$`` —
    inside double quotes too), and double-quoted strings use C-style
    backslash escapes. Quote whenever any character could be split or
    expanded; over-quoting is harmless, under-quoting corrupts argv."""
    needs_quotes = arg == "" or any(c in arg for c in " \t'\";\\$")
    s = arg.replace("%", "%%").replace("$", "$$")
    if not needs_quotes:
        return s
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _env_assignment(key: str, value: str) -> str:
    """One ``Environment="KEY=value"`` assignment. ``%`` is specifier-
    expanded in Environment= (escape it); ``$`` is NOT expanded there
    (leave it literal)."""
    if not _ENV_KEY_RE.match(key):
        raise ValueError(f"JobSpec.env key is not a valid environment name: {key!r}")
    v = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{key}={v}"'


def _on_calendar(rule: dict) -> str:
    """One launchd StartCalendarInterval dict → an ``OnCalendar=`` expression.

    Missing components are wildcards on both platforms:
    ``{Hour: 7, Minute: 30}`` → ``*-*-* 07:30:00`` ·
    ``{Weekday: 1, Hour: 3, Minute: 30}`` → ``Mon *-*-* 03:30:00`` ·
    ``{Minute: 15}`` → ``*-*-* *:15:00`` (hourly).
    """
    unknown = set(rule) - _CALENDAR_KEYS
    if unknown:
        raise ValueError(
            f"start_calendar keys {sorted(unknown)} have no systemd OnCalendar "
            f"rendering — model them or fix the spec (supported: {sorted(_CALENDAR_KEYS)})"
        )
    month = f"{int(rule['Month']):02d}" if "Month" in rule else "*"
    day = f"{int(rule['Day']):02d}" if "Day" in rule else "*"
    hour = f"{int(rule['Hour']):02d}" if "Hour" in rule else "*"
    minute = f"{int(rule['Minute']):02d}" if "Minute" in rule else "*"
    expr = f"*-{month}-{day} {hour}:{minute}:00"
    if "Weekday" in rule:
        expr = f"{_WEEKDAY_NAMES[int(rule['Weekday']) % 7]} {expr}"
    return expr


def _restart_policy(keep_alive: "bool | dict") -> "str | None":
    """JobSpec.keep_alive → systemd ``Restart=`` value (None = don't emit).

    Shapes beyond the three the codebase uses fail loudly: silently
    rendering ``{PathState: …}`` as plain Restart would change semantics.
    """
    if keep_alive is False:
        return None
    if keep_alive is True:
        return "always"
    if isinstance(keep_alive, dict) and set(keep_alive) == {"SuccessfulExit"}:
        # launchd: restart iff the exit matched the stated success-ness.
        return "on-failure" if keep_alive["SuccessfulExit"] is False else "on-success"
    raise ValueError(
        f"keep_alive shape {keep_alive!r} has no systemd rendering — "
        "supported: True, False, {'SuccessfulExit': bool}"
    )


def render_systemd_units(spec: JobSpec) -> "dict[str, str]":
    """Render ``spec`` to its systemd unit file set: ``{filename: content}``.

    Pure — no filesystem or systemctl access (the systemd sibling of
    :func:`render_launchd_plist`). Always contains ``<label>.service``;
    adds ``<label>.timer`` when the spec has ``start_interval`` /
    ``start_calendar`` and ``<label>.path`` when it has ``watch_paths``.

    Field map (design doc §3) and the deliberate divergences:

    - ``keep_alive`` → ``Restart=always|on-failure|on-success`` +
      ``RestartSec`` (``throttle_interval`` if set, else 10 — launchd's
      default throttle) + ``StartLimitIntervalSec=0`` (launchd KeepAlive
      retries forever; systemd's default start-rate limit would mark the
      unit failed and stop respawning).
    - ``start_interval`` → ``OnBootSec`` + ``OnUnitActiveSec`` per the
      design. ``run_at_load`` folds into ``OnBootSec=0``. Nuance: a
      monotonic timer whose ``OnBootSec`` already elapsed fires once
      immediately when the timer starts, so installing an interval job on
      a long-running host runs it once at install (launchd waited N
      seconds). Our interval jobs are idempotent sweeps; accepted.
    - ``jitter_seconds`` → ``RandomizedDelaySec`` on the timer (native —
      the bash-sleep wrapper is launchd-only and is NOT reproduced).
      A jitter on a spec with no timer is unrenderable → ValueError.
    - ``watch_paths`` → a ``.path`` unit with ``PathModified=`` per path
      (covers creation and modification — the flag-file pattern
      better-engine uses; launchd additionally fired on deletion).
    - ``start_calendar`` → ``OnCalendar=`` per rule. ``Persistent=`` is
      intentionally not emitted: launchd does not run powered-off-missed
      calendar jobs at boot either, so the default (false) is parity.
    - ``comment`` → ``Description=`` (falls back to the label).
    - ``extra`` → renders NOTHING; logs an error (design §3: the launchd
      escape hatch never smuggles ini keys — model real fields instead).
    """
    if not spec.label:
        raise ValueError("JobSpec.label is required")
    if not _LABEL_RE.match(spec.label):
        raise ValueError(f"JobSpec.label is not a valid systemd unit basename: {spec.label!r}")
    if not spec.program_args:
        raise ValueError("JobSpec.program_args must be a non-empty list of strings")
    for arg in spec.program_args:
        if not isinstance(arg, str):
            raise ValueError(
                f"JobSpec.program_args entries must be strings, got {type(arg).__name__}"
            )
        _no_newline("program_args", arg)
    if not spec.program_args[0].startswith("/"):
        raise ValueError(
            f"systemd ExecStart requires an absolute executable path, got "
            f"{spec.program_args[0]!r} (full-path discipline applies on both platforms)"
        )

    has_timer = spec.start_interval is not None or spec.start_calendar is not None
    if spec.jitter_seconds and spec.jitter_seconds > 0 and not has_timer:
        raise ValueError(
            "jitter_seconds is rendered as RandomizedDelaySec on the timer; "
            f"{spec.label!r} has no start_interval/start_calendar to attach it to"
        )
    if spec.extra:
        _log.error(
            "render_systemd_units(%s): JobSpec.extra is launchd-only and renders "
            "NOTHING on systemd (keys dropped: %s) — model load-bearing keys as "
            "real JobSpec fields (design-linux-port §3)",
            spec.label, sorted(spec.extra),
        )

    restart = _restart_policy(spec.keep_alive)
    service_name = systemd_unit_name(spec.label, "service")

    # ── <label>.service ──────────────────────────────────────────────────────
    unit_hdr = "# Generated by Evolve (runtime.scheduler.render_systemd_units) — do not edit."
    svc: list[str] = [unit_hdr, "[Unit]"]
    svc.append(f"Description={_specifier_safe('comment', spec.comment or spec.label)}")
    if restart is not None:
        svc.append("StartLimitIntervalSec=0")
    svc.append("")
    svc.append("[Service]")
    if spec.user:
        svc.append(f"User={_no_newline('user', spec.user)}")
    if spec.group_name:
        svc.append(f"Group={_no_newline('group_name', spec.group_name)}")
    if spec.umask is not None:
        svc.append(f"UMask={int(spec.umask):04o}")
    if spec.working_dir:
        svc.append(f"WorkingDirectory={_specifier_safe('working_dir', spec.working_dir)}")
    if spec.env:
        for k, v in spec.env.items():
            svc.append(f"Environment={_env_assignment(_no_newline('env', str(k)), _no_newline('env', str(v)))}")
    if spec.soft_file_limit is not None:
        # systemd's LimitNOFILE sets soft=hard when given one value; the
        # soft cap is what EMFILE storms hit, so one value is the intent.
        svc.append(f"LimitNOFILE={int(spec.soft_file_limit)}")
    svc.append("ExecStart=" + " ".join(_exec_token(a) for a in spec.program_args))
    if restart is not None:
        svc.append(f"Restart={restart}")
        throttle = spec.throttle_interval if spec.throttle_interval is not None else 10
        svc.append(f"RestartSec={int(throttle)}")
    elif spec.throttle_interval is not None:
        svc.append(f"RestartSec={int(spec.throttle_interval)}")
    if spec.stdout_path:
        svc.append(f"StandardOutput=append:{_specifier_safe('stdout_path', spec.stdout_path)}")
    if spec.stderr_path:
        svc.append(f"StandardError=append:{_specifier_safe('stderr_path', spec.stderr_path)}")
    # The service carries [Install] iff it must start on boot on its own:
    # launchd would start it at load (KeepAlive/RunAtLoad) and no timer
    # owns that duty (run_at_load folds into the timer's OnBootSec=0). A
    # watch_paths-only job with run_at_load keeps its boot start this way.
    daemon_activated = not has_timer and (restart is not None or spec.run_at_load)
    if daemon_activated:
        svc += ["", "[Install]", "WantedBy=multi-user.target"]
    units = {service_name: "\n".join(svc) + "\n"}

    # ── <label>.timer ────────────────────────────────────────────────────────
    if has_timer:
        tmr = [unit_hdr, "[Unit]", f"Description=Timer for {spec.label}", "",
               "[Timer]", f"Unit={service_name}"]
        if spec.start_interval is not None:
            interval = int(spec.start_interval)
            tmr.append(f"OnBootSec={0 if spec.run_at_load else interval}s")
            tmr.append(f"OnUnitActiveSec={interval}s")
        if spec.start_calendar is not None:
            rules = spec.start_calendar
            if isinstance(rules, dict):
                rules = [rules]
            for rule in rules:
                tmr.append(f"OnCalendar={_on_calendar(rule)}")
            if spec.run_at_load and spec.start_interval is None:
                tmr.append("OnBootSec=0s")
        if spec.jitter_seconds and spec.jitter_seconds > 0:
            tmr.append(f"RandomizedDelaySec={int(spec.jitter_seconds)}s")
        tmr += ["", "[Install]", "WantedBy=timers.target"]
        units[systemd_unit_name(spec.label, "timer")] = "\n".join(tmr) + "\n"

    # ── <label>.path ─────────────────────────────────────────────────────────
    if spec.watch_paths:
        pth = [unit_hdr, "[Unit]", f"Description=Path watch for {spec.label}", "",
               "[Path]", f"Unit={service_name}"]
        for p in spec.watch_paths:
            pth.append(f"PathModified={_specifier_safe('watch_paths', str(p))}")
        pth += ["", "[Install]", "WantedBy=multi-user.target"]
        units[systemd_unit_name(spec.label, "path")] = "\n".join(pth) + "\n"

    return units


class SystemdScheduler:
    """The systemd adapter — Linux port L1 (design-linux-port §3).

    Renders each :class:`JobSpec` through :func:`render_systemd_units` into
    ``unit_dir`` (default ``/etc/systemd/system``, the system domain — one
    admin plane manages N bot users via ``User=``, exactly like the
    LaunchDaemons-with-``UserName`` model; per-user systemd instances were
    rejected in the design). The service+timer(+path) set is treated
    atomically by every verb: ``install``/``remove`` write/delete the whole
    set, activation verbs (``enable``/``restart`` on install) target the
    units that own activation (timer/path when present, else the service).

    Mirrors :class:`LaunchdScheduler` contracts call sites can observe:
    the byte-identical install skip (``InstallResult.skipped`` — including
    "no stale sibling unit on disk", so a job that loses its timer
    reinstalls), the ``status()`` dict shape, and the injectable ``runner``
    seam (tests must never reach a real ``systemctl``).

    Deliberately and permanently has NO ``raw()``: raw runs verbatim
    launchctl subcommands. The non-test call sites still on
    ``get_launchd_scheduler()`` / ``.raw()`` are exactly the sites this
    adapter can never serve — the frozen census in
    ``packages/admin/tests/test_launchctl_seam_gates.py`` is that
    migration worklist, and those paths raising on Linux is by design.
    """

    def __init__(
        self,
        *,
        unit_dir: "Path | str" = "/etc/systemd/system",
        use_sudo: bool = True,
        sudo_non_interactive: bool = False,
        runner: "LaunchctlRunner | None" = None,
        timeout: float = 30.0,
    ) -> None:
        self.unit_dir = Path(unit_dir)
        self.use_sudo = use_sudo
        # sudo -n: fail fast instead of blocking on a password prompt
        # (same daemon-context rationale as LaunchdScheduler).
        self.sudo_non_interactive = sudo_non_interactive
        # Constructor timeout + per-call-override plumbing — identical shape
        # to LaunchdScheduler (see its __init__/_run for the rationale and
        # the fake-runner backward-compat guarantee).
        self._timeout = timeout
        self._custom_runner = runner is not None
        self._runner = runner or (lambda argv: _subprocess_runner(argv, timeout=timeout))

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _run(self, argv: "list[str]", timeout: "float | None") -> "tuple[int, str, str]":
        """Single chokepoint for every subprocess the adapter spawns.

        ``timeout=None`` ⇒ the constructor value (byte-identical to the
        pre-enrichment behavior); a non-None override only takes effect on
        the DEFAULT subprocess runner (an injected custom runner is called
        1-arg, exactly as before)."""
        if timeout is not None and not self._custom_runner:
            return _subprocess_runner(argv, timeout=timeout)
        return self._runner(argv)

    def _systemctl_raw(self, *args: str, timeout: "float | None" = None) -> "tuple[int, str, str]":
        if self.use_sudo:
            prefix = ["sudo", "-n"] if self.sudo_non_interactive else ["sudo"]
        else:
            prefix = []
        return self._run([*prefix, _SYSTEMCTL, *args], timeout)

    def _systemctl(self, *args: str, timeout: "float | None" = None) -> "tuple[int, str]":
        rc, out, err = self._systemctl_raw(*args, timeout=timeout)
        return rc, (out + err).strip()

    def _sudo(self, *args: str, timeout: "float | None" = None) -> "tuple[int, str]":
        rc, out, err = self._run(["sudo", *args], timeout)
        return rc, (out + err).strip()

    def _unit_path(self, unit_name: str) -> Path:
        return self.unit_dir / unit_name

    def artifact_path(self, label: str) -> str:
        """The PRIMARY unit :meth:`install` writes for ``label`` — the
        ``.service`` file (always rendered; a ``.timer``/``.path`` sibling is
        implied by the unit set). What callers stamp as
        ``installed_artifact``."""
        return str(self._unit_path(systemd_unit_name(label, "service")))

    def _candidate_units(self, label: str) -> "list[str]":
        return [f"{label}{suffix}" for suffix in _UNIT_KINDS]

    def _write_unit(
        self, unit_name: str, content: str, *, timeout: "float | None" = None
    ) -> "tuple[bool, str]":
        dst = self._unit_path(unit_name)
        if self.use_sudo:
            fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".unit")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                rc, out = self._sudo(_LINUX_CP, tmp, str(dst), timeout=timeout)
                if rc != 0:
                    return False, f"unit write failed: {out}"
            finally:
                try:
                    os.unlink(tmp)
                except OSError as e:
                    _log.debug("install(%s): staged tmp cleanup failed (%s)", unit_name, e)
            rc, out = self._sudo(_LINUX_CHOWN, "root:root", str(dst), timeout=timeout)
            if rc != 0:
                return False, f"unit chown failed: {out}"
            rc, out = self._sudo(_LINUX_CHMOD, "644", str(dst), timeout=timeout)
            if rc != 0:
                return False, f"unit chmod failed: {out}"
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(content)
            except OSError as e:
                return False, f"unit write failed: {e}"
        return True, ""

    @staticmethod
    def _log_dir_refusal(parent: str) -> "str | None":
        """No-follow audit of a log-dir path before a privileged mkdir/chown.

        The log parent lives under a *bot-writable* prefix (typically
        ``{bot_home}/.openclaw/logs``): a compromised bot user can plant
        ``~/.openclaw/logs -> /etc/whatever`` and the next privileged
        materialize/deploy would ``mkdir -p`` + ``chown <bot>`` the TARGET —
        root handing the bot ownership of an arbitrary path (the
        privileged-fs TOCTOU family). lstat (never follows) every existing
        component and return a refusal message when:

        - any component cannot be lstat'd (other than not existing yet) —
          an unauditable path gets no privileged fs op;
        - the leaf itself is a symlink (any owner — the chown subject must
          be a real directory);
        - an intermediate component is a symlink NOT owned by root.
          Root-owned intermediate symlinks (Ubuntu /usr-merge, macOS
          ``/var`` on dev boxes running the suite) are not plantable by an
          unprivileged user and pass.

        Missing components cannot be symlinks and pass (also keeps the
        injected-runner tests, whose fake mkdir touches no real fs, inert).
        """
        leaf = Path(parent)
        for comp in (leaf, *leaf.parents):
            try:
                st = os.lstat(comp)
            except FileNotFoundError:
                continue
            except OSError as e:
                return (
                    f"log dir {parent}: cannot lstat {comp} ({e}) — refusing "
                    "privileged mkdir/chown on a path that cannot be audited"
                )
            if not stat.S_ISLNK(st.st_mode):
                continue
            if comp != leaf and st.st_uid == 0:
                continue  # root-planted layout symlink — not attacker-reachable
            try:
                target = os.readlink(comp)
            except OSError:
                target = "?"
            return (
                f"log dir {parent}: {comp} is a symlink -> {target!r} (owner "
                f"uid {st.st_uid}) — refusing privileged mkdir/chown through "
                "a symlink planted on a bot-writable path (TOCTOU privesc); "
                "remove the symlink or pre-create the real directory"
            )
        return None

    def _ensure_log_dirs(
        self, spec: JobSpec, *, timeout: "float | None" = None
    ) -> "tuple[bool, str]":
        """Create + chown the parent dir of every stdout/stderr path.

        launchd auto-creates the directory behind ``StandardOutPath`` /
        ``StandardErrorPath``; **systemd does not**. A unit whose
        ``StandardOutput=append:<path>`` points at a missing directory
        crash-loops with ``status=209/STDOUT`` ("Failed to set up standard
        output: No such file or directory") before its ExecStart ever runs.
        ``install_bot_gateway_plist`` learned this for the gateway log dir;
        generalizing it here covers the admin-ui daemon and the ~50 evolve
        infra units that route through this seam, so no install site has to
        remember the platform gap.

        Each leaf dir is chowned to the unit's ``User=`` (``spec.user``) so
        systemd — which opens the log file *as that user* — can write it.
        Root-run units (no ``spec.user``) keep the mkdir but skip the chown.
        Best-effort per path: a failure is returned so the caller can abort
        the install before systemd hits the missing dir.

        Symlink hardening (see :meth:`_log_dir_refusal`): the path is
        no-follow-audited before the mkdir AND again between mkdir and chown,
        and the chown itself runs ``--no-dereference`` (``-h``) so even a
        leaf swapped to a symlink in the audit→exec window changes only the
        link, never its target. Disclosed residual: an *intermediate*
        component swapped to a symlink inside that same window still
        redirects the op — closing it needs an fd-walking helper running in
        the privileged context (openat/O_NOFOLLOW), not a path-string sudo;
        this layer turns the zero-effort persistent plant into a ms-scale
        active race. The unit-file ops (`_write_unit`, `_delete_unit_file`)
        need none of this: they target root-owned ``/etc/systemd/system``,
        which an unprivileged user cannot seed with symlinks.
        """
        # Route through self._run with the same use_sudo-conditional prefix as
        # _systemctl_raw: production (use_sudo=True) shells out to sudo mkdir /
        # chown; tests (use_sudo=False) flow through the injected runner so they
        # never touch the real filesystem behind the spec's absolute log paths.
        prefix = (
            (["sudo", "-n"] if self.sudo_non_interactive else ["sudo"])
            if self.use_sudo else []
        )
        seen: "set[str]" = set()
        for path in (spec.stdout_path, spec.stderr_path):
            if not path:
                continue
            parent = str(Path(path).parent)
            if parent in seen or parent in ("", "/"):
                continue
            seen.add(parent)
            refusal = self._log_dir_refusal(parent)
            if refusal:
                _log.error("ensure_log_dirs(%s): %s", spec.label, refusal)
                return False, refusal
            rc, out, err = self._run([*prefix, _LINUX_MKDIR, "-p", parent], timeout)
            if rc != 0:
                return False, f"log dir mkdir failed ({parent}): {(out + err).strip()}"
            # Re-audit what mkdir actually left on disk before handing the
            # path to a privileged chown (a plant can race the mkdir).
            refusal = self._log_dir_refusal(parent)
            if refusal:
                _log.error("ensure_log_dirs(%s): %s", spec.label, refusal)
                return False, refusal
            if spec.user:
                rc, out, err = self._run(
                    [*prefix, _LINUX_CHOWN, "-h", spec.user, parent], timeout
                )
                if rc != 0:
                    return False, f"log dir chown failed ({parent}): {(out + err).strip()}"
        return True, ""

    def _delete_unit_file(
        self, unit_name: str, *, timeout: "float | None" = None
    ) -> "tuple[bool, str]":
        dst = self._unit_path(unit_name)
        if self.use_sudo:
            rc, out = self._sudo(_LINUX_RM, str(dst), timeout=timeout)
            if rc != 0:
                return False, f"unit delete failed: {out}"
        else:
            try:
                dst.unlink()
            except OSError as e:
                return False, f"unit delete failed: {e}"
        return True, ""

    @staticmethod
    def _activation_units(spec: JobSpec, rendered: "dict[str, str]") -> "list[str]":
        """The units that own activation: timer/path when present, plus
        the service itself when it carries [Install] — i.e. launchd would
        start it at load (KeepAlive/RunAtLoad) and no timer owns the boot
        start via OnBootSec (the renderer's ``daemon_activated`` rule)."""
        units = [
            name for name in rendered
            if name.endswith(".timer") or name.endswith(".path")
        ]
        has_timer = any(name.endswith(".timer") for name in rendered)
        if not has_timer and (spec.keep_alive is not False or spec.run_at_load):
            units.append(systemd_unit_name(spec.label, "service"))
        return units

    # ── the six operations ───────────────────────────────────────────────────

    def install(self, spec: JobSpec, *, timeout: "float | None" = None) -> InstallResult:
        """Render ``spec``, write the unit set, (re)register with systemd.

        Skips everything when the on-disk unit set is byte-identical —
        identical content for every rendered file AND no stale sibling
        unit (a leftover ``.timer``/``.path`` from a previous shape forces
        a reinstall that also removes it). Mirrors the launchd contract:
        the skip is reported as ``InstallResult.skipped``; callers that
        need a restart-happens guarantee key on that field.

        ``timeout`` optionally bounds each subprocess this install spawns
        (None ⇒ the constructor value).
        """
        rendered = render_systemd_units(spec)  # ValueError on unrenderable specs
        try:
            identical = all(
                (self._unit_path(name).read_text() == rendered[name])
                if name in rendered else not self._unit_path(name).exists()
                for name in self._candidate_units(spec.label)
            )
        except (OSError, PermissionError) as e:
            # Unreadable → fall through; the privileged write below wins.
            _log.debug("install(%s): cannot read existing units (%s)", spec.label, e)
            identical = False
        if identical:
            return InstallResult(
                True, f"Up-to-date: {spec.label} (skipped reinstall)", skipped=True
            )

        # Stale siblings (e.g. the spec lost its timer): stop + deregister
        # them while systemd still knows the unit, then delete the file.
        stale = [
            name for name in self._candidate_units(spec.label)
            if name not in rendered and self._unit_path(name).exists()
        ]
        if stale:
            self._systemctl("disable", "--now", *stale, timeout=timeout)  # rc ignored: not-enabled is fine
            for name in stale:
                ok, msg = self._delete_unit_file(name, timeout=timeout)
                if not ok:
                    return InstallResult(False, f"stale {msg}")

        # systemd won't create the dirs behind StandardOutput/StandardError
        # (launchd does) — make them before the unit starts, else 209/STDOUT
        # crash-loop. See _ensure_log_dirs.
        ok, msg = self._ensure_log_dirs(spec, timeout=timeout)
        if not ok:
            return InstallResult(False, msg)

        for name, content in rendered.items():
            ok, msg = self._write_unit(name, content, timeout=timeout)
            if not ok:
                return InstallResult(False, msg)

        rc, out = self._systemctl("daemon-reload", timeout=timeout)
        if rc != 0:
            return InstallResult(False, f"systemctl daemon-reload failed (rc={rc}): {out}")

        activation = self._activation_units(spec, rendered)
        if activation:
            rc, out = self._systemctl("enable", *activation, timeout=timeout)
            if rc != 0:
                return InstallResult(False, f"systemctl enable failed (rc={rc}): {out}")
            # restart (not `enable --now`): applies changed unit files to
            # already-active units — the launchd bootout+bootstrap bounce.
            rc, out = self._systemctl("restart", *activation, timeout=timeout)
            if rc != 0:
                return InstallResult(False, f"systemctl restart failed (rc={rc}): {out}")
        return InstallResult(True, f"Installed: {spec.label}")

    def remove(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        """``disable --now`` + delete the unit set + ``daemon-reload`` +
        ``reset-failed``. Idempotent — removing a job that was never installed
        succeeds. ``timeout`` optionally bounds each subprocess (None ⇒ the
        constructor value)."""
        existing = self._existing_units(label)
        if not existing:
            return True, f"Removed (no unit files on disk): {label}"
        # rc ignored: disabling a not-enabled/not-loaded unit is fine, and
        # --now stops any running instance (the bootout analogue).
        self._systemctl("disable", "--now", *existing, timeout=timeout)
        for name in existing:
            ok, msg = self._delete_unit_file(name, timeout=timeout)
            if not ok:
                return False, msg
        rc, out = self._systemctl("daemon-reload", timeout=timeout)
        if rc != 0:
            return False, f"systemctl daemon-reload failed (rc={rc}): {out}"
        # Clear the recorded failure state. Deleting the unit file does NOT
        # retract it: systemd keeps the failed result for a unit it can no
        # longer find, so `systemctl list-units --state=failed` goes on
        # listing the removed unit as `not-found / failed` forever, and
        # anything watching for failed units trips on residue from a
        # SUCCESSFUL teardown. Observed on the Linux pod 2026-08-18 after the
        # #3705 apply-unit retirement: both units had been failing on their
        # timer, and removal left both in the failed list until a manual
        # `reset-failed`. Must run AFTER daemon-reload (before it, systemd
        # still has the old unit loaded and re-derives the state).
        #
        # rc ignored deliberately, in both directions: a unit that was never
        # in a failed state is a no-op here on current systemd but has
        # errored on older versions, and either way a teardown that removed
        # the files succeeded — this is bookkeeping, not the operation. The
        # macOS adapter needs no analogue: launchd keeps no failure record
        # for a booted-out label.
        self._systemctl("reset-failed", *existing, timeout=timeout)
        return True, f"Removed: {label}"

    def _existing_units(self, label: str) -> "list[str]":
        return [
            name for name in self._candidate_units(label)
            if self._unit_path(name).exists()
        ]

    @staticmethod
    def _activation_from_units(units: "list[str]") -> "list[str]":
        """The units that own activation among a label's on-disk set: the
        timer/path when present (a timer-driven job), else the service (a
        plain daemon). Mirrors :meth:`_activation_units`, but keyed off what
        is on disk rather than a spec — disable/enable get only a label."""
        activation = [n for n in units if n.endswith(".timer") or n.endswith(".path")]
        if not activation:
            activation = [n for n in units if n.endswith(".service")]
        return activation

    def disable(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        """Persistently pause the job: ``systemctl disable --now`` on the
        label's on-disk unit set (removes the ``timers.target`` /
        ``multi-user.target`` symlinks so it does NOT start at the next boot,
        and ``--now`` stops any running/armed instance). The unit files stay
        on disk (unlike :meth:`remove`) — :meth:`enable` reverses it.

        Idempotent: disabling a not-enabled/not-installed label succeeds.
        ``timeout`` optionally bounds the subprocess (None ⇒ the constructor
        value)."""
        existing = self._existing_units(label)
        if not existing:
            return True, f"Disabled (no unit files on disk): {label}"
        rc, out = self._systemctl("disable", "--now", *existing, timeout=timeout)
        # `disable` of a not-enabled unit prints to stderr but still exits 0;
        # a real failure (permission, bad unit) is a non-zero rc.
        if rc != 0:
            return False, f"systemctl disable --now failed (rc={rc}): {out}"
        return True, f"Disabled: {label}"

    def enable(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        """Resume a :meth:`disable`-d job: ``systemctl enable`` the activation
        unit (re-creates the target symlink) + ``systemctl restart`` it so it
        arms/starts now — ``enable`` alone does not start a timer.

        The activation unit is the timer/path when present, else the service
        (see :meth:`_activation_from_units`). No ``daemon-reload`` is issued:
        disable/enable never change unit-file content. When no units are on
        disk this is an idempotent no-op success. ``timeout`` optionally
        bounds each subprocess (None ⇒ the constructor value)."""
        existing = self._existing_units(label)
        activation = self._activation_from_units(existing)
        if not activation:
            return True, f"Enabled (no unit files on disk): {label}"
        rc, out = self._systemctl("enable", *activation, timeout=timeout)
        if rc != 0:
            return False, f"systemctl enable failed (rc={rc}): {out}"
        # restart (not `enable --now`): arms a timer / (re)starts a service —
        # the launchd enable-then-bootstrap analogue.
        rc, out = self._systemctl("restart", *activation, timeout=timeout)
        if rc != 0:
            return False, f"systemctl restart failed (rc={rc}): {out}"
        return True, f"Enabled: {label}"

    def restart(self, label: str, *, timeout: "float | None" = None) -> "tuple[bool, str]":
        """⚠️ Destructive: restarts the service (kills a live gateway
        instance and starts a fresh one — the ``kickstart -k`` analogue).
        For timer jobs this runs the job now, exactly as kickstart did.
        ``timeout`` optionally bounds the subprocess (None ⇒ the
        constructor value)."""
        rc, out = self._systemctl("restart", systemd_unit_name(label, "service"), timeout=timeout)
        return rc == 0, out

    def list(self, *, prefix: "str | None" = None, timeout: "float | None" = None) -> "list[str]":
        """Labels systemd knows in this domain, deduped across unit kinds
        (``list-units`` + ``list-timers``; a label with a service, timer
        and path appears once). Empty list on failure. ``timeout`` optionally
        bounds each probe subprocess (None ⇒ the constructor value)."""
        pattern = [f"{prefix}*"] if prefix is not None else []
        rc, out, _err = self._systemctl_raw(
            "list-units", "--all", "--plain", "--no-legend", *pattern, timeout=timeout
        )
        if rc != 0:
            return []
        chunks = [out]
        rc, out, _err = self._systemctl_raw(
            "list-timers", "--all", "--plain", "--no-legend", *pattern, timeout=timeout
        )
        if rc == 0:
            chunks.append(out)
        labels: "list[str]" = []
        seen: "set[str]" = set()
        for chunk in chunks:
            for line in chunk.splitlines():
                for tok in line.split():
                    lab = label_from_unit_name(tok)
                    if lab is None:
                        continue
                    if (prefix is None or lab.startswith(prefix)) and lab not in seen:
                        seen.add(lab)
                        labels.append(lab)
                    break
        return labels

    def status(self, label: str, *, timeout: "float | None" = None) -> dict:
        """``systemctl show`` → the same ``{installed, managed, running,
        pid, last_exit, status_error}`` shape as the launchd adapter.

        installed: service unit file on disk · managed: systemd knows the
        unit (LoadState=loaded) · running: has a MainPID right now ·
        last_exit: ExecMainStatus as a string.

        ``timeout`` optionally bounds the probe subprocess (None ⇒ the
        constructor value).
        """
        base: dict = {
            "installed": self._unit_path(systemd_unit_name(label, "service")).exists(),
            "managed": False,
            "running": False,
            "pid": None,
            "last_exit": None,
            "status_error": None,
        }
        rc, out, err = self._systemctl_raw(
            "show", systemd_unit_name(label, "service"),
            "-p", "LoadState,ActiveState,MainPID,ExecMainStatus",
            timeout=timeout,
        )
        if rc != 0:
            if rc != 4:  # not-found is rc=4 on current systemd
                base["status_error"] = (
                    "cannot_escalate" if is_sudo_escalation_error(err.strip())
                    else "error"
                )
            return base
        props: "dict[str, str]" = {}
        for line in out.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                props[k.strip()] = v.strip()
        if props.get("LoadState") != "loaded":
            return base  # older systemd: rc=0 + LoadState=not-found
        base["managed"] = True
        try:
            pid = int(props.get("MainPID", "0") or "0")
        except ValueError:
            pid = 0
            _log.debug("status(%s): non-integer MainPID %r", label, props.get("MainPID"))
        if pid > 0:
            base["pid"] = pid
            base["running"] = True
        if "ExecMainStatus" in props:
            base["last_exit"] = props["ExecMainStatus"]
        return base

    def running(self, label: str) -> bool:
        rc, out, _err = self._systemctl_raw(
            "is-active", systemd_unit_name(label, "service")
        )
        return rc == 0 and out.strip() == "active"

    def supervision(self, label: str, *, timeout: "float | None" = None) -> dict:
        """Crash-loop telemetry via ``systemctl show -p
        ActiveState,SubState,NRestarts,MainPID,ExecMainStatus,Environment``,
        normalized to ``{n_restarts, sub_state, active_state, pid, last_exit,
        env, status_error}``.

        ``sub_state == "auto-restart"`` is systemd's "actively bouncing right
        now" state; ``n_restarts`` is the monotonic restart counter. ``env``
        carries the unit's ``Environment=`` assignments so the consumer can spot
        two units declaring the same gateway port. ``status_error`` keeps the
        PR #1579 probe tri-state (None when authoritative, ``cannot_escalate`` /
        ``error`` otherwise)."""
        base: dict = {
            "n_restarts": None,
            "sub_state": None,
            "active_state": None,
            "pid": None,
            "last_exit": None,
            "env": {},
            "status_error": None,
        }
        rc, out, err = self._systemctl_raw(
            "show", systemd_unit_name(label, "service"),
            "-p", "LoadState,ActiveState,SubState,NRestarts,MainPID,ExecMainStatus,Environment",
            timeout=timeout,
        )
        if rc != 0:
            if rc != 4:  # not-found is rc=4 on current systemd
                base["status_error"] = (
                    "cannot_escalate" if is_sudo_escalation_error(err.strip())
                    else "error"
                )
            return base
        props: "dict[str, str]" = {}
        for line in out.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                props[k.strip()] = v.strip()
        if props.get("LoadState") != "loaded":
            return base  # older systemd: rc=0 + LoadState=not-found
        base["sub_state"] = props.get("SubState") or None
        base["active_state"] = props.get("ActiveState") or None
        try:
            base["n_restarts"] = int(props["NRestarts"]) if props.get("NRestarts") else None
        except ValueError:
            _log.debug("supervision(%s): non-integer NRestarts %r", label, props.get("NRestarts"))
        try:
            pid = int(props.get("MainPID", "0") or "0")
        except ValueError:
            pid = 0
        if pid > 0:
            base["pid"] = pid
        if props.get("ExecMainStatus"):
            base["last_exit"] = props["ExecMainStatus"]
        # ``Environment=KEY=val KEY2=val2`` — one line, space-separated assignments.
        env_line = props.get("Environment", "")
        env: "dict[str, str]" = {}
        for tok in env_line.split():
            ek, esep, ev = tok.partition("=")
            if esep:
                env[ek] = ev
        base["env"] = env
        return base

    def kill(
        self, label: str, *, signal: str = "SIGTERM", timeout: "float | None" = None
    ) -> "tuple[bool, str]":
        """Signal the service's processes (default SIGTERM). The unit stays
        registered; ``Restart=always`` jobs will be respawned by systemd.
        ``timeout`` optionally bounds the subprocess (None ⇒ the constructor
        value)."""
        rc, out = self._systemctl(
            "kill", "-s", signal, systemd_unit_name(label, "service"), timeout=timeout
        )
        return rc == 0, out


# ── factory (mirrors runtime.perms.get_perms, this package) ──────────────────

_scheduler_override: "Scheduler | None" = None
_scheduler_defaults: "dict[str, Scheduler]" = {}


def get_scheduler() -> Scheduler:
    """Return the process-wide scheduler adapter.

    The default is keyed off the active platform profile (macOS → launchd,
    Linux → systemd) so a pinned profile selects the matching adapter — the
    same shape as :func:`runtime.perms.get_perms`, including the cache: the
    per-profile default is cached BY PROFILE NAME, not first-call-wins, so a
    profile pinned after some earlier call still selects the matching adapter
    (the seam-factory contract — a first-call-wins cache silently kept the
    stale platform's adapter for the rest of the process). Production never
    wired ``SystemdScheduler`` anywhere else, so before the profile keying a
    real ``deploy``/``setup`` on Linux silently used launchd and tried to
    write plists to ``/Library/LaunchDaemons`` (ENOENT on Linux). A
    :func:`set_scheduler` injection still wins over the default (tests/harness;
    the wizard's Linux platform gate)."""
    if _scheduler_override is not None:
        return _scheduler_override
    name = get_profile().name
    if name not in _scheduler_defaults:
        _scheduler_defaults[name] = (
            SystemdScheduler() if name == "linux" else LaunchdScheduler()
        )
    return _scheduler_defaults[name]


def set_scheduler(scheduler: "Scheduler | None") -> None:
    """Swap the adapter (tests inject a fake; a future port injects systemd).
    Pass ``None`` to restore the profile-keyed default on next
    ``get_scheduler()``."""
    global _scheduler_override
    _scheduler_override = scheduler


def get_launchd_scheduler() -> LaunchdScheduler:
    """The launchd-typed adapter, for call sites still on ``raw()``.

    ``raw()`` is deliberately NOT on the ``Scheduler`` Protocol — it runs
    verbatim launchctl subcommands, which no other platform's adapter can
    honor. Call sites that need it are S2 migration debt: grep for this
    function to enumerate them. Each such site cannot run on a non-launchd
    platform until migrated to Protocol verbs, and this raises rather than
    misbehaving if a different adapter is active.
    """
    sched = get_scheduler()
    if not isinstance(sched, LaunchdScheduler):
        raise RuntimeError(
            "this call site uses launchctl-verbatim raw(); it requires the "
            "LaunchdScheduler adapter (S2 debt — migrate to Scheduler verbs)"
        )
    return sched
