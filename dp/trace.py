"""Perfetto traces: the on-device config, the PC-side trace_processor, and a short summary.

Everything here is pure or touches only the PC (no adb), so it can be tested without a device.
The device side (start / stop / pull) lives in cli.py.
"""
import csv
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
import http.client
import zipfile
from collections import namedtuple
from pathlib import Path

from . import config, logs
from .out import Fail

VERSION = "v59.1"
RELEASE_URL = "https://github.com/google/perfetto/releases/download/{version}/{plat}.zip"
DIGESTS = {
    "linux-amd64": "7deb8c2572c6ae7ec80cd719d388041079827b0c44a1659694d5d88faa4f501e",
    "linux-arm64": "8f59e2cf7197c8de388ab4765a650eaf1774a405b1753c54a9741910b6f11350",
    "mac-amd64": "392cb203372f8904a138d7f5e0d5c6163b1657cb772b6036807bbb8247bcdadd",
    "mac-arm64": "91b2cb63c706526a2c067c219115f9f044fa8467459868757bae15c97906037f",
}
REMOTE_DIR = "/data/misc/perfetto-traces"
MIN_API = 29
FRAME_MS = 1000 / 60
UI_URL = "https://ui.perfetto.dev"
MAX_TOP_FRAMES = 4
NOT_JANK = ("None", "Buffer Stuffing", "SurfaceFlinger Stuffing")
MAX_WORK = 6  # the query returns a few more so merging names does not leave gaps

Marker = namedtuple("Marker", "t msg")  # t = seconds from when tracing started (what logs._step_at wants)


# --- config ---------------------------------------------------------------------------------

FTRACE_EVENTS = ("sched/sched_switch", "sched/sched_waking", "sched/sched_wakeup", "sched/sched_process_exit",
                 "sched/sched_process_free", "task/task_newtask", "task/task_rename", "power/suspend_resume",
                 "sched/sched_blocked_reason")
ATRACE_CATEGORIES = ("gfx", "view", "input", "am", "wm", "dalvik", "binder_driver", "aidl", "res")


def remote_path(package: str, t: float) -> str:
    """A file name of its own per recording, so two apps (or two runs) never share one."""
    return f"{REMOTE_DIR}/dp-{package}-{int(t)}.pftrace"


def perfetto_config(package: str, max_secs: int) -> str:
    """The TraceConfig (text proto) for `perfetto --txt -c -`. Repeated fields are one line per value."""
    if not re.fullmatch(r"[A-Za-z0-9_.]+", package):
        raise Fail(f"パッケージ名がおかしいです: {package}")
    events = "".join(f'      ftrace_events: "{e}"\n' for e in FTRACE_EVENTS)
    cats = "".join(f'      atrace_categories: "{c}"\n' for c in ATRACE_CATEGORIES)
    return f"""buffers {{
  size_kb: 65536
  fill_policy: RING_BUFFER
}}
buffers {{
  size_kb: 4096
  fill_policy: RING_BUFFER
}}
data_sources {{
  config {{
    name: "linux.ftrace"
    target_buffer: 0
    ftrace_config {{
{events}{cats}      atrace_apps: "{package}"
    }}
  }}
}}
data_sources {{
  config {{
    name: "linux.process_stats"
    target_buffer: 1
    process_stats_config {{
      scan_all_processes_on_start: true
    }}
  }}
}}
data_sources {{
  config {{
    name: "android.surfaceflinger.frametimeline"
    target_buffer: 0
  }}
}}
duration_ms: {int(max_secs) * 1000}
"""


# --- trace_processor_shell on the PC -----------------------------------------------------------

def platform_name() -> str:
    system = {"Linux": "linux", "Darwin": "mac"}.get(platform.system())
    arch = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if not system or not arch or f"{system}-{arch}" not in DIGESTS:
        raise Fail(f"この PC（{platform.system()} {platform.machine()}）用の trace_processor がありません",
                   ["DP_TRACE_PROCESSOR に trace_processor_shell のパスを入れてください"])
    return f"{system}-{arch}"


def check_digest(data: bytes, plat: str):
    got = hashlib.sha256(data).hexdigest()
    if got != DIGESTS[plat]:
        raise Fail("ダウンロードした trace_processor のハッシュが合いません（実行しません）",
                   [f"期待 {DIGESTS[plat]}", f"実際 {got}"])


