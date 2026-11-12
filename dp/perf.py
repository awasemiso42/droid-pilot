"""Measurements of the app under test, read from `am start -W`, `dumpsys gfxinfo` and `dumpsys meminfo`.

The parsers are pure (text in, dict out) so they can be tested without a device.
"""
import re
import statistics
import zipfile
from collections import Counter

GFX_COUNTS = ("Missed Vsync", "High input latency", "Slow UI thread", "Slow bitmap uploads",
              "Slow issue draw commands", "Frame deadline missed")
MEM_SUMMARY = ("Java Heap", "Native Heap", "Code", "Stack", "Graphics", "Private Other", "System")
MEM_OBJECTS = ("Views", "ViewRootImpl", "AppContexts", "Activities", "WebViews")


def parse_start(text: str) -> dict:
    """`am start -W` output -> {total, wait, state}; total is None when the start did not complete."""
    def num(key):
        m = re.search(rf"{key}:\s*(\d+)", text)
        return int(m.group(1)) if m else None
    m = re.search(r"LaunchState:\s*(\w+)", text)
    return {"total": num("TotalTime"), "wait": num("WaitTime"), "state": m.group(1) if m else ""}


def start_summary(runs: list) -> dict:
    totals = [r["total"] for r in runs if r["total"] is not None]
    return {"runs": totals, "median": int(statistics.median(totals)) if totals else None,
            "not_cold": sum(1 for r in runs if r["state"] and r["state"] != "COLD")}


def parse_frames(text: str) -> dict:
    """`dumpsys gfxinfo <pkg>` -> totals, janky share, percentiles (ms) and the slow-frame counters."""
    def find(pattern, cast=int):
        m = re.search(pattern, text)
        return cast(m.group(1)) if m else None
    out = {"total": find(r"Total frames rendered:\s*(\d+)"),
           "janky": find(r"Janky frames:\s*(\d+)"),
           "janky_pct": find(r"Janky frames:\s*\d+\s*\(([\d.]+)%\)", float),
           "pct": {p: find(rf"{p}th percentile:\s*(\d+)ms") for p in (50, 90, 95, 99)},
           "counts": {k: find(rf"Number {re.escape(k)}:\s*(\d+)") for k in GFX_COUNTS}}
    out["found"] = out["total"] is not None
    return out


def frames_lines(f: dict) -> list:
    if not f["found"]:
        return ["フレームの記録がありません（アプリが動いていないか、まだ描画していません）"]
    pct = "  ".join(f"p{p} {v}ms" for p, v in f["pct"].items() if v is not None)
    lines = [f"フレーム {f['total']}  遅いフレーム {f['janky']}"
             + (f"（{f['janky_pct']:g}%）" if f["janky_pct"] is not None else ""),
             pct]
    slow = "  ".join(f"{k} {v}" for k, v in f["counts"].items() if v)
    if slow:
        lines.append(slow)
    return [l for l in lines if l]


def parse_mem(text: str) -> dict:
    """`dumpsys meminfo <pkg>` -> kB per App Summary row, TOTAL PSS and the object counts."""
    def find(key):
        m = re.search(rf"(?<![\w]){re.escape(key)}:\s+(\d+)", text)
        return int(m.group(1)) if m else None
    return {"total_pss": find("TOTAL PSS") or find("TOTAL"),
            "summary": {k: find(k) for k in MEM_SUMMARY},
            "objects": {k: find(k) for k in MEM_OBJECTS}}


def mem_lines(m: dict) -> list:
    def mb(kb):
        return f"{kb / 1024:.1f}MB"
    if m["total_pss"] is None:
        return ["メモリの記録がありません（アプリが動いていません）"]
    lines = [f"TOTAL PSS {mb(m['total_pss'])}（{m['total_pss']}kB）",
             "  ".join(f"{k} {mb(v)}" for k, v in m["summary"].items() if v is not None),
             "  ".join(f"{k} {v}" for k, v in m["objects"].items() if v is not None)]
    return [l for l in lines if l]


def apk_groups(names_sizes, depth=2, top=8):
    """[(path, uncompressed, compressed)] -> the largest groups by compressed size.

    Files are grouped by their first `depth` directories; top-level files by extension (classes*.dex -> *.dex).
    """
    comp, raw = Counter(), Counter()
    for name, size, csize in names_sizes:
        parts = name.split("/")
        key = "/".join(parts[:depth]) + "/" if len(parts) > depth else (
            "/".join(parts[:-1]) + "/" if len(parts) > 1 else "*." + name.rsplit(".", 1)[-1])
        comp[key] += csize
        raw[key] += size
    return [(k, raw[k], c) for k, c in comp.most_common(top)]


def apk_report(path, depth=2, top=8) -> dict:
    with zipfile.ZipFile(path) as z:
        infos = [(i.filename, i.file_size, i.compress_size) for i in z.infolist()]
    return {"size": path.stat().st_size, "files": len(infos), "groups": apk_groups(infos, depth, top)}
