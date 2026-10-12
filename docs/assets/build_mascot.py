"""Build the pixel elephant from docs/assets/mascot-grid.txt, with the standard library only.

Writes three files to src/omsync/web/static/img/:

- mascot.svg: the mascot on its grid. The outline and eye use currentColor, and the body uses the
  --mascot-body variable (set in styles/brand.css). The eye blinks with CSS.
- favicon.svg: the mascot on a white 32 x 32 tile, with fixed colours.
- favicon-32.png: the same tile as a PNG, drawn cell for cell.

Grid characters: '#' is ink (the outline, the coin and the dollar sign), 'E' is the eye, and '.' is
paper. Paper that touches the grid edge, or that is reached from it through paper, is exterior and
is not drawn. The rest of the paper is body. Run it from the repository root:

    python3 docs/assets/build_mascot.py
"""

import struct
import zlib
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GRID_PATH = ROOT / "docs" / "assets" / "mascot-grid.txt"
OUT_DIR = ROOT / "src" / "omsync" / "web" / "static" / "img"

COLS, ROWS = 32, 22
INK = "#0b0b0b"  # Sure's black token
BODY = "#d3d7d9"  # the app's --mascot-body variable takes over in the page
TILE_FILL = "#ffffff"
TILE = 32
TILE_RADIUS = 6
# The mascot is as wide as the tile and centred on its height, so it is drawn one cell per pixel.
TILE_ORIGIN = (0, (TILE - ROWS) // 2)
CLOSED_EYE_HEIGHT = 0.3  # the closed eye is a thin line across the middle of the open eye

Kind = str | None  # "ink", "eye", "body", or None for exterior


def read_grid(path: Path) -> list[str]:
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    if len(rows) != ROWS or any(len(row) != COLS for row in rows):
        raise ValueError(f"{path} must hold {ROWS} rows of {COLS} characters")
    unknown = {char for row in rows for char in row} - set("#E.")
    if unknown:
        raise ValueError(f"{path} has unknown characters: {sorted(unknown)}")
    return rows


def classify(rows: list[str]) -> list[list[Kind]]:
    exterior: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    for y in range(ROWS):
        for x in range(COLS):
            on_edge = x in (0, COLS - 1) or y in (0, ROWS - 1)
            if on_edge and rows[y][x] == ".":
                exterior.add((x, y))
                queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            inside = 0 <= nx < COLS and 0 <= ny < ROWS
            if inside and rows[ny][nx] == "." and (nx, ny) not in exterior:
                exterior.add((nx, ny))
                queue.append((nx, ny))

    kinds: list[list[Kind]] = []
    for y in range(ROWS):
        line: list[Kind] = []
        for x in range(COLS):
            char = rows[y][x]
            if char == "#":
                line.append("ink")
            elif char == "E":
                line.append("eye")
            elif (x, y) in exterior:
                line.append(None)
            else:
                line.append("body")
        kinds.append(line)
    return kinds


def runs(kinds: list[list[Kind]], wanted: str) -> list[tuple[int, int, int]]:
    """Horizontal runs of one kind, as (x, y, width). Each run is one rectangle in the SVG."""
    found = []
    for y, line in enumerate(kinds):
        x = 0
        while x < COLS:
            if line[x] == wanted:
                start = x
                while x < COLS and line[x] == wanted:
                    x += 1
                found.append((start, y, x - start))
            else:
                x += 1
    return found


def eye_block(kinds: list[list[Kind]]) -> tuple[int, int, int, int]:
    """The eye's bounding box as (x, y, width, height), in cells."""
    cells = [(x, y) for y, line in enumerate(kinds) for x, kind in enumerate(line) if kind == "eye"]
    if not cells:
        raise ValueError("the grid has no eye cells (E)")
    xs = [x for x, _ in cells]
    ys = [y for _, y in cells]
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def rect(x: float, y: float, width: float, height: float, extra: str = "") -> str:
    return f'<rect x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}"{extra}/>'


def mascot_svg(kinds: list[list[Kind]]) -> str:
    """The reusable mascot. Colours come from CSS: currentColor and the body variable."""
    body = "".join(rect(x, y, w, 1) for x, y, w in runs(kinds, "body"))
    ink = "".join(rect(x, y, w, 1) for x, y, w in runs(kinds, "ink"))
    eye = "".join(rect(x, y, w, 1) for x, y, w in runs(kinds, "eye"))
    x, y, w, h = eye_block(kinds)
    # The closed eye is hidden by its opacity attribute until the CSS blink runs it.
    closed = rect(
        x,
        round(y + h / 2 - CLOSED_EYE_HEIGHT / 2, 3),
        w,
        CLOSED_EYE_HEIGHT,
        ' class="mascot-eye-closed" opacity="0"',
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {COLS} {ROWS}" '
        'shape-rendering="crispEdges">\n'
        f'<g class="mascot-body" fill="{BODY}">{body}</g>\n'
        f'<g fill="currentColor">{ink}</g>\n'
        f'<g class="mascot-eye" fill="currentColor">{eye}</g>\n'
        f"{closed}\n"
        "</svg>\n"
    )


def colour_for(kind: Kind) -> str | None:
    return {"ink": INK, "eye": INK, "body": BODY}.get(kind or "")


def favicon_svg(kinds: list[list[Kind]]) -> str:
    """The favicon: the same cells with fixed colours, on a rounded white tile."""
    ox, oy = TILE_ORIGIN
    parts = [rect(0, 0, TILE, TILE, f' rx="{TILE_RADIUS}" fill="{TILE_FILL}"')]
    for wanted, colour in (("body", BODY), ("ink", INK), ("eye", INK)):
        cells = "".join(rect(x + ox, y + oy, w, 1) for x, y, w in runs(kinds, wanted))
        parts.append(f'<g fill="{colour}">{cells}</g>')
    body = "\n".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {TILE} {TILE}" '
        'shape-rendering="crispEdges">\n'
        f"{body}\n"
        "</svg>\n"
    )


def hex_rgb(colour: str) -> tuple[int, int, int]:
    value = int(colour.lstrip("#"), 16)
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def in_tile(x: int, y: int) -> bool:
    """False in the four corners outside the rounded tile."""
    cx = min(max(x + 0.5, TILE_RADIUS), TILE - TILE_RADIUS)
    cy = min(max(y + 0.5, TILE_RADIUS), TILE - TILE_RADIUS)
    return (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= TILE_RADIUS**2


def favicon_png(kinds: list[list[Kind]]) -> bytes:
    ox, oy = TILE_ORIGIN
    raw = bytearray()
    for y in range(TILE):
        raw.append(0)  # filter type: none
        for x in range(TILE):
            if not in_tile(x, y):
                raw += bytes((0, 0, 0, 0))
                continue
            gx, gy = x - ox, y - oy
            kind = kinds[gy][gx] if 0 <= gx < COLS and 0 <= gy < ROWS else None
            colour = colour_for(kind) or TILE_FILL
            raw += bytes((*hex_rgb(colour), 255))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", TILE, TILE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    kinds = classify(read_grid(GRID_PATH))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "mascot.svg").write_text(mascot_svg(kinds), encoding="utf-8")
    (OUT_DIR / "favicon.svg").write_text(favicon_svg(kinds), encoding="utf-8")
    (OUT_DIR / "favicon-32.png").write_bytes(favicon_png(kinds))
    print(f"wrote mascot.svg, favicon.svg and favicon-32.png to {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
