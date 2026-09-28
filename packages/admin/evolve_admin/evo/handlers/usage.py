"""``evo usage`` — 1d / 7d / 30d activity summary.

Scope: per-bot, pod-wide on ``evolve``. Reads the same daily cost rollups
as ``evo cost`` (``{shared_dir}/metrics/{bot}/cost-{date}.json``) — they
carry ``event_count``, ``input_tokens``, ``output_tokens``,
``cache_read_tokens``, ``cache_write_tokens`` alongside the spend. Calls
≈ sessions for at-a-glance purposes; tokens give a sense of how much
work landed.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import is_pod_wide_caller, pod_member_bots, speak


_WINDOWS: tuple[tuple[str, int], ...] = (
    ("Today",   1),
    ("7 days",  7),
    ("30 days", 30),
)


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    today = date.today()
    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(shared_dir, network, today)
    else:
        body = _render_bot(shared_dir, bot_id, today)
    return speak("usage", body, role)


def _render_bot(shared_dir: Path, bot_id: str, today: date) -> str:
    rollups = _load_rollups(shared_dir, bot_id, 30, today)
    if not rollups:
        return (
            f"**Usage — {bot_id}**\n\n"
            "No usage data yet. Either nothing has run, or the rollup "
            "job hasn't filled in numbers for this bot."
        )

    lines = [f"**Usage — {bot_id}**", ""]
    for label, days in _WINDOWS:
        calls, tokens, cache_hit = _window_stats(rollups, today, days)
        lines.append(f"{label}: {calls} calls, {_fmt_tokens(tokens)} tokens, "
                     f"{_fmt_pct(cache_hit)} cache hit")
    return "\n".join(lines)


def _render_pod(shared_dir: Path, network: dict[str, Any], today: date) -> str:
    members = pod_member_bots(network)
    rollups_by_bot = {
        b: _load_rollups(shared_dir, b, 30, today) for b in members
    }

    lines = ["**Usage — pod**", ""]
    for label, days in _WINDOWS:
        total_calls = 0
        total_tokens = 0
        total_cache_read = 0
        total_input = 0
        for r in rollups_by_bot.values():
            c, t, _ = _window_stats(r, today, days)
            total_calls += c
            total_tokens += t
            for d in r.values():
                d_date_str = d.get("date", "")
                try:
                    d_date = date.fromisoformat(d_date_str)
                except (ValueError, TypeError):
                    continue
                if today - timedelta(days=days - 1) <= d_date <= today:
                    total_cache_read += int(d.get("cache_read_tokens") or 0)
                    total_input += int(d.get("input_tokens") or 0)
        total = total_cache_read + total_input
        cache_hit = total_cache_read / total if total > 0 else 0.0
        lines.append(f"{label}: {total_calls} calls, "
                     f"{_fmt_tokens(total_tokens)} tokens, "
                     f"{_fmt_pct(cache_hit)} cache hit")

    active = [(b, _window_stats(r, today, 30)[0])
              for b, r in rollups_by_bot.items() if r]
    active = [(b, c) for b, c in active if c > 0]
    if active:
        lines.append("")
        lines.append("By bot (30d):")
        for bot, calls in sorted(active, key=lambda x: -x[1]):
            lines.append(f"  {bot}: {calls} calls")
    return "\n".join(lines)


def _load_rollups(
    shared_dir: Path, bot_id: str, days: int, today: date
) -> dict[date, dict]:
    base = shared_dir / "metrics" / bot_id
    if not base.exists():
        return {}
    out: dict[date, dict] = {}
    for i in range(days):
        d = today - timedelta(days=i)
        path = base / f"cost-{d.isoformat()}.json"
        if not path.exists():
            continue
        try:
            out[d] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _window_stats(
    rollups: dict[date, dict], today: date, days: int
) -> tuple[int, int, float]:
    """Return (calls, total_tokens, cache_hit_ratio) for the window."""
    start = today - timedelta(days=days - 1)
    calls = 0
    total_tokens = 0
    cache_read = 0
    input_tokens = 0
    for d, r in rollups.items():
        if not (start <= d <= today):
            continue
        calls += int(r.get("event_count") or 0)
        in_t = int(r.get("input_tokens") or 0)
        out_t = int(r.get("output_tokens") or 0)
        cr = int(r.get("cache_read_tokens") or 0)
        total_tokens += in_t + out_t + cr
        cache_read += cr
        input_tokens += in_t
    denom = cache_read + input_tokens
    cache_hit = cache_read / denom if denom > 0 else 0.0
    return calls, total_tokens, cache_hit


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.0f}%"
