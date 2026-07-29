"""Host power & hardware posture helpers (Phase 8.2 — dedicated-Apple support).

Evolve's daemon fleet assumes an always-on host: launchd ``StartInterval``
jobs don't fire during sleep (they coalesce into a single firing on wake),
and ``KeepAlive`` gateways are suspended — a sleeping Mac takes every bot
dark with the lid. These helpers let the setup wizard detect battery /
portable hardware, inspect the AC power profile, and flip the host to
never-sleep-on-AC, so any *dedicated, always-on* Mac — not just a Mac
mini — is a supported chassis
(docs/research-platform-expansion-2026-06-10.md §4a).

The chassis itself is never a gate: a retired MacBook on a shelf is
exactly as isolated as a mini. The wizard explains, offers the fix, and
records the operator's choice — it never hard-blocks on hardware.

Read paths are safe for any caller. ``set_never_sleep_on_ac`` writes the
system power profile and requires root (the wizard runs under sudo).

Platform backend (Linux port L3 / W1, docs/design-linux-port-2026-06-10.md
§1): the ``pmset``/``sysctl`` helpers above are the macOS implementation and
stay byte-for-byte identical (macOS byte-identity is a hard platform
invariant). The wizard reaches them through the :class:`HostPower` adapter,
selected by :func:`platform_profile.get_profile` exactly like the
``runtime.perms`` / ``runtime.scheduler`` / ``runtime.isolation`` seams.
The Linux backend (:class:`LinuxHostPower`) is a *report-only no-op*: a
headless always-on host (the VPS target) has no sleep targets to manage, so
``manages_sleep()`` is ``False`` and the wizard skips the whole pmset offer
(operator decision 2026-06-16: VPS-only — the portable-laptop-Linux
``systemctl mask sleep.target suspend.target`` analogue is a documented
future option, census §4 Q3, not built here).
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from platform_profile import get_profile

_PMSET = "/usr/bin/pmset"
_SYSCTL = "/usr/sbin/sysctl"


def _run(cmd: list[str], timeout: int = 10) -> str:
    """Run a command and return stdout ('' on any failure)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:
        return ""


# ── Hardware identity ─────────────────────────────────────────────────────────


def hardware_model() -> str:
    """The Mac model identifier, e.g. 'Macmini9,1' or 'MacBookPro18,2'."""
    return _run([_SYSCTL, "-n", "hw.model"]).strip()


def is_apple_silicon() -> bool:
    return platform.machine() == "arm64"


def has_internal_battery() -> bool:
    """True when pmset reports an internal battery (portable hardware)."""
    return "InternalBattery" in _run([_PMSET, "-g", "batt"])


def is_portable(model: str | None = None, battery: bool | None = None) -> bool:
    """Battery-equipped / laptop hardware. Either evidence suffices —
    `pmset -g batt` can miss a battery the SMC has deprioritized, and the
    model string catches that case."""
    if battery is None:
        battery = has_internal_battery()
    if model is None:
        model = hardware_model()
    return battery or model.startswith("MacBook")


def on_ac_power() -> bool:
    """True when currently drawing from AC ('Now drawing from 'AC Power'')."""
    return "AC Power" in _run([_PMSET, "-g", "batt"]).split("\n", 1)[0]


# ── Power profile (pmset) ─────────────────────────────────────────────────────


def parse_pmset_custom(text: str) -> dict[str, dict[str, int]]:
    """Parse `pmset -g custom` output into {section: {setting: int}}.

    Sections are 'AC Power' / 'Battery Power'. Keys may contain spaces
    ('Sleep On Power Button 1'); the value is the last whitespace-separated
    token. Non-integer values (e.g. hibernatefile paths) are skipped —
    callers only consume the numeric timers.
    """
    sections: dict[str, dict[str, int]] = {}
    current: dict[str, int] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith("Power:"):
            current = sections.setdefault(line[:-1].strip(), {})
            continue
        if current is None:
            # Headerless output (defensive) — attribute to AC Power.
            current = sections.setdefault("AC Power", {})
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        key, val = parts
        try:
            current[key.strip()] = int(val)
        except ValueError:
            continue
    return sections


