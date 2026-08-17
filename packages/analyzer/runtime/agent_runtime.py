"""agent_runtime — the AgentRuntime seam (roadmap 4.3, Phases A + B + D).

Evolve talks to a bot's agent through exactly one chokepoint today: ``oc_cli``
(documented: "Never call openclaw directly from other modules"). This formalizes
that boundary into an ``AgentRuntime`` interface so the OpenClaw/macOS coupling
becomes a swappable adapter rather than a load-bearing assumption — the move that
answers the diligence "single-runtime lock-in" finding. See
``docs/design-phase4.3-runtime-adapter-seam-2026-06-09.md``.

Phase A defined the interface + ``OpenClawRuntime`` (delegates to ``oc_cli``) +
``FakeRuntime`` (no OpenClaw/launchd/macOS present — the test-without-a-Mac
payoff). Phase B migrated the 18 ``oc_cli`` importers onto ``get_runtime()`` and
extended the interface to cover their *actual* usage (``cron_runs`` /
``full_config_set_with_error`` / ``keys_get``; ``network_path`` threaded through
the config/model/memory/restart methods; corrected signatures/return types for
``model_set`` / ``full_config_set`` / ``memory_set`` / ``gateway_restart``).

**Phase D retires the ``command()`` / ``command_raw()`` escape hatch.** Phase B
shipped a generic OpenClaw-CLI-shaped escape hatch for the handful of callers
that ran ``openclaw`` argv directly. Every one of those sites was a
``security audit`` / ``doctor --fix`` / ``cron list`` / ``agents set-identity``
call, so Phase D lifts them into typed methods and drops the escape hatch:

* ``security_audit`` now carries ``deep`` / ``timeout`` / ``cache_ttl`` /
  ``_err_out`` (the knobs the audit callers needed), and ``cron_list`` carries
  ``_err_out`` (the fleet-cron view classifies the error string).
* ``doctor_fix`` and ``set_identity`` are new typed methods over the two raw
  (non-JSON) commands.

With these in place ``command`` / ``command_raw`` are gone from the Protocol and
both adapters — the runtime surface is now fully typed, so a non-OpenClaw adapter
implements a closed set of operations rather than an open ``argv`` passthrough.

Still **behavior-preserving**: every ``OpenClawRuntime`` method is the same
``oc_cli`` call behind an interface, and ``get_runtime()`` returns it by default.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentRuntime(Protocol):
    """Read/modify one bot's agent. Methods mirror ``oc_cli`` 1:1.

    Read methods return ``None`` on failure (matching ``oc_cli``); mutating
    methods return ``bool`` success unless noted (``full_config_set`` returns the
    updated config dict, ``gateway_restart`` returns a restart-result dict).

    ``network_path`` (where it appears) overrides the ``network.json`` ``oc_cli``
    resolves a bot's unix user / service domain from — a macOS/OpenClaw concept
    today, harmless for adapters that don't have a network.json.
    """

    # ── reads ────────────────────────────────────────────────────────────────
    def status(self, bot_id: str) -> "dict | None": ...
    def health(self, bot_id: str, *, timeout: "int | None" = None) -> "dict | None": ...
    def models(self, bot_id: str) -> "list | None": ...
    def channels(self, bot_id: str) -> "list | None": ...
    def cron_list(self, bot_id: str, *, _err_out: "list | None" = None) -> "list | None": ...
    def cron_runs(self, bot_id: str, job_id: str, limit: int = 10) -> "list | None": ...
    def security_audit(
        self, bot_id: str, *, deep: bool = False,
        timeout: int = 60, cache_ttl: int = 0,
        _err_out: "list | None" = None,
    ) -> "dict | list | None": ...
    def approvals(self, bot_id: str) -> "dict | None": ...
    def sessions(self, bot_id: str) -> "dict | None": ...
    def keys_get(self, bot_id: str, network_path: "str | None" = None) -> "dict | None": ...
    def config_get(self, bot_id: str, key: str = "agents.defaults") -> "dict | None": ...
    def full_config_get(self, bot_id: str, network_path: "str | None" = None) -> "dict | None": ...
    def model_get(self, bot_id: str, network_path: "str | None" = None) -> "dict | None": ...
    def memory_get(self, bot_id: str, network_path: "str | None" = None) -> "dict | None": ...

    # ── mutations ────────────────────────────────────────────────────────────
    def config_set(self, bot_id: str, key: str, value: str) -> bool: ...
    def full_config_set(
        self, bot_id: str, updates: dict, network_path: "str | None" = None
    ) -> "dict | None": ...
    def full_config_set_with_error(
        self, bot_id: str, updates: dict, network_path: "str | None" = None
    ) -> "tuple[dict | None, str | None]": ...
    def model_set(
        self, bot_id: str, primary: str, fallbacks: "list[str]",
        network_path: "str | None" = None,
    ) -> bool: ...
    def memory_set(
        self, bot_id: str, provider: str, fallback: "str | None" = None,
        network_path: "str | None" = None,
    ) -> bool: ...
    def gateway_restart(self, bot_id: str, network_path: "str | None" = None) -> dict: ...
    def doctor_fix(self, bot_id: str, *, timeout: int = 20) -> "str | None": ...
    def set_identity(
        self, bot_id: str, *, agent_id: str, name: str, timeout: int = 20
    ) -> "str | None": ...


class OpenClawRuntime:
    """The default adapter — delegates to ``oc_cli`` (sudo -u <bot> openclaw …).

    Imports of ``oc_cli`` are lazy/per-method so this module loads even in
    environments where the OpenClaw CLI isn't installed (e.g. running the test
    suite against ``FakeRuntime``).
    """

    @staticmethod
    def _oc():
        import oc_cli  # ships in this package; lazy so non-OC envs can import us

        return oc_cli

    # ── reads ────────────────────────────────────────────────────────────────
    def status(self, bot_id: str) -> "dict | None":
        return self._oc().oc_status(bot_id)

    def health(self, bot_id: str, *, timeout: "int | None" = None) -> "dict | None":
        return self._oc().oc_health(bot_id, timeout=timeout)

    def models(self, bot_id: str) -> "list | None":
        return self._oc().oc_models(bot_id)

    def channels(self, bot_id: str) -> "list | None":
        return self._oc().oc_channels(bot_id)

    def cron_list(self, bot_id: str, *, _err_out: "list | None" = None) -> "list | None":
        return self._oc().oc_cron_list(bot_id, _err_out=_err_out)

    def cron_runs(self, bot_id: str, job_id: str, limit: int = 10) -> "list | None":
        return self._oc().oc_cron_runs(bot_id, job_id, limit)

    def security_audit(
        self, bot_id: str, *, deep: bool = False,
        timeout: int = 60, cache_ttl: int = 0,
        _err_out: "list | None" = None,
    ) -> "dict | list | None":
        return self._oc().oc_security_audit(
            bot_id, deep=deep, timeout=timeout, cache_ttl=cache_ttl, _err_out=_err_out
        )

    def approvals(self, bot_id: str) -> "dict | None":
        return self._oc().oc_approvals(bot_id)

    def sessions(self, bot_id: str) -> "dict | None":
        return self._oc().oc_sessions(bot_id)

    def keys_get(self, bot_id: str, network_path: "str | None" = None) -> "dict | None":
        return self._oc().oc_keys_get(bot_id, network_path=network_path)

    def config_get(self, bot_id: str, key: str = "agents.defaults") -> "dict | None":
        return self._oc().oc_config_get(bot_id, key)

    def full_config_get(self, bot_id: str, network_path: "str | None" = None) -> "dict | None":
        return self._oc().oc_full_config_get(bot_id, network_path=network_path)

    def model_get(self, bot_id: str, network_path: "str | None" = None) -> "dict | None":
        return self._oc().oc_model_get(bot_id, network_path=network_path)

    def memory_get(self, bot_id: str, network_path: "str | None" = None) -> "dict | None":
        return self._oc().oc_memory_get(bot_id, network_path=network_path)

    # ── mutations ────────────────────────────────────────────────────────────
    # network_path is forwarded as a KEYWORD (matching how oc_cli declares it and
    # how callers/tests pass it) — passing it positionally would slip past a
    # ``def stub(bot_id, updates, **kw)`` test double as an unexpected 3rd
    # positional arg. See test_openclaw_*_forwards_network_path_by_keyword.
    def config_set(self, bot_id: str, key: str, value: str) -> bool:
        return self._oc().oc_config_set(bot_id, key, value)

    def full_config_set(
        self, bot_id: str, updates: dict, network_path: "str | None" = None
    ) -> "dict | None":
        return self._oc().oc_full_config_set(bot_id, updates, network_path=network_path)

    def full_config_set_with_error(
        self, bot_id: str, updates: dict, network_path: "str | None" = None
    ) -> "tuple[dict | None, str | None]":
        return self._oc().oc_full_config_set_with_error(bot_id, updates, network_path=network_path)

    def model_set(
        self, bot_id: str, primary: str, fallbacks: "list[str]",
        network_path: "str | None" = None,
    ) -> bool:
        return self._oc().oc_model_set(bot_id, primary, fallbacks, network_path=network_path)

    def memory_set(
        self, bot_id: str, provider: str, fallback: "str | None" = None,
        network_path: "str | None" = None,
    ) -> bool:
        return self._oc().oc_memory_set(bot_id, provider, fallback, network_path=network_path)

    def gateway_restart(self, bot_id: str, network_path: "str | None" = None) -> dict:
        return self._oc().oc_gateway_restart(bot_id, network_path=network_path)

    def doctor_fix(self, bot_id: str, *, timeout: int = 20) -> "str | None":
        return self._oc().oc_doctor_fix(bot_id, timeout=timeout)

    def set_identity(
        self, bot_id: str, *, agent_id: str, name: str, timeout: int = 20
    ) -> "str | None":
        return self._oc().oc_set_identity(bot_id, agent_id=agent_id, name=name, timeout=timeout)


class FakeRuntime:
    """In-memory adapter for tests — exercise the stack with no OpenClaw/macOS.

    Seed per-bot return values via ``seed(bot_id, status=..., models=...)``;
    mutating calls record into ``self.calls`` and update the seeded state.
    """

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple] = []

    def seed(self, bot_id: str, **values: Any) -> None:
        self._state.setdefault(bot_id, {}).update(values)

    def _get(self, bot_id: str, field: str):
        return self._state.get(bot_id, {}).get(field)

    # ── reads ────────────────────────────────────────────────────────────────
    def status(self, bot_id: str): return self._get(bot_id, "status")
    def health(self, bot_id: str, *, timeout=None): return self._get(bot_id, "health")
    def models(self, bot_id: str): return self._get(bot_id, "models")
    def channels(self, bot_id: str): return self._get(bot_id, "channels")
    def cron_list(self, bot_id: str, *, _err_out=None): return self._get(bot_id, "cron_list")
    def cron_runs(self, bot_id: str, job_id: str, limit: int = 10): return self._get(bot_id, "cron_runs")
    def security_audit(self, bot_id: str, *, deep=False, timeout=60, cache_ttl=0, _err_out=None):
        return self._get(bot_id, "security_audit")
    def approvals(self, bot_id: str): return self._get(bot_id, "approvals")
    def sessions(self, bot_id: str): return self._get(bot_id, "sessions")
    def keys_get(self, bot_id: str, network_path=None): return self._get(bot_id, "keys")
    def config_get(self, bot_id: str, key="agents.defaults"): return self._get(bot_id, "config")
    def full_config_get(self, bot_id: str, network_path=None): return self._get(bot_id, "full_config")
    def model_get(self, bot_id: str, network_path=None): return self._get(bot_id, "model")
    def memory_get(self, bot_id: str, network_path=None): return self._get(bot_id, "memory")

    # ── mutations ────────────────────────────────────────────────────────────
    def config_set(self, bot_id: str, key: str, value: str) -> bool:
        self.calls.append(("config_set", bot_id, key, value)); return True

    def full_config_set(self, bot_id: str, updates: dict, network_path=None) -> "dict | None":
        self.calls.append(("full_config_set", bot_id, updates))
        self.seed(bot_id, full_config=updates); return updates

    def full_config_set_with_error(
        self, bot_id: str, updates: dict, network_path=None
    ) -> "tuple[dict | None, str | None]":
        self.calls.append(("full_config_set_with_error", bot_id, updates))
        self.seed(bot_id, full_config=updates); return (updates, None)

    def model_set(self, bot_id: str, primary: str, fallbacks, network_path=None) -> bool:
        self.calls.append(("model_set", bot_id, primary, list(fallbacks)))
        self.seed(bot_id, model={"primary": primary, "fallback_order": list(fallbacks)})
        return True

    def memory_set(self, bot_id: str, provider: str, fallback=None, network_path=None) -> bool:
        self.calls.append(("memory_set", bot_id, provider, fallback)); return True

    def gateway_restart(self, bot_id: str, network_path=None) -> dict:
        self.calls.append(("gateway_restart", bot_id)); return {"ok": True, "method": "fake"}

    def doctor_fix(self, bot_id: str, *, timeout=20) -> "str | None":
        self.calls.append(("doctor_fix", bot_id)); return self._get(bot_id, "doctor_fix")

    def set_identity(self, bot_id: str, *, agent_id: str, name: str, timeout=20) -> "str | None":
        self.calls.append(("set_identity", bot_id, agent_id, name)); return self._get(bot_id, "set_identity")


# ── factory ─────────────────────────────────────────────────────────────────────

_runtime: "AgentRuntime | None" = None


def get_runtime() -> AgentRuntime:
    """Return the process-wide runtime adapter (OpenClaw by default)."""
    global _runtime
    if _runtime is None:
        _runtime = OpenClawRuntime()
    return _runtime


def set_runtime(runtime: "AgentRuntime | None") -> None:
    """Swap the adapter (tests inject FakeRuntime; a future port injects another).
    Pass ``None`` to reset to the default on next ``get_runtime()``."""
    global _runtime
    _runtime = runtime
