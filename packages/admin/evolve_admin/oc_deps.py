"""oc_deps — declarative manifest of every OpenClaw artifact Evolve reads
out-of-band, plus a real-reader probe for each.

Why this module exists
----------------------
On 2026-06-22 OpenClaw migrated each bot's
``auth-profiles.json`` into a per-agent SQLite store. Evolve's
Anthropic-key resolvers still read the now-absent JSON → returned ``""``
→ the app scanner aborted LLM discovery with ``error_kind="missing_api_key"``
pod-wide. The operator-run safe-upgrade preflight gave a SAFE verdict: its
gates encode named failure shapes scoped to plugins + gateway infra and
model NO Evolve→OpenClaw *data-file* dependencies. Nothing exercised the
real readers, so a silent data-layout migration sailed through.

This module closes that gap. ``OC_DEPENDENCIES`` is the single declarative
source of truth: one record per OpenClaw artifact Evolve reads out-of-band.
Each record carries a ``probe`` that runs the **real Evolve reader** against
the live pod and reports a tri-state verdict — so the instant OpenClaw moves
a file Evolve depends on, the probe goes from ``ok`` to ``missing`` and the
safe-upgrade gate (``safe_upgrade.gate_oc_data_dependencies``) fires.

Probe contract
--------------
``probe(bot_id, *, network=None) -> ProbeResult``. Pod-scope probes ignore
``bot_id`` (called once with ``None``). Probes are **read-only, cheap, and
exception-safe** — they log on a fall-through rather than raising, and a
probe that genuinely cannot determine state returns ``indeterminate`` (never
a false ``ok`` and never a false ``missing``). The tri-state mirrors the
delivery-monitor / soak-probe rule already in the codebase: a check that
RUNS and finds breakage fails closed; a check that could not run is a tooling
fault reported as exactly that.

Verdict → gate routing (in ``gate_oc_data_dependencies``)
--------------------------------------------------------
  ok / not_applicable → PASS (no finding)
  missing / empty     → blocking FAIL  when the dependency is ``load_bearing``
                        non-blocking WARN otherwise
  indeterminate       → non-blocking WARN (tooling fault, never a hard block)

Stdlib-only at import time; the readers themselves are imported lazily inside
each probe to avoid an import cycle (``safe_upgrade`` imports this module's
gate, and the probes call back into ``safe_upgrade`` / ``oc_store`` /
``ocadmin``).
"""

from __future__ import annotations

import logging
import plistlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("evolve.oc_deps")

# ── Probe verdict vocabulary ──────────────────────────────────────────────────

PROBE_OK = "ok"                       # the real reader resolved the dependency
PROBE_EMPTY = "empty"                 # artifact present + readable, but the data
#                                       Evolve relies on is absent from it
PROBE_MISSING = "missing"             # artifact present on disk but the real
#                                       reader resolved nothing — layout/format
#                                       drift (the 2026-06-22 incident shape)
PROBE_INDETERMINATE = "indeterminate"  # could not determine (permission /
#                                       tooling fault) — never a hard block
PROBE_NOT_APPLICABLE = "not_applicable"  # the dependency does not apply to this
#                                       bot (e.g. a gateway-auth bot that never
#                                       had a raw key) — counts as PASS

# Probe scope.
SCOPE_PER_BOT = "per_bot"   # probe runs once per bot in the pod
SCOPE_POD = "pod"           # probe runs once for the whole pod

# macOS system-daemon dir. Not a /Users/ literal (platform-path-lint targets
# only /Users/). On a non-launchd pod the gateway-plist probe degrades to
# not_applicable.
_LAUNCH_DAEMONS_DIR = Path("/Library/LaunchDaemons")


@dataclass(frozen=True)
class ProbeResult:
    """A single probe's verdict. ``status`` is one of the ``PROBE_*`` constants;
    ``ok`` is the convenience bool the manifest contract advertises."""

    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (PROBE_OK, PROBE_NOT_APPLICABLE)


