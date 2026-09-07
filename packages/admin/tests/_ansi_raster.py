"""Read a terminal's ANSI output the way a camera does — as pixels, not text.

``tests/_qr_decode.py`` reads *characters*, which proves the encoding and the
module layout. It cannot see how a cell is painted, and that is precisely
where the first live phone test failed (2026-09-04, Terminal.app): the
``█ ▀ ▄`` glyphs are drawn inside the font's ink box, which is shorter than
the terminal cell, so a light stripe cut every row and the finder patterns
came out as ladders. Every character-level assertion in the suite passed.

So this helper models the screen:

  * ``parse_cells`` walks the SGR codes and gives, per terminal cell, the
    character and the foreground/background colours it was drawn with. It
    understands only the 16-colour codes this renderer is allowed to emit —
    a truecolor grey raises, rather than being quietly accepted;
  * ``rasterise`` paints those cells into a pixel grid. A background colour
    fills the **whole** cell. A glyph fills only its ink box — ``glyph_h``
    pixels of a ``cell_h``-pixel cell — which is the font behaviour that
    broke the phone;
  * ``modules_from_raster`` divides the pixels back into the module grid and
    demands each module be uniformly ink or paper, because a module that is
    part ink and part paper is one a scanner cannot derive a grid from. The
    result goes to ``_qr_decode.decode``.

The cell renderer passes because painted backgrounds fill the cell; the glyph
renderers cannot, whatever the encoder does. That is the discrimination the
character decoder could never make.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Foreground SGR codes we accept, mapped to "is this ink?".
_FG = {30: True, 90: True, 37: False, 97: False}
#: Background SGR codes we accept, mapped to "is this ink?".
_BG = {40: True, 100: True, 47: False, 107: False}

#: Colour codes outside the ink/paper pair — a hue that is neither.
_OTHER_COLOURS = frozenset(
    list(range(31, 37)) + list(range(41, 47))
    + list(range(91, 97)) + list(range(101, 107))
)

_SGR = re.compile(r"\x1b\[([0-9;]*)m")

#: Glyph coverage inside the ink box, as a fraction range of ``glyph_h``.
#: ``█`` fills it, ``▀``/``▄`` take a half each, ``#`` is the ASCII stand-in.
_GLYPHS = {"█": (0.0, 1.0), "▀": (0.0, 0.5), "▄": (0.5, 1.0), "#": (0.15, 1.0)}


class NotUniform(ValueError):
    """A module came out part ink and part paper — a striped rendering."""


@dataclass(frozen=True)
class Cell:
    """One terminal cell: what was printed there and in which colours."""

    char: str
    fg_ink: bool | None   # None = the terminal's own foreground
    bg_ink: bool | None   # None = the terminal's own background


def parse_cells(ansi: str) -> list[list[Cell]]:
    """One row of :class:`Cell` per line of ``ansi``.

    Rows are padded to the widest line with default-coloured spaces, so the
    grid is rectangular even when the emitter trims a line.
    """
    rows: list[list[Cell]] = []
    for line in ansi.split("\n"):
        fg: bool | None = None
        bg: bool | None = None
        row: list[Cell] = []
        at = 0
        for match in _SGR.finditer(line):
            for char in line[at:match.start()]:
                row.append(Cell(char, fg, bg))
            for part in (match.group(1) or "0").split(";"):
                code = int(part or "0")
                if code == 0:
                    fg = bg = None
                elif code == 39:
                    fg = None
                elif code == 49:
                    bg = None
                elif code in _FG:
                    fg = _FG[code]
                elif code in _BG:
                    bg = _BG[code]
                elif code in (38, 48) or code in _OTHER_COLOURS:
                    # 38/48 introduce a 256-colour or truecolor value; the
                    # rest are hues that are neither ink nor paper. Either
                    # way the renderer has left the named palette, which is
                    # the thing a 16-colour terminal cannot follow.
                    raise ValueError(
                        f"SGR {code} is not a 16-colour ink/paper code; the "
                        "QR renderer must stay inside the named palette")
                # Anything else is an attribute (bold, dim, italic…) that the
                # surrounding prose sets and that paints no colour.
            at = match.end()
        for char in line[at:]:
            row.append(Cell(char, fg, bg))
        rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    width = max((len(row) for row in rows), default=0)
    return [row + [Cell(" ", None, None)] * (width - len(row)) for row in rows]


def modules_from_backgrounds(rows: list[list[Cell]], *,
                             columns_per_module: int) -> list[list[bool]]:
    """The module matrix a background-painted block carries.

    The character-level read of the cell renderer: every module is a run of
    ``columns_per_module`` cells that agree on their background colour.
    """
    matrix: list[list[bool]] = []
    for row in rows:
        if len(row) % columns_per_module:
            raise ValueError(f"row of {len(row)} cells is not a whole number "
                             f"of {columns_per_module}-column modules")
        modules: list[bool] = []
        for start in range(0, len(row), columns_per_module):
            group = row[start:start + columns_per_module]
            inks = {cell.bg_ink for cell in group}
            if len(inks) != 1:
                raise NotUniform(f"module at column {start} is split between "
                                 f"backgrounds {inks}")
            ink = inks.pop()
            if ink is None:
                raise ValueError("a module was left on the terminal's own "
                                 "background — the paper has to be painted")
            modules.append(ink)
        matrix.append(modules)
    return matrix


def rasterise(rows: list[list[Cell]], *, cell_w: int, cell_h: int,
              glyph_h: int) -> list[list[bool]]:
    """Paint ``rows`` into a pixel grid; ``True`` is a dark pixel.

    ``glyph_h < cell_h`` is the whole point: a real terminal font paints its
    glyphs inside an ink box that does not reach the bottom of the cell, and
    the leftover rows keep the cell's background. A background colour has no
    such box, so it fills all ``cell_h`` rows.
    """
    if glyph_h > cell_h:
        raise ValueError("a glyph cannot be taller than the cell it sits in")
    pixels = [[False] * (len(rows[0]) * cell_w) for _ in range(len(rows) * cell_h)]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            top, left = r * cell_h, c * cell_w
            # The background fills the cell, gap included. An unpainted cell
            # takes the terminal's own background, which we model as light.
            for y in range(top, top + cell_h):
                for x in range(left, left + cell_w):
                    pixels[y][x] = bool(cell.bg_ink)
            coverage = _GLYPHS.get(cell.char)
            if coverage is None:
                if cell.char != " ":
                    raise ValueError(f"no glyph model for {cell.char!r}")
                continue
            ink = bool(cell.fg_ink)
            start = top + int(coverage[0] * glyph_h)
            end = top + int(coverage[1] * glyph_h)
            for y in range(start, end):
                for x in range(left, left + cell_w):
                    pixels[y][x] = ink
    return pixels


def modules_from_raster(pixels: list[list[bool]], *, modules_x: int,
                        modules_y: int) -> list[list[bool]]:
    """Sample ``pixels`` back onto its module grid.

    A module is ink only if every pixel in its block is dark, and paper only
    if every pixel is light — the threshold is set hard against a partly-lit
    module on purpose. A scanner derives its sampling grid from the finder
    patterns' run lengths, and a module cut by a stripe destroys those runs;
    demanding a uniform block is the simple, checkable stand-in.
    """
    height, width = len(pixels), len(pixels[0])
    if width % modules_x or height % modules_y:
        raise ValueError(f"{width}x{height} pixels is not a whole number of "
                         f"{modules_x}x{modules_y} modules")
    block_w, block_h = width // modules_x, height // modules_y
    matrix: list[list[bool]] = []
    for my in range(modules_y):
        row: list[bool] = []
        for mx in range(modules_x):
            block = [pixels[y][x]
                     for y in range(my * block_h, (my + 1) * block_h)
                     for x in range(mx * block_w, (mx + 1) * block_w)]
            if all(block):
                row.append(True)
            elif not any(block):
                row.append(False)
            else:
                raise NotUniform(
                    f"module ({mx}, {my}) is {sum(block)}/{len(block)} dark — "
                    "a striped cell, which is what a phone cannot read")
        matrix.append(row)
    return matrix
