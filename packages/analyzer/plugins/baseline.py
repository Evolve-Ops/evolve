"""plugins.baseline — pod-curated plugin policy baseline.

Spec: internal/spec-plugin-posture-rework-2026-06-06.md §3 (schema v2).
Original: internal/spec-plugin-inventory-2026-05-10.md (schema v1 — superseded
for the alert layer; the inventory layer continues to use it).

Single file at ``{shared_dir}/policy/plugin-baseline.json``. Schema v2 is
intentionally narrow:

  - ``denied_plugins`` — explicit blocklist; ``plugin_denied_present`` fires
    when any bot enables one.
  - ``expected_load_paths`` — directories OpenClaw is allowed to scan for
    plugin code. ``plugin_load_path_unexpected`` fires on anything else.
  - ``required_plugins`` — kept as a schema field but defaults to empty.
    Today there is no plugin every bot must have (``evolve`` is
    tautological — its absence shows up as heartbeat loss, not a file
    diff; ``brave`` is one option among many search backends). Reserved
    for a future "every bot must have telemetry plugin X" use case.

v1 fields that were retired (``common_optional_plugins``,
``per_bot_overrides``) are accepted on read for back-compat but ignored
and dropped on next write. The migration is in-place: the next monitor
cycle reads the v1 file, computes v2 from it, and rewrites.

The original v2 design also carried ``trusted_install_sources`` for a
``plugin_unverified_source`` alert. That alert was retired on
2026-06-06 (see spec §1.4 amendment) once we discovered OC's real
``installs[*].source`` values plus the separate ``clawhubChannel``
field make trust multi-dimensional. The field is still accepted on
read for back-compat with already-shipped v2 files and ignored on
write. Provenance lives on the inventory layer (read by the Plugins
page) but doesn't drive Signals.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from platform_profile import get_profile


CURRENT_VERSION = 2


# ── Locations ─────────────────────────────────────────────────────────────────

def baseline_path(shared_dir: Path) -> Path:
    return shared_dir / "policy" / "plugin-baseline.json"


# ── Data model ────────────────────────────────────────────────────────────────

def default_expected_load_paths() -> list[str]:
    """The platform-correct default supply-chain perimeter for plugin
    code: the pod's stable root-owned plugin install dir.

    OpenClaw loads the built plugin from this directory, so it legitimately
    appears in every bot's ``plugins.load.paths``. The baseline must expect
    it or ``plugin_load_path_unexpected`` fires for a wholly normal config.

    The directory is platform-divergent — ``/Users/Shared/evolve-plugin``
    on macOS, ``/var/lib/evolve-plugin`` on Linux (``PlatformProfile.
    plugin_install_dir``). A hardcoded macOS literal made the baseline
    macOS-shaped: on a fresh Linux pod every bot's load path is
    ``/var/lib/evolve-plugin``, which wasn't in the baseline →
    ``plugin_load_path_unexpected`` fired pod-wide. Deriving from the
    active profile keeps macOS byte-identical (same literal) and makes
    Linux correct.
    """
    return [get_profile().plugin_install_dir]


@dataclass
class PluginBaseline:
    version: int = CURRENT_VERSION
    required_plugins: list[str] = field(default_factory=list)
    denied_plugins: list[str] = field(default_factory=list)
    expected_load_paths: list[str] = field(
        default_factory=default_expected_load_paths
    )
    bootstrapped_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "bootstrapped_at": self.bootstrapped_at,
            "required_plugins": list(self.required_plugins),
            "denied_plugins": list(self.denied_plugins),
            "expected_load_paths": list(self.expected_load_paths),
        }


@dataclass
class ResolvedBotBaseline:
    """Effective baseline for one bot.

    v2 has no per-bot overrides — every bot sees the same policy. The
    type stays for API stability; ``bot_id`` is carried so callers can
    pass it back into details/logging without a second arg.
    """

    bot_id: str
    required: set[str]
    denied: set[str]
    expected_load_paths: list[str]


def resolve_for(baseline: PluginBaseline, bot_id: str) -> ResolvedBotBaseline:
    """Compute the effective baseline for one bot."""
    return ResolvedBotBaseline(
        bot_id=bot_id,
        required=set(baseline.required_plugins),
        denied=set(baseline.denied_plugins),
        expected_load_paths=list(baseline.expected_load_paths),
    )


# ── Read / write ──────────────────────────────────────────────────────────────

def _load_raw(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load(shared_dir: Path) -> PluginBaseline:
    """Load the pod baseline.

    Accepts both v1 (the original schema with per_bot_overrides and
    common_optional_plugins) and v2 files. v1 fields are read but their
    additional structure is discarded — the next write rewrites in v2
    shape. Callers should ensure the bootstrap has run before relying on
    this; the monitor calls write_default_if_missing() first.
    """
    raw = _load_raw(baseline_path(shared_dir))
    if raw is None:
        return PluginBaseline()

    version = int(raw.get("version") or 1)

    if version >= 2:
        # ``trusted_install_sources`` accepted on read for back-compat
        # with already-shipped v2 files but ignored — the
        # plugin_unverified_source alert it backed was retired (see
        # spec §1.4 amendment).
        return PluginBaseline(
            version=version,
            required_plugins=list(raw.get("required_plugins") or []),
            denied_plugins=list(raw.get("denied_plugins") or []),
            expected_load_paths=list(
                raw.get("expected_load_paths") or default_expected_load_paths()
            ),
            bootstrapped_at=str(raw.get("bootstrapped_at") or ""),
        )

    # v1 → v2 migration on read. The v1 ``pod_default.required_plugins``
    # held ``["evolve", "brave"]`` — both are intentionally dropped per
    # the rework spec (evolve = tautological, brave = one option among
    # many).
    pd = raw.get("pod_default") or {}
    return PluginBaseline(
        version=CURRENT_VERSION,
        required_plugins=[],
        denied_plugins=list(pd.get("denied_plugins") or []),
        expected_load_paths=list(
            pd.get("expected_load_paths") or default_expected_load_paths()
        ),
        bootstrapped_at=str(raw.get("bootstrapped_at") or ""),
    )


def write(baseline: PluginBaseline, shared_dir: Path) -> None:
    """Atomically write the pod baseline in v2 shape."""
    path = baseline_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = baseline.to_dict()
    payload["_comment"] = (
        "Pod-wide plugin baseline (v2). "
        "Spec: internal/spec-plugin-posture-rework-2026-06-06.md. "
        "Policy knobs: denied_plugins (blocklist), expected_load_paths "
        "(supply-chain perimeter), required_plugins (empty by default). "
        "Plugin enable/disable on individual bots is not governed here — "
        "the inventory layer is the source of truth for what's actually "
        "running. Install-source provenance lives on the inventory "
        "layer (advisory only); see PluginEntry for fields."
    )
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)
