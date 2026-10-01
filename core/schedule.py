"""课表与时间冲突：把教务返回的「上课时间」原文解析成结构化时段。

这一层是**纯函数**——不碰 HTTP、不碰界面，只做字符串 ↔ 结构体的翻译。

## 字段来源（2026-09-29 我校实测，全部有原始响应佐证）

已选课程列表 `zzxkyzb_cxZzxkYzbChoosedDisplay.html` 的每一行（实测 55 列）都带：

| 字段 | 实测样例 | 含义 |
|---|---|---|
| `sksj` | `星期三第3-5节{1-17周}<br/>星期六第3-5节{4周}` | 上课时间，`<br/>` 分多段 |
| `jxdd` | `11-403<br/>9-401` | 上课地点，**按下标与 sksj 各段一一对应** |
| `jsxx` | `13059/杨小松/讲师` | 教师，`工号/姓名/职称`；多人用 `;` 分隔 |
| `zixf` | `1` / `0` | 「自选否」列：1=自选上、0=系统调整 |
| `sxbj` | `1` | 「选上否」列：1=已选上 |

教学班级列表 `zzxkyzbjk_cxJxbWithKchZzxkYzb.html` 的每行同样带 `sksj` / `jxdd` / `jsxx`。

反向验证方法（可复现）：选课首页 `zzxkyzb_cxZzxkYzbIndex.html` 的服务端渲染结果里，
每门课的面板含 `<p class="zixf">系统调整</p>` / `<p class="zixf">自选上</p>`；
把 12 门课的该文案与 JSON 各字段逐一比对，**只有 `zixf` 能一一对上**
（`1`→自选上、`0`→系统调整）。`jsxx` 的拆解规则来自
`zzxkYzbChoosedZy.js` 中 `modelA.jsxx.split(";")` → `tmpArray[1]`/`tmpArray[2]`。

铁律 #1：以上只是**格式契约**，不含任何学校专有常量；解析器对未知写法一律容错降级。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# 「能不能退这门课」由教务说了算，判定逻辑单独放在 core/drop.py（纯函数、零依赖）。
# 这里只是把它算出来的结果挂到 ScheduleEntry 上，让界面层一次就能拿到，
# 不必自己再回头去翻原始行。方向是 schedule → drop，而 drop 不反向依赖任何模块。
from core.drop import drop_state

# ---------------------------------------------------------------------------
# 基础表
# ---------------------------------------------------------------------------

WEEKDAY_CN: dict[int, str] = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
_WEEKDAY_ALIAS: dict[str, int] = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

# `<br/>`、换行、分号都可能是多时段之间的分隔符（不同页面写法不同）
_RE_SPLIT = re.compile(r"<br\s*/?>|\r?\n|[;；]", re.I)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WEEKDAY = re.compile(r"星期([一二三四五六日天])")
_RE_JIE = re.compile(r"第\s*(\d+)\s*(?:[-~－—]\s*(\d+))?\s*节")
_RE_BRACE = re.compile(r"\{([^}]*)\}")
_RE_NUM = re.compile(r"(\d+)\s*(?:[-~－—]\s*(\d+))?")

# zixf「自选否」列：实测 1=自选上、0=系统调整
ZIXF_TEXT: dict[str, str] = {"1": "自选上", "0": "系统调整"}
SXBJ_TEXT: dict[str, str] = {"1": "已选上", "0": "未选中"}


# ---------------------------------------------------------------------------
# 时段
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """一个上课时段：星期几、第几节到第几节、哪些周。

    `weeks is None` 表示原文没写周次（信息缺失），冲突判定时**保守视为重叠**。
    """

    weekday: int  # 1=周一 … 7=周日
    start: int  # 起始节
    end: int  # 结束节
    weeks: tuple[int, ...] | None = None
    raw: str = ""

    @property
    def weekday_cn(self) -> str:
        return "周" + WEEKDAY_CN.get(self.weekday, "?")

    @property
    def jie_text(self) -> str:
        return f"{self.start}节" if self.start == self.end else f"{self.start}-{self.end}节"

    @property
    def weeks_text(self) -> str:
        """压缩显示：{1,2,3,5,6,7} → `1-3,5-7周`。"""
        if not self.weeks:
            return ""
        runs: list[tuple[int, int]] = []
        lo = hi = self.weeks[0]
        for w in self.weeks[1:]:
            if w == hi + 1:
                hi = w
            else:
                runs.append((lo, hi))
                lo = hi = w
        runs.append((lo, hi))
        parts = [f"{a}" if a == b else f"{a}-{b}" for a, b in runs]
        return ",".join(parts) + "周"

    @property
    def text(self) -> str:
        """人类可读：`周三 3-5节 · 1-17周`。"""
        base = f"{self.weekday_cn} {self.jie_text}"
        return f"{base} · {self.weeks_text}" if self.weeks_text else base

    def as_dict(self) -> dict:
        return {
            "weekday": self.weekday,
            "weekday_cn": self.weekday_cn,
            "start": self.start,
            "end": self.end,
            "weeks": list(self.weeks) if self.weeks else None,
            "weeks_text": self.weeks_text,
            "jie_text": self.jie_text,
            "text": self.text,
            # 原文一起带上：前端往返（清单项存时段再回传）时能无损还原，
            # 出问题时也有据可查
            "raw": self.raw,
        }


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def split_parts(raw: str) -> list[str]:
    """把 `<br/>`/换行/分号分隔的多段文本切成干净的片段。"""
    if not raw:
        return []
    out = []
    for piece in _RE_SPLIT.split(str(raw)):
        piece = _RE_TAG.sub("", piece).strip()
        if piece:
            out.append(piece)
    return out


def parse_weeks(text: str) -> tuple[int, ...] | None:
    """从 `{1-17周}` / `{4周}` / `{1,3,5-7周}` 里取出周次集合。

    找不到 `{}` 块时，会退回扫描整段里带「周」的区间；都没有则返回 None。
    """
    blocks = _RE_BRACE.findall(text or "")
    haystack = " ".join(blocks) if blocks else (text or "")
    if "周" not in haystack and not blocks:
        return None
    weeks: set[int] = set()
    for m in _RE_NUM.finditer(haystack):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if b < a:
            a, b = b, a
        if b - a > 60:  # 明显不是周次（例如误抓到年份）
            continue
        weeks.update(range(a, b + 1))
    return tuple(sorted(w for w in weeks if 1 <= w <= 60)) or None


def parse_sksj(raw: str) -> list[Slot]:
    """把上课时间原文解析成 Slot 列表。

    容忍的写法（全部来自真实响应或其合理变体）：
      - `星期三第3-5节{1-17周}`
      - `星期一第14-15节{1-9周}`
      - `星期二第12-14节{1-17周}<br/>星期三第8-10节{14周}`
      - 缺周次：`星期三第3-5节`
      - 缺节次范围：`星期三第3节`

    解析不出「星期 + 节次」的片段会被**丢弃**（宁可少画一格，也不画错）。
    """
    slots: list[Slot] = []
    for piece in split_parts(raw):
        wd = _RE_WEEKDAY.search(piece)
        jie = _RE_JIE.search(piece)
        if not wd or not jie:
            continue
        weekday = _WEEKDAY_ALIAS.get(wd.group(1), 0)
        start = int(jie.group(1))
        end = int(jie.group(2)) if jie.group(2) else start
        if end < start:
            start, end = end, start
        slots.append(
            Slot(weekday=weekday, start=start, end=end, weeks=parse_weeks(piece), raw=piece)
        )
    return slots


def parse_jxdd(raw: str) -> list[str]:
    """上课地点，按 `<br/>` 分段。与 sksj 各段按下标对应。"""
    return split_parts(raw)


def parse_teachers(jsxx: str) -> list[tuple[str, str, str]]:
    """教师原文 → [(工号, 姓名, 职称), ...]。多人用 `;` 分隔。"""
    out: list[tuple[str, str, str]] = []
    for piece in str(jsxx or "").split(";"):
        piece = piece.strip()
        if not piece:
            continue
        parts = [p.strip() for p in piece.split("/")]
        code = parts[0] if len(parts) > 0 else ""
        name = parts[1] if len(parts) > 1 else ""
        title = parts[2] if len(parts) > 2 else ""
        out.append((code, name, title))
    return out


def teacher_names(jsxx: str) -> str:
    """`工号/姓名/职称;…` → `姓名、姓名`。"""
    return "、".join(n for _, n, _ in parse_teachers(jsxx) if n)


def teacher_titles(jsxx: str) -> str:
    """职称串，重复的合并（多人同职称时只留一个）。"""
    titles = [t for _, _, t in parse_teachers(jsxx) if t]
    uniq: list[str] = []
    for t in titles:
        if t not in uniq:
            uniq.append(t)
    return "、".join(uniq)


def clean_text(raw: str) -> str:
    """把多段原文压成单行展示文本：`11-403<br/>9-401` → `11-403 / 9-401`。"""
    return " / ".join(split_parts(raw))


def zixf_text(value) -> str:
    """「自选否」列文案。未知取值返回空串（并保留原值在调用方）。"""
    return ZIXF_TEXT.get(str(value or "").strip(), "")


def sxbj_text(value) -> str:
    """「选上否」列文案。"""
    return SXBJ_TEXT.get(str(value or "").strip(), "")


# ---------------------------------------------------------------------------
# 冲突判定
# ---------------------------------------------------------------------------


def jie_overlap(a: Slot, b: Slot) -> bool:
    """星期相同且节次有交集（**不看周次**）。"""
    return a.weekday == b.weekday and a.start <= b.end and b.start <= a.end


def weeks_overlap(a: Slot, b: Slot) -> bool:
    """周次有交集。任一方周次未知时**保守返回 True**——宁可提示，不可漏判。"""
    if a.weeks is None or b.weeks is None:
        return True
    return bool(set(a.weeks) & set(b.weeks))


def is_hard_conflict(a: Slot, b: Slot) -> bool:
    """硬冲突 = 星期 + 节次 + **周次** 三者都重叠。这种课同时上不了。"""
    return jie_overlap(a, b) and weeks_overlap(a, b)


def is_soft_conflict(a: Slot, b: Slot) -> bool:
    """软冲突 = 节次占位重叠但**周次错开** —— 实际不撞。

    例：`周三第8-10节{7周}` vs `周三第8-10节{10周}`。
    只做提示、**不参与互斥跳过**（跳过它等于白白放弃一门能上的课）。
    之所以还要提示：我校教务在提交时自己也查冲突，判定口径可能只看到节次。
    """
    return jie_overlap(a, b) and not weeks_overlap(a, b)


def is_time_conflict(a: Slot, b: Slot) -> bool:
    """两节课是否**真的**撞在一起：星期相同 + 节次有交集 + **周次有交集**。

    这是「清单内部互斥」的判定口径：

    - **节次**不要求范围完全一致 —— `周二 3-5节` 与 `周二 4-5节` 共享 4、5 节，算撞；
    - **周次**必须有交集 —— `周二 3-5节{1-5周}` 与 `周二 3-5节{6-17周}` 永远不撞，
      抢到前者之后不该跳过后者（用户 2026-09-29 明确要求）；
    - 任一方周次**未知**（原文没写 `{}`）→ `weeks_overlap` 保守返回 True ——
      宁可多拦一项，不可漏判。

    等价于 `is_hard_conflict`；保留两个名字是因为调用点的语义侧重不同：
    `is_time_conflict` 用于「互斥 / 跳过」，`is_hard_conflict` 用于「界面提示分级」
    （与它成对的 `is_soft_conflict` 表示「节次占位重叠、周次错开」，只提示不跳过）。
    """
    return jie_overlap(a, b) and weeks_overlap(a, b)


def slots_conflict(xs: Iterable[Slot], ys: Iterable[Slot]) -> bool:
    """两组时段是否存在任意一处**真正撞上**（星期+节次+周次）。"""
    return any(is_time_conflict(a, b) for a in xs for b in ys)


@dataclass(frozen=True)
class ConflictHit:
    """一次冲突：候选时段的哪一段，撞上了谁的哪一段。"""

    level: str  # "hard" | "soft"
    slot: Slot  # 候选的时段
    other: Slot  # 对方的时段
    kch_id: str = ""
    kcmc: str = ""
    source: str = ""  # "selected"（已选） | "pending"（清单）

    @property
    def source_cn(self) -> str:
        return {"selected": "已选", "pending": "清单"}.get(self.source, self.source)

    @property
    def detail(self) -> str:
        # hard（周次也重叠）= 真上不了；soft（周次错开）= 只是节次占位重叠
        tag = "时间冲突" if self.level == "hard" else "节次重叠（周次错开）"
        return f"{tag}：{self.slot.text} ←→ {self.source_cn}「{self.kcmc}」{self.other.text}"

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "slot": self.slot.as_dict(),
            "other": self.other.as_dict(),
            "kch_id": self.kch_id,
            "kcmc": self.kcmc,
            "source": self.source,
            "source_cn": self.source_cn,
            "detail": self.detail,
        }


def find_conflicts(
    candidate: list[Slot],
    others: list["ScheduleEntry"],
    *,
    exclude_kch: str = "",
) -> list[ConflictHit]:
    """候选时段 vs 一批已有安排，返回所有冲突命中（hard + soft，供**提示**用）。

    注意这是「提示口径」：只要节次占位重叠就报（含周次错开的 soft）。
    「要不要跳过提交」得用 `is_time_conflict`（要求周次也重叠），两者别混用 ——
    提示宁可啰嗦，跳过必须精准。

    `exclude_kch`：同一门课自己跟自己不算冲突（铁律 #6：同一门课只押一个班）。
    """
    hits: list[ConflictHit] = []
    for entry in others:
        if exclude_kch and entry.kch_id and entry.kch_id == exclude_kch:
            continue
        for a in candidate:
            for b in entry.slots:
                if is_hard_conflict(a, b):
                    hits.append(ConflictHit("hard", a, b, entry.kch_id, entry.kcmc, entry.source))
                elif is_soft_conflict(a, b):
                    hits.append(ConflictHit("soft", a, b, entry.kch_id, entry.kcmc, entry.source))
    # 硬冲突排前面
    hits.sort(key=lambda h: 0 if h.level == "hard" else 1)
    return hits


def max_jie(entries: list["ScheduleEntry"]) -> int:
    """所有安排里出现过的最大节次（用来决定课表要画多少行）。"""
    top = 0
    for e in entries:
        for s in e.slots:
            top = max(top, s.end)
    return top


# ---------------------------------------------------------------------------
# 一条「占时间」的记录
# ---------------------------------------------------------------------------


@dataclass
class ScheduleEntry:
    """已选课 or 待选教学班 —— 只要它能占住课表上的格子，就是这个结构。"""

    kch_id: str = ""
    kch: str = ""
    kcmc: str = ""
    jxbmc: str = ""
    teachers: list[tuple[str, str, str]] = field(default_factory=list)
    sksj: str = ""
    jxdd: list[str] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    xf: str = ""
    zixf: str = ""
    zixf_cn: str = ""
    sxbj: str = ""
    sxbj_cn: str = ""
    kklxdm: str = ""
    kklxmc: str = ""
    do_id: str = ""
    jxb_id: str = ""
    yxzrs: str = ""
    jxbrs: str = ""
    source: str = "selected"  # selected | pending

    # 退课资格（只有 source="selected" 的行才有意义）。
    # can_drop 的含义严格等于「教务已选列表给这一行渲染的是『退课』按钮而不是『已选』」，
    # 详见 core/drop.py。教学班行默认 False —— 它们本来就不是已选，无从退起。
    can_drop: bool = False
    drop_block: str = ""  # 不可退的原因；can_drop 为 True 时为空

    @property
    def teacher_names(self) -> str:
        return "、".join(n for _, n, _ in self.teachers if n)

    @property
    def teacher_titles(self) -> str:
        titles: list[str] = []
        for _, _, t in self.teachers:
            if t and t not in titles:
                titles.append(t)
        return "、".join(titles)

    @property
    def teacher_text(self) -> str:
        """`杨小松（讲师）`；没有职称时只显示姓名。"""
        name, title = self.teacher_names, self.teacher_titles
        if name and title and title != "无":
            return f"{name}（{title}）"
        return name or "教师待定"

    @property
    def sksj_text(self) -> str:
        return clean_text(self.sksj)

    @property
    def jxdd_text(self) -> str:
        return " / ".join(self.jxdd)

    def as_dict(self) -> dict:
        return {
            "kch_id": self.kch_id,
            "kch": self.kch,
            "kcmc": self.kcmc,
            "jxbmc": self.jxbmc,
            "jsxx": self.teacher_text,
            "teacher_names": self.teacher_names,
            "teacher_titles": self.teacher_titles,
            "sksj": clean_text(self.sksj),
            "sksj_raw": self.sksj,
            "jxdd": self.jxdd,
            "jxdd_text": self.jxdd_text,
            "slots": [s.as_dict() for s in self.slots],
            "xf": self.xf,
            "zixf": self.zixf,
            "zixf_text": self.zixf_cn,
            "sxbj": self.sxbj,
            "sxbj_text": self.sxbj_cn,
            "kklxdm": self.kklxdm,
            "kklxmc": self.kklxmc,
            "do_id": self.do_id,
            "jxb_id": self.jxb_id,
            "yxzrs": self.yxzrs,
            "jxbrs": self.jxbrs,
            "source": self.source,
            # 界面据此决定卡片上显示「退课」按钮还是「已选」二字
            "can_drop": self.can_drop,
            "drop_block": self.drop_block,
        }


# 兼容不同接口的字段别名（已选列表用 kch_id/xf/jxdd，教学班级用 kch_id/jxbxf/…）
_ALIASES: dict[str, tuple[str, ...]] = {
    "kch_id": ("kch_id", "t_kch_id", "kch"),
    "kch": ("kch",),
    "kcmc": ("kcmc",),
    "jxbmc": ("jxbmc",),
    "jsxx": ("jsxx", "jsxm"),
    "sksj": ("sksj",),
    "jxdd": ("jxdd",),
    "xf": ("xf", "jxbxf"),
    "zixf": ("zixf",),
    "sxbj": ("sxbj",),
    "kklxdm": ("kklxdm",),
    "kklxmc": ("kklxmc", "kkxymc"),
    "do_id": ("do_id", "do_jxb_id"),
    "jxb_id": ("jxb_id",),
    "yxzrs": ("yxzrs",),
    "jxbrs": ("jxbrs", "jxbrl"),
}


def _pick(raw: dict, key: str) -> str:
    for k in _ALIASES.get(key, (key,)):
        if k in raw and raw[k] not in (None, ""):
            return str(raw[k])
    return ""


def build_entry(
    raw: dict, *, source: str = "selected", page: dict | None = None
) -> ScheduleEntry:
    """从任意一种教务行（已选行 / 教学班行）构建 ScheduleEntry。

    `page` 是页面级隐藏域（Index 页那套，至少含 `xxdm`）——退课资格判定要用它，
    不传则退课资格一律判为不可退（保守兜底：宁可少给一个按钮，也不能给错一个）。
    """
    kch_id = _pick(raw, "kch_id")
    sksj = _pick(raw, "sksj")
    st = drop_state(raw, page)
    return ScheduleEntry(
        kch_id=kch_id,
        kch=_pick(raw, "kch"),
        kcmc=_pick(raw, "kcmc"),
        jxbmc=_pick(raw, "jxbmc"),
        teachers=parse_teachers(_pick(raw, "jsxx")),
        sksj=sksj,
        jxdd=parse_jxdd(_pick(raw, "jxdd")),
        slots=parse_sksj(sksj),
        xf=_pick(raw, "xf"),
        zixf=_pick(raw, "zixf"),
        zixf_cn=zixf_text(_pick(raw, "zixf")),
        sxbj=_pick(raw, "sxbj"),
        sxbj_cn=sxbj_text(_pick(raw, "sxbj")),
        kklxdm=_pick(raw, "kklxdm"),
        kklxmc=_pick(raw, "kklxmc"),
        do_id=_pick(raw, "do_id"),
        jxb_id=_pick(raw, "jxb_id"),
        yxzrs=_pick(raw, "yxzrs"),
        jxbrs=_pick(raw, "jxbrs"),
        source=source,
        can_drop=st.allowed,
        drop_block=st.reason,
    )


def entries_from_rows(
    rows: list[dict], *, source: str = "selected", page: dict | None = None
) -> list[ScheduleEntry]:
    return [build_entry(r, source=source, page=page) for r in (rows or [])]


# ---------------------------------------------------------------------------
# Slot ↔ dict（前端来回传时段用，例如清单项要记住自己占哪几格）
# ---------------------------------------------------------------------------


def slot_from_dict(d: dict) -> Slot | None:
    """把 `Slot.as_dict()` 的产物还原成 Slot。字段缺失/非法时返回 None。"""
    if not isinstance(d, dict):
        return None
    try:
        weekday = int(d.get("weekday") or 0)
        start = int(d.get("start") or 0)
        end = int(d.get("end") or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= weekday <= 7) or start < 1 or end < start:
        return None
    weeks_raw = d.get("weeks")
    weeks: tuple[int, ...] | None = None
    if isinstance(weeks_raw, (list, tuple)) and weeks_raw:
        try:
            weeks = tuple(sorted({int(w) for w in weeks_raw}))
        except (TypeError, ValueError):
            weeks = None
    return Slot(
        weekday=weekday,
        start=start,
        end=end,
        weeks=weeks,
        raw=str(d.get("raw") or ""),
    )


def slots_from_dicts(items) -> list[Slot]:
    """批量还原，静默丢弃非法项（宁可少画一格，也不让整个请求挂掉）。"""
    out: list[Slot] = []
    for d in items or []:
        if isinstance(d, Slot):
            out.append(d)
            continue
        if isinstance(d, dict):
            s = slot_from_dict(d)
            if s:
                out.append(s)
    return out
