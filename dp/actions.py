"""Input (taps and keys through `adb shell input`, strokes and pinches through inject.py),
and what changed on screen afterwards."""
import math
import shlex

from . import inject, ui
from .out import Fail

KEYS = {
    "back": 4, "home": 3, "enter": 66, "del": 67, "backspace": 67, "delete": 112, "tab": 61,
    "esc": 111, "escape": 111, "menu": 82, "up": 19, "down": 20, "left": 21, "right": 22,
    "space": 62, "search": 84, "recent": 187, "pageup": 92, "pagedown": 93,
    "move_home": 122, "move_end": 123, "cut": 277, "copy": 278, "paste": 279,
}


def keycode(name: str):
    n = name.lower()
    if n in KEYS:
        return str(KEYS[n])
    if n.isdigit():
        return n
    if n.upper().startswith("KEYCODE_"):
        return n.upper()
    raise Fail(f"キー「{name}」が分かりません", ["使える名前: " + " ".join(KEYS)])


def tap(adb, x, y):
    adb.sh(f"input tap {x} {y}", check=True)


def long_press(adb, x, y, ms=800):
    adb.sh(f"input swipe {x} {y} {x} {y} {ms}", check=True)


def swipe(adb, x1, y1, x2, y2, ms=300):
    adb.sh(f"input swipe {x1} {y1} {x2} {y2} {ms}", check=True)


def key(adb, name):
    adb.sh(f"input keyevent {keycode(name)}", check=True)


def text(adb, s: str):
    if not s.isascii():
        raise Fail("adb からは日本語などの ASCII 以外を入れられません",
                   ["probe（第 2 段階）の type で入れられるようになります。今は ASCII だけ"])
    adb.sh("input text " + shlex.quote(s.replace(" ", "%s")), check=True)


# --- strokes -----------------------------------------------------------------

def preset(name, box):
    """Strokes (lists of points) for a named shape inside box = (x1, y1, x2, y2)."""
    x1, y1, x2, y2 = box
    mx, my = (x2 - x1) * 0.2, (y2 - y1) * 0.2
    l, t, r, b = x1 + mx, y1 + my, x2 - mx, y2 - my
    w, h = r - l, b - t
    cx, cy = (l + r) / 2, (t + b) / 2
    if name == "line":
        return [[(l, cy), (r, cy)]]
    if name == "zigzag":
        return [[(l + w * i / 8, t if i % 2 else b) for i in range(9)]]
    if name == "wave":
        # Kept flat so it stays a line of writing: a tall wave counts as a scribble in note apps.
        amp = min(h / 2, w / 10)
        return [[(l + w * i / 60, cy + amp * math.sin(i / 60 * 4 * math.pi)) for i in range(61)]]
    if name == "circle":
        rad = min(w, h) / 2
        return [[(cx + rad * math.cos(a / 48 * 2 * math.pi), cy + rad * math.sin(a / 48 * 2 * math.pi))
                 for a in range(49)]]
    if name == "check":
        return [[(l, cy), (l + w * 0.35, b), (r, t)]]
    if name == "cross":
        return [[(l, t), (r, b)], [(r, t), (l, b)]]
    raise Fail(f"形「{name}」はありません", ["line zigzag wave circle check cross、または点列 \"x,y x,y …\"（/ で線を分ける）"])


PRESETS = ("line", "zigzag", "wave", "circle", "check", "cross")


def parse_points(spec: str, s: float):
    """'x,y x,y / x,y …' in shot space -> strokes in device pixels."""
    strokes = []
    for part in spec.split("/"):
        pts = []
        for tok in part.split():
            try:
                x, y = tok.split(",")
                pts.append((float(x) / s, float(y) / s))
            except ValueError:
                raise Fail(f"点「{tok}」が読めません（x,y の形で）")
        if len(pts) >= 2:
            strokes.append(pts)
    if not strokes:
        raise Fail("線には点が 2 つ以上要ります")
    return strokes


def draw(dev, strokes, stylus=False, pressure=None):
    """Strokes in device pixels, each sent as one gesture from one process (see inject.py)."""
    return inject.run(dev, inject.stroke_lines(strokes, stylus=stylus, pressure=pressure))


def pinch(dev, cx, cy, d0, d1, vertical=False):
    return inject.run(dev, inject.pinch_lines(cx, cy, d0, d1, vertical=vertical))


# --- change summary -------------------------------------------------------------

def _key(kind, label, rid, flags):
    return (kind, label, rid, tuple(flags))


def summarize(before: dict, after: ui.Screen, took: float, top=12):
    """Lines describing the screen after an action, compared with `before` (state dict)."""
    s = after.scale
    if not before:
        return ui.render(after.elems, s, max_lines=top)
    if before.get("activity") != after.activity or before.get("package") != after.package:
        head = f"→ {before.get('activity') or '?'} → {after.activity}（{took:.1f}s）"
        return [head, *ui.render(after.elems, s, max_lines=top)]
    if before.get("hash") == after.hash:
        return [f"→ 変化なし（{took:.1f}s）"]
    old = {_key(d["kind"], d["label"], d["rid"], d["flags"]) for d in before.get("elems", [])}
    new = {_key(e.kind, e.label, e.node.rid, e.flags) for e in after.elems}
    added = [e for e in after.elems if _key(e.kind, e.label, e.node.rid, e.flags) not in old]
    removed = [d for d in before.get("elems", []) if _key(d["kind"], d["label"], d["rid"], d["flags"]) not in new]
    if len(added) + len(removed) > max(6, len(after.elems) // 2):
        return [f"→ 同じ画面で大きく変化（{took:.1f}s）", *ui.render(after.elems, s, max_lines=top)]
    lines = [f"→ 同じ画面、変化 {len(added) + len(removed)} か所（{took:.1f}s）"]
    lines += ["  + " + ui._fmt(e, s) for e in added[:6]]
    if len(added) > 6:
        lines.append(f"  + ほか {len(added) - 6}")
    for d in removed[:4]:
        flags = (" " + " ".join(d["flags"])) if d["flags"] else ""
        lines.append(f'  - {d["kind"]} "{d["label"]}"{flags}')
    if len(removed) > 4:
        lines.append(f"  - ほか {len(removed) - 4}")
    return lines