def extract_shell(data: bytes, dest: Path):
    """Writes only trace_processor_shell out of the release zip. Atomic, and safe against a parallel download."""
    tmp = None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == "trace_processor_shell"]
            if not names:
                raise Fail("zip の中に trace_processor_shell がありません")
            content = z.read(names[0])
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".part")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    except (OSError, zipfile.BadZipFile) as e:
        raise Fail(f"trace_processor を取り出せません: {e}")
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def download_shell(plat: str, dest: Path, notify=print):
    url = RELEASE_URL.format(version=VERSION, plat=plat)
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
    except (OSError, http.client.HTTPException) as e:
        raise Fail(f"trace_processor をダウンロードできません: {e}", [url, "DP_TRACE_PROCESSOR にパスを入れる方法もあります"])
    check_digest(data, plat)
    extract_shell(data, dest)
    notify(f"trace_processor {VERSION} を取得しました（{dest}）")


def find_shell(notify=print) -> str:
    """DP_TRACE_PROCESSOR, then PATH, then the cache; downloads into the cache as a last resort."""
    env = os.environ.get("DP_TRACE_PROCESSOR")
    if env:
        if not Path(env).is_file():
            raise Fail(f"DP_TRACE_PROCESSOR が見つかりません: {env}")
        return env
    found = shutil.which("trace_processor_shell")
    if found:
        return found
    plat = platform_name()
    cached = config.CACHE / "perfetto" / VERSION / plat / "trace_processor_shell"
    if not cached.is_file():
        download_shell(plat, cached, notify)
    return str(cached)


# --- the query ------------------------------------------------------------------------------------

# trace_processor_shell does not escape quotes inside CSV fields, so the names are made quote-free in SQL.
# The window is [tracing_started_ns, tracing_disabled_ns): FrameTimeline back-fills frames from before it, so they are ignored.
# `anchor` carries the offset from trace time to device realtime, which is how dp's journal steps are placed on the trace.
# One statement, one row shape: k (kind), name, n, dur (ns), tot (ns or a count), ts (ns). The trace is loaded once.
# `work` ranks main-thread slices by self time (dur minus children) so umbrella slices do not hide the cause.
QUERY = """
WITH win AS (
  SELECT coalesce((SELECT int_value FROM metadata WHERE name = 'tracing_started_ns'), (SELECT start_ts FROM trace_bounds)) AS s,
         coalesce((SELECT int_value FROM metadata WHERE name = 'tracing_disabled_ns'), (SELECT end_ts FROM trace_bounds)) AS e),
app AS (
  SELECT upid, pid, name FROM process WHERE name = '{pkg}' OR substr(name, 1, {plen}) = '{pkg}:'),
mt AS (
  SELECT t.utid FROM thread t JOIN app ON t.upid = app.upid WHERE t.is_main_thread = 1 OR t.tid = app.pid),
ms AS (
  SELECT s.id, s.ts, s.dur, s.name FROM slice s JOIN thread_track tt ON s.track_id = tt.id, win
  WHERE tt.utid IN (SELECT utid FROM mt) AND s.dur > 0 AND s.ts >= win.s AND s.ts < win.e),
kids AS (SELECT parent_id, sum(dur) AS cd FROM slice WHERE parent_id IN (SELECT id FROM ms) GROUP BY parent_id),
self AS (SELECT ms.name, ms.ts, ms.dur - coalesce(kids.cd, 0) AS self FROM ms LEFT JOIN kids ON kids.parent_id = ms.id),
frames AS (
  -- FrameTimeline has one row per layer (a SurfaceView, its background, BLAST ...) for the same app frame:
  -- one frame per surface frame token, with the longest layer's duration and jank type (SQLite takes the
  -- bare columns from the max() row).
  SELECT min(f.ts) AS ts, max(f.dur) AS dur, f.jank_type FROM actual_frame_timeline_slice f, win
  WHERE f.upid IN (SELECT upid FROM app) AND f.ts >= win.s AND f.ts < win.e
  GROUP BY f.upid, coalesce(f.surface_frame_token, -f.id)),
dofr AS (SELECT ts, dur FROM ms WHERE name GLOB 'Choreographer#doFrame*'),
gcs AS (
  SELECT s.ts, s.dur FROM slice s JOIN thread_track tt ON s.track_id = tt.id JOIN thread t ON t.utid = tt.utid, win
  WHERE t.upid IN (SELECT upid FROM app) AND s.name GLOB '*GC' AND s.dur > 0 AND s.ts >= win.s AND s.ts < win.e),
binder AS (SELECT ts, dur FROM ms WHERE name GLOB 'binder transaction*')
SELECT k, replace(replace(name, '"', ''''), char(10), ' ') AS name, n, dur, tot, ts FROM (
SELECT 'span' AS k, '' AS name, 0 AS n, e - s AS dur, 0 AS tot, s AS ts FROM win
UNION ALL SELECT 'anchor', '', 0, coalesce(to_realtime(s) - s, 0), 0, s FROM win
UNION ALL SELECT 'app', name, pid, 0, 0, 0 FROM app
UNION ALL SELECT 'jank', coalesce(jank_type, ''), count(*), max(dur), sum(dur), 0 FROM frames GROUP BY jank_type
UNION ALL SELECT * FROM (SELECT 'ftop', coalesce(jank_type, ''), 1, dur, dur, ts FROM frames ORDER BY dur DESC LIMIT 5)
UNION ALL SELECT 'dofr', '', count(*), coalesce(max(dur), 0), coalesce(sum(dur > 16700000), 0), 0 FROM dofr
UNION ALL SELECT * FROM (SELECT 'dtop', '', 1, dur, dur, ts FROM dofr ORDER BY dur DESC LIMIT 5)
UNION ALL SELECT 'state', CASE WHEN io_wait = 1 THEN 'IO' ELSE state END, count(*),
  sum(min(thread_state.ts + dur, win.e) - max(thread_state.ts, win.s)), 0, 0
  FROM thread_state, win
  WHERE utid IN (SELECT utid FROM mt) AND dur > 0 AND thread_state.ts < win.e AND thread_state.ts + dur > win.s
  GROUP BY 2
UNION ALL SELECT * FROM (
  SELECT 'work', name, count(*), max(self), sum(self), (SELECT ts FROM self s2 WHERE s2.name = self.name ORDER BY s2.self DESC LIMIT 1)
  FROM self WHERE name NOT GLOB 'Choreographer#doFrame*' AND name NOT GLOB 'binder transaction*'
  GROUP BY name HAVING max(self) >= 1000000 ORDER BY max(self) DESC LIMIT 12)
UNION ALL SELECT 'binder', '', count(*), coalesce(max(dur), 0), coalesce(sum(dur), 0),
  coalesce((SELECT ts FROM binder ORDER BY dur DESC LIMIT 1), 0) FROM binder
UNION ALL SELECT * FROM (
  SELECT 'aidl', name, count(*), max(dur), sum(dur), (SELECT ts FROM ms m2 WHERE m2.name = ms.name ORDER BY m2.dur DESC LIMIT 1)
  FROM ms WHERE name GLOB 'AIDL::*' GROUP BY name HAVING max(dur) >= 1000000 ORDER BY max(dur) DESC LIMIT 3)
UNION ALL SELECT 'gc', '', count(*), coalesce(max(dur), 0), coalesce(sum(dur), 0),
  coalesce((SELECT ts FROM gcs ORDER BY dur DESC LIMIT 1), 0) FROM gcs)
"""


