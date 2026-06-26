#!/usr/bin/env python3
"""
邮件发送脚本 — Smart Invest Skill
支持通过 QQ 邮箱 SMTP 发送 HTML 格式的分析报告。
纯 Python 3 标准库。
"""

import argparse
import json
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_FILE = DATA_DIR / "email_config.json"
OUTBOX_DIR = DATA_DIR / "outbox"   # 发失败的邮件落盘待发，下次自动补发


def load_config():
    """加载邮件配置"""
    if not CONFIG_FILE.exists():
        print(f"[ERROR] 配置文件不存在: {CONFIG_FILE}", file=sys.stderr)
        print("请先配置 email_config.json", file=sys.stderr)
        return None

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    if not config.get("enabled", False):
        print("[WARN] 邮件通知未启用，请在 email_config.json 中设置 enabled: true", file=sys.stderr)
        return None

    smtp = config.get("smtp", {})
    required = ["server", "port", "sender", "password", "receiver"]
    for key in required:
        val = smtp.get(key)
        if key == "receiver":
            if isinstance(val, str):
                if not val or val.startswith("你的"):
                    print(f"[ERROR] 请先在 {CONFIG_FILE} 中填写 {key}", file=sys.stderr)
                    return None
                smtp["receiver"] = [val]
            elif isinstance(val, list):
                val = [v for v in val if v and not v.startswith("你的")]
                if not val:
                    print(f"[ERROR] 请先在 {CONFIG_FILE} 中填写 {key}", file=sys.stderr)
                    return None
                smtp["receiver"] = val
            else:
                print(f"[ERROR] 请先在 {CONFIG_FILE} 中填写 {key}", file=sys.stderr)
                return None
        else:
            if not val or (isinstance(val, str) and val.startswith("你的")):
                print(f"[ERROR] 请先在 {CONFIG_FILE} 中填写 {key}", file=sys.stderr)
                return None

    return config


