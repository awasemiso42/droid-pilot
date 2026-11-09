"""Offline tests: run with `python3 -m unittest discover -s tests` from the repository."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import re  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

from dp import actions, build, inject, logs, safety, shot, ui, flow  # noqa: E402
from dp.out import Blocked  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def screen_from(xml_name, package="com.example.notes", activity="LibraryActivity"):
    roots, rot = ui.parse((FIX / xml_name).read_text())
    w, h = ui.screen_size(roots)
    elems = ui.compact(roots, w, h)
    return ui.Screen(roots, elems, w, h, rot, package, activity, ui.fingerprint(elems), 0.0)


class FakeDev:
    def __init__(self, kind):
        self.kind = kind

    def locked(self):
        return ""


class UiTest(unittest.TestCase):
    def setUp(self):
        self.sc = screen_from("library.xml")

    def test_compact_keeps_buttons_and_merges_row_texts(self):
        labels = [(e.kind, e.label) for e in self.sc.elems]
        self.assertIn(("btn", "検索"), labels)
        self.assertIn(("btn", "新しいノート"), labels)
        self.assertIn(("btn", "化学 3ページ"), labels)
        # Row texts are folded into the row, not listed again.
        self.assertNotIn(("text", "化学"), labels)
        self.assertEqual(self.sc.width, 2560)
        self.assertEqual(self.sc.rotation, 1)

    def test_canvas_and_list(self):
        kinds = {e.kind for e in self.sc.elems}
        self.assertIn("canvas", kinds)
        lst = [e for e in self.sc.elems if e.kind == "list"][0]
        self.assertEqual(lst.items, 12)

    def test_render_is_short_and_in_shot_space(self):
        lines = ui.render(self.sc.elems, self.sc.scale)
        text = "\n".join(lines)
        self.assertIn("(+4件。--all で全部)", text)
        self.assertLessEqual(len(lines), ui.MAX_LINES + 1)
        # 2560 wide -> 1024 shot space: the search button centre 2340 -> 936
        self.assertIn('btn "検索" @936,32', text)
        # Toolbar items share a line.
        self.assertTrue(any('"検索"' in l and '"設定"' in l for l in lines))

    def test_render_all(self):
        text = "\n".join(ui.render(self.sc.elems, self.sc.scale, all_=True))
        self.assertIn("音楽", text)

    def test_find_and_selectors(self):
        nodes = ui.find(self.sc, "text", "新しいノート")
        self.assertEqual(len(nodes), 1)
        e = ui.elem_for(self.sc, nodes[0])
        self.assertEqual(ui.stable_selector(self.sc, e), "text:新しいノート")
        self.assertEqual(len(ui.find(self.sc, "text~", "ノート")), 2)
        self.assertEqual(len(ui.find(self.sc, "desc", "検索")), 1)
        self.assertEqual(len(ui.find(self.sc, "id", "settings")), 1)
        row = ui.find(self.sc, "text", "化学")[0]
        self.assertEqual(ui.elem_for(self.sc, row).label, "化学 3ページ")

    def test_parse_target(self):
        self.assertEqual(ui.parse_target("4"), ("index", 4))
        self.assertEqual(ui.parse_target("@50%,40%"), ("point", 50.0, True, 40.0, True))
        self.assertEqual(ui.parse_target("text:'a b'#2"), ("text", "a b", 2))
        with self.assertRaises(Exception):
            ui.parse_target("なにか")

    def test_fingerprint_changes_with_state(self):
        before = self.sc.hash
        self.sc.elems[0].flags.append("sel")
        self.assertNotEqual(before, ui.fingerprint(self.sc.elems))

    def test_bad_xml_chars(self):
        raw = (FIX / "library.xml").read_text().replace("ライブラリ", "ラ&#1;イブ\x07ラリ")
        roots, _ = ui.parse(raw)
        self.assertTrue(any(n.text == "ライブラリ" for r in roots for n in r.walk()))


class SummaryTest(unittest.TestCase):
    def test_change_summary(self):
        sc = screen_from("library.xml")
        before = sc.to_state()
        self.assertEqual(actions.summarize(before, sc, 0.5), ["→ 変化なし（0.5s）"])
        sc2 = screen_from("library.xml")
        sc2.elems[1].flags.append("disabled")
        sc2.hash = ui.fingerprint(sc2.elems)
        lines = actions.summarize(before, sc2, 0.5)
        self.assertIn("変化 2 か所", lines[0])
        sc3 = screen_from("library.xml", activity="EditorActivity")
        self.assertIn("LibraryActivity → EditorActivity", actions.summarize(before, sc3, 1.0)[0])


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.sc = screen_from("library.xml")
        from dp import config
        self.app = config.App(name="notes", package="com.example.notes", repo=Path("."), build="",
                              apk=[], code=["com.example.notes"], outward=[], launch="")

    def node(self, sel):
        kind, value, nth = ui.parse_target(sel)
        return ui.find(self.sc, kind, value)[0]

    def test_outward_needs_yes_on_every_device(self):
        for kind in ("emu", "test", "safe"):
            with self.assertRaises(Blocked) as cm:
                safety.check_press(FakeDev(kind), self.app, self.sc, self.node("desc:共有"))
            self.assertEqual(cm.exception.rule, "needs-human")
            note = safety.check_press(FakeDev(kind), self.app, self.sc, self.node("desc:共有"), yes=True)
            self.assertIn("--yes", note)

    def test_plain_button_passes(self):
        self.assertEqual(safety.check_press(FakeDev("safe"), self.app, self.sc, self.node("text:新しいノート")), "")

    def test_foreground_outside_app(self):
        sc = screen_from("library.xml", package="com.android.vending")
        with self.assertRaises(Blocked) as cm:
            safety.check_press(FakeDev("emu"), self.app, sc, None)
        self.assertEqual(cm.exception.rule, "foreground")

    def test_ops_by_kind(self):
        safety.require(FakeDev("emu"), "clear")
        safety.require(FakeDev("test"), "reinstall")
        with self.assertRaises(Blocked):
            safety.require(FakeDev("safe"), "clear")
        with self.assertRaises(Blocked):
            safety.require(FakeDev("safe"), "install")


class LogsTest(unittest.TestCase):
    def test_crash_and_errors(self):
        lines = logs.parse((FIX / "crash.logcat").read_text())
        self.assertEqual(len(lines), 16)
        names = logs.uid_names(10213)
        self.assertIn("u0_a213", names)
        mine = [l for l in lines if l.uid in names]
        crashes = logs.crash_blocks([l for l in mine if l.tag == "AndroidRuntime"],
                                    "com.example.notes", names)
        crash_keys = {(c.t, c.msg) for b in crashes for c in b}
        rest = [l for l in mine if (l.t, l.msg) not in crash_keys]
        out, counts = logs.report(rest, crashes, [], [], ["com.example.notes"])
        text = "\n".join(out)
        self.assertIn("CRASH", out[0])
        self.assertIn("IllegalStateException: page index -1", out[0])
        self.assertIn("at com.example.notes.ui.PageView.scrollTo(PageView.java:812)", text)
        self.assertIn("Caused by: java.lang.ArrayIndexOutOfBoundsException", text)
        self.assertIn("at com.example.notes.model.Book.page(Book.java:90)", text)
        self.assertNotIn("Instrumentation", text)
        self.assertIn("E x2 CoEdit", text)
        self.assertEqual(counts["E"], 2)
        self.assertEqual(counts["W"], 1)
        self.assertNotIn("NoteSync", text)
        out_w, _ = logs.report(rest, crashes, [], [], ["com.example.notes"], level="W")
        self.assertIn("NoteSync", "\n".join(out_w))


    def test_noise_filter(self):
        lines = logs.parse((FIX / "crash.logcat").read_text())
        kept, dropped = logs.noise(lines, [re.compile("^CoEdit: ")])
        self.assertEqual(dropped, 2)
        self.assertFalse(any(l.tag == "CoEdit" for l in kept))
        self.assertEqual(logs.noise(lines, []), (lines, 0))


class InjectTest(unittest.TestCase):
    def test_stroke_keeps_corners_and_pace(self):
        lines = inject.stroke_lines([[(0, 0), (100, 0), (100, 50)]], stylus=True)
        self.assertEqual(lines[:3], ["src stylus", "dt 8", "down 0.0 0.0 0.60"])
        self.assertIn("move 100.0 0.0", lines)  # the corner itself is sent
        moves = [l for l in lines if l.startswith("move")]
        self.assertGreaterEqual(len(moves), 150 // 8)
        self.assertEqual(lines[-2], "up 100.0 50.0")

    def test_long_strokes_are_capped(self):
        lines = inject.stroke_lines([[(0, 0), (20000, 0)]])
        self.assertLessEqual(len([l for l in lines if l.startswith("move")]), 300)
        self.assertEqual(lines[2], "down 0.0 0.0 1.00")

    def test_pinch(self):
        lines = inject.pinch_lines(500, 500, 100, 400, steps=4)
        self.assertEqual(lines[2], "down2 450.0 500.0 550.0 500.0")
        self.assertEqual(lines[-1], "up2 300.0 500.0 700.0 500.0")
        v = inject.pinch_lines(500, 500, 100, 400, steps=4, vertical=True)
        self.assertEqual(v[2], "down2 500.0 450.0 500.0 550.0")

    def test_wave_stays_flat_in_a_tall_box(self):
        # A tall wave reads as a scratch-out scribble in note apps.
        (pts,) = actions.preset("wave", (0, 0, 1000, 3000))
        ys = [y for _, y in pts]
        self.assertLess(max(ys) - min(ys), (1000 * 0.6) / 4)

    def test_region_change_sees_thin_lines(self):
        a = Image.new("RGB", (1800, 2880), "white")
        b = a.copy()
        ImageDraw.Draw(b).line([(400, 900), (1400, 1500)], fill=(150, 170, 40), width=3)
        pct = shot.region_change(a, b, (300, 800, 1500, 1600))
        self.assertGreater(pct, 0.1)
        self.assertEqual(shot.region_change(a, a.copy(), (300, 800, 1500, 1600)), 0)


class BuildTest(unittest.TestCase):
    def test_errors(self):
        errs = build.parse_errors((FIX / "kotlin-error.txt").read_text(), Path("/home/u/apps/cards"))
        self.assertEqual(errs[0], "app/src/main/kotlin/com/example/cards/Deck.kt:12:5: Unresolved reference 'foo'.")
        self.assertEqual(len([e for e in errs if "Deck.kt" in e]), 1)
        self.assertTrue(any(e.startswith("app/src/main/java/com/example/cards/A.java:40: cannot find symbol") for e in errs))
        self.assertTrue(any(e.startswith("Gradle: Execution failed for task ':app:compileDebugKotlin'.") for e in errs))
        self.assertFalse(any("Old.kt" in e for e in errs))


class FlowTest(unittest.TestCase):
    def test_range(self):
        self.assertEqual(flow.parse_range("31-33,38", 0), [31, 32, 33, 38])

    def test_presets_inside_box(self):
        for name in actions.PRESETS:
            for stroke in actions.preset(name, (100, 100, 300, 200)):
                for x, y in stroke:
                    self.assertTrue(100 <= x <= 300 and 100 <= y <= 200, (name, x, y))


class HookTest(unittest.TestCase):
    def run_hook(self, command):
        p = subprocess.run([sys.executable, str(ROOT / "claude" / "hooks" / "guard.py")],
                           input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
                           capture_output=True, text=True)
        if not p.stdout.strip():
            return None
        return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]

    def test_decisions(self):
        self.assertEqual(self.run_hook("adb shell pm clear x"), "deny")
        self.assertEqual(self.run_hook("timeout 9 ~/Android/Sdk/platform-tools/adb devices"), "deny")
        self.assertEqual(self.run_hook("cd app && ./gradlew :app:installDebug"), "deny")
        self.assertEqual(self.run_hook("bash -c 'adb install x.apk'"), "deny")
        self.assertEqual(self.run_hook("echo $(adb devices)"), "deny")
        self.assertIsNone(self.run_hook("grep -n adb notes.txt"))
        self.assertIsNone(self.run_hook("./gradlew :app:assembleDebug"))
        self.assertIsNone(self.run_hook("dp tap 4"))
        self.assertEqual(self.run_hook("dp tap 9 --yes"), "ask")
        self.assertEqual(self.run_hook("dp -s emu raw shell ls"), "ask")
        self.assertEqual(self.run_hook("dp raw shell ls"), "ask")
        self.assertEqual(self.run_hook("dp register R5C --policy test"), "ask")


if __name__ == "__main__":
    unittest.main()
