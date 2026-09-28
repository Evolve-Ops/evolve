"""permissions.intent_inference — Phase 3 of spec-config-intent-system-2026-05-21.

A single haiku call invoked from ``config_intent.set_intent()`` when
the caller passes ``set_by="inferred:auto"``. Deduces *why* a config
field landed on a non-baseline value, with four pieces of context
fed into the prompt:

  1. The diff: ``(bot_id, field_path, old_value, new_value)``.
  2. Currently-enabled plugins on the bot (read from openclaw.json).
  3. Plugin → field dependency map (plugin_field_deps.yaml). When a
     diff matches a documented dep, the deduction is straightforward
     and the model returns ``high`` confidence; without a match, the
     model has to reason from prior intents and plugin context.
  4. Recent activity on the bot — admin-actions + watchdog events
     filtered by bot_id within a configurable lookback window (10
     minutes default — spec §5.2 row 2). Phase 3.1 add: lets the
     model reason "plugin install N minutes before this write →
     high confidence the write is a documented side effect."

Spec §5 enumerates four design decisions worth preserving in the
module surface:

  - **Synchronous**: the call lives inside set_intent(), not in a
    daemon. v1 chooses ~500ms-1s save latency over reconciliation pain.
  - **Fail-open**: any failure (model unreachable, malformed JSON,
    contradiction with known plugin state) collapses to
    ``inferred:low`` with a stable fallback reason; the config write
    is never blocked.
  - **Contradiction check**: if the model claims ``depends_on.plugin``
    but the plugin isn't enabled on the bot, the result is downgraded
    to ``low`` and the depends_on cleared. The fallback reason flags
    this explicitly so the operator can confirm.
  - **Confidence routing**: ``high`` / ``medium`` write the recorded
    reason as-is; ``low`` writes the same record but also sets
    ``queued=true`` so the operator-facing surface (Phase 4 popover,
    deferred today) can prompt for a manual note.

Cost target (spec §5.1): ~500 input + ~150 output tokens at Haiku
pricing → under $5/year across a typical pod's write rate.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — yaml is a hard dep of the analyzer suite
    yaml = None  # type: ignore[assignment]


# ── Public result shape ─────────────────────────────────────────────────────


@dataclass
class InferenceResult:
    """Output of ``infer()`` — used by config_intent.set_intent to populate
    the recorded intent's metadata.

    Fields mirror the JSON shape the spec defines (§5.3) plus a
    ``queued`` flag the writer uses to mark low-confidence records.
    """

    reason: str
    confidence: str  # "high" | "medium" | "low"
    set_by: str      # "inferred:high" | "inferred:medium" | "inferred:low"
    depends_on: dict | None = None
    queued: bool = False
    contradictions: list[str] = field(default_factory=list)


# ── Defaults + constants ────────────────────────────────────────────────────

DEFAULT_MODEL = "anthropic/claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 250
DEFAULT_TIMEOUT_SEC = 30


_FALLBACK_REASON = (
    "Inference unavailable for this write. Click 'Edit reason' on the "
    "Intentional Deviations row to record the operator's actual intent."
)


# ── Plugin → field dep map (§6) ─────────────────────────────────────────────


def _default_deps_path() -> Path:
    """Location of the seed dependency map shipped with the analyzer."""
    return Path(__file__).resolve().parent / "plugin_field_deps.yaml"


def load_plugin_field_deps(path: Path | None = None) -> dict[str, list[dict]]:
    """Parse ``plugin_field_deps.yaml`` into ``{plugin_id: [{field, values,
    rationale}, ...]}``.

    Returns an empty dict on any read or parse error so the inference
    layer falls through to the LLM with empty context rather than
    blowing up. Per-plugin entries that don't carry ``required_fields``
    are silently skipped — the spec format requires the field, but
    tolerating missing entries protects the inference path from a
    misformatted YAML upgrade.
    """
    if yaml is None:
        return {}
    target = path or _default_deps_path()
    try:
        raw = yaml.safe_load(target.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for plugin_id, spec in raw.items():
        if not isinstance(plugin_id, str) or not isinstance(spec, dict):
            continue
        req = spec.get("required_fields")
        if not isinstance(req, list):
            continue
        out[plugin_id] = [
            entry for entry in req if isinstance(entry, dict)
        ]
    return out


# ── Bot plugin discovery ─────────────────────────────────────────────────────


def _enabled_plugins(bot_id: str, network: dict | None) -> list[str]:
    """Best-effort list of plugin ids currently enabled on ``bot_id``.

    Reads two sources, unions the result:
      - ``network.json::bots.<id>.plugins`` (operator-curated, sometimes
        unset on member bots).
      - ``openclaw.json::plugins.entries.<id>.enabled`` (runtime truth).

    Fails silently — an empty list collapses to the inference layer
    treating the bot as plugin-free, which only loses precision; it
    doesn't cause incorrect intents (those are caught by the
    contradiction check downstream).

    The network dict is passed in to avoid this module having to know
    where network.json lives — the caller (set_intent) already
    resolved it.
    """
    found: set[str] = set()

    if isinstance(network, dict):
        bots = network.get("bots") or {}
        if isinstance(bots, dict):
            entry = bots.get(bot_id) or {}
            if isinstance(entry, dict):
                plugins_list = entry.get("plugins") or []
                if isinstance(plugins_list, list):
                    for p in plugins_list:
                        if isinstance(p, str) and p:
                            found.add(p)

    # openclaw.json read — direct ACL read first, then sudo /bin/cat
    # fallback (CLAUDE.md §"File Access Pattern").
    # bot_id is the logical name, not the macOS account (team-bot-b runs on the
    # personal-bot-user account) — resolve it, or the read below misses the config and
    # the bot's declared plugins go undetected.
    try:
        from evolve_config import bot_home
        # `network` is already in hand — pass it so bot_home does not reload
        # network.json on every call.
        oc_path = bot_home(bot_id, network) / ".openclaw" / "openclaw.json"
    except Exception:
        oc_path = Path(f"/Users/{bot_id}/.openclaw/openclaw.json")
    text: str | None = None
    try:
        text = oc_path.read_text()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    if text is None:
        try:
            proc = subprocess.run(
                ["sudo", "/bin/cat", str(oc_path)],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                text = proc.stdout
        except (subprocess.SubprocessError, OSError):
            pass
    if text:
        try:
            oc = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            oc = None
        if isinstance(oc, dict):
            entries = ((oc.get("plugins") or {}).get("entries") or {})
            if isinstance(entries, dict):
                for pid, cfg in entries.items():
                    if isinstance(pid, str) and isinstance(cfg, dict) \
                            and cfg.get("enabled") is True:
                        found.add(pid)

    return sorted(found)


# ── LLM call boundary ────────────────────────────────────────────────────────


def _call_llm(prompt: str, *,
              model: str = DEFAULT_MODEL,
              max_tokens: int = DEFAULT_MAX_TOKENS,
              timeout: int = DEFAULT_TIMEOUT_SEC,
              shared_dir: Path | None = None) -> str | None:
    """Run one inference completion. Returns text on success, ``None`` on
    any failure.

    Single-shot wrapper that exists primarily as a test seam — every test
    of the inference layer monkey-patches this function and asserts on the
    prompt it received, never running a real LLM. That is exactly how this
    call site's breakage stayed hidden: until 2026-08-18 the body shelled
    out to ``openclaw llm complete``, a subcommand no shipped OpenClaw has,
    so every production inference returned the low-confidence fallback
    while the whole test suite stayed green against the mocked seam.

    It now routes through ``engine_llm`` (→ ``infra_llm``) like the rest of
    Evolve's engine-side LLM work: the model resolves through the pod's
    tier config and the key through the primary bot's credentials, and a
    call that FAILS against a credentialed provider raises an
    ``engine_llm_call_failed`` Signal instead of being indistinguishable
    from "the pod has no key".
    """
    from engine_llm import OK, engine_complete  # type: ignore

    text, outcome = engine_complete(
        prompt,
        job="intent_inference",
        shared_dir=shared_dir,
        model_hint=model,
        role="fast",
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return text if outcome == OK else None


# ── Prompt construction ──────────────────────────────────────────────────────


_PROMPT_TEMPLATE = """\
You are an Evolve admin assistant deducing why a permission-config field
on the bot ``{bot_id}`` was written to a non-baseline value.

