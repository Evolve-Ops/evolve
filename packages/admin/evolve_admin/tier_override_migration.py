"""tier_override_migration — move stranded per-bot Power caps to where routing reads.

``evolve-admin models migrate-tier-overrides``. Two generations of the
``models cap`` bug left operator intent in places the gateway does not consult,
and neither failure is visible: the CLI printed a green check both times.

    generation 1 (until #4023)   value landed in {sharedDir}/{bot}/tiers.json
                                 — a file ``ModelRouter.loadTiersFile`` reads
                                 only as a fallback, i.e. never on a real pod.
    generation 2 (#4023 itself)  value landed in the canonical
                                 evolve-tiers.json, but under
                                 ``userTierOverride.dailyCap`` — a legacy key
                                 the merged ``roleCaps`` block shadows on
                                 every pod (see ``power_cap``).

Both are repaired by moving the value to ``roleCaps.power.maxPerDayPerBot``,
which is what :func:`user_tier_override.apply_power_daily_cap` now writes. This
command is that move, for values already on disk.

RULES, because a cap migration that guesses is worse than one that refuses
------------------------------------------------------------------------
* **Never overwrites a canonical value.** A bot whose canonical config already
  carries ``roleCaps.power.maxPerDayPerBot`` is left alone — even when the
  mirror disagrees. The value there may be an operator's choice or a
  materialized default and nothing on disk distinguishes them, so the command
  REPORTS the divergence with the exact one-line command to resolve it rather
  than picking a winner. Silence is the one option the Hold ruled out; a guess
  is the other.
* **It relocates, it never retunes.** Every row moves a number that is already
  written somewhere, verbatim. No row invents, rounds, or defaults a cap.
* **Idempotent.** After a successful ``--apply`` every bot reports
  ``ok`` — the canonical key is set, so the first rule now skips it.
* **The shared-dir mirror is untrusted input.** The bot holds ``add_file`` on
  ``{sharedDir}/{bot}/`` (#3565), so it can replace its own ``tiers.json``.
  A mirror-sourced value is read through the SAME ``O_NOFOLLOW`` + owner-uid
  gate ``home_chat_routes`` applies (:func:`_read_mirror_override`) and is
  REFUSED outright when the file is not operator-owned — promoting it into the
  canonical routing config would launder a forged cap past the
  ``_UNTRUSTED_DAILY_CAP_CEILING`` clamp that exists to bound exactly that.
  Canonical-sourced values need no such gate; they are already the file the
  router reads.

Dry-run by default (same shape as ``migrate-model-roles``); ``--apply`` writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: What a bot's row can say. Ordered worst-first for the summary line.
BLOCKED = "blocked"      # a mirror we may not trust carries the only value
CONFLICT = "conflict"    # canonical already set and the mirror disagrees
MOVE = "move"            # a value to relocate
OK = "ok"                # canonical already carries the routed key
NOTHING = "nothing"      # no cap set anywhere for this bot


@dataclass(frozen=True)
class BotPlan:
    """One bot's disposition. The CLI renders it; the tests assert on it."""

    bot: str
    status: str
    detail: str
    value: "int | None" = None
    source: str = ""


def _read_mirror_override(shared_dir: Path, bot: str) -> "tuple[dict, bool]":
    """Return ``(userTierOverride, trusted)`` from the shared-dir mirror.

    RAW, unlike ``home_chat_routes._read_user_tier_override``, which fills in
    defaults — this command must tell "the operator set 10" apart from "no one
    set anything", and a defaults-filled read cannot.

    Reuses that module's ``_read_tiers_file`` / ``_trusted_uids`` so the
    symlink and FIFO refusals and the owner-uid question have ONE
    implementation, not a second one written here that drifts from it.
    ``trusted`` is False only when the platform can answer the uid question
    AND the answer is "not an operator uid".
    """
    import json

    from .web.home_chat_routes import _read_tiers_file, _trusted_uids

    text, uid = _read_tiers_file(shared_dir / bot / "tiers.json")
    if text is None:
        return {}, True
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}, True
    override = (data or {}).get("userTierOverride") if isinstance(data, dict) else None
    if not isinstance(override, dict):
        return {}, True
    known = _trusted_uids()
    trusted = known is None or (uid is not None and uid in known)
    return override, trusted


