# -*- coding: utf-8 -*-
"""AI早知道 控制台 —— 本机界面的后台，双击 AI早知道.exe 启动（独立窗口打开 http://127.0.0.1:8765）

只监听本机地址，不对局域网开放。页签：
    早报          按日期浏览历史早报（与推送内容一致，另带溯源与剔除清单），一键推送
    检索日志      逐条查看每次检索 / 打开网页的原始记录
    回查          输入链接或关键词，查它出自哪次检索、最后是否入选
    栏目与关键词  改栏目、关键词、时间窗、条数等
    推送与定时    选微信通道、填密钥、发测试、装/停每日定时任务
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import loglib  # noqa: E402
import push  # noqa: E402
import run_brief  # noqa: E402

APP_ID = "ai-brief-console"
PORTS = range(8765, 8776)
NOWIN = 0x08000000
TASK_NAME = "AI早知道"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_RE = re.compile(r"^run_\d{6}$")
WEEK = "一二三四五六日"


def cfg():
    return run_brief.load_json(ROOT / "config.json")


def secrets():
    return run_brief.load_json(ROOT / "secrets.json", {}) or {}


def tz():
    return run_brief.tz_of(cfg())[0]


def console_log(msg):
    (ROOT / "logs").mkdir(exist_ok=True)
    with open(ROOT / "logs" / "console.log", "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def ps(cmd, timeout=60):
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + cmd
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", full],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NOWIN)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


# ---------- 状态（带缓存，PowerShell / claude 命令都比较慢） ----------
_cache = {}
_refreshing = set()


def cached(key, ttl, fn):
    """过期或没有缓存时在后台刷新，先返回旧值（首次返回「检查中」），页面不用等慢命令。"""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    if key not in _refreshing:
        _refreshing.add(key)

        def work():
            try:
                _cache[key] = (time.time(), fn())
            finally:
                _refreshing.discard(key)
        threading.Thread(target=work, daemon=True).start()
    return hit[1] if hit else {"pending": True}


def fresh(key, fn):
    """立刻同步刷新（用户点了「重新检查」或刚改过定时任务）。"""
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _to_cfg_tz(s):
    if not s:
        return ""
    try:
        return dt.datetime.fromisoformat(s).astimezone(tz()).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s


def schedule_status(now=False):
    def get():
        cmd = (f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
               "if($t){$i=$t|Get-ScheduledTaskInfo;"
               "$n=if($i.NextRunTime){$i.NextRunTime.ToString('yyyy-MM-ddTHH:mm:sszzz')}else{''};"
               "$l=if($i.LastRunTime -and $i.LastRunTime.Year -gt 2000){$i.LastRunTime.ToString('yyyy-MM-ddTHH:mm:sszzz')}else{''};"
               "@{installed=$true;state=[string]$t.State;next=$n;last=$l;result=$i.LastTaskResult}|ConvertTo-Json}"
               "else{'{\"installed\":false}'}")
        try:
            code, out, err = ps(cmd, 30)
            d = json.loads(out or "{}")
        except Exception as exc:
            return {"installed": None, "error": str(exc)}
        if d.get("installed"):
            d["next"] = _to_cfg_tz(d.get("next"))
            d["last"] = _to_cfg_tz(d.get("last"))
            res = d.get("result")
            d["result_text"] = {0: "成功", 267011: "还没运行过", 267009: "正在运行"}.get(res, f"返回码 {res}")
        return d
    return fresh("schedule", get) if now else cached("schedule", 20, get)


def cli_status(now=False):
    def get():
        token = (secrets().get("claude_oauth_token") or "").strip()
        if token:  # 用长期令牌：状态以最近一次真实验证 / 运行的结果为准（auth status 不校验令牌）
            st = run_brief.load_json(run_brief.TOKEN_STATE, {}) or {}
            return {"mode": "token", "logged_in": st.get("ok"), "checked_at": st.get("time", ""),
                    "error": st.get("error", ""), "masked": push._mask(token)}
        try:
            exe = run_brief.find_claude(cfg())
            p = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=40, creationflags=NOWIN)
            d = json.loads(p.stdout or "{}")
            return {"mode": "login", "logged_in": bool(d.get("loggedIn")), "plan": d.get("subscriptionType", "")}
        except Exception as exc:
            return {"mode": "login", "logged_in": None, "error": str(exc)[:200]}
    return fresh("cli", get) if now else cached("cli", 300, get)


def token_action(body):
    """长期令牌：generate 打开生成窗口；save 保存并验证；verify 重新验证；clear 删除改回账号登录。"""
    action = body.get("action")
    if action == "generate":
        try:
            exe = run_brief.find_claude(cfg())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        open_terminal([exe, "setup-token"], "生成 Claude 长期令牌")
        return {"ok": True, "message": "已打开窗口：按提示在浏览器里授权，窗口里会显示一串 sk-ant- 开头的令牌，复制后粘贴到这里"}
    if action in ("save", "verify"):
        token = str(body.get("token") or "").strip() if action == "save" else (secrets().get("claude_oauth_token") or "")
        token = re.sub(r"\s+", "", token)
        if not token:
            return {"ok": False, "error": "没有令牌可验证" if action == "verify" else "请先粘贴令牌"}
        if action == "save" and not token.startswith("sk-ant-"):
            return {"ok": False, "error": "这看起来不像令牌：应该是 sk-ant- 开头的一长串字符"}
        ok, err = run_brief.verify_token(cfg(), token)
        if action == "save":
            if not ok:
                return {"ok": False, "error": "令牌没有通过验证，未保存：" + err}
            sec = secrets()
            sec["claude_oauth_token"] = token
            (ROOT / "secrets.json").write_text(json.dumps(sec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run_brief.record_token_state(ok, err)
        _cache.pop("cli", None)
        return {"ok": ok, "message": "令牌有效，已保存。之后每天的生成都用它，不再依赖账号登录" if ok and action == "save"
                else "令牌有效" if ok else "", "error": "" if ok else "令牌无效或已过期：" + err}
    if action == "clear":
        sec = secrets()
        sec.pop("claude_oauth_token", None)
        (ROOT / "secrets.json").write_text(json.dumps(sec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            run_brief.TOKEN_STATE.unlink()
        except FileNotFoundError:
            pass
        _cache.pop("cli", None)
        return {"ok": True, "message": "已删除令牌，改回使用账号登录"}
    return {"ok": False, "error": "未知操作"}


def run_dirs():
    out = []
    for day in sorted((ROOT / "logs").glob("20*-*-*"), reverse=True):
        if day.is_dir() and DATE_RE.match(day.name):
            for r in sorted(day.glob("run_*"), reverse=True):
                if RUN_RE.match(r.name):
                    out.append(r)
    return out


def run_info(r, running):
    s = run_brief.load_json(r / "summary.json", {}) or {}
    info = {"date": r.parent.name, "run": r.name, "ok": s.get("ok"), "selected": s.get("selected"),
            "rejected": s.get("rejected"), "stats": s.get("stats"), "reject_counts": s.get("reject_counts"),
            "by": s.get("by") or (run_brief.load_json(r / "meta.json", {}) or {}).get("by", ""),
            "error": s.get("error"), "started": s.get("started"), "finished": s.get("finished")}
    pushed = s.get("pushed")
    if pushed is None:
        p = run_brief.load_json(r / "推送结果.json")
        pushed = p.get("results") if isinstance(p, dict) else p
    info["pushed"] = None if not pushed else all(x.get("ok") for x in pushed)
    if running and running.get("run_dir") == r.relative_to(ROOT).as_posix():
        info["status"] = "running"
    elif s.get("ok"):
        info["status"] = "ok"
    elif s or (r / "error.txt").exists():
        info["status"] = "failed"
    else:
        info["status"] = "unknown"
    w = (run_brief.load_json(r / "meta.json", {}) or {}).get("now", "")
    if w:
        d = dt.datetime.fromisoformat(w)
        info["weekday"] = "周" + WEEK[d.weekday()]
        info["time"] = d.strftime("%H:%M")
    return info


def list_runs():
    running = run_brief.running_info()
    return [run_info(r, running) for r in run_dirs()]


def progress():
    running = run_brief.running_info()
    if not running:
        return None
    r = ROOT / running["run_dir"]
    pres = sorted((r / "raw").glob("*_pre_*.json"))
    recent = []
    n_search = n_fetch = 0
    for p in pres:
        try:
            pl = json.loads(p.read_text(encoding="utf-8")).get("payload", {})
        except Exception:
            continue
        t = pl.get("tool_name")
        if t == "WebSearch":
            n_search += 1
        elif t == "WebFetch":
            n_fetch += 1
        recent.append({"tool": loglib.TOOL_LABEL.get(t, t),
                       "what": (pl.get("tool_input") or {}).get("query") or (pl.get("tool_input") or {}).get("url", "")})
    log_tail = ""
    if (r / "run.log").exists():
        log_tail = "\n".join((r / "run.log").read_text(encoding="utf-8").splitlines()[-6:])
    return {"run_dir": running["run_dir"], "started": running.get("started", "")[11:16],
            "search": n_search, "fetch": n_fetch, "recent": recent[-6:][::-1], "log": log_tail}


def overview():
    c = cfg()
    tzinfo, label = run_brief.tz_of(c)
    runs = list_runs()
    sec = secrets()
    return {
        "brief_name": c["brief_name"], "tz_label": label,
        "now": dt.datetime.now(tzinfo).strftime("%Y-%m-%d %H:%M"),
        "run_time": c["schedule"]["run_time"], "push_time": c["schedule"]["push_time"],
        "latest": runs[0] if runs else None,
        "schedule": schedule_status(),
        "push": {"channels": c.get("push", {}).get("channels", []), "format": c.get("push", {}).get("format", "markdown"),
                 "filled": {ch: push.channel_ready(ch, sec) for ch in push.CHANNEL_FIELDS}},
        "cli": cli_status(),
        "progress": progress(),
        "local_tz": time.strftime("%z"),
    }


# ---------- 日志 ----------
def run_path(q):
    date, run = q.get("date", [""])[0], q.get("run", [""])[0]
    if not DATE_RE.match(date):
        raise ValueError("日期不对")
    if not run:
        rs = sorted((ROOT / "logs" / date).glob("run_*"))
        if not rs:
            raise ValueError(f"{date} 没有运行记录")
        return rs[-1]
    if not RUN_RE.match(run):
        raise ValueError("运行编号不对")
    return ROOT / "logs" / date / run


def entries_of(r):
    f = r / "检索日志.jsonl"
    if f.exists():
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return loglib.build_entries(loglib.load_raw(r / "raw"))


def short_input(e):
    inp = e.get("input") or {}
    return inp.get("query") or inp.get("url") or json.dumps(inp, ensure_ascii=False)


def log_list(q):
    r = run_path(q)
    es = entries_of(r)
    rows = []
    for e in es:
        if e["tool"] == "WebSearch":
            res = f"{len(loglib.extract_title_links(e.get('response')))} 条链接"
        elif isinstance(e.get("response"), dict):
            res = f"{e['response'].get('bytes', '?')} 字节"
        else:
            res = ""
        rows.append({"seq": e["seq"], "time": loglib._hm(e.get("started_at")), "tool": e["tool"],
                     "label": loglib.TOOL_LABEL.get(e["tool"], e["tool"]), "input": short_input(e),
                     "status": e["status"], "result": res})
    judged = run_brief.load_json(r / "候选与判定.json", {}) or {}
    return {"date": r.parent.name, "run": r.name, "stats": loglib.stats(es), "rows": rows,
            "runs": [p.name for p in sorted(r.parent.glob("run_*"), reverse=True)],
            "notes": judged.get("search_notes", "")}


def log_entry(q):
    r = run_path(q)
    seq = int(q.get("seq", ["0"])[0])
    e = next((x for x in entries_of(r) if x["seq"] == seq), None)
    if not e:
        raise ValueError(f"没有第 {seq} 条")
    raws = {}
    for fn in e.get("raw_files", []):
        if re.match(r"^[\w.-]+\.json$", fn):
            raws[fn] = (r / "raw" / fn).read_text(encoding="utf-8")
    links = loglib.extract_title_links(e.get("response")) if e["tool"] == "WebSearch" else []
    body = ""
    if e["tool"] == "WebFetch" and isinstance(e.get("response"), dict):
        body = str(e["response"].get("result") or "")
    def fmt(ts):
        try:
            return dt.datetime.fromisoformat(ts).astimezone(tz()).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return ts or ""
    return {"seq": seq, "tool": e["tool"], "label": loglib.TOOL_LABEL.get(e["tool"], e["tool"]),
            "input": e.get("input"), "status": e["status"], "started": fmt(e.get("started_at")),
            "finished": fmt(e.get("finished_at")), "tz_label": run_brief.tz_of(cfg())[1],
            "links": [{"title": t, "url": u} for t, u in links],
            "body": body, "error": e.get("error"), "raw": raws}


def lookup(q):
    text = (q.get("q", [""])[0] or "").strip()
    if not text:
        return {"results": []}
    is_url = text.startswith("http")
    key = loglib.url_key(text) if is_url else None
    out = []
    for r in run_dirs():
        es = entries_of(r)
        judged = run_brief.load_json(r / "候选与判定.json", {}) or {}
        allj = (judged.get("selected") or []) + (judged.get("rejected") or [])
        if is_url:
            hits = loglib.lookup(loglib.url_index(es), text)
            emap = {e["seq"]: e for e in es}
            hit_rows = [{"seq": s, "kind": k, "label": loglib.TOOL_LABEL.get(emap[s]["tool"], ""),
                         "input": short_input(emap[s])} for s, k in hits if s in emap]
            verdicts = [{"status": j.get("status"), "bucket": j.get("bucket", ""), "reason": j.get("reason", ""),
                         "title": j.get("title_zh", "")} for j in allj
                        if j.get("url") and loglib.url_key(j["url"]) == key]
        else:
            low = text.lower()
            hit_rows = [{"seq": e["seq"], "kind": e["status"], "label": loglib.TOOL_LABEL.get(e["tool"], ""),
                         "input": short_input(e)} for e in es
                        if low in json.dumps(e, ensure_ascii=False).lower()]
            verdicts = [{"status": j.get("status"), "bucket": j.get("bucket", ""), "reason": j.get("reason", ""),
                         "title": j.get("title_zh", ""), "url": j.get("url", "")} for j in allj
                        if low in json.dumps(j, ensure_ascii=False).lower()]
        if hit_rows or verdicts:
            out.append({"date": r.parent.name, "run": r.name, "hits": hit_rows[:50], "verdicts": verdicts[:20],
                        "more": max(0, len(hit_rows) - 50)})
    return {"query": text, "is_url": is_url, "results": out}


# ---------- 设置 ----------
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def save_config(new):
    old = cfg()
    errs = []
    secs = new.get("sections") or []
    if not secs:
        errs.append("至少要有一个栏目")
    names = set()
    for i, s in enumerate(secs, 1):
        n = str(s.get("name", "")).strip()
        if not n:
            errs.append(f"第 {i} 个栏目没有名称")
        if n in names:
            errs.append(f"栏目名重复：{n}")
        names.add(n)
        try:
            if not 1 <= int(s.get("max_items")) <= 20:
                errs.append(f"「{n}」条数上限要在 1–20 之间")
            if not 1 <= int(s.get("window_hours")) <= 24 * 31:
                errs.append(f"「{n}」时间窗不对")
        except (TypeError, ValueError):
            errs.append(f"「{n}」的数字填写不对")
        if not [k for k in s.get("keywords", []) if str(k).strip()]:
            errs.append(f"「{n}」至少要有一个关键词")
    sch = new.get("schedule", {})
    for k, label in (("run_time", "开始生成时间"), ("push_time", "推送时间")):
        if not HHMM.match(str(sch.get(k, ""))):
            errs.append(f"{label}要写成 08:30 这种格式")
    if not errs and sch["run_time"] >= sch["push_time"]:
        errs.append("开始生成时间要早于推送时间（生成一般需要 10–15 分钟）")
    if errs:
        return {"ok": False, "errors": errs}
    for k in ("min_importance", "max_total_items", "dedupe_days", "min_searches_per_section", "max_tool_calls"):
        if k in new:
            old[k] = int(new[k])
    old["sections"] = [{
        "name": str(s["name"]).strip(), "icon": str(s.get("icon", "")).strip(),
        "max_items": int(s["max_items"]), "window_hours": int(s["window_hours"]),
        "focus": str(s.get("focus", "")).strip(),
        "tags": [str(t).strip() for t in s.get("tags", []) if str(t).strip()],
        "corroboration_exempt_tags": [str(t).strip() for t in s.get("corroboration_exempt_tags", []) if str(t).strip()],
        "writing_hint": str(s.get("writing_hint", "")).strip(),
        "keywords": [str(k).strip() for k in s.get("keywords", []) if str(k).strip()],
    } for s in secs]
    sched_changed = old["schedule"] != {"run_time": sch["run_time"], "push_time": sch["push_time"]}
    old["schedule"] = {"run_time": sch["run_time"], "push_time": sch["push_time"]}
    if "model" in new:
        old.setdefault("claude", {})["model"] = str(new["model"] or "")
    if "push" in new:
        chans = [c for c in new["push"].get("channels", []) if c in push.CHANNELS]
        old["push"] = {"channels": chans, "format": "html" if new["push"].get("format") == "html" else "markdown"}
    (ROOT / "config.json").write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "schedule_changed": sched_changed}


def secrets_view():
    sec = secrets()
    out = {}
    for ch, fields in push.CHANNEL_FIELDS.items():
        fs = []
        for k, label, req, hint in fields:
            v = str(sec.get(k) or "")
            shown = v if k in push.PLAIN_FIELDS else (push._mask(v) if v else "")
            fs.append({"key": k, "label": label, "required": req, "hint": hint, "filled": bool(v), "shown": shown,
                       "secret": k not in push.PLAIN_FIELDS})
        out[ch] = {"fields": fs, "ready": push.channel_ready(ch, sec)}
    out["_last_test"] = run_brief.load_json(ROOT / "state" / "push_test.json", {}) or {}
    return out


def save_secrets(body):
    sec = secrets()
    allowed = push.SECRET_FIELDS
    for k, v in body.items():
        if k in allowed:
            if v is None:
                sec.pop(k, None)
            elif str(v).strip():
                sec[k] = str(v).strip()
    (ROOT / "secrets.json").write_text(json.dumps(sec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "secrets": secrets_view()}


# ---------- 动作 ----------
def start_run(do_push):
    if run_brief.running_info():
        return {"ok": False, "error": "已经有一次在运行，等它结束再开始"}
    args = [sys.executable, str(ROOT / "run_brief.py"), "--by", "控制台", "--no-wait" if do_push else "--no-push"]
    subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, creationflags=NOWIN)
    for _ in range(20):  # 等它写好运行锁，界面马上能显示进度
        time.sleep(0.25)
        if run_brief.running_info():
            break
    return {"ok": True}


def push_brief(date):
    if not DATE_RE.match(date):
        return {"ok": False, "error": "日期不对"}
    p = subprocess.run([sys.executable, str(ROOT / "run_brief.py"), "--push-only", date], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                       creationflags=NOWIN)
    runs = sorted((ROOT / "logs" / date).glob("run_*"))
    res = run_brief.load_json(runs[-1] / "推送结果.json", {}) if runs else {}
    results = res.get("results") if isinstance(res, dict) else res
    return {"ok": p.returncode == 0, "results": results or [], "output": (p.stdout or "")[-600:]}


def push_test(fmt):
    c = cfg()
    chans = c.get("push", {}).get("channels", [])
    if not chans:
        return {"ok": False, "error": "先勾选至少一个推送通道并保存"}
    push.SENDER_NAME = c.get("brief_name") or push.SENDER_NAME
    res = push.test_push(ROOT, chans, secrets(), "html" if fmt == "html" else "markdown")
    (ROOT / "state").mkdir(exist_ok=True)
    run_brief.write_json(ROOT / "state" / "push_test.json",
                         {"time": dt.datetime.now(tz()).strftime("%Y-%m-%d %H:%M"), "format": fmt, "results": res})
    return {"ok": all(r["ok"] for r in res), "results": res}


# ---------- 环境检查（换电脑时逐项引导） ----------
LAUNCHER = "AI早知道.exe"


def desktop_dir():
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)  # CSIDL_DESKTOPDIRECTORY
        if buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return Path.home() / "Desktop"


def env_status():
    c = cfg()
    try:
        claude_path = run_brief.find_claude(c)
    except Exception:
        claude_path = ""
    cli = cli_status() if claude_path else {"logged_in": False}
    sec = secrets()
    chans = c.get("push", {}).get("channels", [])
    last_test = run_brief.load_json(ROOT / "state" / "push_test.json", {}) or {}
    shortcut = desktop_dir() / "AI早知道.lnk"
    return {
        "computer": os.environ.get("COMPUTERNAME", ""), "folder": str(ROOT),
        "python": {"ok": True, "version": sys.version.split()[0], "path": sys.executable,
                   "bundled": "runtime" in Path(sys.executable).parts},
        "claude": {"installed": bool(claude_path), "path": claude_path, **cli},
        "push": {"channels": [push.CHANNEL_NAMES.get(x, x) for x in chans],
                 "ready": bool(chans) and all(push.channel_ready(x, sec) for x in chans),
                 "last_test": last_test},
        "schedule": schedule_status(),
        "shortcut": {"exists": shortcut.exists(), "path": str(shortcut),
                     "launcher": (ROOT / LAUNCHER).exists()},
    }


EXPORT_SKIP_DIRS = {"__pycache__", ".git"}
EXPORT_SKIP_FILES = {"logs/console.log", "state/running.json"}
PERSONAL = {"secrets.json", "state", "logs", "briefs"}  # 只有「给自己换电脑」才带


def export_package(mode):
    """导出移交包。self：给自己的另一台电脑，带令牌、推送密钥、历史早报和日志；
    other：交给别人，不带任何密钥和个人历史，推送通道清空，对方用自己的账号重新配置。"""
    import zipfile
    if mode not in ("self", "other"):
        return {"ok": False, "error": "未知导出方式"}
    stamp = dt.datetime.now(tz()).strftime("%Y%m%d_%H%M")
    out = ROOT.parent / f"AI早知道_{'换电脑' if mode == 'self' else '移交他人'}_{stamp}.zip"
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ROOT.rglob("*")):
            rel = p.relative_to(ROOT)
            parts = rel.parts
            if p.is_dir() or any(x in EXPORT_SKIP_DIRS for x in parts) or rel.as_posix() in EXPORT_SKIP_FILES:
                continue
            if p.suffix == ".pyc" or (mode == "other" and parts[0] in PERSONAL):
                continue
            arc = "AI早知道/" + rel.as_posix()
            if mode == "other" and rel.as_posix() == "config.json":
                c = json.loads(p.read_text(encoding="utf-8"))
                c["push"] = {"channels": [], "format": c.get("push", {}).get("format", "markdown")}
                z.writestr(arc, json.dumps(c, ensure_ascii=False, indent=2) + "\n")
            else:
                z.write(p, arc)
            n += 1
    subprocess.Popen(["explorer", "/select,", str(out)])
    return {"ok": True, "path": str(out), "files": n, "size_mb": round(out.stat().st_size / 1048576, 1)}


def open_terminal(args, title):
    """开一个可见的命令行窗口执行（登录、安装这类需要人操作的步骤）。"""
    subprocess.Popen(["cmd", "/k", "title " + title + " && " + subprocess.list2cmdline(args)],
                     cwd=str(ROOT), creationflags=subprocess.CREATE_NEW_CONSOLE)


def env_action(action):
    if action == "login":
        try:
            exe = run_brief.find_claude(cfg())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        open_terminal([exe, "auth", "login"], "登录 Claude")
        _cache.pop("cli", None)
        return {"ok": True, "message": "已打开登录窗口：按提示在浏览器里登录，完成后回来点「重新检查」"}
    if action == "install_claude":
        open_terminal(["winget", "install", "--id", "Anthropic.ClaudeCode", "-e"], "安装 Claude Code")
        return {"ok": True, "message": "已打开安装窗口（使用 Windows 自带的 winget）。装完后点「登录」"}
    if action == "shortcut":
        exe = ROOT / LAUNCHER
        if not exe.exists():
            return {"ok": False, "error": f"找不到 {LAUNCHER}"}
        lnk = desktop_dir() / "AI早知道.lnk"
        cmd = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
               f"$s.TargetPath='{exe}';$s.WorkingDirectory='{ROOT}';$s.IconLocation='{exe},0';"
               "$s.Description='AI早知道 每日资讯';$s.Save()")
        code, out, err = ps(cmd, 30)
        return {"ok": code == 0, "message": "已在桌面创建「AI早知道」快捷方式" if code == 0 else "", "error": err[-300:]}
    return {"ok": False, "error": "未知操作"}


def schedule_action(action):
    if action == "install":
        code, out, err = ps(f"& '{ROOT / 'install_task.ps1'}' -Python '{sys.executable}'", 60)
    elif action == "remove":
        code, out, err = ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false", 60)
    else:
        return {"ok": False, "error": "未知操作"}
    return {"ok": code == 0, "output": out, "error": err[-500:] if code else "", "status": schedule_status(now=True)}


def open_path(body):
    what = body.get("what")
    if what == "folder":
        target = ROOT
    elif what in ("brief_html", "brief_md") and DATE_RE.match(body.get("date", "")):
        target = ROOT / "briefs" / f"{body['date']}.{'html' if what == 'brief_html' else 'md'}"
    elif what == "run":
        target = run_path({"date": [body.get("date", "")], "run": [body.get("run", "")]})
    else:
        return {"ok": False, "error": "不认识的位置"}
    if not target.exists():
        return {"ok": False, "error": "文件不存在"}
    os.startfile(str(target))
    return {"ok": True}


# ---------- HTTP ----------
LAST_SEEN = [time.time()]
IDLE_EXIT_SECONDS = 15 * 60  # 窗口关掉后（没有页面再来请求）15 分钟自动退出后台，不常驻占资源


class Handler(BaseHTTPRequestHandler):
    server_version = "AIBriefConsole/1.0"

    def log_message(self, *a):
        pass

    def parse_request(self):
        ok = super().parse_request()
        if ok and not self.path.startswith("/api/ping"):
            LAST_SEEN[0] = time.time()
        return ok

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, (ROOT / "console.html").read_bytes(), "text/html; charset=utf-8")
            if u.path in ("/icon.png", "/favicon.ico"):
                f = ROOT / "launcher" / "icon.png"
                return self.send(200, f.read_bytes(), "image/png") if f.exists() else self.send(404, b"", "image/png")
            m = re.match(r"^/brief/(\d{4}-\d{2}-\d{2})\.html$", u.path)
            if m:
                f = ROOT / "briefs" / f"{m.group(1)}.html"
                if not f.exists():
                    return self.send(404, "<p style='font-family:sans-serif;color:#888;padding:24px'>这天还没有生成早报</p>",
                                     "text/html; charset=utf-8")
                return self.send(200, f.read_bytes(), "text/html; charset=utf-8")
            routes = {"/api/ping": lambda: {"app": APP_ID},
                      "/api/overview": overview,
                      "/api/runs": lambda: {"runs": list_runs()},
                      "/api/progress": lambda: {"progress": progress()},
                      "/api/log": lambda: log_list(q),
                      "/api/log/entry": lambda: log_entry(q),
                      "/api/lookup": lambda: lookup(q),
                      "/api/config": cfg,
                      "/api/secrets": secrets_view,
                      "/api/env": env_status,
                      "/api/schedule": lambda: schedule_status(now=True)}
            if u.path in routes:
                return self.send(200, routes[u.path]())
            self.send(404, {"error": "not found"})
        except ValueError as exc:
            self.send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            console_log(traceback.format_exc())
            self.send(500, {"ok": False, "error": repr(exc)})

    def do_POST(self):
        # 只接受本页面发起的请求：自定义请求头会让其他网站的跨域请求被浏览器拦下
        if self.headers.get("X-Console") != "1":
            return self.send(403, {"error": "forbidden"})
        u = urlsplit(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            routes = {"/api/run": lambda: start_run(bool(body.get("push"))),
                      "/api/push-brief": lambda: push_brief(body.get("date", "")),
                      "/api/push-test": lambda: push_test(body.get("format")),
                      "/api/config": lambda: save_config(body),
                      "/api/secrets": lambda: save_secrets(body),
                      "/api/schedule": lambda: schedule_action(body.get("action")),
                      "/api/open": lambda: open_path(body),
                      "/api/env": lambda: env_action(body.get("action")),
                      "/api/token": lambda: token_action(body),
                      "/api/export": lambda: export_package(body.get("mode")),
                      "/api/refresh-cli": lambda: cli_status(now=True)}
            if u.path in routes:
                return self.send(200, routes[u.path]())
            self.send(404, {"error": "not found"})
        except ValueError as exc:
            self.send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            console_log(traceback.format_exc())
            self.send(500, {"ok": False, "error": repr(exc)})


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP_ID
    except Exception:
        return False


def main():
    no_browser = "--no-browser" in sys.argv
    loglib.DISPLAY_TZ = tz()  # 日志时间按 config 的时区显示
    for port in PORTS:
        if already_running(port):
            if not no_browser:
                webbrowser.open(f"http://127.0.0.1:{port}/")
            return
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            continue
        schedule_status()  # 后台预热状态缓存
        cli_status()

        def watchdog():
            while True:
                time.sleep(30)
                if time.time() - LAST_SEEN[0] > IDLE_EXIT_SECONDS:
                    console_log("窗口已关闭一段时间，后台自动退出")
                    srv.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
        console_log(f"控制台启动：http://127.0.0.1:{port}/")
        if not no_browser:
            threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
        print(f"AI早知道 控制台：http://127.0.0.1:{port}/", flush=True)
        srv.serve_forever()
        return
    console_log("端口 8765–8775 都被占用，无法启动")


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
