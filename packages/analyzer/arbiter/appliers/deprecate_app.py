"""DeprecateApp applier — mark an app manifest as deprecated.

Spec §5. Sets ``status: deprecated`` on the bot's app manifest JSON.
Supports revert (restores prior status). Requires the manifest file to
exist; refuses to create a manifest from scratch (that's a feature, not
a deprecation action).
"""

from __future__ import annotations

import json
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import DeprecateApp

from evolve_config import bot_home as _bot_home


def _manifest_path(bot_id: str, app_id: str) -> Path:
    # Mirror the correlator's lookup path from the existing codebase.
    return _bot_home(bot_id) / ".openclaw" / "workspace" / "manifests" / f"{app_id}.json"


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class DeprecateAppApplier:
    def capture_snapshot(
        self, action: DeprecateApp, bot_id: str
    ) -> dict:
        path = _manifest_path(bot_id, action.app_id)
        manifest = _read_json(path)
        return {
            "action_kind": "DeprecateApp",
            "path": str(path),
            "prior_manifest": manifest,
        }

    def apply(self, action: DeprecateApp, bot_id: str) -> ApplyResult:
        path = _manifest_path(bot_id, action.app_id)
        manifest = _read_json(path)
        if manifest is None:
            return ApplyResult(
                ok=False,
                details={"reason": "manifest_missing"},
                message=f"no manifest for app {action.app_id!r}",
            )
        manifest["status"] = "deprecated"
        manifest["deprecation_reason"] = action.reason
        if action.replacement_app_id:
            manifest["replacement_app_id"] = action.replacement_app_id
        if action.days_inactive:
            manifest["days_inactive"] = action.days_inactive

        try:
            _atomic_write_json(path, manifest, sort_keys=True)
        except OSError as e:
            return ApplyResult(ok=False, message=f"write failed: {e}")
        return ApplyResult(
            ok=True,
            details={"app_id": action.app_id},
            message=f"deprecated {action.app_id}",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        path = Path(snapshot.get("path", ""))
        prior = snapshot.get("prior_manifest")
        if prior is None:
            # Nothing existed before → delete
            try:
                if path.exists():
                    path.unlink()
            except OSError as e:
                return RevertResult(ok=False, message=f"delete failed: {e}")
            return RevertResult(ok=True, message=f"deleted {path}")
        try:
            # The manifests dir may have been wiped since the snapshot —
            # recreate it so a revert can always restore the prior file.
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, prior, sort_keys=True)
        except OSError as e:
            return RevertResult(ok=False, message=f"restore failed: {e}")
        return RevertResult(ok=True, message=f"restored {path}")


register_applier("DeprecateApp", DeprecateAppApplier())
