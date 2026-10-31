"""Build every Hollis brand asset from one geometric definition.

    uv run --no-project --with fonttools --with brotli --with pillow python brand/build.py FONT_DIR

FONT_DIR holds `ibm-plex-sans-latin-600-normal.woff2` (from the @fontsource/ibm-plex-sans npm
package; IBM Plex is licensed under the SIL Open Font License 1.1, which permits using its outlines
in artwork). Outputs go to brand/svg, brand/png and web/public. The script is deterministic.

The mark ("Continuation"): a hollow ring (the open task) joined by a sharp, right-angled path (a
terminal tree connector) to a solid square of equal visual weight (the recorded result).
Geometry lives on a 24-unit grid; a pixel-aligned 16-unit variant is used at 16 and 32 px.
"""

import io
import sys
from pathlib import Path

from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
SVG_DIR = ROOT / "svg"
PNG_DIR = ROOT / "png"
WEB_PUBLIC = ROOT.parent / "web" / "public"

INK = "#17140F"
PAPER = "#F6F3EC"
BRASS = "#B8862F"
BRASS_BRIGHT = "#D9A94E"
BLACK = "#000000"
WHITE = "#FFFFFF"

# --- geometry (24-unit grid) -------------------------------------------------------------------

TILE_RADIUS = 5.4
STROKE = 1.9
RING = (7.7, 7.7, 2.75)  # cx, cy, r (stroke centreline): a clearly open ring
STEM_X, STEM_TOP = 7.7, 10.45  # the path leaves the ring straight down...
BAR_Y, BAR_END = 16.6, 13.4  # ...turns a sharp corner (a terminal tree connector) to the square
SQUARE = (13.3, 13.3, 6.6, 0.7)  # x, y, size, corner radius: optically equal to the ring

# Pixel-aligned variant (16-unit grid) for 16 and 32 px.
SMALL = {
    "tile_radius": 3.5,
    "ring": (5.0, 5.0, 2.0, 2.0),  # cx, cy, r, stroke
    "stem": (4.0, 7.0, 6.0, 13.0),  # x0, y0, x1, y1
    "bar": (4.0, 11.0, 9.0, 13.0),
    "square": (9.0, 9.0, 15.0, 15.0),
}


def mark_svg(color: str) -> str:
    cx, cy, r = RING
    x, y, size, rx = SQUARE
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
        f'stroke-width="{STROKE}"/>'
        f'<path d="M{STEM_X} {STEM_TOP}V{BAR_Y}H{BAR_END}" fill="none" stroke="{color}" '
        f'stroke-width="{STROKE}"/>'
        f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="{rx}" fill="{color}"/>'
    )


def small_mark_svg(color: str) -> str:
    cx, cy, r, stroke = SMALL["ring"]
    sx0, sy0, sx1, _ = SMALL["stem"]  # the bar covers the stem's foot
    bx0, by0, bx1, by1 = SMALL["bar"]
    qx0, qy0, qx1, qy1 = SMALL["square"]
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}"/>'
        f'<path d="M{sx0} {sy0}H{sx1}V{by0}H{bx1}V{by1}H{bx0}Z" fill="{color}"/>'
        f'<rect x="{qx0}" y="{qy0}" width="{qx1 - qx0}" height="{qy1 - qy0}" fill="{color}"/>'
    )


def document(view: str, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" role="img" aria-label="{title}">'
        f"<title>{title}</title>{body}</svg>\n"
    )


def icon_svg(tile: str, mark: str, *, small: bool = False) -> str:
    if small:
        radius = SMALL["tile_radius"]
        body = f'<rect width="16" height="16" rx="{radius}" fill="{tile}"/>' + small_mark_svg(mark)
        return document("0 0 16 16", body, "Hollis")
    body = f'<rect width="24" height="24" rx="{TILE_RADIUS}" fill="{tile}"/>' + mark_svg(mark)
    return document("0 0 24 24", body, "Hollis")


# --- wordmark (IBM Plex Sans SemiBold outlines) --------------------------------------------------

TRACKING = -12  # font units (per 1000 em)


