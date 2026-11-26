# -*- coding: utf-8 -*-
"""AI早知道 主流程

  检索与初选（Claude 无头运行，只开放 WebSearch / WebFetch 两个工具）
  → 代码核验（时间窗、跳转页、重复、链接是否真出现在检索日志里）
  → 成稿（Markdown + HTML，推送版 + 留档版）→ 到点推送微信 → 全程留档

用法：
  python run_brief.py                        完整运行：生成 → 等到 push_time → 推送
  python run_brief.py --no-push              只生成不推送（试跑）
  python run_brief.py --no-wait              生成后立即推送，不等 push_time
  python run_brief.py --refinalize <运行目录>  不重新检索，用该次已有结果重新核验成稿（调规则用）
  python run_brief.py --push-only [日期]       把某天已生成的早报再推一次（默认今天）
"""
import argparse
import datetime as dt
import difflib
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import loglib  # noqa: E402
import push  # noqa: E402

WEEKDAY = "一二三四五六日"
LEVELS = ["一手/官方", "权威媒体", "行业媒体", "自媒体/社区"]
BUCKETS = ["旧闻", "跳转页", "重复/已报", "无法核实", "价值不足", "其他"]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LOCK = ROOT / "state" / "running.json"


# ---------- 基础 ----------
def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def tz_of(cfg):
    hours = cfg.get("timezone_utc_offset", 8)
    return dt.timezone(dt.timedelta(hours=hours)), cfg.get("timezone_label", "北京时间")


