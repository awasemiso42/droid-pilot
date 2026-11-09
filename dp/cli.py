"""dp: commands. Each returns a Result whose first line is OK / NG / BLOCKED."""
import argparse
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import (actions, build as buildmod, config, emu, flow as flowmod, ime, inject, javabuild, logs,
               safety, shot, ui)
from .device import Device, select
from .adb import list_devices
from .out import Blocked, Fail, Result, ok, ng, OK, NG, BLOCKED
from .state import State, rel

RESOLVE_WAIT_IN_FLOW = 5.0


class Ctx:
    def __init__(self, args):
        self.args = args
        self.app = config.find_app(getattr(args, "app", None))
        self.state = State(self.app)
        self._dev = None
        self.in_flow = False
        self.flow_yes = False

    @property
    def dev(self) -> Device:
        if self._dev is None:
            self._dev = select(getattr(self.args, "serial", None))
        return self._dev

    @property
    def dstate(self) -> dict:
        return self.state.dev(self.dev.serial)

    def need_app(self):
        if not self.app:
            names = ", ".join(config.apps())
            raise Fail("アプリが決まりません。-a で選ぶか、アプリのリポジトリの中で実行してください", [f"アプリ: {names}"])
        return self.app

    def capture(self, store=True) -> ui.Screen:
        sc = ui.capture(self.dev)
        if store:
            self.dstate["screen"] = sc.to_state()
            self.dstate["screen_t"] = time.time()
        return sc

    def step(self, desc: str):
        """Numbers the step and writes a marker into logcat. Returns (n, device time)."""
        n = self.state.next_step()
        msg = f"step {n}: {desc}"[:200]
        out = self.dev.adb.sh(f"log -t DP {shlex.quote(msg)}; date +%s.%N", timeout=10).strip()
        try:
            dev_t = float(out.splitlines()[-1])
        except (ValueError, IndexError):
            dev_t = None
        return n, dev_t

    def journal(self, n, cmd, res: Result, dev_t=None, **extra):
        self.state.journal({"n": n, "cmd": cmd, "status": res.first_line().split()[0],
                            "dev": self._dev.serial if self._dev else None,
                            "app": self.app.name if self.app else None, "dev_t": dev_t,
                            "flow": self.in_flow, **extra})


# --- helpers ---------------------------------------------------------------------

def _since_launch(ctx):
    t = ctx.dstate.get("launch_t")
    return t


def _resolve(ctx, screen: ui.Screen, target: str):
    """(node or None, device x, y, stable selector, note). Waits a little inside flows."""
    kind, *rest = ui.parse_target(target)
    s = screen.scale
    if kind == "index":
        i = rest[0]
        prev = ctx.dstate.get("screen")
        if not prev:
            raise Fail("番号を使う前に dp screen で画面を見てください")
        if not 1 <= i <= len(prev["elems"]):
            raise Fail(f"[{i}] はありません（前の画面は {len(prev['elems'])} 個）")
        old = prev["elems"][i - 1]
        note = ""
        if prev["hash"] == screen.hash and i <= len(screen.elems):
            e = screen.elems[i - 1]
        else:
            cand = [e for e in screen.elems if (e.kind, e.label, e.node.rid) == (old["kind"], old["label"], old["rid"])]
            if not cand:
                raise Fail(f"stale: [{i}] {old['kind']} 「{old['label']}」は今の画面にありません。dp screen で見直してください")
            ox = (old["bounds"][0] + old["bounds"][2]) / 2
            oy = (old["bounds"][1] + old["bounds"][3]) / 2
            e = min(cand, key=lambda c: abs(c.center[0] - ox) + abs(c.center[1] - oy))
            note = f"（画面が変わっていたので「{e.label}」を探し直しました）"
        x, y = e.center
        return e.node, x, y, ui.stable_selector(screen, e), note
    if kind == "point":
        x, xp, y, yp = rest
        dx = x / 100 * screen.width if xp else x / s
        dy = y / 100 * screen.height if yp else y / s
        dx, dy = int(min(max(dx, 0), screen.width - 1)), int(min(max(dy, 0), screen.height - 1))
        node = ui.node_at(screen, dx, dy)
        sel = target if xp else f"@{100 * dx / screen.width:.1f}%,{100 * dy / screen.height:.1f}%"
        return node, dx, dy, sel, ""
    value, nth = rest
    deadline = time.time() + (RESOLVE_WAIT_IN_FLOW if ctx.in_flow else 0)
    while True:
        nodes = ui.find(screen, kind, value)
        if len(nodes) >= nth or time.time() >= deadline:
            break
        time.sleep(0.8)
        screen = ctx.capture()
    if len(nodes) < nth:
        near = [e.label for e in screen.elems if e.label and value.casefold()[:2] in e.label.casefold()][:5]
        raise Fail(f"{kind}:{value} が見つかりません" + (f"（{nth} 番目）" if nth > 1 else ""),
                   [f"近いもの: {' / '.join(near)}"] if near else ["dp screen で今の画面を見てください"])
    node = nodes[nth - 1]
    x, y = node.center
    note = f"（{len(nodes)} 件一致、{nth} 番目）" if len(nodes) > 1 else ""
    sel = ui.q(f"{kind}:{value}" + (f"#{nth}" if nth > 1 else ""))
    return node, x, y, sel, note


def _act(ctx, verb, target, do, yes=False, extra_cmd=""):
    """Common path for actions that press somewhere: resolve, check, mark, act, summarize."""
    ctx.need_app()
    dev = ctx.dev
    before = ctx.capture(store=False)
    prev = ctx.dstate.get("screen")
    node, x, y, sel, note = _resolve(ctx, before, target)
    safety.check_unlocked(dev)
    approved = safety.check_press(dev, ctx.app, before, node, yes=yes or ctx.flow_yes)
    cmd = f"{verb} {sel}{extra_cmd}"
    n, dev_t = ctx.step(cmd)
    t0 = time.time()
    do(x, y)
    by_position = ui.parse_target(target)[0] in ("index", "point")
    head = f"{verb} [{target}]" if target.isdigit() else f"{verb} {target}" if by_position else cmd
    return _after(ctx, n, cmd, dev_t, t0, prev or before.to_state(), note, approved=approved,
                  label=_label_of(before, node) if by_position else "", head=head)


def _label_of(screen, node):
    e = ui.elem_for(screen, node) if node is not None else None
    return f' "{e.label}"' if e is not None and e.label else ""


