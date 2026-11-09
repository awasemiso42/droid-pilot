"""Which device, what kind (emu / test / safe), and what is on its screen."""
import os
import re
import time

from . import config
from .adb import Adb, list_devices
from .out import Fail

KINDS = ("emu", "test", "safe")


class Device:
    def __init__(self, serial, model=""):
        self.serial = serial
        self.adb = Adb(serial)
        self.model = model
        self._props = {}

    # --- identity -------------------------------------------------------
    def prop(self, key):
        if key not in self._props:
            self._props[key] = self.adb.getprop(key)
        return self._props[key]

    @property
    def is_emulator(self):
        return self.serial.startswith("emulator-") or self.prop("ro.kernel.qemu") == "1" \
            or self.prop("ro.boot.qemu") == "1"

    @property
    def kind(self):
        if self.is_emulator:
            return "emu"
        return "test" if config.device_policies().get(self.serial) == "test" else "safe"

    @property
    def api(self):
        try:
            return int(self.prop("ro.build.version.sdk"))
        except ValueError:
            return 0

    @property
    def abi(self):
        return self.prop("ro.product.cpu.abi")

    def label(self):
        name = self.model or self.prop("ro.product.model")
        return f"{self.serial} ({self.kind}, {name}, API {self.api})"

    # --- screen state ---------------------------------------------------
    def natural_size(self):
        out = self.adb.sh("wm size", timeout=10)
        m = re.search(r"Override size: (\d+)x(\d+)", out) or re.search(r"Physical size: (\d+)x(\d+)", out)
        if not m:
            raise Fail("画面の大きさが取れません", [out.strip()])
        return int(m.group(1)), int(m.group(2))

    def foreground(self):
        """(package, activity or window name) of what has focus now."""
        out = self.adb.sh("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", timeout=10)
        focus = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}]+)\}", out)
        if focus:
            name = focus.group(1).strip()
            if "/" in name:
                pkg, act = name.split("/", 1)
                return pkg, act
        app = re.search(r"mFocusedApp=.*? ([\w.]+)/([\w.$]+)", out)
        if app:
            return app.group(1), app.group(2)
        if focus:
            return "", focus.group(1).strip()
        return "", ""

    def locked(self):
        out = self.adb.sh("dumpsys window | grep -E 'mDreamingLockscreen|mShowingLockscreen|"
                          "isKeyguardShowing|mKeyguardShowing'; "
                          "dumpsys power | grep -E 'mWakefulness='", timeout=10)
        if re.search(r"(mDreamingLockscreen|mShowingLockscreen|mKeyguardShowing|isKeyguardShowing)=true", out):
            return "ロック画面"
        if re.search(r"mWakefulness=(Asleep|Dozing)", out):
            return "画面が消えている"
        return ""

    def epoch(self):
        """Device clock in seconds (float), for logcat -T."""
        out = self.adb.sh("date +%s.%N", timeout=10).strip()
        try:
            return float(out)
        except ValueError:
            return float(self.adb.sh("date +%s", timeout=10).strip())


def select(want=None) -> Device:
    """-s value: a serial (or its start), 'emu', or 'real'. Without it: the only device,
    else an emulator when one is there (a real device must be named)."""
    want = want or os.environ.get("DP_SERIAL")
    devs = list_devices()
    online = [d for d in devs if d["state"] == "device"]
    bad = [d for d in devs if d["state"] != "device"]
    if not online:
        lines = [f"{d['serial']}: {d['state']}" for d in bad]
        if any(d["state"] == "unauthorized" for d in bad):
            lines.append("端末の画面で USB デバッグを許可してください")
        raise Fail("端末がつながっていません。タブレットを USB でつなぎ、画面で USB デバッグを許可してください", lines)

    def is_emu(d):
        return d["serial"].startswith("emulator-")

    if want:
        if want == "emu":
            pick = [d for d in online if is_emu(d)]
        elif want == "real":
            pick = [d for d in online if not is_emu(d)]
        else:
            pick = [d for d in online if d["serial"] == want] or \
                   [d for d in online if d["serial"].startswith(want)]
        if not pick:
            raise Fail(f"端末「{want}」がありません", [d["serial"] for d in online])
        d = pick[0]
    elif len(online) == 1:
        d = online[0]
    else:
        emus = [d for d in online if is_emu(d)]
        if not emus:
            raise Fail("実機が 2 台以上つながっています。-s で選んでください", [d["serial"] for d in online])
        d = emus[0]
    return Device(d["serial"], d.get("model", "").replace("_", " "))


def wait_boot(dev: Device, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if dev.adb.sh("getprop sys.boot_completed", timeout=10).strip() == "1":
                return True
        except Fail:
            pass
        time.sleep(2)
    return False
