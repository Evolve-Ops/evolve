#!/usr/bin/env python3
"""Regenerate PWA + favicon images from ``artwork/evolve_mobius_white.svg``.

Why this script exists
----------------------
The PWA install icons (Phase 1.1.A, PR #1363) were originally produced as a
one-off from a dark-on-transparent rendering of the Möbius logo, which made
the macOS Dock icon look like a black blob on the Dock's near-black bar. The
real artwork is intentionally light-coloured for dark backdrops — so this
script composites the white Möbius onto a solid accent-purple square that
reads well on every OS theme (light/dark, iOS/macOS/Android/Chrome).

Re-run it whenever the logo or background colour changes. It overwrites the
checked-in PNGs in place; commit the result alongside any artwork change.

Dependency (one-time)
---------------------
    brew install librsvg          # provides /usr/local/bin/rsvg-convert

Usage
-----
From the repo root:
    python3 scripts/gen_pwa_icons.py

Outputs
-------
PWA install icons (referenced by /manifest.json):
    packages/admin/evolve_admin/web/static/icons/icon-192.png            (192×192, any)
    packages/admin/evolve_admin/web/static/icons/icon-512.png            (512×512, any)
    packages/admin/evolve_admin/web/static/icons/icon-512-maskable.png   (512×512, maskable, 20 % safe-area padding)
    packages/admin/evolve_admin/web/static/icons/apple-touch-icon-180.png (180×180)

Legacy favicons (served from the web/ root by routes in server.py):
    packages/admin/evolve_admin/web/favicon-16x16.png
    packages/admin/evolve_admin/web/favicon-32x32.png
    packages/admin/evolve_admin/web/favicon.ico                          (multi-size: 16, 32, 48)
    packages/admin/evolve_admin/web/apple-touch-icon.png                 (180×180, mirrors the static/icons/ copy)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - script-time dep check
    print("error: Pillow is required (pip install Pillow)", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_SVG = REPO_ROOT / "artwork" / "evolve_mobius_white.svg"

WEB_DIR = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
ICONS_DIR = WEB_DIR / "static" / "icons"

# Admin UI's --accent CSS variable (see web/index.html). Strong, on-brand,
# and high-contrast against both light- and dark-mode Dock/launcher
# backgrounds.
DEFAULT_BG = "#7C5CFF"

# Fraction of canvas width the rendered logo occupies. The Möbius SVG is
# ~2.7 : 1 (wide), so width is the binding dimension.
#
# "any" purpose: 0.80 → comfortable breathing room, readable favicon-small.
# "maskable":    0.66 → the logo's *diagonal* must fit inside the inner-80 %
#                       circle the OS may crop to. With w = 0.66·canvas, the
#                       diagonal ≈ 0.70·canvas — safely inside the 0.80
#                       safe-zone diameter.
LOGO_FRACTION_ANY = 0.80
LOGO_FRACTION_MASKABLE = 0.66


def _check_rsvg() -> Path:
    exe = shutil.which("rsvg-convert")
    if not exe:
        print(
            "error: rsvg-convert not found on PATH.\n"
            "       Install with:  brew install librsvg",
            file=sys.stderr,
        )
        sys.exit(1)
    return Path(exe)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) != 6:
        raise ValueError(f"expected 6-digit hex colour, got {color!r}")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _render_svg(rsvg: Path, svg: Path, width: int) -> Image.Image:
    """Run rsvg-convert at the requested pixel width, return a Pillow image."""
    proc = subprocess.run(
        [str(rsvg), "-w", str(width), "-f", "png", str(svg)],
        capture_output=True,
        check=True,
    )
    from io import BytesIO

    return Image.open(BytesIO(proc.stdout)).convert("RGBA")


def _composite(
    rsvg: Path,
    *,
    canvas_size: int,
    logo_fraction: float,
    bg_color: str,
) -> Image.Image:
    """Render the SVG at ``logo_fraction × canvas_size`` and centre it on a
    solid background of ``canvas_size × canvas_size``.
    """
    logo_w = max(1, int(round(canvas_size * logo_fraction)))
    logo = _render_svg(rsvg, SOURCE_SVG, logo_w)

    canvas = Image.new("RGBA", (canvas_size, canvas_size), _hex_to_rgb(bg_color) + (255,))
    x = (canvas_size - logo.width) // 2
    y = (canvas_size - logo.height) // 2
    canvas.alpha_composite(logo, dest=(x, y))
    return canvas


def _save_png(img: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG", optimize=True)
    print(f"  wrote {dest.relative_to(REPO_ROOT)}  ({img.width}×{img.height})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bg",
        default=DEFAULT_BG,
        help=f"background colour as #RRGGBB (default: {DEFAULT_BG}, matches --accent)",
    )
    args = parser.parse_args()

    if not SOURCE_SVG.is_file():
        print(f"error: source SVG not found at {SOURCE_SVG}", file=sys.stderr)
        return 1

    rsvg = _check_rsvg()
    bg = args.bg

    print(f"Source : {SOURCE_SVG.relative_to(REPO_ROOT)}")
    print(f"BG     : {bg}")
    print()

    # PWA install icons ----------------------------------------------------
    print("PWA install icons:")
    _save_png(
        _composite(rsvg, canvas_size=192, logo_fraction=LOGO_FRACTION_ANY, bg_color=bg),
        ICONS_DIR / "icon-192.png",
    )
    _save_png(
        _composite(rsvg, canvas_size=512, logo_fraction=LOGO_FRACTION_ANY, bg_color=bg),
        ICONS_DIR / "icon-512.png",
    )
    _save_png(
        _composite(rsvg, canvas_size=512, logo_fraction=LOGO_FRACTION_MASKABLE, bg_color=bg),
        ICONS_DIR / "icon-512-maskable.png",
    )
    _save_png(
        _composite(rsvg, canvas_size=180, logo_fraction=LOGO_FRACTION_ANY, bg_color=bg),
        ICONS_DIR / "apple-touch-icon-180.png",
    )

    # Legacy favicons ------------------------------------------------------
    print("\nLegacy favicons:")
    fav32 = _composite(rsvg, canvas_size=32, logo_fraction=LOGO_FRACTION_ANY, bg_color=bg)
    fav16 = _composite(rsvg, canvas_size=16, logo_fraction=LOGO_FRACTION_ANY, bg_color=bg)
    fav48 = _composite(rsvg, canvas_size=48, logo_fraction=LOGO_FRACTION_ANY, bg_color=bg)
    _save_png(fav16, WEB_DIR / "favicon-16x16.png")
    _save_png(fav32, WEB_DIR / "favicon-32x32.png")
    _save_png(
        _composite(rsvg, canvas_size=180, logo_fraction=LOGO_FRACTION_ANY, bg_color=bg),
        WEB_DIR / "apple-touch-icon.png",
    )

    # Multi-resolution .ico — Pillow writes the supplied sizes from the
    # base image; we hand it the 48-px source so all three subsidiary sizes
    # have a clean origin.
    ico_dest = WEB_DIR / "favicon.ico"
    fav48.save(
        ico_dest,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )
    print(f"  wrote {ico_dest.relative_to(REPO_ROOT)}  (16, 32, 48)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