def _after(ctx, n, cmd, dev_t, t0, before_state, note="", approved="", label="", extra=None, head=None):
    time.sleep(0.3)
    try:
        after = ctx.capture()
        lines = actions.summarize(before_state, after, time.time() - t0)
    except ui.IdleError:
        lines = [f"→ 画面が動き続けているため UI ツリーなし（{time.time() - t0:.1f}s）。dp shot で確認を"]
    if extra:
        lines.insert(0, extra)
    if approved:
        lines.append(f"人の確認済み: {approved}")
    res = ok(f"{head or cmd}{label} {note}".rstrip(), lines)
    ctx.journal(n, cmd, res, dev_t, approved=approved or None)
    return res


# --- commands ----------------------------------------------------------------------

def c_devices(ctx, a):
    lines = []
    for d in list_devices():
        if d["state"] != "device":
            lines.append(f"  {d['serial']}  {d['state']}")
            continue
        dev = Device(d["serial"], d.get("model", "").replace("_", " "))
        hint = "  （テスト用なら人が dp register で登録）" if dev.kind == "safe" else ""
        lines.append(f"  {dev.serial}  {dev.kind:<4}  {dev.model or dev.prop('ro.product.model')}  "
                     f"API {dev.api}  {dev.abi}{hint}")
    if not lines:
        return ng("端末がつながっていません。タブレットを USB でつなぎ、画面で USB デバッグを許可してください")
    return ok(f"{len(lines)} 台", lines)


def c_doctor(ctx, a):
    lines, bad = [], 0

    def add(good, text):
        nonlocal bad
        lines.append(("ok " if good else "NG ") + text)
        bad += 0 if good else 1

    p = subprocess.run([config.adb_path(), "version"], capture_output=True, text=True)
    add(p.returncode == 0, "adb " + (p.stdout.splitlines()[1] if p.returncode == 0 else config.adb_path()))
    mem = _mem_available_gb()
    add(mem >= 2, f"PC の空きメモリ {mem:.1f} GB" + ("（2 GB 未満。使っていない Gradle デーモンを止めて）" if mem < 2 else ""))
    try:
        dev = ctx.dev
    except Fail as e:
        add(False, e.msg)
        return Result(f"{bad} 件の問題", lines, NG)
    add(True, f"端末 {dev.label()}  ABI {dev.abi}")
    why = dev.locked()
    add(not why, "画面ロック: " + (why or "なし"))
    t0 = time.time()
    try:
        sc = ctx.capture()
        add(True, f"UI ツリー {time.time() - t0:.1f}s（{len(sc.elems)} 要素、前面 {sc.package}）")
    except Fail as e:
        add(False, f"UI ツリー: {e.msg}")
    if dev.kind in safety.OPS["install"]:
        have = ime.installed_version(dev)
        want = javabuild.source_hash(ime.PROJECT)
        kb = "入っている" if have == want else ("古い（dp setup で更新）" if have else "まだ無し（人が端末の前で dp setup）")
        lines.append(f"-- dp のキーボード（日本語の入力）: {kb}")
    probe = dev.adb.sh("logcat -d -t 1 --uid=1000 2>&1 | head -2", timeout=10)
    add(True, "logcat --uid " + ("使えない（pid で絞ります）" if "nrecognized" in probe or "invalid" in probe else "使える"))
    if ctx.app:
        app = ctx.app
        info = _installed(dev, app.package)
        if info:
            add(True, f"{app.name}: 入っている {info.get('name', '?')} (versionCode {info.get('code', '?')})"
                      + ("、debuggable" if info.get("debuggable") else "、debuggable でない"))
        else:
            add(True, f"{app.name}: まだ入っていない")
        apk = buildmod.find_apk(app, dev.abi)
        if apk:
            age = (time.time() - apk.stat().st_mtime) / 3600
            add(True, f"APK {rel(apk)}（{age:.1f} 時間前）")
        else:
            add(True, "APK まだ無し（dp build）")
        lines.append("-- probe: 未対応（第 2 段階）")
    return Result("問題なし" if not bad else f"{bad} 件の問題", lines, OK if not bad else NG)


def _mem_available_gb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 99.0


def _installed(dev, package):
    out = dev.adb.sh(f"dumpsys package {shlex.quote(package)} | grep -E 'versionCode=|versionName=|pkgFlags=' | head -3",
                     timeout=15)
    if "versionCode" not in out:
        return None
    m = re.search(r"versionCode=(\d+)", out)
    n = re.search(r"versionName=(\S+)", out)
    return {"code": int(m.group(1)) if m else None, "name": n.group(1) if n else None,
            "debuggable": "DEBUGGABLE" in out}


def c_build(ctx, a):
    app = ctx.need_app()
    good, took, errors, log = buildmod.run(app, ctx.state.run_dir("build"))
    if good:
        return ok(f"build {took:.0f}s", data={"log": str(log)})
    lines = errors[:buildmod.MAX_ERRORS]
    if len(errors) > buildmod.MAX_ERRORS:
        lines.append(f"(+{len(errors) - buildmod.MAX_ERRORS} 件)")
    lines.append(f"全文: {rel(log)}")
    return ng(f"build {took:.0f}s  エラー {len([e for e in errors if not e.startswith('（')])} 件", lines)


def c_install(ctx, a):
    app = ctx.need_app()
    dev = ctx.dev
    safety.require(dev, "install")
    apk = Path(a.apk) if getattr(a, "apk", None) else buildmod.find_apk(app, dev.abi)
    if not apk or not apk.exists():
        return ng("APK がありません。dp build でビルドしてください", [f"探した場所: {', '.join(app.apk)}"])
    new_code, new_name = buildmod.apk_info(apk, config.sdk_dir())
    cur = _installed(dev, app.package)
    flags = ["-r", "-t"]
    notes = []
    if cur and new_code is not None and cur.get("code") is not None and new_code < cur["code"]:
        safety.require(dev, "reinstall")
        flags.append("-d")
        notes.append(f"versionCode が下がるので -d（{cur['code']} → {new_code}）")
    if getattr(a, "reinstall", False):
        safety.require(dev, "reinstall")
        dev.adb.run("uninstall", app.package, timeout=60)
        notes.append("入れ直し（データは消えました）")
    n, dev_t = ctx.step(f"install {apk.name}")
    t0 = time.time()
    p = dev.adb.run("install", *flags, str(apk), timeout=600)
    out = (p.stdout + p.stderr).strip()
    took = time.time() - t0
    if "Success" not in out:
        m = re.search(r"(INSTALL_[A-Z_]+)[^\n\]]*", out)
        why = m.group(0) if m else out.splitlines()[-1] if out else "?"
        hint = []
        if "UPDATE_INCOMPATIBLE" in why:
            hint.append("署名が違います。dp install --reinstall で入れ直せます（データが消えます）"
                        if dev.kind != "safe" else "署名が違います。人に聞いてください")
        if "USER_RESTRICTED" in why:
            hint.append("端末に「USB経由でインストール」の確認が出ます。人が端末の前で同じコマンドを実行し、"
                        "「インストール」を押してください（数秒で「拒否」になります）")
        if "NO_MATCHING_ABIS" in why:
            hint.append(f"この APK は端末の ABI（{dev.abi}）に合いません")
        res = ng(f"install {why[:120]}", hint)
        ctx.journal(n, "install", res, dev_t)
        return res
    ver = f"{new_name or '?'} (versionCode {new_code})" if new_code is not None else apk.name
    res = ok(f"install {ver} → {dev.serial}（{took:.0f}s）", notes)
    ctx.journal(n, "install", res, dev_t)
    return res


