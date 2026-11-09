"""Thin wrapper around adb with timeouts. Every device command in dp goes through here."""
import shlex
import subprocess

from . import config
from .out import Fail


def _run(args, timeout=30, binary=False, input=None):
    try:
        p = subprocess.run([config.adb_path(), *args], capture_output=True, timeout=timeout,
                           input=input, text=not binary)
    except FileNotFoundError:
        raise Fail(f"adb が見つかりません: {config.adb_path()}（ANDROID_HOME か DP_ADB を設定）")
    except subprocess.TimeoutExpired:
        raise Fail(f"adb が {timeout} 秒で終わりませんでした: adb {' '.join(args)[:80]}")
    return p


def list_devices():
    """[{serial, state, model, ...}] from `adb devices -l`."""
    p = _run(["devices", "-l"], timeout=15)
    out = []
    for line in p.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        d = {"serial": parts[0], "state": parts[1]}
        for kv in parts[2:]:
            if ":" in kv:
                k, v = kv.split(":", 1)
                d[k] = v
        out.append(d)
    return out


class Adb:
    def __init__(self, serial):
        self.serial = serial

    def run(self, *args, timeout=30, binary=False, input=None):
        return _run(["-s", self.serial, *args], timeout=timeout, binary=binary, input=input)

    def sh(self, cmd, timeout=30, check=False):
        """Runs a device shell command line; returns stdout (text)."""
        p = self.run("shell", cmd, timeout=timeout)
        if p.returncode == 255 or "device offline" in p.stderr or "not found" in p.stderr and "device" in p.stderr:
            raise Fail(f"端末 {self.serial} とつながりません: {p.stderr.strip()[:120]}")
        if check and p.returncode != 0:
            raise Fail(f"端末のコマンドが失敗: {cmd[:80]}", [(p.stdout + p.stderr).strip()[:300]])
        return p.stdout

    def sh_args(self, *args, timeout=30, check=False):
        return self.sh(" ".join(shlex.quote(a) for a in args), timeout=timeout, check=check)

    def exec_out(self, cmd, timeout=30) -> bytes:
        p = self.run("exec-out", cmd, timeout=timeout, binary=True)
        if p.returncode != 0 and not p.stdout:
            raise Fail(f"exec-out が失敗: {cmd[:80]}", [p.stderr.decode(errors='replace').strip()[:300]])
        return p.stdout

    def getprop(self, key):
        return self.sh(f"getprop {key}", timeout=10).strip()
