"""Gradle builds with the errors squeezed out of the output, and finding the APK."""
import glob
import os
import re
import subprocess
import time
from pathlib import Path

from . import javabuild
from .out import Fail

MAX_ERRORS = 20

_KOTLIN = re.compile(r"^e: (?:file://)?(\S+?):(\d+):(\d+):? (.*)$")
_JAVA = re.compile(r"^(\S+\.java):(\d+): error: (.*)$")
_AAPT = re.compile(r"^(?:ERROR|AAPT: error|error):?\s*(.*)$", re.I)
_RES = re.compile(r"^(\S+\.xml):(\d+): (?:AAPT: )?error: (.*)$")


def _rel(path, repo: Path):
    try:
        return str(Path(path).resolve().relative_to(repo))
    except ValueError:
        return path


def parse_errors(output: str, repo: Path):
    errors, seen = [], set()

    def add(s):
        if s not in seen:
            seen.add(s)
            errors.append(s)

    lines = output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        m = _KOTLIN.match(line)
        if m:
            add(f"{_rel(m.group(1), repo)}:{m.group(2)}:{m.group(3)}: {m.group(4).strip()}")
            i += 1
            continue
        m = _JAVA.match(line) or _RES.match(line)
        if m:
            msg = m.group(3).strip()
            # javac puts "symbol: ..." on the following lines.
            extra = []
            j = i + 1
            while j < len(lines) and j <= i + 4 and lines[j].strip().startswith(("symbol:", "location:")):
                extra.append(lines[j].strip())
                j += 1
            add(f"{_rel(m.group(1), repo)}:{m.group(2)}: {msg}" + (f" ({'; '.join(extra)})" if extra else ""))
            i = j
            continue
        if line.startswith("* What went wrong:"):
            what = []
            j = i + 1
            while j < len(lines) and not lines[j].startswith("* ") and len(what) < 6:
                s = lines[j].strip()
                if s:
                    what.append(s)
                j += 1
            if what:
                add("Gradle: " + " / ".join(w.lstrip("> ") for w in what if w)[:300])
            i = j
            continue
        i += 1
    return errors


def run(app, run_dir: Path, timeout=1800):
    """Runs app.build in the repository. Returns (ok, seconds, errors, log path)."""
    if not app.build:
        raise Fail(f"{app.name} に build が設定されていません（config/apps.toml）")
    cmd = app.build + " --console=plain"
    log = run_dir / "build.log"
    t0 = time.time()
    env = dict(os.environ, TERM="dumb")
    # Gradle falls back to the java on PATH, which on this PC is a JRE without javac.
    jdk = javabuild.jdk_home()
    if jdk and str(jdk) != env.get("JAVA_HOME"):
        env["JAVA_HOME"] = str(jdk)
    try:
        p = subprocess.run(cmd, shell=True, cwd=app.repo, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise Fail(f"ビルドが {timeout // 60} 分で終わりませんでした")
    took = time.time() - t0
    out = p.stdout + "\n" + p.stderr
    log.write_text(f"$ JAVA_HOME={env.get('JAVA_HOME', '')} {cmd}\n{out}")
    errors = parse_errors(out, app.repo) if p.returncode != 0 else []
    if p.returncode != 0 and not errors:
        tail = [l for l in out.strip().splitlines() if l.strip()][-8:]
        errors = ["（エラー行が見つかりません。最後の出力:）", *tail]
    return p.returncode == 0, took, errors, log


def find_apk(app, abi=""):
    """The newest APK matching the first pattern that matches anything."""
    for pat in app.apk:
        pat = pat.replace("{abi}", abi or "*")
        hits = glob.glob(str(app.repo / pat))
        if hits:
            return Path(max(hits, key=os.path.getmtime))
    return None


def apk_info(apk: Path, sdk: Path):
    """(versionCode, versionName) read with aapt2, or (None, None)."""
    tools = sorted((sdk / "build-tools").glob("*/aapt2"), reverse=True)
    if not tools:
        return None, None
    try:
        p = subprocess.run([str(tools[0]), "dump", "badging", str(apk)], capture_output=True,
                           text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    m = re.search(r"versionCode='(\d*)' versionName='([^']*)'", p.stdout)
    return (int(m.group(1)) if m and m.group(1) else None), (m.group(2) if m else None)
