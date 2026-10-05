"""``evolve-admin connections expire|revive`` — the CLI recovery path for a
spent or lapsed grant (D-TF3). Same writes as the admin UI's
``POST /api/connections/<bot>/<row>/grant`` (``make_standing`` / ``expire_now``)."""
from __future__ import annotations

from pathlib import Path

import click

from . import connections as conn
from .config import DEFAULT_NETWORK_CONFIG, load_network


def _registry_path(network_path: str) -> Path:
    return conn.connections_path(load_network(Path(network_path)))


def _run(row_id: str, network_path: str, fn, done: str) -> None:
    if not fn(row_id, _registry_path(network_path)):
        raise click.ClickException(f"no connection row {row_id!r}")
    click.echo(f"{row_id}: {done}")


def register_cli(main: click.Group) -> None:
    @main.group("connections")
    def connections_group() -> None:
        """Operate on connection-registry grants."""

    for name, fn, done, doc in (
        ("expire", conn.expire_now, "grant expired now",
         "Expire a connection's grant now (until:<now>)."),
        ("revive", lambda r, p: conn.set_grant_scope(r, "standing", p), "grant is standing again",
         "Bring a spent/expired grant back as a standing grant."),
    ):
        def cmd(row_id: str, network: str, _fn=fn, _done=done) -> None:
            _run(row_id, network, _fn, _done)
        cmd.__doc__ = doc
        cmd = click.option("--network", default=str(DEFAULT_NETWORK_CONFIG), show_default=True)(cmd)
        cmd = click.argument("row_id")(cmd)
        connections_group.command(name)(cmd)
