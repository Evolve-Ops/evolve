"""Tests for ``evolve-admin board token|revoke`` (board_cli.py).

WHAT THESE PIN:
  * the CLI is a WRAPPER — it mints through ``board_store``, so there is one
    token-creation path, and the minted token is the one the board accepts;
  * ``token`` prints a working URL exactly once, and ``revoke`` makes every
    board request 401 again — the round trip D-MB2 asked for;
  * a bad bot id is refused before anything is written;
  * the printed URL degrades honestly when no reachable host resolves,
    rather than inventing one the phone cannot reach;
  * the port in that URL is the port the listener BINDS, not a second copy
    of it — the link is shown once, so a port that disagrees with the
    daemon's is unrecoverable;
  * the URL is also printed as a QR code that decodes back to that exact
    URL — a 43-character token is not retyped on a phone, so scanning is
    the delivery path, not a convenience;
  * every way the code can fail to draw still prints the link, because the
    link cannot be printed a second time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_cli  # noqa: E402
from evolve_admin import board_store as bs  # noqa: E402
from evolve_admin import qr_terminal  # noqa: E402
from evolve_admin.board_cli import board_group  # noqa: E402
from evolve_admin.web.routes_board import register_board_routes  # noqa: E402
from tests._qr_decode import decode, read_screen  # noqa: E402

BOT = "personal-bot"


@pytest.fixture()
def pod(tmp_path: Path):
    shared = tmp_path / "evolve"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared)}))
    app = Flask(__name__)
    register_board_routes(app, network)
    return {"shared": shared, "network": network, "client": app.test_client()}


def _run(pod, *args):
    return CliRunner().invoke(board_group, list(args),
                              obj={"network_path": pod["network"]})


def _url_from(output: str) -> str:
    for line in output.splitlines():
        if "/board/" in line and "?t=" in line:
            return line.strip()
    raise AssertionError(f"no board URL in output:\n{output}")


def _qr_lines(output: str) -> list[str]:
    """The block of QR rows in ``output`` — the lines a camera would see."""
    lines = [ln for ln in output.splitlines()
             if len(ln) > 20 and set(ln) <= set("█▀▄ #")]
    if not lines:
        raise AssertionError(f"no QR block in output:\n{output}")
    return lines


def _token_from(output: str) -> str:
    for line in output.splitlines():
        if "?t=" in line:
            return line.split("?t=", 1)[1].strip()
    raise AssertionError(f"no board URL in output:\n{output}")


def test_token_mints_a_credential_the_board_accepts(pod):
    result = _run(pod, "token", BOT)
    assert result.exit_code == 0, result.output
    token = _token_from(result.output)
    assert len(token) >= 32
    r = pod["client"].get(f"/api/board/{BOT}?t={token}")
    assert r.status_code == 200


def test_token_writes_only_the_hash(pod):
    token = _token_from(_run(pod, "token", BOT).output)
    stored = (pod["shared"] / "boards" / BOT / "token.sha256").read_text()
    assert token not in stored
    assert bs.verify_token(pod["shared"], BOT, token) is True


def test_token_rotates_and_invalidates_the_previous_one(pod):
    first = _token_from(_run(pod, "token", BOT).output)
    second = _token_from(_run(pod, "token", BOT).output)
    assert first != second
    assert pod["client"].get(f"/api/board/{BOT}?t={first}").status_code == 401
    assert pod["client"].get(f"/api/board/{BOT}?t={second}").status_code == 200


def test_revoke_round_trip(pod):
    token = _token_from(_run(pod, "token", BOT).output)
    assert pod["client"].get(f"/api/board/{BOT}?t={token}").status_code == 200

    result = _run(pod, "revoke", BOT)
    assert result.exit_code == 0, result.output
    assert "Revoked" in result.output
    assert pod["client"].get(f"/api/board/{BOT}?t={token}").status_code == 401

    # Revoking again is not an error — it is already in the desired state.
    again = _run(pod, "revoke", BOT)
    assert again.exit_code == 0
    assert "nothing to revoke" in again.output


def test_bad_bot_id_is_refused_before_anything_is_written(pod):
    result = _run(pod, "token", "../../etc/passwd")
    assert result.exit_code != 0
    assert not (pod["shared"] / "boards").exists()


def test_url_uses_admin_base_url_when_no_listener_is_configured(pod):
    pod["network"].write_text(json.dumps({
        "sharedDir": str(pod["shared"]),
        "adminBaseUrl": "https://pod.tailnet.ts.net",
    }))
    out = _run(pod, "token", BOT).output
    assert f"https://pod.tailnet.ts.net/board/{BOT}?t=" in out


def test_url_port_is_the_one_the_listener_binds_under_a_serve_port_override(
    pod, monkeypatch,
):
    """``evolve-admin serve --port 8080`` (recorded in the installed launchd
    job) moves the board listener with it, so the shown-once link must move
    too. Asserted against the listener's own resolver rather than a literal,
    so the two cannot drift apart again."""
    from evolve_admin import config as cfg
    from evolve_admin.web import board_listener as bl

    network = {"sharedDir": str(pod["shared"]),
               "board": {"tailnetListener": {"enabled": True}}}
    pod["network"].write_text(json.dumps(network))
    monkeypatch.setattr(cfg, "_admin_port_from_launchd", lambda: 8080)
    monkeypatch.setattr(
        bl, "resolve_bind_address",
        lambda: bl.BindAddress("100.101.102.103", bl.CLI_SOURCE),
    )

    bound = bl.resolve_listener_port(network, admin_port=8080)
    assert bound == 8080
    out = _run(pod, "token", BOT).output
    assert f"http://100.101.102.103:{bound}/board/{BOT}?t=" in out


def test_url_port_follows_the_board_listener_override(pod, monkeypatch):
    """``board.tailnetListener.port`` wins over the admin port on both sides."""
    from evolve_admin import config as cfg
    from evolve_admin.web import board_listener as bl

    network = {"sharedDir": str(pod["shared"]),
               "board": {"tailnetListener": {"enabled": True, "port": 5061}}}
    pod["network"].write_text(json.dumps(network))
    monkeypatch.setattr(cfg, "_admin_port_from_launchd", lambda: 8080)
    monkeypatch.setattr(
        bl, "resolve_bind_address",
        lambda: bl.BindAddress("100.101.102.103", bl.CLI_SOURCE),
    )

    out = _run(pod, "token", BOT).output
    assert f"http://100.101.102.103:5061/board/{BOT}?t=" in out


def test_url_degrades_honestly_when_no_host_resolves(pod):
    out = _run(pod, "token", BOT).output
    assert f"/board/{BOT}?t=" in out
    assert "No reachable host resolved" in out


def test_the_url_is_also_printed_as_a_scannable_code(pod):
    """The delivery path: the phone's camera reads the link off the terminal."""
    result = _run(pod, "token", BOT)
    assert result.exit_code == 0, result.output
    url = _url_from(result.output)
    assert decode(read_screen(_qr_lines(result.output))) == url
    assert "Point the phone's camera at the code above" in result.output

    # And it is the credential the board actually accepts — the code is of
    # the link, not of some other string that merely looks like one.
    token = url.split("?t=", 1)[1]
    assert pod["client"].get(f"/api/board/{BOT}?t={token}").status_code == 200


