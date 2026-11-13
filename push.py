# -*- coding: utf-8 -*-
"""推送通道：Server酱 / 邮件 / WxPusher / PushPlus / 企业微信群机器人。

密钥放在 secrets.json（在控制台「推送与定时」页填写，不要外传），config.json 的 push.channels 选用哪几个。
单独测试：python push.py [markdown|html]   → 用最近一份早报往已选通道各发一条测试
"""
import json
import re
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr

UA = "ai-morning-brief/1.0"

# 每个通道要填的项：(键, 显示名, 是否必填, 补充说明)
CHANNEL_FIELDS = {
    "serverchan": [("serverchan_sendkey", "SendKey", True, "")],
    "email": [("email_address", "发件邮箱", True, ""),
              ("email_auth_code", "SMTP 授权码", True, "在邮箱设置里开启 SMTP 后生成，不是邮箱登录密码"),
              ("email_to", "收件邮箱", False, "不填就发给自己；多人用逗号隔开，每人单独收一封"),
              ("email_smtp_host", "SMTP 服务器", False, "QQ、163、126、Gmail 等常见邮箱不用填")],
    "wxpusher": [("wxpusher_spt", "SPT", True, "多人用逗号隔开，每人扫码拿自己的 SPT")],
    "pushplus": [("pushplus_token", "token", True, "")],
    "wecom_bot": [("wecom_bot_webhook", "Webhook 地址", True, "")],
}
CHANNEL_NAMES = {"serverchan": "Server酱", "email": "邮件", "wxpusher": "WxPusher",
                 "pushplus": "PushPlus", "wecom_bot": "企业微信群机器人"}
SECRET_FIELDS = {f[0] for fields in CHANNEL_FIELDS.values() for f in fields}
PLAIN_FIELDS = {"email_address", "email_to", "email_smtp_host"}  # 不算密钥，界面上可以明文显示
SENDER_NAME = "AI早知道"  # 邮件发件人显示名，由 run_brief / 控制台按 config 的 brief_name 设置


def channel_ready(ch, sec):
    return all(str(sec.get(f[0]) or "").strip() for f in CHANNEL_FIELDS.get(ch, []) if f[2])


def _post(url, data, as_json=True, timeout=20):
    if as_json:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": UA}
    else:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8", "User-Agent": UA}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except Exception:
        return {"_raw": text}


def _mask(s):
    s = str(s or "")
    return s[:6] + "…" + s[-3:] if len(s) > 12 else "***"


def _full_html(title, html):
    if html.lstrip().lower().startswith("<!doctype") or "<html" in html[:200].lower():
        return html
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>'
            f'<body style="margin:0;background:#f5f6f8;">{html}</body></html>')


# ---------- 各通道：fn(密钥, 标题, markdown, html, 格式) -> (是否成功, 原始返回, 说明) ----------
def _serverchan(sec, title, md, html, fmt):  # 只支持 Markdown
    key = sec.get("serverchan_sendkey", "").strip()
    if not key:
        raise ValueError("还没有填 Server酱 的 SendKey")
    m = re.match(r"^sctp(\d+)t", key)
    url = (f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m
           else f"https://sctapi.ftqq.com/{key}.send")
    r = _post(url, {"title": title[:32], "desp": md}, as_json=False)
    ok = r.get("code") == 0 or (isinstance(r.get("data"), dict) and r["data"].get("errno") == 0)
    return ok, r, "已发出，微信里会收到一张服务号卡片，点开看全文" if ok else ""


SMTP_HOSTS = {"qq.com": ("smtp.qq.com", 465), "foxmail.com": ("smtp.qq.com", 465), "vip.qq.com": ("smtp.qq.com", 465),
              "163.com": ("smtp.163.com", 465), "126.com": ("smtp.126.com", 465), "yeah.net": ("smtp.yeah.net", 465),
              "sina.com": ("smtp.sina.com", 465), "aliyun.com": ("smtp.aliyun.com", 465),
              "gmail.com": ("smtp.gmail.com", 465)}


SPLIT_RE = re.compile(r"[\s,;，；、]+")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def split_list(s):
    return [x for x in SPLIT_RE.split(str(s or "").strip()) if x]


