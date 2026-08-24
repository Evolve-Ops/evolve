"""arbiter.appliers.config_patch — Patch a JSON config file.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §5.5.

The existing ``packages/analyzer/apply.py`` owns the canonical safety
gates for patching a bot's ``openclaw.json`` (config blocklist, backup
before patch, gateway restart with rollback on failure, signature
verification). L1's ConfigPatch applier is a **generic JSON patcher**
that operates on any config file path and is tested in isolation; the
integration with bot-specific openclaw.json and its safety gates lands
in L2 when Sysadmin Watchdog first needs it.

This keeps L1's tests hermetic (no dependency on a real bot workspace)
while preserving the spec's Chesterton's-fence commitment — the L2
wrapper will call into ``apply.py`` rather than reimplement.

Operations:
  - ``set``:    write ``value`` at ``target_path``
  - ``unset``:  remove key at ``target_path``
  - ``merge``:  deep-merge ``value`` (must be a dict) into ``target_path``
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json as _atomic_write_json

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import ConfigPatch


_MISSING = object()

# Defensive guard: openclaw.json mutations must NOT route through the
# generic L1 patcher. Its atomic-rename pattern uses
# ``tempfile.mkstemp(dir=path.parent)`` which fails on member bots whose
# ``/Users/<bot>/.openclaw/`` is bot-owned with evolve having only a
# read-only ACL. The correct routing is UpdatePermissionConfig →
# UpdatePermissionConfigApplier → permissions.writer.write_openclaw_fields
# (canonical /tmp + sudo helper, with field-whitelist enforcement).
# See internal/diagnosis-openclaw-json-write-regression-2026-05-21.md.
_BOT_OPENCLAW_JSON_RE = re.compile(
    r"^/Users/[^/]+/\.openclaw/openclaw\.json(::|$)"
)
_BOT_OPENCLAW_REFUSAL = (
    "ConfigPatchApplier is not for bot openclaw.json. "
    "Use UpdatePermissionConfig action kind instead — it routes "
    "through UpdatePermissionConfigApplier with proper /tmp + sudo "
    "staging and field-whitelist enforcement. "
    "(See internal/diagnosis-openclaw-json-write-regression-2026-05-21.md)"
)


def _parse_target_path(target: str) -> tuple[Path, list[str]]:
    """Split a ConfigPatch.target_path into (file_path, key_path).

    Convention: the path is ``<file>::<dotted.key.path>``.

    Example:
        "/tmp/config.json::ui.theme" → (Path("/tmp/config.json"), ["ui", "theme"])

    If no ``::`` separator is present, the entire string is taken as the
    file path and the key_path is empty (operation applies to the entire
    document, which is valid for ``set`` if ``value`` is a dict).
    """
    if "::" in target:
        file_part, key_part = target.split("::", 1)
        keys = [k for k in key_part.split(".") if k]
    else:
        file_part, keys = target, []
    return Path(file_part), keys


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a JSON object")
    return data


def _get_at(data: dict, keys: list[str]) -> Any:
    """Return the value at ``keys`` or ``_MISSING`` if any segment is absent."""
    cur: Any = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return _MISSING
        cur = cur[k]
    return cur


def _set_at(data: dict, keys: list[str], value: Any) -> None:
    """Set ``value`` at ``keys`` in ``data``, creating dicts as needed."""
    if not keys:
        # Whole-document replace requires a dict
        if not isinstance(value, dict):
            raise ValueError("set at root requires dict value")
        data.clear()
        data.update(value)
        return
    cur = data
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def _unset_at(data: dict, keys: list[str]) -> bool:
    """Remove the value at ``keys``. Return True if something was removed."""
    if not keys:
        if data:
            data.clear()
            return True
        return False
    cur = data
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            return False
        cur = cur[k]
    if keys[-1] in cur:
        del cur[keys[-1]]
        return True
    return False


def _deep_merge(dst: dict, src: dict) -> None:
    """Deep-merge ``src`` into ``dst`` (mutates dst in place)."""
    for k, v in src.items():
        if (
            k in dst
            and isinstance(dst[k], dict)
            and isinstance(v, dict)
        ):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


class ConfigPatchApplier:
    """Patch a JSON config file via dotted target_path + operation."""

    def capture_snapshot(self, action: ConfigPatch, bot_id: str) -> dict:
        file_path, keys = _parse_target_path(action.target_path)
        data = _read_json(file_path) if file_path.exists() else {}
        prior = _get_at(data, keys)
        return {
            "action_kind": "ConfigPatch",
            "bot_id": bot_id,
            "file_path": str(file_path),
            "keys": list(keys),
            "existed_before": prior is not _MISSING,
            "prior_value": None if prior is _MISSING else prior,
            "file_existed_before": file_path.exists(),
        }

    def apply(self, action: ConfigPatch, bot_id: str) -> ApplyResult:
        # Refuse bot openclaw.json paths — see _BOT_OPENCLAW_JSON_RE comment.
        if _BOT_OPENCLAW_JSON_RE.match(action.target_path or ""):
            return ApplyResult(
                ok=False,
                details={"error": "wrong_applier_for_openclaw_json"},
                message=_BOT_OPENCLAW_REFUSAL,
            )

        file_path, keys = _parse_target_path(action.target_path)
        data = _read_json(file_path) if file_path.exists() else {}

        if action.operation == "set":
            _set_at(data, keys, action.value)
            msg = f"set {action.target_path!r}"
        elif action.operation == "unset":
            changed = _unset_at(data, keys)
            if not changed:
                return ApplyResult(
                    ok=True,
                    details={"no_op": True},
                    message=f"unset {action.target_path!r}: already absent",
                )
            msg = f"unset {action.target_path!r}"
        elif action.operation == "merge":
            if not isinstance(action.value, dict):
                return ApplyResult(
                    ok=False,
                    details={"error": "merge value must be a dict"},
                    message="ConfigPatch merge requires a dict value",
                )
            target = _get_at(data, keys)
            if target is _MISSING or not isinstance(target, dict):
                _set_at(data, keys, dict(action.value))
            else:
                _deep_merge(target, action.value)
            msg = f"merge into {action.target_path!r}"
        else:
            return ApplyResult(
                ok=False,
                details={"error": f"unknown operation {action.operation!r}"},
                message=f"ConfigPatch: unknown operation {action.operation!r}",
            )

        file_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(file_path, data, sort_keys=True)
        return ApplyResult(
            ok=True,
            details={"file_path": str(file_path), "operation": action.operation},
            message=msg,
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        file_path_str = snapshot.get("file_path", "")
        if not file_path_str:
            return RevertResult(ok=False, message="snapshot missing file_path")

        file_path = Path(file_path_str)
        keys = list(snapshot.get("keys") or [])
        existed = bool(snapshot.get("existed_before"))
        file_existed = bool(snapshot.get("file_existed_before"))
        prior_value = snapshot.get("prior_value")

        # Case 1: file didn't exist before apply. Revert deletes the file.
        if not file_existed:
            try:
                if file_path.exists():
                    file_path.unlink()
            except OSError as e:
                return RevertResult(
                    ok=False,
                    details={"error": str(e)},
                    message=f"Failed to delete {file_path}: {e}",
                )
            return RevertResult(
                ok=True,
                details={"file_path": str(file_path), "deleted": True},
                message=f"Deleted {file_path} (did not exist before apply)",
            )

        # Case 2: file existed, key didn't. Revert by unsetting the key.
        data = _read_json(file_path)
        if not existed:
            _unset_at(data, keys)
        else:
            _set_at(data, keys, prior_value)

        try:
            _atomic_write_json(file_path, data, sort_keys=True)
        except OSError as e:
            return RevertResult(
                ok=False,
                details={"error": str(e)},
                message=f"Failed to write {file_path}: {e}",
            )
        return RevertResult(
            ok=True,
            details={"file_path": str(file_path), "keys": keys, "restored": existed},
            message=f"Reverted {file_path} at {'.'.join(keys) or '<root>'}",
        )


register_applier("ConfigPatch", ConfigPatchApplier())
