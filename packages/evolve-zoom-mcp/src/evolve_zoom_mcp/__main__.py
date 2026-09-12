"""CLI entry point for the Zoom MCP shim.

Subcommands:

  serve          (default) — run the MCP server on stdio.
  login          — kick off the user-OAuth dance and persist a refresh token.
  login --code   — headless: exchange a pasted code for tokens.
  status         — print whether credentials.json exists + when it expires.

When OC spawns ``uvx evolve-zoom-mcp``, no args are passed and we enter the
serve loop. The login + status flows are operator-facing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

import click

from .credentials import load_credentials
from .server import run as run_server
from .zoom_oauth import (
    OAuthConfig,
    OAuthError,
    build_authorize_url,
    exchange_code,
)


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """evolve-zoom-mcp — Zoom MCP shim for Evolve."""
    if ctx.invoked_subcommand is None:
        # Default: run the MCP server on stdio.
        asyncio.run(run_server())


@main.command()
@click.option(
    "--code",
    "code",
    default=None,
    help="Pre-obtained authorization code (skips the local listener).",
)
@click.option(
    "--port",
    "port",
    type=int,
    default=None,
    envvar="ZOOM_OAUTH_PORT",
    help="Local listener port for the OAuth callback (default 8989).",
)
def login(code: Optional[str], port: Optional[int]) -> None:
    """Kick off the Zoom user-OAuth dance and write credentials.json.

    Two modes:

    1. ``--code`` provided — exchange immediately.
    2. No ``--code`` — print the authorize URL, spin up a local HTTP listener
       on 127.0.0.1:8989 (or ``ZOOM_OAUTH_PORT``), wait for Zoom's redirect,
       capture the code, exchange.
    """
    try:
        config = OAuthConfig.from_env(os.environ)
    except KeyError as missing:
        click.echo(f"[error] missing env var: {missing}", err=True)
        sys.exit(2)

    if code:
        creds = _exchange_or_exit(config, code)
        _print_login_summary(creds)
        return

    listener_port = port or 8989
    captured = _CapturedCode()
    server = _start_callback_listener(listener_port, captured)
    try:
        click.echo(
            "Open this URL in your browser, authorize the app, then come back here:"
        )
        click.echo(build_authorize_url(config))
        click.echo(f"\nWaiting for redirect on http://127.0.0.1:{listener_port}/oauth/callback ...")
        captured.event.wait(timeout=600)  # 10 min
        if captured.code is None:
            click.echo(
                f"[error] no code received after 10 minutes; {captured.error or 'timed out'}",
                err=True,
            )
            sys.exit(1)
        creds = _exchange_or_exit(config, captured.code)
        _print_login_summary(creds)
    finally:
        server.shutdown()


@main.command()
def status() -> None:
    """Print whether credentials.json exists and when it expires."""
    try:
        config = OAuthConfig.from_env(os.environ)
    except KeyError as missing:
        click.echo(f"[error] missing env var: {missing}", err=True)
        sys.exit(2)
    creds = load_credentials(config.credentials_dir)
    if creds is None:
        click.echo(json.dumps({"configured": False}))
        return
    click.echo(
        json.dumps(
            {
                "configured": True,
                "user_email": creds.user_email,
                "scopes": creds.scopes,
                "access_token_fresh": creds.is_access_token_fresh(),
                "access_token_expires_at": creds.access_token_expires_at,
            },
            indent=2,
        )
    )


def _exchange_or_exit(config: OAuthConfig, code: str):  # type: ignore[no-untyped-def]
    try:
        return exchange_code(config, code)
    except OAuthError as exc:
        click.echo(f"[error] Zoom rejected the code: {exc} (code={exc.code})", err=True)
        sys.exit(1)


def _print_login_summary(creds) -> None:  # type: ignore[no-untyped-def]
    click.echo(
        f"\nLogged in as {creds.user_email or '(email unknown)'}."
        f"\nScopes granted: {', '.join(creds.scopes) or '(none in response)'}"
        f"\nCredentials saved."
    )


class _CapturedCode:
    """Cross-thread holder for the OAuth code from the local listener."""

    def __init__(self) -> None:
        self.code: Optional[str] = None
        self.error: Optional[str] = None
        self.event = threading.Event()


def _start_callback_listener(port: int, captured: _CapturedCode) -> HTTPServer:
    """Spin up an HTTPServer that captures one ``?code=`` and signals done."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # silence
            pass

        def do_GET(self) -> None:  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            code = qs.get("code", [None])[0]
            err = qs.get("error", [None])[0]
            captured.code = code
            captured.error = err
            body = (
                b"<html><body><h2>You can close this tab.</h2>"
                b"<p>The Zoom OAuth dance completed; the shim has the code.</p>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            captured.event.set()

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    main()
