#!/usr/bin/env python3
"""Regenerate Muster's icon assets from the owner's artwork in muster/icon-source/.

The artwork is a full square.  A Mac app icon is not:  it is a rounded square 824 points
wide on a 1024 point canvas, with a soft shadow in the margin.  So the whole artwork is
scaled into that shape, uncropped, and written out in every size the catalog needs.

Needs Pillow, and only this script does.  The tests read the output with the standard
library, so CI does not need it.

    python3 muster/script/generate_app_icons.py
"""

import json
import pathlib

from PIL import Image, ImageDraw, ImageFilter

MUSTER = pathlib.Path(__file__).resolve().parent.parent
SOURCES = MUSTER / "icon-source"
CATALOG = MUSTER / "Muster" / "Assets.xcassets"

CANVAS = 1024
BODY = 824
CORNER_RADIUS = 185
SHADOW_OFFSET = 12
SHADOW_BLUR = 14
SHADOW_OPACITY = 0.30
MASK_SCALE = 4

# The icon in the app bundle is the one thing macOS shows before Muster runs, and it cannot
# change with the mode, so one variant has to stand for both.
BUNDLE_VARIANT = "light"
RUNTIME_IMAGESETS = {"light": "MusterIconLight", "dark": "MusterIconDark"}

# (points, scale) for every slot of a Mac app icon.
ICON_SLOTS = [
    (16, 1),
    (16, 2),
    (32, 1),
    (32, 2),
    (128, 1),
    (128, 2),
    (256, 1),
    (256, 2),
    (512, 1),
    (512, 2),
]
CATALOG_INFO = {"author": "xcode", "version": 1}


def body_mask():
    """The rounded square, drawn large and scaled down so its edge is smooth."""
    large = Image.new("L", (BODY * MASK_SCALE, BODY * MASK_SCALE), 0)
    ImageDraw.Draw(large).rounded_rectangle(
        (0, 0, large.width - 1, large.height - 1),
        radius=CORNER_RADIUS * MASK_SCALE,
        fill=255,
    )
    return large.resize((BODY, BODY), Image.Resampling.LANCZOS)


def render_master(source):
    """Return the 1024 pixel icon for one piece of artwork."""
    art = Image.open(source).convert("RGB").resize((BODY, BODY), Image.Resampling.LANCZOS)
    mask = body_mask()
    origin = (CANVAS - BODY) // 2

    shadow_alpha = Image.new("L", (CANVAS, CANVAS), 0)
    shadow_alpha.paste(mask, (origin, origin + SHADOW_OFFSET))
    shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
    shadow_alpha = shadow_alpha.point(lambda value: int(value * SHADOW_OPACITY))

    shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha)

    body = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    art.putalpha(mask)
    body.paste(art, (origin, origin))
    return Image.alpha_composite(shadow, body)


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")


def write_app_icon(master):
    folder = CATALOG / "AppIcon.appiconset"
    folder.mkdir(parents=True, exist_ok=True)
    images = []
    for points, scale in ICON_SLOTS:
        pixels = points * scale
        suffix = "@2x" if scale == 2 else ""
        filename = f"icon_{points}x{points}{suffix}.png"
        master.resize((pixels, pixels), Image.Resampling.LANCZOS).save(
            folder / filename, optimize=True
        )
        images.append(
            {
                "filename": filename,
                "idiom": "mac",
                "scale": f"{scale}x",
                "size": f"{points}x{points}",
            }
        )
    write_json(folder / "Contents.json", {"images": images, "info": CATALOG_INFO})


def write_runtime_imageset(name, master):
    folder = CATALOG / f"{name}.imageset"
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{name}.png"
    master.save(folder / filename, optimize=True)
    write_json(
        folder / "Contents.json",
        {
            "images": [{"filename": filename, "idiom": "universal", "scale": "1x"}],
            "info": CATALOG_INFO,
        },
    )


def main():
    CATALOG.mkdir(parents=True, exist_ok=True)
    write_json(CATALOG / "Contents.json", {"info": CATALOG_INFO})
    masters = {variant: render_master(SOURCES / f"{variant}.webp") for variant in RUNTIME_IMAGESETS}
    write_app_icon(masters[BUNDLE_VARIANT])
    for variant, name in RUNTIME_IMAGESETS.items():
        write_runtime_imageset(name, masters[variant])


if __name__ == "__main__":
    main()
