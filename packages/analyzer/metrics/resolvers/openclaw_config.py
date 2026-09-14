"""metrics.resolvers.openclaw_config — openclaw_config.valid.

1.0 if the bot's ``openclaw.json`` parses as a JSON object.
0.0 if the file is missing, unreadable, or malformed.

The canonical location is ``/Users/{bot_id}/.openclaw/openclaw.json`` but
may need to fall back to ``sudo /bin/cat`` on macOS with restrictive ACLs
(see CLAUDE.md). For L2 we try a direct read first; if that fails with
PermissionError, we try the sudoers-grant sudo path. Both paths are
testable via ``set_config_path_resolver()``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from metrics.registry import MetricSpec, MetricValue, register

from evolve_config import bot_home as _bot_home


_PathResolver = Callable[[str], Path]


def _default_path_resolver(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "openclaw.json"


_path_resolver: _PathResolver = _default_path_resolver


def set_config_path_resolver(fn: _PathResolver) -> None:
    """Test hook: override how the openclaw.json path is computed."""
    global _path_resolver
    _path_resolver = fn


def _read_json_maybe_sudo(path: Path) -> dict | None:
    """Return the parsed JSON dict, or None on any failure.

    Tries a direct read first; falls back to ``sudo /bin/cat`` per the
    CLAUDE.md convention if direct read hits PermissionError.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except PermissionError:
        # Fallback: sudoers grant
        try:
            result = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        text = result.stdout
    except OSError:
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def resolve_openclaw_config_valid(
    bot_id: str, as_of: datetime  # noqa: ARG001
) -> MetricValue:
    path = _path_resolver(bot_id)
    data = _read_json_maybe_sudo(path)
    if data is None:
        return MetricValue(
            value=0.0,
            confidence=1.0,
            source_note=f"config unreadable or malformed at {path}",
        )
    return MetricValue(
        value=1.0,
        confidence=1.0,
        source_note=f"config valid at {path}",
    )


register(
    MetricSpec(
        name="openclaw_config.valid",
        description="1.0 if /Users/{bot_id}/.openclaw/openclaw.json parses as a JSON object.",
        unit="bool",
        source="/Users/{bot_id}/.openclaw/openclaw.json",
    ),
    resolve_openclaw_config_valid,
)