def _valid_cap(raw: Any) -> "int | None":
    """The value if it is a cap this command may move, else ``None``.

    Same predicate the enforcement point uses, via the one resolver: a
    non-boolean number in [0, 100]. A junk value is not relocated — moving it
    would only reproduce it at the new key, where ``sanitize_daily_cap``
    discards it anyway.
    """
    from .power_cap import sanitize_daily_cap

    sentinel = -1
    got = sanitize_daily_cap(raw, sentinel)
    return None if got == sentinel else got


def plan_bot(
    network: dict[str, Any], shared_dir: Path, bot: str, canonical: Any,
    *, why: str = "",
) -> BotPlan:
    """Decide what (if anything) to move for one bot. Pure — no writes.

    ``canonical`` is the bot's ``json_full_config`` view (or ``None`` when it
    could not be read, with ``why`` carrying the reason). Precedence for the
    value to move mirrors the rules in the module docstring: canonical's own
    legacy key first (already the file routing reads, and not bot-forgeable
    through the #3565 ACE), then the shared-dir mirror.
    """
    from .power_cap import CAP_FIELD, POWER_ROLE, resolve_effective_power_cap

    if not isinstance(canonical, dict):
        return BotPlan(
            bot, BLOCKED,
            f"could not read this bot's model config — nothing inspected{why}",
        )

    caps = canonical.get("roleCaps")
    entry = (caps or {}).get(POWER_ROLE) if isinstance(caps, dict) else None
    # Two DIFFERENT questions, deliberately answered by different code:
    #   "has this bot been migrated?" is a PRESENCE question about one key,
    #     which only this command asks — a bot with no entry is one whose
    #     intent is still stranded, even though routing happily resolves a
    #     default for it;
    #   "what cap does routing use?" is a RESOLUTION question, and it goes
    #     through the one resolver so this command's rows cannot disagree
    #     with the enforcement point (it also accounts for the pod layer,
    #     which a hand-walk of the bot file would miss).
    migrated = isinstance(entry, dict) and CAP_FIELD in entry
    override = canonical.get("userTierOverride")
    override = override if isinstance(override, dict) else {}
    # Hand the resolver ONLY the two keys it reads. ``canonical`` is the
    # json_full_config VIEW, which carries a synthesized legacy ``tiers``
    # block alongside everything else — feeding that to the catalog merge
    # would put it through legacy-layer normalization for an answer that
    # depends on neither.
    routed = resolve_effective_power_cap(
        network, {"roleCaps": caps if isinstance(caps, dict) else {},
                  "userTierOverride": override},
    ) if migrated else None

    legacy = _valid_cap(override.get("dailyCap"))
    mirrored_block, trusted = _read_mirror_override(shared_dir, bot)
    mirrored = _valid_cap(mirrored_block.get("dailyCap"))

    if routed is not None:
        stale = [
            f"{name} says {val}"
            for name, val in (("the legacy key", legacy), ("the mirror", mirrored))
            if val is not None and val != routed
        ]
        if stale:
            return BotPlan(
                bot, CONFLICT,
                f"routing already uses {routed}/day; {' and '.join(stale)}. "
                f"Left alone — run `evolve-admin models cap {bot} <n>` to "
                f"settle it on one number.",
                value=routed,
            )
        return BotPlan(bot, OK, f"routing already uses {routed}/day", value=routed)

    if legacy is not None:
        return BotPlan(
            bot, MOVE, f"{legacy}/day", value=legacy,
            source="the canonical file's legacy userTierOverride.dailyCap",
        )
    if mirrored is not None and not trusted:
        return BotPlan(
            bot, BLOCKED,
            f"{shared_dir / bot / 'tiers.json'} is not owned by an operator "
            f"uid, so its cap of {mirrored}/day is not trusted to seed routing. "
            f"Inspect it, then set the cap deliberately with "
            f"`evolve-admin models cap {bot} <n>`.",
        )
    if mirrored is not None:
        return BotPlan(
            bot, MOVE, f"{mirrored}/day", value=mirrored,
            source=f"the shared-dir mirror ({shared_dir / bot / 'tiers.json'})",
        )
    return BotPlan(bot, NOTHING, "no per-bot cap set anywhere")


