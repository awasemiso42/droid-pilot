"""Offline tests: run with `python3 -m unittest discover -s tests` from the repository."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import re  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

from dp import actions, build, inject, logs, perf, safety, shot, trace, ui, flow  # noqa: E402
from dp.out import Blocked, Fail  # noqa: E402

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

    def test_device_noise_is_narrow(self):
        from dp import config
        pats = config.log_ignore()
        L = lambda tag, msg, lv="E": logs.Line(0.0, "", 1, 1, lv, tag, msg)  # noqa: E731
        lines = [L("BinderMonitor", "err open binder_delay errno=2"),
                 L("ImeBackDispatcher", "Ime callback not found. Ignoring unregisterReceivedCallback. "
                                        "callbackId: 232825731"),
                 L("BinderMonitor", "binder transaction to com.example.notes took 900ms"),
                 L("ImeBackDispatcher", "something the app caused")]
        kept, dropped = logs.noise(lines, pats)
        self.assertEqual(dropped, 2)
        self.assertEqual([l.msg for l in kept], [lines[2].msg, lines[3].msg])

    def test_errors_name_the_step_before(self):
        L = lambda t, tag, msg, lv="E": logs.Line(t, "", 1, 1, lv, tag, msg)  # noqa: E731
        t0 = 1_700_000_000.0
        markers = [L(t0, "DP", "step 11: launch", "I"),
                   L(t0 + 10, "DP", "step 12: tap 元に戻す", "I"),
                   L(t0 + 15, "DP", "step 13: tap やり直す", "I")]
        mine = [L(t0 + 5, "Once", "boom"),
                L(t0 + 10.2, "RippleDrawable", "anim error 1"),
                L(t0 + 10.3, "RippleDrawable", "anim error 2"),
                L(t0 + 15.1, "RippleDrawable", "anim error 3"),
                L(t0 + 16.4, "RippleDrawable", "anim error 4")]
        out, counts = logs.report(mine, [], [], markers, ["com.example.notes"])
        text = "\n".join(out)
        self.assertEqual(counts["E"], 5)
        once = out.index(next(l for l in out if "Once: boom" in l))
        self.assertEqual(out[once + 1], "  直前の操作: step 11: launch")
        self.assertIn("E x4 RippleDrawable", text)
        c = logs._clock
        self.assertIn(f"  直前の操作: step 12: tap 元に戻す（{c(t0 + 10.2)} x2）", out)
        self.assertIn(f"  直前の操作: step 13: tap やり直す（{c(t0 + 15.1)}、{c(t0 + 16.4)}）", out)
        # No markers (or none before the line): nothing is added.
        self.assertNotIn("直前の操作", "\n".join(logs.report(mine, [], [], [], [])[0]))
        early = [L(t0 - 1, "Early", "x")]
        self.assertNotIn("直前の操作", "\n".join(logs.report(early, [], [], markers, [])[0]))

    def test_full_text_mixes_in_markers(self):
        L = lambda t, tag, msg, lv="E": logs.Line(t, "", 1, 1, lv, tag, msg)  # noqa: E731
        t0 = 1_700_000_000.0
        text = logs.full_text([L(t0 + 1, "RippleDrawable", "anim error"), L(t0, "Same", "same time")],
                              [L(t0, "DP", "step 12: tap 元に戻す", "I")])
        c = logs._clock
        self.assertEqual(text.splitlines(), [f"{c(t0)} ---- step 12: tap 元に戻す ----",
                                             f"{c(t0)} E Same: same time",
                                             f"{c(t0 + 1)} E RippleDrawable: anim error"])


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


class PerfTest(unittest.TestCase):
    GFX = """Applications Graphics Acceleration Info:
** Graphics info for pid 1234 [com.example.notes] **