@dataclass(frozen=True)
class OCDependency:
    """One OpenClaw artifact Evolve reads out-of-band."""

    key: str                         # stable identifier
    artifact: str                    # path / description of what's read
    access: str                      # "sqlite" | "json" | "cli" | "plist"
    scope: str                       # SCOPE_PER_BOT | SCOPE_POD
    load_bearing: bool               # missing/empty → blocking FAIL vs WARN
    why: str                         # what Evolve breaks if this drifts
    probe: Callable[..., ProbeResult] = field(compare=False)


# ── Probes ────────────────────────────────────────────────────────────────────


def _probe_auth_store(bot_id: str | None, *, network: dict[str, Any] | None = None) -> ProbeResult:
    """Exercise the real Anthropic-key reader (``oc_store``) for one bot.

    THIS is the dependency that broke on 2026-06-22. The verdict distinguishes
    the incident shape from a legitimately keyless gateway-auth bot:

      ok             a usable anthropic key resolves, OR the store is readable
                     but carries no anthropic profile (a gateway-token bot —
                     valid, not a failure).
      missing        an auth-store artifact IS present on disk but the reader
                     resolved nothing → the reader can no longer read the store
                     (a layout/format migration drifted out from under it).
      not_applicable no auth-store artifact at all (atlas-style gateway-auth
                     bot that never had a raw key) — nothing to read.
      indeterminate  the bot's home dir could not be resolved.
    """
    from . import oc_store  # lazy: avoid import cycle

    key = oc_store.read_anthropic_key(bot_id, network=network)
    if key:
        return ProbeResult(PROBE_OK, "anthropic api_key resolved via the live reader")
    # No key. Distinguish "store readable, just no anthropic profile"
    # (gateway-auth, fine) from "store unreadable" (the incident shape).
    raw = oc_store.read_auth_store(bot_id, network=network)
    if raw:
        return ProbeResult(
            PROBE_OK, "auth store readable; no anthropic key (gateway-token auth bot)"
        )
    present = oc_store.auth_store_present(bot_id, network=network)
    if present is None:
        return ProbeResult(PROBE_INDETERMINATE, "could not resolve a home dir for the bot")
    if present:
        return ProbeResult(
            PROBE_MISSING,
            "auth-store artifact present on disk but the reader resolved nothing "
            "— reader/format drift (the 2026-06-22 incident shape)",
        )
    return ProbeResult(
        PROBE_NOT_APPLICABLE,
        "no auth-store artifact present (gateway-auth bot, never had a raw key)",
    )


def _probe_plugin_install_registry(
    bot_id: str | None, *, network: dict[str, Any] | None = None
) -> ProbeResult:
    """Exercise the plugin install-registry reader (``safe_upgrade``).

    Encodes the json→sqlite migration precedent honestly: a registry that is
    present on disk but unreadable in any known format is the drift signature
    (the same shape that produced a false NOT-SAFE on the 2026.6.1 → 2026.6.5
    check before ``_install_state_determinable`` was added)."""
    if bot_id is None:
        return ProbeResult(PROBE_INDETERMINATE, "no bot_id supplied to a per-bot probe")
    network = network or {}
    from . import safe_upgrade as _su  # lazy: avoid import cycle
    from .ocadmin import _gateway_runtime_user  # lazy: avoid import cycle

    user = _gateway_runtime_user(bot_id, network)
    if not _su._install_registry_exists(user):
        return ProbeResult(
            PROBE_NOT_APPLICABLE, "no plugin-install registry on disk (bot installed no plugins)"
        )
    data = _su._read_installs_json(user)
    if data is None:
        return ProbeResult(
            PROBE_MISSING,
            "install registry present but unreadable in any known format — on-disk layout drift",
        )
    records = data.get("installRecords") if isinstance(data, dict) else None
    n = len(records) if isinstance(records, dict) else 0
    source = data.get("_source", "legacy json") if isinstance(data, dict) else "?"
    return ProbeResult(PROBE_OK, f"install registry readable ({n} record(s)) via {source}")


