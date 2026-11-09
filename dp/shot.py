"""Screenshots: capture, shrink to shot space, and draw element numbers or a grid on them."""
import io

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import ui
from .out import Fail


def grab(dev) -> Image.Image:
    data = dev.adb.exec_out("screencap -p", timeout=20)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise Fail("スクショが取れません", [data[:120].decode(errors="replace")])
    return img.convert("RGB")


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def to_shot(img: Image.Image) -> tuple:
    s = ui.scale_for(*img.size)
    if s < 1:
        img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    return img, s


def mark(img: Image.Image, elems, s):
    """Numbered boxes for the listed elements (Set-of-Mark)."""
    d = ImageDraw.Draw(img)
    f = _font(13)
    colors = [(230, 40, 40), (20, 120, 230), (20, 160, 60), (200, 100, 0)]
    for e in elems:
        if e.kind == "text":
            continue
        x1, y1, x2, y2 = (round(v * s) for v in e.bounds)
        c = colors[e.i % len(colors)]
        d.rectangle([x1, y1, x2 - 1, y2 - 1], outline=c, width=2)
        label = str(e.i)
        tw = d.textlength(label, font=f)
        d.rectangle([x1, y1, x1 + tw + 6, y1 + 16], fill=c)
        d.text((x1 + 3, y1 + 1), label, fill=(255, 255, 255), font=f)
    return img


def grid(img: Image.Image, step_pct=10):
    """Lines every 10% labelled with shot-space coordinates."""
    d = ImageDraw.Draw(img)
    f = _font(11)
    w, h = img.size
    for k in range(1, 100 // step_pct):
        x = round(w * k * step_pct / 100)
        y = round(h * k * step_pct / 100)
        d.line([(x, 0), (x, h)], fill=(255, 0, 180), width=1)
        d.line([(0, y), (w, y)], fill=(255, 0, 180), width=1)
        d.text((x + 2, 2), str(x), fill=(255, 0, 180), font=f)
        d.text((2, y + 2), str(y), fill=(255, 0, 180), font=f)
    return img


def region_change(a: Image.Image, b: Image.Image, box) -> float:
    """Percent of pixels inside box (device pixels) that changed between two screenshots.
    Kept near full resolution so thin ink lines count."""
    if a is None or b is None or a.size != b.size:
        return 0.0
    x1, y1, x2, y2 = (int(v) for v in box)
    ca, cb = a.crop((x1, y1, x2, y2)).convert("L"), b.crop((x1, y1, x2, y2)).convert("L")
    k = min(1.0, 600 / max(1, ca.width, ca.height))
    if k < 1:
        size = (max(1, round(ca.width * k)), max(1, round(ca.height * k)))
        ca, cb = ca.resize(size, Image.BOX), cb.resize(size, Image.BOX)
    diff = ImageChops.difference(ca, cb).point(lambda v: 255 if v > 18 else 0)
    hist = diff.histogram()
    return 100 * hist[255] / max(1, ca.width * ca.height)