Stats since: 123456789ns
Total frames rendered: 1204
Janky frames: 825 (68.52%)
Janky frames (legacy): 31 (2.57%)
50th percentile: 10ms
90th percentile: 15ms
95th percentile: 18ms
99th percentile: 24ms
Number Missed Vsync: 15
Number High input latency: 168
Number Slow UI thread: 825
Number Slow bitmap uploads: 0
Number Slow issue draw commands: 391
Number Frame deadline missed: 825
Number Frame deadline missed (legacy): 12
"""
    MEM = """Applications Memory Usage (in Kilobytes):
 App Summary
                       Pss(KB)                        Rss(KB)
                        ------                         ------
           Java Heap:    19968                          31000
         Native Heap:    51600                          52000
                Code:    88064                         120000
               Stack:     1900                           1900
            Graphics:    96768                          96768
       Private Other:     8700
              System:    16500
             Unknown:                                    2000

           TOTAL PSS:   283572            TOTAL RSS:   303668       TOTAL SWAP PSS:      120

 Objects
               Views:       10         ViewRootImpl:        1
         AppContexts:        5           Activities:        1
            WebViews:        0
"""

    def test_start(self):
        out = "Starting: Intent { cmp=a/.Main }\nStatus: ok\nLaunchState: COLD\nTotalTime: 1491\nWaitTime: 1503\nComplete"
        r = perf.parse_start(out)
        self.assertEqual((r["total"], r["wait"], r["state"]), (1491, 1503, "COLD"))
        s = perf.start_summary([r, {"total": 1672, "wait": 1, "state": "COLD"}, {"total": 1473, "wait": 1, "state": "WARM"}])
        self.assertEqual((s["median"], s["not_cold"]), (1491, 1))
        self.assertIsNone(perf.start_summary([perf.parse_start("Error: Activity class does not exist")])["median"])

    def test_frames_ignores_the_legacy_lines(self):
        f = perf.parse_frames(self.GFX)
        self.assertEqual((f["total"], f["janky"], f["janky_pct"]), (1204, 825, 68.52))
        self.assertEqual(f["pct"], {50: 10, 90: 15, 95: 18, 99: 24})
        self.assertEqual(f["counts"]["Frame deadline missed"], 825)
        self.assertEqual(f["counts"]["Slow bitmap uploads"], 0)
        text = "\n".join(perf.frames_lines(f))
        self.assertIn("68.52%", text)
        self.assertNotIn("Slow bitmap uploads", text)
        self.assertFalse(perf.parse_frames("No process found for: com.example")["found"])

    def test_mem(self):
        m = perf.parse_mem(self.MEM)
        self.assertEqual(m["total_pss"], 283572)
        self.assertEqual(m["summary"]["Java Heap"], 19968)
        self.assertEqual(m["summary"]["Graphics"], 96768)
        self.assertEqual((m["objects"]["Views"], m["objects"]["ViewRootImpl"], m["objects"]["Activities"]), (10, 1, 1))
        self.assertIsNone(perf.parse_mem("No process found for: com.example")["total_pss"])

    def test_apk_groups(self):
        files = [("classes.dex", 900, 400), ("classes2.dex", 100, 50), ("lib/arm64-v8a/a.so", 500, 300),
                 ("lib/arm64-v8a/b.so", 100, 60), ("assets/dict/x/y.bin", 9000, 2500), ("res/a.xml", 10, 5)]
        g = perf.apk_groups(files, depth=2, top=3)
        self.assertEqual(g[0], ("assets/dict/", 9000, 2500))
        self.assertEqual(g[1], ("*.dex", 1000, 450))
        self.assertEqual(g[2], ("lib/arm64-v8a/", 600, 360))


class TraceTest(unittest.TestCase):
    # The journal steps of a real run (device epoch seconds); the fixture's trace starts at realtime 1795210555.13.
    STEPS = [{"n": 160, "cmd": "trace start --max 60", "dev_t": 1795210555.0647554},
             {"n": 161, "cmd": "tap text:ページ一覧を開く", "dev_t": 1795210560.9172237},
             {"n": 162, "cmd": "scroll down", "dev_t": 1795210568.5490625},
             {"n": 163, "cmd": "tap 'text:ページ 2'", "dev_t": 1795210576.9170756},
             {"n": 166, "cmd": "trace stop", "dev_t": 1795210594.434743}]

    def rows(self):
        rows = trace.parse_rows((FIX / "trace-rows.csv").read_text())
        return rows, trace.step_markers(rows, self.STEPS)

    def test_config_has_package_duration_and_known_fields(self):
        cfg = trace.perfetto_config("com.example.notes", 45)
        self.assertIn('atrace_apps: "com.example.notes"', cfg)
        self.assertIn("duration_ms: 45000", cfg)
        self.assertEqual(cfg.count("fill_policy: RING_BUFFER"), 2)
        self.assertNotIn("android.log", cfg)
        self.assertIn('ftrace_events: "sched/sched_blocked_reason"', cfg)
        self.assertIn('atrace_categories: "binder_driver"', cfg)
        self.assertIn('ftrace_events: "sched/sched_switch"', cfg)
        with self.assertRaises(Fail):
            trace.perfetto_config("bad'pkg", 5)

    def test_query_quotes_the_package(self):
        sql = trace.query_sql("com.example.notes")
        self.assertIn("name = 'com.example.notes'", sql)
        self.assertIn("substr(name, 1, 18) = 'com.example.notes:'", sql)

    def test_parse_rows_reads_quoted_csv_and_blank_lines(self):
        text = '"k","name","n","dur","tot","ts"\n"work","a, ""b""",2,5,7,9\n\n"k","name","n","dur","tot","ts"\n"gc","",1,2,3,4\n'
        rows = trace.parse_rows(text)
        self.assertEqual([r["k"] for r in rows], ["work", "gc"])
        self.assertEqual(rows[0]["name"], 'a, "b"')
        self.assertEqual(rows[0]["tot"], 7)

    def test_query_strips_quotes_from_names(self):
        # trace_processor_shell prints quotes inside a CSV field unescaped, which would break parsing.
        self.assertIn("replace(replace(name, '\"', ''''), char(10), ' ')", trace.query_sql("com.example.notes"))

    def test_remote_path_is_unique_per_app_and_run(self):
        a = trace.remote_path("com.example.a", 1795210555.9)
        self.assertEqual(a, "/data/misc/perfetto-traces/dp-com.example.a-1795210555.pftrace")
        self.assertNotEqual(a, trace.remote_path("com.example.b", 1795210555.9))
        self.assertNotEqual(a, trace.remote_path("com.example.a", 1795210600))

    def test_steps_are_placed_by_device_realtime(self):
        rows, markers = self.rows()
        self.assertEqual([(round(m.t, 1), m.msg) for m in markers][:3],
                         [(-0.1, "step 160: trace start --max 60"), (5.8, "step 161: tap text:ページ一覧を開く"),
                          (13.4, "step 162: scroll down")])

    def test_no_realtime_anchor_means_no_steps(self):
        rows, _ = self.rows()
        rows = [dict(r, dur=0) if r["k"] == "anchor" else r for r in rows]
        self.assertEqual(trace.step_markers(rows, self.STEPS), [])

    def test_summarize_frames_states_and_work(self):
        rows, markers = self.rows()
        head, lines, fields = trace.summarize(rows, markers)
        # Steps 163 and 166 come after this 16.3 s span ended, so they are not in the range.
        self.assertEqual(head, "trace 16.3 秒（step 160–162）")
        self.assertEqual(fields["frames"]["total"], 379)
        self.assertEqual(fields["frames"]["janky"], 4)
        self.assertEqual(fields["frames"]["types"], {"App Deadline Missed": 4})
        self.assertEqual(fields["main_thread_s"]["Runnable"], 0.08)
        self.assertEqual(fields["work"][0]["name"], "traversal")
        self.assertEqual(len(fields["work"]), trace.MAX_WORK)
        self.assertEqual(fields["binder"]["count"], 52)
        self.assertEqual(fields["gc"], {"count": 3, "total_ms": 20.1, "max_ms": 12.4})
        text = "\n".join(lines)
        self.assertIn("フレーム 379  遅い 4（1.1%）  App Deadline Missed 4", text)
        self.assertIn("Running 0.5秒（3%）", text)
        self.assertIn("AIDL IActivityManager::getRunningAppProcesses", text)
        self.assertLessEqual(len(lines), 20)

    def test_longest_frame_at_realtime_1795210563_9_is_step_161(self):
        rows, markers = self.rows()
        _, lines, _ = trace.summarize(rows, markers)
        frame = next(l for l in lines if l.startswith("    +8.8s 90ms"))
        self.assertTrue(frame.endswith("（step 161: tap text:ページ一覧を開く）"))
        self.assertTrue(next(l for l in lines if l.startswith("GC ")).endswith("（step 160: trace start --max 60）"))

    def test_stuffing_alone_is_not_jank_and_types_are_split(self):
        rows = [{"k": "jank", "name": n, "n": c, "dur": 0, "tot": 0, "ts": 0} for n, c in (
            ("None", 10), ("Buffer Stuffing", 5), ("SurfaceFlinger Stuffing", 4),
            ("App Deadline Missed, Buffer Stuffing", 2), ("App Deadline Missed", 1))]
        f = trace.frame_fields(rows)
        self.assertEqual((f["total"], f["janky"], f["types"]), (22, 3, {"App Deadline Missed": 3}))

    def test_io_wait_and_kernel_wait_are_separate(self):
        rows, markers = self.rows()
        self.assertEqual(trace.state_seconds(rows)["I/O"], 0)
        self.assertGreater(trace.state_seconds(rows)["カーネル待ち"], 0)
        rows.append({"k": "state", "name": "IO", "n": 1, "dur": 2_000_000_000, "tot": 0, "ts": 0})
        self.assertEqual(trace.state_seconds(rows)["I/O"], 2.0)

    def test_several_processes_are_said_out_loud(self):
        rows, markers = self.rows()
        rows.append({"k": "app", "name": "com.android.systemui:sub", "n": 4000, "dur": 0, "tot": 0, "ts": 0})
        head, lines, fields = trace.summarize(rows, markers)
        self.assertEqual(fields["processes"], 2)
        self.assertIn("2 個のプロセス", lines[0])

    def test_no_frames_text_does_not_claim_the_screen_was_still(self):
        rows, markers = self.rows()
        rows = [r for r in rows if r["k"] not in ("jank", "ftop", "dofr", "dtop")]
        _, lines, _ = trace.summarize(rows, markers)
        self.assertIn("記録した 16.3 秒の間に、アプリのフレームはありませんでした", lines)

    def test_steps_before_the_first_one_get_no_label(self):
        rows, markers = self.rows()
        _, lines, _ = trace.summarize(rows, [m for m in markers if m.t > 1.9])
        self.assertNotIn("（step", next(l for l in lines if l.startswith("  traversal")))

    def test_choreographer_fallback_without_frametimeline(self):
        rows, markers = self.rows()
        rows = [r for r in rows if r["k"] not in ("jank", "ftop")]
        _, lines, fields = trace.summarize(rows, markers)
        self.assertEqual(fields["frames"]["source"], "choreographer")
        self.assertEqual(fields["frames"]["total"], 247)
        self.assertEqual(fields["frames"]["janky"], 3)
        self.assertIn("Choreographer#doFrame", lines[0])

    def test_no_app_process_is_a_failure(self):
        rows, markers = self.rows()
        with self.assertRaises(Fail):
            trace.summarize([r for r in rows if r["k"] != "app"], markers)

    def test_work_names_lose_instance_ids(self):
        rows = [{"k": "work", "name": f"ViewPostImeInputStage id=0x{i}f", "n": 1, "dur": i * 1000000, "tot": i * 1000000,
                 "ts": i} for i in (1, 2)]
        merged = trace.merge_work(rows)
        self.assertEqual([(r["name"], r["n"], r["dur"], r["ts"]) for r in merged], [("ViewPostImeInputStage", 2, 2000000, 2)])

    def test_aidl_name(self):
        self.assertEqual(trace.aidl_name("AIDL::cpp::IFoo::bar::cppClient"), "IFoo::bar")
        self.assertEqual(trace.aidl_name("AIDL::java::IFoo::bar::server"), "IFoo::bar")

    def _zip(self, inner="linux-amd64/trace_processor_shell", data=b"#!/bin/sh\n"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(inner, data)
        return buf.getvalue()

    def test_digest_check_rejects_a_wrong_file(self):
        with self.assertRaises(Fail) as cm:
            trace.check_digest(self._zip(), "linux-amd64")
        self.assertIn("ハッシュ", cm.exception.msg)
        self.assertEqual(set(trace.DIGESTS), {"linux-amd64", "linux-arm64", "mac-amd64", "mac-arm64"})

    def test_digest_check_accepts_the_matching_file(self):
        data = self._zip()
        import hashlib
        orig = dict(trace.DIGESTS)
        try:
            trace.DIGESTS["linux-amd64"] = hashlib.sha256(data).hexdigest()
            trace.check_digest(data, "linux-amd64")
        finally:
            trace.DIGESTS.update(orig)

    def test_concurrent_extract_leaves_no_part_files(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "trace_processor_shell"
            trace.extract_shell(self._zip(), dest)
            trace.extract_shell(self._zip(), dest)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["trace_processor_shell"])

    def test_broken_zip_is_a_failure_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(Fail):
                trace.extract_shell(b"not a zip", Path(d) / "x")
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_bad_download_is_never_written_or_run(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "tp" / "trace_processor_shell"
            fake = mock.MagicMock()
            fake.__enter__.return_value.read.return_value = self._zip()
            with mock.patch("urllib.request.urlopen", return_value=fake):
                with self.assertRaises(Fail):
                    trace.download_shell("linux-amd64", dest)
            self.assertFalse(dest.exists())

    def test_extract_takes_only_the_shell_and_makes_it_executable(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "x" / "trace_processor_shell"
            trace.extract_shell(self._zip(), dest)
            self.assertTrue(os.access(dest, os.X_OK))
            self.assertEqual([p.name for p in dest.parent.iterdir()], ["trace_processor_shell"])
            with self.assertRaises(Fail):
                trace.extract_shell(self._zip("linux-amd64/perfetto"), Path(d) / "y" / "trace_processor_shell")


class TraceCliTest(unittest.TestCase):
    """The device side of dp trace, against a fake adb."""
    TRACE = None  # set by the tests that need a real file to pull

    def make(self, tmp, cmdline="perfetto --background", boot="b1", remote_exists=True, alive_at_stop=True):
        import types
        from unittest import mock
        from dp import cli, config
        from dp.state import State
        fake = types.SimpleNamespace(calls=[], alive=alive_at_stop, killed=False)
        app = config.App(name="notes", package="com.example.notes", repo=Path(tmp))

        class Adb:
            def run(s, *a, input=None, **k):
                fake.calls.append(a)
                if a[0] == "shell":
                    fake.input = input
                    return subprocess.CompletedProcess(a, 0, "4242\n", "")
                Path(a[2]).write_bytes(b"x")
                return subprocess.CompletedProcess(a, 0, "", "")

            def sh(s, cmd, timeout=0, check=False):
                fake.calls.append(cmd)
                if cmd.startswith("log -t"):
                    return "1700000100.5\n"
                if cmd.startswith("kill"):
                    fake.killed, fake.alive = True, False
                    return ""
                if "boot_id" in cmd and "cmdline" in cmd:
                    return boot + "\n" + (cmdline if fake.alive else "")
                if "boot_id" in cmd:
                    return "b1\n"
                if cmd.startswith("[ -f"):
                    return "yes" if remote_exists else "no"
                return ""

        dev = types.SimpleNamespace(serial="x", api=34, adb=Adb(), epoch=lambda: 1.0)
        ctx = cli.Ctx.__new__(cli.Ctx)
        ctx.args, ctx.app, ctx.state, ctx._dev, ctx.in_flow, ctx.flow_yes = types.SimpleNamespace(), app, State(app), dev, False, False
        return cli, ctx, fake

    def args(self, what, **kw):
        import types
        return types.SimpleNamespace(what=what, max=30, file=None, **kw)

    def start(self, tmp, **kw):
        cli, ctx, fake = self.make(tmp, **kw)
        res = cli.c_trace(ctx, self.args("start"))
        self.assertEqual(res.code, 0, res.render())
        return cli, ctx, fake

    def test_start_stores_boot_id_and_a_remote_file_of_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx, fake = self.start(tmp)
            cur = ctx.dstate["trace"]
            self.assertEqual((cur["pid"], cur["boot_id"], cur["n"]), (4242, "b1", 1))
            self.assertRegex(cur["remote"], r"/dp-com\.example\.notes-1700000100\.pftrace$")
            self.assertIn("duration_ms: 30000", fake.input)

    def test_stop_kills_only_our_perfetto(self):
        with tempfile.TemporaryDirectory() as tmp:
            from unittest import mock
            cli, ctx, fake = self.start(tmp, cmdline="/system/bin/surfaceflinger")
            with mock.patch.object(cli.trace, "analyze", return_value=("trace 30.0 秒", [], {"seconds": 30.0})):
                res = cli.c_trace(ctx, self.args("stop"))
            self.assertFalse(fake.killed)  # pid reused by another process: counts as finished
            self.assertEqual(res.lines[0], "--max 30 秒で自動で止まっていました。そのあとの操作は入っていません")

    def test_a_short_trace_that_ended_early_is_not_called_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            from unittest import mock
            cli, ctx, fake = self.start(tmp, cmdline="")
            with mock.patch.object(cli.trace, "analyze", return_value=("trace 4.4 秒", [], {"seconds": 4.4})):
                res = cli.c_trace(ctx, self.args("stop"))
            self.assertNotIn("--max", res.lines[0])
            self.assertIn("dp trace stop の前に記録が終わっていました", res.lines[0])

    def test_stop_ignores_a_pid_from_another_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli, ctx, fake = self.start(tmp)
            ctx.dstate["trace"]["boot_id"] = "old-boot"
            cli.c_trace(ctx, self.args("stop"))
            self.assertFalse(fake.killed)

    def test_stop_with_a_lost_file_clears_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli, ctx, fake = self.start(tmp, remote_exists=False)
            res = cli.c_trace(ctx, self.args("stop"))
            self.assertEqual(res.code, 1)
            self.assertIn("やり直して", "\n".join(res.lines))
            self.assertNotIn("trace", ctx.dstate)

    def test_stop_saves_the_journal_steps_beside_the_trace(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            cli, ctx, fake = self.start(tmp)
            ctx.state.next_step()
            ctx.state.journal({"n": 2, "cmd": "tap text:OK", "dev": "x", "dev_t": 1700000105.0})
            ctx.state.journal({"n": 9, "cmd": "tap other-device", "dev": "y", "dev_t": 1700000106.0})
            with mock.patch.object(cli.trace, "analyze", return_value=("trace 1.0 秒", [], {})) as an:
                res = cli.c_trace(ctx, self.args("stop"))
            self.assertEqual(res.code, 0, res.render())
            steps = an.call_args[0][2]
            self.assertEqual([(s["n"], s["cmd"]) for s in steps], [(1, "trace start --max 30"), (2, "tap text:OK"), (3, "trace stop")])
            side = json.loads(next(Path(tmp).glob(".dp/runs/*-trace/trace.json")).read_text())
            self.assertEqual(side["steps"], steps)
            self.assertNotIn("trace", ctx.dstate)
            self.assertTrue(fake.killed)


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
        self.assertIsNone(self.run_hook("dp perf start --runs 3"))
        self.assertIsNone(self.run_hook("dp -a calc perf mem"))
        self.assertIsNone(self.run_hook("dp env dark on"))
        self.assertEqual(self.run_hook("dp register R5C --policy test"), "ask")


if __name__ == "__main__":
    unittest.main()
