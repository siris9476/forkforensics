"""Regenerates icon.ico and docs/logo.png from the brand mark (an "FF"
monogram, built from plain rounded rectangles rather than relying on a
system font, so the mark renders identically on any machine that runs this
script) redrawn with PIL instead of depending on an external SVG
rasterizer or a bundled font file. Only needs to be rerun if the logo
changes: python desktop/resources/make_icon.py (requires Pillow, see
requirements-build.txt)."""

from pathlib import Path

from PIL import Image, ImageDraw

BG = (22, 19, 15, 255)       # #16130f
ACCENT = (217, 154, 78, 255)  # #d99a4e

SCALE = 40  # viewBox 32x32 -> canvas 1280x1280, then downscale per-size with antialiasing
CANVAS = 32 * SCALE

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def draw_base() -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def pt(x, y):
        return (x * SCALE, y * SCALE)

    d.rounded_rectangle([pt(0, 0), pt(32, 32)], radius=8 * SCALE, fill=BG)

    f_w, f_h = 8.0, 19.0
    stem_w, top_h, mid_h, mid_w = 2.9, 3.4, 2.8, 6.1
    gap = 2.4
    corner = 0.55 * SCALE

    total_w = 2 * f_w + gap
    start_x = (32 - total_w) / 2
    top_y = (32 - f_h) / 2

    for i in range(2):
        ox = start_x + i * (f_w + gap)
        d.rounded_rectangle([pt(ox, top_y), pt(ox + stem_w, top_y + f_h)], radius=corner, fill=ACCENT)
        d.rounded_rectangle([pt(ox, top_y), pt(ox + f_w, top_y + top_h)], radius=corner, fill=ACCENT)
        mid_top = top_y + f_h * 0.42
        d.rounded_rectangle([pt(ox, mid_top), pt(ox + mid_w, mid_top + mid_h)], radius=corner, fill=ACCENT)

    return img


if __name__ == "__main__":
    base = draw_base()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [base.resize((s, s), Image.LANCZOS) for s in sizes]

    icon_path = REPO_ROOT / "desktop" / "resources" / "icon.ico"
    imgs[0].save(icon_path, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[1:])

    logo_path = REPO_ROOT / "docs" / "logo.png"
    logo_path.parent.mkdir(parents=True, exist_ok=True)
    base.resize((256, 256), Image.LANCZOS).save(logo_path)

    print("OK", icon_path, logo_path)
