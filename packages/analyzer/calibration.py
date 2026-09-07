"""
calibration.py — Calibration layer for Evolve's RSI parameters.

Two-tier loading: local calibration (RSI-learned, persists across upgrades)
overrides community defaults (shipped with the code, may change on upgrade).

  Community defaults: packages/analyzer/calibration_defaults/{name}.json
  Local calibration:  {shared_dir}/calibration/{name}.json

The local calibration file only needs to contain keys it wants to override —
missing keys fall through to the community default. This means a new Evolve
release can add new parameters with sensible defaults without breaking
existing installations, and existing local overrides are preserved.

Typical usage:

    from calibration import CalibrationLoader
    cal = CalibrationLoader(shared_dir="/Users/Shared/evolve")

    # Read one parameter with fallback chain:
    threshold = cal.get("detectors", "high_maintenance_ratio.avg_ratio_threshold")

    # Or load a whole section as a dict:
    detector_cfg = cal.detector("high_maintenance_ratio")
    threshold = detector_cfg["avg_ratio_threshold"]

Writing back to local calibration (RSI auto-update):

    cal.set("detectors", "high_maintenance_ratio.confidence_multiplier", 1.2)
    # This writes atomically to {shared_dir}/calibration/detectors.json,
    # leaving the community defaults file untouched.

Migration:

    When Evolve upgrades and schema_version bumps, the loader detects the
    version mismatch and runs the registered migration function before
    returning data. Migrations are one-directional (N → N+1 only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ── Paths ──────────────────────────────────────────────────────────────────────

_DEFAULTS_DIR = Path(__file__).parent / "calibration_defaults"

# Files managed by this module
CALIBRATION_FILES = ("detectors", "classifier", "measure", "outcomes", "audit", "prompts", "forge")


# ── Migrations ─────────────────────────────────────────────────────────────────
# Register a migration as: _MIGRATIONS[file_name][from_version] = fn(data) -> data

_MIGRATIONS: dict[str, dict[int, Any]] = {
    f: {} for f in CALIBRATION_FILES
}


# ── Core loader ───────────────────────────────────────────────────────────────

class CalibrationLoader:
    """
    Loads calibration with a two-tier fallback:
        local override (shared_dir/calibration/) > community default (calibration_defaults/)

    All get/set operations are scoped to a named file (e.g. "detectors").
    Dot-notation is used for nested keys (e.g. "high_maintenance_ratio.confidence_base").
    """

    def __init__(self, shared_dir: str | Path) -> None:
        self._shared_dir = Path(shared_dir)
        self._local_dir = self._shared_dir / "calibration"
        self._cache: dict[str, dict] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    def load(self, name: str) -> dict:
        """
        Return the merged calibration dict for `name` (e.g. "detectors").
        Result is cached per name per loader instance.
        """
        if name not in self._cache:
            self._cache[name] = self._merged(name)
        return self._cache[name]

    def get(self, name: str, key: str, default: Any = None) -> Any:
        """
        Read one value by dot-notation key from the merged calibration.

        Example:
            cal.get("detectors", "high_maintenance_ratio.avg_ratio_threshold")
        """
        data = self.load(name)
        return _deep_get(data, key, default)

    def detector(self, detector_name: str) -> dict:
        """
        Convenience: return the config dict for one named detector.
        Falls back to empty dict if the detector isn't in the file at all.
        """
        data = self.load("detectors")
        return data.get("detectors", {}).get(detector_name, {})

    def set(self, name: str, key: str, value: Any) -> None:
        """
        Write one value into the local calibration file (atomic write).
        Creates the file and its parent directory if they don't exist.
        Only the local file is modified — community defaults are never touched.
        """
        local_data = self._load_local(name)
        _deep_set(local_data, key, value)
        self._write_local(name, local_data)
        # Invalidate cache so next load() re-merges
        self._cache.pop(name, None)

    def set_detector_outcome_stat(
        self, detector_name: str, stat: str, value: Any
    ) -> None:
        """
        Convenience: update one outcome stat for a detector.
        Creates the detector entry in local calibration if absent.

        Example:
            cal.set_detector_outcome_stat("high_maintenance_ratio", "thumbs_up", 3)
        """
        local_data = self._load_local("detectors")
        detectors = local_data.setdefault("detectors", {})
        det = detectors.setdefault(detector_name, {})
        stats = det.setdefault("outcome_stats", {"thumbs_up": 0, "thumbs_down": 0, "expired": 0})
        stats[stat] = value
        self._write_local("detectors", local_data)
        self._cache.pop("detectors", None)

    def update_confidence_multiplier(
        self,
        detector_name: str,
        thumbs_up: int,
        thumbs_down: int,
        expired: int,
    ) -> float:
        """
        Recompute and persist a detector's confidence_multiplier based on
        accumulated outcome statistics.

        Formula:
          - positive_rate = thumbs_up / (thumbs_up + thumbs_down) ignoring expired
          - positive_rate > 0.70: nudge multiplier up by 0.05 (max 1.50)
          - positive_rate < 0.40: nudge multiplier down by 0.05 (min 0.30)
          - else: no change

        Returns the new multiplier value.
        """
        local_data = self._load_local("detectors")
        detectors = local_data.setdefault("detectors", {})
        det = detectors.setdefault(detector_name, {})

        current = float(det.get("confidence_multiplier", 1.0))
        judged = thumbs_up + thumbs_down
        if judged < 3:
            # Not enough data to recalibrate
            return current

        positive_rate = thumbs_up / judged
        if positive_rate > 0.70:
            new_multiplier = min(1.50, round(current + 0.05, 3))
        elif positive_rate < 0.40:
            new_multiplier = max(0.30, round(current - 0.05, 3))
        else:
            return current

        det["confidence_multiplier"] = new_multiplier
        self._write_local("detectors", local_data)
        self._cache.pop("detectors", None)
        return new_multiplier

    def effective_confidence(self, detector_name: str) -> float:
        """
        Return the effective confidence for a detector:
            min(0.95, confidence_base * confidence_multiplier)
        Falls back to confidence_base if no multiplier is set.
        """
        cfg = self.detector(detector_name)
        base = float(cfg.get("confidence_base", 0.70))
        multiplier = float(cfg.get("confidence_multiplier", 1.0))
        return min(0.95, round(base * multiplier, 3))

    # ── Internal ───────────────────────────────────────────────────────────────

    def _merged(self, name: str) -> dict:
        """Merge community defaults with local overrides (local wins on conflict)."""
        defaults = self._load_defaults(name)
        local = self._load_local(name)
        return _deep_merge(defaults, local)

    def _load_defaults(self, name: str) -> dict:
        path = _DEFAULTS_DIR / f"{name}.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            return _migrate(name, data)
        except Exception:
            return {}

    def _load_local(self, name: str) -> dict:
        path = self._local_dir / f"{name}.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            return _migrate(name, data)
        except Exception:
            return {}

    def _write_local(self, name: str, data: dict) -> None:
        """Atomically write local calibration file, adding schema_version."""
        self._local_dir.mkdir(parents=True, exist_ok=True)
        defaults = self._load_defaults(name)
        data["schema_version"] = defaults.get("schema_version", 1)
        out = self._local_dir / f"{name}.json"
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(out)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _deep_get(data: dict, key: str, default: Any = None) -> Any:
    """Read a value by dot-separated path from a nested dict."""
    parts = key.split(".")
    node = data
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _deep_set(data: dict, key: str, value: Any) -> None:
    """Write a value by dot-separated path into a nested dict (mutates in place)."""
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge `override` into `base`. For dict values, recurse.
    For all other types, override wins. Neither input is mutated.
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _migrate(name: str, data: dict) -> dict:
    """Apply registered migrations to bring data up to the current schema version."""
    migrations = _MIGRATIONS.get(name, {})
    version = data.get("schema_version", 1)
    while version in migrations:
        data = migrations[version](data)
        version = data.get("schema_version", version + 1)
    return data


# ── Module-level convenience (shared_dir resolved lazily) ─────────────────────

_default_loader: CalibrationLoader | None = None


def get_loader(shared_dir: str | Path | None = None) -> CalibrationLoader:
    """
    Return a module-level loader. Pass shared_dir on first call to initialize.
    Subsequent calls without shared_dir reuse the existing loader.
    """
    global _default_loader
    if shared_dir is not None:
        _default_loader = CalibrationLoader(shared_dir)
    if _default_loader is None:
        _default_loader = CalibrationLoader("/Users/Shared/evolve")
    return _default_loader