class RunLog:
    def __init__(self, path, tz):
        self.path, self.tz = Path(path), tz
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg):
        line = f"{dt.datetime.now(self.tz):%H:%M:%S} {msg}"
        print(line)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def find_claude(cfg):
    p = cfg.get("claude", {}).get("claude_path")
    if p and Path(p).exists():
        return p
    w = shutil.which("claude")
    if w:
        return w
    cands = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_*\claude.exe"))
    cands += [os.path.expanduser(r"~\.local\bin\claude.exe"), os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
              os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe")]
    for c in cands:
        if Path(c).exists():
            return c
    raise RuntimeError("找不到 claude 命令行程序，请在 config.json 的 claude.claude_path 填写完整路径")


def console_python():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return exe.parent / "python.exe"
    return exe


TOKEN_STATE = ROOT / "state" / "token.json"
AUTH_WORDS = ("authenticat", "oauth", "unauthorized", "401", "invalid bearer", "invalid api key",
              "credential", "not logged in", "login")


def record_token_state(ok, error=""):
    """记下长期令牌最近一次是否可用（验证或每日运行都会更新），程序界面据此显示状态。"""
    TOKEN_STATE.parent.mkdir(parents=True, exist_ok=True)
    write_json(TOKEN_STATE, {"ok": bool(ok), "error": str(error)[:300],
                             "time": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")})


def verify_token(cfg, token):
    """用令牌真实调用一次（最小的模型、不开任何工具、一轮），返回 (是否可用, 错误说明)。"""
    env = os.environ.copy()
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    cmd = [find_claude(cfg), "-p", "只回复两个字母 OK", "--output-format", "json", "--model", "haiku",
           "--tools", "", "--max-turns", "1", "--no-session-persistence", "--strict-mcp-config",
           "--setting-sources", "project"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
                           env=env, timeout=120, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        out = json.loads(p.stdout or "{}")
    except Exception as exc:
        return False, f"验证时出错：{exc!r}"[:300]
    if out.get("is_error") or p.returncode != 0:
        return False, str(out.get("result") or p.stderr or "未知错误")[:300]
    return True, ""


def pid_alive(pid):
    """Windows 下判断进程是否还在（不能用 os.kill(pid, 0)，那会直接结束进程）。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259
    except Exception:
        return False


def running_info():
    info = load_json(LOCK)
    if info and pid_alive(info.get("pid", 0)):
        return info
    return None


# ---------- 时间窗 ----------
def section_windows(cfg, now):
    """每个栏目的时间窗：{栏目名: (起点, [允许的发布日期])}"""
    out = {}
    for s in cfg["sections"]:
        start = now - dt.timedelta(hours=s.get("window_hours") or cfg["window_hours"])
        days = [start.date() + dt.timedelta(days=i) for i in range((now.date() - start.date()).days + 1)]
        out[s["name"]] = (start, days)
    return out


def windows_to_meta(windows):
    return {k: {"start": v[0].isoformat(), "dates": [d.isoformat() for d in v[1]]} for k, v in windows.items()}


def windows_from_meta(meta, cfg):
    if "windows" in meta:
        return {k: (dt.datetime.fromisoformat(v["start"]), [dt.date.fromisoformat(d) for d in v["dates"]])
                for k, v in meta["windows"].items()}
    start = dt.datetime.fromisoformat(meta["window_start"])  # 旧版留档：所有栏目同一时间窗
    days = [dt.date.fromisoformat(d) for d in meta["allowed_dates"]]
    return {s["name"]: (start, days) for s in cfg["sections"]}


# ---------- 历史（去重用） ----------
HISTORY = ROOT / "state" / "history.jsonl"


def load_history(days, today):
    out = []
    if not HISTORY.exists():
        return out
    cutoff = today - dt.timedelta(days=days)
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            h = json.loads(line)
            d = dt.date.fromisoformat(h["date"])
        except Exception:
            continue
        if cutoff <= d < today:
            out.append(h)
    return out


def save_history(today, selected):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    keep = []
    if HISTORY.exists():
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("date") != today.isoformat():
                    keep.append(line)
            except Exception:
                pass
    for x in selected:
        keep.append(json.dumps({"date": today.isoformat(), "section": x["section"],
                                "title": x["title_zh"], "url": x["url"]}, ensure_ascii=False))
    HISTORY.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")


# ---------- 提示词与输出结构 ----------
def build_prompt(cfg, now, windows, history, tz_label):
    sec_lines = []
    for s in cfg["sections"]:
        start, days = windows[s["name"]]
        hours = s.get("window_hours") or cfg["window_hours"]
        span = f"近 {hours // 24} 天" if hours >= 48 else f"近 {hours} 小时"
        sec_lines += [f"### {s['name']}（最多 {s['max_items']} 条）",
                      f"时间窗：{start:%Y-%m-%d %H:%M} 之后发布（{span}），允许的发布日期 "
                      + ("、".join(d.isoformat() for d in days) if len(days) <= 3
                         else f"{days[0]} 至 {days[-1]}"),
                      f"关注：{s['focus']}"]
        if s.get("tags"):
            sec_lines.append("细分（tag 取值）：" + " / ".join(s["tags"]))
        if s.get("writing_hint"):
            sec_lines.append("写作补充：" + s["writing_hint"])
        sec_lines += ["关键词：" + "、".join(s["keywords"]), ""]
    recent = [f"- [{h['date']}] {h['title']} | {h['url']}" for h in history[-120:]] or ["（无）"]
    rep = {
        "BRIEF_NAME": cfg["brief_name"],
        "NOW": f"{now:%Y-%m-%d %H:%M}",
        "WEEKDAY": "周" + WEEKDAY[now.weekday()],
        "SECTIONS": "\n".join(sec_lines).strip(),
        "DEDUPE_DAYS": str(cfg["dedupe_days"]),
        "RECENT": "\n".join(recent),
        "MIN_SEARCHES": str(cfg["min_searches_per_section"]),
        "MIN_IMPORTANCE": str(cfg["min_importance"]),
        "MAX_TOTAL": str(cfg["max_total_items"]),
        "MAX_TOOL_CALLS": str(cfg["max_tool_calls"]),
    }
    text = (ROOT / "prompt_template.md").read_text(encoding="utf-8")
    for k, v in rep.items():
        text = text.replace("{{" + k + "}}", v)
    return text.replace("北京时间", tz_label)


def build_schema(cfg):
    names = [s["name"] for s in cfg["sections"]]
    s = lambda d="": {"type": "string", "description": d} if d else {"type": "string"}  # noqa: E731
    item = {
        "type": "object",
        "properties": {
            "section": {"type": "string", "enum": names},
            "tag": s("栏目细分，取栏目说明里列出的值；没有细分填空字符串"),
            "title_zh": {"type": "string", "description": "中文标题，≤28字", "maxLength": 36},
            "summary_zh": {"type": "string", "description": "最多2句，≤100字", "maxLength": 130},
            "why_it_matters": {"type": "string", "description": "一句话看点，≤40字", "maxLength": 60},
            "url": s("主链接，必须是本次检索/打开中真实出现过的原文链接"),
            "source_name": s("来源名称"),
            "source_level": {"type": "string", "enum": LEVELS},
            "published_date": s("YYYY-MM-DD"),
            "date_evidence": s("日期出处，如：页面标注 / 搜索结果显示 / 网址中的日期"),
            "verification": {"type": "string", "enum": ["已打开核实", "仅搜索结果"]},
            "corroborating_urls": {"type": "array", "items": {"type": "string"}},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
            "matched_keywords": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["section", "tag", "title_zh", "summary_zh", "why_it_matters", "url", "source_name",
                     "source_level", "published_date", "date_evidence", "verification",
                     "corroborating_urls", "importance", "matched_keywords"],
    }
    return {
        "type": "object",
        "properties": {
            "highlights": {"type": "array", "items": {
                "type": "object",
                "properties": {"text": s("≤40字"), "item_index": {"type": "integer"}},
                "required": ["text", "item_index"]}},
            "items": {"type": "array", "items": item},
            "rejected": {"type": "array", "items": {
                "type": "object",
                "properties": {"title": s(), "url": s(), "reason": s("剔除原因")},
                "required": ["title", "url", "reason"]}},
            "search_notes": s("本次检索过程的简短说明"),
        },
        "required": ["highlights", "items", "rejected", "search_notes"],
    }


# ---------- 调用 Claude（无头） ----------
def parse_json_text(text):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text or "", re.S)
    cand = m.group(1) if m else (text or "")[(text or "").find("{"):(text or "").rfind("}") + 1]
    return json.loads(cand)


def run_claude(cfg, secrets, prompt, schema, run_dir, raw_dir, log):
    ccfg = cfg.get("claude", {})
    # 钩子命令要同时能被 PowerShell 和 bash 解析：程序路径不加引号（PowerShell 不认「"程序" 参数」这种写法）
    py = console_python().as_posix()
    py_cmd = py if " " not in py else f'& "{py}"'
    hook = (ROOT / "hooks" / "log_tool.py").as_posix()
    entry = lambda ev: [{"matcher": "WebSearch|WebFetch",  # noqa: E731
                         "hooks": [{"type": "command", "command": f'{py_cmd} "{hook}" {ev}', "timeout": 30}]}]
    settings = {"hooks": {"PreToolUse": entry("pre"), "PostToolUse": entry("post"),
                          "PostToolUseFailure": entry("fail")},
                "permissions": {"allow": ["WebSearch", "WebFetch"]}}
    settings_path = run_dir / "claude_settings.json"
    write_json(settings_path, settings)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    cmd = [find_claude(cfg), "-p", "--output-format", "json",
           "--json-schema", json.dumps(schema, ensure_ascii=False),
           "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
           "--permission-mode", "dontAsk", "--permission-prompts", "none",
           "--setting-sources", "project", "--settings", str(settings_path), "--strict-mcp-config",
           "--max-turns", str(ccfg.get("max_turns", 160))]
    if ccfg.get("model"):
        cmd += ["--model", ccfg["model"]]
    if ccfg.get("effort"):
        cmd += ["--effort", ccfg["effort"]]

    env = os.environ.copy()
    env["BRIEF_RAW_DIR"] = str(raw_dir)
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"  # 不加载 claude.ai 账号里的连接器
    if secrets.get("claude_oauth_token"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = secrets["claude_oauth_token"]

    log("启动 Claude 检索（只开放网页检索与打开网页两个工具）…")
    t0 = time.time()
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(ROOT), env=env,
                          timeout=int(ccfg.get("timeout_minutes", 45)) * 60,
                          creationflags=CREATE_NO_WINDOW)
    (run_dir / "claude_output.json").write_text(proc.stdout or "", encoding="utf-8")
    if proc.stderr:
        (run_dir / "claude_stderr.txt").write_text(proc.stderr, encoding="utf-8")
    log(f"Claude 结束，用时 {int(time.time() - t0)} 秒，退出码 {proc.returncode}")

    out = None
    for chunk in [proc.stdout] + list(reversed((proc.stdout or "").splitlines())):
        try:
            out = json.loads(chunk)
            break
        except Exception:
            continue
    if not isinstance(out, dict):
        raise RuntimeError(f"Claude 没有返回可解析的结果：{(proc.stderr or proc.stdout or '')[-400:]}")
    sid = out.get("session_id")
    if sid:  # 把完整会话记录也拷进留档
        for t in glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{sid}.jsonl")):
            shutil.copy(t, run_dir / "会话全文.jsonl")
            break
    using_token = bool(secrets.get("claude_oauth_token"))
    if out.get("is_error") or proc.returncode != 0:
        reason = str(out.get("result") or out.get("terminal_reason"))
        if using_token and any(w in reason.lower() for w in AUTH_WORDS):
            record_token_state(False, reason)
        raise RuntimeError(f"Claude 运行失败：{reason}")
    if using_token:
        record_token_state(True)
    data = out.get("structured_output")
    if not data:
        data = parse_json_text(out.get("result", ""))
    log(f"模型返回：候选 {len(data.get('items', []))} 条，自行剔除 {len(data.get('rejected', []))} 条；"
        f"轮次 {out.get('num_turns')}，费用折算 ${out.get('total_cost_usd', 0):.2f}")
    return data


# ---------- 代码核验 ----------
def bucket_of(reason):
    r = reason or ""
    rules = [("旧闻", ("旧", "过时", "早于", "时间窗", "过期")),
             ("跳转页", ("跳转", "聚合", "首页", "列表", "导航", "搜索结果", "标签", "短链")),
             ("重复/已报", ("重复", "已推送", "已报", "同一事件", "已收录", "已并入", "已改用", "并入")),
             ("价值不足", ("软文", "广告", "推广", "营销", "质量", "价值", "标题党", "不相关", "相关性弱",
                       "水文", "重要性", "鸡汤", "不计入", "不属于")),
             ("无法核实", ("核实", "编造", "无日期", "日期", "打不开", "无法访问", "信源", "印证", "传闻"))]
    for b, kws in rules:
        if any(k in r for k in kws):
            return b
    return "其他"


def similar(a, b):
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def validate(data, cfg, entries, history, windows):
    idxp = loglib.url_index(entries)
    hist_keys = {loglib.url_key(h["url"]) for h in history}
    blocked = [p.lower() for p in cfg.get("blocked_url_patterns", [])]
    secs = {s["name"]: s for s in cfg["sections"]}
    names = list(secs)
    passed, rejected = [], []

    def present(hits):
        return any(k in ("检索结果", "打开成功", "页面内链接") for _, k in hits)

    def is_jump(u):
        try:
            p = urlsplit(u)
        except ValueError:
            return True
        return any(b in u.lower() for b in blocked) or (p.path in ("", "/") and not p.query)

    def reject(c, bucket, reason):
        c["status"], c["bucket"], c["reason"], c["by"] = "剔除", bucket, reason, "代码核验"
        rejected.append(c)

    for i, it in enumerate(data.get("items", [])):
        c = dict(it)
        c["model_index"] = i
        url = (c.get("url") or "").strip()
        c["url"] = url
        hits = loglib.lookup(idxp, url) if url else []
        c["evidence"] = [f"#{s}{k}" for s, k in hits]
        c["opened_ok"] = any(k == "打开成功" for _, k in hits)
        corr = [u for u in (c.get("corroborating_urls") or [])
                if u.startswith("http") and not is_jump(u) and present(loglib.lookup(idxp, u))]
        c["corroborating_urls_valid"] = corr
        sec = secs.get(c.get("section"))
        if sec is not None and c.get("tag") not in (sec.get("tags") or []):
            c["tag"] = ""
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", c.get("published_date") or "")
        pub = None
        if m:
            try:
                pub = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pub = None
        days = windows[sec["name"]][1] if sec else []
        exempt = sec is not None and c.get("tag") in (sec.get("corroboration_exempt_tags") or [])

        if sec is None:
            reject(c, "其他", f"栏目不存在：{c.get('section')}")
        elif not url.startswith("http"):
            reject(c, "其他", "链接格式不对")
        elif is_jump(url):
            reject(c, "跳转页", "命中跳转/聚合页规则，或是首页")
        elif not present(hits):
            reject(c, "无法核实", "链接未出现在本次检索记录中（疑似编造）")
        elif pub is None:
            reject(c, "无法核实", "没有可识别的发布日期")
        elif pub < min(days):
            reject(c, "旧闻", f"发布于 {pub}，早于本栏目时间窗")
        elif pub > max(days):
            reject(c, "其他", f"发布日期 {pub} 晚于今天，日期异常")
        elif loglib.url_key(url) in hist_keys:
            reject(c, "重复/已报", "近期已推送过同一链接")
        elif any(similar(c.get("title_zh"), h["title"]) >= 0.8 for h in history):
            reject(c, "重复/已报", "近期已推送过相似标题")
        elif not c["opened_ok"] and not corr:
            reject(c, "无法核实", "原文未能打开，且没有其他来源印证")
        elif c.get("source_level") == "自媒体/社区" and not corr and not exempt:
            reject(c, "无法核实", "自媒体/社区来源的消息缺少独立印证")
        elif int(c.get("importance") or 0) < cfg["min_importance"]:
            reject(c, "价值不足", f"重要性 {c.get('importance')} 分，低于 {cfg['min_importance']} 分")
        else:
            c["published_date"] = pub.isoformat()
            passed.append(c)

    # 本期内去重：同链接或同栏目相似标题，保留重要性高的
    passed.sort(key=lambda x: (-int(x.get("importance") or 0), x["model_index"]))
    kept = []
    for c in passed:
        dup = next((k for k in kept if loglib.url_key(k["url"]) == loglib.url_key(c["url"]) or
                    (k["section"] == c["section"] and similar(k["title_zh"], c["title_zh"]) >= 0.8)), None)
        if dup:
            reject(c, "重复/已报", f"与本期「{dup['title_zh']}」重复")
        else:
            kept.append(c)

    # 栏目上限与总数上限
    selected, per = [], {}
    for c in kept:
        per[c["section"]] = per.get(c["section"], 0) + 1
        if per[c["section"]] > secs[c["section"]]["max_items"]:
            reject(c, "价值不足", "超出栏目条数上限")
        elif len(selected) >= cfg["max_total_items"]:
            reject(c, "价值不足", "超出总条数上限")
        else:
            c["status"] = "入选"
            selected.append(c)
    selected.sort(key=lambda x: (names.index(x["section"]), -int(x.get("importance") or 0), x["model_index"]))

    for r in data.get("rejected", []):
        rejected.append({"title_zh": r.get("title"), "url": r.get("url"), "status": "剔除",
                         "bucket": bucket_of(r.get("reason")), "reason": r.get("reason"), "by": "模型初筛"})

    alive = {c["model_index"] for c in selected}
    highlights = [h["text"] for h in data.get("highlights", []) if h.get("item_index") in alive]
    return selected, rejected, highlights


# ---------- 成稿 ----------
def reject_counts(rejected):
    counts = {b: 0 for b in BUCKETS}
    for r in rejected:
        counts[r.get("bucket", "其他")] = counts.get(r.get("bucket", "其他"), 0) + 1
    return counts


def window_note(cfg, now, windows, tz_label):
    """按本次实际使用的时间窗写说明（重新成稿旧运行时也准确）。"""
    base_start = now - dt.timedelta(hours=cfg["window_hours"])
    note = f"收录 {base_start:%m-%d %H:%M} 至 {now:%m-%d %H:%M}（{tz_label}）发布的内容"
    wider = []
    for name, (start, _) in windows.items():
        hours = round((now - start).total_seconds() / 3600)
        if hours != cfg["window_hours"]:
            wider.append(f"「{name}」放宽到近 {hours // 24} 天" if hours >= 48 else f"「{name}」为近 {hours} 小时")
    return note + ("；" + "，".join(wider) if wider else "")


def stats_line(st, selected, rejected):
    detail = " / ".join(f"{k} {v}" for k, v in reject_counts(rejected).items() if v)
    return (f"本期检索 {st['search']} 次、打开网页 {st['fetch']} 次；候选 {len(selected) + len(rejected)} 条，"
            f"入选 {len(selected)} 条，剔除 {len(rejected)} 条" + (f"（{detail}）" if detail else "") + "。")


def render_md(cfg, ctx, archive):
    now = ctx["now"]
    L = [f"# {cfg['brief_name']}｜{now:%Y-%m-%d}（周{WEEKDAY[now.weekday()]}）", "",
         f"> {ctx['window_note']}，精选 {len(ctx['selected'])} 条", ""]
    if ctx["highlights"]:
        L += ["## 今日要点", ""] + [f"- {h}" for h in ctx["highlights"]] + [""]
    n = 0
    for s in cfg["sections"]:
        its = [x for x in ctx["selected"] if x["section"] == s["name"]]
        L += [f"## {s.get('icon', '')} {s['name']}".replace("##  ", "## "), ""]
        if not its:
            L += ["今日没有达到标准的内容。", ""]
            continue
        for x in its:
            n += 1
            tag = f"【{x['tag']}】" if x.get("tag") else ""
            L += [f"**{n}. {tag}{x['title_zh']}**", "", x["summary_zh"], ""]
            if x.get("why_it_matters"):
                L += [f"> 看点：{x['why_it_matters']}", ""]
            L.append(f"{x.get('source_name', '')} · {x['published_date'][5:]} · [阅读原文]({x['url']})")
            if archive:
                corr = x.get("corroborating_urls_valid") or []
                also = ("；另见 " + " ".join(f"[{i + 1}]({u})" for i, u in enumerate(corr))) if corr else ""
                L += ["", f"<sub>溯源：检索日志 {'、'.join(x['evidence'][:6]) or '无'}｜{x.get('source_level', '')}｜"
                          f"{'已打开原文核实' if x['opened_ok'] else '原文未能打开，依据印证来源'}｜"
                          f"日期出处：{x.get('date_evidence', '')}{also}</sub>"]
            L.append("")
    L += ["---", "", stats_line(ctx["st"], ctx["selected"], ctx["rejected"])]
    if not archive:
        L.append("每条原文链接都能在当日检索日志中查到出处。")
        return "\n".join(L) + "\n"
    L += [f"原始检索日志：{ctx['run_rel']}/检索日志.md（可读版）、{ctx['run_rel']}/raw/（逐条原始留档）", "",
          "## 剔除清单", "", "| 原因 | 判定方 | 标题 | 说明 |", "|---|---|---|---|"]
    for r in ctx["rejected"]:
        t = str(r.get("title_zh") or "").replace("|", "\\|")
        link = f"[{t or '链接'}]({r['url']})" if r.get("url") else t
        L.append(f"| {r.get('bucket')} | {r.get('by')} | {link} | {str(r.get('reason') or '').replace('|', '/')} |")
    return "\n".join(L) + "\n"


# 只用行内样式：同一段 HTML 既能本地双击打开，也能交给推送服务渲染
_C = {"ink": "#1f2328", "sub": "#57606a", "mute": "#8c959f", "line": "#e4e7eb", "card": "#ffffff",
      "bg": "#f5f6f8", "accent": "#1a5fd0", "tip_bg": "#f2f6fd", "tip_ink": "#24497f",
      "hl_bg": "#fff8e6", "hl_line": "#f0c35a", "tag_bg": "#eef1f5", "tag_ink": "#3d4b5c"}


def render_html(cfg, ctx, archive, full_doc):
    e = lambda s: html.escape(str(s or ""))  # noqa: E731
    now = ctx["now"]
    P = []
    P.append(f'<div style="max-width:680px;margin:0 auto;padding:16px 14px 28px;background:{_C["bg"]};'
             f'font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
             f'color:{_C["ink"]};line-height:1.65;font-size:15px;">')
    P.append(f'<div style="padding:6px 4px 14px;"><div style="font-size:24px;font-weight:700;letter-spacing:.5px;">'
             f'{e(cfg["brief_name"])}</div><div style="color:{_C["sub"]};font-size:14px;margin-top:2px;">'
             f'{now:%Y-%m-%d}　周{WEEKDAY[now.weekday()]}　精选 {len(ctx["selected"])} 条</div>'
             f'<div style="color:{_C["mute"]};font-size:12px;margin-top:4px;">{e(ctx["window_note"])}</div></div>')
    if ctx["highlights"]:
        P.append(f'<div style="background:{_C["hl_bg"]};border-left:4px solid {_C["hl_line"]};border-radius:8px;'
                 f'padding:10px 14px;margin:0 0 18px;"><div style="font-weight:700;margin-bottom:4px;">今日要点</div>')
        P += [f'<div style="margin:3px 0;">• {e(h)}</div>' for h in ctx["highlights"]]
        P.append('</div>')
    n = 0
    for s in cfg["sections"]:
        its = [x for x in ctx["selected"] if x["section"] == s["name"]]
        P.append(f'<div style="font-size:18px;font-weight:700;margin:22px 4px 10px;">{e(s.get("icon", ""))} '
                 f'{e(s["name"])} <span style="font-size:13px;font-weight:400;color:{_C["mute"]};">{len(its)} 条</span></div>')
        if not its:
            P.append(f'<div style="color:{_C["mute"]};font-size:14px;margin:0 4px 8px;">今日没有达到标准的内容。</div>')
            continue
        for x in its:
            n += 1
            tag = (f'<span style="display:inline-block;background:{_C["tag_bg"]};color:{_C["tag_ink"]};font-size:12px;'
                   f'padding:1px 8px;border-radius:10px;margin-right:6px;vertical-align:2px;">{e(x["tag"])}</span>'
                   if x.get("tag") else "")
            P.append(f'<div style="background:{_C["card"]};border:1px solid {_C["line"]};border-radius:10px;'
                     f'padding:13px 15px;margin:0 0 10px;">')
            P.append(f'<div style="font-size:16px;font-weight:700;line-height:1.5;margin-bottom:6px;">{tag}'
                     f'{n}. {e(x["title_zh"])}</div>')
            P.append(f'<div style="color:#333;">{e(x["summary_zh"])}</div>')
            if x.get("why_it_matters"):
                P.append(f'<div style="background:{_C["tip_bg"]};color:{_C["tip_ink"]};border-radius:6px;'
                         f'padding:6px 10px;margin-top:8px;font-size:14px;"><b>看点</b>　{e(x["why_it_matters"])}</div>')
            P.append(f'<div style="color:{_C["mute"]};font-size:13px;margin-top:8px;">{e(x.get("source_name"))} · '
                     f'{e(x["published_date"][5:])} · <a href="{e(x["url"])}" style="color:{_C["accent"]};'
                     f'text-decoration:none;">阅读原文 ›</a></div>')
            if archive:
                corr = x.get("corroborating_urls_valid") or []
                also = (" · 另见 " + " ".join(f'<a href="{e(u)}" style="color:{_C["accent"]};">[{i + 1}]</a>'
                                               for i, u in enumerate(corr))) if corr else ""
                P.append(f'<div style="color:{_C["mute"]};font-size:12px;margin-top:6px;border-top:1px dashed '
                         f'{_C["line"]};padding-top:6px;">溯源：检索日志 {e("、".join(x["evidence"][:6]) or "无")} · '
                         f'{e(x.get("source_level"))} · {"已打开原文核实" if x["opened_ok"] else "原文未能打开，依据印证来源"}'
                         f' · 日期出处：{e(x.get("date_evidence"))}{also}</div>')
            P.append('</div>')
    P.append(f'<div style="color:{_C["mute"]};font-size:12px;margin:20px 4px 0;border-top:1px solid {_C["line"]};'
             f'padding-top:10px;">{e(stats_line(ctx["st"], ctx["selected"], ctx["rejected"]))}'
             f'{"" if archive else "每条原文链接都能在当日检索日志中查到出处。"}</div>')
    if archive and ctx["rejected"]:
        P.append(f'<details style="margin:14px 4px 0;font-size:13px;"><summary style="cursor:pointer;color:{_C["sub"]};">'
                 f'剔除清单（{len(ctx["rejected"])} 条）</summary><table style="width:100%;border-collapse:collapse;'
                 f'margin-top:8px;background:{_C["card"]};">')
        for r in ctx["rejected"]:
            t = e(r.get("title_zh") or "链接")
            link = f'<a href="{e(r["url"])}" style="color:{_C["accent"]};">{t}</a>' if r.get("url") else t
            P.append(f'<tr><td style="border:1px solid {_C["line"]};padding:5px 7px;white-space:nowrap;">{e(r.get("bucket"))}</td>'
                     f'<td style="border:1px solid {_C["line"]};padding:5px 7px;">{link}<div style="color:{_C["mute"]};">'
                     f'{e(r.get("reason"))}（{e(r.get("by"))}）</div></td></tr>')
        P.append('</table></details>')
    P.append('</div>')
    body = "\n".join(P)
    if not full_doc:
        return body
    title = f"{cfg['brief_name']} {now:%Y-%m-%d}"
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title></head>'
            f'<body style="margin:0;background:{_C["bg"]};">{body}</body></html>\n')


# ---------- 推送 ----------
def wait_until(hhmm, tz, log):
    h, m = map(int, hhmm.split(":"))
    now = dt.datetime.now(tz)
    secs = (now.replace(hour=h, minute=m, second=0, microsecond=0) - now).total_seconds()
    if 0 < secs < 3 * 3600:
        log(f"等到 {hhmm} 再推送（约 {int(secs // 60)} 分钟）")
        time.sleep(secs)


def notify_desktop(title, body, log=None):
    """在 Windows 右下角弹一条系统通知（推送失败时，推送通道本身靠不住，只能靠这个提醒）。"""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        "$x.Item(0).AppendChild($t.CreateTextNode($env:AIZZD_TITLE)) > $null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:AIZZD_BODY)) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'"
        ").Show([Windows.UI.Notifications.ToastNotification]::new($t))")
    env = os.environ.copy()
    env["AIZZD_TITLE"], env["AIZZD_BODY"] = title, body[:250]
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                           env=env, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        if log and p.returncode != 0:
            log("桌面通知没弹出来：" + (p.stderr or b"").decode("utf-8", "replace")[-200:])
    except Exception as exc:
        if log:
            log(f"桌面通知没弹出来：{exc!r}")


def do_push(cfg, secrets, title, md, html_body, run_dir, log):
    pcfg = cfg.get("push", {})
    chans = pcfg.get("channels", [])
    if not chans:
        log("未配置推送通道（控制台「推送与定时」页），本次只在本地生成")
        return []
    res = push.send_all(chans, secrets, title, md, html_body, pcfg.get("format", "markdown"))
    write_json(run_dir / "推送结果.json", {"time": dt.datetime.now().astimezone().isoformat(), "results": res})
    for r in res:
        name = push.CHANNEL_NAMES.get(r["channel"], r["channel"])
        log(f"推送 {name}：{'成功 ' + (r.get('note') or '') if r['ok'] else '失败 ' + str(r.get('detail') or r.get('response'))}")
    failed = [r for r in res if not r["ok"]]
    if failed:
        names = "、".join(push.CHANNEL_NAMES.get(r["channel"], r["channel"]) for r in failed)
        first = str(failed[0].get("detail") or failed[0].get("response") or "")
        notify_desktop(f"{cfg['brief_name']}：{names}推送失败",
                       f"{first[:150]}　打开 {cfg['brief_name']} →「推送与定时」处理。", log)
    return res


def notify_failure(cfg, secrets, now, reason, run_dir, log):
    notify_desktop(f"{cfg['brief_name']}：今天的早报生成失败", f"{reason[:150]}　打开 {cfg['brief_name']} 查看原因。", log)
    hint = ""
    low = reason.lower()
    if any(w in low for w in AUTH_WORDS):
        hint = ("\n\n**处理**：Claude 长期令牌失效或过期。打开 AI早知道 →「环境检查」→「重新生成令牌」。"
                if secrets.get("claude_oauth_token") else
                "\n\n**处理**：Claude 登录已过期。打开 AI早知道 →「环境检查」→「设置长期令牌」，以后就不用再登录。")
    md = (f"# {cfg['brief_name']} 生成失败｜{now:%Y-%m-%d}\n\n原因：{reason[:500]}{hint}\n\n"
          f"运行日志：{run_dir.relative_to(ROOT).as_posix()}/run.log")
    do_push(cfg, secrets, f"{cfg['brief_name']} 生成失败 {now:%m-%d}", md, None, run_dir, log)


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description="AI早知道")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--refinalize", metavar="运行目录")
    ap.add_argument("--push-only", nargs="?", const="today", metavar="日期")
    ap.add_argument("--by", default="命令行", help="谁发起的：定时任务 / 控制台 / 命令行")
    args = ap.parse_args()

    cfg = load_json(ROOT / "config.json")
    secrets = load_json(ROOT / "secrets.json", {}) or {}
    tz, tz_label = tz_of(cfg)
    loglib.DISPLAY_TZ = tz
    push.SENDER_NAME = cfg.get("brief_name") or push.SENDER_NAME

    if args.push_only:
        day = dt.datetime.now(tz).date().isoformat() if args.push_only == "today" else args.push_only
        runs = sorted(glob.glob(str(ROOT / "logs" / day / "run_*" / "早报_推送版.md")))
        if not runs:
            print(f"{day} 没有已生成的早报")
            return 1
        run_dir = Path(runs[-1]).parent
        log = RunLog(run_dir / "run.log", tz)
        md = Path(runs[-1]).read_text(encoding="utf-8")
        hp = run_dir / "早报_推送版.html"
        html_body = hp.read_text(encoding="utf-8") if hp.exists() else None
        title = md.splitlines()[0].lstrip("# ").replace("｜", " ")
        res = do_push(cfg, secrets, title, md, html_body, run_dir, log)
        summ = load_json(run_dir / "summary.json", {}) or {}
        summ["pushed"] = res
        write_json(run_dir / "summary.json", summ)
        return 0 if res and all(r["ok"] for r in res) else 1

    other = running_info()
    if other and not args.refinalize:
        print(f"已有一次运行在进行中（{other.get('run_dir')}），本次不再启动")
        return 3

    if args.refinalize:
        run_dir = Path(args.refinalize).resolve()
        meta = load_json(run_dir / "meta.json")
        now = dt.datetime.fromisoformat(meta["now"])
        windows = windows_from_meta(meta, cfg)
        log = RunLog(run_dir / "run.log", tz)
        log("—— 重新核验成稿（不重新检索）——")
        data = load_json(run_dir / "模型结果.json")
        args.by = meta.get("by") or args.by  # 保留原来是谁发起的
    else:
        now = dt.datetime.now(tz).replace(second=0, microsecond=0)
        windows = section_windows(cfg, now)
        run_dir = ROOT / "logs" / f"{now:%Y-%m-%d}" / f"run_{dt.datetime.now(tz):%H%M%S}"
        (run_dir / "raw").mkdir(parents=True, exist_ok=True)
        log = RunLog(run_dir / "run.log", tz)
        write_json(run_dir / "meta.json", {"now": now.isoformat(), "windows": windows_to_meta(windows),
                                           "by": args.by})
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        write_json(LOCK, {"pid": os.getpid(), "run_dir": run_dir.relative_to(ROOT).as_posix(),
                          "started": dt.datetime.now(tz).isoformat()})
        log("开始：" + window_note(cfg, now, windows, tz_label))

    prev = (load_json(run_dir / "summary.json", {}) or {}) if args.refinalize else {}
    summary = {"date": f"{now:%Y-%m-%d}", "run": run_dir.name,
               "started": prev.get("started") or dt.datetime.now(tz).isoformat(), "ok": False, "by": args.by}
    if prev.get("pushed") is not None:
        summary["pushed"] = prev["pushed"]
    try:
        history = load_history(cfg["dedupe_days"], now.date())
        if not args.refinalize:
            prompt = build_prompt(cfg, now, windows, history, tz_label)
            data = run_claude(cfg, secrets, prompt, build_schema(cfg), run_dir, run_dir / "raw", log)
            write_json(run_dir / "模型结果.json", data)

        run_rel = run_dir.relative_to(ROOT).as_posix()
        entries = loglib.write_merged(run_dir / "raw", run_dir, f"原始检索日志 · {run_rel}")
        st = loglib.stats(entries)
        log(f"检索日志：共 {st['total']} 次调用（检索 {st['search']}，打开 {st['fetch']}，未成功 {st['failed']}）")
        if st["total"] == 0:
            log("警告：没有留下任何检索记录（留档钩子可能没生效），所有条目都会因无法核实被剔除")

        selected, rejected, highlights = validate(data, cfg, entries, history, windows)
        write_json(run_dir / "候选与判定.json", {"selected": selected, "rejected": rejected,
                                             "highlights": highlights, "search_notes": data.get("search_notes")})
        log(f"核验后入选 {len(selected)} 条，剔除 {len(rejected)} 条")

        ctx = {"now": now, "window_note": window_note(cfg, now, windows, tz_label), "selected": selected,
               "rejected": rejected, "highlights": highlights, "st": st, "run_rel": run_rel}
        push_md = render_md(cfg, ctx, False)
        push_html = render_html(cfg, ctx, False, False)
        (run_dir / "早报_推送版.md").write_text(push_md, encoding="utf-8")
        (run_dir / "早报_推送版.html").write_text(push_html, encoding="utf-8")
        (ROOT / "briefs").mkdir(exist_ok=True)
        (ROOT / "briefs" / f"{now:%Y-%m-%d}.md").write_text(render_md(cfg, ctx, True), encoding="utf-8")
        (ROOT / "briefs" / f"{now:%Y-%m-%d}.html").write_text(render_html(cfg, ctx, True, True), encoding="utf-8")
        save_history(now.date(), selected)
        summary.update({"ok": True, "selected": len(selected), "rejected": len(rejected), "stats": st,
                        "reject_counts": reject_counts(rejected), "finished": dt.datetime.now(tz).isoformat()})
        write_json(run_dir / "summary.json", summary)
        log(f"早报已生成：briefs/{now:%Y-%m-%d}.html（及同名 .md）")
    except Exception as exc:
        log("失败：" + "".join(traceback.format_exception_only(type(exc), exc)).strip())
        (run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        summary.update({"error": str(exc)[:500], "finished": dt.datetime.now(tz).isoformat()})
        write_json(run_dir / "summary.json", summary)
        if not args.no_push and not args.refinalize:
            notify_failure(cfg, secrets, now, str(exc), run_dir, log)
        return 1
    finally:
        if not args.refinalize and LOCK.exists():
            try:
                if (load_json(LOCK) or {}).get("pid") == os.getpid():
                    LOCK.unlink()
            except Exception:
                pass

    if args.no_push or args.refinalize:
        return 0
    if not args.no_wait:
        wait_until(cfg["schedule"]["push_time"], tz, log)
    title = f"{cfg['brief_name']} {now:%Y-%m-%d}（周{WEEKDAY[now.weekday()]}）"
    res = do_push(cfg, secrets, title, push_md, push_html, run_dir, log)
    summary["pushed"] = res
    write_json(run_dir / "summary.json", summary)
    return 0 if (not res or all(r["ok"] for r in res)) else 2


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