def _launch_activity(dev, app):
    if app.launch:
        return app.launch if "/" in app.launch else f"{app.package}/{app.launch}"
    out = dev.adb.sh(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {app.package}",
                     timeout=15)
    last = [l.strip() for l in out.splitlines() if "/" in l]
    if not last:
        raise Fail(f"{app.package} の起動画面が見つかりません（入っていますか？）")
    return last[-1]


def c_setup(ctx, a):
    """Builds dp's device helpers and puts them on the device. Run by a person at the device:
    some phones ask to confirm the keyboard install on screen."""
    dev = ctx.dev
    lines = []
    jar = inject.build()
    lines.append(f"線を引く補助  {jar.name}（端末には最初に描くときに送ります）")
    if dev.kind not in safety.OPS["install"]:
        lines.append("dp のキーボード  入れません（safe 端末。日本語の入力は emu / test 端末だけ）")
        return ok(f"setup {dev.serial} ({dev.kind})", lines)
    t0 = time.time()
    ime.ensure(dev)
    lines.append(f"dp のキーボード  入っています（{ime.installed_version(dev)}、{time.time() - t0:.0f}s）")
    return ok(f"setup {dev.serial} ({dev.kind})", lines)


def c_launch(ctx, a):
    app = ctx.need_app()
    dev = ctx.dev
    target = getattr(a, "target", None)
    if target and "://" in target:
        start = f"am start -W -a android.intent.action.VIEW -d {shlex.quote(target)} {app.package}"
        cmd = f"launch {shlex.quote(target)}"
    else:
        comp = (f"{app.package}/{target}" if target else _launch_activity(dev, app))
        start = f"am start -W -n {shlex.quote(comp)}"
        cmd = f"launch {target}" if target else "launch"
    n, dev_t = ctx.step(cmd)
    ctx.dstate["launch_t"] = dev_t
    t0 = time.time()
    out = dev.adb.sh(start, timeout=60)
    if "Error" in out or "Exception" in out:
        res = ng(f"{cmd}: 起動できません", [l for l in out.splitlines() if "Error" in l or "Exception" in l][:3])
        ctx.journal(n, cmd, res, dev_t)
        return res
    m = re.search(r"TotalTime: (\d+)", out)
    took = f" {int(m.group(1)) / 1000:.1f}s" if m else ""
    res = _after(ctx, n, cmd, dev_t, t0, None)
    res.head = f"{cmd}{took}"
    return res


def c_stop(ctx, a):
    app = ctx.need_app()
    n, dev_t = ctx.step("stop")
    ctx.dev.adb.sh(f"am force-stop {app.package}", timeout=15)
    res = ok(f"stop {app.name}")
    ctx.journal(n, "stop", res, dev_t)
    return res


def c_reset(ctx, a):
    app = ctx.need_app()
    safety.require(ctx.dev, "clear")
    n, dev_t = ctx.step("reset")
    out = ctx.dev.adb.sh(f"pm clear {app.package}", timeout=30).strip()
    res = ok(f"reset {app.name}（データを消しました）") if "Success" in out else ng(f"reset: {out[:120]}")
    ctx.journal(n, "reset", res, dev_t)
    return res


def c_up(ctx, a):
    app = ctx.need_app()
    dev = ctx.dev
    lines = [f"端末 {dev.label()}"]
    if not a.no_build:
        r = c_build(ctx, a)
        if not r.ok:
            return r
        lines.append(f"build    ok {r.head.split()[-1]}")
    if not a.no_install:
        a.apk = None
        a.reinstall = False
        r = c_install(ctx, a)
        if not r.ok:
            r.lines = lines + r.lines
            return r
        lines.append("install  ok " + r.head.split(" ", 1)[1])
        lines += ["         " + x for x in r.lines]
    a.target = None
    r = c_launch(ctx, a)
    if not r.ok:
        r.lines = lines + r.lines
        return r
    lines.append(f"launch   ok {r.head.split(' ', 1)[1] if ' ' in r.head else ''}".rstrip())
    sc = ctx.dstate.get("screen", {})
    lines.append(f"screen   {sc.get('activity', '?')}  {sc.get('width')}x{sc.get('height')} "
                 f"rot{sc.get('rotation')}  #{sc.get('hash')}")
    lines += r.lines
    if a.shot:
        lines.append("shot: " + _save_shot(ctx, mark_elems=True))
    time.sleep(1.0)
    mine, crashes, anr, markers, dropped = logs.collect(dev, app.package, ctx.dstate.get("launch_t"),
                                                        config.log_ignore())
    log_lines, counts = logs.report(mine, crashes, anr, markers, app.code)
    if counts["crash"] or counts["anr"] or counts["E"]:
        lines.append(f"log      crash {counts['crash']}、ANR {counts['anr']}、エラー {counts['E']}"
                     f"{_noise_note(dropped, '（', '）')}")
        lines += ["  " + l for l in log_lines[:12]]
        return Result(f"up {app.name}（起動後にエラーあり）", lines, NG)
    lines.append(f"log      エラーなし（警告 {counts['W']}{_noise_note(dropped, '、')}）")
    return ok(f"up {app.name} → {dev.serial} ({dev.kind})", lines)


