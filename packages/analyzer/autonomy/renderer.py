"""autonomy.renderer — render posture intent into enforcement surfaces.

Spec: docs/spec-autonomy-ladder-2026-06-10.md §2.3 / §2.4.

Two surfaces in Phase A:

  - ``mcp_tool_allowlist`` (mechanical) — denied tools rendered into
    OpenClaw's global ``tools.deny`` list as ``mcp__<server>__<tool>``
    entries. Ownership boundary is the per-integration prefix: the
    renderer replaces exactly the entries under its prefixes and
    preserves every foreign entry (the slack-writer merge discipline).
  - ``bot_guidance`` (procedural) — NOT written by this module at all:
    ``session_surface.load_autonomy_block`` reads the intent file
    directly at session start and injects the rung guidance via
    systemAppend. Live by construction once the posture file is
    written; the renderer only records the enforcement entry for it.

Write path honors CLAUDE.md §File Access Pattern: openclaw.json is
read with the sudo-cat fallback and written through
``safe_write_bot_config`` (validate → /tmp stage → sudo /bin/cp) +
scheduler-seam gateway kickstart — the exact L2 discipline; **no new
bot-side write mechanism**. Test path (``home_override``) reads and
writes the synthetic tree directly.

Backfill-inferred postures are NOT rendered (spec §5.2 observe-first:
never demote — or constrain — a working bot the operator hasn't
confirmed). They become render-eligible on the first deliberate
operator action, which rewrites ``set_by``.

Concurrency note: openclaw.json read→merge→write here is unlocked, the
same exposure every existing writer of that file has (slack writer, L2
appliers, deploy). A racing deploy can win the write; the coherence
check flags the gap as ``autonomy_posture_drift`` and re-applying (or
the next deploy) repairs it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from evolve_util import now_iso

from . import catalog as _catalog
from . import store as _store


SURFACE_MCP = "mcp_tool_allowlist"
SURFACE_GUIDANCE = "bot_guidance"


@dataclass
class RenderResult:
    """Outcome of one bot render. Never raised — every failure path
    lands in ``write_error`` (the slack-writer contract)."""

    bot_id: str
    changed: bool = False
    written: bool = False
    write_error: str | None = None
    # integration_ids skipped as observe-only (backfill_inferred)
    skipped_unconfirmed: list[str] = field(default_factory=list)


def read_live_openclaw(
    bot_id: str,
    config: dict | None = None,
    *,
    home_override: Path | None = None,
) -> dict[str, Any] | None:
    """Read the bot's live openclaw.json (direct read, sudo-cat fallback).

    The one home-resolution + read path shared by the renderer, the
    backfill inference, and the coherence check — all three MUST look
    at the same file or render and audit can disagree about a bot.
    """
    from permissions.writer import read_openclaw_json
    if home_override is not None:
        home = home_override
    else:
        from evolve_config import bot_home as _bot_home
        home = _bot_home(bot_id, config)
    return read_openclaw_json(home / ".openclaw" / "openclaw.json")


# ── Pure layer ────────────────────────────────────────────────────────────────


def posture_is_render_eligible(posture: _store.IntegrationPosture) -> bool:
    """Observe-first gate: backfill-inferred postures are recorded and
    surfaced but never rendered until an operator makes them deliberate."""
    return (posture.set_by or {}).get("actor") != _store.ACTOR_BACKFILL


def expected_deny_entries(
    posture: _store.IntegrationPosture,
    *,
    server_tools: list[str] | None = None,
    paused: bool = False,
) -> list[str] | None:
    """Full ``mcp__<server>__<tool>`` entries this posture requires.

    Returns ``None`` when the integration has no binding / kind spec
    (not ladder-managed) — and, deliberately, when the known tool
    surface is EMPTY. An unknown surface must fail closed: if the
    vetted catalog became unimportable or the entry vanished, treating
    that as "deny nothing" would strip the existing owned deny entries
    and silently widen the bot. ``None`` means "don't touch this
    prefix"; the previously-rendered denies stay in place.
    """
    binding = _catalog.binding_for(posture.integration_id)
    if binding is None:
        return None
    spec = _catalog.kind_spec(binding.kind)
    if spec is None:
        return None
    tools = (
        server_tools if server_tools is not None
        else _catalog.known_server_tools(posture.integration_id)
    )
    if not tools:
        return None
    denied = _catalog.expected_denied_tools(
        spec, binding, tools, posture.rung, posture.rules, paused=paused,
    )
    return [_catalog.oc_deny_entry(posture.integration_id, t) for t in denied]


def expected_deny_by_integration(
    doc: _store.AutonomyDoc,
    *,
    paused: "set[str] | None" = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Compute the deny slice for every render-eligible integration.

    Returns ``(expected, skipped_unconfirmed)``. Only integrations
    present in ``expected`` have their prefix owned by the merge —
    skipped ones keep whatever is live (including operator hand-edits).

    ``paused`` is the set of integration ids currently paused by a
    rung-3 daily cap (``autonomy.limits.paused_integrations``); their
    outward verbs are denied on top of the rung's normal slice.
    """
    expected: dict[str, list[str]] = {}
    skipped: list[str] = []
    paused = paused or set()
    for iid, posture in sorted(doc.integrations.items()):
        if not posture_is_render_eligible(posture):
            skipped.append(iid)
            continue
        entries = expected_deny_entries(posture, paused=iid in paused)
        if entries is None:
            continue
        expected[iid] = entries
    return expected, skipped


