"""
better_engine/storage.py — Atomic file I/O for Better Engine data.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from evolve_util import atomic_write_json

if TYPE_CHECKING:
    from .model import Recommendation


# ── Recommendations ───────────────────────────────────────────────────────────

def load_recommendations(path: Path) -> list["Recommendation"]:
    """Load recommendations from JSON file; returns [] if missing or corrupt.

    Handles both plain-list format (legacy) and dict-with-metadata format
    written by the engine when source_freshness is included.
    """
    from .model import Recommendation
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        # Support wrapped format: {"source_freshness": {...}, "recommendations": [...]}
        if isinstance(raw, dict):
            raw = raw.get("recommendations", [])
        return [Recommendation.from_dict(d) for d in raw]
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, TypeError, KeyError):
        return []


def load_source_freshness(path: Path) -> dict:
    """Load source_freshness metadata from recommendations.json; returns {} if absent."""
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        if isinstance(raw, dict):
            return raw.get("source_freshness", {})
        return {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}


def save_recommendations(recs: list["Recommendation"], path: Path) -> None:
    """Atomically write recommendations list to JSON file."""
    atomic_write_json(path, [r.to_dict() for r in recs])


# ── Learning ──────────────────────────────────────────────────────────────────

_LEARNING_DEFAULTS = {
    "signals": {},
    "bot_overrides": {},
    "recent_events": [],
}


def load_learning(path: Path) -> dict:
    """Load learning.json; returns defaults if missing or corrupt."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        # Ensure all top-level keys are present
        result = dict(_LEARNING_DEFAULTS)
        result.update(data)
        return result
    except FileNotFoundError:
        return dict(_LEARNING_DEFAULTS)
    except json.JSONDecodeError:
        return dict(_LEARNING_DEFAULTS)


def save_learning(data: dict, path: Path) -> None:
    """Atomically write learning data to JSON file."""
    atomic_write_json(path, data)


# ── Getting Started ───────────────────────────────────────────────────────────

_GETTING_STARTED_DEFAULTS = {
    "schema_version": 1,
    "started_at": None,
    "completed_at": None,
    "dismissed": False,
    "tasks": {},
}


def load_getting_started(path: Path) -> dict:
    """Load getting-started.json; returns defaults if missing or corrupt."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        result = dict(_GETTING_STARTED_DEFAULTS)
        result.update(data)
        return result
    except FileNotFoundError:
        return dict(_GETTING_STARTED_DEFAULTS)
    except json.JSONDecodeError:
        return dict(_GETTING_STARTED_DEFAULTS)


def save_getting_started(data: dict, path: Path) -> None:
    """Atomically write getting-started data to JSON file."""
    atomic_write_json(path, data)
