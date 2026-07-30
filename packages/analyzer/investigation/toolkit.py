"""investigation.toolkit — Pure-Python lookups for investigating generators.

Each tool is a small, named function that an investigating generator can
call from observe() to gather evidence before deciding what to propose.

Failure semantics: every tool fails open. An import error, a missing file,
or an upstream raise returns an empty result rather than propagating —
investigation is best-effort context, never a hard dependency. A
generator that's mid-investigation should still produce a proposal (with
a thinner evidence block) rather than crash silently.

Tools intentionally do *not* read each other. A generator composes the
result of multiple tools in its attribution step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Result types — small dataclasses so tests can pattern-match easily.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CorrelatedSignal:
    """A Signal firing on the same bot in the same window as the primary."""

    signal_id: str
    type: str
    producer: str
    severity: str
    title: str
    signature: str
    details: dict = field(default_factory=dict)


@dataclass
class FileSize:
    """Size in bytes for a workspace file at a known relative path."""

    path: str  # workspace-relative (e.g. "memory/2026-05-02.md")
    size_bytes: int


@dataclass
class ConfigIntent:
    """An operator-recorded intent for ``(bot_id, field_path)``.

    Wraps ``evolve_admin.config_intent.get_intent`` with a typed surface
    so callers can branch on presence without re-validating the raw dict.
    """

    bot_id: str
    field_path: str
    intent_id: str
    set_at: str
    reason: str
    raw: dict


# ─────────────────────────────────────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────────────────────────────────────


def correlated_signals(
    shared_dir: Path,
    bot_id: str,
    *,
    exclude_signatures: set[str] | None = None,
    producer: str | None = None,
    types: list[str] | None = None,
) -> list[CorrelatedSignal]:
    """Other firing/snoozed Signals on this bot.

    ``exclude_signatures``: skip these (typically the caller's primary
    signal — generators look for *other* signals to corroborate).
    ``producer`` / ``types``: filter to the most diagnostic family
    (e.g. cost_watchdog + [workspace_growth, efficiency_drift]).

    Fail-open: ImportError on the signals store returns ``[]``.
    """
    try:
        from signals import store as signals_store
    except ImportError:
        return []
    exclude = exclude_signatures or set()
    types_set = set(types) if types else None
    out: list[CorrelatedSignal] = []
    try:
        for sig in signals_store.iter_active(
            shared_dir, bot_id=bot_id, producer=producer,
        ):
            sig_signature = getattr(sig, "signature", "")
            if sig_signature in exclude:
                continue
            sig_type = getattr(sig, "type", "")
            if types_set is not None and sig_type not in types_set:
                continue
            details = getattr(sig, "details", {}) or {}
            if not isinstance(details, dict):
                details = {}
            out.append(
                CorrelatedSignal(
                    signal_id=getattr(sig, "id", ""),
                    type=sig_type,
                    producer=getattr(sig, "producer", ""),
                    severity=getattr(sig, "severity", "warn"),
                    title=getattr(sig, "title", ""),
                    signature=sig_signature,
                    details=details,
                )
            )
    except Exception:
        return out
    return out


def recent_config_changes(
    bot_id: str,
    paths: list[str],
    *,
    since_days: int,
    bot_home: Path | None = None,
    now: datetime | None = None,
) -> list[tuple[str, datetime]]:
    """Return [(path, mtime)] for files whose mtime is within the window.

    ``paths`` are *bot-home-relative* (e.g. ``.openclaw/openclaw.json``).
    Missing files are skipped silently — the absence of a file is its own
    signal but not this tool's responsibility to convey. Files outside the
    window are also skipped (caller knows the window).

    ``bot_home`` is injectable for tests; production resolves it through
    ``evolve_config.bot_home``. Fail-open: any resolver/stat error skips
    the file.
    """
    if bot_home is None:
        try:
            from evolve_config import bot_home as _bot_home
            bot_home = _bot_home(bot_id)
        except Exception:
            return []
    now_dt = now or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=since_days)
    out: list[tuple[str, datetime]] = []
    for rel in paths:
        try:
            p = bot_home / rel
            mtime_ts = p.stat().st_mtime
            mtime = datetime.fromtimestamp(mtime_ts, tz=timezone.utc)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if mtime < cutoff:
            continue
        out.append((rel, mtime))
    return out


def config_intent(
    bot_id: str,
    field_path: str,
    *,
    shared_dir: Path | None = None,
) -> ConfigIntent | None:
    """Return the recorded intent for ``(bot_id, field_path)``, or None.

    Fail-open: ImportError, sidecar read failure, or missing intent → None.
    The generator treats None as "no recorded intent, propose normally";
    presence means "operator made this choice deliberately, suppress
    revert proposals and surface as audit-only."
    """
    try:
        from evolve_admin.config_intent import get_intent
    except ImportError:
        return None
    try:
        rec = get_intent(bot_id, field_path, shared_dir=shared_dir)
    except Exception:
        return None
    if not rec:
        return None
    return ConfigIntent(
        bot_id=bot_id,
        field_path=field_path,
        intent_id=str(rec.get("id", "")),
        set_at=str(rec.get("set_at", "")),
        reason=str(rec.get("reason", "")),
        raw=dict(rec),
    )


def file_top_contributors(
    bot_id: str,
    *,
    n: int = 5,
    config: dict | None = None,
    sizes: dict[str, int] | None = None,
) -> list[FileSize]:
    """Top-N largest .md files in the bot's workspace, sorted descending.

    ``sizes`` is injectable for tests. Production fetches via
    ``cost_watchdog.workspace_md_sizes`` (covers both top-level and the
    memory/ subdir per Gap A). Returns ``[]`` on any read failure.
    """
    if sizes is None:
        try:
            from cost_watchdog import workspace_md_sizes
            sizes = workspace_md_sizes(bot_id, config)
        except Exception:
            return []
    if not sizes:
        return []
    pairs = sorted(sizes.items(), key=lambda kv: int(kv[1] or 0), reverse=True)
    return [FileSize(path=p, size_bytes=int(s)) for p, s in pairs[:n]]


@dataclass
class ManifestMention:
    """One bot-manifest reference matching an investigation query.

    Used by ``manifest_mentions`` to surface evidence that the bot's
    installed-apps system "expects" the queried capability — when the
    exec_outcome_investigator's allowlist-gap rule sees this, it can
    bump urgency from improvement to critical (the system has been
    told the bot should be able to do X, yet it can't).
    """

    app_id: str
    app_name: str
    manifest_path: str  # filename only, not full path
    snippet: str  # short context snippet showing the match


# Manifest fields we search for the query term. Skips bookkeeping fields
# (created_at, last_test_run, etc.) and anything that would yield noise.
_MANIFEST_SEARCHABLE_KEYS: tuple[str, ...] = (
    "display_name", "name", "purpose", "goals", "objective",
    "files", "realized_files", "evidence_files",
    "crons", "scheduled_actions", "configured_schedules",
    "example_triggers", "session_keywords", "tags", "capability_tags",
    "build_spec", "test_command", "test_cases",
    "permissions",
)


def manifest_mentions(
    bot_id: str,
    query: str,
    *,
    manifests_dir: Path | None = None,
    config: dict | None = None,
    max_mentions: int = 5,
) -> list[ManifestMention]:
    """Search the bot's app manifests for any mention of ``query``.

    Bots store installed-app manifests at
    ``/Users/<bot>/.openclaw/workspace/manifests/*.json``. Each manifest
    describes one app — its purpose, declared files, crons, capability
    tags, etc. When the operator's investigation surfaces a denied tool,
    a manifest that mentions that tool is evidence the bot was MEANT to
    be able to do this — which makes the gap mechanically wrong rather
    than just operationally suboptimal.

    Returns matches sorted by manifest file name (stable). Empty list
    on any read failure or when no manifests match — fail-open per the
    investigation-toolkit convention.

    ``manifests_dir`` is injectable for tests; production resolves it
    through ``evolve_config.bot_home``. The query is matched
    case-insensitively against the serialized values of a narrow set of
    "intent-bearing" fields (display_name, purpose, files, etc.) — not
    every field, to keep noise down on bookkeeping like timestamps.
    """
    import json
    query_lc = query.strip().lower()
    if not query_lc:
        return []
    if manifests_dir is None:
        try:
            from evolve_config import bot_home
            home = bot_home(bot_id)
        except Exception:
            return []
        manifests_dir = home / ".openclaw" / "workspace" / "manifests"
    try:
        entries = sorted(manifests_dir.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return []
    out: list[ManifestMention] = []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        name = entry.name
        # Skip scanner state + dotfiles. Manifests are i-*.json
        # (instance) or manifest-*.json (pkg-template) shapes.
        if name.startswith(".") or not name.endswith(".json"):
            continue
        try:
            text = entry.read_text()
        except (PermissionError, OSError):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        # Restrict to the intent-bearing field set. Skipping irrelevant
        # fields keeps the query from matching e.g. an ISO timestamp's
        # `"created"` substring on a bot that searches for "create".
        haystack_parts: list[str] = []
        for key in _MANIFEST_SEARCHABLE_KEYS:
            val = data.get(key)
            if val is None:
                continue
            # Serialize to JSON so nested lists/dicts (files, crons,
            # permissions block) are included intact.
            try:
                haystack_parts.append(json.dumps(val, ensure_ascii=False))
            except (TypeError, ValueError):
                continue
        haystack = "\n".join(haystack_parts)
        if query_lc not in haystack.lower():
            continue
        # Build a short snippet showing the match in context.
        idx = haystack.lower().index(query_lc)
        start = max(0, idx - 40)
        end = min(len(haystack), idx + len(query_lc) + 40)
        snippet = haystack[start:end].replace("\n", " ")
        if start > 0:
            snippet = "…" + snippet
        if end < len(haystack):
            snippet = snippet + "…"
        out.append(ManifestMention(
            app_id=str(data.get("instance_id") or data.get("id") or ""),
            app_name=str(
                data.get("display_name") or data.get("name") or name
            ),
            manifest_path=name,
            snippet=snippet,
        ))
        if len(out) >= max_mentions:
            break
    return out


def time_series_cost_per_call(
    shared_dir: Path,
    bot_id: str,
    *,
    days: int,
    model_tier: str | None = None,
    now: datetime | None = None,
    events_reader: Any | None = None,
) -> list[tuple[str, float, int]]:
    """Return ``[(YYYY-MM-DD, cost_per_call_usd, call_count), ...]`` per day.

    Optional ``model_tier`` filters to "high" / "low" / "unknown" via
    ``classify_model_tier``. Days with zero calls are omitted.

    ``events_reader`` is injectable for tests; production wires
    ``cost_ledger.read_events``. Fail-open returns ``[]`` on read errors.
    """
    if events_reader is None:
        try:
            from cost_ledger import read_events
            events_reader = read_events
        except ImportError:
            return []
    now_dt = now or datetime.now(timezone.utc)
    try:
        from metrics.resolvers.cost_metrics import classify_model_tier
    except ImportError:
        classify_model_tier = None  # type: ignore[assignment]

    by_day: dict[str, tuple[float, int]] = {}
    try:
        events = events_reader(bot_id, days=days, shared_dir=shared_dir, now=now_dt)
    except Exception:
        return []
    for e in events:
        ts = e.get("ts") or ""
        if not isinstance(ts, str) or len(ts) < 10:
            continue
        if model_tier is not None and classify_model_tier is not None:
            if classify_model_tier(e.get("model")) != model_tier:
                continue
        day = ts[:10]
        cost, n = by_day.get(day, (0.0, 0))
        by_day[day] = (cost + float(e.get("cost_usd") or 0.0), n + 1)

    out: list[tuple[str, float, int]] = []
    for day in sorted(by_day):
        cost, n = by_day[day]
        if n == 0:
            continue
        out.append((day, round(cost / n, 6), n))
    return out
