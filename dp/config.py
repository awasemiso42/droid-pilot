"""Configuration: apps.toml, policy.toml, devices.toml and the SDK location."""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("DP_CONFIG", HOME / "config"))
CACHE = Path(os.environ.get("DP_CACHE", Path.home() / ".cache" / "droid-pilot"))


def sdk_dir() -> Path:
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(key):
            return Path(os.environ[key])
    return Path.home() / "Android" / "Sdk"


def adb_path() -> str:
    return os.environ.get("DP_ADB") or str(sdk_dir() / "platform-tools" / "adb")


def load(name: str) -> dict:
    path = CONFIG / name
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class App:
    name: str
    package: str
    repo: Path
    build: str = ""
    apk: list = field(default_factory=list)
    code: list = field(default_factory=list)
    outward: list = field(default_factory=list)
    launch: str = ""

    @property
    def state_dir(self) -> Path:
        return self.repo / ".dp"


def apps() -> dict:
    out = {}
    for name, a in load("apps.toml").get("apps", {}).items():
        apk = a.get("apk", [])
        out[name] = App(
            name=name,
            package=a["package"],
            repo=Path(os.path.expanduser(a.get("repo", "."))).resolve(),
            build=a.get("build", ""),
            apk=[apk] if isinstance(apk, str) else list(apk),
            code=list(a.get("code", [])) + [a["package"]],
            outward=list(a.get("outward", [])),
            launch=a.get("launch", ""),
        )
    return out


def find_app(name=None, cwd=None):
    """The app named by -a, else the one whose repository holds the current directory."""
    all_apps = apps()
    if name:
        if name in all_apps:
            return all_apps[name]
        for a in all_apps.values():
            if a.package == name:
                return a
        from .out import Fail
        raise Fail(f"アプリ「{name}」は apps.toml にありません",
                   [f"あるもの: {', '.join(all_apps)}" if all_apps else
                    "config/apps.example.toml を config/apps.toml にコピーして書いてください"])
    cwd = Path(cwd or os.getcwd()).resolve()
    for a in all_apps.values():
        if cwd == a.repo or a.repo in cwd.parents:
            return a
    return None


def policy() -> dict:
    p = load("policy.toml")
    return {
        "outward": [re.compile(x, re.I) for x in p.get("outward", [])],
        "destructive": [re.compile(x, re.I) for x in p.get("destructive", [])],
        "allowed_packages": set(p.get("allowed_packages", [])),
        "test_only_packages": set(p.get("test_only_packages", [])),
        "permission_packages": set(p.get("permission_packages", [])),
    }


def log_ignore() -> list:
    return [re.compile(x) for x in load("logs.toml").get("ignore", [])]


def device_policies() -> dict:
    return {serial: d.get("policy", "safe") for serial, d in load("devices.toml").get("devices", {}).items()}


def register_device(serial: str, policy_name: str, note: str = "") -> Path:
    """Adds or replaces one [devices."serial"] table, keeping the rest of the file as written."""
    path = CONFIG / "devices.toml"
    text = path.read_text() if path.exists() else ""
    header = f'[devices."{serial}"]'
    # Drop an existing table for this serial (up to the next table or the end).
    text = re.sub(r'(?ms)^\[devices\."' + re.escape(serial) + r'"\]\n.*?(?=^\[|\Z)', "", text)
    block = f'{header}\npolicy = "{policy_name}"\n'
    if note:
        block += f'note = "{note}"\n'
    text = text.rstrip("\n") + "\n\n" + block
    path.write_text(text)
    return path
