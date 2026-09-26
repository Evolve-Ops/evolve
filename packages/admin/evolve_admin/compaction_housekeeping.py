"""The deploy half of housekeeping on the cheap rung — ``agents.defaults.compaction``.

Brief: ``compaction-and-memory-flush-on-cheap-rung``. Finding:
``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §2.

Kept out of ``deploy.py`` (no-growth-capped, ``tools/file-size-ratchet``); deploy
calls :func:`apply_for_bot` once per ``ensure_plugin_config`` in place of the
bare retired-key strip, which this now runs first.

What OpenClaw 2026.9.2 exposes, and so what this writes (verified against the
installed dist — the PR body carries the trail):

**Compaction's model and thinking are config-only.** The summariser is not an
agent run and never fires ``before_model_resolve``; it uses
``compaction.model`` (else the session's model) and ``compaction.thinkingLevel``
(else ``"low"``). So with the lever on, deploy converges both to the bot's
``fast`` role and ``"off"``. The memory flush IS an agent run and the plugin
routes it (``housekeeping.ts``); ``compaction.memoryFlush.model`` is left alone
on purpose — setting it makes OC drop the session's fallback chain for the
flush (``fallbacksOverride: []``), so a cheap-rung outage would fail the flush
and cost the user their memory note.

**The compaction floor is a budget now, not a cliff.** OC 2026.9.2 retired
``compaction.reserveTokensFloor`` (strict-schema reject; the reserve is a
fixed 20k), so token-triggered compaction fires at *window − 20k* — ~980k on
the 1M-window Sonnet 5 / Opus 5, where the old 80k floor meant ~120k on a 200k
window. The flush then carries the whole of that to write one note. The only
budget knob left is ``compaction.maxActiveTranscriptBytes`` (compact when the
active transcript passes N bytes, any model), paired with
``memoryFlush.forceFlushTranscriptBytes`` below it so the flush always runs on
an earlier turn than the compaction it precedes (F-CE2: no silent memory loss —
OC also runs the flush before preflight compaction within a turn, so a single
turn that crosses both still flushes first). ``docs/configuration.md`` carries
the trade-off.

Opt-out (D-CS5): ``network.json`` ``bots.<id>.cost.levers.housekeeping_cheap_rung:
false`` (or the pod-level ``cost.levers`` key). Opting out removes the model and
thinking values this module wrote — recognised as "equal to the fast role's
model" / ``"off"`` — and leaves anything else the operator set. The budget is
not part of the lever: it is the cost profile's, gap-filled like any other
cost default and never overwritten.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .oc_retired_keys import strip_retired_openclaw_keys

#: Balanced profile's compaction budget, in bytes of active transcript. At the
#: ~3 bytes/token measured on long real sessions (two team bots — 2026-09-24) this
#: compacts at roughly 130k tokens of history, ~170k of context with the fixed
#: prefix: about where a 200k-window model compacted under the old floor, and a
#: sixth of where a 1M-window model compacts with no budget at all. Mirrored in
#: ``cost_profiles.BUILTIN_PROFILES["balanced"]`` (coherence-tested).
BALANCED_COMPACTION_BUDGET_BYTES = 400 * 1024

#: The flush fires at this share of the compaction budget, so it lands on an
#: earlier turn than the compaction it exists to precede.
FLUSH_BEFORE_COMPACTION_RATIO = 0.8

#: The thinking level housekeeping runs at. Summarising and note-writing do not
#: need extended thinking; on the measured turn thinking was most of the output.
HOUSEKEEPING_THINKING_LEVEL = "off"

_BYTE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2,
               "g": 1024 ** 3, "gb": 1024 ** 3, "t": 1024 ** 4, "tb": 1024 ** 4}
_BYTE_RE = re.compile(r"^(\d+(?:\.\d+)?)([a-z]*)$")


def parse_byte_size(value: Any) -> int | None:
    """OC's byte-size grammar (binary units: ``512kb``, ``2mb``, or a bare
    integer). ``None`` for anything OC itself would not accept."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    m = _BYTE_RE.match(value.strip().lower())
    if not m or m.group(2) not in _BYTE_UNITS:
        return None
    return int(round(float(m.group(1)) * _BYTE_UNITS[m.group(2)]))


#: OC 2026.9.2 constants the flush trigger uses (memory-core + agent-settings).
OC_COMPACTION_RESERVE_TOKENS = 20_000
OC_DEFAULT_FLUSH_SOFT_TOKENS = 4_000
OC_DEFAULT_FORCE_FLUSH_BYTES = 2 * 1024 * 1024

#: Transcript bytes per context token, measured on the longest real sessions on
#: the test pod (two team bots, 2026-09-24): ~3.0. The fixed prefix (system prompt,
#: bootstrap, tool schemas) is not in the transcript, so it is added back.
MEASURED_BYTES_PER_TOKEN = 3.0
FIXED_PREFIX_TOKENS = 40_000


