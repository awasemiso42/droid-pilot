"""Per-app working state (<repo>/.dp/): the last screen, shot counter, journal, runs."""
import json
import time
from datetime import datetime
from pathlib import Path

from . import config


def state_dir(app) -> Path:
    d = app.state_dir if app else config.CACHE / "noapp"
    d.mkdir(parents=True, exist_ok=True)
    ignore = d / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# droid-pilot の作業場所（スクショ・記録）。Git に入れない\n*\n")
    return d


class State:
    def __init__(self, app):
        self.dir = state_dir(app)
        self.path = self.dir / "state.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False))
        tmp.replace(self.path)

    def dev(self, serial) -> dict:
        return self.data.setdefault("devices", {}).setdefault(serial, {})

    def next_shot(self) -> Path:
        n = self.data.get("shot_seq", 0) + 1
        self.data["shot_seq"] = n
        d = self.dir / "shots"
        d.mkdir(exist_ok=True)
        return d / f"{n:04d}.png"

    def run_dir(self, name="") -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        d = self.dir / "runs" / (stamp + (f"-{name}" if name else ""))
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- journal ------------------------------------------------------------
    @property
    def journal_path(self):
        return self.dir / "journal.jsonl"

    def next_step(self) -> int:
        n = self.data.get("step_seq", 0) + 1
        self.data["step_seq"] = n
        return n

    def journal(self, entry: dict):
        entry = {"t": time.time(), **entry}
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_journal(self):
        try:
            lines = self.journal_path.read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out


def rel(path: Path) -> str:
    """A path as short as possible for output (relative to the current directory when inside it)."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)
