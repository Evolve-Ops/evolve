"""Render a QR code as terminal text, so a link can be scanned instead of typed.

The board's phone link carries a 43-character token. There is no practical way
to move that string from an ssh terminal to a phone by hand, and every side
channel that would (clipboard sync, a message app) either does not exist on a
headless pod or parks the credential in a third party's store. So the terminal
draws the code and the phone's camera reads it off the screen.

Rendering
---------
Two module rows share one terminal row via the half-block characters
``█ ▀ ▄``, because a terminal cell is about twice as tall as it is wide —
one cell per module horizontally and half a cell vertically comes out
square, which is what a scanner expects. A 45-module code (the usual size
for a board link) is then 45 columns by 23 rows.

Ink is drawn **black on white** via the returned ``style``: a QR is specified
dark-on-light, and a terminal's own background is whatever the operator chose.
Painting our own paper makes the code scannable on a dark theme without
relying on the scanner's tolerance for inverted codes.

When stdout cannot encode the block characters (a non-UTF-8 locale over ssh),
``render_qr`` falls back to ``##`` / two spaces per module — pure ASCII, one
terminal row per module row, and twice as wide.

The encoder is the ``qrcode`` package, already a declared dependency of this
distribution (the WhatsApp/Signal pairing wizards render pairing codes with
it). Nothing here shells out.
"""
from __future__ import annotations

from dataclasses import dataclass

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

#: Dark ink on light paper, as a QR is specified. Applied by the caller to
#: every line of ``TerminalQR.lines``.
QR_STYLE = "black on white"


@dataclass(frozen=True)
class TerminalQR:
    """A QR code ready to print: its lines, the style each line wants, size."""

    lines: list[str]
    style: str
    unicode: bool

    @property
    def width(self) -> int:
        """Columns the block occupies — what the caller checks against the
        terminal, since a reflowed QR is an unscannable one."""
        return len(self.lines[0]) if self.lines else 0


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


def render_qr(data: str, *, encoding: str | None = "utf-8",
              border: int = QUIET_ZONE) -> TerminalQR:
    """Render ``data`` for a terminal whose stdout uses ``encoding``."""
    matrix = qr_matrix(data, border=border)
    if supports_half_blocks(encoding):
        return TerminalQR(render_half_blocks(matrix), QR_STYLE, unicode=True)
    return TerminalQR(render_ascii(matrix), QR_STYLE, unicode=False)
