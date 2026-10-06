#!/usr/bin/env python3
"""Draws watchcat's app icons (a minimalist cat face) with the standard library only.

    python3 tools/make_icons.py        # writes static/icon-192.png, icon-512.png, apple-touch-icon.png

Icons are full-bleed squares (the phone applies its own rounded/circular mask); the artwork stays inside
the central ~70 % "safe zone" so it survives any mask.
"""
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
BG, FACE, EAR_IN, DARK, GLOW = (12, 14, 19), (52, 211, 153), (24, 130, 98), (12, 14, 19), (60, 235, 175)


def inside_tri(px, py, a, b, c):
    def s(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = s((px, py), a, b), s((px, py), b, c), s((px, py), c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def ell(px, py, cx, cy, rx, ry):
    return ((px - cx) / rx) ** 2 + ((py - cy) / ry) ** 2 <= 1


def shade(x, y):
    """Colour at unit coordinates (0..1) or None for background."""
    # ears (outer, then inner), mirrored
    for sx in (1, -1):
        def m(px):
            return .5 + sx * (px - .5)
        outer = ((m(.25), .52), (m(.27), .19), (m(.45), .36))
        inner = ((m(.29), .47), (m(.30), .27), (m(.40), .38))
        if inside_tri(x, y, *inner):
            return EAR_IN
        if inside_tri(x, y, *outer):
            return FACE
    if ell(x, y, .5, .58, .27, .23):
        # eyes
        for ex in (.40, .60):
            if ell(x, y, ex, .56, .035, .055):
                return DARK
        # nose + mouth
        if inside_tri(x, y, (.47, .64), (.53, .64), (.5, .68)):
            return DARK
        if abs(x - .5) < .006 and .68 <= y <= .72:
            return DARK
        for sx in (1, -1):
            cx = .5 + sx * .035
            if ell(x, y, cx, .72, .036, .028) and y > .72 and (x - cx) * sx >= -.002:
                if not ell(x, y, cx, .72, .029, .021):
                    return DARK
        return FACE
    return None


def render(size, ss=2):
    n = size * ss
    rows = []
    for j in range(size):
        row = bytearray([0])  # PNG filter 0
        for i in range(size):
            r = g = b = 0
            for dj in range(ss):
                for di in range(ss):
                    x, y = (i * ss + di + .5) / n, (j * ss + dj + .5) / n
                    # soft radial glow behind the face
                    d = ((x - .5) ** 2 + (y - .56) ** 2) ** .5
                    k = max(0.0, 1 - d / .55) ** 2 * .22
                    c = shade(x, y) or tuple(int(BG[t] + (GLOW[t] - BG[t]) * k) for t in range(3))
                    r, g, b = r + c[0], g + c[1], b + c[2]
            q = ss * ss
            row += bytes((r // q, g // q, b // q))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(render(size))
        print("wrote", name)
