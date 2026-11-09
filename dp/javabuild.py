"""Builds dp's two small device helpers from source on this PC (no Gradle):
inject/ (a dex run with app_process) and ime/ (the droid-pilot keyboard APK).
Outputs are cached in ~/.cache/droid-pilot by source hash."""
import glob
import hashlib
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import config
from .out import Fail

ROOT = Path(__file__).resolve().parent.parent


def source_hash(*paths) -> str:
    h = hashlib.sha256()
    for base in paths:
        files = sorted(p for p in Path(base).rglob("*") if p.is_file()) if Path(base).is_dir() else [Path(base)]
        for f in files:
            h.update(str(f.relative_to(ROOT)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


def jdk_home():
    """A full JDK (with javac): JAVA_HOME when it is one, else Android Studio's bundled JDK
    (what Android Studio builds with), else other JDKs found on this PC."""
    home = os.environ.get("JAVA_HOME")
    h = Path.home()
    cands = [Path(home)] if home else []
    cands += [h / ".local/opt/android-studio/jbr", h / "android-studio/jbr",
              Path("/opt/android-studio/jbr"), Path("/snap/android-studio/current/jbr")]
    on_path = shutil.which("javac")
    if on_path:
        cands.append(Path(on_path).resolve().parent.parent)
    cands += [Path(p) for p in sorted(glob.glob(str(h / ".gradle/jdks/*")))]
    return next((c for c in cands if (c / "bin" / "javac").is_file()), None)


def _javac():
    home = jdk_home()
    return home / "bin" / "javac" if home else None


def _newest(pattern, leaf):
    def ver(p):
        return [int(x) if x.isdigit() else 0
                for x in Path(p).name.replace("android-", "").replace("-", ".").split(".")]
    dirs = sorted((d for d in glob.glob(pattern) if (Path(d) / leaf).exists()), key=ver)
    return Path(dirs[-1]) / leaf if dirs else None


class Tools:
    def __init__(self, what):
        sdk = config.sdk_dir()
        bt = str(sdk / "build-tools" / "*")
        self.javac = _javac()
        self.android_jar = _newest(str(sdk / "platforms" / "android-*"), "android.jar")
        self.d8 = _newest(bt, "d8")
        self.aapt2 = _newest(bt, "aapt2")
        self.zipalign = _newest(bt, "zipalign")
        self.apksigner = _newest(bt, "apksigner")
        self.what = what
        missing = [n for n, v in (("JDK の javac", self.javac), ("build-tools", self.d8),
                                  ("platforms の android.jar", self.android_jar)) if not v]
        if missing:
            raise Fail(f"{what}を作れません: " + "・".join(missing) + " が見つかりません",
                       ["JAVA_HOME を JDK 11 以上に向けるか、Android Studio と SDK を入れてください"])
        self.env = dict(os.environ)
        self.env.setdefault("JAVA_HOME", str(self.javac.parent.parent))

    def run(self, cmd):
        p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=180, env=self.env)
        if p.returncode != 0:
            raise Fail(f"{self.what}のビルドに失敗: {Path(str(cmd[0])).name}",
                       (p.stdout + p.stderr).strip().splitlines()[-8:])
        return p.stdout

    def dex(self, sources, out_dir: Path) -> Path:
        """javac + d8; returns the classes.dex path."""
        classes = out_dir / "classes"
        self.run([self.javac, "--release", "11", "-nowarn", "-cp", self.android_jar, "-d", classes, *sources])
        class_files = [str(p) for p in classes.rglob("*.class")]
        if not class_files:
            raise Fail(f"{self.what}のビルドに失敗: クラスができていません")
        dex_dir = out_dir / "dex"
        dex_dir.mkdir()
        self.run([self.d8, "--min-api", "26", "--lib", self.android_jar, "--output", dex_dir, *class_files])
        return dex_dir / "classes.dex"


def build_dex_jar(src: Path, name: str, what: str) -> Path:
    out = config.CACHE / f"{name}-{source_hash(src)}.jar"
    if out.exists():
        return out
    tools = Tools(what)
    with tempfile.TemporaryDirectory() as td:
        dex = tools.dex([src], Path(td))
        tmp = Path(td) / "out.jar"
        with zipfile.ZipFile(tmp, "w") as z:
            z.write(dex, "classes.dex")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp, out)
    return out


def build_apk(project: Path, name: str, what: str, version_name: str) -> Path:
    """manifest + res/ + src/ -> a signed debug APK (debug keystore)."""
    out = config.CACHE / f"{name}-{version_name}.apk"
    if out.exists():
        return out
    tools = Tools(what)
    if not (tools.aapt2 and tools.zipalign and tools.apksigner):
        raise Fail(f"{what}を作れません: build-tools の aapt2 / zipalign / apksigner が見つかりません")
    keystore = Path.home() / ".android" / "debug.keystore"
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        if not keystore.exists():
            keystore = t / "debug.keystore"
            tools.run([tools.javac.parent / "keytool", "-genkeypair", "-keystore", keystore, "-storepass", "android",
                       "-alias", "androiddebugkey", "-keypass", "android", "-keyalg", "RSA", "-validity", "10000",
                       "-dname", "CN=Android Debug,O=Android,C=US"])
        tools.run([tools.aapt2, "compile", "--dir", project / "res", "-o", t / "res.zip"])
        tools.run([tools.aapt2, "link", "-o", t / "base.apk", "-I", tools.android_jar,
                   "--manifest", project / "AndroidManifest.xml", "--min-sdk-version", "26",
                   "--target-sdk-version", "34", "--version-code", "1", "--version-name", version_name,
                   t / "res.zip"])
        dex = tools.dex([str(p) for p in (project / "src").rglob("*.java")], t)
        with zipfile.ZipFile(t / "base.apk", "a") as z:
            z.write(dex, "classes.dex")
        tools.run([tools.zipalign, "-f", "4", t / "base.apk", t / "aligned.apk"])
        tools.run([tools.apksigner, "sign", "--ks", keystore, "--ks-pass", "pass:android",
                   "--ks-key-alias", "androiddebugkey", "--key-pass", "pass:android",
                   "--out", t / "signed.apk", t / "aligned.apk"])
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(t / "signed.apk", out)
    return out
