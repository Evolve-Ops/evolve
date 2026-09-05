"""Tests for ``qr_terminal`` — the board link drawn to be scanned, not typed.

WHAT THESE PIN:
  * what is ON SCREEN decodes to the URL. The block is read back with a
    decoder that shares no code with the encoder (``tests/_qr_decode.py``),
    so an inverted, transposed or half-shifted rendering fails here rather
    than on the operator's phone;
  * the same holds for the ASCII fallback a non-UTF-8 terminal gets;
  * the choice of renderer follows what stdout can ENCODE — a locale that
    cannot carry ``█`` gets ASCII rather than a UnicodeEncodeError;
  * the quiet zone is inside the block we print, because the terminal's own
    background is whatever theme the operator picked, not guaranteed light.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import qr_terminal as qt  # noqa: E402
from tests._qr_decode import decode, read_screen  # noqa: E402

# The shape a real board link has: tailnet host, port, bot id, 43-character
# urlsafe token. 90 characters, which lands at version 5 — the size an
# operator will actually be looking at.
URL = ("http://100.74.228.85:5050/board/personal-bot"
       "?t=VB-HYThb6pjs4Wh4JjGEB6RQhHlDq8k6hM1FLFBGSrs")


def test_the_printed_block_decodes_to_the_url():
    code = qt.render_qr(URL)
    assert code.unicode is True
    assert decode(read_screen(code.lines)) == URL


def test_the_ascii_fallback_decodes_to_the_url():
    code = qt.render_qr(URL, encoding="ascii")
    assert code.unicode is False
    assert code.lines[0].strip() == ""  # still pure ASCII, quiet zone included
    code.lines[0].encode("ascii")
    assert decode(read_screen(code.lines)) == URL


@pytest.mark.parametrize("url", [
    "/board/b?t=x",                                    # no host resolved
    "https://pod.tailnet.ts.net/board/a_b-c?t=" + "Zz9-_" * 8,
    "http://100.64.0.1:5061/board/" + "b" * 18 + "?t=" + "Q" * 43,
])
def test_other_link_shapes_also_decode(url):
    assert decode(read_screen(qt.render_qr(url).lines)) == url


def test_renderer_choice_follows_what_stdout_can_encode():
    assert qt.supports_half_blocks("utf-8") is True
    assert qt.supports_half_blocks("UTF-8") is True
    assert qt.supports_half_blocks("ascii") is False
    assert qt.supports_half_blocks("latin-1") is False
    assert qt.supports_half_blocks("not-a-codec") is False
    assert qt.supports_half_blocks(None) is False
    assert qt.supports_half_blocks("") is False


def test_two_module_rows_share_one_terminal_row():
    """The aspect ratio a scanner expects: one cell wide, half a cell tall."""
    matrix = qt.qr_matrix(URL)
    code = qt.render_qr(URL)
    assert code.width == len(matrix[0])
    assert len(code.lines) == (len(matrix) + 1) // 2
    assert len({len(line) for line in code.lines}) == 1


def test_the_quiet_zone_is_inside_the_block_we_print():
    """We paint our own paper, so the margin has to be part of the block."""
    code = qt.render_qr(URL)
    margin_rows = qt.QUIET_ZONE // 2
    for line in code.lines[:margin_rows] + code.lines[-margin_rows:]:
        assert line.strip() == ""
    for line in code.lines:
        assert line[:qt.QUIET_ZONE] == " " * qt.QUIET_ZONE
        assert line[-qt.QUIET_ZONE:] == " " * qt.QUIET_ZONE
    assert qt.QR_STYLE == "black on white"


def test_an_odd_module_count_does_not_drop_the_last_row():
    """Every module row reaches the screen even when they do not pair up."""
    matrix = [[True, False], [False, True], [True, True]]
    lines = qt.render_half_blocks(matrix)
    assert lines == ["▀▄", "▀▀"]
    assert read_screen(lines)[:3] == matrix


def test_a_larger_symbol_still_renders_module_for_module():
    """Past version 5 a board link needs more than one RS block, which the
    test decoder does not reassemble. The rendering is still checked — the
    screen has to carry the encoder's matrix exactly, module for module."""
    url = "http://100.64.0.1:5061/board/" + "b" * 40 + "?t=" + "Q" * 43
    matrix = qt.qr_matrix(url)
    assert len(matrix) - 2 * qt.QUIET_ZONE == 41  # version 6
    assert read_screen(qt.render_qr(url).lines)[:len(matrix)] == matrix
    assert read_screen(qt.render_qr(url, encoding="ascii").lines) == matrix