def wordmark_paths(font_path: Path) -> tuple[list[tuple[str, str]], float, float, float]:
    """Glyph outlines for "hollis" as (role, svg path) pairs in font units, y pointing down.

    The i's dot is returned separately (role "dot") so it can carry the accent colour: it is the
    mark's result square, set into the word."""
    font = TTFont(str(font_path))
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    ascent = font["OS/2"].sCapHeight
    x_height = font["OS/2"].sxHeight
    pieces: list[tuple[str, str]] = []
    cursor = 0.0
    for char in "hollis":
        name = cmap[ord(char)]
        recording = RecordingPen()
        glyphs[name].draw(recording)
        contours: list[list[tuple[str, tuple]]] = [[]]
        for operator, operands in recording.value:
            contours[-1].append((operator, operands))
            if operator in ("closePath", "endPath"):
                contours.append([])
        for contour in (c for c in contours if c):
            ys = [pt[1] for _, operands in contour for pt in operands]
            role = "dot" if char == "i" and ys and min(ys) > x_height * 0.9 else "letter"
            pen = SVGPathPen(glyphs)
            replay = TransformPen(pen, (1, 0, 0, -1, cursor, ascent))
            for operator, operands in contour:
                getattr(replay, operator)(*operands)
            pieces.append((role, pen.getCommands()))
        cursor += glyphs[name].width + TRACKING
    return pieces, cursor - TRACKING, float(ascent), float(x_height)


def wordmark_body(
    pieces: list[tuple[str, str]], letters: str, dot: str, dx: float = 0, dy: float = 0
) -> str:
    letter_path = " ".join(p for role, p in pieces if role == "letter")
    dot_path = " ".join(p for role, p in pieces if role == "dot")
    transform = f' transform="translate({dx:.2f} {dy:.2f})"' if dx or dy else ""
    return (
        f"<g{transform}>"
        f'<path d="{letter_path}" fill="{letters}"/>'
        f'<path d="{dot_path}" fill="{dot}"/>'
        "</g>"
    )


# --- raster rendering (Pillow, supersampled from the same geometry) -----------------------------


def _hex(color: str) -> tuple[int, int, int, int]:
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), 255)