def _email(sec, title, md, html, fmt):
    """邮件永远用 HTML 排版（邮箱都能渲染），附带纯文本备份。多个收件人时每人单独发一封，互相看不到对方地址。"""
    addr = sec.get("email_address", "").strip()
    code = sec.get("email_auth_code", "").strip()
    if not addr or not code:
        raise ValueError("还没有填发件邮箱或 SMTP 授权码")
    rcpts = split_list(sec.get("email_to")) or [addr]
    bad = [x for x in rcpts if not EMAIL_RE.match(x)]
    if bad:
        raise ValueError("收件邮箱格式不对：" + "、".join(bad))
    host = sec.get("email_smtp_host", "").strip()
    if host:
        h, _, p = host.partition(":")
        port = int(p or 465)
    else:
        dom = addr.split("@")[-1].lower()
        if dom not in SMTP_HOSTS:
            raise ValueError(f"不认识 {dom} 的发信服务器，请在「SMTP 服务器」里填写，例如 smtp.xxx.com:465")
        h, port = SMTP_HOSTS[dom]
    body_html = _full_html(title, html) if html else None
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(h, port, timeout=30, context=ctx)
    else:
        s = smtplib.SMTP(h, port, timeout=30)
        s.starttls(context=ctx)
    sent, failed = [], {}
    with s:
        s.login(addr, code)  # 登录失败直接抛出，由外层给出「授权码不对」的提示
        for to in rcpts:     # 逐个发：某个地址出错不影响其他人，也不会因为重试给已收到的人重复发
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = formataddr((SENDER_NAME, addr))
            msg["To"] = to
            msg.set_content(md)
            if body_html:
                msg.add_alternative(body_html, subtype="html")
            try:
                s.send_message(msg)
                sent.append(to)
            except Exception as exc:
                failed[to] = repr(exc)[:200]
            if len(rcpts) > 1:
                time.sleep(1)
    note = f"已发给 {len(sent)} 人" + (f"（{sent[0]}）" if len(sent) == 1 else "")
    if failed:
        note += f"；{len(failed)} 人失败：" + "、".join(failed)
    return not failed, {"sent_to": sent, "failed": failed}, note


def _wxpusher(sec, title, md, html, fmt):
    spts = [x for x in split_list(sec.get("wxpusher_spt")) if x.startswith("SPT_")]
    if not spts:
        raise ValueError("还没有填 WxPusher 的 SPT（应以 SPT_ 开头）")
    use_html = fmt == "html" and html
    rs = []
    for i in range(0, len(spts), 10):  # 接口一次最多 10 个接收人
        rs.append(_post("https://wxpusher.zjiecode.com/api/send/message/simple-push",
                        {"content": html if use_html else md, "summary": title[:100],
                         "contentType": 2 if use_html else 3, "sptList": spts[i:i + 10]}))
    r = rs[0] if len(rs) == 1 else {"batches": rs}
    ok = all(x.get("code") == 1000 or x.get("success") is True for x in rs)
    note = ("WxPusher 已受理（不等于已进微信：微信端要先绑定并激活「微信 ClawBot」，"
            "每次激活只管 24 小时、最多 10 条；没激活时只会出现在 WxPusher 自己的 App 里）") if ok else ""
    return ok, r, note


def _pushplus(sec, title, md, html, fmt):
    token = sec.get("pushplus_token", "").strip()
    if not token:
        raise ValueError("还没有填 PushPlus 的 token")
    use_html = fmt == "html" and html
    r = _post("https://www.pushplus.plus/send",
              {"token": token, "title": title, "content": html if use_html else md,
               "template": "html" if use_html else "markdown"})
    ok = r.get("code") == 200
    if r.get("code") == 905:
        return False, r, "PushPlus 账号还没实名认证，发不出去（实名需付费）"
    return ok, r, "已发出" if ok else ""


def _split_bytes(md, limit=3800):
    """企业微信 markdown 单条上限 4096 字节：按行装箱，尽量在栏目标题处断开。"""
    nbytes = lambda s: len(s.encode("utf-8"))  # noqa: E731
    parts, cur = [], ""
    for ln in md.splitlines(keepends=True):
        while nbytes(ln) > limit:  # 单行就超长，硬切
            head = ln.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(head)
            ln = ln[len(head):]
        at_section = ln.startswith("## ") and nbytes(cur) > limit * 0.6
        if cur and (at_section or nbytes(cur + ln) > limit):
            parts.append(cur)
            cur = ""
        cur += ln
    if cur.strip():
        parts.append(cur)
    return parts


