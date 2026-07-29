"""list_signals — read recent Signal envelopes from {shared_dir}/signals/.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §5.1.
Backing store: ``analyzer.signals.store`` (spec
docs/spec-alerts-signal-store-2026-05-07.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any



# Spec §5.1: ≤20 default, ≤100 hard cap. Caps applied here, not in the
# HTTP layer, so the MCP bridge and direct callers inherit the same
# guardrail.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


# Valid state values per the Signal schema. Out-of-range strings are
# coerced to None (= no filter) rather than raising — the bot is more
# useful when it returns "no firing signals" than when it 400s the LLM.
_VALID_STATES = {"firing", "snoozed", "resolved", "dismissed"}


def list_signals(
    shared_dir: Path,
    *,
    state: str | None = "firing",
    producer: str | None = None,
    bot_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return recent Signals filtered by state / producer / bot_id.

    Default state is ``"firing"`` because the bot's most common
    "show me alerts" question wants live, not historical, signals.
    Pass ``state="all"`` (or ``state=None``) to fan out across the
    full lifecycle — firing + snoozed (active) + archived (resolved +
    dismissed).

    Returns ``{"signals": [{...}, ...], "count": N, "filters": {...}}``.
    Each signal is the dict produced by ``Signal.to_dict()`` —
    JSON-serializable as-is.

    Validation: an unrecognized ``state`` value falls back to the
    default (``"firing"``), not to "no filter" — silent broadening
    on an LLM typo would be a worse failure mode than over-narrowing.
    ``filters.state`` echoes what was actually applied so the caller
    can detect coercion.
    """
    # Imported lazily so test runs without the analyzer dir on path
    # still load this module (the test's failure mode becomes a clear
    # "iter_signals returned []" rather than an import error at module
    # import time, which would block every other pod_state function too).
    from signals import store as _store  # type: ignore[import-not-found]

    norm_state: str | None
    if state in (None, "all", "*"):
        norm_state = None  # = fan out across all subdirs (see below)
    elif state in _VALID_STATES:
        norm_state = state
    else:
        # Invalid filter — coerce to the default ("firing") not to None.
        # A typoed `state=fring` should NOT silently broaden to "every
        # state ever recorded"; that's the silent-success-but-wrong
        # shape the second-pass review on Bundle 3 caught. Caller can
        # detect coercion by comparing ``filters.state`` to the value
        # they passed in.
        norm_state = "firing"

    capped_limit = max(1, min(int(limit) if limit else DEFAULT_LIMIT, MAX_LIMIT))

    # State → subdirs to scan. The signal store splits subdirs by
    # lifecycle: firing/, snoozed/, archived/ (= resolved+dismissed).
    # ``iter_active`` reads firing+snoozed; for resolved/dismissed we
    # use ``iter_signals`` with the archived subdir and post-filter.
    matches: list[dict[str, Any]] = []
    if norm_state in ("firing", "snoozed"):
        for sig in _store.iter_active(
            shared_dir,
            producer=producer,
            bot_id=bot_id,
            state=norm_state,
        ):
            matches.append(sig.to_dict())
            if len(matches) >= capped_limit:
                break
    elif norm_state in ("resolved", "dismissed"):
        for sig in _store.iter_signals(shared_dir, subdirs=("archived",)):
            if sig.state != norm_state:
                continue
            if producer is not None and sig.producer != producer:
                continue
            if bot_id is not None and sig.bot_id != bot_id:
                continue
            matches.append(sig.to_dict())
            if len(matches) >= capped_limit:
                break
    else:
        # norm_state is None — fan out across firing+snoozed (via
        # iter_active) AND archived (via iter_signals). Without the
        # second pass, ``state="all"`` would silently miss every
        # resolved/dismissed signal — a bug caught in second-pass
        # review.
        for sig in _store.iter_active(
            shared_dir,
            producer=producer,
            bot_id=bot_id,
        ):
            matches.append(sig.to_dict())
            if len(matches) >= capped_limit:
                break
        if len(matches) < capped_limit:
            for sig in _store.iter_signals(shared_dir, subdirs=("archived",)):
                if producer is not None and sig.producer != producer:
                    continue
                if bot_id is not None and sig.bot_id != bot_id:
                    continue
                matches.append(sig.to_dict())
                if len(matches) >= capped_limit:
                    break

    return {
        "signals": matches,
        "count": len(matches),
        "filters": {
            "state": norm_state,
            "producer": producer,
            "bot_id": bot_id,
            "limit": capped_limit,
        },
    }