def _save_shot(ctx, mark_elems=False, grid=False, region=None) -> str:
    img = shot.grab(ctx.dev)
    path = ctx.state.next_shot()
    if region:
        x, y, w, h = region
        sc_w, sc_h = img.size
        s = ui.scale_for(sc_w, sc_h)
        box = [round(x / s), round(y / s), round((x + w) / s), round((y + h) / s)]
        crop = img.crop(box)
        k = min(1.0, ui.SHOT_EDGE / max(crop.size))
        if k < 1:
            crop = crop.resize((round(crop.width * k), round(crop.height * k)))
        crop.save(path)
        per = crop.width / w
        return (f"{rel(path)} {crop.width}x{crop.height}（shot 座標 {x},{y} から {w}x{h} を切り抜き。"
                f"画像の 1px = shot {1 / per:.2f}）")
    small, s = shot.to_shot(img)
    if mark_elems:
        sc = ctx.dstate.get("screen")
        live = ctx.capture() if not sc else None
        elems = live.elems if live else [_ElemView(d) for d in sc["elems"]]
        shot.mark(small, elems, s)
    if grid:
        shot.grid(small)
    small.save(path)
    what = "（番号入り）" if mark_elems else ""
    what += "（目盛り入り）" if grid else ""
    return f"{rel(path)} {small.width}x{small.height}{what}"


class _ElemView:
    """A stored element, enough for drawing marks."""

    def __init__(self, d):
        self.i, self.kind, self.label = d["i"], d["kind"], d["label"]
        self.bounds = tuple(d["bounds"])


def c_screen(ctx, a):
    dev = ctx.dev
    try:
        sc = ctx.capture()
    except ui.IdleError as e:
        path = _save_shot(ctx, grid=a.grid)
        return ng(e.msg, [f"shot: {path}"])
    elems = sc.elems
    if a.find:
        f = a.find.casefold()
        elems = [e for e in elems if f in e.label.casefold() or f in e.node.rid.casefold()]
    lines = ui.render(elems, sc.scale, all_=a.all or bool(a.find), bounds=a.bounds)
    if not lines:
        lines = ["（表示できる要素がありません。キャンバスだけの画面なら dp shot --grid）"]
    if a.shot or a.mark or a.grid:
        lines.append("shot: " + _save_shot(ctx, mark_elems=a.mark, grid=a.grid))
    head = sc.header(ctx.app.package if ctx.app else None)
    return ok(head, lines)


def c_shot(ctx, a):
    region = None
    if a.region:
        region = _shot_box(a.region)
    if a.mark:
        ctx.capture()
    return ok("shot " + _save_shot(ctx, mark_elems=a.mark, grid=a.grid, region=region))


def c_tap(ctx, a):
    return _act(ctx, "tap", a.target, lambda x, y: actions.tap(ctx.dev.adb, x, y), yes=a.yes)


def c_long(ctx, a):
    extra = f" --ms {a.ms}" if a.ms != 800 else ""
    return _act(ctx, "long", a.target, lambda x, y: actions.long_press(ctx.dev.adb, x, y, a.ms),
                yes=a.yes, extra_cmd=extra)


def c_type(ctx, a):
    ctx.need_app()
    dev = ctx.dev
    # The droid-pilot keyboard types any language. It is an install, so only on emu / test devices.
    use_ime = dev.kind in safety.OPS["install"]
    if not use_ime and (not a.text.isascii() or a.clear):
        raise Fail("safe 端末では ASCII の追記だけです（dp のキーボードを入れないため）",
                   ["日本語や --clear は emu / test 端末で使えます", safety.REGISTER_HINT])
    before = ctx.capture(store=False)
    prev = ctx.dstate.get("screen") or before.to_state()
    safety.check_unlocked(dev)
    safety.check_foreground(dev, ctx.app, before)
    node = x = y = None
    sel = ""
    approved = ""
    if a.into:
        node, x, y, sel, _ = _resolve(ctx, before, a.into)
        approved = safety.check_press(dev, ctx.app, before, node, yes=a.yes or ctx.flow_yes)
    approved = safety.check_type(before, node, yes=a.yes or ctx.flow_yes) or approved
    cmd = (f"type {ui.q(a.text)}" + (f" --into {sel}" if a.into else "") + (" --clear" if a.clear else "")
           + (" --enter" if a.enter else ""))
    if use_ime:
        ime.ensure(dev)
    n, dev_t = ctx.step(cmd)
    t0 = time.time()
    if a.into:
        actions.tap(dev.adb, x, y)
        time.sleep(0.4)
    if use_ime:
        ime.type_text(dev, a.text, clear=a.clear, enter=a.enter)
    else:
        actions.text(dev.adb, a.text)
        if a.enter:
            actions.key(dev.adb, "enter")
    return _after(ctx, n, cmd, dev_t, t0, prev, approved=approved)


def c_key(ctx, a):
    ctx.need_app()
    dev = ctx.dev
    for k in a.keys:
        actions.keycode(k)
    before = ctx.dstate.get("screen")
    safety.check_unlocked(dev)
    cmd = "key " + " ".join(a.keys)
    n, dev_t = ctx.step(cmd)
    t0 = time.time()
    for k in a.keys:
        actions.key(dev.adb, k)
    return _after(ctx, n, cmd, dev_t, t0, before)


def _region(ctx, screen, target):
    if not target:
        return None, (0, 0, screen.width, screen.height), ""
    node, x, y, sel, _ = _resolve(ctx, screen, target)
    return node, (node.bounds if node is not None else (0, 0, screen.width, screen.height)), sel


def c_swipe(ctx, a, scroll=False):
    ctx.need_app()
    dev = ctx.dev
    before = ctx.capture(store=False)
    prev = ctx.dstate.get("screen") or before.to_state()
    safety.check_unlocked(dev)
    safety.check_foreground(dev, ctx.app, before)
    node, box, sel = _region(ctx, before, a.on)
    finger = a.direction
    if scroll:  # content direction -> finger moves the other way
        finger = {"up": "down", "down": "up", "left": "right", "right": "left"}[a.direction]
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    dx, dy = int((x2 - x1) * a.dist / 2), int((y2 - y1) * a.dist / 2)
    vec = {"up": (0, dy), "down": (0, -dy), "left": (dx, 0), "right": (-dx, 0)}[finger]
    sx, sy, ex, ey = cx + vec[0], cy + vec[1], cx - vec[0], cy - vec[1]
    verb = "scroll" if scroll else "swipe"
    cmd = f"{verb} {a.direction}" + (f" --on {sel}" if a.on else "") + (f" --dist {a.dist}" if a.dist != 0.6 else "")
    n, dev_t = ctx.step(cmd)
    t0 = time.time()
    actions.swipe(dev.adb, sx, sy, ex, ey, 300)
    return _after(ctx, n, cmd, dev_t, t0, prev)