The diff:
  field:      {field_path}
  old_value:  {old_value_json}
  new_value:  {new_value_json}

Currently-enabled plugins on this bot: {enabled_plugins}

Plugin → field dependency map (filtered to plugins enabled on this bot):
{dep_map_text}

Existing recorded intents on this bot (each has a field, value, and
operator-facing reason — they show prior patterns you can build on):
{existing_intents_text}

Recent activity on this bot (last {activity_minutes} minutes —
operator admin-actions and pod-wide watchdog events). When an event
shortly preceded this write, it's strong evidence of a documented
side effect (e.g. ``plugin install codex 2 min ago`` → exec=full
write is the plugin's documented side effect, high confidence):
{recent_activity_text}

Your task: emit one JSON object explaining WHY this write probably
happened. Use the dependency map when a plugin clearly implicates the
field — that's high confidence. If a recent activity event shortly
preceded the write and explains it, that's also high confidence. If
no plugin matches but the new value plausibly aligns with the bot's
role, return medium. Otherwise return low.

Return ONLY this JSON object — no preamble, no markdown fence:

{{
  "reason": "<one operator-readable sentence; reference the plugin id if applicable>",
  "depends_on": {{"plugin": "<plugin_id>"}} or null,
  "confidence": "high" | "medium" | "low"
}}

If you reference depends_on.plugin, that plugin MUST be in the
currently-enabled list above — referencing a plugin that isn't
enabled will be detected as a contradiction and your output downgraded
to low.
"""


def _format_dep_map(deps: dict[str, list[dict]],
                    enabled_plugins: list[str]) -> str:
    """Filter the dep map to plugins actually enabled, format for the
    prompt. Empty lines if no matches — keeps the prompt cheap."""
    relevant = {p: deps[p] for p in enabled_plugins if p in deps}
    if not relevant:
        return "  (none enabled — no plugin context available)"
    lines: list[str] = []
    for plugin_id, fields in relevant.items():
        lines.append(f"  {plugin_id}:")
        for f in fields:
            field_name = f.get("field", "")
            values = f.get("values") or []
            rationale = f.get("rationale", "")
            lines.append(
                f"    - {field_name} ∈ {values}: {rationale}"
            )
    return "\n".join(lines)


def _format_existing_intents(intents: list[dict]) -> str:
    """One-line-per-intent summary for the prompt."""
    if not intents:
        return "  (none)"
    lines: list[str] = []
    for intent in intents:
        if not isinstance(intent, dict):
            continue
        field_path = intent.get("field_path", "")
        value = intent.get("value")
        reason = intent.get("reason", "")
        lines.append(
            f"  - {field_path}={json.dumps(value)}: {reason}"
        )
    return "\n".join(lines) or "  (none)"


# ── Recent activity (Phase 3.1) ──────────────────────────────────────────────

DEFAULT_ACTIVITY_WINDOW_MINUTES = 10


def _parse_iso_ts(value: Any) -> "datetime | None":
    """Parse an ISO-8601 timestamp tolerantly.

    Both activity sources emit slightly different shapes:
      - admin-actions.jsonl: ``"2026-04-11T22:07:01.810880Z"`` (with subsec)
      - watchdog/<date>.jsonl: ``"2026-06-06T10:28:18Z"`` (no subsec)

    Both end in ``Z``; ``datetime.fromisoformat`` in Python 3.10
    rejects that suffix so we swap it for ``+00:00`` before parsing.
    Anything that doesn't parse cleanly returns None and the caller
    drops the event from the recent set rather than crashing.
    """
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _read_jsonl_lines(path: Path, *, max_bytes: int = 256 * 1024) -> "list[dict]":
    """Read the last ``max_bytes`` of a JSONL file and parse line-by-line.

    Bounded read so a giant log file doesn't bloat memory or stall the
    inference path. 256 KB is plenty for the 10-minute activity window
    on a typical pod — admin-actions averages a few hundred bytes per
    entry, watchdog similarly modest. Returns ``[]`` on any read/parse
    failure (silent best-effort, fail-open).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    try:
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                # Drop the partial leading line.
                f.readline()
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _recent_activity(
    bot_id: str,
    shared_dir: Path | None,
    *,
    minutes: int = DEFAULT_ACTIVITY_WINDOW_MINUTES,
    now: "datetime | None" = None,
) -> list[dict]:
    """Return recent events on ``bot_id`` from the two pod-wide logs.

    Sources (per spec §5.2 row 2):
      - ``{shared_dir}/logs/admin-actions.jsonl`` — operator-initiated
        admin actions. Shape: ``{ts, action, bot, initiated_by, result}``.
      - ``{shared_dir}/watchdog/<YYYY-MM-DD>.jsonl`` — pod-wide watchdog
        events. Shape: ``{id, bot_id, timestamp, event_type, severity,
        details}``.

    Returns a list of normalized event dicts ordered oldest → newest::

        {
          "source": "admin-actions" | "watchdog",
          "at": <iso str>,
          "minutes_ago": <int>,
          "summary": <human readable str>,
        }

    Empty list on any failure (missing log dirs, malformed events,
    no events in the window). The inference layer formats this list
    into the prompt; an empty list collapses to "(no recent activity
    on this bot)" in the prompt — the model can still reason from
    plugin context alone.

    Time-window scoping: ``minutes`` defaults to spec's 10-minute
    lookback. Tests pass a fixed ``now`` so the recent-vs-old line
    is deterministic.
    """
    from datetime import datetime, timezone, timedelta

    if shared_dir is None:
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(minutes=minutes)

    events: list[dict] = []

    # admin-actions.jsonl — single file
    admin_log = shared_dir / "logs" / "admin-actions.jsonl"
    for entry in _read_jsonl_lines(admin_log):
        if entry.get("bot") != bot_id:
            continue
        at = _parse_iso_ts(entry.get("ts"))
        if at is None or at < cutoff:
            continue
        action = str(entry.get("action") or "unknown")
        initiated_by = str(entry.get("initiated_by") or "")
        result = str(entry.get("result") or "")
        events.append({
            "source": "admin-actions",
            "at": entry.get("ts"),
            "minutes_ago": max(0, int((now - at).total_seconds() // 60)),
            "summary": (
                f"action={action}"
                + (f" by {initiated_by}" if initiated_by else "")
                + (f" → {result}" if result else "")
            ),
        })

    # watchdog/<date>.jsonl — one file per UTC day; the 10-minute window
    # crosses midnight rarely, but when it does we need both files.
    today_path = shared_dir / "watchdog" / f"{now.date().isoformat()}.jsonl"
    paths_to_scan = [today_path]
    if cutoff.date() != now.date():
        paths_to_scan.append(
            shared_dir / "watchdog" / f"{cutoff.date().isoformat()}.jsonl",
        )
    seen_ids: set[str] = set()
    for wp in paths_to_scan:
        for entry in _read_jsonl_lines(wp):
            if entry.get("bot_id") != bot_id:
                continue
            at = _parse_iso_ts(entry.get("timestamp"))
            if at is None or at < cutoff:
                continue
            eid = entry.get("id")
            if isinstance(eid, str) and eid in seen_ids:
                continue
            if isinstance(eid, str):
                seen_ids.add(eid)
            event_type = str(entry.get("event_type") or "unknown")
            details = entry.get("details") or {}
            summary_text = ""
            if isinstance(details, dict):
                summary_text = str(details.get("summary") or "")
            events.append({
                "source": "watchdog",
                "at": entry.get("timestamp"),
                "minutes_ago": max(0, int((now - at).total_seconds() // 60)),
                "summary": (
                    f"event={event_type}"
                    + (f": {summary_text}" if summary_text else "")
                ),
            })

    events.sort(key=lambda e: e.get("at") or "")
    return events


def _format_recent_activity(events: list[dict]) -> str:
    """One-line-per-event summary for the prompt; empty-state fallback
    when no events are in the window."""
    if not events:
        return "  (no recent activity on this bot)"
    lines: list[str] = []
    for e in events:
        src = e.get("source", "?")
        minutes_ago = e.get("minutes_ago")
        summary = e.get("summary", "")
        ago = (
            f"{minutes_ago} min ago"
            if isinstance(minutes_ago, int) else "(time unknown)"
        )
        lines.append(f"  - [{src}, {ago}] {summary}")
    return "\n".join(lines)


def _build_prompt(*, bot_id: str, field_path: str,
                  old_value: Any, new_value: Any,
                  enabled_plugins: list[str],
                  deps: dict[str, list[dict]],
                  existing_intents: list[dict],
                  recent_activity: list[dict],
                  activity_minutes: int = DEFAULT_ACTIVITY_WINDOW_MINUTES) -> str:
    return _PROMPT_TEMPLATE.format(
        bot_id=bot_id,
        field_path=field_path,
        old_value_json=json.dumps(old_value),
        new_value_json=json.dumps(new_value),
        enabled_plugins=enabled_plugins or ["(none discovered)"],
        dep_map_text=_format_dep_map(deps, enabled_plugins),
        existing_intents_text=_format_existing_intents(existing_intents),
        recent_activity_text=_format_recent_activity(recent_activity),
        activity_minutes=activity_minutes,
    )


# ── Output parsing + validation ─────────────────────────────────────────────


def _parse_llm_output(raw: str) -> dict | None:
    """Extract the first ``{...}`` object from the LLM output and
    json-load it. Returns ``None`` if no parseable object found.

    Tolerates leading prose and markdown fences the model occasionally
    emits despite the explicit instruction — pattern-match the
    outermost brace pair rather than trusting the text shape.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


_VALID_CONFIDENCES = frozenset({"high", "medium", "low"})


def _validate_and_finalize(
    parsed: dict,
    *,
    enabled_plugins: list[str],
    deps: dict[str, list[dict]],
    field_path: str,
    new_value: Any,
) -> InferenceResult:
    """Take the parsed LLM output, run contradiction checks, finalize
    the InferenceResult.

    Contradiction sources (spec §5.5):
      1. ``depends_on.plugin`` references a plugin that isn't in
         ``enabled_plugins``.
      2. ``depends_on.plugin`` IS enabled, but the plugin's dep map
         doesn't list ``field_path`` — or lists it but with a different
         expected value than what we just wrote.

    Any contradiction collapses confidence to ``low``, blanks the
    depends_on, and records the contradiction in the result for the
    operator surface to display.
    """
    reason = str(parsed.get("reason") or "").strip()
    if not reason:
        return InferenceResult(
            reason=_FALLBACK_REASON,
            confidence="low",
            set_by="inferred:low",
            depends_on=None,
            queued=True,
            contradictions=["LLM returned no reason text"],
        )
    confidence = str(parsed.get("confidence") or "").strip().lower()
    if confidence not in _VALID_CONFIDENCES:
        confidence = "low"

    depends_on = parsed.get("depends_on")
    if depends_on is not None and not isinstance(depends_on, dict):
        depends_on = None

    contradictions: list[str] = []
    if isinstance(depends_on, dict):
        claimed_plugin = depends_on.get("plugin")
        if isinstance(claimed_plugin, str) and claimed_plugin:
            if claimed_plugin not in enabled_plugins:
                contradictions.append(
                    f"Model claimed depends_on.plugin={claimed_plugin!r} "
                    f"but that plugin is not enabled on this bot."
                )
                depends_on = None
            else:
                plugin_deps = deps.get(claimed_plugin) or []
                matched_field = False
                for dep_entry in plugin_deps:
                    if dep_entry.get("field") != field_path:
                        continue
                    matched_field = True
                    allowed = dep_entry.get("values") or []
                    if isinstance(allowed, list) and new_value not in allowed:
                        contradictions.append(
                            f"Model claimed {claimed_plugin!r} requires "
                            f"{field_path}={json.dumps(new_value)}, but the "
                            f"plugin → field map lists allowed values "
                            f"{allowed}."
                        )
                if not matched_field and plugin_deps:
                    contradictions.append(
                        f"Model claimed depends_on.plugin={claimed_plugin!r} "
                        f"for field {field_path!r}, but that plugin's "
                        f"dep map doesn't list this field."
                    )

    if contradictions:
        confidence = "low"
        depends_on = None

    set_by = f"inferred:{confidence}"
    queued = confidence == "low"
    return InferenceResult(
        reason=reason,
        confidence=confidence,
        set_by=set_by,
        depends_on=depends_on,
        queued=queued,
        contradictions=contradictions,
    )


# ── Public entry point ──────────────────────────────────────────────────────


def infer(
    *,
    bot_id: str,
    field_path: str,
    old_value: Any,
    new_value: Any,
    shared_dir: Path | None = None,
    network: dict | None = None,
    existing_intents: list[dict] | None = None,
    recent_activity: list[dict] | None = None,
    activity_minutes: int = DEFAULT_ACTIVITY_WINDOW_MINUTES,
    deps_path: Path | None = None,
) -> InferenceResult:
    """Deduce why ``bot_id``'s ``field_path`` was written from
    ``old_value`` to ``new_value``.

    Args:
      bot_id, field_path, old_value, new_value: the diff to explain.
      shared_dir: forwarded to ``config_intent.list_intents`` (existing
        intents context) and ``_recent_activity`` (admin-actions +
        watchdog event log). None → skip both, inference works with
        plugin context only.
      network: the loaded network dict. Used to read
        ``network.json::bots.<id>.plugins``. None → skip that source.
      existing_intents: short-circuit override for tests + callers that
        already have the list in hand.
      recent_activity: short-circuit override for tests + callers that
        already have the list in hand. Each entry should have ``source``,
        ``at``, ``minutes_ago``, ``summary`` keys (see ``_recent_activity``).
      activity_minutes: lookback window for the activity reader. Spec
        default is 10; tests use shorter windows for determinism.
      deps_path: override for the plugin → field dep YAML location.
        Tests pass a tmp_path here; production defaults to the file
        shipped at packages/analyzer/permissions/plugin_field_deps.yaml.

    Returns an ``InferenceResult``. Always returns — never raises.
    Failure modes (model unreachable, malformed JSON, blank response,
    contradictions) all collapse to ``confidence="low"`` with a
    fallback reason and ``queued=True``.
    """
    deps = load_plugin_field_deps(deps_path)
    enabled = _enabled_plugins(bot_id, network)

    if existing_intents is None and shared_dir is not None:
        try:
            from evolve_admin.config_intent import list_intents
            existing_intents = list_intents(bot_id, shared_dir=shared_dir)
        except Exception:  # noqa: BLE001 — context-gathering is best-effort
            existing_intents = []
    existing_intents = existing_intents or []

    if recent_activity is None:
        try:
            recent_activity = _recent_activity(
                bot_id, shared_dir, minutes=activity_minutes,
            )
        except Exception:  # noqa: BLE001 — same fail-open convention
            recent_activity = []
    recent_activity = recent_activity or []

    prompt = _build_prompt(
        bot_id=bot_id, field_path=field_path,
        old_value=old_value, new_value=new_value,
        enabled_plugins=enabled, deps=deps,
        existing_intents=existing_intents,
        recent_activity=recent_activity,
        activity_minutes=activity_minutes,
    )

    raw = _call_llm(prompt, shared_dir=shared_dir)
    if raw is None:
        return InferenceResult(
            reason=_FALLBACK_REASON,
            confidence="low",
            set_by="inferred:low",
            depends_on=None,
            queued=True,
            contradictions=["LLM call failed (model unreachable or timed out)"],
        )
    parsed = _parse_llm_output(raw)
    if parsed is None:
        return InferenceResult(
            reason=_FALLBACK_REASON,
            confidence="low",
            set_by="inferred:low",
            depends_on=None,
            queued=True,
            contradictions=["LLM returned malformed or non-JSON output"],
        )
    return _validate_and_finalize(
        parsed,
        enabled_plugins=enabled, deps=deps,
        field_path=field_path, new_value=new_value,
    )
