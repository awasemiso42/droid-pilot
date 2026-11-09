"""Safety rules. Device kinds: emu (emulator), test (registered real device), safe (any other).

Inside the device anything goes on emu / test. Actions that leave the device (money, messages,
publishing) always need a person: dp refuses them unless --yes is given, and the Claude Code
hook turns --yes into a confirmation prompt.
"""
import re

from . import config, ui
from .out import Blocked

# op -> device kinds that may do it
OPS = {
    "install": {"emu", "test"},
    "reinstall": {"emu", "test"},
    "clear": {"emu", "test"},
    "settings": {"emu", "test"},
    "raw-write": {"emu", "test"},
}

OP_TEXT = {
    "install": "インストール（safe 端末では自動バックアップができるまで止めています）",
    "reinstall": "入れ直し・ダウングレード（データが消える）",
    "clear": "データの消去",
    "settings": "端末設定の変更",
    "raw-write": "読み取り以外の adb コマンド",
}

REGISTER_HINT = "テスト用の端末なら、人が dp register <serial> --policy test で登録してください"


def require(dev, op):
    if dev.kind not in OPS[op]:
        raise Blocked("safe-device", f"登録していない実機（safe）では{OP_TEXT[op]}はできません",
                      [REGISTER_HINT])


def allowed_packages(dev, app):
    pol = config.policy()
    allowed = set(pol["allowed_packages"]) | {a.package for a in config.apps().values()}
    if app:
        allowed.add(app.package)
    if dev.kind != "safe":
        allowed |= pol["test_only_packages"]
    return allowed


def check_foreground(dev, app, screen):
    pkg = screen.package
    if pkg and pkg not in allowed_packages(dev, app):
        raise Blocked("foreground", f"前面が {pkg} です。対象アプリ以外は操作しません",
                      ["dp key back で戻るか、dp launch で対象アプリを前に出してください"])
    if dev.kind == "safe" and pkg in config.policy()["permission_packages"]:
        raise Blocked("permission", "safe 端末の権限ダイアログは押しません。人に聞いてください")


def check_unlocked(dev):
    why = dev.locked()
    if why:
        raise Blocked("locked", f"{why}です。人が解除してください（dp は PIN やパスワードを入れません）")


def press_labels(screen, node):
    """Everything a person would read on the thing being pressed, one string per source."""
    if node is None:
        return []
    e = ui.elem_for(screen, node)
    parts = [node.text, node.desc]
    if e is not None:
        parts += [e.label, e.node.text, e.node.desc, ui._inner_label(e.node)]
    out = []
    for p in parts:
        p = " ".join((p or "").split())
        if p and p not in out:
            out.append(p)
    return out


def _match(patterns, labels):
    for pat in patterns:
        for lab in labels:
            m = pat.search(lab)
            if m:
                return m, lab
    return None, ""


def is_input(screen, node):
    e = ui.elem_for(screen, node) if node is not None else None
    return e is not None and e.kind == "input"


def check_press(dev, app, screen, node, yes=False):
    """Before tapping `node`. Returns a note for the journal when --yes let an outward press through."""
    check_foreground(dev, app, screen)
    if node is None or is_input(screen, node):
        return ""
    labels = press_labels(screen, node)
    pol = config.policy()
    outward = pol["outward"] + [re.compile(x, re.I) for x in (app.outward if app else [])]
    m, label = _match(outward, labels)
    if m:
        if not yes:
            raise Blocked("needs-human", f"「{ui._clip(label)}」は外に出る操作です（{m.group(0)}）",
                          ["人の確認が要ります。同じコマンドに --yes を付けると Claude Code の確認が出ます"])
        return f"outward:{m.group(0)} (--yes)"
    if dev.kind == "safe":
        m, label = _match(pol["destructive"], labels)
        if m:
            raise Blocked("safe-device", f"safe 端末では「{ui._clip(label)}」（{m.group(0)}）は押しません",
                          [REGISTER_HINT])
    return ""


def check_type(screen, node, yes=False):
    """Before typing into `node` (or the focused field). Passwords and PINs go through a person."""
    target = node
    if target is None:
        target = next((n for n in screen.all_nodes() if n.focused and n.password), None)
    if target is not None and target.password:
        if not yes:
            raise Blocked("needs-human", "パスワード欄には人の確認なしで入力しません",
                          ["テスト用アカウントの値なら、同じコマンドに --yes を付けると Claude Code の確認が出ます"])
        return "password (--yes)"
    return ""
