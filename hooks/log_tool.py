# -*- coding: utf-8 -*-
"""检索留档钩子：Claude 每次调用 WebSearch / WebFetch 前后各触发一次。

把钩子收到的原始数据（查询词 / 网址 / 原始返回）原样写成一个独立文件，一次调用一份，
不做任何加工。写到环境变量 BRIEF_RAW_DIR 指定的目录；没有该变量时写到 logs/manual/<日期>/raw。
任何异常都吞掉并以 0 退出，保证不影响检索本身。
用法（由 Claude Code 调用）：python log_tool.py pre|post|fail
"""
import datetime
import json
import os
import re
import sys
import time


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"_unparsed_stdin": raw}

    out_dir = os.environ.get("BRIEF_RAW_DIR")
    if not out_dir:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out_dir = os.path.join(root, "logs", "manual", datetime.date.today().isoformat(), "raw")
    os.makedirs(out_dir, exist_ok=True)

    ns = time.time_ns()
    tool_use_id = str(payload.get("tool_use_id") or "na") if isinstance(payload, dict) else "na"
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", tool_use_id)[-32:] or "na"
    record = {
        "logged_at": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "logged_ns": ns,
        "event": event,
        "payload": payload,
    }
    path = os.path.join(out_dir, f"{ns}_{event}_{safe_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # 留档失败也不能挡住检索
        try:
            err_dir = os.environ.get("BRIEF_RAW_DIR") or os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(err_dir, "_hook_errors.txt"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now().isoformat()} {exc!r}\n")
        except Exception:
            pass
    sys.exit(0)
