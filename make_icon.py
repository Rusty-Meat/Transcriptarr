"""Build transcriptarr.ico from an input image.

Save the source as `icon-source.png` next to this script and run:

    python make_icon.py --as-is

`--as-is` is the no-op path: just resize to standard ico sizes and save.

For more involved processing (auto-detect the bright subject and crop
to it, pad to square, apply a rounded-rectangle mask, render small
sizes as a clean glyph instead of a downsampled source), drop `--as-is`
and use these flags:

    --shape <s>             rounded | circle | square      (default: rounded)
    --radius <pct>          corner radius % of side                  (default: 22)
    --bright-threshold <N>  R+G+B sum above this is treated as "subject"
                            for crop detection. 0-765, default 200.
    --crop-pad <pct>        breathing room around the subject          (default: 8)
    --no-crop               skip auto-crop, use the whole source
    --bg "#hex"             override the square-pad fill color
    --glyph-cutoff <N>      sizes <= N use a clean T glyph instead of the
                            downsampled source                       (default: 48)
    --glyph-color "#hex"    override the T glyph color
    --input <path>          pick a different source image
    --output <path>         pick a different output filename
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit(
        "Pillow isn't installed.\n"
        "Install with:\n"
        '  "C:\\Second Brain\\Second Brain\\.venv\\Scripts\\python.exe" -m pip install pillow'
    )


def parse_hex(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"Bad hex color: {s}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def detect_subject_bbox(img: Image.Image, threshold_sum: int) -> tuple[int, int, int, int] | None:
    """Find the bounding box of pixels brighter than the threshold.

    Returns (left, top, right, bottom) or None if nothing matches.
    """
    pixels = img.load()
    w, h = img.size
    min_x, min_y = w, h
    max_x, max_y = -1, -1
    for y in range(h):
        for x in range(w):
            px = pixels[x, y]
            r, g, b = px[0], px[1], px[2]
            if r + g + b >= threshold_sum:
                if x < min_x: min_x = x
                if y < min_y: min_y = y
                if x > max_x: max_x = x
                if y > max_y: max_y = y
    if max_x < 0:
        return None
    return min_x, min_y, max_x + 1, max_y + 1


def make_mask(side: int, shape: str, radius_pct: int) -> Image.Image:
    """Single-channel alpha mask. Rendered 4x then downsampled for clean AA edges."""
    SS = 4
    big = side * SS
    mask = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(mask)
    if shape == "circle":
        draw.ellipse((0, 0, big, big), fill=255)
    elif shape == "square":
        draw.rectangle((0, 0, big, big), fill=255)
    else:  # rounded
        r = int(big * radius_pct / 100)
        draw.rounded_rectangle((0, 0, big, big), radius=r, fill=255)
    return mask.resize((side, side), Image.LANCZOS)


def sample_brightest_color(img: Image.Image) -> tuple[int, int, int]:
    """Return the RGB of the brightest non-transparent pixel - used as the
    accent color for the glyph version of the icon at small sizes."""
    pixels = img.load()
    w, h = img.size
    best = (0, 0, 0)
    best_score = -1
    for y in range(0, h, 2):  # stride for speed
        for x in range(0, w, 2):
            px = pixels[x, y]
            r, g, b = px[0], px[1], px[2]
            a = px[3] if len(px) > 3 else 255
            if a < 128:
                continue
            score = max(r, g, b) + (r + g + b) // 4
            if score > best_score:
                best_score = score
                best = (r, g, b)
    return best


def render_glyph_T(side: int, t_color: tuple, bg_color: tuple,
                   shape: str, radius_pct: int) -> Image.Image:
    """Render a clean, solid T on a rounded-tile background.

    Used at small icon sizes where downsampling the source's soft glow
    would just turn into mush. Drawn at 4x then downsampled for crisp
    anti-aliased edges.
    """
    SS = 4
    big = side * SS
    img = Image.new("RGBA", (big, big), bg_color)
    draw = ImageDraw.Draw(img)

    # T proportions, tuned to look balanced inside a rounded tile.
    margin     = int(big * 0.22)  # outer breathing room
    bar_h      = int(big * 0.16)  # top bar thickness
    stem_w     = int(big * 0.20)  # vertical stem width
    corner_rad = max(1, int(big * 0.025))

    # Top horizontal bar
    bar_top = margin
    bar_bot = margin + bar_h
    draw.rounded_rectangle(
        (margin, bar_top, big - margin, bar_bot),
        radius=corner_rad, fill=t_color,
    )

    # Vertical stem (centered, runs from bottom of bar to bottom margin)
    stem_l = (big - stem_w) // 2
    stem_r = stem_l + stem_w
    draw.rounded_rectangle(
        (stem_l, bar_bot - 1, stem_r, big - margin),  # -1 to overlap bar by 1px
        radius=corner_rad, fill=t_color,
    )

    img = img.resize((side, side), Image.LANCZOS)

    # Apply tile-shape mask (rounded / circle / square)
    if shape != "square":
        mask = make_mask(side, shape, radius_pct)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(img, (0, 0), mask)
        img = canvas

    return img


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  default=str(here / "icon-source.png"))
    parser.add_argument("--output", default=str(here / "transcriptarr.ico"))
    parser.add_argument("--shape",  default="rounded",
                        choices=["rounded", "circle", "square"])
    parser.add_argument("--radius", type=int, default=22)
    parser.add_argument("--bright-threshold", type=int, default=200,
                        help="R+G+B sum above this = subject for auto-crop (0-765)")
    parser.add_argument("--crop-pad", type=int, default=8,
                        help="breathing-room padding as %% of subject side")
    parser.add_argument("--no-crop", action="store_true")
    parser.add_argument("--bg", default=None)
    parser.add_argument(
        "--as-is", action="store_true",
        help=("use the source image EXACTLY as it is - no cropping, no padding, "
              "no rounded corners, no glyph swap. Just resize to each .ico size "
              "and save. Best when you've already prepared the icon yourself."),
    )
    parser.add_argument(
        "--glyph-cutoff", type=int, default=48,
        help=("icon sizes <= this are rendered as a clean solid-T glyph "
              "instead of a downsampled source image (default 48). "
              "Set to 0 to use the source at every size."),
    )
    parser.add_argument(
        "--glyph-color", default=None,
        help='hex color for the T glyph (default: sample brightest from source)',
    )
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            cand = here / f"icon-source{ext}"
            if cand.exists():
                src = cand
                break
        else:
            sys.exit(
                f"Source image not found at {args.input}\n"
                f"Save your image to {here}\\icon-source.png first."
            )

    print(f"Source: {src}")
    img = Image.open(src).convert("RGBA")
    w, h = img.size
    print(f"Loaded: {w}x{h}")

    # --as-is short-circuit: no cropping, no padding, no rounding, no glyph.
    # Just save the source at each .ico size and exit.
    if args.as_is:
        sizes = [16, 24, 32, 48, 64, 128, 256]
        layers = [img.resize((s, s), Image.LANCZOS) for s in sizes]
        out = Path(args.output)
        layers[-1].save(
            out, format="ICO",
            sizes=[(s, s) for s in sizes],
            append_images=layers[:-1],
        )
        print(f"\nWrote {out} as-is from source ({out.stat().st_size // 1024} KB)")
        print(f"Embedded sizes: {sizes}")
        return 0

    # 1. Sample the background color BEFORE cropping (top-left should be
    #    safely in the dark vignette).
    if args.bg:
        bg = (*parse_hex(args.bg), 255)
    else:
        tl = img.getpixel((0, 0))
        if isinstance(tl, int):
            bg = (tl, tl, tl, 255)
        elif len(tl) == 3:
            bg = (*tl, 255)
        else:
            bg = tl
    print(f"Background color: {bg[:3]}")

    # 2. Auto-crop to the subject (bright pixels = the T).
    if not args.no_crop:
        bbox = detect_subject_bbox(img, args.bright_threshold)
        if bbox is None:
            print(f"No bright subject found above threshold {args.bright_threshold}; skipping crop.")
        else:
            l, t, r, b = bbox
            sub_w, sub_h = r - l, b - t
            longest = max(sub_w, sub_h)
            margin = int(longest * args.crop_pad / 100)
            l = max(0, l - margin); t = max(0, t - margin)
            r = min(w, r + margin); b = min(h, b + margin)
            img = img.crop((l, t, r, b))
            print(f"Cropped to subject: {img.size[0]}x{img.size[1]} (margin {margin}px)")

    # 3. Pad to square with the sampled background color.
    w2, h2 = img.size
    if w2 != h2:
        side_src = max(w2, h2)
        square = Image.new("RGBA", (side_src, side_src), bg)
        square.paste(img, ((side_src - w2) // 2, (side_src - h2) // 2), img)
        img = square
        print(f"Padded to {side_src}x{side_src} square")

    # 4. Sample colors for the glyph mode (small sizes get a clean T glyph).
    if args.glyph_color:
        t_color_rgb = parse_hex(args.glyph_color)
    else:
        t_color_rgb = sample_brightest_color(img)
    print(f"Glyph T color: rgb{t_color_rgb}")
    glyph_t = (*t_color_rgb, 255)
    glyph_bg = bg

    # 5. Render each .ico size. Sizes <= glyph_cutoff use the clean glyph
    #    (legible at 16x16, 24x24); larger sizes use the source image.
    sizes = [16, 24, 32, 48, 64, 128, 256]
    layers = []
    for s in sizes:
        if args.glyph_cutoff > 0 and s <= args.glyph_cutoff:
            layers.append(
                render_glyph_T(s, glyph_t, glyph_bg, args.shape, args.radius)
            )
            continue

        resized = img.resize((s, s), Image.LANCZOS)
        if args.shape == "square":
            layers.append(resized)
            continue
        mask = make_mask(s, args.shape, args.radius)
        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        canvas.paste(resized, (0, 0), mask)
        layers.append(canvas)

    out = Path(args.output)
    layers[-1].save(
        out, format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=layers[:-1],
    )
    suffix = f" (radius {args.radius}%)" if args.shape == "rounded" else ""
    print(f"\nShape: {args.shape}{suffix}")
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
    print(f"Embedded sizes: {sizes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