def paused_for_bot(
    shared_dir: Path, bot_id: str, *, now: datetime | None = None,
) -> set[str]:
    """The current daily-cap pause set — soft-failing: a broken limits
    sidecar must not take render or coherence down with it (the pause
    is an extra constraint on top of the posture, never the posture).

    ``now`` pins the evaluation instant: the pause set is "what is paused
    as of ``now``", so a render triggered by an evaluation at a given
    instant denies exactly what that evaluation saw. Left ``None`` (every
    production caller) it reads the wall clock, as before; the limits
    pass and the demotion reflex thread their own ``now`` through so the
    render they trigger agrees with the cap state they just wrote, rather
    than re-reading a clock that may have crossed the UTC day boundary."""
    try:
        from . import limits as _limits
        return _limits.paused_integrations(shared_dir, bot_id, now=now)
    except Exception:  # noqa: BLE001 — derived state only
        return set()


def merge_deny_slice(
    existing: dict[str, Any],
    expected: dict[str, list[str]],
) -> tuple[dict[str, Any], bool]:
    """Merge the autonomy deny slice into an openclaw.json dict.

    Ownership boundary: entries under ``mcp__<integration>__`` for the
    integrations in ``expected`` are replaced wholesale; every other
    entry (other servers, non-MCP denies, operator hand-edits) is
    preserved in its original order. Owned entries append sorted.

    Returns ``(merged, changed)`` — ``merged`` is a deep-enough copy;
    ``existing`` is never mutated.
    """
    merged = json.loads(json.dumps(existing))
    tools = merged.get("tools")
    if not isinstance(tools, dict):
        tools = {}
    current_deny = tools.get("deny")
    if not isinstance(current_deny, list):
        current_deny = []

    # Ownership boundary is per-integration and source-aware: mcp_server
    # integrations own the ``mcp__<id>__`` prefix; plugin integrations
    # own their bare tool names (see catalog.deny_entry_is_owned). Only
    # entries owned by an integration in ``expected`` are replaced.
    owned_iids = list(expected)
    kept = [
        e for e in current_deny
        if not (isinstance(e, str)
                and any(_catalog.deny_entry_is_owned(iid, e) for iid in owned_iids))
    ] if owned_iids else list(current_deny)

    owned_new = sorted({e for entries in expected.values() for e in entries})
    new_deny = kept + [e for e in owned_new if e not in kept]

    if new_deny == current_deny:
        return merged, False

    # A hand-edited non-dict "tools" value would TypeError on assignment;
    # replace it (safe_write_bot_config validates the merged result
    # against the OC schema before anything lands on disk).
    if not isinstance(merged.get("tools"), dict):
        merged["tools"] = {}
    merged["tools"]["deny"] = new_deny
    return merged, True


def is_render_up_to_date(
    existing: dict[str, Any],
    doc: _store.AutonomyDoc,
    *,
    paused: "set[str] | None" = None,
) -> bool:
    """Drift-check primitive: does the live config already match what
    the renderer would produce? (Used by ``autonomy.coherence``.)"""
    expected, _ = expected_deny_by_integration(doc, paused=paused)
    _, changed = merge_deny_slice(existing, expected)
    return not changed


def enforcement_records(
    posture: _store.IntegrationPosture,
    *,
    verified: bool,
    rendered_at: str | None = None,
) -> list[dict[str, Any]]:
    """The spec §2.2 ``enforcement[]`` entries for a rendered posture."""
    binding = _catalog.binding_for(posture.integration_id)
    spec = _catalog.kind_spec(binding.kind) if binding else None
    at = rendered_at or now_iso()
    guidance = (
        _catalog.guidance_for(spec, posture.rung, posture.rules) if spec else ""
    )
    return [
        {
            "surface": SURFACE_MCP,
            "mode": "mechanical",
            "rendered_at": at,
            "verified": verified,
        },
        {
            "surface": SURFACE_GUIDANCE,
            "mode": "procedural",
            "rendered_at": at,
            # The guidance surface is injected from the intent file at
            # session start — live by construction when non-empty.
            "verified": bool(guidance),
        },
    ]


def _enforcement_equivalent(
    old: list[dict[str, Any]], new: list[dict[str, Any]],
) -> bool:
    """Equal ignoring timestamps — avoids rewriting autonomy.json on
    every no-op render."""
    def strip(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in r.items() if k != "rendered_at"} for r in records
        ]
    return strip(old) == strip(new)


# ── I/O layer ─────────────────────────────────────────────────────────────────