def query_sql(package: str) -> str:
    pkg = package.replace("'", "''")
    return QUERY.format(pkg=pkg, plen=len(package) + 1)


def parse_rows(text: str) -> list:
    """trace_processor_shell output (quoted CSV with a header; blank lines between statements) -> list of dicts."""
    rows = []
    header = None
    for rec in csv.reader(io.StringIO(text)):
        if not rec:
            continue
        if header is None or rec == header:
            header = rec
            continue
        r = dict(zip(header, rec))
        for key in ("n", "dur", "tot", "ts"):
            try:
                r[key] = int(r.get(key) or 0)
            except ValueError:
                r[key] = 0
        rows.append(r)
    return rows


def run_query(package: str, trace: Path, shell=None, notify=print) -> list:
    shell = shell or find_shell(notify)
    with tempfile.TemporaryDirectory() as d:
        sql = Path(d) / "dp.sql"
        sql.write_text(query_sql(package))
        try:
            p = subprocess.run([shell, "--query-file", str(sql), str(trace)], capture_output=True, text=True,
                               timeout=300)
        except subprocess.TimeoutExpired:
            raise Fail("trace_processor が 300 秒で終わりませんでした")
        except OSError as e:
            raise Fail(f"trace_processor を実行できません: {e}", [shell])
    if p.returncode != 0:
        err = [l for l in p.stderr.splitlines() if l.strip()][-3:]
        raise Fail("トレースを読めませんでした", err or [f"trace_processor が {p.returncode} で終わりました"])
    return parse_rows(p.stdout)


