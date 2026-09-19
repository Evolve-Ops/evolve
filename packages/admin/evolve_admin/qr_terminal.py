"""Render a QR code as terminal text, so a link can be scanned instead of typed.

The board's phone link carries a 43-character token. There is no practical way
to move that string from an ssh terminal to a phone by hand, and every side
channel that would (clipboard sync, a message app) either does not exist on a
headless pod or parks the credential in a third party's store. So the terminal
draws the code and the phone's camera reads it off the screen.

Rendering — why the cells are painted, not drawn
------------------------------------------------
A module is **two spaces with a background colour**: ``on black`` for ink,
``on bright_white`` for paper, one module row per terminal row. Two columns
wide by one row tall is very close to square on a normal terminal cell, which
is the aspect a scanner expects.

The first version of this drew ``█ ▀ ▄`` half-blocks instead — twice as
compact, two module rows to a terminal row. It failed the first live phone
test (2026-09-04, macOS Terminal.app, default profile): those glyphs are
painted inside the font's ink box, which is shorter than the terminal cell, so
the line gap left a light stripe across every row. The finder patterns became
ladders and no scanner could lock on. A *background* colour has no such box —
the terminal fills the whole cell, gap included — so the rendering no longer
depends on the operator's font or line spacing. That is the entire reason for
the format, and it costs width: a board link is 90 columns by 45 rows.

Colours are named 16-colour entries, so a terminal with no truecolor still
gets pure ink and paper rather than a downgraded grey. Paper is
``bright_white`` rather than ``white``: ANSI 7 is a light grey in several
common profiles (Terminal.app's Basic among them), and grey paper is what the
operator reported seeing.

Fallbacks
---------
If the console will not carry colour — a dumb terminal or stdout redirected
to a file (rich reports ``color_system is None``), or ``NO_COLOR`` in the
environment (rich leaves ``color_system`` set and instead sets
``console.no_color``, stripping every SGR at print time, so painted cells
would arrive as blank rows) — background painting has nothing to paint with,
so the half-block renderer comes back. Both are the caller's to check; see
``board_cli._print_qr``, which passes ``color_system is not None and not
no_color``. The fallback prints a line under the code saying so, because that
path is the one that can fail on a phone. If stdout also cannot encode the
block glyphs (a non-UTF-8 locale over ssh), it degrades again to ``##`` / two
spaces per module, pure ASCII.

The encoder is the ``qrcode`` package, already a declared dependency of this
distribution (the WhatsApp/Signal pairing wizards render pairing codes with
it). Nothing here shells out.
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

#: Half-block glyphs, indexed by (top module is ink, bottom module is ink).
_FULL = "█"   # ink over ink
_UPPER = "▀"  # ink over paper
_LOWER = "▄"  # paper over ink
_BLANK = " "       # paper over paper

#: ASCII stand-ins, two columns per module so the code stays roughly square.
_ASCII_INK = "##"
_ASCII_PAPER = "  "

#: Modules of quiet zone. Four is what the spec asks for, and it has to be
#: inside our painted paper — the terminal's own background around the block
#: is not guaranteed to be light.
QUIET_ZONE = 4

#: Columns per module in the cell renderer. A terminal cell is about twice as
#: tall as it is wide, so two of them next to one row is square enough to scan.
CELL_COLUMNS = 2

#: The cell renderer's two styles. Background only: the glyph is a space, so
#: nothing here depends on the font. Both are 16-colour names, which every
#: colour terminal renders the same way.
INK_STYLE = "on black"
PAPER_STYLE = "on bright_white"

#: Dark ink on light paper, as a QR is specified — the style the *fallback*
#: renderers want applied to their glyphs. (The cell renderer carries its own
#: per-module styles and ignores this.)
QR_STYLE = "black on bright_white"

#: ``TerminalQR.kind`` values.
KIND_CELLS = "cells"
KIND_HALF_BLOCKS = "half-blocks"
KIND_ASCII = "ascii"


@dataclass(frozen=True)
class TerminalQR:
    """A QR code ready to print: styled rows, and which renderer drew them."""

    rows: list[Text]
    kind: str

    @property
    def width(self) -> int:
        """Columns the block occupies — what the caller checks against the
        terminal, since a reflowed QR is an unscannable one."""
        return self.rows[0].cell_len if self.rows else 0

    @property
    def plain(self) -> list[str]:
        """The rows as unstyled characters. All spaces for the cell renderer,
        where the code lives in the background colours."""
        return [row.plain for row in self.rows]

    @property
    def font_dependent(self) -> bool:
        """True when the code is drawn with glyphs, whose ink box may not fill
        the terminal cell. The caller says so under the code: that is the
        rendering that failed a phone once."""
        return self.kind != KIND_CELLS


def qr_matrix(data: str, *, border: int = QUIET_ZONE) -> list[list[bool]]:
    """The code for ``data`` as rows of booleans (``True`` = a dark module).

    Includes the quiet zone, so the matrix is what should be drawn verbatim.
    """
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L

    # ERROR_CORRECT_L, deliberately. The channel here is a clean terminal
    # screen photographed from 30cm — there is no print damage for a higher
    # level to recover from, and what does defeat a phone camera is small
    # modules. L holds a board link at version 5 where M needs version 6, so
    # every module is ~10% larger on screen and the block is two rows shorter
    # in a cramped ssh window.
    #
    # optimize=0 keeps the payload as ONE byte-mode segment. The optimizer
    # would split this URL into ten numeric/alphanumeric/byte runs to save
    # bits it cannot spend — the version comes out at 5 either way — while
    # making what is on screen depend on the token's characters.
    code = qrcode.QRCode(border=border, error_correction=ERROR_CORRECT_L)
    code.add_data(data, optimize=0)
    code.make(fit=True)
    return [[bool(cell) for cell in row] for row in code.get_matrix()]


def render_cells(matrix: list[list[bool]]) -> list[Text]:
    """One terminal row per module row, each module two background-painted
    spaces. The quiet zone is painted too — the terminal's own background is
    whatever theme the operator picked, not guaranteed light."""
    rows: list[Text] = []
    for row in matrix:
        text = Text(no_wrap=True, end="")
        # Runs of like modules are appended as one span. A version-5 board
        # link is 45 modules wide; emitting 45 separate SGR pairs per row
        # would quadruple the bytes on the wire for no visible difference.
        start = 0
        for index in range(1, len(row) + 1):
            if index < len(row) and row[index] == row[start]:
                continue
            span = " " * (CELL_COLUMNS * (index - start))
            text.append(span, style=INK_STYLE if row[start] else PAPER_STYLE)
            start = index
        rows.append(text)
    return rows


def render_half_blocks(matrix: list[list[bool]]) -> list[str]:
    """Two module rows per terminal row, using ``█ ▀ ▄`` and a space."""
    lines: list[str] = []
    width = len(matrix[0]) if matrix else 0
    blank_row = [False] * width
    for top_index in range(0, len(matrix), 2):
        top = matrix[top_index]
        # An odd number of module rows leaves the last cell half paper, which
        # simply extends the quiet zone downwards.
        bottom = matrix[top_index + 1] if top_index + 1 < len(matrix) else blank_row
        lines.append("".join(
            _FULL if (t and b) else _UPPER if t else _LOWER if b else _BLANK
            for t, b in zip(top, bottom)
        ))
    return lines


def render_ascii(matrix: list[list[bool]]) -> list[str]:
    """One terminal row per module row, two ASCII columns per module."""
    return ["".join(_ASCII_INK if cell else _ASCII_PAPER for cell in row)
            for row in matrix]


def supports_half_blocks(encoding: str | None) -> bool:
    """Whether ``encoding`` can carry the half-block glyphs."""
    if not encoding:
        return False
    try:
        (_FULL + _UPPER + _LOWER).encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def render_qr(data: str, *, encoding: str | None = "utf-8", color: bool = True,
              border: int = QUIET_ZONE) -> TerminalQR:
    """Render ``data`` for a terminal that uses ``encoding`` and may have colour.

    ``color`` is the console's own answer about colour, which takes two
    questions, not one: ``console.color_system is not None and not
    console.no_color`` — under ``NO_COLOR`` rich keeps a ``color_system`` and
    strips the SGR at render instead, so asking only the first would paint
    cells that print as blank rows. With colour we paint cells, which no font
    can break; without it we fall back to glyphs, which one already did.
    """
    matrix = qr_matrix(data, border=border)
    if color:
        return TerminalQR(render_cells(matrix), KIND_CELLS)
    if supports_half_blocks(encoding):
        return TerminalQR([Text(line, style=QR_STYLE, no_wrap=True, end="")
                           for line in render_half_blocks(matrix)],
                          KIND_HALF_BLOCKS)
    return TerminalQR([Text(line, style=QR_STYLE, no_wrap=True, end="")
                       for line in render_ascii(matrix)], KIND_ASCII)
