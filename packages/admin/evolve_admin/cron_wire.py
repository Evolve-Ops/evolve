"""cron_wire — translate Evolve manifest cron vocabulary into OpenClaw's wire schema.

OpenClaw ≥2026.7 moved its cron store to SQLite (~/.openclaw/state/
openclaw.sqlite, table cron_jobs). ~/.openclaw/cron/jobs.json is now an
import-once seed: read at gateway start, validated, migrated into the store,
and renamed jobs.json.migrated. The migrator REQUIRES a ``payload`` object —
{kind:"systemEvent",text} | {kind:"agentTurn",message} | {kind:"command",argv}
— and quarantines anything else into ~/.openclaw/cron/jobs-quarantine.json
SILENTLY (no lastRunStatus update, no alert). The legacy Evolve wire shape
(``task`` string + ``schedule`` string "cron <expr> [@ <tz>]") fails with
reason "missing-payload": the primary bot's security:cve-scan-discover cron
sat quarantined from 2026-07-28 with no CVE scans running. Manifest entries
are therefore translated to the payload-based schema at materialization —
a raw legacy entry must NEVER be written to jobs.json.

Consumed by ``deploy._merge_cron_entries`` (app-manifest cron install).
"""
from __future__ import annotations

import re
from typing import Any

# A manifest cron whose only deliverable is a file (e.g. the security-cve-scan
# isolated discoverer writes candidates-<date>.json; a deterministic Python
# finalizer owns the actual alert) declares ``"delivery": "file"`` in its
# manifest. OpenClaw's cron schema spells no-delivery as
# ``"delivery": {"mode": "none"}`` (the same shape its own isolated "dreaming"
# jobs use). When a cron entry OMITS ``delivery`` the gateway defaults to mode
# "announce" and tries to send the isolated session's reply to the configured
# channel — which errors with ``Delivering to Telegram requires target
# <chatId>`` on a primary account that has no chatId (the CVE-scan failure,
# issue #3151 Facet B). Keying off the manifest field — not the app id — keeps
# this a general rule: any first-party file-only cron installs with no target.
_FILE_ONLY_DELIVERY_VALUES = {"file", "none"}


def _normalize_cron_delivery(entry: dict) -> dict:
    """Translate a manifest cron entry's delivery INTENT into OpenClaw's
    jobs.json wire schema. Returns the entry unchanged unless a translation is
    needed (a fresh copy is returned in that case — the manifest dict is never
    mutated, so the stored manifest keeps its Evolve-vocabulary ``delivery``).

    - ``delivery: "file"`` / ``"none"`` (Evolve manifest vocabulary) → the
      OpenClaw no-delivery knob ``{"mode": "none"}``.
    - ``delivery`` already an object → passed through untouched (forward-compat
      for explicit webhook/announce shapes authored directly in a manifest).
    - no ``delivery`` field → left as-is; alert-bearing crons keep the gateway's
      announce default.
    """
    raw = entry.get("delivery")
    if isinstance(raw, str) and raw.strip().lower() in _FILE_ONLY_DELIVERY_VALUES:
        out = dict(entry)
        out["delivery"] = {"mode": "none"}
        return out
    return entry


def _is_no_delivery(entry: dict) -> bool:
    """True when a (wire-shape) cron entry carries the no-delivery knob."""
    d = entry.get("delivery")
    return isinstance(d, dict) and str(d.get("mode", "")).strip().lower() == "none"


_LEGACY_CRON_SCHEDULE_RE = re.compile(
    r"^\s*cron\s+(?P<expr>.+?)(?:\s*@\s*(?P<tz>\S+))?\s*$"
)

# A bare cron expression string ("0 1 * * *", "0 9 * * MON-FRI"): 5-6
# whitespace-separated cron-charset fields, at least one carrying a digit or
# `*` (rules out prose like "every day at nine sharp"). OpenClaw itself
# accepts a bare string schedule (its migrator wraps it verbatim as the expr),
# so these are translated rather than refused.
_BARE_CRON_EXPR_RE = re.compile(
    r"^\s*(?:[\d*,\-/a-zA-Z#?]+\s+){4,5}[\d*,\-/a-zA-Z#?]+\s*$"
)