def c_scroll(ctx, a):
    if not a.to:
        if not a.direction:
            raise Fail("dp scroll down / up / left / right、または dp scroll --to 対象")
        return c_swipe(ctx, a, scroll=True)
    # Scroll until the target is on screen.
    ctx.need_app()
    kind, *rest = ui.parse_target(a.to)
    if kind in ("index", "point"):
        raise Fail("--to には text: / id: / desc: を使ってください")
    direction = a.direction or "down"
    t0 = time.time()
    n, dev_t = ctx.step(f"scroll --to {a.to}")
    for i in range(a.max + 1):
        sc = ctx.capture()
        if ui.find(sc, kind, rest[0]):
            cmd = f"scroll {direction} --to {ui.q(a.to)}"
            res = ok(f"{cmd}（{i} 回）", ui.render(sc.elems, sc.scale, max_lines=12))
            ctx.journal(n, cmd, res, dev_t)
            return res
        safety.check_foreground(ctx.dev, ctx.app, sc)
        finger = {"up": "down", "down": "up", "left": "right", "right": "left"}[direction]
        x1, y1, x2, y2 = 0, 0, sc.width, sc.height
        lists = [e for e in sc.elems if e.kind == "list"]
        if lists:
            x1, y1, x2, y2 = max(lists, key=lambda e: e.node.area).bounds
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        dx, dy = int((x2 - x1) * 0.3), int((y2 - y1) * 0.3)
        vec = {"up": (0, dy), "down": (0, -dy), "left": (dx, 0), "right": (-dx, 0)}[finger]
        actions.swipe(ctx.dev.adb, cx + vec[0], cy + vec[1], cx - vec[0], cy - vec[1], 300)
        time.sleep(0.3)
    res = ng(f"{a.max} 回送っても {a.to} が出ません（{time.time() - t0:.0f}s）")
    ctx.journal(n, f"scroll --to {a.to}", res, dev_t)
    return res


def _shot_box(spec):
    try:
        v = [int(float(t)) for t in spec.split(",")]
        assert len(v) == 4 and v[2] > 0 and v[3] > 0
        return v
    except (ValueError, AssertionError):
        raise Fail("x,y,w,h（shot 座標）で書いてください", [f"受け取った値: {spec}"])


def _pixels_line(pct, what):
    if pct < 0.02:
        return f"→ {what}の画素が変わっていません（効いていない？ dp shot --region で確認を）"
    return f"→ {what}の画素の変化 {pct:.1f}%"


def c_draw(ctx, a):
    ctx.need_app()
    dev = ctx.dev
    before = ctx.capture(store=False)
    prev = ctx.dstate.get("screen") or before.to_state()
    safety.check_unlocked(dev)
    safety.check_foreground(dev, ctx.app, before)
    target = a.inside
    if a.shape.startswith("in:"):
        raise Fail("dp draw 形 in:対象 の順で書いてください")
    node, box, sel = _region(ctx, before, target[3:] if target and target.startswith("in:") else target)
    if a.box:
        x, y, w, h = _shot_box(a.box)
        k = before.scale
        box = (x / k, y / k, (x + w) / k, (y + h) / k)
    if a.shape in actions.PRESETS:
        strokes = actions.preset(a.shape, box)
        what = a.shape
    else:
        strokes = actions.parse_points(a.shape, before.scale)
        what = ui.q(a.shape)
    if a.pressure is not None and not 0 < a.pressure <= 1:
        raise Fail("--pressure は 0 より大きく 1 以下で")
    cmd = (f"draw {what}" + (f" in:{sel}" if target else "") + (f" --box {a.box}" if a.box else "")
           + (" --stylus" if a.stylus else "")
           + (f" --pressure {a.pressure:g}" if a.pressure is not None else ""))
    xs = [x for pts in strokes for x, _ in pts]
    ys = [y for pts in strokes for _, y in pts]
    span = (max(0, min(xs) - 40), max(0, min(ys) - 40),
            min(before.width, max(xs) + 40), min(before.height, max(ys) + 40))
    img0 = shot.grab(dev)
    n, dev_t = ctx.step(cmd)
    t0 = time.time()
    actions.draw(dev, strokes, stylus=a.stylus, pressure=a.pressure)
    time.sleep(0.3)
    pct = shot.region_change(img0, shot.grab(dev), span)
    return _after(ctx, n, cmd, dev_t, t0, prev, extra=_pixels_line(pct, "線のまわり"),
                  note=f"（{len(strokes)} 本）")


def c_zoom(ctx, a):
    ctx.need_app()
    dev = ctx.dev
    before = ctx.capture(store=False)
    prev = ctx.dstate.get("screen") or before.to_state()
    safety.check_unlocked(dev)
    safety.check_foreground(dev, ctx.app, before)
    if a.factor <= 1:
        raise Fail("--factor は 1 より大きく（2 で 2 倍）")
    node, box, sel = _region(ctx, before, a.on)
    x1, y1, x2, y2 = box
    vertical = (y2 - y1) > (x2 - x1)
    big = 0.6 * max(x2 - x1, y2 - y1)
    small = max(80, big / a.factor)
    d0, d1 = (small, big) if a.direction == "in" else (big, small)
    cmd = f"zoom {a.direction}" + (f" --on {sel}" if a.on else "") + (f" --factor {a.factor:g}" if a.factor != 2 else "")
    img0 = shot.grab(dev)
    n, dev_t = ctx.step(cmd)
    t0 = time.time()
    actions.pinch(dev, (x1 + x2) / 2, (y1 + y2) / 2, d0, d1, vertical=vertical)
    time.sleep(0.4)
    pct = shot.region_change(img0, shot.grab(dev), box)
    return _after(ctx, n, cmd, dev_t, t0, prev, extra=_pixels_line(pct, "対象の範囲"))


def _check(ctx, cond: str, screen=None):
    """(met, description) for wait / expect conditions."""
    if cond in ("nocrash", "noanr") or cond.startswith("nolog"):
        app = ctx.need_app()
        since = ctx.dstate.get("launch_t")
        mine, crashes, anr, markers, _ = logs.collect(ctx.dev, app.package, since, config.log_ignore())
        if cond == "nocrash":
            return not crashes, f"クラッシュ {len(crashes)} 件"
        if cond == "noanr":
            return not anr, f"ANR {len(anr)} 件"
        level = cond.split(":", 1)[1] if ":" in cond else "E"
        bad = [l for l in mine if l.level in logs.LEVELS[logs.LEVELS.index(level):]]
        return not bad and not crashes, f"{level} 以上のログ {len(bad)} 行" + (
            f"（最初: {bad[0].tag}: {bad[0].msg[:80]}）" if bad else "")
    screen = screen or ctx.capture()
    if cond.startswith("activity:"):
        want = cond.split(":", 1)[1]
        return screen.activity.endswith(want), f"今は {screen.activity}"
    state = next((f for f in STATE_CONDS if cond.startswith(f + ":")), None)
    neg = cond.startswith("gone:")
    sel = cond[len(state) + 1:] if state else cond[5:] if neg else cond
    kind, *rest = ui.parse_target(sel)
    if kind in ("index", "point"):
        raise Fail("条件には text: / text~: / id: / desc: / activity: / gone: / enabled: / disabled: / "
                   "checked: / selected: / nocrash / noanr / nolog を使ってください")
    found = ui.find(screen, kind, rest[0])
    if state:
        nth = rest[1] if len(rest) > 1 else 1
        if len(found) < nth:
            return False, f"{sel} が見つかりません"
        n = found[nth - 1]
        have = {"enabled": n.enabled, "disabled": not n.enabled, "checked": n.checked,
                "selected": n.selected, "unchecked": not n.checked}[state]
        now = [w for w, v in (("押せる", n.enabled), ("チェックあり", n.checked), ("選択中", n.selected)) if v]
        return have, "今は " + ("・".join(now) or "押せない")
    return (not found) if neg else bool(found), f"{len(found)} 件"


