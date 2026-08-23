"""One-off: recolor HUGO's gold gem icon to blue for HUGO — same shape/shading,
shifted hue so the two apps are easy to tell apart at a glance. Regenerates every
size in icon.iconset from the source, keeping saturation/value (i.e. all the
gloss/shading/highlights) and only rotating hue to a blue target.
"""
import colorsys
from pathlib import Path
from PIL import Image

ICONSET = Path(__file__).resolve().parent.parent / "electron" / "assets" / "icon.iconset"
TARGET_HUE = 0.58  # ~209 degrees — a clear blue, close enough to gold's warmth
                    # in structure to read as "the same gem", far enough to
                    # never be confused with HUGO's yellow at a glance.

SIZES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}

src_path = ICONSET / "icon_512x512@2x.png"
src = Image.open(src_path).convert("RGBA")
px = src.load()
w, h = src.size
for y in range(h):
    for x in range(w):
        r, g, b, a = px[x, y]
        if a == 0:
            continue
        hue, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        nr, ng, nb = colorsys.hsv_to_rgb(TARGET_HUE, s, v)
        px[x, y] = (round(nr * 255), round(ng * 255), round(nb * 255), a)

for name, size in SIZES.items():
    out = src.resize((size, size), Image.LANCZOS)
    out.save(ICONSET / name)

print("Recolored all iconset sizes to blue.")