def render_icon(size: int, tile: str, mark: str) -> Image.Image:
    small = size <= 32
    grid = 16 if small else 24
    scale = 8  # supersampling
    canvas = size * scale
    unit = canvas / grid
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = (SMALL["tile_radius"] if small else TILE_RADIUS) * unit
    draw.rounded_rectangle((0, 0, canvas - 1, canvas - 1), radius=radius, fill=_hex(tile))
    color = _hex(mark)
    if small:
        cx, cy, r, stroke = SMALL["ring"]
        outer, inner = (r + stroke / 2) * unit, (r - stroke / 2) * unit
        draw.ellipse(
            (cx * unit - outer, cy * unit - outer, cx * unit + outer, cy * unit + outer), fill=color
        )
        draw.ellipse(
            (cx * unit - inner, cy * unit - inner, cx * unit + inner, cy * unit + inner),
            fill=_hex(tile),
        )
        for x0, y0, x1, y1 in (SMALL["stem"], SMALL["bar"], SMALL["square"]):
            draw.rectangle((x0 * unit, y0 * unit, x1 * unit - 1, y1 * unit - 1), fill=color)
    else:
        cx, cy, r = RING
        outer, inner = (r + STROKE / 2) * unit, (r - STROKE / 2) * unit
        draw.ellipse(
            (cx * unit - outer, cy * unit - outer, cx * unit + outer, cy * unit + outer), fill=color
        )
        draw.ellipse(
            (cx * unit - inner, cy * unit - inner, cx * unit + inner, cy * unit + inner),
            fill=_hex(tile),
        )
        half = STROKE / 2 * unit
        draw.rectangle(
            (STEM_X * unit - half, STEM_TOP * unit, STEM_X * unit + half, BAR_Y * unit + half),
            fill=color,
        )
        draw.rectangle(
            (STEM_X * unit - half, BAR_Y * unit - half, BAR_END * unit, BAR_Y * unit + half),
            fill=color,
        )
        x, y, side, rx = SQUARE
        draw.rounded_rectangle(
            (x * unit, y * unit, (x + side) * unit, (y + side) * unit), radius=rx * unit, fill=color
        )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main(font_dir: Path) -> None:
    SVG_DIR.mkdir(parents=True, exist_ok=True)
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    WEB_PUBLIC.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    # Icons (tile + mark).
    files["icon.svg"] = icon_svg(BRASS, INK)
    files["icon-dark.svg"] = icon_svg(INK, BRASS_BRIGHT)
    files["icon-mono-black.svg"] = icon_svg(BLACK, WHITE)
    files["icon-mono-white.svg"] = icon_svg(WHITE, BLACK)
    files["icon-small.svg"] = icon_svg(BRASS, INK, small=True)
    files["icon-small-dark.svg"] = icon_svg(INK, BRASS_BRIGHT, small=True)

    # Marks (no tile).
    for name, color in (
        ("ink", INK),
        ("paper", PAPER),
        ("brass", BRASS),
        ("black", BLACK),
        ("white", WHITE),
    ):
        files[f"mark-{name}.svg"] = document("0 0 24 24", mark_svg(color), "Hollis")
    files["mark-current.svg"] = document("0 0 24 24", mark_svg("currentColor"), "Hollis")
    files["mark-small-current.svg"] = document(
        "0 0 16 16", small_mark_svg("currentColor"), "Hollis"
    )

    # Wordmark and lockups.
    pieces, width, cap, _ = wordmark_paths(font_dir / "ibm-plex-sans-latin-600-normal.woff2")
    descent_pad = 40
    height = cap + descent_pad
    for name, letters, dot in (
        ("ink", INK, BRASS),
        ("paper", PAPER, BRASS_BRIGHT),
        ("mono-black", BLACK, BLACK),
        ("mono-white", WHITE, WHITE),
    ):
        files[f"wordmark-{name}.svg"] = document(
            f"0 -20 {width:.0f} {height:.0f}", wordmark_body(pieces, letters, dot), "Hollis"
        )
    files["wordmark-current.svg"] = document(
        f"0 -20 {width:.0f} {height:.0f}",
        wordmark_body(pieces, "currentColor", "currentColor"),
        "Hollis",
    )
    # Horizontal lockup: icon height = 1.32 x cap height, gap = 0.36 x icon height.
    icon_size = cap * 1.32
    gap = icon_size * 0.36
    scale = icon_size / 24
    total = icon_size + gap + width
    top = -(icon_size - cap) / 2
    for name, tile, mark, letters, dot in (
        ("ink", BRASS, INK, INK, BRASS),
        ("paper", BRASS_BRIGHT, INK, PAPER, BRASS_BRIGHT),
        ("mono-black", BLACK, WHITE, BLACK, BLACK),
        ("mono-white", WHITE, BLACK, WHITE, WHITE),
    ):
        tile_group = (
            f'<g transform="translate(0 {top:.2f}) scale({scale:.4f})">'
            f'<rect width="24" height="24" rx="{TILE_RADIUS}" fill="{tile}"/>{mark_svg(mark)}</g>'
        )
        files[f"lockup-{name}.svg"] = document(
            f"0 {top:.0f} {total:.0f} {icon_size:.0f}",
            tile_group + wordmark_body(pieces, letters, dot, dx=icon_size + gap),
            "Hollis",
        )
    for path_name, content in files.items():
        (SVG_DIR / path_name).write_text(content)

    # Rasters.
    sizes = [16, 32, 48, 64, 128, 180, 192, 256, 512, 1024]
    for size in sizes:
        render_icon(size, BRASS, INK).save(PNG_DIR / f"icon-{size}.png", optimize=True)
        render_icon(size, INK, BRASS_BRIGHT).save(PNG_DIR / f"icon-dark-{size}.png", optimize=True)
    favicon = io.BytesIO()
    render_icon(48, BRASS, INK).save(
        favicon,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
        append_images=[render_icon(16, BRASS, INK), render_icon(32, BRASS, INK)],
    )
    (PNG_DIR / "favicon.ico").write_bytes(favicon.getvalue())

    # Web application copies.
    (WEB_PUBLIC / "favicon.ico").write_bytes(favicon.getvalue())
    (WEB_PUBLIC / "favicon.svg").write_text(files["icon-small.svg"])
    (WEB_PUBLIC / "apple-touch-icon.png").write_bytes((PNG_DIR / "icon-180.png").read_bytes())
    (WEB_PUBLIC / "icon-192.png").write_bytes((PNG_DIR / "icon-192.png").read_bytes())
    (WEB_PUBLIC / "icon-512.png").write_bytes((PNG_DIR / "icon-512.png").read_bytes())
    print(f"wrote {len(files)} SVGs, {len(sizes) * 2} PNGs and favicon.ico")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
