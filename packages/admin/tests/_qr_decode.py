"""A minimal QR decoder, written for the tests and used by nothing else.

The board's phone link is only useful if a camera pointed at the terminal
recovers the URL. Comparing our rendered block against the encoder's own
matrix would only prove the two agree; it would not catch the encoder and
the renderer being wrong together, and it says nothing about what a scanner
reads. So the tests decode the printed characters back to a string, with an
implementation that shares no code with ``qrcode``.

Scope, deliberately narrow — exactly what ``qr_terminal.qr_matrix`` emits:

  * one byte-mode segment (``qr_matrix`` passes ``optimize=0``);
  * a single Reed-Solomon block, so the codewords are not interleaved.
    Versions 1-5 at ECC L are single-block, and a board link is version 5;
    ``decode`` raises for anything larger rather than returning nonsense.

Error correction is not applied: the input is a matrix we generated, not a
photograph, so any bit error is a bug we want to see rather than repair.

Reference: ISO/IEC 18004 §7.7 (symbol placement), §7.8 (format information),
§7.9 (masking).
"""
from __future__ import annotations

# Alignment-pattern centre coordinates by version (ISO/IEC 18004 annex E).
# Versions 1-10 only; ``decode`` refuses larger symbols anyway.
_ALIGNMENT_CENTRES = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}

# Reed-Solomon blocks per version at ECC L. More than one means the codewords
# are interleaved and this decoder's flat read order is wrong.
_L_BLOCKS = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 2, 7: 2, 8: 2, 9: 2, 10: 4}

_MASKS = (
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
)

_FORMAT_XOR = 0b101010000010010
_MODE_BYTE = 0b0100


def strip_quiet_zone(matrix: list[list[bool]]) -> list[list[bool]]:
    """Drop the all-light border rows/columns around the symbol.

    Safe because every corner of a symbol proper holds a finder pattern,
    whose outer ring is dark — no edge of the code itself is all-light.
    """
    rows = [r for r in matrix]
    while rows and not any(rows[0]):
        rows.pop(0)
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows:
        raise ValueError("matrix is entirely light — no symbol to decode")
    left = 0
    while all(not r[left] for r in rows):
        left += 1
    right = len(rows[0]) - 1
    while all(not r[right] for r in rows):
        right -= 1
    return [r[left:right + 1] for r in rows]


def _function_modules(size: int, version: int) -> list[list[bool]]:
    """True where a module is structure, not data."""
    fn = [[False] * size for _ in range(size)]

    def fill(top: int, left: int, height: int, width: int) -> None:
        for i in range(top, top + height):
            for j in range(left, left + width):
                fn[i][j] = True

    # Finder patterns with their separators, and the format-information
    # strips that share those edges.
    fill(0, 0, 9, 9)
    fill(0, size - 8, 9, 8)
    fill(size - 8, 0, 8, 9)
    # Timing patterns.
    for k in range(size):
        fn[6][k] = True
        fn[k][6] = True
    # Alignment patterns, except where they would sit on a finder.
    centres = _ALIGNMENT_CENTRES[version]
    for r in centres:
        for c in centres:
            if (r, c) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            fill(r - 2, c - 2, 5, 5)
    # Version information (version 7+).
    if version >= 7:
        fill(0, size - 11, 6, 3)
        fill(size - 11, 0, 3, 6)
    return fn


def _read_format_mask(matrix: list[list[bool]], size: int) -> int | None:
    """The mask pattern from the top-left format-information copy, or None."""
    # Most significant bit first, walking the row-8 strip left to right and
    # then up column 8, skipping the timing module at (6, 8) and the dark
    # module's column neighbour at (8, 6).
    positions = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                 (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    bits = 0
    for row, col in positions:
        bits = (bits << 1) | int(matrix[row][col])
    return ((bits ^ _FORMAT_XOR) >> 10) & 0b111


def _data_bits(matrix: list[list[bool]], fn: list[list[bool]],
               size: int, mask: int) -> list[int]:
    """Unmasked data bits in symbol read order (ISO/IEC 18004 §7.7.3)."""
    mask_fn = _MASKS[mask]
    bits: list[int] = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:  # the vertical timing pattern is not a data column
            col -= 1
        for step in range(size):
            row = (size - 1 - step) if upward else step
            for c in (col, col - 1):
                if fn[row][c]:
                    continue
                bit = int(matrix[row][c])
                if mask_fn(row, c):
                    bit ^= 1
                bits.append(bit)
        upward = not upward
        col -= 2
    return bits


def _take(bits: list[int], start: int, count: int) -> int:
    if start + count > len(bits):
        raise ValueError("ran out of data bits")
    value = 0
    for bit in bits[start:start + count]:
        value = (value << 1) | bit
    return value


def _parse_byte_segment(bits: list[int], version: int) -> str:
    if _take(bits, 0, 4) != _MODE_BYTE:
        raise ValueError("not a single byte-mode segment")
    count_bits = 8 if version <= 9 else 16
    length = _take(bits, 4, count_bits)
    at = 4 + count_bits
    payload = bytes(_take(bits, at + 8 * k, 8) for k in range(length))
    return payload.decode("utf-8")


def decode(matrix: list[list[bool]]) -> str:
    """Decode a single-block, byte-mode QR matrix (quiet zone optional)."""
    symbol = strip_quiet_zone(matrix)
    size = len(symbol)
    if size != len(symbol[0]) or size < 21 or (size - 17) % 4:
        raise ValueError(f"not a square QR symbol: {size}x{len(symbol[0])}")
    version = (size - 17) // 4
    if version > 10 or _L_BLOCKS[version] != 1:
        raise ValueError(
            f"version {version} interleaves multiple RS blocks; this decoder "
            "reads a single block (see the module docstring)")

    fn = _function_modules(size, version)
    candidates = [_read_format_mask(symbol, size)]
    # The format bits carry their own BCH correction, which we do not apply;
    # if the declared mask does not yield a coherent segment, try them all
    # rather than reporting a decode failure for a symbol that is fine.
    candidates += [m for m in range(8) if m != candidates[0]]
    errors = []
    for mask in candidates:
        try:
            return _parse_byte_segment(_data_bits(symbol, fn, size, mask),
                                       version)
        except (ValueError, UnicodeDecodeError) as exc:
            errors.append(f"mask {mask}: {exc}")
    raise ValueError("no mask decoded to a byte segment — " + "; ".join(errors))


def read_screen(lines: list[str]) -> list[list[bool]]:
    """The module matrix a camera would see in a rendered block.

    The inverse of ``qr_terminal``'s two renderers, written here rather than
    imported so that a renderer bug cannot cancel itself out: an inverted or
    transposed block decodes to garbage instead of round-tripping.
    """
    half_blocks = {"█": (True, True), "▀": (True, False),
                   "▄": (False, True), " ": (False, False)}
    if any(ch in "█▀▄" for line in lines for ch in line):
        matrix: list[list[bool]] = []
        for line in lines:
            pairs = [half_blocks[ch] for ch in line]
            matrix.append([top for top, _ in pairs])
            matrix.append([bottom for _, bottom in pairs])
        return matrix
    # ASCII form: two columns per module, one line per module row.
    return [[line[2 * i] == "#" for i in range(len(line) // 2)]
            for line in lines]