def _probe_openclaw_json_schema(
    bot_id: str | None, *, network: dict[str, Any] | None = None
) -> ProbeResult:
    """Exercise the openclaw.json reader (``safe_upgrade._read_bot_openclaw_json``)
    and confirm the top-level ``gateway`` block Evolve reads is present.

    Evolve reads ``gateway.auth`` / ``gateway.port`` (and ``channels.*``) from
    each bot's openclaw.json. If the ``gateway`` key disappears, those reads
    silently resolve to nothing."""
    if bot_id is None:
        return ProbeResult(PROBE_INDETERMINATE, "no bot_id supplied to a per-bot probe")
    network = network or {}
    from . import safe_upgrade as _su  # lazy: avoid import cycle

    cfg = _su._read_bot_openclaw_json(bot_id, network)
    if cfg is None:
        return ProbeResult(PROBE_INDETERMINATE, "openclaw.json unreadable, absent, or unparseable")
    if "gateway" not in cfg:
        return ProbeResult(
            PROBE_EMPTY,
            "openclaw.json readable but has no top-level `gateway` key "
            "(Evolve reads gateway.auth / gateway.port from here)",
        )
    return ProbeResult(PROBE_OK, "openclaw.json exposes the `gateway` block Evolve reads")


def _probe_gateway_plist(
    bot_id: str | None, *, network: dict[str, Any] | None = None
) -> ProbeResult:
    """Exercise the gateway-plist reader (``ocadmin._gateway_runtime_user``).

    Evolve reads ``UserName`` from each bot's gateway launchd plist to learn
    which POSIX account the daemon actually runs as (authoritative for
    launchctl ops). Missing plists are already a hard blocker via
    ``gate_plist_paths``, so a plist-absent here is not_applicable (avoids a
    duplicate finding, and is the correct verdict on a non-launchd pod)."""
    if bot_id is None:
        return ProbeResult(PROBE_INDETERMINATE, "no bot_id supplied to a per-bot probe")
    network = network or {}
    from .ocadmin import _bot_service  # lazy: avoid import cycle

    plist = _LAUNCH_DAEMONS_DIR / f"{_bot_service(bot_id)}.plist"
    # Read directly rather than exists()-then-read: on Python 3.13+
    # ``Path.exists()`` swallows EACCES and returns False, which would mask a
    # present-but-unreadable plist as not_applicable. Classify off the read.
    try:
        raw = plist.read_bytes()
    except FileNotFoundError:
        return ProbeResult(
            PROBE_NOT_APPLICABLE,
            "no gateway launchd plist (bot not deployed, or non-launchd pod)",
        )
    except OSError as exc:
        logger.debug("oc_deps: gateway plist %s unreadable (%s)", plist, exc)
        return ProbeResult(PROBE_INDETERMINATE, f"gateway plist present but unreadable: {exc}")
    try:
        data = plistlib.loads(raw)
    except plistlib.InvalidFileException as exc:
        return ProbeResult(PROBE_MISSING, f"gateway plist present but unparseable: {exc}")
    user = data.get("UserName") if isinstance(data, dict) else None
    if isinstance(user, str) and user:
        return ProbeResult(PROBE_OK, f"gateway plist resolves UserName={user}")
    return ProbeResult(PROBE_EMPTY, "gateway plist present but has no UserName key")


