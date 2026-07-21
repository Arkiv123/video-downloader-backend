#!/usr/bin/env python3
"""
Open Graph image generator.

Renders a branded 1200x630 PNG per landing page (the size Facebook, X, WhatsApp,
Discord, LinkedIn, Telegram etc. expect). Social crawlers largely IGNORE SVG for
og:image, so a real raster image is what actually shows a rich card when a link
is shared — a meaningful click-through win over the tiny icon we referenced before.

Called from build.py. Degrades gracefully: if Pillow isn't installed, build.py
skips OG generation and falls back to the icon, so the site still builds.
"""

import os

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except Exception:  # Pillow missing — caller falls back to icon.svg
    HAVE_PIL = False

# Site palette (approximating the oklch tokens in styles.css as sRGB).
BG      = (14, 20, 23)      # near --bg
PANEL   = (26, 34, 38)      # near --panel
LIME    = (184, 230, 0)     # near --lime accent
RED     = (255, 59, 48)     # download-circle motif, from icon.svg
TEXT    = (238, 242, 246)   # near --text
MUTED   = (150, 165, 168)   # near --muted
LINE    = (56, 70, 74)      # near --line-soft

W, H = 1200, 630

# Font candidates in preference order (bold/display first). Windows + Linux paths.
_BOLD = ["C:/Windows/Fonts/bahnschrift.ttf", "C:/Windows/Fonts/arialbd.ttf",
         "C:/Windows/Fonts/impact.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "arialbd.ttf"]
_REG  = ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"]


def _font(paths, size):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    """Greedy word-wrap to fit max_w pixels; returns list of lines."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _brand_tile(draw, x, y, s):
    """Mini version of icon.svg: lime rounded tile + dark download glyph."""
    draw.rounded_rectangle([x, y, x + s, y + s], radius=int(s * 0.23), fill=LIME)
    ink = BG
    w = max(3, int(s * 0.085))
    cx = x + s / 2
    # vertical stem
    draw.line([(cx, y + s * 0.26), (cx, y + s * 0.60)], fill=ink, width=w)
    # arrowhead
    draw.line([(x + s * 0.36, y + s * 0.47), (cx, y + s * 0.62), (x + s * 0.64, y + s * 0.47)],
              fill=ink, width=w, joint="curve")
    # base line
    draw.line([(x + s * 0.30, y + s * 0.74), (x + s * 0.70, y + s * 0.74)], fill=ink, width=w)


def render(title, brand, subtitle, out_path):
    """Render one OG card. No-op returning False if Pillow is unavailable."""
    if not HAVE_PIL:
        return False

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # inset panel with a hairline border + lime top accent
    m = 40
    d.rounded_rectangle([m, m, W - m, H - m], radius=28, fill=PANEL, outline=LINE, width=2)
    d.rounded_rectangle([m, m, W - m, m + 10], radius=6, fill=LIME)

    pad = 90
    # header row: brand tile + wordmark
    _brand_tile(d, pad, pad + 4, 56)
    f_brand = _font(_BOLD, 34)
    d.text((pad + 74, pad + 14), brand, font=f_brand, fill=TEXT)

    # big title, wrapped, vertically centered-ish
    f_title = _font(_BOLD, 78)
    lines = _wrap(d, title, f_title, W - 2 * pad)
    # shrink if too many lines
    while len(lines) > 3 and f_title.size > 48:
        f_title = _font(_BOLD, f_title.size - 6)
        lines = _wrap(d, title, f_title, W - 2 * pad)
    ty = 235
    for ln in lines:
        d.text((pad, ty), ln, font=f_title, fill=TEXT)
        ty += int(f_title.size * 1.12)

    # lime underline accent under the title block
    d.rectangle([pad, ty + 8, pad + 120, ty + 16], fill=LIME)

    # subtitle at the bottom
    f_sub = _font(_REG, 32)
    d.text((pad, H - pad - 24), subtitle, font=f_sub, fill=MUTED)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return True


if __name__ == "__main__":
    ok = render("YouTube Video Downloader", "GOOGLY RANKS",
                "Free · No signup · 1000+ sites · Every quality", "og/_test.png")
    print("rendered" if ok else "Pillow missing")
