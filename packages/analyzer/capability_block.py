#!/usr/bin/env python3
"""capability_block.py — the unified ``[INSTALLED CAPABILITIES]`` push block.

Spec: docs/spec-bot-capability-awareness-2026-06-22.md §3 (PUSH) + §4
(insertion points). This is **P1** — the capability *push*: tell a bot's
agent, every session, what it actually has installed.

The defect this fixes (grounded 2026-06-22): a bot's agent is never told
what skills/integration-tools it has, so it falls back to training — a
consumer bot invented a nonexistent "Himalaya" email CLI instead of using
its 15 registered Google tools. **Apps** are already injected (into
``AGENTS.md`` via ``app_registry``); **skills** and **integration tools**
are injected nowhere. This module closes that gap with one block listing,
for each capability the bot actually has: name, one-line purpose, example
trigger phrases, and an explicit *"use this — do NOT improvise or invent
an alternative."*

Three sources, unified::

  1. Installed skills   — ``~/.openclaw/skills/*/SKILL.md`` frontmatter
                          (name / description / when_to_use)
  2. Integration tools  — configured integrations contribute their tool set
                          via a provider registry; Google sources from the
                          shared ``google_service.TOOL_SPECS`` so the block
                          never lists a tool the bot doesn't actually have
  3. Apps               — reuse the existing app inventory (caller-supplied
                          ``CapabilityItem`` list; kept adjacent + consistent)

Delivery (spec §5, P1): the block ships **per turn** via the plugin's
``before_prompt_build`` hook, which shells out to
``session_surface.py --capabilities-only`` (TTL-cached). Per-turn — not
session_start — because session_start fires once per OC session and never
re-fires for existing long-running Telegram chats, so a session_start-only
push (what CA-P1 #3080 shipped) never reaches an already-deployed bot. Apps
are delivered separately, durably, via the bot's ``AGENTS.md`` Installed-Apps
section; this block fills the gap apps don't (skills + integration tools),
so ``app_items`` is omitted by the runtime caller (it remains a supported
renderer parameter for flexibility/tests).

Every collector soft-fails to ``[]`` and the renderer hard-caps total
size, so a malformed ``SKILL.md`` or an integration import error never
blocks a turn. This mirrors the contract the other session_surface blocks
already honor.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ── The unified capability item ───────────────────────────────────────────────


@dataclass(frozen=True)
class CapabilityItem:
    """One installed capability the agent should be told about.

    ``source`` keys the rendered section ("skill" | "integration" |
    "app"). ``group`` sub-labels a source (e.g. the integration name
    "Google") so several integrations can each get their own header.
    ``group_hint`` is an optional one-line preamble for the whole group
    — Google uses it to say "use these to read/send mail; do NOT use a
    CLI or invent an email tool". ``triggers`` are example phrases that
    should route here; ``how_to`` is an optional "how to reach it" line.
    """

    name: str
    purpose: str
    source: str
    group: str = ""
    group_hint: str = ""
    triggers: list[str] = field(default_factory=list)
    how_to: str = ""


# Order capability sources appear in the block. Skills + tools first
# (they're the gap this block closes — injected nowhere before P1), apps
# last (already injected via INSTALLED_APPS.md; included here for a single
# coherent list, with a pointer to the detailed file).
_SOURCE_ORDER = ("skill", "integration", "app")

# Per-source section headers. The skill/app headers are fixed; integration
# capabilities carry their own group header (see _render).
_SOURCE_HEADER = {
    "skill": "Skills (installed recipes — follow the skill's steps; do NOT reinvent them):",
    "app": "Apps (see INSTALLED_APPS.md for triggers + exact commands):",
}

# Hard cap on the rendered block (bytes). Comfortably above a typical
# render (a dozen skills + 15 Google tools + a few apps ≈ 2-3 KB) but
# bounded so a pod with many capabilities or long descriptions can't blow
# the system-prompt budget. Truncated at a line boundary with a pointer.
DEFAULT_CAP_BYTES = 4096

# One-line purpose cap — skill descriptions can run several sentences; we
# keep the first sentence (or a hard truncation) so each row stays short.
_PURPOSE_CAP = 200

# Example trigger phrases shown per capability.
_MAX_TRIGGERS = 3


# ── Skills source ─────────────────────────────────────────────────────────────


def _split_frontmatter(raw: str) -> dict[str, Any]:
    """Parse YAML frontmatter from a ``SKILL.md``.

    Same convention as ``evolve_admin.evo.skill_retirement._split_frontmatter``
    — kept independent so analyzer doesn't import the admin module just to
    read a skill header. Returns ``{}`` for no/malformed frontmatter or
    when PyYAML is unavailable; never raises.
    """
    text = raw.lstrip("﻿")  # strip BOM if present
    if not text.startswith("---"):
        return {}
    rest = text[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    fence_idx = rest.find("\n---")
    if fence_idx == -1:
        return {}
    fm_text = rest[:fence_idx]
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load(fm_text) if fm_text.strip() else {}
    except Exception:  # noqa: BLE001 — yaml errors of any shape
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _first_sentence(text: str, cap: int = _PURPOSE_CAP) -> str:
    """Collapse whitespace, take the first sentence, hard-cap length."""
    one = " ".join((text or "").split()).strip()
    if not one:
        return ""
    # First sentence: split on ". " but keep it simple — many descriptions
    # are a single clause with no period.
    dot = one.find(". ")
    if 0 < dot < cap:
        return one[: dot + 1]
    if len(one) > cap:
        return one[: cap - 1].rstrip() + "…"
    return one


def _skill_triggers(fm: dict[str, Any]) -> list[str]:
    """Pull example trigger phrases from a skill's frontmatter.

    Looks at ``when_to_use`` (a list of phrases, or a single string) and
    an optional ``metadata.evolve.example_triggers`` list. Returns a
    de-duplicated, capped list; ``[]`` when nothing usable is present.
    """
    out: list[str] = []
    wtu = fm.get("when_to_use")
    if isinstance(wtu, list):
        out.extend(str(x) for x in wtu if isinstance(x, (str, int, float)))
    elif isinstance(wtu, str) and wtu.strip():
        out.append(wtu.strip())
    md = fm.get("metadata")
    if isinstance(md, dict):
        ev = md.get("evolve")
        if isinstance(ev, dict):
            ex = ev.get("example_triggers")
            if isinstance(ex, list):
                out.extend(str(x) for x in ex if isinstance(x, (str, int, float)))
    # De-dup preserving order, drop empties, collapse whitespace, cap.
    seen: set[str] = set()
    cleaned: list[str] = []
    for t in out:
        t = " ".join(str(t).split()).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            cleaned.append(t)
    return cleaned[:_MAX_TRIGGERS]


def collect_skill_capabilities(skills_dir: Path) -> list[CapabilityItem]:
    """Scan ``skills_dir`` for ``SKILL.md`` files and build skill items.

    ``skills_dir`` is the bot's ``~/.openclaw/skills`` (the bot reads its
    own; the admin/evolve side reads ``/Users/<bot>/.openclaw/skills`` via
    the ACL). Recursive — OC's loader scans the whole subtree. A skill with
    no ``description`` still appears (name alone is useful). Broken files
    are skipped silently; the whole function soft-fails to ``[]``.
    """
    items: list[CapabilityItem] = []
    try:
        if not skills_dir.is_dir():
            return []
        paths = sorted(skills_dir.rglob("SKILL.md"))
    except OSError:
        return []
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _split_frontmatter(raw)
        name = ""
        raw_name = fm.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        else:
            # OC's loader defaults the skill name to its parent dir.
            name = path.parent.name
        if not name:
            continue
        desc = fm.get("description")
        purpose = _first_sentence(desc) if isinstance(desc, str) else ""
        items.append(
            CapabilityItem(
                name=name,
                purpose=purpose,
                source="skill",
                triggers=_skill_triggers(fm),
            )
        )
    # Stable, readable order.
    items.sort(key=lambda c: c.name.lower())
    return items


# ── Integration-tool source (provider registry) ───────────────────────────────

# An integration provider takes ``(bot_id, network)`` and returns the
# CapabilityItems this bot gets from that integration — or ``[]`` when the
# integration isn't configured for the bot. Each provider owns its own
# (lazy) imports so analyzer stays importable standalone and one broken
# provider can't sink the others. Register new integrations here.
IntegrationProvider = Callable[[str, dict], "list[CapabilityItem]"]


def _google_provider(bot_id: str, network: dict) -> list[CapabilityItem]:
    """Google integration tools, sourced from the shared TOOL_SPECS.

    Gated on ``google_service.is_google_configured`` so the block never
    lists a tool a bot without a configured ``google_integration`` would
    not actually have. The tool list + one-line descriptions come from
    ``google_service.google_state(...)['tools']`` — the SAME source the
    Evolve OC plugin registers from (Google-arch §4.1) — so this can't
    drift from what's registered. Lazy import + soft-fail to ``[]``.
    """
    try:
        from evolve_admin import google_service  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            f"[capability_block] google_service unavailable ({e}) — "
            "Google tools will not be listed",
            file=sys.stderr,
        )
        return []
    try:
        state = google_service.google_state(bot_id, network)
    except Exception as e:  # noqa: BLE001 — never block on a config quirk
        print(
            f"[capability_block] google_state error for {bot_id} ({e}) — "
            "Google tools will not be listed",
            file=sys.stderr,
        )
        return []
    if not state.get("configured"):
        return []
    hint = (
        "call these by their exact tool name to read/send mail and manage "
        "the calendar & Drive — do NOT use a shell CLI (no gws, no Himalaya) "
        "or invent an email tool; these registered tools are the only "
        "correct way to reach Google."
    )
    items: list[CapabilityItem] = []
    for tool in state.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        items.append(
            CapabilityItem(
                name=name.strip(),
                purpose=_first_sentence(str(tool.get("description") or "")),
                source="integration",
                group="Google",
                group_hint=hint,
            )
        )
    return items


# The default provider registry. Tests / callers can pass an override list.
INTEGRATION_PROVIDERS: list[IntegrationProvider] = [_google_provider]


def collect_integration_capabilities(
    bot_id: str,
    network: dict,
    providers: list[IntegrationProvider] | None = None,
) -> list[CapabilityItem]:
    """Run every integration provider and collect their CapabilityItems.

    One provider raising can't sink the rest — each is wrapped. Soft-fails
    to ``[]`` overall when ``bot_id`` / ``network`` are unusable.
    """
    if not bot_id or not isinstance(network, dict):
        return []
    out: list[CapabilityItem] = []
    for provider in providers if providers is not None else INTEGRATION_PROVIDERS:
        try:
            out.extend(provider(bot_id, network) or [])
        except Exception as e:  # noqa: BLE001 — isolate provider failures
            print(
                f"[capability_block] integration provider "
                f"{getattr(provider, '__name__', provider)!r} failed ({e})",
                file=sys.stderr,
            )
            continue
    return out


# ── Rendering ─────────────────────────────────────────────────────────────────


_BLOCK_HEADER = (
    "[INSTALLED CAPABILITIES — the skills, integration tools, and apps "
    "actually registered for you right now. USE THESE for the things they "
    "do; do NOT improvise, reach for a half-remembered CLI, or invent an "
    "alternative tool.]"
)

_BLOCK_PREAMBLE = (
    "Before you say you can't do something — or reach for a tool/CLI you "
    "remember from training — check this list. These are your real, "
    "registered capabilities. If a request matches one, use it; never "
    "substitute or invent another tool."
)


def _trigger_suffix(triggers: list[str]) -> str:
    """Render the ``e.g. "a", "b"`` example-trigger suffix, or ""."""
    shown = [t for t in triggers if t][:_MAX_TRIGGERS]
    if not shown:
        return ""
    quoted = ", ".join(f'"{t}"' for t in shown)
    return f"  e.g. {quoted}"


def render_capabilities_block(
    items: list[CapabilityItem],
    *,
    cap_bytes: int = DEFAULT_CAP_BYTES,
) -> str:
    """Render the capability items as the labeled ``[INSTALLED CAPABILITIES]``
    block, grouped by source (skills → integration tools → apps).

    Returns ``""`` for an empty list (the caller gates the whole block on
    ``items`` being non-empty). The render is hard-capped at ``cap_bytes``
    and truncated at a line boundary with a pointer, so it can never blow
    the system-prompt budget.
    """
    items = [c for c in items if isinstance(c, CapabilityItem) and c.name]
    if not items:
        return ""

    by_source: dict[str, list[CapabilityItem]] = {}
    for c in items:
        by_source.setdefault(c.source, []).append(c)

    lines: list[str] = [_BLOCK_HEADER, _BLOCK_PREAMBLE]

    for source in _SOURCE_ORDER:
        bucket = by_source.get(source)
        if not bucket:
            continue
        if source == "integration":
            lines.extend(_render_integration_groups(bucket))
        else:
            lines.append("")
            header = _SOURCE_HEADER.get(source)
            if header:
                lines.append(header)
            for c in bucket:
                lines.extend(_render_item_lines(c))

    rendered = "\n".join(lines)
    encoded = rendered.encode("utf-8")
    if len(encoded) > cap_bytes:
        rendered = _truncate_at_line_boundary(rendered, cap_bytes)
    return rendered


def _render_integration_groups(bucket: list[CapabilityItem]) -> list[str]:
    """Render integration capabilities grouped by their ``group`` label,
    each with its one-line ``group_hint`` preamble."""
    out: list[str] = []
    by_group: dict[str, list[CapabilityItem]] = {}
    order: list[str] = []
    for c in bucket:
        g = c.group or "Tools"
        if g not in by_group:
            by_group[g] = []
            order.append(g)
        by_group[g].append(c)
    for g in order:
        members = by_group[g]
        hint = next((m.group_hint for m in members if m.group_hint), "")
        out.append("")
        label = f"{g} tools (configured integration"
        out.append(f"{label} — {hint}):" if hint else f"{label}):")
        for c in members:
            out.extend(_render_item_lines(c))
    return out


def _render_item_lines(c: CapabilityItem) -> list[str]:
    """Render one capability: the name/purpose line plus optional
    trigger-example and how-to continuation lines."""
    if c.purpose:
        head = f"- {c.name} — {c.purpose}"
    else:
        head = f"- {c.name}"
    suffix = _trigger_suffix(c.triggers)
    if suffix:
        head += suffix
    out = [head]
    if c.how_to:
        out.append(f"    {c.how_to}")
    return out


def _truncate_at_line_boundary(text: str, cap_bytes: int) -> str:
    """Trim ``text`` so its UTF-8 encoding fits in ``cap_bytes``, at the
    last full-line boundary that fits, appending a truncation pointer.
    Mirrors session_surface._truncate_at_line_boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return text
    tail = "\n(…capability list truncated; the tools above are real and usable.)"
    tail_bytes = len(tail.encode("utf-8"))
    budget = max(cap_bytes - tail_bytes, 0)
    head = encoded[:budget]
    last_nl = head.rfind(b"\n")
    if last_nl > 0:
        head = head[:last_nl]
    return head.decode("utf-8", errors="ignore") + tail