def estimate_flush_input_tokens(
    *,
    context_window_tokens: int,
    compaction: dict,
    bytes_per_token: float = MEASURED_BYTES_PER_TOKEN,
    fixed_prefix_tokens: int = FIXED_PREFIX_TOKENS,
) -> int:
    """Input tokens the memory flush carries, from OC's two flush triggers.

    The flush runs on the session's whole active context, so its input is
    whatever that context has grown to when the FIRST trigger fires:

      * token: ``window − reserve − softThreshold`` (reserve fixed at 20k
        since 2026.9.2; soft threshold capped at half the usable window);
      * bytes: ``memoryFlush.forceFlushTranscriptBytes`` (OC default 2 MiB).

    A model of OC's trigger, not a measurement — the before/after ratio it
    gives is what the brief asks to be asserted; the absolute numbers ride on
    the measured bytes/token.
    """
    raw_flush = compaction.get("memoryFlush")
    flush: dict = raw_flush if isinstance(raw_flush, dict) else {}
    usable = max(context_window_tokens - OC_COMPACTION_RESERVE_TOKENS, 0)
    soft = flush.get("softThresholdTokens")
    soft = soft if isinstance(soft, int) and soft >= 0 else OC_DEFAULT_FLUSH_SOFT_TOKENS
    soft = min(soft, usable // 2)
    token_trigger = max(usable - soft, 0)
    force_bytes = parse_byte_size(flush.get("forceFlushTranscriptBytes"))
    if force_bytes is None:
        force_bytes = OC_DEFAULT_FORCE_FLUSH_BYTES
    byte_trigger = fixed_prefix_tokens + int(force_bytes / bytes_per_token)
    return min(token_trigger, byte_trigger)


def apply_compaction_housekeeping(
    cfg: dict, *, fast_model: str | None, lever_on: bool,
) -> list[str]:
    """Converge ``agents.defaults.compaction`` in ``cfg`` (in place).

    Returns a human-readable list of what changed (empty ⇒ no-op), so deploy
    can log it and callers can assert idempotency. Never creates the
    compaction block: a bot whose cost snapshot deliberately removed it keeps
    it removed (``gap_fill_cost_settings`` owns that decision and runs first).
    """
    agents = cfg.get("agents")
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    if not isinstance(defaults, dict):
        return []
    comp = defaults.get("compaction")
    if not isinstance(comp, dict):
        return []
    changes: list[str] = []

    # ── Budget (profile-owned; gap-fill, never overwrite) ─────────────────
    if "maxActiveTranscriptBytes" not in comp:
        comp["maxActiveTranscriptBytes"] = BALANCED_COMPACTION_BUDGET_BYTES
        changes.append(f"compaction.maxActiveTranscriptBytes={BALANCED_COMPACTION_BUDGET_BYTES}")
    budget = parse_byte_size(comp.get("maxActiveTranscriptBytes"))
    flush = comp.get("memoryFlush")
    if budget and isinstance(flush, dict) and flush.get("enabled") is not False:
        current = parse_byte_size(flush.get("forceFlushTranscriptBytes"))
        # F-CE2 guard: a flush threshold at/after the compaction threshold
        # means some cycles compact before the note is written. Absent, or
        # past the budget ⇒ set it below; an operator value under it stands.
        if current is None or current >= budget:
            want = int(budget * FLUSH_BEFORE_COMPACTION_RATIO)
            flush["forceFlushTranscriptBytes"] = want
            changes.append(f"compaction.memoryFlush.forceFlushTranscriptBytes={want}")

    # ── Model + thinking (lever-owned) ────────────────────────────────────
    if fast_model:
        if lever_on:
            if comp.get("model") != fast_model:
                comp["model"] = fast_model
                changes.append(f"compaction.model={fast_model}")
            if comp.get("thinkingLevel") != HOUSEKEEPING_THINKING_LEVEL:
                comp["thinkingLevel"] = HOUSEKEEPING_THINKING_LEVEL
                changes.append(f"compaction.thinkingLevel={HOUSEKEEPING_THINKING_LEVEL}")
        else:
            if comp.get("model") == fast_model:
                del comp["model"]
                changes.append("compaction.model removed (lever off)")
            if comp.get("thinkingLevel") == HOUSEKEEPING_THINKING_LEVEL:
                del comp["thinkingLevel"]
                changes.append("compaction.thinkingLevel removed (lever off)")
    return changes


def _default_fast_model(network: dict, bot_id: str) -> str | None:
    from primary_bot import resolve_roles_with_provenance  # type: ignore[import]

    fast = (resolve_roles_with_provenance(network, bot_id) or {}).get("fast") or {}
    model = fast.get("primary")
    return model if isinstance(model, str) and model.strip() else None


def apply_for_bot(
    cfg: dict,
    network: dict,
    bot_id: str,
    *,
    resolve_fast_model: Callable[[dict, str], str | None] | None = None,
    log: Callable[[str], None] = print,
) -> bool:
    """Deploy entry point: strip retired keys, then converge compaction.

    ``resolve_fast_model`` is injectable for tests; production reads the
    bot's ``fast`` role through the defaults ← pod ← bot merge. A resolver
    failure leaves model/thinking untouched (never a half-write) and still
    applies the budget. Returns True iff ``cfg`` changed.
    """
    changed = strip_retired_openclaw_keys(cfg)
    try:
        from cost_levers import HOUSEKEEPING_CHEAP_RUNG, lever_enabled  # type: ignore[import]
        lever_on = lever_enabled(network, bot_id, HOUSEKEEPING_CHEAP_RUNG)
    except Exception as exc:  # noqa: BLE001 — analyzer path unavailable: default on
        log(f"[evolve/deploy] {bot_id}: cost_levers unavailable ({exc}); housekeeping lever defaults on")
        lever_on = True
    try:
        fast_model = (resolve_fast_model or _default_fast_model)(network, bot_id)
    except Exception as exc:  # noqa: BLE001 — best-effort; budget still applies
        log(f"[evolve/deploy] {bot_id}: fast role unresolved ({type(exc).__name__}: {exc}); "
            f"compaction model left as-is")
        fast_model = None
    changes = apply_compaction_housekeeping(cfg, fast_model=fast_model, lever_on=lever_on)
    if changes:
        log(f"[evolve/deploy] {bot_id}: housekeeping — " + "; ".join(changes))
    return changed or bool(changes)
