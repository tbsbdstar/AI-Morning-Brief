# -*- coding: utf-8 -*-
"""检索日志工具：合并钩子写下的原始留档、提取链接、生成可读版日志。"""
import datetime
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

URL_RE = re.compile(r"https?://[^\s\"'<>\\\]\)）】」，。、；]+")
TRACKING_PREFIXES = ("utm_", "spm", "from", "share", "ref", "src", "wfr", "scene",
                     "fbclid", "gclid", "mc_", "ncid", "cmpid", "sessionid")
TOOL_LABEL = {"WebSearch": "检索", "WebFetch": "打开"}


# ---------- 链接规范化 ----------
def url_key(u):
    """比对用的规范形：去协议/www、域名小写、路径解码去尾斜杠、去跟踪参数和锚点。"""
    u = (u or "").strip().rstrip(".,;:")
    try:
        sp = urlsplit(u)
    except ValueError:
        return u.lower()
    host = (sp.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(sp.path or "").rstrip("/")
    pairs = sorted((k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
                   if not k.lower().startswith(TRACKING_PREFIXES))
    qs = "&".join(f"{k}={v}" for k, v in pairs)
    return host + path + ("?" + qs if qs else "")


def url_key_noquery(u):
    return url_key(u).split("?", 1)[0]


def extract_urls(obj):
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    seen, out = set(), []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_title_links(obj):
    """从检索返回里找 {title, url} 对，找不到标题的只留链接。"""
    pairs = []

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("url"), str) and x["url"].startswith("http"):
                pairs.append((str(x.get("title") or "").strip(), x["url"]))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    got = {u for _, u in pairs}
    for u in extract_urls(obj):
        if u not in got:
            pairs.append(("", u))
            got.add(u)
    return pairs


# ---------- 合并原始留档 ----------
def load_raw(raw_dir):
    recs = []
    for p in sorted(Path(raw_dir).glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        rec["_file"] = p.name
        recs.append(rec)
    recs.sort(key=lambda r: r.get("logged_ns", 0))
    return recs


def _fetch_code(resp):
    if isinstance(resp, dict) and isinstance(resp.get("code"), int):
        return resp["code"]
    return None


def build_entries(recs):
    """把一次调用的 pre / post / fail 三份原始记录配成一条检索记录，按发起时间编号。"""
    entries, by_id = [], {}
    for r in recs:
        pl = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        tid = pl.get("tool_use_id")
        tool = pl.get("tool_name")
        ev = r.get("event")
        if ev == "pre":
            e = {"seq": len(entries) + 1, "tool": tool, "tool_use_id": tid,
                 "started_at": r.get("logged_at"), "finished_at": None,
                 "input": pl.get("tool_input"), "status": "无返回",
                 "response": None, "error": None, "raw_files": [r["_file"]]}
            entries.append(e)
            if tid:
                by_id[tid] = e
            continue
        e = by_id.get(tid) if tid else None
        if e is None:  # 没有 tool_use_id 时按「同工具 + 同输入 + 尚未结束」配对
            e = next((x for x in entries if x["finished_at"] is None and x["tool"] == tool
                      and x["input"] == pl.get("tool_input")), None)
        if e is None:
            e = {"seq": len(entries) + 1, "tool": tool, "tool_use_id": tid,
                 "started_at": r.get("logged_at"), "finished_at": None,
                 "input": pl.get("tool_input"), "status": "无返回",
                 "response": None, "error": None, "raw_files": []}
            entries.append(e)
        e["finished_at"] = r.get("logged_at")
        e["raw_files"].append(r["_file"])
        if ev == "post":
            e["response"] = pl.get("tool_response")
            code = _fetch_code(e["response"])
            if tool == "WebFetch" and code and code >= 400:
                e["status"] = f"失败(HTTP {code})"
            elif tool == "WebFetch" and code and 300 <= code < 400:
                e["status"] = f"重定向(HTTP {code})"
            else:
                e["status"] = "成功"
        else:
            e["error"] = pl.get("error") or pl.get("tool_response") or "（无错误详情）"
            e["status"] = "失败"
    return entries


def stats(entries):
    s = {"total": len(entries), "search": 0, "fetch": 0, "fetch_ok": 0, "failed": 0}
    for e in entries:
        if e["tool"] == "WebSearch":
            s["search"] += 1
        elif e["tool"] == "WebFetch":
            s["fetch"] += 1
            if e["status"] == "成功":
                s["fetch_ok"] += 1
        if not e["status"].startswith("成功"):
            s["failed"] += 1
    return s


# ---------- 链接索引：每条链接在哪几次调用里出现过 ----------
def url_index(entries):
    idx, idx_nq = {}, {}

    def add(u, seq, kind):
        k = url_key(u)
        idx.setdefault(k, []).append((seq, kind))
        nq = url_key_noquery(u)
        if nq.count("/") >= 1 and len(nq.split("/", 1)[1]) > 1:  # 只对有实际路径的链接做去参数匹配
            idx_nq.setdefault(nq, []).append((seq, kind))

    for e in entries:
        if e["tool"] == "WebSearch":
            for _, u in extract_title_links(e.get("response")):
                add(u, e["seq"], "检索结果")
        elif e["tool"] == "WebFetch":
            target = (e.get("input") or {}).get("url", "")
            add(target, e["seq"], "打开成功" if e["status"] == "成功" else "打开未成功")
            tk = url_key(target)
            for u in extract_urls(e.get("response")):
                if url_key(u) != tk:
                    add(u, e["seq"], "页面内链接")
    return idx, idx_nq


def lookup(idx_pair, url):
    idx, idx_nq = idx_pair
    hits = idx.get(url_key(url))
    if hits:
        return hits
    return idx_nq.get(url_key_noquery(url), [])


# ---------- 可读版日志 ----------
DISPLAY_TZ = None  # 由调用方按 config.json 的时区设置；留档原文保留本机时间与时差


def _hm(ts):
    if DISPLAY_TZ and ts:
        try:
            return datetime.datetime.fromisoformat(ts).astimezone(DISPLAY_TZ).strftime("%H:%M:%S")
        except ValueError:
            pass
    return (ts or "")[11:19]


def _brief_input(e):
    inp = e.get("input") or {}
    if e["tool"] == "WebSearch":
        return inp.get("query", json.dumps(inp, ensure_ascii=False))
    if e["tool"] == "WebFetch":
        return inp.get("url", json.dumps(inp, ensure_ascii=False))
    return json.dumps(inp, ensure_ascii=False)


def _cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_md(entries, title):
    s = stats(entries)
    lines = [f"# {title}", "",
             f"共 {s['total']} 次调用：检索 {s['search']} 次，打开网页 {s['fetch']} 次"
             f"（成功 {s['fetch_ok']}），未成功 {s['failed']} 次。",
             "原始留档在同目录 raw/ 下，一次调用一份文件，未经加工；本文件是它的可读版。", "",
             "| # | 时间 | 动作 | 查询词 / 网址 | 状态 | 结果 |", "|---|---|---|---|---|---|"]
    for e in entries:
        if e["tool"] == "WebSearch":
            res = f"{len(extract_title_links(e.get('response')))} 条链接"
        elif e["tool"] == "WebFetch":
            r = e.get("response")
            res = f"{r.get('bytes', '?')} 字节" if isinstance(r, dict) else ""
        else:
            res = ""
        lines.append(f"| {e['seq']} | {_hm(e['started_at'])} | {TOOL_LABEL.get(e['tool'], e['tool'])} | "
                     f"{_cell(_brief_input(e))} | {e['status']} | {res} |")
    lines += ["", "## 逐条明细", ""]
    for e in entries:
        lines.append(f"### #{e['seq']} {TOOL_LABEL.get(e['tool'], e['tool'])} · {_hm(e['started_at'])} · {e['status']}")
        if e["tool"] == "WebSearch":
            lines.append(f"查询：{_brief_input(e)}")
            lines.append("")
            for i, (t, u) in enumerate(extract_title_links(e.get("response")), 1):
                lines.append(f"{i}. [{t or u}]({u})")
        elif e["tool"] == "WebFetch":
            inp = e.get("input") or {}
            lines.append(f"网址：{inp.get('url', '')}")
            if inp.get("prompt"):
                lines.append(f"阅读要求：{inp['prompt']}")
            r = e.get("response")
            if isinstance(r, dict):
                body = r.get("result") or json.dumps(r, ensure_ascii=False)
                lines.append(f"HTTP {r.get('code', '?')} · {r.get('bytes', '?')} 字节")
            else:
                body = json.dumps(r, ensure_ascii=False) if r is not None else ""
            if body:
                body = str(body)
                lines.append("")
                lines.append("返回内容（节选）：")
                lines.append("")
                lines += ["> " + ln for ln in body[:1500].splitlines()]
                if len(body) > 1500:
                    lines.append(f"> …（共 {len(body)} 字，全文见 raw/）")
        if e.get("error"):
            lines.append(f"错误：{_cell(json.dumps(e['error'], ensure_ascii=False)[:500])}")
        lines.append(f"原始文件：{', '.join(e['raw_files'])}")
        lines.append("")
    return "\n".join(lines)


def write_merged(raw_dir, out_dir, title):
    entries = build_entries(load_raw(raw_dir))
    out_dir = Path(out_dir)
    with open(out_dir / "检索日志.jsonl", "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    (out_dir / "检索日志.md").write_text(render_md(entries, title), encoding="utf-8")
    return entries
