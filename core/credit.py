"""本学期选课学分要求（从选课首页 Index 解析）。

⚠️ **千万不要直接读页面上显示的那几个 `<font>`** —— 我校实测，服务端返回的
原始 HTML 里它们全是**空壳占位**：

    <font id="xkxn"></font> 学年 <font id="xkxq"></font> 学期 <font id="txt_xklc"></font>
    <span id="sysj"></span>
    本学期选课要求 总学分最低 <font color="red">0</font> 最高 <font color="red">30</font>
    本学期已选学分 <font color="red" id="yxxfs">0</font>     ← 恒为 0！

真正的值在**隐藏域**里，由 `zzxkYzbZy.js` 在浏览器端填进去（`$("#yxxfs").text($("#zxfs").val())`）。
所以只有用 CDP 在浏览器里 dump 页面才会看到「28.0」这种数字；
直接 HTTP GET 拿到 0，会得出「已选 0 学分」这种完全错误的结论。

实测的字段对应（2026-09-29）：

| 隐藏域 | 值 | 含义 |
|---|---|---|
| `zxfs` | `28.0` | 本学期已选学分 |
| `xkzgxf` | `30` | 选课资格（最高）学分 |
| `xkxnmc` | `2026-2027` | 学年 |
| `xkxqmc` | `1` | 学期 |
| `xkxfqzfs` | `0` | 学分取值口径开关（0 → 显示到 `yxxfs`，1 → 显示到 `yxxfs_jxb`） |

只有「总学分最低」没有对应隐藏域，它是服务端直接渲染的，所以仍按文案解析。

这一块为什么重要：**「已选 + 待选 > 最高」就是教务驳回的最常见原因**。
本工具实测收到过「超过本学期最高选课学分限制，不可选！」→ 归 UNKNOWN → 直接终止
（见 core/errors.py 的说明）。提前把剩余额度摆出来，用户就不必靠撞墙才发现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 作用域锚点。「总学分最低」只在这一小段里找，避免误匹配页面别处的字样。
_SCOPE_ANCHOR = "本学期选课要求"
_SCOPE_LEN = 2000

# ---- 首选：隐藏域（可靠，不随页面排版变）----------------------------------
_H_YEAR = ("xkxnmc",)           # 学年：2026-2027
_H_TERM = ("xkxqmc",)           # 学期：1
_H_ROUND = ("xklcmc",)          # 轮次名（选课期才有，平时可能为空）
_H_USED = ("zxfs",)             # 已选学分
_H_MAX = ("xkzgxf",)            # 选课资格（最高）学分
_H_START = ("xkkssj",)          # 选课开始时间
_H_END = ("xkjssj",)            # 选课结束时间

# ---- 兜底：页面上渲染后的文本（浏览器 dump 才有值，仅作后备）---------------
_RE_YEAR = re.compile(r'id="xkxn"[^>]*>\s*([^<]*?)\s*<')
_RE_TERM = re.compile(r'id="xkxq"[^>]*>\s*([^<]*?)\s*<')
_RE_ROUND = re.compile(r'id="txt_xklc"[^>]*>\s*(?:<[^>]*>\s*)*([^<]+)')
_RE_TIME_SPAN = re.compile(r'id="sysj"[^>]*>(.*?)</span>', re.S)
_RE_TAGS = re.compile(r"<[^>]*>")

_NUM = r"([0-9]+(?:\.[0-9]+)?)"
_RE_MIN = re.compile(r"总学分最低[\s\S]{0,160}?<font[^>]*>\s*" + _NUM + r"\s*</font>")
_RE_MAX = re.compile(r"最高[\s\S]{0,120}?<font[^>]*>\s*" + _NUM + r"\s*</font>")
_RE_USED_TEXT = re.compile(r'id="yxxfs"[^>]*>\s*' + _NUM + r"\s*<")


def _txt(s: str) -> str:
    """去标签、去 &nbsp;、压缩空白。"""
    s = _RE_TAGS.sub("", s or "")
    s = s.replace("&nbsp;", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _f(v) -> float | None:
    """宽松转 float：None / 空串 / 非数字一律 None（绝不用 0 冒充）。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pick(html: str, pattern: re.Pattern) -> str:
    """取第一个捕获组并清洗；没匹配到返回空串。"""
    m = pattern.search(html or "")
    return _txt(m.group(1)) if m else ""


