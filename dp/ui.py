"""UI tree: dump it with uiautomator, squeeze it into a short numbered list, and find targets.

Coordinates shown to the AI are in "shot space": the screen scaled so its long edge is
SHOT_EDGE pixels, the same size as the default screenshot. dp converts them back.
"""
import hashlib
import os
import re
import shlex
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .out import Fail

SHOT_EDGE = int(os.environ.get("DP_SHOT_EDGE", "1024"))
LABEL_MAX = 32
LIST_ITEMS = 8
MAX_LINES = 40


# --- parsing ------------------------------------------------------------

@dataclass(eq=False)
class Node:
    cls: str
    text: str
    rid: str
    desc: str
    pkg: str
    bounds: tuple
    clickable: bool = False
    long_clickable: bool = False
    checkable: bool = False
    checked: bool = False
    scrollable: bool = False
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    password: bool = False
    hint: str = ""
    children: list = field(default_factory=list)
    parent: "Node" = None

    @property
    def short_cls(self):
        return self.cls.rsplit(".", 1)[-1]

    @property
    def rid_short(self):
        return self.rid.split("/", 1)[-1] if self.rid else ""

    @property
    def area(self):
        x1, y1, x2, y2 = self.bounds
        return max(0, x2 - x1) * max(0, y2 - y1)

    @property
    def center(self):
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def contains(self, x, y):
        x1, y1, x2, y2 = self.bounds
        return x1 <= x < x2 and y1 <= y < y2

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_CHARREF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean_xml(raw: str) -> str:
    i = raw.find("<hierarchy")
    j = raw.rfind("</hierarchy>")
    if i < 0 or j < 0:
        raise Fail("UI ツリーを読めません", [raw.strip()[:200]])
    xml = raw[i:j + len("</hierarchy>")]

    def fix(m):
        v = m.group(1)
        cp = int(v[1:], 16) if v[0] in "xX" else int(v)
        return "" if cp < 0x20 and cp not in (9, 10, 13) else m.group(0)

    return _CTRL.sub("", _CHARREF.sub(fix, xml))


def _b(a, key):
    return a.get(key) == "true"


def parse(raw: str):
    """Returns (roots, rotation)."""
    root = ET.fromstring(_clean_xml(raw))
    rotation = int(root.get("rotation", "0") or 0)

    def build(el, parent):
        a = el.attrib
        m = _BOUNDS.search(a.get("bounds", ""))
        bounds = tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)
        n = Node(cls=a.get("class", ""), text=a.get("text", ""), rid=a.get("resource-id", ""),
                 desc=a.get("content-desc", ""), pkg=a.get("package", ""), bounds=bounds,
                 clickable=_b(a, "clickable"), long_clickable=_b(a, "long-clickable"),
                 checkable=_b(a, "checkable"), checked=_b(a, "checked"), scrollable=_b(a, "scrollable"),
                 enabled=a.get("enabled", "true") == "true", focused=_b(a, "focused"),
                 selected=_b(a, "selected"), password=_b(a, "password"), hint=a.get("hint", ""),
                 parent=parent)
        n.children = [build(c, n) for c in el if c.tag == "node"]
        return n

    return [build(c, None) for c in root if c.tag == "node"], rotation


# --- compacting -----------------------------------------------------------

@dataclass(eq=False)
class Elem:
    kind: str
    label: str
    node: Node
    flags: list
    depth: int = 0
    items: int = 0
    i: int = 0

    @property
    def bounds(self):
        return self.node.bounds

    @property
    def center(self):
        return self.node.center

    def sig(self):
        return (self.kind, self.label, self.node.rid)

    def to_dict(self):
        return {"i": self.i, "kind": self.kind, "label": self.label, "rid": self.node.rid,
                "flags": self.flags, "bounds": list(self.bounds)}


def _actionable(n: Node):
    return n.clickable or n.long_clickable or n.checkable or n.scrollable or _is_input(n)


def _is_input(n: Node):
    return "EditText" in n.cls or "TextField" in n.cls


