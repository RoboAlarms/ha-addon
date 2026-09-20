#!/usr/bin/env python3
"""Generate the placeholder brand icon (shield + check) without Pillow.

Renders at 6x (1536px) with scanline polygon fill, box-downsamples to
icon.png (256x256) and icon@2x.png (512x512) in
custom_components/roboalarms/brand/. Standard library only.

This is a placeholder until the project has real branding; rerun after
tweaking the shapes below.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

S = 6  # supersampling factor relative to the 256-space coordinates below
BIG = 256 * S

NAVY = (20, 38, 63, 255)  # background square
WHITE = (237, 242, 247, 255)  # shield
TEAL = (23, 184, 166, 255)  # check mark
CLEAR = (0, 0, 0, 0)


def bezier(p0, c, p1, steps=32):
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1.0 - t
        pts.append(
            (
                u * u * p0[0] + 2 * u * t * c[0] + t * t * p1[0],
                u * u * p0[1] + 2 * u * t * c[1] + t * t * p1[1],
            )
        )
    return pts


def arc(cx, cy, r, a0, a1, steps=24):
    return [
        (
            cx + r * math.cos(a0 + (a1 - a0) * i / steps),
            cy + r * math.sin(a0 + (a1 - a0) * i / steps),
        )
        for i in range(steps + 1)
    ]


def rounded_rect(x0, y0, x1, y1, r):
    pts = []
    pts += arc(x1 - r, y0 + r, r, -math.pi / 2, 0)
    pts += arc(x1 - r, y1 - r, r, 0, math.pi / 2)
    pts += arc(x0 + r, y1 - r, r, math.pi / 2, math.pi)
    pts += arc(x0 + r, y0 + r, r, math.pi, 3 * math.pi / 2)
    return pts


def shield():
    top_y, side_y, tip = 66.0, 128.0, (128.0, 198.0)
    left, right = 80.0, 176.0
    pts = [(left, top_y), (right, top_y), (right, side_y)]
    pts += bezier((right, side_y), (right - 6, 174.0), tip)
    pts += bezier(tip, (left + 6, 174.0), (left, side_y))
    return pts


def thick_polyline(points, width):
    """Return polygons (quads + round joints) covering the polyline."""
    half = width / 2.0
    polys = []
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        nx, ny = -dy / length * half, dx / length * half
        polys.append(
            [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
        )
    for x, y in points:
        polys.append(arc(x, y, half, 0, 2 * math.pi, 24))
    return polys


def fill_polygon(px, pts, color):
    scaled = [(x * S, y * S) for x, y in pts]
    ys = [y for _, y in scaled]
    for row in range(max(0, int(min(ys))), min(BIG, int(max(ys)) + 2)):
        yc = row + 0.5
        xs = []
        n = len(scaled)
        for i in range(n):
            x0, y0 = scaled[i]
            x1, y1 = scaled[(i + 1) % n]
            if (y0 <= yc < y1) or (y1 <= yc < y0):
                xs.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
        xs.sort()
        for a, b in zip(xs[::2], xs[1::2], strict=False):
            for col in range(max(0, int(a + 0.5)), min(BIG, int(b + 0.5))):
                px[row * BIG + col] = color


def downsample(px, factor):
    size = BIG // factor
    out = bytearray(size * size * 4)
    area = factor * factor
    for row in range(size):
        for col in range(size):
            r = g = b = a = 0
            for dy in range(factor):
                base = (row * factor + dy) * BIG + col * factor
                for dx in range(factor):
                    pr, pg, pb, pa = px[base + dx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            i = (row * size + col) * 4
            if a:
                out[i : i + 4] = bytes((r // a, g // a, b // a, a // area))
    return size, out


def write_png(path, size, rgba):
    def chunk(tag, data):
        block = tag + data
        return struct.pack(">I", len(data)) + block + struct.pack(">I", zlib.crc32(block))

    stride = size * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride : (y + 1) * stride]) for y in range(size))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main():
    px = [CLEAR] * (BIG * BIG)
    fill_polygon(px, rounded_rect(8, 8, 248, 248, 56), NAVY)
    fill_polygon(px, shield(), WHITE)
    for poly in thick_polyline([(103.0, 125.0), (121.0, 143.0), (154.0, 103.0)], 15.0):
        fill_polygon(px, poly, TEAL)

    out_dir = Path(__file__).resolve().parents[1] / "custom_components" / "roboalarms" / "brand"
    out_dir.mkdir(parents=True, exist_ok=True)
    size, data = downsample(px, S)
    write_png(out_dir / "icon.png", size, data)
    size, data = downsample(px, S // 2)
    write_png(out_dir / "icon@2x.png", size, data)
    print(f"wrote {out_dir / 'icon.png'} and icon@2x.png")


if __name__ == "__main__":
    main()
