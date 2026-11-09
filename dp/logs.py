"""The app's own log lines, crashes and ANRs, squeezed down."""
import re
import shlex
from collections import OrderedDict
from dataclasses import dataclass

MAX_LINES = 30
APP_FRAMES = 6

_LINE = re.compile(r"^\s*(\d+\.\d+)\s+(?:(\S+)\s+)?(\d+)\s+(\d+)\s+([VDIWEFA])\s+(.*?)\s*: ?(.*)$")
_NUM = re.compile(r"0x[0-9a-fA-F]+|\b\d+\b")
LEVELS = "VDIWEFA"


@dataclass
class Line:
    t: float
    uid: str
    pid: int
    tid: int
    level: str
    tag: str
    msg: str


def parse(text: str):
    out = []
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        t, uid, pid, tid, lv, tag, msg = m.groups()
        out.append(Line(float(t), uid or "", int(pid), int(tid), lv, tag.strip(), msg))
    return out


def app_uid(dev, package):
    out = dev.adb.sh(f"pm list packages -U {shlex.quote(package)}", timeout=10)
    for line in out.splitlines():
        m = re.match(r"package:(\S+) uid:(\d+)", line.strip())
        if m and m.group(1) == package:
            return int(m.group(2))
    return None


def uid_names(uid):
    """The spellings logcat may use for an app uid."""
    if uid is None:
        return set()
    names = {str(uid)}
    user, app_id = divmod(uid, 100000)
    if app_id >= 10000:
        names.add(f"u{user}_a{app_id - 10000}")
    return names


def fetch(dev, since, buffers="main,system,crash", uid=None, extra=""):
    t = f"-T {since:.3f}" if since else ""
    u = f"--uid={uid}" if uid is not None else ""
    cmd = f"logcat -d -b {buffers} -v epoch,uid {t} {u} {extra}"
    return dev.adb.sh(cmd, timeout=40)


def noise(lines, patterns):
    """(kept, number dropped) after removing lines that match the ignore patterns."""
    if not patterns:
        return lines, 0
    kept = [l for l in lines if not any(p.search(f"{l.tag}: {l.msg}") for p in patterns)]
    return kept, len(lines) - len(kept)


def collect(dev, package, since, ignore=()):
    """(app lines, crash blocks, anr lines, step markers, lines dropped as noise)."""
    uid = app_uid(dev, package)
    names = uid_names(uid)
    text = fetch(dev, since, uid=uid)
    if "unrecognized option" in text or "invalid option" in text.lower():
        text = fetch(dev, since)
    lines = parse(text)
    mine = [l for l in lines if l.uid in names] if names else lines
    crash_text = fetch(dev, since, buffers="crash")
    crashes = crash_blocks(parse(crash_text), package, names)
    anr = [l for l in parse(fetch(dev, since, buffers="system", extra=f"-e {shlex.quote('ANR in ' + package)}"))]
    markers = [l for l in parse(fetch(dev, since, buffers="main", extra="-s DP:I"))]
    # The crash buffer lines are already in `mine`; keep them out of the plain errors.
    crash_keys = {(c.t, c.msg) for b in crashes for c in b}
    mine = [l for l in mine if (l.t, l.msg) not in crash_keys]
    mine, dropped = noise(mine, ignore)
    return mine, crashes, anr, markers, dropped


def crash_blocks(lines, package, names):
    """Runs of crash-buffer lines (same pid and tag) that belong to the app."""
    blocks, cur = [], []
    for l in lines:
        if cur and (l.pid != cur[-1].pid or l.tag != cur[-1].tag or l.t - cur[-1].t > 2):
            blocks.append(cur)
            cur = []
        cur.append(l)
    if cur:
        blocks.append(cur)
    out = []
    for b in blocks:
        body = "\n".join(l.msg for l in b)
        if (b[0].uid in names) or (package in body):
            out.append(b)
    return out


def _is_app_frame(msg, code):
    s = msg.strip()
    return s.startswith("at ") and any(s[3:].startswith(p) for p in code)


