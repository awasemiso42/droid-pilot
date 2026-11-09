"""Flow files: one dp command per line, saved from the journal and replayed as a regression test."""
import re
import shlex
from datetime import datetime
from pathlib import Path

from .out import Fail

SUFFIX = ".dpflow"
# Commands that may appear in a flow (the rest are looking, not doing).
STEP_COMMANDS = {"launch", "stop", "reset", "tap", "long", "type", "key", "swipe", "scroll",
                 "draw", "zoom", "wait", "expect", "mark", "install"}


def flow_dir(app) -> Path:
    return app.repo / "dpflows"


def path_for(app, name) -> Path:
    if not re.fullmatch(r"[\w.-]+", name):
        raise Fail("手順の名前は英数字と - _ . だけにしてください")
    return flow_dir(app) / f"{name}{SUFFIX}"


def parse_range(spec: str, last_n: int):
    """'31-36,38' -> [31..36, 38]."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out += list(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise Fail(f"範囲「{part}」が読めません（例: 31-36,38）")
    return out


def save(app, name, steps, force=False, note=""):
    path = path_for(app, name)
    if path.exists() and not force:
        raise Fail(f"{path.name} はもうあります。上書きするなら --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    nums = [s["n"] for s in steps]
    head = [f"# {note}" if note else f"# {name}",
            f"# {datetime.now():%Y-%m-%d %H:%M} に dp journal の {nums[0]}〜{nums[-1]} から保存",
            f"app {app.name}"]
    path.write_text("\n".join(head + [s["cmd"] for s in steps]) + "\n")
    return path


def load(path: Path):
    """[(line number, argv)] without comments and blank lines."""
    out = []
    for i, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            argv = shlex.split(line, comments=True)
        except ValueError as e:
            raise Fail(f"{path.name}:{i} が読めません: {e}")
        if argv:
            out.append((i, argv))
    return out


def listing(app):
    d = flow_dir(app)
    out = []
    for p in sorted(d.glob(f"*{SUFFIX}")):
        first = next((l[1:].strip() for l in p.read_text().splitlines() if l.startswith("#")), "")
        steps = sum(1 for l in p.read_text().splitlines() if l.strip() and not l.startswith("#"))
        out.append((p.stem, steps, first))
    return out