def plan(
    network: dict[str, Any], network_path: Path, bots: "list[str]",
) -> "list[BotPlan]":
    """Plan every bot. Reads only."""
    from runtime.agent_runtime import get_runtime  # type: ignore[import-not-found]

    from .config import DEFAULT_SHARED_DIR

    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    runtime = get_runtime()
    out = []
    for bot in bots:
        try:
            canonical = runtime.full_config_get(bot, network_path=str(network_path))
            why = ""
        except Exception as e:  # noqa: BLE001
            # One unreachable bot must not abort the sweep over the others —
            # but it must not read as "nothing to do" either. Carry the reason
            # onto that bot's row so the operator sees which bot and why, and
            # knows the sweep's silence about it is a gap, not a clean bill.
            canonical, why = None, f": {e}"
        out.append(plan_bot(network, shared_dir, bot, canonical, why=why))
    return out


def register_cli(models) -> None:  # noqa: ANN001 — models is a click.Group
    """Attach ``migrate-tier-overrides`` to the ``models`` command group."""
    import click
    from rich.console import Console

    from .config import load_network

    console = Console()

    @models.command("migrate-tier-overrides")
    @click.option("--bot", default=None, help="One bot (default: every member).")
    @click.option("--apply", "do_apply", is_flag=True, default=False,
                  help="Actually write (dry-run by default).")
    @click.pass_context
    def migrate_tier_overrides(
        ctx: click.Context, bot: "str | None", do_apply: bool,
    ) -> None:
        """Move per-bot Power caps to the key the gateway enforces.

        A cap set before this release landed in a file or under a key routing
        never reads, and the CLI reported success anyway — so the failure is
        invisible until you look. This finds those values and relocates them
        verbatim to ``roleCaps.power.maxPerDayPerBot``.

        Never overwrites a cap that already routes: a bot whose canonical
        config disagrees with its mirror is REPORTED, not resolved. Safe to
        re-run — after one successful pass every bot reports ok.

        \b
        Examples:
          evolve-admin models migrate-tier-overrides           # what would move
          evolve-admin models migrate-tier-overrides --apply   # move it
        """
        from .user_tier_override import (
            UserTierOverrideWriteError,
            apply_power_daily_cap,
        )

        network_path: Path = ctx.obj["network_path"]
        network = load_network(network_path)
        bots = [bot] if bot else list(network.get("members") or [])
        if not bots:
            console.print("[yellow]no bots to inspect[/yellow]")
            return

        rows = plan(network, network_path, bots)
        moved = failed = 0
        for row in rows:
            if row.status == MOVE:
                # Narrowing, not a default: ``plan`` only emits MOVE with a
                # value (the two constructors above both pass one). ``continue``
                # rather than a fallback, because a 0 default here would write
                # the "stop Power turns" sentinel onto a bot nobody asked to
                # stop.
                if row.value is None:
                    continue
                if not do_apply:
                    console.print(
                        f"[cyan]would move[/cyan] {row.bot}: {row.detail} "
                        f"from {row.source} → roleCaps.power.maxPerDayPerBot"
                    )
                    continue
                assert row.value is not None  # plan_bot sets it on every MOVE
                try:
                    apply_power_daily_cap(
                        network, network_path, row.bot, row.value,
                    )
                except UserTierOverrideWriteError as e:
                    failed += 1
                    console.print(f"[red]✗[/red] {row.bot}: {e}")
                    continue
                moved += 1
                console.print(
                    f"[green]✓[/green] {row.bot}: moved {row.detail} from "
                    f"{row.source} → roleCaps.power.maxPerDayPerBot"
                )
            elif row.status in (CONFLICT, BLOCKED):
                console.print(f"[yellow]![/yellow] {row.bot}: {row.detail}")
            else:
                console.print(f"[dim]·[/dim] {row.bot}: {row.detail}")

        pending = sum(1 for r in rows if r.status == MOVE)
        needs_you = sum(1 for r in rows if r.status in (CONFLICT, BLOCKED))
        if not do_apply and pending:
            console.print(
                f"\n{pending} to move. Re-run with [bold]--apply[/bold] to "
                f"write them; no cap value changes, they only move."
            )
        elif do_apply:
            console.print(f"\nmoved {moved}" + (f", {failed} failed" if failed else ""))
        if needs_you:
            console.print(
                f"{needs_you} bot{'s' if needs_you > 1 else ''} "
                f"need{'' if needs_you > 1 else 's'} a decision — "
                f"see the ! rows above."
            )
        if failed:
            ctx.exit(1)