def merge_autonomy_into_config(
    cfg: dict[str, Any], bot_id: str, shared_dir: Path,
    *,
    now: datetime | None = None,
) -> bool:
    """Deploy-pipeline hook: merge the deny slice into an in-memory
    openclaw.json dict (mutates ``cfg``). Returns True when changed.

    Called from ``deploy.ensure_plugin_config`` after the policy-slice
    pass, so posture enforcement survives every redeploy by
    construction (the slack-policy property, spec §2.1-C). A malformed
    posture file logs and no-ops — a deploy must not fail because the
    posture file is bad; the coherence check surfaces it instead.

    Deliberately does NOT write enforcement records: deploys run as
    root, and a root-written autonomy.json (mkstemp 0600) would lock
    the evolve-user admin server out of the posture file. Records are
    owned by the evolve-side render path (``render_bot``); the UI's
    live coherence status covers the gap between renders, and the
    deploy hook also cannot know yet whether the final config write at
    the end of ensure_plugin_config will succeed.
    """
    try:
        doc = _store.load(shared_dir, bot_id)
    except ValueError as exc:
        print(f"[autonomy/renderer] {bot_id}: posture file unreadable, skipping render: {exc}")
        return False
    if doc is None or not doc.integrations:
        return False
    expected, _skipped = expected_deny_by_integration(
        doc, paused=paused_for_bot(shared_dir, bot_id, now=now),
    )
    if not expected:
        return False
    merged, changed = merge_deny_slice(cfg, expected)
    if changed:
        cfg.clear()
        cfg.update(merged)
    return changed


def render_bot(
    bot_id: str,
    shared_dir: Path,
    *,
    home_override: Path | None = None,
    now: datetime | None = None,
) -> RenderResult:
    """Read openclaw.json, merge the deny slice, write, kickstart.

    The on-demand variant (posture write path: API promote/demote).
    The deploy pipeline reproduces the same result on the next deploy
    via :func:`merge_autonomy_into_config`. Never raises — failures
    land in ``RenderResult.write_error``.

    ``now`` pins the daily-cap pause lookup to the evaluation instant
    (see :func:`paused_for_bot`). The limits pass and the demotion
    reflex pass their own ``now`` so the pause they just recorded for
    today is the pause this render denies — without it, a render reading
    the wall clock just after the UTC day rolls would treat a fresh
    same-day pause as stale and skip the deny.
    """
    result = RenderResult(bot_id=bot_id)
    try:
        doc = _store.load(shared_dir, bot_id)
    except ValueError as exc:
        result.write_error = f"posture_file_invalid: {exc}"
        return result
    if doc is None or not doc.integrations:
        return result

    expected, skipped = expected_deny_by_integration(
        doc, paused=paused_for_bot(shared_dir, bot_id, now=now),
    )
    result.skipped_unconfirmed = skipped
    if not expected:
        return result

    current = read_live_openclaw(bot_id, home_override=home_override)
    if current is None:
        result.write_error = f"openclaw_unreadable: {bot_id}"
        return result

    merged, changed = merge_deny_slice(current, expected)
    result.changed = changed

    if changed:
        if home_override is not None:
            oc_path = home_override / ".openclaw" / "openclaw.json"
            oc_path.parent.mkdir(parents=True, exist_ok=True)
            oc_path.write_text(json.dumps(merged, indent=2))
            result.written = True
        else:
            try:
                from evolve_admin.deploy import (  # pyright: ignore[reportMissingImports]
                    safe_write_bot_config,
                )
            except ImportError as exc:
                result.write_error = f"safe_write_bot_config unavailable: {exc}"
                return result
            ok, err = safe_write_bot_config(
                bot_id, merged, reason="autonomy.renderer",
            )
            if not ok:
                result.write_error = err
                return result
            result.written = True
            # Scheduler-seam restart (kickstart -k). Result ignored:
            # restart can fail if the service isn't loaded — not a
            # write failure (the L2 applier contract).
            from runtime.scheduler import get_scheduler
            get_scheduler().restart(f"ai.openclaw.{bot_id}-gateway")

    # Record enforcement for every rendered integration. verified=True:
    # the merge is on disk (or already was). Timestamp-insensitive skip
    # keeps no-op renders from churning the posture file.
    at = now_iso()
    for iid in expected:
        posture = doc.integrations.get(iid)
        if posture is None:
            continue
        records = enforcement_records(posture, verified=True, rendered_at=at)
        if _enforcement_equivalent(posture.enforcement, records):
            continue
        try:
            _store.record_enforcement(shared_dir, bot_id, iid, records)
        except Exception as exc:  # noqa: BLE001 — audit metadata only
            print(f"[autonomy/renderer] {bot_id}/{iid}: enforcement record failed: {exc}")

    return result


__all__ = [
    "RenderResult",
    "SURFACE_GUIDANCE",
    "SURFACE_MCP",
    "enforcement_records",
    "expected_deny_by_integration",
    "expected_deny_entries",
    "is_render_up_to_date",
    "merge_autonomy_into_config",
    "merge_deny_slice",
    "posture_is_render_eligible",
    "read_live_openclaw",
    "render_bot",
]