def _kind(n: Node):
    c = n.short_cls
    if n.scrollable:
        return "list"
    if _is_input(n):
        return "input"
    if "Switch" in c:
        return "switch"
    if "RadioButton" in c:
        return "radio"
    if "CheckBox" in c or n.checkable:
        return "check"
    if n.clickable or n.long_clickable or "Button" in c:
        return "btn"
    if "Image" in c:
        return "img"
    if "WebView" in c:
        return "web"
    return "text"


def _flags(n: Node):
    f = []
    if not n.enabled:
        f.append("disabled")
    if n.checkable:
        f.append("on" if n.checked else "off")
    if n.selected:
        f.append("sel")
    if n.focused and _is_input(n):
        f.append("focus")
    if n.password:
        f.append("pw")
    if n.long_clickable and not n.clickable:
        f.append("long")
    return f


def _clip(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= LABEL_MAX else s[:LABEL_MAX - 1] + "…"


def _own(n: Node) -> str:
    return (n.text or n.desc or "").strip()


def _inner_label(n: Node) -> str:
    """Texts under an actionable node, skipping nested actionable parts when possible."""
    def collect(node, skip_actionable):
        out = []
        for c in node.children:
            if skip_actionable and _actionable(c):
                continue
            t = _own(c)
            if t:
                out.append(t)
            out.extend(collect(c, skip_actionable))
        return out

    parts = collect(n, True) or collect(n, False)
    seen, uniq = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return " ".join(uniq[:3])


def compact(roots, width, height):
    """Every element worth showing, in reading order, numbered from 1."""
    screen_area = max(1, width * height)
    elems = []

    def visit(n, consumed, depth):
        if n.area == 0:
            return
        if _actionable(n):
            label = _own(n) or _inner_label(n) or n.hint or n.rid_short
            e = Elem(_kind(n), _clip(label), n, _flags(n), depth)
            elems.append(e)
            if n.scrollable:
                before = len(elems)
                for c in n.children:
                    visit(c, False, depth + 1)
                e.items = sum(1 for x in elems[before:] if x.depth == depth + 1)
            else:
                for c in n.children:
                    visit(c, True, depth)
            return
        label = _own(n)
        if label and not consumed:
            kind = "img" if "Image" in n.short_cls else "text"
            elems.append(Elem(kind, _clip(label), n, _flags(n), depth))
        elif not label and not n.children and not consumed and n.area >= 0.08 * screen_area:
            elems.append(Elem("canvas", n.rid_short or n.short_cls, n, _flags(n), depth))
        for c in n.children:
            visit(c, consumed, depth)

    for r in roots:
        visit(r, False, 0)
    for i, e in enumerate(elems, 1):
        e.i = i
    return elems


def screen_size(roots):
    w = max((r.bounds[2] for r in roots), default=0)
    h = max((r.bounds[3] for r in roots), default=0)
    return w, h


def scale_for(width, height):
    return min(1.0, SHOT_EDGE / max(1, width, height))


def fingerprint(elems):
    h = hashlib.sha1()
    for e in elems:
        cx, cy = e.center
        h.update(f"{e.kind}|{e.label}|{','.join(e.flags)}|{cx // 40}|{cy // 40}\n".encode())
    return h.hexdigest()[:4]


# --- rendering ------------------------------------------------------------

def _fmt(e: Elem, s: float, bounds=False):
    def pt(x, y):
        return f"{round(x * s)},{round(y * s)}"

    parts = [f"[{e.i}]"]
    if e.kind != "text":
        parts.append(e.kind if e.kind != "list" else f"list {e.items}件")
    if e.label and e.kind != "canvas":
        parts.append(f'"{e.label}"')
    elif e.kind == "canvas":
        parts.append(e.label)
    parts += e.flags
    x1, y1, x2, y2 = e.bounds
    if bounds or e.kind in ("canvas", "list", "web"):
        parts.append(f"@{pt(x1, y1)}-{pt(x2, y2)}")
    else:
        parts.append("@" + pt(*e.center))
    return " ".join(parts)


def render(elems, s, all_=False, bounds=False, max_lines=MAX_LINES):
    """Lines of the numbered list. Elements on the same row share a line."""
    tol = max(12, int(0.015 * max((e.bounds[3] for e in elems), default=1000)))
    lines, row, row_cy, row_depth = [], [], None, None
    hidden_list = 0
    list_stack = []  # (depth, shown_items)

    def flush():
        nonlocal row
        if row:
            lines.append("  " * row_depth + "  ".join(row))
            row = []

    for e in elems:
        while list_stack and e.depth <= list_stack[-1][0]:
            d, shown, extra = list_stack.pop()
            if extra:
                flush()
                lines.append("  " * (d + 1) + f"(+{extra}件。--all で全部)")
        if list_stack and not all_:
            top = list_stack[-1]
            if e.depth == top[0] + 1:
                top[1] += 1
            if top[1] > LIST_ITEMS:
                if e.depth == top[0] + 1:
                    top[2] += 1
                hidden_list += 1
                continue
        cy = e.center[1]
        new_row = (not row or e.depth != row_depth or abs(cy - row_cy) > tol or len(row) >= 4
                   or e.kind in ("list", "canvas", "web"))
        if new_row:
            flush()
            row_cy, row_depth = cy, e.depth
        row.append(_fmt(e, s, bounds))
        if e.kind == "list":
            flush()
            list_stack.append([e.depth, 0, 0])
    flush()
    while list_stack:
        d, shown, extra = list_stack.pop()
        if extra:
            lines.append("  " * (d + 1) + f"(+{extra}件。--all で全部)")
    if not all_ and len(lines) > max_lines:
        cut = len(lines) - max_lines
        lines = lines[:max_lines] + [f"(+{cut} 行省略。--all で全部)"]
    return lines


# --- dumping ----------------------------------------------------------------

@dataclass
class Screen:
    roots: list
    elems: list
    width: int
    height: int
    rotation: int
    package: str
    activity: str
    hash: str
    took: float

    @property
    def scale(self):
        return scale_for(self.width, self.height)

    def header(self, app_pkg=None):
        if self.package and app_pkg and self.package != app_pkg:
            where = f"{self.package}/{self.activity}（対象アプリの外）"
        else:
            where = self.activity or self.package or "?"
        return f"{where}  {self.width}x{self.height} rot{self.rotation}  #{self.hash}"

    def all_nodes(self):
        for r in self.roots:
            yield from r.walk()

    def to_state(self):
        return {"hash": self.hash, "package": self.package, "activity": self.activity,
                "width": self.width, "height": self.height, "rotation": self.rotation,
                "elems": [e.to_dict() for e in self.elems]}


class IdleError(Fail):
    pass


def dump_xml(dev, timeout=14) -> str:
    out = dev.adb.exec_out("uiautomator dump /dev/tty", timeout=timeout).decode("utf-8", "replace")
    if "<hierarchy" in out:
        return out
    if "idle state" in out:
        raise IdleError("UI ツリーが取れません（画面が動き続けています。アニメーションや描き続けるキャンバス）",
                        ["dp shot でスクショを見てください"])
    # Some builds cannot write to /dev/tty: go through a file.
    dev.adb.sh("uiautomator dump /sdcard/dp-ui.xml >/dev/null 2>&1", timeout=timeout)
    out = dev.adb.exec_out("cat /sdcard/dp-ui.xml", timeout=10).decode("utf-8", "replace")
    if "<hierarchy" not in out:
        raise Fail("UI ツリーが取れません", [out.strip()[:200]])
    return out


def capture(dev) -> Screen:
    t0 = time.time()
    raw = dump_xml(dev)
    roots, rotation = parse(raw)
    w, h = screen_size(roots)
    elems = compact(roots, w, h)
    pkg, act = dev.foreground()
    if not pkg and roots:
        pkg = roots[0].pkg
    act_short = act.rsplit(".", 1)[-1] if act else ""
    return Screen(roots, elems, w, h, rotation, pkg, act_short, fingerprint(elems), time.time() - t0)


# --- targets ----------------------------------------------------------------

_POINT = re.compile(r"@(-?\d+(?:\.\d+)?)(%?),(-?\d+(?:\.\d+)?)(%?)")
_PREFIXES = ("text~:", "text:", "id:", "desc~:", "desc:")

TARGET_HELP = ("対象の書き方: 番号（4）、text:文字、text~:部分一致、id:名前、desc:説明、"
               "@x,y（スクショ座標）、@x%,y%（割合）。n 番目は末尾に #2")


def parse_target(t: str):
    t = t.strip()
    if re.fullmatch(r"\d+", t):
        return ("index", int(t))
    m = _POINT.fullmatch(t)
    if m:
        return ("point", float(m.group(1)), bool(m.group(2)), float(m.group(3)), bool(m.group(4)))
    for p in _PREFIXES:
        if t.startswith(p):
            v = t[len(p):]
            nth = 1
            mm = re.search(r"#(\d+)$", v)
            if mm:
                nth, v = int(mm.group(1)), v[:mm.start()]
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return (p[:-1], v, nth)
    raise Fail(f"対象「{t}」が分かりません", [TARGET_HELP])


def find(screen: Screen, kind: str, value: str):
    """Nodes matching a text/id/desc selector, in reading order (elements first)."""
    v = value.casefold()
    found = []

    def add(n):
        # A node inside an element already found is the same thing on screen.
        p = n
        while p is not None:
            if p in found:
                return
            p = p.parent
        found.append(n)

    if kind in ("text", "text~"):
        for e in screen.elems:
            lab = e.label.casefold()
            if (kind == "text" and lab == v) or (kind == "text~" and v in lab):
                add(e.node)
        for n in screen.all_nodes():
            t = n.text.strip().casefold()
            if t and ((kind == "text" and t == v) or (kind == "text~" and v in t)):
                add(n)
    elif kind == "id":
        for n in screen.all_nodes():
            if n.rid and (n.rid_short == value or n.rid == value):
                add(n)
    elif kind in ("desc", "desc~"):
        for n in screen.all_nodes():
            d = n.desc.strip().casefold()
            if d and ((kind == "desc" and d == v) or (kind == "desc~" and v in d)):
                add(n)
    return [n for n in found if n.area > 0]


def elem_for(screen: Screen, node: Node):
    """The listed element that is this node or holds it."""
    n = node
    while n is not None:
        for e in screen.elems:
            if e.node is n:
                return e
        n = n.parent
    return None


def node_at(screen: Screen, x, y):
    """The smallest node containing the point."""
    best = None
    for n in screen.all_nodes():
        if n.contains(x, y) and (best is None or n.area <= best.area):
            best = n
    return best


_SPECIAL = re.compile(r"[\s'\"\\$`|&;<>(){}\[\]*?!]")


def q(s: str) -> str:
    """Shell-quotes only when needed (shlex.quote also quotes plain Japanese)."""
    return shlex.quote(s) if not s or _SPECIAL.search(s) else s


def stable_selector(screen: Screen, e: Elem, point=None) -> str:
    """A selector that should survive layout changes, for the journal and flows."""
    if e is not None:
        if e.label and e.kind not in ("canvas", "list"):
            # Merged labels such as "ノート 無題, 11/7 9:22, 1 ページ" change with the data:
            # use their first part when that alone finds only this element.
            head = e.label.split(", ")[0].strip()
            if head != e.label and len(head) >= 2:
                if [x for x in screen.elems if head.casefold() in x.label.casefold()] == [e]:
                    return q(f"text~:{head}")
            same = [x for x in screen.elems if x.label == e.label]
            if len(same) == 1:
                return q(f"text:{e.label}")
            if len(same) > 1:
                return q(f"text:{e.label}#{same.index(e) + 1}")
        if e.node.rid:
            same = [n for n in screen.all_nodes() if n.rid == e.node.rid]
            if len(same) == 1:
                return f"id:{e.node.rid_short}"
    x, y = point or (e.center if e else (0, 0))
    return f"@{round(100 * x / screen.width, 1):g}%,{round(100 * y / screen.height, 1):g}%"
