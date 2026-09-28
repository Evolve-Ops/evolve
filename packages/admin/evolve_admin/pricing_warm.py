"""``evolve-admin pricing warm`` — fill the pricing cache and say, per model, what happened.

Brief: internal/dispatch/done/board-sheet-renders-time-text-and-price-is-warmed.md.
The Board disables every LLM action whose fast-rung model has no price (D-PA1:
never a tap without a price). On a pod whose discovery sweep had never run, that
rule read as "nothing works" — the cache was simply cold. This is the one command
that warms it, and ``deploy`` runs it best-effort.

THE RULE (D-CS2): a model whose price cannot be fetched stays ABSENT from the
cache — never ``0``, never a guess, never a stale value silently reused. A wrong
number flows into every cost line, receipt and breaker, and nothing downstream
can tell it from a measured one; an absent one disables the action and says so.

Exit status: non-zero only when the price source could not be reached at all
(or the cache could not be written) — "no price exists for this model" is exit 0
with the model named, so the two stay distinguishable.

Registration runs at cli.py module load, so the analyzer imports live inside
the functions (same rule as ``model_swap_cli``).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

#: Every role a bot's tier chain can name — the models ``warm`` reports on.
ROLES = ("fast", "standard", "power")


def _rates(cache: dict | None, model: str) -> tuple[float, float] | None:
    """(input, output) $/token for ``provider/model``, or None — BOTH or nothing."""
    from model_pricing import lookup_price
    provider, _, bare = model.partition("/")
    rec = lookup_price(cache, provider, bare) if bare else None
    if not rec or rec.get("input_cost_per_token") is None or rec.get("output_cost_per_token") is None:
        return None
    return float(rec["input_cost_per_token"]), float(rec["output_cost_per_token"])


def fast_rung_model(network: dict[str, Any], bot_id: str) -> str | None:
    """The bot's fast-rung model (first of the chain), or None when unset."""
    from primary_bot import bot_tier_models
    chain = bot_tier_models(network, bot_id, "fast")
    return chain[0] if chain and "/" in chain[0] else None


def unpriced_fast_rungs(network: dict[str, Any], members: list[str], cache: dict | None) -> list[tuple[str, str | None]]:
    """``(bot, model)`` for every bot whose fast rung has no price; model None = no fast rung."""
    out = []
    for bot in members:
        model = fast_rung_model(network, bot)
        if model is None or _rates(cache, model) is None:
            out.append((bot, model))
    return out


def tier_chain_models(network: dict[str, Any], members: list[str]) -> list[str]:
    """Every distinct ``provider/model`` in every member's tier chains, in first-seen order."""
    from primary_bot import bot_tier_models
    seen: dict[str, None] = {}
    for bot in members:
        for role in ROLES:
            for m in bot_tier_models(network, bot, role):
                if "/" in m:
                    seen.setdefault(m, None)
    return list(seen)


def warm(shared_dir: Path, network: dict[str, Any], members: list[str],
         fetcher: Callable[[str], dict] | None = None) -> tuple[list[str], int]:
    """Fetch the catalog, write it, and return ``(lines, exit_code)`` — one line per model."""
    import model_pricing as mp
    models = tier_chain_models(network, members)
    before = mp.read_pricing_cache(shared_dir)
    doc = mp.fetch_pricing_catalog(refreshed_at=datetime.now(timezone.utc).isoformat(), fetcher=fetcher)
    degraded = doc.get("degraded") or []
    why_down = "; ".join(f"{d.get('source')}: {d.get('reason')}" for d in degraded)
    if not doc.get("models"):
        lines = [f"{m}: could not fetch — price source unreachable ({why_down or 'empty catalog'})" for m in models]
        return lines + [f"could not reach the price source; cache left as it was ({why_down or 'empty catalog'})"], 1
    try:
        path = mp.write_pricing_cache(Path(shared_dir), doc)
        _hand_to_shared_dir_owner(path)
    except OSError as exc:
        return [f"could not write the pricing cache: {exc}"], 1
    lines = []
    for m in models:
        new, old = _rates(doc, m), _rates(before, m)
        if new is None:
            reason = "the price source has no input+output price for it"
            if why_down:
                reason += f" (partly unreachable: {why_down})"
            lines.append(f"{m}: could not fetch — {reason}")
        else:
            lines.append(f"{m}: {'unchanged' if new == old else 'filled'}")
    return lines, 0


def _hand_to_shared_dir_owner(path: Path) -> None:
    """``sudo`` runs leave a root-owned cache the evolve daemon's sweep can never
    replace (sticky ``{shared_dir}``) — give it to whoever owns the dir."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        st = path.parent.stat()
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o644)  # a public price catalog; readers are every service account


def warm_best_effort(network_path: Path, echo: Callable[[str], Any]) -> None:
    """``deploy``'s hook: warm, print, and NEVER fail the deploy — ``health`` is the gate."""
    try:
        from evolve_config import get_shared_dir

        from .config import load_network
        network = load_network(network_path)
        lines, code = warm(Path(get_shared_dir(network)), network, network.get("members", []))
        for line in lines:
            echo(f"  pricing: {line}")
        if code:
            echo("  pricing: warm did not complete — deploy continues; `evolve-admin health` will name what is unpriced")
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        echo(f"  pricing: warm skipped ({exc}) — deploy continues")


def register_cli(main) -> None:  # noqa: ANN001 — click.Group
    """Attach ``pricing warm`` to the top-level group."""
    import sys

    import click

    @main.group("pricing")
    def pricing() -> None:
        """Model pricing cache."""

    @pricing.command("warm")
    @click.pass_context
    def warm_cmd(ctx: click.Context) -> None:
        """Fill the pricing cache for every model in every bot's tier chain."""
        from evolve_config import get_shared_dir

        from .config import load_network
        network = load_network(ctx.obj["network_path"])
        lines, code = warm(Path(get_shared_dir(network)), network, network.get("members", []))
        for line in lines:
            click.echo(line)
        sys.exit(code)
