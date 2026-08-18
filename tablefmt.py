"""终端对齐表格渲染（零依赖）。

Markdown 表格在终端里不换行不对齐，人眼难读。这里按「显示宽度」逐列补齐：
CJK 全宽字符按 2 格、零宽/组合符按 0 格，数字列右对齐，中文内容对齐不歪。

用法:
    from tablefmt import render_table
    print(render_table(
        [["#", "商品", "利润"], ["1", "Quartz", "108.5W"]],
        aligns="lrl"))   # l=左 r=右 c=中，每列一个字符
"""

import unicodedata

# 少数字符的 East Asian Width 是 Ambiguous，但终端渲染宽度是确定的：
# ⚠ 带 VS16 时按宽字符渲染，✓ 在多数等宽字体里是窄字符。
_WIDE_OVERRIDES = {"\u26a0": 2}
_NARROW_OVERRIDES = {"\u2713": 1, "\ufe0f": 0}


def disp_w(ch):
    """单个字符的终端显示宽度（列）。"""
    if ch in _NARROW_OVERRIDES:
        return _NARROW_OVERRIDES[ch]
    if ch in _WIDE_OVERRIDES:
        return _WIDE_OVERRIDES[ch]
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_len(s):
    """字符串的终端显示宽度（CJK 按 2 格）。"""
    return sum(disp_w(c) for c in (s or ""))


def clip(s, width):
    """按显示宽度截断，超宽时去尾补 …。"""
    s = s or ""
    if disp_len(s) <= width:
        return s
    out, n = [], 0
    for c in s:
        w = disp_w(c)
        if n + w > width - 1:
            break
        out.append(c)
        n += w
    return "".join(out) + "…"


def render_table(rows, aligns):
    """渲染对齐文本表。rows 首行为表头；aligns 每列一个字符：l/r/c。"""
    cols = len(aligns)
    widths = [max(disp_len(r[j]) for r in rows) for j in range(cols)]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = []
    for i, r in enumerate(rows):
        cells = []
        for j in range(cols):
            t = clip(r[j], widths[j])
            gap = widths[j] - disp_len(t)
            a = aligns[j]
            if a == "r":
                t = " " * gap + t
            elif a == "c":
                t = " " * (gap // 2) + t + " " * (gap - gap // 2)
            else:
                t = t + " " * gap
            cells.append(" " + t + " ")
        out.append("|" + "|".join(cells) + "|")
        if i == 0:
            out.append(sep)
    out.append(sep)
    return "\n".join(out)
