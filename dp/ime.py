"""Typing through the droid-pilot keyboard (ime/): any language, no conversion by the device keyboard.

dp switches the device to this keyboard only for the moment it types, then switches back.
Installing it counts as an install (emu / test devices only); elsewhere dp falls back to
`adb shell input text` (ASCII only).
"""
import base64
import re
import time
from pathlib import Path

from . import javabuild
from .out import Fail

PROJECT = Path(__file__).resolve().parent.parent / "ime"
PACKAGE = "dev.droidpilot.ime"
IME_ID = f"{PACKAGE}/.DpIme"
ACTION = f"{PACKAGE}.TEXT"
USB_INSTALL_HINT = ("端末の前で dp setup を実行し、確認が出たら「インストール」を押してください"
                    "（数秒で「拒否」になります。入れるのは中身を変えたときだけです）")


def build() -> Path:
    return javabuild.build_apk(PROJECT, "dp-ime", "dp のキーボード", javabuild.source_hash(PROJECT))


def installed_version(dev):
    out = dev.adb.sh(f"dumpsys package {PACKAGE} | grep versionName", timeout=15)
    m = re.search(r"versionName=(\S+)", out)
    return m.group(1) if m else None


def ensure(dev):
    """Installs or updates the keyboard and enables it. Callers check the device kind first."""
    apk = build()
    want = javabuild.source_hash(PROJECT)
    if installed_version(dev) != want:
        p = dev.adb.run("install", "-r", str(apk), timeout=120)
        out = (p.stdout + p.stderr).strip()
        if "Success" not in out:
            if "USER_RESTRICTED" in out:
                raise Fail("dp のキーボードを入れるには、端末に出る「USB経由でインストール」で人が「インストール」を押す必要があります",
                           [USB_INSTALL_HINT])
            raise Fail("dp のキーボードを入れられません", [out[-300:]])
    dev.adb.sh(f"ime enable {IME_ID}", timeout=15)


def current(dev):
    return dev.adb.sh("settings get secure default_input_method", timeout=10).strip()


def type_text(dev, text, clear=False, enter=False, timeout=6.0):
    """Commits text into the focused field. Restores the previous keyboard afterwards."""
    prev = current(dev)
    b64 = base64.b64encode(text.encode()).decode()
    cmd = (f"am broadcast -a {ACTION} -p {PACKAGE} --es b64 {b64}"
           + (" --ez clear true" if clear else "") + (" --ez enter true" if enter else ""))
    if prev != IME_ID:
        dev.adb.sh(f"ime set {IME_ID}", timeout=15, check=True)
    try:
        end = time.time() + timeout
        code = None
        while True:
            out = dev.adb.sh(cmd, timeout=15)
            m = re.search(r"result=(-?\d+)", out)
            code = int(m.group(1)) if m else None
            if code in (1, 3) or time.time() > end:
                break
            time.sleep(0.3)  # 0: the keyboard is still starting; 2: the field is not bound yet
    finally:
        if prev and prev != IME_ID and prev != "null":
            dev.adb.sh(f"ime set {prev}", timeout=15)
    if code == 1:
        return
    if code == 2:
        raise Fail("文字を入れる欄にフォーカスがありません", ["--into 対象 で欄を指定してください"])
    raise Fail("dp のキーボードが応答しません", [f"am broadcast の結果: {code}"])
