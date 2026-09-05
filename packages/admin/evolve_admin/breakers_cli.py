"""evolve-admin breaker … — circuit-breaker operator surface.

Spec: internal/spec-circuit-breakers-2026-05-21.md (Phase 2 manual trip
primitive; Phase 5 auto-trip arming). The group lives in its own module
(rather than inline in ``cli.py``, which is line-count capped) and is
registered onto the top-level ``main`` group by ``cli.py`` via
``main.add_command(breaker_group)`` — same shape as ``release_cli.py``.

Manual controls (Phase 2/3): ``trip`` / ``reset`` / ``extend`` /
``status`` operate on breaker state in ``{shared_dir}/breakers/`` and
synchronously enforce via ``breakers_enforce``.

Arming toggle (§5.2): ``arm`` / ``disarm`` record
``breakers.auto_trip_enabled`` in network.json — the flag the
breakers-runner daemon reads each cycle. Since the arming PR the code
default is ARMED (see ``breakers.runner.read_auto_trip_enabled``), so
``disarm`` is the operator opt-out that returns the runner to
observe-only mode, and ``arm`` clears that opt-out by recording an
explicit ``true``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from .config import load_network, save_network

console = Console()


def _breakers_store():
    """Lazy import of breakers.store."""
    from breakers import store as _store  # noqa: WPS433
    return _store


@click.group("breaker")
def breaker_group() -> None:
    """Manage per-bot and pod-wide circuit breakers.

    Two breaker types:

      cost — blocks background activity (heartbeats, crons, auto agents).
             User messaging through normal channels still works.
      full — full halt; bot gateway is taken down. Equivalent to per-bot
             pause-all. Reset requires admin context.

    Use scope=pod to operate pod-wide. See `breaker trip --help` for the
    duration format ("1h", "24h", "7d", "indefinite").

    `breaker arm` / `breaker disarm` toggle the auto-trip flag the
    detector runner honors (armed by default); `breaker status` shows
    the current arming state alongside active trips.
    """


def _format_breaker_row(rec, *, now=None) -> str:
    store = _breakers_store()
    state = "EXPIRED" if store.is_expired(rec, now=now) else "tripped"
    color = "[yellow]" if state == "EXPIRED" else "[red]"
    expires = rec.expires_at or "indefinite"
    return (
        f"  {color}{rec.bot_id}/{rec.type}[/]  {state}  "
        f"since {rec.tripped_at}  expires {expires}  "
        f"by {rec.initiated_by}  reason={rec.reason!r}"
    )


def _shared_dir_from_network(network: dict) -> Path:
    """Resolve the pod shared dir from network config (platform-keyed
    canonical default when network.json doesn't declare a sharedDir
    override)."""
    from evolve_config import CANONICAL_SHARED_DIR
    raw = (network or {}).get("sharedDir")
    return Path(raw) if raw else CANONICAL_SHARED_DIR


def _breakers_shared_dir(ctx: click.Context) -> Path:
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    return _shared_dir_from_network(network)


@breaker_group.command("trip")
@click.argument("scope")
@click.argument("breaker_type", type=click.Choice(["cost", "full"]))
@click.option("--duration", "-d", default="24h",
              help='Trip duration: "1h"/"4h"/"24h"/"7d"/"30m" or "indefinite". '
                   "Default 24h.")
@click.option("--reason", "-r", default="manual trip", help="Why this trip.")
@click.option("--initiated-by", default="cli",
              help='Audit-log actor ID (e.g. "admin:pod_admin"). Defaults to "cli".')
@click.option("--motivating-signal", multiple=True,
              help="Signal ID this trip responds to (repeatable).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def breaker_trip_cmd(
    ctx: click.Context, scope: str, breaker_type: str, duration: str,
    reason: str, initiated_by: str,
    motivating_signal: tuple[str, ...], as_json: bool,
) -> None:
    """Trip a breaker. SCOPE is a bot id or "pod" (pod-wide)."""
    store = _breakers_store()
    try:
        dur = store.parse_duration(duration)
    except ValueError as e:
        console.print(f"[red]Bad --duration: {e}[/]")
        sys.exit(2)

    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _shared_dir_from_network(network)
    try:
        rec = store.trip(
            shared_dir=shared_dir, scope=scope, breaker_type=breaker_type,
            duration=dur, initiated_by=initiated_by, reason=reason,
            motivating_signals=list(motivating_signal),
        )
    except ValueError as e:
        console.print(f"[red]Trip rejected: {e}[/]")
        sys.exit(2)

    # Phase 3a: synchronously enforce. For type=full this boots out the
    # affected gateway(s); for type=cost (partial Phase 3b) this stashes
    # and removes agents.defaults.heartbeat.every from openclaw.json
    # and kickstarts the gateway. The stash lives in
    # ``{shared_dir}/breakers/<bot>/heartbeat-stash.json`` and is
    # restored on the matching reset.
    from . import breakers_enforce
    try:
        enforce_result = breakers_enforce.enforce_trip(
            scope=scope, breaker_type=breaker_type, network=network,
            shared_dir=shared_dir,
            reason=rec.reason, expires_at_iso=rec.expires_at,
        )
    except ValueError as e:
        # Unknown bot — file was written but we can't enforce. Surface
        # the inconsistency to the operator; don't silently swallow.
        console.print(f"[red]State written, enforce FAILED: {e}[/]")
        console.print("  [yellow]The breaker file exists but the gateway "
                      "was not bootout. Reset the breaker, or fix the "
                      "scope.[/]")
        sys.exit(3)

    if as_json:
        print(json.dumps({
            "trip": rec.to_json(),
            "enforce": enforce_result.to_dict(),
        }, indent=2, sort_keys=True))
        if not enforce_result.ok:
            sys.exit(1)
        return

    console.print(f"[red]●[/] tripped: {scope}/{breaker_type} "
                  f"(trip_id={rec.trip_id[:8]}, "
                  f"expires={rec.expires_at or 'indefinite'})")
    console.print(f"  reason: {rec.reason}")

    if enforce_result.no_op:
        console.print(
            f"  [dim]{enforce_result.no_op_reason}.[/]"
        )
    else:
        _print_enforce_result(enforce_result)
    if not enforce_result.ok:
        sys.exit(1)


@breaker_group.command("reset")
@click.argument("scope")
@click.argument("breaker_type", type=click.Choice(["cost", "full"]))
@click.option("--reason", "-r", default="manual reset")
@click.option("--initiated-by", default="cli")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def breaker_reset_cmd(
    ctx: click.Context, scope: str, breaker_type: str, reason: str,
    initiated_by: str, as_json: bool,
) -> None:
    """Reset (clear) a breaker. No-op if not tripped."""
    store = _breakers_store()
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    shared_dir = _shared_dir_from_network(network)
    try:
        prior = store.reset(
            shared_dir=shared_dir, scope=scope, breaker_type=breaker_type,
            initiated_by=initiated_by, reason=reason,
        )
    except ValueError as e:
        console.print(f"[red]Reset rejected: {e}[/]")
        sys.exit(2)

    # Phase 3a: bootstrap the gateway(s) (L2) or restore heartbeat + clear
    # the spend-cap flag (L1). Cost enforcement runs even with no tripped
    # breaker: the flag is a separate store ModelRouter reads directly.
    enforce_result = None
    if prior is not None or breaker_type == "cost":
        from . import breakers_enforce
        try:
            enforce_result = breakers_enforce.enforce_reset(
                scope=scope, breaker_type=breaker_type, network=network,
                shared_dir=shared_dir,
            )
        except ValueError as e:
            console.print(f"[red]State cleared, restart FAILED: {e}[/]")
            console.print("  [yellow]The breaker file is deleted but the "
                          "gateway may still be down. Run 'evolve-admin "
                          "restart-gateways' or fix the scope.[/]")
            sys.exit(3)

    if as_json:
        print(json.dumps({
            "reset": prior.to_json() if prior else None,
            "enforce": enforce_result.to_dict() if enforce_result else None,
        }, indent=2, sort_keys=True))
        if enforce_result and not enforce_result.ok:
            sys.exit(1)
        return
    if prior is None:
        console.print(f"[green]✓[/] {scope}/{breaker_type} not tripped — nothing to reset")
    else:
        console.print(f"[green]●[/] reset: {scope}/{breaker_type} "
                      f"(was trip_id={prior.trip_id[:8]})")
    if enforce_result and enforce_result.no_op:
        console.print(f"  [dim]{enforce_result.no_op_reason}.[/]")
    elif enforce_result:
        _print_enforce_result(enforce_result)
    if enforce_result and not enforce_result.ok:
        sys.exit(1)


@breaker_group.command("extend")
@click.argument("scope")
@click.argument("breaker_type", type=click.Choice(["cost", "full"]))
@click.option("--by", "by_duration", required=True,
              help='Extend by this much: "1h", "24h", "7d", etc.')
@click.option("--initiated-by", default="cli")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def breaker_extend_cmd(
    ctx: click.Context, scope: str, breaker_type: str, by_duration: str,
    initiated_by: str, as_json: bool,
) -> None:
    """Push a tripped breaker's expiry further out."""
    store = _breakers_store()
    try:
        delta = store.parse_duration(by_duration)
    except ValueError as e:
        console.print(f"[red]Bad --by: {e}[/]")
        sys.exit(2)
    if delta is None:
        console.print("[red]--by cannot be 'indefinite'; reset and re-trip instead.[/]")
        sys.exit(2)

    shared_dir = _breakers_shared_dir(ctx)
    try:
        rec = store.extend(
            shared_dir=shared_dir, scope=scope, breaker_type=breaker_type,
            additional=delta, initiated_by=initiated_by,
        )
    except ValueError as e:
        console.print(f"[red]Extend rejected: {e}[/]")
        sys.exit(2)

    if as_json:
        print(json.dumps(
            {"extended": rec.to_json() if rec else None}, indent=2, sort_keys=True,
        ))
        return
    if rec is None:
        console.print(f"[yellow]No active trip on {scope}/{breaker_type}.[/]")
        sys.exit(1)
    else:
        console.print(f"[green]●[/] extended {scope}/{breaker_type} → "
                      f"expires {rec.expires_at}")


