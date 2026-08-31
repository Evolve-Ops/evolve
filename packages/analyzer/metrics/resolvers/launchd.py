"""metrics.resolvers.launchd — launchd.service_loaded.

1.0 if the bot's gateway service is loaded and running, 0.0 otherwise.

PLATFORM-AWARE (Linux port, internal/design-linux-port-2026-06-10.md §3):
the metric name stays ``launchd.service_loaded`` for registry stability,
but the *probe* branches on the active platform profile:

- **macOS** queries via ``launchctl print system/<label>``, not
  ``launchctl list <label>``. The reason is privilege scope: bot gateways
  live in the system domain (installed under ``/Library/LaunchDaemons/``),
  and a non-root user — which is what the scheduled analyzer runs as —
  gets rc=1 and empty output from ``launchctl list <label>`` against a
  system-domain target, even when the daemon is loaded and running.
  ``launchctl print system/<label>`` reads the system domain explicitly
  and works for unprivileged service accounts.

- **Linux** queries via the active ``get_scheduler()`` adapter
  (``SystemdScheduler`` on a Linux pod, wired by the wizard's platform
  gate). systemd *system* units (``User=<bot>``, design §3) are queryable
  by the daemon's own ``systemctl show`` WITHOUT run-as-bot indirection,
  so the Linux branch uses ``status()`` (``managed`` + ``running``) — no
  ``raw()``, no unprivileged-print dance. The key invariant: on Linux a
  loaded+running gateway returns 1.0 and the probe NEVER raises → never a
  false 0.0 (the false ``launchd_not_loaded`` Signal this resolver used to
  emit on every Linux bot, every cycle, because the launchd-only ``raw()``
  path raised through the fail-fast ``get_launchd_scheduler`` guard).

macOS posture (preserved exactly): the probe goes through a dedicated
``LaunchdScheduler(use_sudo=False, runner=…)`` and its ``raw()`` escape
hatch — neither ``status()`` nor ``running()`` can express this read on
macOS: both shell ``launchctl list <label>``, which is exactly the call
that fails unprivileged for system-domain targets (see above), and the
process-wide ``get_scheduler()``'s sudo default would change this
resolver's privilege posture. So the macOS branch constructs its own
unprivileged adapter (the ``service._user_scheduler`` /
``retire._probe_scheduler`` guarded-derive pattern) rather than routing
through ``get_launchd_scheduler`` — that factory returns the sudo'd
process singleton and cannot carry the run-as-bot runner + use_sudo=False
this per-bot probe requires. The adapter builds the argv; this module
never constructs a launchctl command line itself.

The label convention is ``ai.openclaw.{bot_id}-gateway`` — what
``evolve_admin.deploy.install_bot_gateway_plist`` installs and what
``heal.restart_gateway`` probes. Configurable per site via
``set_label_builder()``; macOS tests override the launchctl probe via
``set_launchctl_runner()`` (seam runner shape:
``argv -> (rc, stdout, stderr)``), Linux tests inject a ``FakeScheduler``
via ``runtime.scheduler.set_scheduler``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from typing import Callable

from metrics.registry import MetricSpec, MetricValue, register
from platform_profile import get_profile
from runtime.scheduler import LaunchdScheduler, get_scheduler


_LabelBuilder = Callable[[str], str]
_Runner = Callable[[list[str]], "tuple[int, str, str]"]


def _default_label(bot_id: str) -> str:
    return f"ai.openclaw.{bot_id}-gateway"


def _default_runner(argv: list[str]) -> "tuple[int, str, str]":
    """Seam-shaped runner that PROPAGATES OSError/TimeoutExpired.

    Deliberately not the seam's default runner: the resolver maps
    "launchctl could not be invoked at all" to confidence 0.7, distinct
    from the authoritative rc!=0 "not loaded" (confidence 1.0). The seam
    default folds exceptions into rc=1, which would erase that tri-state.
    """
    r = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    return r.returncode, r.stdout, r.stderr


_label_builder: _LabelBuilder = _default_label
_runner: _Runner = _default_runner


def set_label_builder(fn: _LabelBuilder) -> None:
    global _label_builder
    _label_builder = fn


def set_launchctl_runner(fn: _Runner) -> None:
    global _runner
    _runner = fn


def _resolve_macos(label: str) -> MetricValue:
    """macOS branch — UNPRIVILEGED ``launchctl print system/<label>`` via a
    dedicated run-as-bot adapter. Byte-identical to the pre-Linux-port
    behavior; see the module docstring for why this can't route through
    the fail-fast ``get_launchd_scheduler`` accessor."""
    target = f"system/{label}"
    # Cheap per-call construction; reads module-level _runner at call time
    # so set_launchctl_runner() swaps are honored.
    sched = LaunchdScheduler(use_sudo=False, runner=_runner)
    try:
        rc, out, _err = sched.raw("print", target)
    except (OSError, subprocess.TimeoutExpired) as e:
        return MetricValue(
            value=0.0,
            confidence=0.7,
            source_note=f"launchctl invocation failed: {e}",
        )
    if rc != 0:
        return MetricValue(
            value=0.0,
            confidence=1.0,
            source_note=f"{target!r} not loaded (rc={rc})",
        )
    if "state = running" in out:
        return MetricValue(
            value=1.0,
            confidence=1.0,
            source_note=f"{target!r} state=running",
        )
    return MetricValue(
        value=0.0,
        confidence=1.0,
        source_note=f"{target!r} loaded but not running",
    )


def _resolve_via_scheduler(label: str) -> MetricValue:
    """Non-macOS branch — query the active ``get_scheduler()`` adapter
    (``SystemdScheduler`` on a Linux pod) via the platform-neutral
    ``status()`` verb. Maps ``managed`` + ``running`` to 1.0, everything
    else to 0.0, and the probe-failure tri-state (``status_error``) to the
    low-confidence 0.0.

    The invariant this branch exists to hold: a loaded+running gateway
    returns 1.0 and the probe NEVER raises → never the false 0.0 that fired
    a spurious ``launchd_not_loaded`` Signal on every Linux bot, every
    cycle (the launchd-only ``raw()`` path raised under
    the fail-fast ``get_launchd_scheduler`` guard on a non-launchd
    adapter)."""
    st = get_scheduler().status(label)
    if st.get("status_error"):
        # Probe couldn't complete (sudo couldn't escalate, systemctl errored):
        # nothing authoritative is knowable — mirror the macOS
        # invocation-failure confidence rather than asserting "not loaded".
        return MetricValue(
            value=0.0,
            confidence=0.7,
            source_note=f"{label!r} status probe failed: {st['status_error']}",
        )
    if st.get("managed") and st.get("running"):
        return MetricValue(
            value=1.0,
            confidence=1.0,
            source_note=f"{label!r} loaded and running",
        )
    if st.get("managed"):
        return MetricValue(
            value=0.0,
            confidence=1.0,
            source_note=f"{label!r} loaded but not running",
        )
    return MetricValue(
        value=0.0,
        confidence=1.0,
        source_note=f"{label!r} not loaded",
    )


def resolve_launchd_service_loaded(
    bot_id: str, as_of: datetime  # noqa: ARG001
) -> MetricValue:
    label = _label_builder(bot_id)
    if get_profile().name == "macos":
        return _resolve_macos(label)
    return _resolve_via_scheduler(label)


register(
    MetricSpec(
        name="launchd.service_loaded",
        description=(
            "1.0 if the bot's gateway service is loaded and running "
            "(launchctl print on macOS, get_scheduler().status on Linux)."
        ),
        unit="bool",
        source="/bin/launchctl print system/{label} (macOS) | systemctl show (Linux)",
    ),
    resolve_launchd_service_loaded,
)