STATE_CONDS = ("enabled", "disabled", "checked", "unchecked", "selected")


def c_wait(ctx, a):
    ctx.need_app()
    n, dev_t = ctx.step(f"wait {a.cond}")
    t0 = time.time()
    met, why = False, ""
    while True:
        try:
            met, why = _check(ctx, a.cond)
        except ui.IdleError:
            met, why = False, "画面が動き続けています"
        if met or time.time() - t0 > a.timeout:
            break
        time.sleep(0.5)
    cmd = f"wait {ui.q(a.cond)}" + (f" --timeout {a.timeout:g}" if a.timeout != 10 else "")
    if met:
        sc = ctx.dstate.get("screen")
        res = ok(f"{cmd}（{time.time() - t0:.1f}s）", [f"→ {sc.get('activity')} #{sc.get('hash')}"] if sc else [])
    else:
        res = ng(f"{cmd}: {a.timeout:g} 秒待っても満たされません（{why}）")
    ctx.journal(n, cmd, res, dev_t)
    return res


def c_expect(ctx, a):
    ctx.need_app()
    n, dev_t = ctx.step(f"expect {a.cond}")
    try:
        met, why = _check(ctx, a.cond)
    except ui.IdleError:
        met, why = False, "画面が動き続けていて UI ツリーが取れません"
    cmd = f"expect {ui.q(a.cond)}"
    res = ok(cmd) if met else ng(f"{cmd}: 満たされません（{why}）")
    ctx.journal(n, cmd, res, dev_t)
    return res


def _since(ctx, spec):
    dev = ctx.dev
    if spec in (None, "launch"):
        t = ctx.dstate.get("launch_t")
        return (t, "起動後") if t else (dev.epoch() - 120, "直近 120 秒（起動の記録なし）")
    if spec == "all":
        return None, "全部"
    m = re.fullmatch(r"(\d+)([sm])", spec)
    if m:
        sec = int(m.group(1)) * (60 if m.group(2) == "m" else 1)
        return dev.epoch() - sec, f"直近 {spec}"
    m = re.fullmatch(r"step(\d+)", spec.replace(" ", ""))
    if m:
        want = int(m.group(1))
        for e in reversed(ctx.state.read_journal()):
            if e.get("n") == want and e.get("dev_t"):
                return e["dev_t"] - 0.05, f"step {want} から"
        raise Fail(f"step {want} の時刻が記録にありません")
    raise Fail("--since は launch / step12 / 60s / 5m / all のどれか")


def c_log(ctx, a):
    app = ctx.need_app()
    since, what = _since(ctx, a.since)
    mine, crashes, anr, markers, dropped = logs.collect(ctx.dev, app.package, since,
                                                        () if a.raw else config.log_ignore())
    lines, counts = logs.report(mine, crashes, anr, markers, app.code, level=a.level, grep=a.grep)
    if a.full:
        path = ctx.state.run_dir("log") / "log.txt"
        path.write_text("\n".join(f"{logs._clock(l.t)} {l.level} {l.tag}: {l.msg}" for l in mine))
        lines.append(f"全文: {rel(path)}")
    parts = []
    if counts["W"] and a.level == "E":
        parts.append(f"警告 {counts['W']} 行は --level W")
    if dropped:
        parts.append(f"端末のノイズ {dropped} 行は --raw")
    note = f"（{what}。" + "、".join(parts) + " で表示）" if parts else f"（{what}）"
    if counts["crash"] or counts["anr"] or counts["E"]:
        return Result(f"crash {counts['crash']}、ANR {counts['anr']}、エラー {counts['E']}{note}", lines, NG)
    return ok(f"エラーなし{note}", lines)


def _noise_note(dropped, before="", after=""):
    return f"{before}端末のノイズ {dropped} 行を除外{after}" if dropped else ""


def c_journal(ctx, a):
    entries = [e for e in ctx.state.read_journal() if e.get("n")]
    if not entries:
        return ok("記録はまだありません")
    lines = []
    for e in entries[-a.last:]:
        flag = "" if e.get("status") == "OK" else f" [{e.get('status')}]"
        src = " (flow)" if e.get("flow") else ""
        lines.append(f"{e['n']:>4} {e['cmd']}{flag}{src}")
    return ok(f"最近の {len(lines)} 件（dp flow save 名前 範囲 で手順にできます）", lines)


def c_mark(ctx, a):
    n, dev_t = ctx.step(f"mark {a.text}")
    res = ok(f"mark {n}: {a.text}")
    ctx.journal(n, f"mark {ui.q(a.text)}", res, dev_t)
    return res


