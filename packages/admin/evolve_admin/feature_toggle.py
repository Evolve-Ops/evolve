"""feature_toggle.py — Write side of install.json::features.<name>.

The read side lives in ``packages/analyzer/install_profile.py``
(:func:`is_feature_enabled`, :func:`get_feature_profile`,
:func:`get_feature_config`). That module is intentionally read-only so
gating decisions taken anywhere in the codebase are cheap and side-effect
free.

This module is the write counterpart. It exposes:

  - :func:`set_feature_enabled`: write the per-feature override flag
    and (for features with a launchd job) install or uninstall the
    plist so the runtime state matches the new gate value.
  - :func:`get_feature_status`: a single shape covering enabled state,
    source ("explicit" / "profile default" / "off by default"),
    plist-installed flag, and optional "last activity" timestamp for
    the UI to render "ran 4m ago" hints.

Used by the admin UI's ``/api/features`` endpoint, but kept independent
of Flask so the CLI can share the same path. The CLI's
``evolve-admin features set`` does its own install.json write today;
follow-on work could converge them onto this helper.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# ── Analyzer-side gating helpers (read side) ────────────────────────────────
#
# install_profile ships in the installed evolve-analyzer package.

try:
    from install_profile import (  # type: ignore[import-not-found]
        DEFAULT_PROFILE,
        PROFILE_DEFAULTS,
        get_feature_profile,
        is_feature_enabled,
    )
except ImportError:  # pragma: no cover — defensive
    # Fall back to a None marker; callers handle this via the same error
    # surface they'd use for any other gate failure.
    DEFAULT_PROFILE = "standard"  # type: ignore[assignment]
    PROFILE_DEFAULTS = {}  # type: ignore[assignment]

    def get_feature_profile(*_a, **_kw):  # type: ignore[no-redef]
        return DEFAULT_PROFILE

    def is_feature_enabled(*_a, **_kw):  # type: ignore[no-redef]
        return False


# ── Known features with a launchd job ───────────────────────────────────────
#
# Map feature name → callable producing (install_fn, uninstall_fn, plist_label).
# Lazy because deploy.py is heavyweight to import. Extend this catalog when a
# new toggle-able feature ships.


def _watcher_handlers() -> dict[str, dict[str, Any]]:
    """Resolve handlers for features that own a launchd job.

    Returns one row per feature with:
      - install_fn(result, shared_dir) — force-install (skips gate)
      - uninstall_fn(result)            — bootout + rm
      - plist_label: str                — used to detect "installed?" state
      - state_path: callable(shared_dir) -> Path | None for "last ran" mtime
    """
    from . import deploy  # local import; deploy.py is heavy

    return {
        "inbound_issues_watcher": {
            "install_fn": deploy.install_inbound_issues_watcher_now,
            "uninstall_fn": deploy.uninstall_inbound_issues_watcher_now,
            "plist_label": deploy.INBOUND_ISSUES_WATCHER_LABEL,
            "state_path": lambda sd: (
                Path(sd) / "inbound_issues_watcher" / "state.json"
            ),
        },
    }


_LAUNCHD_DIR = Path("/Library/LaunchDaemons")


# ── install.json read/write ────────────────────────────────────────────────


def _install_json_path(shared_dir: Path | str) -> Path:
    return Path(shared_dir) / "install.json"


def _read_install_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_install_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic write of install.json with /tmp + sudo cp fallback.

    The admin daemon runs as ``evolve`` and owns ``/Users/Shared/evolve``,
    so the direct write path almost always succeeds. The sudo fallback is
    a belt-and-suspenders for installs where the dir got chmod'd unexpectedly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".install-",
        suffix=".json.tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        try:
            os.replace(tmp, path)
            return
        except (PermissionError, OSError):
            pass
        # Fall back to /tmp + sudo cp (CLAUDE.md write pattern). Use mkstemp,
        # not a predictable name: /tmp is world-writable and `cp` follows
        # symlinks, so a fixed /tmp/evolve-install-<pid>.json is a TOCTOU/
        # symlink target. O_EXCL + a random suffix close that race.
        ofd, tmp_other_name = tempfile.mkstemp(dir="/tmp", prefix="evolve-stage-", suffix=".json")
        tmp_other = Path(tmp_other_name)
        with os.fdopen(ofd, "w") as of:
            of.write(payload)
        os.chmod(tmp_other_name, 0o600)
        subprocess.run(
            ["sudo", "/bin/cp", str(tmp_other), str(path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(path)],
            check=False, capture_output=True,
        )
        tmp_other.unlink(missing_ok=True)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── Public read API ────────────────────────────────────────────────────────


def get_feature_status(
    name: str, *, shared_dir: Path | str,
) -> dict[str, Any]:
    """Return one feature's resolved state for the UI.

    Shape (intentionally flat — JSON-friendly for the API surface):
      {
        name, enabled, source ("explicit"|"profile default"|"off by default"),
        profile, plist_installed, last_activity_at, has_launchd_job,
        on_dev_profile,
      }

    ``last_activity_at`` is the mtime (ISO-8601) of the feature's state
    file when one exists, or None. Used by the UI as a "ran 4m ago" hint.
    """
    shared = Path(shared_dir)
    install_json = _install_json_path(shared)
    enabled = is_feature_enabled(name, install_json)
    profile = get_feature_profile(install_json)

    # Determine the source of the truth (for the UI subtitle).
    data = _read_install_json(install_json)
    features_block = data.get("features") if isinstance(data, dict) else None
    entry = features_block.get(name) if isinstance(features_block, dict) else None
    if isinstance(entry, dict) and "enabled" in entry:
        source = "explicit"
    elif name in PROFILE_DEFAULTS.get(profile, frozenset()):
        source = "profile default"
    else:
        source = "off by default"

    handlers = _watcher_handlers().get(name)
    has_launchd_job = handlers is not None
    plist_installed = False
    last_activity_at: str | None = None
    if has_launchd_job:
        plist_path = _LAUNCHD_DIR / f"{handlers['plist_label']}.plist"
        plist_installed = plist_path.exists()
        try:
            state_path = handlers["state_path"](shared)
            if state_path and state_path.exists():
                from datetime import datetime, timezone
                mtime = state_path.stat().st_mtime
                last_activity_at = datetime.fromtimestamp(
                    mtime, tz=timezone.utc,
                ).isoformat(timespec="seconds")
        except (OSError, KeyError, AttributeError):
            pass

    return {
        "name": name,
        "enabled": enabled,
        "source": source,
        "profile": profile,
        "has_launchd_job": has_launchd_job,
        "plist_installed": plist_installed,
        "last_activity_at": last_activity_at,
        "on_dev_profile": name in PROFILE_DEFAULTS.get("developer", frozenset()),
    }


def list_features(*, shared_dir: Path | str) -> dict[str, Any]:
    """Return the full feature catalog with resolved state.

    Returns ``{feature_profile, features: [...]}``. The features list
    contains one entry per name catalogued in any
    :data:`install_profile.PROFILE_DEFAULTS` value — i.e. every "power
    feature" that can be toggled, regardless of whether it's currently on.
    """
    shared = Path(shared_dir)
    install_json = _install_json_path(shared)
    catalogued = sorted(
        {name for names in PROFILE_DEFAULTS.values() for name in names}
    )
    return {
        "feature_profile": get_feature_profile(install_json),
        "default_profile": DEFAULT_PROFILE,
        "features": [
            get_feature_status(name, shared_dir=shared) for name in catalogued
        ],
    }


# ── Public write API ──────────────────────────────────────────────────────


class FeatureToggleError(Exception):
    """Raised on a non-recoverable error during set_feature_enabled."""


def set_feature_enabled(
    name: str, enabled: bool, *,
    shared_dir: Path | str,
) -> dict[str, Any]:
    """Flip a feature's explicit override + install/uninstall its launchd job.

    Writes ``install.json::features.<name>.enabled``. For features with a
    registered launchd handler, also calls install / uninstall so the
    runtime state matches the new gate.

    Returns a status dict with ``ok``, ``enabled``, ``log`` (lines from
    the install/uninstall path — surfaced to the UI for transparency),
    and the refreshed :func:`get_feature_status` result.

    Raises :class:`FeatureToggleError` only for catastrophic failures
    (install.json unwritable, unknown feature). launchd install/uninstall
    failures are surfaced via ``log`` but don't fail the call — the
    install.json change is still durable, and the operator can re-run
    ``install-infra-jobs`` if launchd needs a retry.
    """
    if not isinstance(name, str) or not name:
        raise FeatureToggleError("feature name is required")

    catalogued = {n for names in PROFILE_DEFAULTS.values() for n in names}
    if name not in catalogued:
        raise FeatureToggleError(
            f"unknown feature {name!r}; catalogued: {sorted(catalogued)}"
        )

    shared = Path(shared_dir)
    install_json = _install_json_path(shared)
    data = _read_install_json(install_json)
    features_block = data.get("features")
    if not isinstance(features_block, dict):
        features_block = {}
    entry = features_block.get(name)
    if not isinstance(entry, dict):
        entry = {}
    entry["enabled"] = bool(enabled)
    features_block[name] = entry
    data["features"] = features_block
    try:
        _write_install_json(install_json, data)
    except Exception as exc:
        raise FeatureToggleError(f"install.json write failed: {exc}") from exc

    # Install or uninstall the launchd job, if this feature has one.
    log_lines: list[str] = [f"install.json: features.{name}.enabled = {bool(enabled)}"]
    handlers = _watcher_handlers().get(name)
    if handlers is not None:
        # Lazy import so test fixtures that stub deploy don't get bypassed.
        from .deploy import DeployResult

        result = DeployResult(bot_id="evolve", success=True)
        try:
            if enabled:
                handlers["install_fn"](result, shared)
            else:
                handlers["uninstall_fn"](result)
            log_lines.extend(result.steps)
        except Exception as exc:  # noqa: BLE001
            log_lines.append(f"[warn] launchd op raised: {exc}")

    return {
        "ok": True,
        "enabled": bool(enabled),
        "log": log_lines,
        "status": get_feature_status(name, shared_dir=shared),
    }