def step_markers(rows: list, steps: list) -> list:
    """dp journal steps [{n, cmd, dev_t}] -> markers placed on the trace (seconds from when tracing started).

    A step's dev_t is when dp began the action, so what happens after it belongs to that step.
    Without a realtime anchor in the trace the steps cannot be placed, so none are returned.
    """
    anchor = next((r for r in rows if r["k"] == "anchor"), None)
    if not anchor or not anchor["dur"]:
        return []
    origin = (anchor["ts"] + anchor["dur"]) / 1e9
    out = [Marker(st["dev_t"] - origin, f"step {st['n']}: {st['cmd']}") for st in steps if st.get("dev_t")]
    return sorted(out, key=lambda m: m.t)


# --- summary -------------------------------------------------------------------------------------------

# "IO" is a D state with io_wait = 1 (needs sched_blocked_reason); other D / DK is waiting inside the kernel.
STATE_GROUPS = (("Running", ("Running",)), ("Runnable", ("R", "R+")), ("I/O", ("IO",)),
                ("カーネル待ち", ("D", "DK")), ("Sleeping", ("S",)))


OPTIONAL_STATES = ("カーネル待ち",)


def _ms(ns):
    return ns / 1e6


def _ms_text(ns):
    ms = _ms(ns)
    return f"{ms:.0f}ms" if ms >= 10 else f"{ms:.1f}ms"


def _step(markers, t):
    s = logs._step_at(markers, t)
    return f"（{s}）" if s else ""


def _step_numbers(markers):
    nums = [int(m.group(1)) for mk in markers if (m := re.match(r"step (\d+)", mk.msg))]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    return f"step {lo}" if lo == hi else f"step {lo}–{hi}"


def merge_work(rows) -> list:
    """`work` rows with per-instance ids dropped from the names (ViewPostImeInputStage id=0x1f), merged, worst first."""
    merged = {}
    for r in rows:
        if r["k"] != "work":
            continue
        name = re.sub(r"\s+id=0x[0-9a-f]+", "", r["name"])
        m = merged.get(name)
        if not m:
            merged[name] = {**r, "name": name}
            continue
        m["n"] += r["n"]
        m["tot"] += r["tot"]
        if r["dur"] > m["dur"]:
            m["dur"], m["ts"] = r["dur"], r["ts"]
    return sorted(merged.values(), key=lambda r: -r["dur"])


def aidl_name(name: str) -> str:
    """AIDL::java::IFoo::bar::client -> IFoo::bar"""
    m = re.fullmatch(r"AIDL::\w+::(.+?)::\w*(?:[cC]lient|[sS]erver)", name)
    return m.group(1) if m else name


def frame_fields(rows):
    """Frames from FrameTimeline when there are any, else from Choreographer#doFrame on the main thread."""
    jank = [r for r in rows if r["k"] == "jank"]
    total = sum(r["n"] for r in jank)
    if total:
        # jank_type is a comma list ("App Deadline Missed, Buffer Stuffing"); stuffing alone is not the app's fault.
        types, janky = {}, 0
        for r in jank:
            real = [t for t in (x.strip() for x in r["name"].split(",")) if t and t not in NOT_JANK]
            janky += r["n"] if real else 0
            for t in real:
                types[t] = types.get(t, 0) + r["n"]
        top = [r for r in rows if r["k"] == "ftop"]
        return {"source": "frametimeline", "total": total, "janky": janky, "types": types, "top": top}
    do = next((r for r in rows if r["k"] == "dofr"), None)
    if do and do["n"]:
        return {"source": "choreographer", "total": do["n"], "janky": do["tot"], "types": {},
                "top": [r for r in rows if r["k"] == "dtop"]}
    return {"source": "none", "total": 0, "janky": 0, "types": {}, "top": []}


def state_seconds(rows) -> dict:
    by = {r["name"]: r["dur"] for r in rows if r["k"] == "state"}
    return {label: sum(by.get(s, 0) for s in states) / 1e9 for label, states in STATE_GROUPS}