def c_flow(ctx, a):
    app = ctx.need_app()
    if a.action == "list":
        items = flowmod.listing(app)
        if not items:
            return ok("手順はまだありません（dp flow save 名前 範囲）")
        return ok(f"{len(items)} 本（{rel(flowmod.flow_dir(app))}）", [f"{n}  {k} 手順  {c}" for n, k, c in items])
    if a.action == "show":
        p = flowmod.path_for(app, _one(a.names))
        return ok(rel(p), p.read_text().splitlines())
    if a.action == "save":
        name = _one(a.names)
        if not a.range:
            raise Fail("範囲が要ります（例: dp flow save 名前 31-36,38）。dp journal で番号を見られます")
        wanted = flowmod.parse_range(a.range, 0)
        by_n = {e["n"]: e for e in ctx.state.read_journal() if e.get("n")}
        steps, skipped = [], []
        for k in wanted:
            e = by_n.get(k)
            if not e:
                skipped.append(f"{k}（記録なし）")
            elif e.get("status") != "OK":
                skipped.append(f"{k}（{e.get('status')}: {e['cmd']}）")
            elif e["cmd"].split()[0] not in flowmod.STEP_COMMANDS:
                skipped.append(f"{k}（{e['cmd'].split()[0]} は手順に入れません）")
            else:
                steps.append(e)
        if not steps:
            return ng("入れられる手順がありません", skipped)
        path = flowmod.save(app, name, steps, force=a.force, note=a.note)
        lines = [s["cmd"] for s in steps]
        if skipped:
            lines.append("除いたもの: " + "、".join(skipped))
        return ok(f"{rel(path)} に {len(steps)} 手順を保存", lines)
    if a.action == "run":
        names = [p for p, _, _ in flowmod.listing(app)] if a.all else a.names
        if not names:
            raise Fail("手順の名前か --all が要ります")
        results = [_run_flow(ctx, app, name, a) for name in names]
        if len(results) == 1:
            return results[0]
        lines = [r.first_line().replace("OK flow", "✓").replace("NG flow", "✗").replace("BLOCKED", "-")
                 for r in results]
        bad = sum(1 for r in results if r.code == NG)
        return Result(f"{len(results)} 本中 {len(results) - bad} 本成功", lines, NG if bad else OK)
    raise Fail(f"flow {a.action} は分かりません")


def _one(names):
    if not names or len(names) != 1:
        raise Fail("手順の名前を 1 つ書いてください")
    return names[0]


def _run_flow(ctx, app, name, a):
    path = flowmod.path_for(app, name)
    if not path.exists():
        return ng(f"flow {name}: ありません")
    steps = flowmod.load(path)
    run_dir = None
    t0 = time.time()
    done = 0
    sub_parser = build_parser()
    ctx.in_flow, ctx.flow_yes = True, bool(a.yes)
    try:
        for lineno, argv in steps:
            if argv[0] == "app":
                if argv[1:] and argv[1] != app.name:
                    return ng(f"flow {name}: アプリ {argv[1]} 用の手順です")
                continue
            if argv[0] not in flowmod.STEP_COMMANDS:
                return ng(f"flow {name}:{lineno} 「{argv[0]}」は手順に使えません")
            try:
                sub = sub_parser.parse_args(argv)
            except SystemExit:
                return ng(f"flow {name}:{lineno} が読めません: {' '.join(argv)}")
            for key in ("serial", "app"):
                setattr(sub, key, getattr(ctx.args, key, None))
            try:
                res = sub.func(ctx, sub)
            except Blocked as e:
                res = e.result()
            except Fail as e:
                res = e.result()
            if res.code == OK:
                done += 1
                continue
            run_dir = ctx.state.run_dir(f"flow-{name}")
            evidence = _evidence(ctx, run_dir, lineno)
            if res.code == BLOCKED:
                return Result(f"flow {name}: {lineno} 行目で止まりました（{res.rule}）",
                              [res.first_line(), *res.lines], BLOCKED, rule="flow")
            return ng(f"flow {name}  {len(steps) - 1} 手順中 {done} まで成功（{time.time() - t0:.0f}s）",
                      [f"✗ {lineno} 行目 {' '.join(argv)}", "  " + res.first_line(), *("  " + l for l in res.lines[:6]),
                       *evidence])
    finally:
        ctx.in_flow, ctx.flow_yes = False, False
    return ok(f"flow {name}  {done} 手順（{time.time() - t0:.0f}s）")


def _evidence(ctx, run_dir: Path, lineno):
    out = []
    try:
        img, _ = shot.to_shot(shot.grab(ctx.dev))
        p = run_dir / f"line{lineno}.png"
        img.save(p)
        out.append(f"  shot: {rel(p)}")
    except Fail:
        pass
    try:
        raw = ui.dump_xml(ctx.dev)
        (run_dir / f"line{lineno}.xml").write_text(raw)
        out.append(f"  tree: {rel(run_dir / f'line{lineno}.xml')}")
    except Fail:
        pass
    return out


READONLY_SHELL = ("dumpsys", "getprop", "pm list", "pm path", "pm dump", "cat /proc", "ls", "ps", "top -n",
                  "wm size", "wm density", "settings get", "cmd package list", "df", "uptime", "date")


def c_raw(ctx, a):
    args = a.args
    if not args:
        raise Fail("dp raw の後に adb の引数を書いてください（例: dp raw shell dumpsys meminfo）")
    dev = ctx.dev
    readonly = args[0] in ("logcat", "get-state", "get-serialno") or (
        args[0] in ("shell", "exec-out") and " ".join(args[1:]).strip().startswith(READONLY_SHELL))
    if not readonly:
        safety.require(dev, "raw-write")
    p = dev.adb.run(*args, timeout=120)
    out = (p.stdout + p.stderr).rstrip().splitlines()
    path = ctx.state.run_dir("raw") / "out.txt"
    path.write_text("\n".join(out))
    lines = out[:60] + ([f"(+{len(out) - 60} 行。全文: {rel(path)})"] if len(out) > 60 else [])
    return Result(f"adb {' '.join(args)[:80]}（終了コード {p.returncode}）", lines, OK if p.returncode == 0 else NG)


def c_register(ctx, a):
    if a.policy not in ("test", "safe"):
        raise Fail("--policy は test か safe")
    path = config.register_device(a.serial_to_register, a.policy, a.note or "")
    return ok(f"{a.serial_to_register} を {a.policy} として登録（{path}）")


def c_emu(ctx, a):
    if a.action == "create":
        return ok(emu.create(a.name, ram=a.ram))
    if a.action == "start":
        serial, started = emu.start(a.name, headless=a.headless)
        return ok(f"{a.name} {'を起動' if started else 'は起動済み'}: {serial}",
                  ["アニメーション 0・画面を消さない・ロックなしにしました"])
    if a.action == "stop":
        run = emu.running()
        if a.name not in run:
            return ok(f"{a.name} は起動していません")
        emu.stop(run[a.name])
        return ok(f"{a.name} を止めました")
    if a.action == "list":
        run = emu.running()
        return ok("AVD", [f"{n}  {'起動中 ' + run[n] if n in run else '停止'}" for n in emu.avds()])
    raise Fail(f"emu {a.action} は分かりません")


