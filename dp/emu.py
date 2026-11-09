"""Creating and starting the emulator (Google APIs image: no Play Store, so no purchases)."""
import os
import re
import subprocess
import time
from pathlib import Path

from . import config
from .adb import list_devices, Adb
from .out import Fail

AVD = "dp-tablet"
IMAGE = "system-images;android-36;google_apis;x86_64"
DEVICE = "pixel_tablet"
RAM_MB = 3072


def _emulator():
    return str(config.sdk_dir() / "emulator" / "emulator")


def _avdmanager():
    return str(config.sdk_dir() / "cmdline-tools" / "latest" / "bin" / "avdmanager")


def avds():
    p = subprocess.run([_emulator(), "-list-avds"], capture_output=True, text=True, timeout=30)
    return [l.strip() for l in p.stdout.splitlines() if l.strip() and not l.startswith("INFO")]


def avd_dir(name):
    home = Path(os.environ.get("ANDROID_AVD_HOME") or Path.home() / ".android" / "avd")
    return home / f"{name}.avd"


def image_installed(image=IMAGE):
    return (config.sdk_dir() / image.replace(";", "/")).exists()


def create(name=AVD, image=IMAGE, device=DEVICE, ram=RAM_MB):
    if name in avds():
        return f"{name} は作成済み"
    if not image_installed(image):
        raise Fail(f"システムイメージ {image} がありません",
                   [f"{config.sdk_dir()}/cmdline-tools/latest/bin/sdkmanager --install '{image}'"])
    p = subprocess.run([_avdmanager(), "create", "avd", "-n", name, "-k", image, "-d", device],
                       input="no\n", capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise Fail("AVD を作れませんでした", (p.stdout + p.stderr).strip().splitlines()[-6:])
    ini = avd_dir(name) / "config.ini"
    if ini.exists():
        text = ini.read_text()
        for key, value in (("hw.ramSize", str(ram)), ("hw.keyboard", "yes"), ("showDeviceFrame", "no")):
            if re.search(rf"^{re.escape(key)}\s*=", text, re.M):
                text = re.sub(rf"^{re.escape(key)}\s*=.*$", f"{key}={value}", text, flags=re.M)
            else:
                text += f"\n{key}={value}"
        ini.write_text(text.strip() + "\n")
    return f"{name} を作成（{device}、{image.split(';')[1]}、RAM {ram}MB）"


def running():
    """{avd name: serial} for running emulators."""
    out = {}
    for d in list_devices():
        if d["serial"].startswith("emulator-") and d["state"] in ("device", "offline"):
            p = Adb(d["serial"]).run("emu", "avd", "name", timeout=10)
            name = p.stdout.splitlines()[0].strip() if p.stdout.strip() else ""
            out[name] = d["serial"]
    return out


def start(name=AVD, headless=False, timeout=300):
    from .device import Device, wait_boot
    run = running()
    if name in run:
        serial = run[name]
        started = False
    else:
        if name not in avds():
            raise Fail(f"AVD {name} がありません。dp emu create で作ってください")
        before = {d["serial"] for d in list_devices()}
        log = config.CACHE / f"emulator-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        args = [_emulator(), "-avd", name, "-no-boot-anim", "-no-audio", "-netdelay", "none", "-netspeed", "full"]
        if headless:
            args += ["-no-window"]
        with open(log, "w") as f:
            subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        serial = None
        end = time.time() + 60
        while time.time() < end and not serial:
            time.sleep(2)
            new = [d["serial"] for d in list_devices()
                   if d["serial"].startswith("emulator-") and d["serial"] not in before]
            serial = new[0] if new else None
        if not serial:
            tail = log.read_text().strip().splitlines()[-6:] if log.exists() else []
            raise Fail("エミュレータが起動しませんでした", tail + [f"ログ: {log}"])
        started = True
    dev = Device(serial)
    if not wait_boot(dev, timeout):
        raise Fail(f"{serial} の起動が {timeout} 秒で終わりませんでした")
    prepare(dev)
    return serial, started


def prepare(dev):
    """Emulator only: no animations (steadier UI dumps), screen stays on, no lock screen."""
    dev.adb.sh("settings put global window_animation_scale 0; "
               "settings put global transition_animation_scale 0; "
               "settings put global animator_duration_scale 0; "
               "svc power stayon true; wm dismiss-keyguard; input keyevent 224", timeout=20)


def stop(serial):
    Adb(serial).run("emu", "kill", timeout=20)
