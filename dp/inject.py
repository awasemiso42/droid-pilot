"""Strokes and pinches sent from one device process (inject/Inject.java run with app_process).

The helper is compiled on this PC the first time (javac + d8, cached by source hash) and pushed to
/data/local/tmp. Plain taps and swipes still use `adb shell input`.
"""
import math
from pathlib import Path

from . import javabuild
from .out import Fail

SRC = Path(__file__).resolve().parent.parent / "inject" / "Inject.java"
REMOTE_DIR = "/data/local/tmp"


def build() -> Path:
    return javabuild.build_dex_jar(SRC, "dp-inject", "線を引く補助")


def run(dev, lines, timeout=120) -> int:
    """Sends the command lines; returns the number of events sent."""
    jar = build()
    remote = f"{REMOTE_DIR}/{jar.name}"
    if dev.adb.sh(f"ls {remote} 2>/dev/null", timeout=10).strip() != remote:
        dev.adb.sh(f"rm -f {REMOTE_DIR}/dp-inject-*.jar", timeout=10)
        p = dev.adb.run("push", str(jar), remote, timeout=60)
        if p.returncode != 0:
            raise Fail("線を引く補助を端末に送れません", [(p.stdout + p.stderr).strip()[-300:]])
    p = dev.adb.run("shell", f"CLASSPATH={remote} app_process / dp.Inject",
                    input="\n".join(lines) + "\n", timeout=timeout)
    out = [l for l in (p.stdout or "").splitlines() if l.startswith(("ok ", "error "))]
    if not out or not out[-1].startswith("ok "):
        detail = out[-1] if out else ((p.stdout or "") + (p.stderr or "")).strip()[-300:]
        raise Fail("入力を送れませんでした", [detail])
    return int(out[-1].split()[1])


# --- what to send ------------------------------------------------------------------

def _densify(pts, step):
    """Points every `step` px along the polyline, keeping its corners."""
    out = [pts[0]]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        k = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / step))
        out += [(x1 + (x2 - x1) * i / k, y1 + (y2 - y1) * i / k) for i in range(1, k + 1)]
    return out


def stroke_lines(strokes, stylus=False, pressure=None, step=8.0, dt=8, max_events=300):
    """Commands for strokes (lists of device-pixel points). The pen moves about `step` px per event."""
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for pts in strokes for a, b in zip(pts, pts[1:]))
    step = max(step, total / max_events)
    p = pressure if pressure is not None else (0.6 if stylus else 1.0)
    lines = [f"src {'stylus' if stylus else 'touch'}", f"dt {dt}"]
    for pts in strokes:
        dense = _densify(pts, step)
        lines.append(f"down {dense[0][0]:.1f} {dense[0][1]:.1f} {p:.2f}")
        lines += [f"move {x:.1f} {y:.1f}" for x, y in dense[1:]]
        lines.append(f"up {dense[-1][0]:.1f} {dense[-1][1]:.1f}")
        lines.append("wait 150")
    return lines


def pinch_lines(cx, cy, d0, d1, steps=24, dt=12, vertical=False):
    """Two fingers on a line through (cx, cy), from distance d0 to d1."""
    def at(d):
        h = d / 2
        if vertical:
            return f"{cx:.1f} {cy - h:.1f} {cx:.1f} {cy + h:.1f}"
        return f"{cx - h:.1f} {cy:.1f} {cx + h:.1f} {cy:.1f}"
    lines = ["src touch", f"dt {dt}", f"down2 {at(d0)}"]
    lines += [f"move2 {at(d0 + (d1 - d0) * i / steps)}" for i in range(1, steps + 1)]
    lines += ["wait 60", f"up2 {at(d1)}"]
    return lines