def _probe_message_send_cli(
    bot_id: str | None, *, network: dict[str, Any] | None = None
) -> ProbeResult:
    """Exercise the message-send CLI contract probe (``safe_upgrade``).

    Pod-scope — the CLI is installed once per host. The dedicated
    ``gate_send_surface`` owns the BLOCKING verdict for this surface, so this
    manifest entry is not load-bearing: a FAILED surface surfaces here as a
    WARN (one fix, surfaced twice would be redundant), while the catalog stays
    complete."""
    from . import safe_upgrade as _su  # lazy: avoid import cycle

    probe = _su.probe_send_surface()
    status = probe.get("status")
    if status == _su.SEND_SURFACE_OK:
        return ProbeResult(PROBE_OK, "`message send --help` exposes every required flag")
    if status == _su.SEND_SURFACE_FAILED:
        missing = probe.get("missing_flags") or []
        detail = (
            f"missing flags: {', '.join(missing)}"
            if missing
            else (probe.get("reason") or "surface broken")
        )
        return ProbeResult(PROBE_MISSING, f"send surface broken ({detail})")
    return ProbeResult(PROBE_INDETERMINATE, f"send surface unverified ({probe.get('reason')})")


# ── The manifest ──────────────────────────────────────────────────────────────

OC_DEPENDENCIES: list[OCDependency] = [
    OCDependency(
        key="auth_store",
        artifact="~/.openclaw/agents/main/agent/openclaw-agent.sqlite "
        "(auth_profile_store) → legacy auth-profiles.json → .sqlite-import.*.bak",
        access="sqlite",
        scope=SCOPE_PER_BOT,
        load_bearing=True,
        why="Evolve resolves each bot's Anthropic api_key from here; if the reader "
        "can't read the store, the app scanner aborts LLM discovery with "
        "error_kind=missing_api_key pod-wide (the 2026-06-22 incident).",
        probe=_probe_auth_store,
    ),
    OCDependency(
        key="plugin_install_registry",
        artifact="~/.openclaw/state/openclaw.sqlite (installed_plugin_index) → "
        "legacy plugins/installs.json[.migrated]",
        access="sqlite",
        scope=SCOPE_PER_BOT,
        load_bearing=False,  # downstream gates fail-safe; drift degrades, not breaks
        why="The config_references gate reads installed plugin ids from here to "
        "decide whether an upgrade drops a plugin a bot relies on. A format "
        "migration that out-runs the reader degrades that gate to fail-safe.",
        probe=_probe_plugin_install_registry,
    ),
    OCDependency(
        key="openclaw_json_schema",
        artifact="~/.openclaw/openclaw.json (top-level `gateway` / `channels` keys)",
        access="json",
        scope=SCOPE_PER_BOT,
        load_bearing=False,  # gate_bot_config_schema validates configs more deeply
        why="Evolve reads gateway.auth / gateway.port and channels.* out-of-band; "
        "if the `gateway` block moves or is renamed, those reads silently "
        "resolve to nothing.",
        probe=_probe_openclaw_json_schema,
    ),
    OCDependency(
        key="gateway_plist",
        artifact="/Library/LaunchDaemons/ai.openclaw.<bot>-gateway.plist (UserName)",
        access="plist",
        scope=SCOPE_PER_BOT,
        load_bearing=False,  # degrades to network/bot_id fallback; gate_plist_paths blocks
        why="Evolve reads UserName to learn which POSIX account the gateway daemon "
        "runs as (authoritative for launchctl ops). A label/format change "
        "drops Evolve to a config/bot_id fallback that can be wrong.",
        probe=_probe_gateway_plist,
    ),
    OCDependency(
        key="message_send_cli",
        artifact="`openclaw message send` CLI contract (--channel/--target/--message/--json)",
        access="cli",
        scope=SCOPE_POD,
        load_bearing=False,  # gate_send_surface owns the blocking verdict
        why="Every scheduled gallery delivery sends through this CLI surface. A "
        "removed/renamed flag breaks delivery pod-wide (the 2026-06-11 P0). "
        "The dedicated send_surface gate is the blocking owner; this entry "
        "keeps the dependency catalog complete.",
        probe=_probe_message_send_cli,
    ),
]