def _wecom_bot(sec, title, md, html, fmt):  # 企业微信只支持 Markdown
    hook = sec.get("wecom_bot_webhook", "").strip()
    if not hook:
        raise ValueError("还没有填企业微信群机器人的 Webhook 地址")
    results, ok = [], True
    for part in _split_bytes(md):
        r = _post(hook, {"msgtype": "markdown", "markdown": {"content": part}})
        results.append(r)
        ok = ok and r.get("errcode") == 0
        time.sleep(1)
    return ok, results, "已发到企业微信群" if ok else ""


CHANNELS = {"serverchan": _serverchan, "email": _email, "wxpusher": _wxpusher,
            "pushplus": _pushplus, "wecom_bot": _wecom_bot}
HTML_CAPABLE = {"email", "wxpusher", "pushplus"}


def send_all(channels, secrets, title, md, html=None, fmt="markdown", retries=3):
    """逐个通道发送，网络类错误最多重试 3 次。返回每个通道的结果（不含密钥）。"""
    results = []
    for ch in channels:
        fn = CHANNELS.get(ch)
        if fn is None:
            results.append({"channel": ch, "ok": False, "detail": "未知通道"})
            continue
        last = None
        for attempt in range(1, retries + 1):
            try:
                ok, resp, note = fn(secrets, title, md, html, fmt)
                used = "html" if (html and (ch == "email" or (fmt == "html" and ch in HTML_CAPABLE))) else "markdown"
                last = {"channel": ch, "ok": ok, "attempt": attempt, "format": used, "note": note, "response": resp}
                if ok:
                    break
            except (ValueError, smtplib.SMTPAuthenticationError) as exc:
                detail = ("邮箱拒绝登录：请确认填的是 SMTP 授权码（在邮箱设置里开启 SMTP 服务后生成），不是登录密码"
                          if isinstance(exc, smtplib.SMTPAuthenticationError) else str(exc))
                last = {"channel": ch, "ok": False, "attempt": attempt, "detail": detail}
                break
            except Exception as exc:
                last = {"channel": ch, "ok": False, "attempt": attempt, "detail": repr(exc)}
            if attempt < retries:
                time.sleep(3 * attempt)
        results.append(last)
    return results


def test_push(root, channels, secrets, fmt):
    """用最近一份真实早报做测试推送（没有早报时发一条示例），方便在微信里对比效果。"""
    from pathlib import Path
    runs = sorted(Path(root).glob("logs/20*/run_*/早报_推送版.md"))
    if runs:
        md = runs[-1].read_text(encoding="utf-8")
        hp = runs[-1].with_suffix(".html")
        html = hp.read_text(encoding="utf-8") if hp.exists() else None
        title = "【测试】" + md.splitlines()[0].lstrip("# ").replace("｜", " ")
    else:
        md = "# 推送测试\n\n这是一条来自 **AI早知道** 的测试消息。\n\n- 收到即表示通道可用\n- [示例链接](https://example.com)"
        html = ('<div style="padding:16px;font-size:15px;"><h2>推送测试</h2><p>这是一条来自 <b>AI早知道</b> 的测试消息，'
                '收到即表示通道可用。</p><p><a href="https://example.com">示例链接</a></p></div>')
        title = "【测试】AI早知道 推送测试"
    return send_all(channels, secrets, title, md, html, fmt)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(__file__).resolve().parent
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    sec_path = root / "secrets.json"
    sec = json.loads(sec_path.read_text(encoding="utf-8")) if sec_path.exists() else {}
    chans = cfg.get("push", {}).get("channels", [])
    if not chans:
        print("还没有选推送通道：打开控制台「推送与定时」页设置")
        sys.exit(1)
    fmt = sys.argv[1] if len(sys.argv) > 1 else cfg.get("push", {}).get("format", "markdown")
    for r in test_push(root, chans, sec, fmt):
        print(json.dumps(r, ensure_ascii=False))