def summarize_stack(msgs, code, frames=APP_FRAMES):
    """Exception lines, the app's own frames, and a count of the rest."""
    out, hidden, shown = [], 0, 0
    has_app = any(_is_app_frame(m, code) for m in msgs)
    first_after_headline = False

    def flush_hidden():
        nonlocal hidden
        if hidden:
            out.append(f"  … ほか {hidden} 行")
            hidden = 0

    for m in msgs:
        s = m.strip()
        if not s:
            continue
        if s.startswith("at "):
            app = _is_app_frame(s, code)
            # Without any app frame, keep the top frame under each headline so the place is visible.
            if (app and shown < frames) or (first_after_headline and not has_app):
                flush_hidden()
                out.append("  " + s)
                shown += 1 if app else 0
            else:
                hidden += 1
            first_after_headline = False
        elif s.startswith("..."):
            hidden += 1
        else:
            flush_hidden()
            out.append(s)
            first_after_headline = True
            if s.startswith("Caused by"):
                shown = 0
    flush_hidden()
    return out


def _clock(t):
    import time
    return time.strftime("%H:%M:%S", time.localtime(t))


def _step_at(markers, t):
    best = None
    for m in markers:
        if m.t <= t:
            best = m
    return best.msg if best else ""


def report(mine, crashes, anr, markers, code, level="E", grep=None):
    """(head, lines, counts) for `dp log`."""
    lines = []
    for b in crashes:
        msgs = [l.msg for l in b]
        tag = b[0].tag
        if tag == "AndroidRuntime":
            body = [m for m in msgs if not m.startswith(("FATAL EXCEPTION", "Process:"))]
            stack = summarize_stack(body, code)
            lines.append(f"CRASH {_clock(b[0].t)} {stack[0] if stack else msgs[0]}")
            lines += stack[1:]
        else:
            interesting = [m for m in msgs if re.search(r"signal|Abort|Cause|backtrace|#0\d ", m)][:8]
            lines.append(f"CRASH(native) {_clock(b[0].t)} {tag}")
            lines += ["  " + m.strip() for m in interesting]
        step = _step_at(markers, b[0].t)
        if step:
            lines.append(f"  直前の操作: {step}")
    for a in anr:
        lines.append(f"ANR {_clock(a.t)} {a.msg}")

    want = LEVELS[LEVELS.index(level):]
    groups = OrderedDict()
    counts = {"E": 0, "W": 0}
    i = 0
    while i < len(mine):
        l = mine[i]
        # Collect the stack lines logged right after this one.
        stack = []
        j = i + 1
        while j < len(mine) and mine[j].tid == l.tid and mine[j].tag == l.tag and \
                (mine[j].msg.lstrip().startswith(("at ", "Caused by", "...")) or mine[j].msg.startswith("\t")):
            stack.append(mine[j].msg)
            j += 1
        i = j
        if l.level in "EFA":
            counts["E"] += 1
        elif l.level == "W":
            counts["W"] += 1
        if l.level not in want:
            continue
        if grep and not re.search(grep, f"{l.tag} {l.msg}", re.I):
            continue
        key = (l.level, l.tag, _NUM.sub("#", l.msg))
        g = groups.setdefault(key, {"n": 0, "last": l, "stack": stack})
        g["n"] += 1
        g["last"] = l
    for (lv, tag, _), g in groups.items():
        l = g["last"]
        n = f"x{g['n']} " if g["n"] > 1 else ""
        lines.append(f"{lv} {n}{tag}: {l.msg.strip()[:160]}  ({_clock(l.t)})")
        if g["stack"]:
            lines += summarize_stack(g["stack"], code, frames=2)[:3]
    if len(lines) > MAX_LINES:
        cut = len(lines) - MAX_LINES
        lines = lines[:MAX_LINES] + [f"(+{cut} 行省略。--full で全部)"]
    return lines, {"crash": len(crashes), "anr": len(anr), **counts}