@dataclass
class CreditInfo:
    """本学期学分要求。任何一项没解析到都是 None（绝不用 0 冒充）。"""

    year: str = ""          # 2026-2027
    term: str = ""          # 1
    round_name: str = ""    # 第1轮
    time_text: str = ""     # 2026-09-29 13:00:00 - 2026-09-29 19:00:00
    min_credit: float | None = None
    max_credit: float | None = None
    used_credit: float | None = None
    xfqzfs: str = ""        # 学分口径开关（xkxfqzfs）：0=按课程学分，1=按教学班学分
    found: bool = False     # 是否真的在页面上找到了「本学期选课要求」那一块

    @property
    def remain_credit(self) -> float | None:
        """剩余可选学分 = 最高 - 已选。任一未知则返回 None。

        刻意**不**把负值截成 0：教务允许已选超过上限（先选后退的场景真实存在），
        显示成 0 会掩盖「你现在已经超了」这个事实。
        """
        if self.max_credit is None or self.used_credit is None:
            return None
        return round(self.max_credit - self.used_credit, 2)

    def as_dict(self) -> dict:
        return {
            "year": self.year,
            "term": self.term,
            "round": self.round_name,
            "time_text": self.time_text,
            "min": self.min_credit,
            "max": self.max_credit,
            "used": self.used_credit,
            "remain": self.remain_credit,
            "found": self.found,
        }


def parse_credit(html: str, hidden: dict[str, str] | None = None) -> CreditInfo:
    """从选课首页解析学分要求。

    `hidden` 建议传入 `parse_hidden_inputs(html)` 的结果 —— 学分那几项的真值都在
    隐藏域里（页面上显示的 `<font>` 是空壳，见模块开头的说明）。
    不传也能用，只是会退化成读页面文本，在原始 HTTP 响应下会读到 0。
    """
    info = CreditInfo()
    if not html:
        return info

    h = hidden or {}

    def hv(*keys: str) -> str:
        """按顺序取第一个非空隐藏域值。"""
        for k in keys:
            v = str(h.get(k) or "").strip()
            if v:
                return v
        return ""

    # 学年 / 学期 / 轮次 / 选课时间：隐藏域优先，文本兜底
    info.year = hv(*_H_YEAR) or _pick(html, _RE_YEAR)
    info.term = hv(*_H_TERM) or _pick(html, _RE_TERM)
    info.round_name = hv(*_H_ROUND) or _pick(html, _RE_ROUND)
    info.xfqzfs = hv("xkxfqzfs")

    start, end = hv(*_H_START), hv(*_H_END)
    if start or end:
        info.time_text = f"{start} - {end}".strip(" -")
    else:
        m = _RE_TIME_SPAN.search(html)
        if m:
            t = _txt(m.group(1))
            # 页面写的是「（选课时间：…）」，只要冒号后面那段，顺带剥掉收尾括号
            info.time_text = (t.split("：", 1)[1] if "：" in t else t).strip("（）() ")

    # 已选 / 最高：隐藏域是唯一可靠来源
    info.used_credit = _f(hv(*_H_USED))
    info.max_credit = _f(hv(*_H_MAX))

    # 最低 / 最高：页面文本（最高仅在隐藏域缺失时采用）
    at = html.find(_SCOPE_ANCHOR)
    if at >= 0:
        info.found = True
        scope = html[at : at + _SCOPE_LEN]
        m = _RE_MIN.search(scope)
        if m:
            info.min_credit = _f(m.group(1))
        if info.max_credit is None:
            m = _RE_MAX.search(scope)
            if m:
                info.max_credit = _f(m.group(1))
    elif hidden:
        # 没找到锚点但隐藏域里有数据 —— 也算拿到了，别让 found 卡死界面
        info.found = bool(info.used_credit is not None or info.max_credit is not None)

    # 最后的兜底：读页面上那个（几乎是 0 的）显示值。
    # 只有在隐藏域完全缺失时才用，避免把「占位 0」当成真实已选学分。
    if info.used_credit is None:
        m = _RE_USED_TEXT.search(html)
        if m:
            info.used_credit = _f(m.group(1))

    return info