def _translate_cron_schedule(schedule: Any) -> dict:
    """Return the OpenClaw schedule object for a manifest ``schedule`` value.

    - dict → passed through, EXCEPT the one broken shape OpenClaw's own
      migrator mints from a legacy string: ``{"kind": "cron", "expr":
      "cron <expr> @ <tz>"}`` (it wraps the raw string verbatim — a cron
      expression the scheduler can never parse). That expr is re-parsed.
    - legacy string ``"cron <expr> [@ <tz>]"`` → ``{"kind": "cron",
      "expr": <expr>[, "tz": <tz>]}``.
    - anything else → ValueError (fail loudly; never emit quarantine-bait).
    """
    if isinstance(schedule, dict):
        expr = schedule.get("expr")
        if (
            schedule.get("kind") == "cron"
            and isinstance(expr, str)
            and _LEGACY_CRON_SCHEDULE_RE.match(expr)
        ):
            healed = dict(schedule)
            healed.update(_translate_cron_schedule(expr))
            return healed
        return schedule
    if isinstance(schedule, str):
        m = _LEGACY_CRON_SCHEDULE_RE.match(schedule)
        if m:
            out: dict = {"kind": "cron", "expr": m.group("expr")}
            if m.group("tz"):
                out["tz"] = m.group("tz")
            return out
        stripped = schedule.strip()
        if _BARE_CRON_EXPR_RE.match(schedule) and any(c.isdigit() or c == "*" for c in stripped):
            return {"kind": "cron", "expr": stripped}
        raise ValueError(f"unrecognized legacy schedule string {schedule!r}")
    raise ValueError("cron entry has no schedule")


_CRON_PAYLOAD_KINDS = {"systemEvent", "agentTurn", "command"}


def _normalize_cron_wire(entry: dict) -> dict:
    """Translate one manifest cron entry into OpenClaw's ≥2026.7 wire schema.

    Always returns a fresh dict (the manifest entry is never mutated).
    Composes the delivery translation with the schedule/payload migration:

    - ``schedule`` → object shape via :func:`_translate_cron_schedule`.
    - legacy ``task`` string → ``payload: {"kind": "agentTurn", "message":
      <task>}`` (the ``task`` key is dropped from the wire entry).
    - an authored ``payload`` object passes through after a kind check.

    Raises ValueError for any entry that cannot be translated — the caller
    must surface that as a deploy error, not write the entry raw (OpenClaw
    would quarantine it silently).
    """
    wire = dict(_normalize_cron_delivery(entry))
    wire["schedule"] = _translate_cron_schedule(wire.get("schedule"))

    payload = wire.get("payload")
    task = wire.pop("task", None)
    if isinstance(payload, dict):
        if payload.get("kind") not in _CRON_PAYLOAD_KINDS:
            raise ValueError(f"unrecognized payload kind {payload.get('kind')!r}")
        if task is not None:
            raise ValueError("cron entry has both 'payload' and legacy 'task'")
        return wire
    if payload is not None:
        raise ValueError(f"payload must be an object, got {type(payload).__name__}")
    if isinstance(task, str) and task.strip():
        wire["payload"] = {"kind": "agentTurn", "message": task}
        return wire
    raise ValueError("cron entry has neither 'payload' nor a legacy 'task' string")


def _cron_entry_needs_shape_heal(entry: dict) -> bool:
    """True when an already-installed jobs.json entry is in the legacy wire
    shape (or the migrator's broken wrap of it) and would be quarantined —
    or mis-scheduled — by the OpenClaw ≥2026.7 import."""
    if "task" in entry or not isinstance(entry.get("payload"), dict):
        return True
    schedule = entry.get("schedule")
    if isinstance(schedule, str):
        return True
    return (
        isinstance(schedule, dict)
        and schedule.get("kind") == "cron"
        and isinstance(schedule.get("expr"), str)
        and bool(_LEGACY_CRON_SCHEDULE_RE.match(schedule["expr"]))
    )
