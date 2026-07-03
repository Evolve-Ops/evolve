"""metrics.resolvers.version — version.currency_days_behind.

Count of days the bot's deployed Evolve version lags the latest released
version. 0 means current. Larger numbers indicate increasing staleness.

For L2 this reads from two files:
  - Deployed: ``/Users/{bot_id}/.openclaw/.evolve-version`` (a single ISO
    date string recording when the current deploy was cut)
  - Latest: ``{shared_dir}/release/latest.json`` with ``{"released_at": "YYYY-MM-DD"}``

Both sources are optional; resolver returns confidence < 1.0 when data
is incomplete. Tests override via ``set_version_sources()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from metrics.registry import MetricSpec, MetricValue, register

from evolve_config import bot_home as _bot_home


@dataclass
class VersionSources:
    deployed_date: date | None
    latest_date: date | None


_Sources = Callable[[str], VersionSources]


def _default_sources(bot_id: str) -> VersionSources:
    deployed_path = _bot_home(bot_id) / ".openclaw" / ".evolve-version"
    latest_path = Path("/Users/Shared/evolve/release/latest.json")

    deployed = _read_deployed(deployed_path)
    latest = _read_latest(latest_path)
    return VersionSources(deployed_date=deployed, latest_date=latest)


def _read_deployed(path: Path) -> date | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _read_latest(path: Path) -> date | None:
    if not path.exists():
        return None
    try:
        import json

        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):  # type: ignore[name-defined]
        return None
    released = data.get("released_at") if isinstance(data, dict) else None
    if not isinstance(released, str):
        return None
    try:
        return date.fromisoformat(released)
    except ValueError:
        return None


_sources_fn: _Sources = _default_sources


def set_version_sources(fn: _Sources) -> None:
    global _sources_fn
    _sources_fn = fn


def resolve_version_currency_days_behind(
    bot_id: str, as_of: datetime  # noqa: ARG001
) -> MetricValue:
    sources = _sources_fn(bot_id)

    if sources.deployed_date is None and sources.latest_date is None:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note="neither deployed nor latest version info available",
        )
    if sources.deployed_date is None:
        return MetricValue(
            value=0.0,
            confidence=0.5,
            source_note="no deployed version marker on bot",
        )
    if sources.latest_date is None:
        return MetricValue(
            value=0.0,
            confidence=0.5,
            source_note="no latest-release info available",
        )

    days_behind = max(
        0, (sources.latest_date - sources.deployed_date).days
    )
    return MetricValue(
        value=float(days_behind),
        confidence=1.0,
        source_note=(
            f"deployed={sources.deployed_date}, latest={sources.latest_date}, "
            f"days_behind={days_behind}"
        ),
    )


register(
    MetricSpec(
        name="version.currency_days_behind",
        description="Days between the bot's deployed version and the latest release.",
        unit="count",
        source="bot's .evolve-version file vs release/latest.json",
    ),
    resolve_version_currency_days_behind,
)