# ── §5.2 arming toggle — breakers.auto_trip_enabled ──────────────────────────


def _set_auto_trip(ctx: click.Context, enabled: bool) -> None:
    """Record ``breakers.auto_trip_enabled`` in network.json (load-modify-
    save; other ``breakers.*`` keys are preserved)."""
    network_path: Path = ctx.obj["network_path"]
    network = load_network(network_path)
    network.setdefault("breakers", {})["auto_trip_enabled"] = enabled
    save_network(network, network_path)


def _print_arming_state(network: dict) -> None:
    from breakers.runner import read_auto_trip_enabled
    armed = read_auto_trip_enabled(network)
    explicit = isinstance(network.get("breakers"), dict) and \
        "auto_trip_enabled" in network["breakers"]
    origin = "explicit in network.json" if explicit else "code default"
    if armed:
        console.print(f"[red]Auto-trip: ARMED[/] ({origin}) — the detector "
                      "runner trips cost breakers on its own.")
    else:
        console.print(f"[green]Auto-trip: DISARMED[/] ({origin}) — "
                      "observe-only; decisions are logged, nothing trips.")


@breaker_group.command("arm")
@click.pass_context
def breaker_arm_cmd(ctx: click.Context) -> None:
    """Arm auto-trip: the detector runner acts on trip decisions.

    Records ``breakers.auto_trip_enabled: true`` in network.json. Armed
    is also the code default since the §5.2 arming PR, so this mainly
    clears a prior ``disarm``; recording the explicit value makes the
    operator's decision visible in the config either way. Takes effect
    on the runner's next 10-minute cycle — no restart needed.
    """
    _set_auto_trip(ctx, True)
    console.print("[green]✓[/] armed — breakers.auto_trip_enabled = true")
    _print_arming_state(load_network(ctx.obj["network_path"]))