def summarize(rows, markers):
    """(head, lines, fields) for `dp trace`. `rows` come from parse_rows, `markers` from step_markers."""
    span = next((r for r in rows if r["k"] == "span"), None)
    apps = [r for r in rows if r["k"] == "app"]
    if not span or not apps:
        raise Fail("トレースにアプリのプロセスがありません",
                   ["アプリが動いていない間に取ったか、パッケージ名が違います"])
    start = span["ts"]
    secs = span["dur"] / 1e9
    steps = _step_numbers([m for m in markers if m.t <= secs])  # not the steps after it ended
    head = f"trace {secs:.1f} 秒" + (f"（{steps}）" if steps else "")
    lines, fields = [], {"seconds": round(secs, 2), "process": apps[0]["name"], "pid": apps[0]["n"],
                         "processes": len(apps)}
    if len(apps) > 1:
        lines.append(f"{len(apps)} 個のプロセス（{', '.join(a['name'] for a in apps[:4])}）をまとめています。"
                     "フレームと GC は全部、メインスレッドは全部のメインスレッドの合計")

    f = frame_fields(rows)
    fields["frames"] = {k: f[k] for k in ("source", "total", "janky", "types")}
    if f["total"]:
        pct = 100 * f["janky"] / f["total"]
        fields["frames"]["janky_pct"] = round(pct, 1)
        if f["source"] == "frametimeline":
            lines.append(f"フレーム {f['total']}  遅い {f['janky']}（{pct:.1f}%）"
                         + "".join(f"  {k} {v}" for k, v in sorted(f["types"].items(), key=lambda kv: -kv[1])))
        else:
            lines.append(f"フレーム {f['total']}  遅い {f['janky']}（{pct:.1f}%）"
                         "（FrameTimeline がないので Choreographer#doFrame が 16.7ms を超えた数）")
        lines.append("  長いフレーム:")
        for r in f["top"][:MAX_TOP_FRAMES]:
            t = (r["ts"] - start) / 1e9
            kind = f" {r['name']}" if r["name"] and r["name"] != "None" else ""
            lines.append(f"    +{t:.1f}s {_ms_text(r['dur'])}{kind}{_step(markers, t)}")
    else:
        lines.append(f"記録した {secs:.1f} 秒の間に、アプリのフレームはありませんでした")

    st = state_seconds(rows)
    fields["main_thread_s"] = {k: round(v, 2) for k, v in st.items()}
    whole = sum(st.values())
    if whole:
        lines.append("メインスレッド  " + "  ".join(f"{k} {v:.1f}秒（{100 * v / whole:.0f}%）"
                                                for k, v in st.items() if v or k not in OPTIONAL_STATES))

    work = merge_work(rows)[:MAX_WORK]
    fields["work"] = [{"name": r["name"], "count": r["n"], "max_ms": round(_ms(r["dur"]), 1),
                       "total_ms": round(_ms(r["tot"]), 1)} for r in work]
    if work:
        lines.append("メインスレッドの重い処理（自分の時間。子の処理は除く）:")
        for r in work:
            t = (r["ts"] - start) / 1e9
            lines.append(f"  {r['name'][:60]}  x{r['n']} 最長 {_ms_text(r['dur'])} 合計 {_ms_text(r['tot'])}"
                         f"{_step(markers, t)}")

    lines += _binder_gc_lines(rows, markers, start, fields)
    return head, lines, fields


def _binder_gc_lines(rows, markers, start, fields):
    lines = []
    b = next((r for r in rows if r["k"] == "binder"), None)
    fields["binder"] = {"count": b["n"], "total_ms": round(_ms(b["tot"]), 1), "max_ms": round(_ms(b["dur"]), 1)} if b else {}
    if b and b["n"]:
        t = (b["ts"] - start) / 1e9
        lines.append(f"Binder（メイン） {b['n']} 回  合計 {_ms_text(b['tot'])}  最長 {_ms_text(b['dur'])}{_step(markers, t)}")
        for r in (r for r in rows if r["k"] == "aidl"):
            lines.append(f"  AIDL {aidl_name(r['name'])[:60]}  x{r['n']} 最長 {_ms_text(r['dur'])}")
    g = next((r for r in rows if r["k"] == "gc"), None)
    fields["gc"] = {"count": g["n"], "total_ms": round(_ms(g["tot"]), 1), "max_ms": round(_ms(g["dur"]), 1)} if g else {}
    if g and g["n"]:
        t = (g["ts"] - start) / 1e9
        lines.append(f"GC {g['n']} 回  合計 {_ms_text(g['tot'])}  最長 {_ms_text(g['dur'])}{_step(markers, t)}")
    return lines


def analyze(package: str, trace: Path, steps=(), shell=None, notify=print):
    """Runs the query and summarizes. `steps` are dp journal steps [{n, cmd, dev_t}] to attribute things to."""
    rows = run_query(package, trace, shell, notify)
    return summarize(rows, step_markers(rows, steps))


def latest_trace(app) -> Path:
    """The newest `.dp/runs/*-trace/trace.pftrace` of the app."""
    found = sorted((app.state_dir / "runs").glob("*-trace/trace.pftrace"))
    if not found:
        raise Fail("トレースがまだありません", ["dp trace start → 操作 → dp trace stop で取れます"])
    return found[-1]
