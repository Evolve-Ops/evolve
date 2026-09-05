"""evolve-admin board … — the operator surface for the Board's phone link.

Design: ``internal/design-pa-mobile-board-2026-08-31.md`` D-MB2, which
specified ``sudo evolve-admin board token <bot_id>`` as the mint command.
Slice 1 shipped the implementation as a module entry point
(``python3 -m evolve_admin.board_store mint``) because ``cli.py`` is
line-count capped; this group is the wrapper that closes the gap, living in
its own module and registered onto ``main`` by ``cli.py`` via
``main.add_command(board_group)`` — the same shape as ``release_cli``.

Every command here is thin: ``board_store`` stays the single implementation
of mint and revoke, so there is one place a token can be created.

    sudo evolve-admin board token <bot_id>    mint (or rotate); prints the
                                              phone URL — and a QR code of it
                                              — once
    sudo evolve-admin board revoke <bot_id>   kill the token; every board
                                              request 401s until re-minted

The URL carries a 43-character token, which nobody is going to retype on a
phone, so ``token`` also draws it as a QR code the phone's camera reads off
the terminal (``qr_terminal``). ``--no-qr`` drops back to the URL alone for
scripts and transcripts. Neither the URL nor the code is ever logged: both
go to this stdout, once, exactly where the URL already went.
"""
from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from .config import DEFAULT_NETWORK_CONFIG, load_network

console = Console()


@click.group("board")
def board_group() -> None:
    """Board phone link: mint or revoke a bot's board token."""


def _network_path(ctx: click.Context) -> Path:
    obj = ctx.obj or {}
    return obj.get("network_path") or DEFAULT_NETWORK_CONFIG


def _shared_dir(network_path: Path) -> Path:
    from .config import CANONICAL_SHARED_DIR
    return Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))


def _board_base_url(network_path: Path) -> str:
    """The origin an operator should type on the phone.

    Preference order, most-specific first:
      1. the tailnet address the board listener actually binds — the only
         address a phone can reach, and the one the listener resolves;
      2. ``adminBaseUrl`` from network.json, for a pod fronted by
         ``tailscale serve`` or another reverse proxy;
      3. nothing — print the path alone and say so, rather than inventing a
         host that will not resolve on the operator's phone.
    """
    try:
        network = load_network(network_path)
    except Exception:
        network = {}
    from .config import resolve_admin_port
    from .web.board_listener import (
        listener_config, resolve_bind_address, resolve_listener_port,
    )
    enabled, _ = listener_config(network)
    if enabled:
        try:
            address = resolve_bind_address().address
            # The port comes from the listener's own resolver, not a second
            # copy of it: the link is shown once, so a port that disagrees
            # with what the daemon bound is unrecoverable.
            port = resolve_listener_port(
                network, admin_port=resolve_admin_port(network))
            return f"http://{address}:{port}"
        except Exception as exc:  # noqa: BLE001
            # The listener is configured but Tailscale cannot say where it
            # would bind. Say so — silently printing an adminBaseUrl instead
            # would look like success and send the operator to a host the
            # phone may not reach.
            console.print(f"[yellow]Tailnet address unavailable[/] ({exc}); "
                          "falling back to adminBaseUrl.")
    admin_url = str(network.get("adminBaseUrl") or "").strip().rstrip("/")
    return admin_url


def _stdout_encoding() -> str | None:
    """The encoding the operator's terminal will actually receive."""
    return getattr(console.file, "encoding", None)


def _print_qr(url: str) -> bool:
    """Draw ``url`` as a scannable QR block on ``console``. True if drawn.

    Best-effort by construction: the URL is shown once, so a renderer that
    cannot run must cost the operator the code, never the link. Every failure
    path returns False and leaves the printed URL as the fallback.
    """
    from .qr_terminal import render_qr

    try:
        code = render_qr(url, encoding=_stdout_encoding())
    except Exception as exc:  # noqa: BLE001 — a missing/odd encoder is not fatal
        console.print(f"[yellow]Could not draw the QR code[/] ({exc}); "
                      "use the link above.")
        return False

    if console.is_terminal and code.width > console.width:
        # A wrapped QR is an unscannable QR, and we cannot un-print it. Only a
        # real terminal wraps, though — a redirected stdout just gets long
        # lines, so the width rich reports there is not a constraint (which is
        # why the rows below go out with soft_wrap, leaving reflow to us).
        console.print(f"[yellow]Terminal too narrow[/] for the QR code "
                      f"({code.width} columns needed, {console.width} "
                      "available) — widen the window and re-run, or use the "
                      "link above.")
        return False

    console.print()
    for line in code.lines:
        console.print(line, style=code.style, markup=False, highlight=False,
                      soft_wrap=True)
    console.print()
    return True


@board_group.command("token")
@click.argument("bot_id")
@click.option("--no-qr", is_flag=True,
              help="Print only the URL — no QR code (for scripts and logs).")
@click.pass_context
def board_token(ctx: click.Context, bot_id: str, no_qr: bool) -> None:
    """Mint (or rotate) BOT_ID's board token and print the phone URL once.

    The URL is also drawn as a QR code, because a 43-character token is not
    something anyone retypes on a phone: point the phone's camera at the
    terminal instead. Rotating invalidates the previous link immediately,
    including any already-issued browser cookie — the stored hash is
    replaced, so the old credential stops verifying on the next request, and
    the code printed here is the new link's.
    """
    from .board_store import mint_token, validate_bot_id
    network_path = _network_path(ctx)
    try:
        validate_bot_id(bot_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    token = mint_token(_shared_dir(network_path), bot_id)
    base = _board_base_url(network_path)
    url = f"{base}/board/{bot_id}?t={token}" if base else f"/board/{bot_id}?t={token}"

    console.print("[bold]Board link — shown once.[/] "
                  "The pod stores only its hash; there is no way to print it again.")
    console.print(url)
    scanned = False if no_qr else _print_qr(url)
    if scanned:
        console.print("[dim]Point the phone's camera at the code above — no "
                      "typing. Opening it once is all it takes: the pod swaps "
                      "the token for a cookie and drops it from the address "
                      "bar, so the bookmark you make afterwards carries no "
                      "credential.[/]")
    else:
        console.print()
        console.print("[dim]Open it on the phone once: the pod swaps the token "
                      "for a cookie and drops it from the address bar, so the "
                      "bookmark you make afterwards carries no credential.[/]")
    if not base:
        console.print("[yellow]No reachable host resolved[/] — set "
                      "board.tailnetListener.enabled in network.json (and sign in "
                      "to Tailscale), or set adminBaseUrl. The path above is "
                      "correct; prefix it with the host the phone can reach.")
    console.print("[dim]Revoke with: sudo evolve-admin board revoke "
                  f"{bot_id}[/]")


@board_group.command("revoke")
@click.argument("bot_id")
@click.pass_context
def board_revoke(ctx: click.Context, bot_id: str) -> None:
    """Revoke BOT_ID's board token. Every board request 401s until re-minted."""
    from .board_store import revoke_token, validate_bot_id
    network_path = _network_path(ctx)
    try:
        validate_bot_id(bot_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if revoke_token(_shared_dir(network_path), bot_id):
        console.print(f"[green]Revoked[/] the board token for {bot_id}. "
                      "Existing links and cookies no longer work.")
    else:
        console.print(f"[dim]No board token was set for {bot_id} — "
                      "nothing to revoke.[/]")