@breaker_group.command("disarm")
@click.pass_context
def breaker_disarm_cmd(ctx: click.Context) -> None:
    """Disarm auto-trip: the detector runner becomes observe-only.

    Records ``breakers.auto_trip_enabled: false`` in network.json — the
    operator opt-out from the armed-by-default §5.2 posture. The
    detector keeps running and logging would-trip decisions to
    ``{shared_dir}/breakers/runner-log/``; it just stops acting on
    them. Manual ``breaker trip``/``reset`` are unaffected. Takes
    effect on the runner's next 10-minute cycle — no restart needed.
    """
    _set_auto_trip(ctx, False)
    console.print("[yellow]✓[/] disarmed — breakers.auto_trip_enabled = false")
    _print_arming_state(load_network(ctx.obj["network_path"]))


@breaker_group.command("status")
@click.option("--all", "show_all", is_flag=True,
              help="Include expired (not-yet-reaped) trips.")
@click.option("--audit-days", type=int, default=7,
              help="Show audit-log entries from the last N days (0 = skip).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def breaker_status_cmd(
    ctx: click.Context, show_all: bool, audit_days: int, as_json: bool,
) -> None:
    """Print arming state, currently-tripped breakers + recent audit history."""
    from breakers.runner import read_auto_trip_enabled
    store = _breakers_store()
    network = load_network(ctx.obj["network_path"])
    shared_dir = _shared_dir_from_network(network)
    armed = read_auto_trip_enabled(network)
    trips = store.list_all(shared_dir) if show_all else store.list_active(shared_dir)
    audit = store.read_audit_log(shared_dir, days=audit_days) if audit_days > 0 else []

    if as_json:
        print(json.dumps({
            "auto_trip_enabled": armed,
            "trips": [r.to_json() for r in trips],
            "audit": audit,
        }, indent=2, sort_keys=True))
        return

    _print_arming_state(network)

    if not trips:
        console.print("[green]No active circuit breakers.[/]")
    else:
        console.print(f"[bold]{len(trips)} active breaker(s):[/]")
        for r in trips:
            console.print(_format_breaker_row(r))

    if audit:
        console.print(f"\n[bold]Recent audit ({len(audit)} entries, last {audit_days}d):[/]")
        for entry in audit[:20]:
            action = entry.get("action", "?")
            ts = entry.get("timestamp", "?")
            scope = entry.get("scope", "?")
            etype = entry.get("type", "?")
            by = entry.get("initiated_by", "?")
            console.print(f"  {ts}  {action:7s}  {scope}/{etype}  by {by}")


def _print_enforce_result(result) -> None:
    """Pretty-print a breakers_enforce.EnforceResult."""
    if result.no_op:
        console.print(f"  [dim]{result.no_op_reason}.[/]")
        return
    header = "[green]✓[/]" if result.ok else "[red]✗[/]"
    suffix = " (DRY-RUN)" if result.dry_run else ""
    elapsed = f"{result.elapsed_ms} ms" if result.elapsed_ms is not None else "?"
    console.print(f"  {header} {result.action} {result.breaker_type}"
                  f"{suffix} — {elapsed}, {len(result.per_bot)} bot(s)")
    for r in result.per_bot:
        sym = "[green]✓[/]" if r.ok else "[red]✗[/]"
        tag = " (skipped: " + r.skip_reason + ")" if r.skipped else ""
        line = f"    {sym} {r.bot_id}  rc={r.rc}{tag}"
        if r.stderr:
            line += f"   stderr={r.stderr!r}"
        console.print(line)