def _md(text):
    """行内 Markdown → HTML"""
    import re
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # 删除线 ~~撤销的操作~~ → 灰色划掉（今日操作计划里"改判作废"的项）
    text = re.sub(r"~~(.+?)~~",
                  r'<s style="color:#999;">\1</s>', text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    return text


def _color_pct(text):
    """给百分比涨跌上色"""
    import re
    text = _md(text)
    text = re.sub(r'(\+[\d,.]+%)', r'<span style="color:#FF453A;font-weight:700;">\1</span>', text)
    text = re.sub(r'(-[\d,.]+%)', r'<span style="color:#30D158;font-weight:700;">\1</span>', text)
    return text


def _is_separator_row(cells):
    """判断是否为表格分隔行 (---|---|---)"""
    return all(set(c.strip()) <= set("-: |") for c in cells if c.strip())


def _looks_numeric(text):
    """判断单元格内容是否像数字（用于右对齐）"""
    import re
    t = text.strip().lstrip("+-¥$€£").replace(",", "").replace(" ", "")
    if not t:
        return False
    # 纯数字、带小数点、带百分号、带¥符号
    return bool(re.match(r'^[\d,.]+%?$', t))


def markdown_to_html(md_text):
    """Markdown → HTML — 支付宝基金风格，白底扁平，移动端优先"""
    import re

    lines = md_text.split("\n")
    parts = []
    block_type = None
    block_lines = []
    table_headers = []
    table_done = False
    in_table = False
    in_list = False

    GRAY = "#999999"
    BODY = "#333333"
    LINE = "#EEEEEE"
    UP   = "#E64340"
    DN   = "#09BB07"

    def flush_list():
        nonlocal in_list
        if in_list:
            parts.append("</table>")
            in_list = False

    def flush_table():
        nonlocal in_table, table_headers, table_done
        in_table = False
        table_headers = []
        table_done = False

    for line in lines:
        s = line.strip()
        if not s:
            continue

        # === block markers ===
        if s.startswith(":::"):
            tag = s[3:].strip()
            if tag and block_type is None:
                flush_list(); flush_table()
                block_type = tag
                block_lines = []
                continue
            elif block_type:
                if block_type == "card":
                    lbl = block_lines[0] if block_lines else ""
                    num = block_lines[1] if len(block_lines) > 1 else ""
                    neg = num.lstrip().startswith("-")
                    nc = DN if neg else UP   # 红涨绿跌：盈利(正)→红，亏损(负)→绿
                    parts.append(
                        f'<table width="100%" cellpadding="0" cellspacing="0">'
                        f'<tr><td style="padding:32px 20px 24px;text-align:center;'
                        f'background-color:#ffffff;">'
                        f'<p style="margin:0 0 8px;font-size:13px;color:{GRAY};">{_md(lbl)}</p>'
                        f'<p style="margin:0;font-size:48px;font-weight:700;'
                        f'color:{nc};line-height:1;letter-spacing:-1px;">{_md(num)}</p>'
                    )
                    if len(block_lines) > 2:
                        stats = [x.strip() for x in " ".join(block_lines[2:]).split("|")]
                        parts.append(
                            f'<table cellpadding="0" cellspacing="0" style="margin:16px auto 0;"><tr>'
                        )
                        for i, st in enumerate(stats):
                            if i > 0:
                                parts.append(
                                    f'<td style="padding:0 6px;color:#DDDDDD;font-size:12px;">|</td>'
                                )
                            parts.append(
                                f'<td style="padding:0 4px;">'
                                f'<p style="margin:0;font-size:12px;color:{GRAY};white-space:nowrap;">{_md(st)}</p></td>'
                            )
                        parts.append('</tr></table>')
                    parts.append(
                        f'</td></tr>'
                        f'<tr><td style="height:8px;background-color:#F5F5F5;"></td></tr>'
                        f'</table>'
                    )
                elif block_type == "spark":
                    # 行1: "标题 | 右侧数值"；行2: 逗号分隔的价格序列
                    _sdir = str(Path(__file__).resolve().parent)
                    if _sdir not in sys.path:
                        sys.path.insert(0, _sdir)
                    import chart as _chart
                    head = block_lines[0] if block_lines else ""
                    segs = [x.strip() for x in head.split("|")]
                    label = segs[0] if segs else ""
                    right = segs[1] if len(segs) > 1 else ""
                    try:
                        vals = [float(x) for x in
                                (block_lines[1] if len(block_lines) > 1 else "").split(",") if x.strip()]
                    except ValueError:
                        vals = []
                    rc = DN if right.rstrip("%").split()[-1].startswith("-") else UP
                    parts.append(
                        '<table width="100%" cellpadding="0" cellspacing="0">'
                        '<tr><td style="padding:14px 20px 12px;background-color:#ffffff;">'
                        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
                        f'<td style="font-size:13px;color:{GRAY};">{_md(label)}</td>'
                        f'<td style="font-size:13px;font-weight:700;color:{rc};'
                        f'text-align:right;white-space:nowrap;">{_md(right)}</td>'
                        '</tr></table>'
                    )
                    if len(vals) >= 2:
                        parts.append(
                            f'<div style="margin-top:8px;">{_chart.spark_html(vals)}</div>'
                        )
                    parts.append(
                        '</td></tr>'
                        '<tr><td style="height:8px;background-color:#F5F5F5;"></td></tr>'
                        '</table>'
                    )
                elif block_type == "blocks":
                    import re as _re
                    items = []
                    for bl in block_lines:
                        m = _re.search(r'([+-]?\d+\.?\d*)%', bl)
                        if m:
                            pct = float(m.group(1))
                            label = bl[:m.start()].strip()
                            pct_str = m.group(0)
                        else:
                            label = bl.strip()
                            pct = 0.0
                            pct_str = ""
                        items.append((label, pct, pct_str))
                    items.sort(key=lambda x: abs(x[1]), reverse=True)

                    def _block_color(pv):
                        if pv > 3:   return "#B71C1C", "#ffffff"
                        if pv > 1:   return "#E64340", "#ffffff"
                        if pv > 0:   return "#FDEDEC", "#C0392B"
                        if pv == 0:  return "#F0F0F0", "#999999"
                        if pv > -1:  return "#EAFAF1", "#1E8449"
                        if pv > -3:  return "#09BB07", "#ffffff"
                        return "#1E8449", "#ffffff"

                    def _block_cell(lb, pv, ps, pad_v, name_sz, pct_sz):
                        bg, fg = _block_color(pv)
                        return (
                            f'<td style="background-color:{bg};border-radius:6px;'
                            f'padding:{pad_v}px 6px;text-align:center;vertical-align:middle;">'
                            f'<p style="margin:0 0 2px;font-size:{name_sz}px;color:{fg};'
                            f'opacity:0.85;white-space:nowrap;overflow:hidden;">{lb}</p>'
                            f'<p style="margin:0;font-size:{pct_sz}px;font-weight:700;color:{fg};">{ps}</p>'
                            f'</td>'
                        )

                    n = len(items)
                    if n <= 2:
                        tiers = [(items, 2, 20, 13, 20)]
                    elif n <= 5:
                        tiers = [
                            (items[:2], 2, 20, 13, 20),
                            (items[2:], 3, 12, 11, 14),
                        ]
                    else:
                        tiers = [
                            (items[:2], 2, 22, 13, 22),
                            (items[2:5], 3, 14, 11, 15),
                            (items[5:], min(len(items[5:]), 4), 10, 10, 12),
                        ]

                    parts.append(
                        '<table width="100%" cellpadding="0" cellspacing="0" '
                        'style="margin:0;background-color:#ffffff;padding:4px 16px 8px;">'
                    )
                    for tier_items, cols, pad_v, name_sz, pct_sz in tiers:
                        for row_start in range(0, len(tier_items), cols):
                            row_items = tier_items[row_start:row_start + cols]
                            parts.append(
                                '<tr><td colspan="100%" style="padding:0;">'
                                '<table width="100%" cellpadding="0" cellspacing="3" '
                                'style="table-layout:fixed;"><tr>'
                            )
                            for lb, pv, ps in row_items:
                                parts.append(_block_cell(lb, pv, ps, pad_v, name_sz, pct_sz))
                            parts.append('</tr></table></td></tr>')
                    parts.append('</table>')
                elif block_type == "returns":
                    parts.append(
                        '<table width="100%" cellpadding="0" cellspacing="0"'
                        ' style="background-color:#ffffff;">'
                    )
                    for ri, rl in enumerate(block_lines):
                        segs = [x.strip() for x in rl.split("|")]
                        name = segs[0] if segs else ""
                        pct = segs[1] if len(segs) > 1 else ""
                        amt = segs[2] if len(segs) > 2 else ""
                        is_total = name.startswith("合计") or name.startswith("总计")
                        pct_s = _color_pct(pct) if pct and pct != "--" else f'<span style="color:#CCC;">{pct}</span>'
                        amt_neg = amt.strip().startswith("-")
                        amt_c = DN if amt_neg else (UP if amt.strip().startswith("+") else GRAY)
                        if is_total:
                            parts.append(
                                f'<tr><td style="padding:0 20px;">'
                                f'<div style="height:1px;background:{LINE};"></div></td></tr>'
                                f'<tr><td style="padding:10px 20px 12px;">'
                                f'<table width="100%" cellpadding="0" cellspacing="0"><tr>'
                                f'<td style="font-size:14px;font-weight:600;color:#333;">{name}</td>'
                                f'<td style="text-align:right;font-size:16px;font-weight:700;color:{amt_c};">'
                                f'{amt}元</td>'
                                f'</tr></table></td></tr>'
                            )
                        else:
                            parts.append(
                                f'<tr><td style="padding:8px 20px;'
                                f'border-bottom:1px solid #FAFAFA;">'
                                f'<table width="100%" cellpadding="0" cellspacing="0"><tr>'
                                f'<td style="vertical-align:middle;">'
                                f'<p style="margin:0;font-size:14px;color:#333;">{_md(name)}</p></td>'
                                f'<td style="text-align:center;width:70px;font-size:13px;">{pct_s}</td>'
                                f'<td style="text-align:right;width:80px;font-size:14px;font-weight:600;'
                                f'color:{amt_c};">{amt}元</td>'
                                f'</tr></table></td></tr>'
                            )
                    parts.append('</table>')
                elif block_type == "timeline":
                    import re as _re2
                    parts.append(
                        '<table width="100%" cellpadding="0" cellspacing="0"'
                        ' style="background-color:#ffffff;">'
                    )
                    prev_date = None
                    for tl in block_lines:
                        segs = tl.split("|", 1) if "|" in tl else [tl[:5], tl[5:]]
                        dt = segs[0].strip() if len(segs) > 1 else ""
                        desc = segs[1].strip() if len(segs) > 1 else tl.strip()
                        if dt and dt != prev_date:
                            parts.append(
                                f'<tr><td style="padding:10px 20px 2px;">'
                                f'<p style="margin:0;font-size:12px;font-weight:600;color:#999;">{dt}</p>'
                                f'</td></tr>'
                            )
                            prev_date = dt
                        is_buy = "买入" in desc or "加仓" in desc
                        is_sell = "卖出" in desc or "减仓" in desc
                        dot_c = "#09BB07" if is_buy else ("#E64340" if is_sell else "#CCCCCC")
                        parts.append(
                            f'<tr><td style="padding:0 20px;">'
                            f'<table width="100%" cellpadding="0" cellspacing="0"><tr>'
                            f'<td style="width:20px;vertical-align:top;padding-top:8px;">'
                            f'<div style="width:8px;height:8px;border-radius:50%;background:{dot_c};'
                            f'margin:0 auto;"></div></td>'
                            f'<td style="padding:4px 0 4px 4px;font-size:14px;color:#333;'
                            f'line-height:1.5;border-left:2px solid #F0F0F0;">'
                            f'&nbsp;&nbsp;{_color_pct(desc)}</td>'
                            f'</tr></table></td></tr>'
                        )
                    parts.append('</table>')
                elif block_type == "action":
                    txt = " ".join(block_lines)
                    parts.append(
                        f'<table width="100%" cellpadding="0" cellspacing="0">'
                        f'<tr><td style="padding:14px 20px;background-color:#FFF9ED;'
                        f'border-left:3px solid #FF9500;">'
                        f'<p style="margin:0;font-size:14px;color:#664400;line-height:1.6;">{_md(txt)}</p>'
                        f'</td></tr></table>'
                    )
                block_type = None
                block_lines = []
                continue

        if block_type:
            block_lines.append(s)
            continue

        # === table → fund row ===
        if "|" in s and s.startswith("|"):
            flush_list()
            cells = [c.strip() for c in s.split("|")[1:-1]]
            if _is_separator_row(cells):
                table_done = True
                continue
            if not table_done:
                in_table = True
                table_headers = cells
                continue
            name = _md(cells[0]) if cells else ""
            today = cells[1] if len(cells) > 1 else ""
            held = cells[6].strip() if len(cells) > 6 else ""   # 持有天数（可选第7列）
            if len(cells) >= 6:
                today_pnl = cells[2]
                hold_pnl = cells[3]      # 持有收益（累计浮盈金额）
                ret = cells[4]
                val = cells[5]
            elif len(cells) >= 5:
                today_pnl = cells[2]
                hold_pnl = ""
                ret = cells[3]
                val = cells[4]
            else:
                today_pnl = ""
                hold_pnl = ""
                ret = cells[2] if len(cells) > 2 else ""
                val = cells[-1] if len(cells) > 1 else ""
            today_s = _color_pct(today)
            ret_s = _color_pct(ret)
            pnl_neg = today_pnl.strip().startswith("-")
            pnl_color = DN if pnl_neg else UP if today_pnl.strip().startswith("+") else GRAY
            parts.append(
                f'<table width="100%" cellpadding="0" cellspacing="0">'
                f'<tr><td style="padding:14px 20px;background-color:#ffffff;'
                f'border-bottom:1px solid {LINE};">'
                f'<table width="100%" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="vertical-align:middle;">'
                f'<p style="margin:0;font-size:16px;font-weight:500;color:#222222;">{name}</p>'
                f'<p style="margin:4px 0 0;font-size:12px;color:{GRAY};">'
                f'市值 &#165;{val}'
            )
            if held:
                parts.append(f'&nbsp;&nbsp;持有 {held}')
            if today_pnl.strip():
                parts.append(
                    f'&nbsp;&nbsp;今日 <span style="color:{pnl_color};font-weight:600;">{_md(today_pnl)}</span>'
                )
            if hold_pnl.strip() and hold_pnl.strip() != "--":
                yp = hold_pnl.strip()
                if yp.startswith("-"):
                    yc = DN
                elif yp.startswith("+"):
                    yc = UP
                else:
                    yc = GRAY
                parts.append(
                    f'&nbsp;&nbsp;持有收益 <span style="color:{yc};font-weight:600;">{_md(yp)}</span>'
                )
            parts.append(
                f'</p></td>'
                f'<td style="text-align:right;vertical-align:middle;">'
                f'<p style="margin:0 0 2px;font-size:18px;font-weight:700;'
                f'letter-spacing:-0.5px;">{today_s}</p>'
                f'<p style="margin:0;font-size:12px;color:{GRAY};">累计 {ret_s}</p>'
                f'</td>'
                f'</tr></table></td></tr></table>'
            )
            continue
        else:
            if in_table:
                flush_table()

        # === headings ===
        if s.startswith("### "):
            flush_list()
            parts.append(
                f'<table width="100%" cellpadding="0" cellspacing="0">'
                f'<tr><td style="padding:16px 20px 8px;background-color:#F5F5F5;">'
                f'<p style="margin:0;font-size:13px;font-weight:600;color:{GRAY};">{_md(s[4:])}</p>'
                f'</td></tr></table>'
            )
            continue
        if s.startswith("## ") or s.startswith("# "):
            flush_list()
            continue

        # === list ===
        if s.startswith("- ") or s.startswith("* "):
            if not in_list:
                parts.append(
                    '<table width="100%" cellpadding="0" cellspacing="0">'
                )
                in_list = True
            parts.append(
                f'<tr><td style="padding:6px 20px;background-color:#ffffff;font-size:14px;'
                f'color:{BODY};line-height:1.5;">{_color_pct(s[2:])}</td></tr>'
            )
            continue

        flush_list()

        if s in ("---", "***", "___"):
            continue

        # === paragraph ===
        parts.append(
            f'<table width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td style="padding:6px 20px;background-color:#ffffff;'
            f'font-size:14px;color:{BODY};line-height:1.5;">{_color_pct(s)}</td></tr></table>'
        )

    flush_list()
    flush_table()

    body = "\n".join(parts)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background-color:#F5F5F5;">
<table width="100%" cellpadding="0" cellspacing="0"
  style="max-width:420px;margin:0 auto;background-color:#F5F5F5;
  font-family:-apple-system,'PingFang SC','Helvetica Neue','Microsoft YaHei',sans-serif;">
<tr><td>
{body}

<table width="100%" cellpadding="0" cellspacing="0">
<tr><td style="padding:20px;text-align:center;">
  <p style="margin:0;font-size:11px;color:#CCCCCC;">Smart Invest &middot; 投资有风险，入市需谨慎</p>
  <p style="margin:4px 0 0;font-size:11px;color:#AAAAAA;">由小杀 🔪 (OpenClaw Agent · Claude Sonnet 4) 自动生成并发送</p>
</td></tr>
</table>

</td></tr>
</table>
</body>
</html>"""


def _build_msg(subject, body_text, html_body, sender, receivers):
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def _smtp_send(smtp_conf, receivers, msg, retries=3):
    """单封邮件的 SMTP 投递 + 进程内重试（指数退避 1/2/4s）。返回 bool，绝不抛异常。"""
    last_err = None
    for attempt in range(retries):
        try:
            if smtp_conf.get("use_ssl", True):
                server = smtplib.SMTP_SSL(smtp_conf["server"], smtp_conf["port"], timeout=30)
            else:
                server = smtplib.SMTP(smtp_conf["server"], smtp_conf["port"], timeout=30)
                server.starttls()
            server.login(smtp_conf["sender"], smtp_conf["password"])
            server.sendmail(smtp_conf["sender"], receivers, msg.as_string())
            server.quit()
            return True
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    print(f"[ERROR] 邮件发送失败（已重试 {retries} 次）: {last_err}", file=sys.stderr)
    return False


def _deliver(subject, body_text, html_body, receivers, config=None):
    """投递一封邮件（含进程内重试）。不碰 outbox、不递归 flush。返回 bool。"""
    config = config or load_config()
    if not config:
        return False
    smtp_conf = config["smtp"]
    rcv = receivers or smtp_conf["receiver"]
    if isinstance(rcv, str):
        rcv = [rcv]
    msg = _build_msg(subject, body_text, html_body, smtp_conf["sender"], rcv)
    ok = _smtp_send(smtp_conf, rcv, msg)
    if ok:
        print(f"[OK] 邮件已发送至 {', '.join(rcv)}")
    return ok


def _enqueue_outbox(subject, body_text, html_body, receivers):
    """投递失败时落盘到 data/outbox/，下次任何发信前自动补发。"""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = len(list(OUTBOX_DIR.glob("*.json")))
    fp = OUTBOX_DIR / f"{ts}-{n:03d}.json"
    rcv = receivers if isinstance(receivers, list) else [receivers]
    fp.write_text(json.dumps({
        "subject": subject, "body_text": body_text, "html": html_body,
        "receivers": rcv, "created_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    print(f"[WARN] 邮件发送失败，已存入待发队列 {fp.name}，下次运行将自动补发", file=sys.stderr)
    return fp


def flush_outbox(config=None):
    """补发 data/outbox/ 里积压的邮件，成功则删文件。返回补发数量。"""
    if not OUTBOX_DIR.exists():
        return 0
    files = sorted(OUTBOX_DIR.glob("*.json"))
    if not files:
        return 0
    config = config or load_config()
    if not config:
        return 0
    sent = 0
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            fp.unlink(missing_ok=True)   # 损坏的队列文件直接丢弃
            continue
        ok = _deliver(data.get("subject", ""), data.get("body_text", ""),
                      data.get("html"), data.get("receivers"), config)
        if ok:
            fp.unlink(missing_ok=True)
            sent += 1
        else:
            break   # 还发不出去，保留全部，留待下次
    if sent:
        print(f"[OK] 已补发积压邮件 {sent} 封")
    return sent


def send_email(subject, body_text, html_body=None):
    """发送邮件：先补发积压队列 → 投递（含重试）→ 失败落盘待发。"""
    config = load_config()
    if not config:
        return False   # 邮件未配置/已关闭：不入队，避免无意义堆积
    flush_outbox(config)
    rcv = config["smtp"]["receiver"]
    if isinstance(rcv, str):
        rcv = [rcv]
    ok = _deliver(subject, body_text, html_body, rcv, config)
    if not ok:
        _enqueue_outbox(subject, body_text, html_body, rcv)
    return ok


def cmd_flush_outbox(args):
    """手动补发待发队列（cron 可定期调）"""
    n = flush_outbox()
    if n == 0:
        pending = len(list(OUTBOX_DIR.glob("*.json"))) if OUTBOX_DIR.exists() else 0
        print(f"无可补发邮件（队列积压 {pending} 封，可能邮件未配置或仍发不出）")


def cmd_send(args):
    """从文件或 stdin 读取报告内容并发送"""
    subject = args.subject

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            body = f.read()
    else:
        body = sys.stdin.read()

    if not body.strip():
        print("[ERROR] 报告内容为空", file=sys.stderr)
        return

    html = markdown_to_html(body)
    send_email(subject, body, html)


def cmd_trade_notify(args):
    """发送交易操作报告邮件（每笔买卖强制；含操作依据 + 相关要闻 + 操作后钱包）"""
    action_cn = "买入" if args.action == "buy" else "卖出"
    emoji = "🟢" if args.action == "buy" else "🔴"
    amount = float(args.amount)

    subject = f"{emoji} 操作报告 - {action_cn}「{args.name}」¥{amount:,.0f}"

    reason = (getattr(args, "reason", None) or args.note or "").strip()
    news = [n.strip() for n in (getattr(args, "news", None) or []) if n and n.strip()]
    wallet = (getattr(args, "wallet", None) or "").strip()

    parts = [
        ":::card",
        f"{action_cn}操作已完成",
        f"¥{amount:,.0f}",
        f"{args.code} {args.name} | {float(args.shares):,.2f} 份 @ "
        f"¥{float(args.nav):.4f} | {datetime.now().strftime('%m-%d %H:%M')}",
        ":::",
        "",
        "### 📋 操作依据",
        reason if reason else "（未提供操作依据——按规范每笔操作都应说明原因）",
        "",
        "### 📰 相关要闻",
    ]
    if news:
        parts += [f"- {n}" for n in news[:5]]
    else:
        parts.append("- （本次未附带新闻，建议补充操作的消息面依据）")
    parts.append("")
    if wallet:
        parts += ["### 💰 操作后钱包", wallet, ""]
    parts += [
        "### 提示",
        "- 买入 T+1 确认份额，QDII 可能 T+2；可在支付宝查看订单状态",
        "- ⚠️ 投资有风险，入市需谨慎。",
    ]

    body = "\n".join(parts)
    html = markdown_to_html(body)
    send_email(subject, body, html)


def cmd_test(args):
    """发送测试邮件"""
    subject = "Smart Invest 邮件测试"
    body = "这是一封测试邮件，如果你收到了说明邮件配置成功！"
    html = markdown_to_html("# 测试成功\n\nSmart Invest 邮件通知已配置成功。\n\n- 数据来源：天天基金/东方财富\n- 定时任务：每个交易日 14:30")
    send_email(subject, body, html)


def _interactive_setup():
    """Phase 4: 交互式 setup — Claude 直接调时其实不会用，但用户在 shell
    里跑 send_email.py setup --interactive 时会被引导逐步填写。"""
    print("\n=== Smart-Invest 邮件配置向导 ===\n")
    print("此向导帮你设置 QQ 邮箱 SMTP。需要先在 QQ 邮箱网页版开启 SMTP 服务并生成授权码。")
    print("位置：QQ 邮箱 → 设置 → 账户 → POP3/IMAP/SMTP… → 开启 → 生成授权码")
    print("（授权码是一串 16 位字母，跟登录密码不一样）\n")

    sender = input("发件邮箱（你的 QQ 邮箱地址）: ").strip()
    if not sender or "@" not in sender:
        print("[ERROR] 邮箱格式不正确", file=sys.stderr)
        return None
    password = input("SMTP 授权码: ").strip()
    if not password or len(password) < 8:
        print("[ERROR] 授权码看起来太短（应为 16 位字母）", file=sys.stderr)
        return None
    receivers_raw = input(
        "收件邮箱（多个用空格分隔；留空则发到自己）: "
    ).strip()
    receivers = receivers_raw.split() if receivers_raw else [sender]
    for r in receivers:
        if "@" not in r:
            print(f"[ERROR] 收件邮箱 '{r}' 格式不正确", file=sys.stderr)
            return None
    return sender, password, receivers


def cmd_setup(args):
    """配置邮件（首次引导或修改配置）"""
    if args.no_email:
        config = {"enabled": False}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print("[OK] 邮件功能已关闭")
        return

    if args.interactive:
        result = _interactive_setup()
        if result is None:
            return
        args.sender, args.password, args.receiver = result

    if not args.sender or not args.password or not args.receiver:
        print(
            "[ERROR] 邮件配置不完整。可选：\n"
            "  1. 交互式引导：python3 scripts/send_email.py setup --interactive\n"
            "  2. 命令行参数：python3 scripts/send_email.py setup "
            "--sender X@qq.com --password 授权码 --receiver Y@qq.com\n"
            "  3. 关闭邮件：python3 scripts/send_email.py setup --no-email",
            file=sys.stderr,
        )
        return

    receivers = args.receiver if isinstance(args.receiver, list) else [args.receiver]

    config = {
        "smtp": {
            "server": args.server,
            "port": args.port,
            "use_ssl": True,
            "sender": args.sender,
            "password": args.password,
            "receiver": receivers,
        },
        "enabled": True,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"[OK] 邮件配置已保存，收件人: {', '.join(receivers)}")


def cmd_check(args):
    """检查邮件配置状态"""
    if not CONFIG_FILE.exists():
        print("NOT_CONFIGURED")
        if getattr(args, 'verbose', False):
            print(
                "→ 运行以下任一命令开始：\n"
                "    python3 scripts/send_email.py setup --interactive\n"
                "    python3 scripts/send_email.py setup --no-email",
                file=sys.stderr,
            )
        return
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    if not config.get("enabled", False):
        print("DISABLED")
        if getattr(args, 'verbose', False):
            print(
                "→ 邮件已关闭。如需重新启用，跑 setup --interactive",
                file=sys.stderr,
            )
    else:
        print("CONFIGURED")
        if getattr(args, 'verbose', False):
            smtp = config.get("smtp", {})
            recv = smtp.get("receiver", [])
            if isinstance(recv, str):
                recv = [recv]
            print(
                f"→ 发件: {smtp.get('sender', '?')}\n"
                f"→ 收件: {', '.join(recv) or '?'}",
                file=sys.stderr,
            )


def main():
    parser = argparse.ArgumentParser(description="邮件发送工具 — Smart Invest Skill")
    sub = parser.add_subparsers(dest="command")

    p_send = sub.add_parser("send", help="发送报告邮件")
    p_send.add_argument("--subject", "-s", required=True, help="邮件主题")
    p_send.add_argument("--file", "-f", help="报告文件路径（不指定则从 stdin 读取）")

    p_trade = sub.add_parser("trade-notify", help="发送交易通知邮件")
    p_trade.add_argument("--action", "-a", required=True, choices=["buy", "sell"], help="交易方向")
    p_trade.add_argument("--code", "-c", required=True, help="基金代码")
    p_trade.add_argument("--name", "-n", required=True, help="基金名称")
    p_trade.add_argument("--amount", required=True, help="交易金额")
    p_trade.add_argument("--nav", required=True, help="成交净值")
    p_trade.add_argument("--shares", required=True, help="交易份额")
    p_trade.add_argument("--note", default="", help="备注（规则名，无 --reason 时作操作依据）")
    p_trade.add_argument("--reason", help="操作依据（为什么买/卖，引擎 reason_zh 或叙事）")
    p_trade.add_argument("--news", action="append", default=[],
                         help="相关要闻（可多次传入，每次一条）")
    p_trade.add_argument("--wallet", help="操作后钱包一行（如 总 ¥X｜现金 ¥Y）")

    sub.add_parser("test", help="发送测试邮件")

    sub.add_parser("flush-outbox", help="补发待发队列里发失败的邮件")

    p_setup = sub.add_parser("setup", help="配置邮件")
    p_setup.add_argument("--sender", help="发件邮箱")
    p_setup.add_argument("--password", help="SMTP 授权码")
    p_setup.add_argument("--receiver", nargs="+", help="收件邮箱（支持多个）")
    p_setup.add_argument("--server", default="smtp.qq.com", help="SMTP 服务器")
    p_setup.add_argument("--port", type=int, default=465, help="SMTP 端口")
    p_setup.add_argument("--no-email", action="store_true", help="关闭邮件功能")
    p_setup.add_argument(
        "--interactive", "-i", action="store_true",
        help="交互式向导（命令行直接跑时推荐）",
    )

    p_check = sub.add_parser("check", help="检查邮件配置状态")
    p_check.add_argument(
        "--verbose", "-v", action="store_true",
        help="额外打印行动建议或当前配置摘要",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "send":
        cmd_send(args)
    elif args.command == "trade-notify":
        cmd_trade_notify(args)
    elif args.command == "test":
        cmd_test(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "flush-outbox":
        cmd_flush_outbox(args)


if __name__ == "__main__":
    main()