# ── Top-level assembly ────────────────────────────────────────────────────────


def build_capability_items(
    bot_id: str,
    *,
    skills_dir: Path | None = None,
    network: dict | None = None,
    app_items: list[CapabilityItem] | None = None,
    integration_providers: list[IntegrationProvider] | None = None,
) -> list[CapabilityItem]:
    """Gather every capability source into one ``CapabilityItem`` list.

    ``skills_dir`` (when given) is scanned for ``SKILL.md`` files;
    ``network`` (when given) drives the integration providers; ``app_items``
    are caller-supplied (the apps inventory is owned by ``app_registry`` —
    we reuse its data rather than re-deriving it here). Any source can be
    omitted; each soft-fails independently.
    """
    items: list[CapabilityItem] = []
    if skills_dir is not None:
        items.extend(collect_skill_capabilities(skills_dir))
    if network is not None and bot_id:
        items.extend(
            collect_integration_capabilities(
                bot_id, network, providers=integration_providers
            )
        )
    if app_items:
        items.extend(c for c in app_items if isinstance(c, CapabilityItem))
    return items


def build_capabilities_block(
    bot_id: str,
    *,
    skills_dir: Path | None = None,
    network: dict | None = None,
    app_items: list[CapabilityItem] | None = None,
    integration_providers: list[IntegrationProvider] | None = None,
    cap_bytes: int = DEFAULT_CAP_BYTES,
) -> str:
    """Collect every source and render the ``[INSTALLED CAPABILITIES]``
    block in one call. Returns ``""`` when the bot has no capabilities.

    Convenience wrapper used by both injection surfaces; never raises.
    """
    try:
        items = build_capability_items(
            bot_id,
            skills_dir=skills_dir,
            network=network,
            app_items=app_items,
            integration_providers=integration_providers,
        )
        return render_capabilities_block(items, cap_bytes=cap_bytes)
    except Exception as e:  # noqa: BLE001 — must never block session start
        print(
            f"[capability_block] block assembly failed for {bot_id} ({e}) — "
            "capability block will not be injected",
            file=sys.stderr,
        )
        return ""
