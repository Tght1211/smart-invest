#!/usr/bin/env python3
"""终端走势图 + 邮件 sparkline 渲染（纯 stdlib，无网络）。

- render_chart(): asciichart 风格折线图，给 fetch_fund.py chart 子命令用
- spark_html(): 邮件安全的迷你走势条（table+div，QQ/163 邮箱可渲染），
  给 send_email.py 的 :::spark DSL 用

红涨绿跌：整段涨(末值>=首值)用红 #E64340，跌用绿 #09BB07。
"""

UP = "#E64340"
DN = "#09BB07"


def downsample(values, target):
    """等距降采样到 target 个点，保留首尾。"""
    n = len(values)
    if n <= target:
        return list(values)
    if target == 1:
        return [values[-1]]
    return [values[round(i * (n - 1) / (target - 1))] for i in range(target)]


def render_chart(values, height=12, width=84, label_fmt="{:>10.2f}"):
    """折线图字符画。返回 height 行图体 + 1 行底轴。"""
    series = downsample(list(values), width)
    lo, hi = min(series), max(series)
    rng = hi - lo
    if rng == 0:
        rng = abs(hi) or 1.0
        lo = hi - rng
    ratio = (height - 1) / rng
    mn = round(lo * ratio)
    w = len(series)
    grid = [[" "] * w for _ in range(height)]

    def row(v):
        return height - 1 - (round(v * ratio) - mn)

    for x in range(w):
        if x == 0:
            grid[row(series[0])][0] = "─"
            continue
        y0, y1 = row(series[x - 1]), row(series[x])
        if y0 == y1:
            grid[y0][x] = "─"
        else:
            grid[y1][x] = "╮" if y0 > y1 else "╯"
            grid[y0][x] = "╰" if y0 > y1 else "╭"
            for yy in range(min(y0, y1) + 1, max(y0, y1)):
                grid[yy][x] = "│"

    out = []
    for r in range(height):
        label_val = (mn + (height - 1 - r)) / ratio
        out.append(label_fmt.format(label_val) + " ┤" + "".join(grid[r]))
    pad = len(label_fmt.format(0.0)) + 1
    out.append(" " * pad + "└" + "─" * w)
    return "\n".join(out)


def axis_line(times, width, indent):
    """底轴时间标注行：左/中/右三个刻度。times 为字符串列表。"""
    if not times:
        return ""
    axis = [" "] * width
    marks = [(0.0, times[0]), (0.5, times[len(times) // 2]), (1.0, times[-1])]
    for frac, t in marks:
        pos = min(int(frac * width), width - len(t))
        for i, ch in enumerate(t):
            if 0 <= pos + i < width:
                axis[pos + i] = ch
    return " " * indent + "".join(axis)


def spark_html(values, bars=60, height=36, color=None):
    """邮件安全 sparkline：单行 table，每个 td 底对齐一个色块 div。"""
    series = downsample([float(v) for v in values], bars)
    if not series:
        return ""
    if color is None:
        color = UP if series[-1] >= series[0] else DN
    lo, hi = min(series), max(series)
    rng = hi - lo or 1.0
    tds = []
    for v in series:
        h = max(2, round((v - lo) / rng * (height - 2)) + 2)
        tds.append(
            f'<td style="vertical-align:bottom;padding:0 1px;">'
            f'<div style="height:{h}px;background-color:{color};'
            f'border-radius:1px;font-size:0;line-height:0;">&nbsp;</div></td>'
        )
    return (
        f'<table cellpadding="0" cellspacing="0" width="100%" '
        f'style="table-layout:fixed;height:{height}px;">'
        f'<tr>{"".join(tds)}</tr></table>'
    )
