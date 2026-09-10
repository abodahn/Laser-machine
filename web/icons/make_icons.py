"""Regenerate the PWA icons: python web/icons/make_icons.py

Pillow is a dev-time tool only, never a runtime dependency — the PNGs are
committed. Drawn geometrically rather than by rendering the 🎯 emoji, because a
colour emoji font exists on this Windows box and not in the Render Linux image.
Colours are lifted verbatim from web/style.css :root.
"""
from PIL import Image, ImageDraw

BG, RED, INK = "#070d18", "#d03b3b", "#eef4fb"
RINGS = [(1.00, RED), (0.78, INK), (0.56, RED), (0.34, INK), (0.16, RED)]
SS = 4  # supersample: PIL's ellipse is aliased, so draw big and downscale


def target(size, fill):
    """fill = target diameter as a fraction of the canvas."""
    n = size * SS
    im = Image.new("RGB", (n, n), BG)   # RGB, not RGBA: iOS shows black behind alpha
    d = ImageDraw.Draw(im)
    c, r = n / 2, n * fill / 2
    for frac, col in RINGS:
        rr = r * frac
        d.ellipse([c - rr, c - rr, c + rr, c + rr], fill=col)
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    import pathlib
    here = pathlib.Path(__file__).parent
    for name, size, fill in [
        ("icon-192.png", 192, 0.84),
        ("icon-512.png", 512, 0.84),
        ("icon-180.png", 180, 0.84),          # apple-touch: iOS rounds the corners itself
        ("icon-512-maskable.png", 512, 0.62),  # inside the centre 80% safe zone
    ]:
        p = here / name
        target(size, fill).save(p, optimize=True)
        print(p, p.stat().st_size, "bytes")