def ac_power_settings() -> dict[str, int]:
    """The 'AC Power' section of `pmset -g custom` ({} when unavailable)."""
    return parse_pmset_custom(_run([_PMSET, "-g", "custom"])).get("AC Power", {})


def sleep_disabled() -> bool:
    """True when `pmset disablesleep 1` is in effect (SleepDisabled 1)."""
    for line in _run([_PMSET, "-g"]).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "SleepDisabled":
            return parts[1] == "1"
    return False


def set_never_sleep_on_ac() -> tuple[bool, str]:
    """Set the AC power profile to never system-sleep (requires root).

    `pmset -c sleep 0 displaysleep 0` — scoped to the AC ('-c') profile,
    so a portable on battery still sleeps normally; the dedicated-host
    posture only requires the plugged-in profile to stay awake.
    Returns (ok, error_detail).
    """
    try:
        proc = subprocess.run(
            [_PMSET, "-c", "sleep", "0", "displaysleep", "0"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            return True, ""
        return False, (proc.stderr or proc.stdout).strip()[:300]
    except Exception as e:
        return False, str(e)


def power_posture() -> dict[str, Any]:
    """One-shot posture snapshot for the wizard (and network.json `host`).

    All reads, no writes. Keys:
      hardware_model, apple_silicon, has_battery, portable, on_ac_power,
      ac_sleep, ac_displaysleep (None when pmset is unreadable),
      sleep_disabled.
    """
    model = hardware_model()
    battery = has_internal_battery()
    ac = ac_power_settings()
    return {
        "hardware_model": model,
        "apple_silicon": is_apple_silicon(),
        "has_battery": battery,
        "portable": is_portable(model=model, battery=battery),
        "on_ac_power": on_ac_power(),
        "ac_sleep": ac.get("sleep"),
        "ac_displaysleep": ac.get("displaysleep"),
        "sleep_disabled": sleep_disabled(),
    }


# ── Homebrew prefix (Apple Silicon vs Intel) ──────────────────────────────────


def homebrew_prefix() -> str:
    """Homebrew prefix for this machine: /opt/homebrew (Apple Silicon) or
    /usr/local (Intel). Prefers an actually-installed brew; falls back by
    architecture when brew is absent."""
    return _homebrew_prefix_impl(
        exists=lambda p: Path(p).exists(),
        machine=platform.machine(),
    )


def _homebrew_prefix_impl(exists, machine: str) -> str:
    for prefix in ("/opt/homebrew", "/usr/local"):
        if exists(f"{prefix}/bin/brew"):
            return prefix
    return "/opt/homebrew" if machine == "arm64" else "/usr/local"


# ── Platform backend (Linux port L3 / W1) ─────────────────────────────────────
#
# The sleep-posture functions above are the macOS implementation; the wizard
# consumes them through this adapter so the platform divergence lives in one
# place (the adapter layer), never in `sys.platform` branches at the call
# site. Mirrors runtime.perms's profile-keyed get_perms()/set_perms().


@runtime_checkable
class HostPower(Protocol):
    """Inspect + manage the host's sleep posture for the always-on fleet.

    ``manages_sleep`` is the capability predicate the wizard gates on: a host
    that cannot sleep (a headless Linux VPS) returns ``False``, and the
    power-posture step renders a one-line "always-on" skip instead of the
    pmset offer. ``power_posture`` returns the snapshot recorded under
    network.json ``host`` (same key shape on both platforms so downstream
    readers stay platform-neutral).
    """

    def manages_sleep(self) -> bool: ...
    def power_posture(self) -> dict[str, Any]: ...
    def ac_power_settings(self) -> dict[str, int]: ...
    def set_never_sleep_on_ac(self) -> tuple[bool, str]: ...


class MacOSHostPower:
    """The default adapter — today's ``pmset``/``sysctl`` behavior verbatim.

    Every method delegates to the module-level function of the same name
    (looked up at call time), so the macOS path is byte-for-byte unchanged
    and the existing tests that patch ``host_power.power_posture`` /
    ``set_never_sleep_on_ac`` keep intercepting.
    """

    def manages_sleep(self) -> bool:
        return True

    def power_posture(self) -> dict[str, Any]:
        return power_posture()

    def ac_power_settings(self) -> dict[str, int]:
        return ac_power_settings()

    def set_never_sleep_on_ac(self) -> tuple[bool, str]:
        return set_never_sleep_on_ac()


def _linux_hardware_model() -> str:
    """Best-effort host model for the network.json record — DMI product
    name (informative on a VPS, e.g. the hypervisor/board) falling back to
    the CPU architecture (never empty, so the network.json record always
    carries *something* — unlike macOS ``hardware_model()`` which returns ''
    when sysctl is unreadable)."""
    try:
        name = Path("/sys/class/dmi/id/product_name").read_text().strip()
    except (OSError, ValueError):  # unreadable, or non-UTF8 DMI bytes
        return platform.machine()
    return name or platform.machine()


class LinuxHostPower:
    """The Linux adapter — a report-only no-op (Linux port L3 / W1).

    The VPS target is headless and always-on: there are no AC/battery
    profiles, no ``pmset`` analogue to set, and nothing to manage. So
    ``manages_sleep()`` is ``False`` (the wizard skips the offer) and the
    posture snapshot reports the honest always-on shape — same keys as the
    macOS dict so ``network.json`` ``host`` readers don't branch. The
    portable-laptop-Linux ``systemctl mask sleep.target suspend.target``
    analogue is a documented future option (census §4 Q3 / design §1),
    intentionally not built for the VPS-only spike.
    """

    def manages_sleep(self) -> bool:
        return False

    def power_posture(self) -> dict[str, Any]:
        return {
            "hardware_model": _linux_hardware_model(),
            "apple_silicon": False,
            "has_battery": False,
            "portable": False,
            "on_ac_power": True,
            "ac_sleep": 0,
            "ac_displaysleep": 0,
            "sleep_disabled": True,
        }

    def ac_power_settings(self) -> dict[str, int]:
        return {}

    def set_never_sleep_on_ac(self) -> tuple[bool, str]:
        # Nothing to do on an always-on host — succeed as a no-op. The
        # wizard never calls this on Linux (manages_sleep() short-circuits),
        # but the Protocol contract stays honest.
        return True, ""


_override: "HostPower | None" = None
_defaults: "dict[str, HostPower]" = {}


def get_host_power() -> HostPower:
    """Return the process-wide host-power adapter.

    Default is keyed off the active platform profile (macOS → pmset/sysctl
    backend, Linux → always-on no-op) so a pinned profile selects the
    matching backend; a :func:`set_host_power` injection wins over both.
    Mirrors ``runtime.perms.get_perms``.
    """
    if _override is not None:
        return _override
    name = get_profile().name
    if name not in _defaults:
        _defaults[name] = LinuxHostPower() if name == "linux" else MacOSHostPower()
    return _defaults[name]


def set_host_power(backend: "HostPower | None") -> None:
    """Swap the adapter (tests inject a fake). Pass ``None`` to restore the
    profile-keyed default on the next :func:`get_host_power`."""
    global _override
    _override = backend


__all__ = [
    "hardware_model",
    "is_apple_silicon",
    "has_internal_battery",
    "is_portable",
    "on_ac_power",
    "parse_pmset_custom",
    "ac_power_settings",
    "sleep_disabled",
    "set_never_sleep_on_ac",
    "power_posture",
    "homebrew_prefix",
    "HostPower",
    "MacOSHostPower",
    "LinuxHostPower",
    "get_host_power",
    "set_host_power",
]
