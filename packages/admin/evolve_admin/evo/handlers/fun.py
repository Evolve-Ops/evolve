"""``evo fun`` — surface a whimsy item.

Pulls a random entry from the whimsy seed pool that the BetterEngine
already curates — riddles, dad jokes, fun facts, historical trivia,
quotes, brain teasers, word-of-the-day. Same content the engine
surfaces in the dashboard's whimsy rec slot, accessible in chat
without waiting for an engine cycle.

Self-contained: doesn't depend on a populated recommendations.json or
a running BetterEngine. Worst case (seed dir missing entirely) we
return a benign fallback rather than an error.

Scope: per-bot. Same flavor on every bot — no pod-wide variant since
fun isn't aggregable.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from platform_profile import get_profile
from ..identity import Role
from ._shared import speak


_FORMATTERS = {
    "riddle": lambda i: (
        f"**Riddle** 🧩\n\n{i.get('content', '')}\n\n"
        f"_Answer: {i.get('answer', '(no answer recorded)')}_"
    ),
    "brain_teaser": lambda i: (
        f"**Brain teaser** 🧠\n\n{i.get('content', '')}"
        + (f"\n\n_Answer: {i['answer']}_" if i.get("answer") else "")
    ),
    "dad_joke": lambda i: f"**Dad joke** 🙄\n\n{i.get('content', '')}",
    "fun_fact": lambda i: f"**Fun fact** ✨\n\n{i.get('content', '')}",
    "historical_trivia": lambda i: (
        f"**On this kind of day** 📜\n\n{i.get('content', '')}"
    ),
    "quote": lambda i: f"**Quote** 💬\n\n{i.get('content', '')}",
    "word_of_the_day": lambda i: (
        f"**Word of the day** 📖\n\n{i.get('content', '')}"
    ),
}


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    items = _load_whimsy_pool()
    if not items:
        return speak("fun", (
            "**Fun** 🪄\n\nWhimsy pool unreachable. Ask your pod admin to "
            "check the better_engine whimsy seeds."
        ), role)

    pick = random.choice(items)
    body = _format(pick)
    return speak("fun", body, role)


# ─────────────────────────────────────────────────────────────────────────────
# Pool loading
# ─────────────────────────────────────────────────────────────────────────────


def _load_whimsy_pool() -> list[dict]:
    """Aggregate every entry across the per-flavor whimsy seed files.

    Returns ``[]`` on directory-not-found or all-files-empty. Individual
    malformed files are skipped silently.
    """
    seed_dir = _seed_dir()
    if seed_dir is None or not seed_dir.exists():
        return []
    items: list[dict] = []
    for path in sorted(seed_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and entry.get("content"):
                    items.append(entry)
    return items


def _seed_dir() -> Path | None:
    """Locate the whimsy seed directory across both deploy and dev paths."""
    candidates = [
        # Dev / worktree
        Path(__file__).resolve().parents[2] / "better_engine" / "whimsy",
        # Deploy checkout (same shape — just a different repo root; platform-keyed)
        Path(get_profile().deploy_checkout_default)
        / "packages/admin/evolve_admin/better_engine/whimsy",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _format(item: dict) -> str:
    """Render one whimsy entry. Unknown types fall back to a generic
    'fun' framing so new seed flavors don't break the handler."""
    kind = str(item.get("type") or "").lower()
    formatter = _FORMATTERS.get(kind)
    if formatter is not None:
        return formatter(item)
    content = str(item.get("content") or "").strip()
    if not content:
        return "**Fun** 🪄\n\n(no content)"
    return f"**Fun** ✨\n\n{content}"