# --- parser -------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-s", dest="serial", default=argparse.SUPPRESS, help="端末（serial、emu、real）")
    common.add_argument("-a", dest="app", default=argparse.SUPPRESS, help="アプリ（apps.toml の名前）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON で出力")

    p = argparse.ArgumentParser(prog="dp", parents=[common],
                                description="droid-pilot: AI が Android 端末を操作するための道具")
    sp = p.add_subparsers(dest="cmd", metavar="コマンド")

    def cmd(name, func, help):
        s = sp.add_parser(name, parents=[common], help=help)
        s.set_defaults(func=func)
        return s

    cmd("devices", c_devices, "つながっている端末")
    cmd("doctor", c_doctor, "点検")
    cmd("build", c_build, "ビルド（エラーだけ表示）")
    s = cmd("install", c_install, "インストール")
    s.add_argument("apk", nargs="?")
    s.add_argument("--reinstall", action="store_true", help="入れ直す（データが消える）")
    s = cmd("launch", c_launch, "起動")
    s.add_argument("target", nargs="?", help="Activity か deeplink")
    cmd("stop", c_stop, "停止")
    cmd("reset", c_reset, "データを消す（safe 以外）")
    s = cmd("up", c_up, "ビルド → インストール → 起動 → 画面 → ログ")
    s.add_argument("--no-build", action="store_true")
    s.add_argument("--no-install", action="store_true")
    s.add_argument("--shot", action="store_true")
    s = cmd("screen", c_screen, "番号付きの UI ツリー")
    s.add_argument("--shot", action="store_true")
    s.add_argument("--mark", action="store_true", help="スクショに番号を描く")
    s.add_argument("--grid", action="store_true", help="スクショに目盛りを描く")
    s.add_argument("--all", action="store_true")
    s.add_argument("--bounds", action="store_true")
    s.add_argument("--find")
    s = cmd("shot", c_shot, "スクショ")
    s.add_argument("--region", help="x,y,w,h（shot 座標）")
    s.add_argument("--mark", action="store_true")
    s.add_argument("--grid", action="store_true")
    s = cmd("tap", c_tap, "タップ")
    s.add_argument("target")
    s.add_argument("--yes", action="store_true", help="外に出る操作を人の確認つきで通す")
    s = cmd("long", c_long, "長押し")
    s.add_argument("target")
    s.add_argument("--ms", type=int, default=800)
    s.add_argument("--yes", action="store_true")
    s = cmd("type", c_type, "文字入力（日本語も可。safe 端末は ASCII だけ）")
    s.add_argument("text")
    s.add_argument("--into")
    s.add_argument("--clear", action="store_true", help="先に欄を空にする")
    s.add_argument("--enter", action="store_true", help="入力後に決定（検索・送信キー）")
    s.add_argument("--yes", action="store_true")
    s = cmd("key", c_key, "キー")
    s.add_argument("keys", nargs="+")
    for name, func in (("swipe", c_swipe), ("scroll", c_scroll)):
        s = cmd(name, func, "指の向きにスワイプ" if name == "swipe" else "中身を送る（--to で目的まで）")
        s.add_argument("direction", nargs="?" if name == "scroll" else None,
                       choices=["up", "down", "left", "right"])
        s.add_argument("--on")
        s.add_argument("--dist", type=float, default=0.6)
        if name == "scroll":
            s.add_argument("--to")
            s.add_argument("--max", type=int, default=10)
    s = cmd("draw", c_draw, "線を引く（形か点列）")
    s.add_argument("shape", help="line zigzag wave circle check cross か \"x,y x,y …\"")
    s.add_argument("inside", nargs="?", help="in:対象")
    s.add_argument("--box", help="形を描く範囲 x,y,w,h（shot 座標。in: より優先）")
    s.add_argument("--stylus", action="store_true", help="ペンとして描く")
    s.add_argument("--pressure", type=float, help="筆圧 0〜1（ペンは既定 0.6）")
    s = cmd("zoom", c_zoom, "2 本指で拡大（in）・縮小（out）")
    s.add_argument("direction", choices=["in", "out"])
    s.add_argument("--on")
    s.add_argument("--factor", type=float, default=2.0)
    s = cmd("wait", c_wait, "条件まで待つ")
    s.add_argument("cond")
    s.add_argument("--timeout", type=float, default=10)
    s = cmd("expect", c_expect, "確かめる")
    s.add_argument("cond")
    s = cmd("log", c_log, "自アプリのログ")
    s.add_argument("--since")
    s.add_argument("--level", default="E", choices=list("EWID"))
    s.add_argument("--grep")
    s.add_argument("--full", action="store_true")
    s.add_argument("--raw", action="store_true", help="端末のノイズ（config/logs.toml）も出す")
    s = cmd("journal", c_journal, "最近の操作")
    s.add_argument("--last", type=int, default=15)
    s = cmd("mark", c_mark, "目印")
    s.add_argument("text")
    s = cmd("flow", c_flow, "手順の保存・再生")
    s.add_argument("action", choices=["save", "run", "list", "show"])
    s.add_argument("names", nargs="*")
    s.add_argument("--range", "-r", dest="range")
    s.add_argument("--all", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--note")
    s.add_argument("--yes", action="store_true")
    cmd("setup", c_setup, "dp の補助（線を引く補助・キーボード）を用意する。人が端末の前で")
    s = cmd("raw", c_raw, "adb をそのまま（safe 端末は読み取りだけ）")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s = cmd("register", c_register, "実機をテスト用として登録（人がする）")
    s.add_argument("serial_to_register")
    s.add_argument("--policy", default="test")
    s.add_argument("--note")
    s = cmd("emu", c_emu, "エミュレータ（今は使っていない）")
    s.add_argument("action", choices=["create", "start", "stop", "list"])
    s.add_argument("--name", default=emu.AVD)
    s.add_argument("--headless", action="store_true")
    s.add_argument("--ram", type=int, default=emu.RAM_MB)
    return p


def _fix_flow_save_args(argv):
    """`dp flow save NAME 31-36` reads more naturally than --range."""
    if len(argv) >= 4 and argv[0] == "flow" and argv[1] == "save" and re.fullmatch(r"[\d,\-]+", argv[3]):
        return argv[:3] + ["--range", argv[3]] + argv[4:]
    return argv


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Allow "dp -s emu tap 4" and "dp tap 4 -s emu" alike; keep flow save's positional range.
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] not in ("-h", "--help"):
        i += 2 if argv[i] in ("-s", "-a") else 1
    argv = argv[:i] + _fix_flow_save_args(argv[i:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    as_json = getattr(args, "json", False)
    for key in ("serial", "app"):
        if not hasattr(args, key):
            setattr(args, key, None)
    try:
        ctx = Ctx(args)
        try:
            res = args.func(ctx, args)
        finally:
            ctx.state.save()
    except Fail as e:
        res = e.result()
    except KeyboardInterrupt:
        res = ng("中断しました")
    print(res.render(as_json))
    return res.code
