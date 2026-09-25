"""Draw the toolbar icon at the four sizes Chrome asks for.

A rounded square in the house green with a white tick: the button says
"compliance", and at 16 pixels a tick is the only mark that still reads.
Drawn at 8x and averaged down, because a tick drawn directly at 16 pixels is
a staircase.

Written with zlib and struct so the repository needs no image library to
rebuild them.
"""
import struct
import zlib
from pathlib import Path

GREEN = (1, 169, 130)
WHITE = (255, 255, 255)
SUPERSAMPLE = 8


def rounded_square(x, y, size, radius):
    """Is this point inside a rounded square inset in a size x size box?"""
    if x < radius and y < radius:
        return (x - radius) ** 2 + (y - radius) ** 2 <= radius ** 2
    if x > size - radius and y < radius:
        return (x - (size - radius)) ** 2 + (y - radius) ** 2 <= radius ** 2
    if x < radius and y > size - radius:
        return (x - radius) ** 2 + (y - (size - radius)) ** 2 <= radius ** 2
    if x > size - radius and y > size - radius:
        return (x - (size - radius)) ** 2 + (y - (size - radius)) ** 2 <= radius ** 2
    return True


def near_segment(px, py, ax, ay, bx, by):
    """Distance from a point to the line segment ab."""
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** 0.5


def draw(size):
    """One icon, as rows of (r, g, b, a) tuples."""
    big = size * SUPERSAMPLE
    radius = big * 0.22
    # The tick, in fractions of the box: down-stroke then the long up-stroke.
    ax, ay = big * 0.28, big * 0.52
    mx, my = big * 0.44, big * 0.68
    bx, by = big * 0.74, big * 0.34
    stroke = big * 0.085

    coarse = [[[0, 0, 0, 0] for _ in range(size)] for _ in range(size)]
    for y in range(big):
        for x in range(big):
            px, py = x + 0.5, y + 0.5
            if not rounded_square(px, py, big, radius):
                continue
            on_tick = (
                near_segment(px, py, ax, ay, mx, my) <= stroke
                or near_segment(px, py, mx, my, bx, by) <= stroke
            )
            colour = WHITE if on_tick else GREEN
            cell = coarse[y // SUPERSAMPLE][x // SUPERSAMPLE]
            cell[0] += colour[0]
            cell[1] += colour[1]
            cell[2] += colour[2]
            cell[3] += 255

    per_cell = SUPERSAMPLE * SUPERSAMPLE
    rows = []
    for row in coarse:
        out = []
        for r, g, b, a in row:
            if a == 0:
                out.append((0, 0, 0, 0))
                continue
            # Colour is the average of the pixels that were covered, so the
            # edge keeps its hue instead of darkening towards black.
            covered = a // 255
            out.append((r // covered, g // covered, b // covered, a // per_cell))
        rows.append(out)
    return rows


def write_png(path, rows):
    width, height = len(rows[0]), len(rows)
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBBB", *pixel) for pixel in row) for row in rows
    )

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


here = Path(__file__).resolve()
icons = Path("icons")
icons.mkdir(exist_ok=True)
for size in (16, 32, 48, 128):
    target = icons / f"icon{size}.png"
    write_png(target, draw(size))
    print(f"{target}  {target.stat().st_size} bytes")
