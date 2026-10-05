"""pod_default_role — the pod-wide default conversation role writer (D-DT4).

internal/decision-default-tier-power-2026-09-23.md. ``network.json::models.
defaultRole`` is the pod-level fallback a bot's ``userTierOverride.
defaultTier`` of "auto"/absent resolves to
(``ModelRouter._resolveOperatorDefaultRole`` /
``primary_bot.resolve_bot_default_conversation_role``). ONE writer for both
the CLI (``evolve-admin models default-role``) and the
``PUT /api/admin/config/pod/models/default-role`` endpoint, so the validation
(``fast``/``standard``/``power`` only — ``max`` is pull-only, spec §max #3)
cannot drift between the two surfaces.

Kept out of ``cli.py`` / ``routes_admin_config.py`` on purpose: both are
frozen no-growth hot files (4.1a); a write this small belongs in its own
module, same shape as ``tier_override_migration.register_cli``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

VALID_ROLES = frozenset({"fast", "standard", "power"})


class PodDefaultRoleError(ValueError):
    """The requested role is not a valid pod-wide default."""


def validate_default_role(role: Any) -> str:
    """Raise :class:`PodDefaultRoleError` unless ``role`` is fast/standard/power."""
    if not isinstance(role, str) or role not in VALID_ROLES:
        extra = (
            " ('max' is pull-only: the pod default cannot be max)"
            if role == "max" else ""
        )
        raise PodDefaultRoleError(
            f"defaultRole must be one of {sorted(VALID_ROLES)}{extra}"
        )
    return role


def set_pod_default_role(network_path: Path, role: Any) -> str:
    """Validate and write ``network.json::models.defaultRole``. Returns it.

    Splices only ``models.defaultRole`` (``evolve_config._patch_network_json``)
    so sibling keys (``models.rungs``/``roles``/``roleCaps``/``embedding``)
    are untouched.
    """
    role = validate_default_role(role)
    from evolve_config import _patch_network_json  # type: ignore

    _patch_network_json(network_path, ["models", "defaultRole"], role)
    return role


def register_cli(models) -> None:  # noqa: ANN001 — models is a click.Group
    """Attach ``default-role`` to the ``models`` command group."""
    import click

    @models.command("default-role")
    @click.argument("role", metavar="ROLE")
    @click.pass_context
    def models_default_role(ctx: click.Context, role: str) -> None:
        """Set the pod-wide default conversation role (D-DT4).

        ROLE is fast, standard, or power — the role a bot's conversations
        use when its own userTierOverride.defaultTier is "auto" (the
        product default) or absent. Every bot moves to this default with no
        per-bot edit; a bot with an explicit defaultTier keeps it. `max` is
        rejected — it is pull-only (spec §max #3).

        Writes network.json::models.defaultRole. POD-level by construction:
        a per-bot evolve-tiers.json copy is ignored by the router
        (mergeModelCatalog strips it, logging once) — there is no per-bot
        writer for it. Set a per-bot default via `evolve-admin models
        user-tier-control` / the "Conversations" picker instead.

        \b
        Examples:
          evolve-admin models default-role power
          evolve-admin models default-role standard
        """
        from rich.console import Console

        console = Console()
        network_path: Path = ctx.obj["network_path"]
        try:
            set_pod_default_role(network_path, role)
        except PodDefaultRoleError as e:
            console.print(f"[red]{e}[/red]")
            raise click.Abort()
        console.print(
            f"[green]✓[/green] models.defaultRole → [bold]{role}[/bold] "
            f"— every bot on 'auto' (the default) now resolves conversations "
            f"here; a bot with its own explicit default is unaffected."
        )