def test_rotation_prints_a_code_for_the_new_link(pod):
    first = _run(pod, "token", BOT)
    second = _run(pod, "token", BOT)
    first_url = _url_from(first.output)
    second_url = _url_from(second.output)
    assert first_url != second_url
    assert decode(read_screen(_qr_lines(second.output))) == second_url


def test_no_qr_prints_the_url_alone(pod):
    result = _run(pod, "token", BOT, "--no-qr")
    assert result.exit_code == 0, result.output
    assert f"/board/{BOT}?t=" in result.output
    with pytest.raises(AssertionError):
        _qr_lines(result.output)


def test_a_non_utf8_terminal_gets_an_ascii_code(pod, monkeypatch):
    """A C-locale ssh session still gets a scannable code, not a traceback."""
    monkeypatch.setattr(board_cli, "_stdout_encoding", lambda: "ascii")
    result = _run(pod, "token", BOT)
    assert result.exit_code == 0, result.output
    lines = _qr_lines(result.output)
    assert not any(ch in "█▀▄" for line in lines for ch in line)
    "\n".join(lines).encode("ascii")  # the code itself carries no wide glyphs
    assert decode(read_screen(lines)) == _url_from(result.output)


def test_the_link_survives_a_renderer_that_cannot_draw(pod, monkeypatch):
    """The URL is shown once. A broken encoder costs the code, never the link."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("no encoder here")

    monkeypatch.setattr(qr_terminal, "render_qr", boom)
    result = _run(pod, "token", BOT)
    assert result.exit_code == 0, result.output
    assert "Could not draw the QR code" in result.output
    token = _token_from(result.output)
    assert pod["client"].get(f"/api/board/{BOT}?t={token}").status_code == 200


def test_a_too_narrow_terminal_says_so_instead_of_wrapping_the_code(pod,
                                                                    monkeypatch):
    """A wrapped QR is an unscannable QR, and it cannot be un-printed."""
    from rich.console import Console

    monkeypatch.setattr(Console, "is_terminal", property(lambda self: True))
    monkeypatch.setattr(Console, "width", property(lambda self: 40))
    result = _run(pod, "token", BOT)
    assert result.exit_code == 0, result.output
    assert "Terminal too narrow" in result.output
    with pytest.raises(AssertionError):
        _qr_lines(result.output)
