# -*- coding: utf-8 -*-
"""回查检索日志

  python 回查.py                    列出所有日期和每次运行的概况
  python 回查.py 2026-11-13         看这天（最近一次运行）的检索清单
  python 回查.py 2026-11-13 12      看这天第 12 条调用的原始留档全文
  python 回查.py --url <链接>        这个链接在哪天、哪条检索/打开记录里出现过，最后是否入选
  python 回查.py --kw <关键词>       在全部日志里按关键词找（查询词、网址、标题、返回内容）
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import loglib  # noqa: E402

LOGS = ROOT / "logs"
LABEL = loglib.TOOL_LABEL


def runs():
    for day in sorted(p for p in LOGS.glob("*") if p.is_dir() and p.name[:2] == "20"):
        for r in sorted(day.glob("run_*")):
            yield day.name, r


def entries_of(run_dir):
    f = run_dir / "检索日志.jsonl"
    if f.exists():
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return loglib.build_entries(loglib.load_raw(run_dir / "raw"))


def judged(run_dir):
    f = run_dir / "候选与判定.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text(encoding="utf-8"))
    return d.get("selected", []) + d.get("rejected", [])


def short(e):
    inp = e.get("input") or {}
    return inp.get("query") or inp.get("url") or json.dumps(inp, ensure_ascii=False)


def cmd_list():
    any_run = False
    for day, r in runs():
        any_run = True
        st = loglib.stats(entries_of(r))
        sel = sum(1 for j in judged(r) if j.get("status") == "入选")
        print(f"{day}  {r.name}  检索 {st['search']:>3}  打开 {st['fetch']:>3}  未成功 {st['failed']:>2}  入选 {sel}")
    if not any_run:
        print("还没有任何运行记录")


def latest_run(day):
    rs = sorted((LOGS / day).glob("run_*"))
    if not rs:
        sys.exit(f"{day} 没有运行记录")
    return rs[-1]


def cmd_day(day):
    r = latest_run(day)
    print(f"{day} {r.name}（可读版：{(r / '检索日志.md').relative_to(ROOT)}）\n")
    for e in entries_of(r):
        print(f"#{e['seq']:<3} {(e.get('started_at') or '')[11:19]}  {LABEL.get(e['tool'], e['tool'])}  "
              f"{e['status']:<10} {short(e)}")


def cmd_entry(day, seq):
    r = latest_run(day)
    e = next((x for x in entries_of(r) if x["seq"] == seq), None)
    if not e:
        sys.exit(f"{day} 没有第 {seq} 条")
    for fn in e.get("raw_files", []):
        print(f"===== raw/{fn} =====")
        print((r / "raw" / fn).read_text(encoding="utf-8"))


def cmd_url(url):
    key, found = loglib.url_key(url), False
    for day, r in runs():
        es = entries_of(r)
        idx = loglib.url_index(es)
        hits = loglib.lookup(idx, url)
        verdict = [j for j in judged(r) if j.get("url") and loglib.url_key(j["url"]) == key]
        if not hits and not verdict:
            continue
        found = True
        print(f"\n{day} {r.name}")
        for s, k in hits:
            e = next(x for x in es if x["seq"] == s)
            print(f"  #{s} {k}：{LABEL.get(e['tool'], e['tool'])} {short(e)}")
        for j in verdict:
            print(f"  判定：{j.get('status')} {j.get('bucket', '')} {j.get('reason', '')}")
    if not found:
        print("全部日志里都没有出现过这个链接")


def cmd_kw(kw):
    n = 0
    for day, r in runs():
        for e in entries_of(r):
            blob = json.dumps(e, ensure_ascii=False)
            if kw.lower() in blob.lower():
                n += 1
                print(f"{day} {r.name} #{e['seq']} {LABEL.get(e['tool'], e['tool'])} {short(e)}")
    print(f"\n共 {n} 条记录包含「{kw}」")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    import datetime as _dt
    loglib.DISPLAY_TZ = _dt.timezone(_dt.timedelta(hours=_cfg.get("timezone_utc_offset", 8)))
    ap = argparse.ArgumentParser(description="回查检索日志")
    ap.add_argument("day", nargs="?")
    ap.add_argument("seq", nargs="?", type=int)
    ap.add_argument("--url")
    ap.add_argument("--kw")
    a = ap.parse_args()
    if a.url:
        cmd_url(a.url)
    elif a.kw:
        cmd_kw(a.kw)
    elif a.day and a.seq:
        cmd_entry(a.day, a.seq)
    elif a.day:
        cmd_day(a.day)
    else:
        cmd_list()
