"""Shared scaffolding for signal-consuming guardian generators.

Generators that follow the pattern "subscribe to one Signal type from one
producer, fan out to per-finding Proposals" all share the same boilerplate:
iterate the firing-Signal index for this bot, filter by type, swallow the
``signals`` ImportError when the package isn't loaded. This module is that
boilerplate, lifted so each generator's ``observe()`` can focus on its
per-signal action logic.

Per-coach differences — dismissal lookup, extra I/O like reading
``jobs.json``, ``config_intent`` gates — stay inline in the calling
``observe()``. They're not universal and pulling them through the helper
would either bloat its signature or hide per-coach safety logic behind
keyword args. The helper covers exactly the duplicated lines, no more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator


def iter_firing_signals(
    shared_dir: Path | None,
    bot_id: str,
    producer: str,
    signal_type: str,
) -> Iterator[Any]:
    """Yield firing Signals of ``(producer, signal_type)`` for one bot.

    Returns empty when ``shared_dir`` is None or the ``signals`` package
    can't be imported (e.g. the analyzer package is loaded standalone in
    a unit test where ``signals`` isn't importable). Callers should
    not guard the import themselves — this helper already does.
    """
    if shared_dir is None:
        return
    try:
        from signals import store as signals_store
    except ImportError:
        return
    for sig in signals_store.iter_active(
        shared_dir,
        producer=producer,
        bot_id=bot_id,
        state="firing",
    ):
        if getattr(sig, "type", None) == signal_type:
            yield sig
