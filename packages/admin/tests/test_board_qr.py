"""Tests for ``qr_terminal`` — the board link drawn to be scanned, not typed.

WHAT THESE PIN:
  * what is ON SCREEN decodes to the URL. The block is read back with a
    decoder that shares no code with the encoder (``tests/_qr_decode.py``),
    so an inverted, transposed or half-shifted rendering fails here rather
    than on the operator's phone;
  * the same holds when the block is read as PIXELS rather than characters
    (``tests/_ansi_raster.py``). That is the test the first version of this
    module could not have passed: its ``█ ▀ ▄`` glyphs are painted inside the
    font's ink box, a light stripe cuts every row, and the phone sees ladders
    where the finder patterns should be. Every character-level assertion
    still passed, which is why the live test was the first thing to fail;
  * the renderer choice follows the console: colour paints cells, no colour
    falls back to glyphs, and a locale that cannot carry ``█`` falls back
    again to ASCII rather than raising;
  * the palette stays inside the 16 named colours, so a terminal without
    truecolor gets ink and paper rather than a downgraded grey;
  * the quiet zone is inside the block we print, because the terminal's own
    background is whatever theme the operator picked, not guaranteed light.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import qr_terminal as qt  # noqa: E402
from tests import _ansi_raster as raster  # noqa: E402
from tests._qr_decode import decode, read_screen  # noqa: E402

# The shape a real board link has: tailnet host, port, bot id, 43-character
# urlsafe token. 90 characters, which lands at version 5 — the size an
# operator will actually be looking at.
URL = ("http://100.74.228.85:5050/board/personal-bot"
       "?t=VB-HYThb6pjs4Wh4JjGEB6RQhHlDq8k6hM1FLFBGSrs")

# A cell three pixels wide and six tall, with a glyph box that stops one row
# short of the bottom. That last row is the line gap Terminal.app leaves and
# the whole reason this module was rewritten; a rendering that survives it
# does not depend on the operator's font.
CELL_W, CELL_H, GLYPH_H = 3, 6, 5


def _ansi(code: qt.TerminalQR, *, color_system: str | None = "standard") -> str:
    """The block as it reaches a terminal — the same print call the CLI makes."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=400,
                      color_system=color_system)
    for row in code.rows:
        console.print(row, markup=False, highlight=False, soft_wrap=True)
    return buf.getvalue()


def test_the_painted_block_decodes_to_the_url():
    code = qt.render_qr(URL)
    assert code.kind == qt.KIND_CELLS
    cells = raster.parse_cells(_ansi(code))
    matrix = raster.modules_from_backgrounds(
        cells, columns_per_module=qt.CELL_COLUMNS)
    assert decode(matrix) == URL


def test_the_block_decodes_as_pixels_too():
    """The rendered-pixels test: cells fill the grid, so the modules are whole.

    This is the assertion the glyph rendering could not have passed, and the
    one that stands in for the phone.
    """
    code = qt.render_qr(URL)
    cells = raster.parse_cells(_ansi(code))
    pixels = raster.rasterise(cells, cell_w=CELL_W, cell_h=CELL_H,
                              glyph_h=GLYPH_H)
    modules = raster.modules_from_raster(pixels, modules_x=len(cells[0]) // 2,
                                         modules_y=len(cells))
    assert decode(modules) == URL


def test_a_glyph_rendering_cannot_survive_the_same_raster():
    """The negative half of the pixel test — otherwise it proves nothing.

    Rendered with colour on purpose, which is the glyph path's best case: the
    ink is styled black-on-white and the encoding is correct. It still fails,
    because the font's ink box does not reach the bottom of the cell.
    """
    matrix = qt.qr_matrix(URL)
    glyphs = qt.TerminalQR(
        [Text(line, style=qt.QR_STYLE, no_wrap=True, end="")
         for line in qt.render_half_blocks(matrix)], qt.KIND_HALF_BLOCKS)
    cells = raster.parse_cells(_ansi(glyphs))
    pixels = raster.rasterise(cells, cell_w=CELL_W, cell_h=CELL_H,
                              glyph_h=GLYPH_H)
    with pytest.raises(raster.NotUniform):
        raster.modules_from_raster(pixels, modules_x=len(cells[0]),
                                   modules_y=2 * len(cells))

    # …and the same glyphs pass the character decoder, which is exactly why
    # the character decoder was not enough.
    assert decode(read_screen(glyphs.plain)) == URL


def test_the_palette_stays_inside_the_named_sixteen():
    """A truecolor grey would look right here and wrong on a 16-colour ssh
    session. ``parse_cells`` refuses anything outside ink and paper."""
    for system in ("standard", "256", "truecolor"):
        cells = raster.parse_cells(_ansi(qt.render_qr(URL), color_system=system))
        assert {cell.bg_ink for row in cells for cell in row} == {True, False}


def test_the_renderer_choice_follows_the_console():
    assert qt.render_qr(URL).kind == qt.KIND_CELLS
    assert qt.render_qr(URL, color=True, encoding="ascii").kind == qt.KIND_CELLS
    assert qt.render_qr(URL, color=False).kind == qt.KIND_HALF_BLOCKS
    assert qt.render_qr(URL, color=False, encoding="ascii").kind == qt.KIND_ASCII
    assert qt.render_qr(URL).font_dependent is False
    assert qt.render_qr(URL, color=False).font_dependent is True


def test_the_fallbacks_still_decode_to_the_url():
    for encoding, kind in (("utf-8", qt.KIND_HALF_BLOCKS),
                           ("ascii", qt.KIND_ASCII)):
        code = qt.render_qr(URL, color=False, encoding=encoding)
        assert code.kind == kind
        assert decode(read_screen(code.plain)) == URL
    qt.render_qr(URL, color=False, encoding="ascii").plain[0].encode("ascii")


@pytest.mark.parametrize("url", [
    "/board/b?t=x",                                    # no host resolved
    "https://pod.tailnet.ts.net/board/a_b-c?t=" + "Zz9-_" * 8,
    "http://100.64.0.1:5061/board/" + "b" * 18 + "?t=" + "Q" * 43,
])
def test_other_link_shapes_also_decode(url):
    cells = raster.parse_cells(_ansi(qt.render_qr(url)))
    assert decode(raster.modules_from_backgrounds(
        cells, columns_per_module=qt.CELL_COLUMNS)) == url


def test_renderer_choice_follows_what_stdout_can_encode():
    assert qt.supports_half_blocks("utf-8") is True
    assert qt.supports_half_blocks("UTF-8") is True
    assert qt.supports_half_blocks("ascii") is False
    assert qt.supports_half_blocks("latin-1") is False
    assert qt.supports_half_blocks("not-a-codec") is False
    assert qt.supports_half_blocks(None) is False
    assert qt.supports_half_blocks("") is False


def test_one_module_row_per_terminal_row_two_columns_per_module():
    """The block an operator has to make room for: 90 columns by 45 rows."""
    matrix = qt.qr_matrix(URL)
    code = qt.render_qr(URL)
    assert code.width == qt.CELL_COLUMNS * len(matrix[0]) == 90
    assert len(code.rows) == len(matrix) == 45
    assert len({row.cell_len for row in code.rows}) == 1


def test_the_quiet_zone_is_painted_paper_across_the_full_row():
    """We paint our own paper, so the margin has to be part of the block —
    and it has to be paper, not the terminal's own background."""
    code = qt.render_qr(URL)
    cells = raster.parse_cells(_ansi(code))
    matrix = raster.modules_from_backgrounds(
        cells, columns_per_module=qt.CELL_COLUMNS)
    for row in matrix[:qt.QUIET_ZONE] + matrix[-qt.QUIET_ZONE:]:
        assert not any(row)
    for row in matrix:
        assert not any(row[:qt.QUIET_ZONE]) and not any(row[-qt.QUIET_ZONE:])
    assert not any(cell.bg_ink is None for line in cells for cell in line)


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
    cells = raster.parse_cells(_ansi(qt.render_qr(url)))
    assert raster.modules_from_backgrounds(
        cells, columns_per_module=qt.CELL_COLUMNS) == matrix
    assert read_screen(qt.render_qr(url, color=False).plain)[:len(matrix)] == matrix
    assert read_screen(qt.render_qr(url, color=False,
                                    encoding="ascii").plain) == matrix
