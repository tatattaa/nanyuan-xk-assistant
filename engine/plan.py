"""引擎层：抢课计划。

一个 Plan 是一组要抢的目标；一个 PlanItem 是「某门课的某个教学班」。

设计要点：
    - 计划项按优先级排序（用户排序 = 意愿顺序）
    - 每项携带自己的重试参数（次数 / 间隔），不共用全局常量
    - 支持「先侦察后抢」：选定 do_id 前可以先 query_classes 看容量
    - 支持「盲提交」：开放期前不知道 do_id 也能挂上，靠 init 后轮询补齐
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from engine.events import TaskState
from core.schedule import (
    ScheduleEntry,
    Slot,
    find_conflicts,
    slots_conflict,
    slots_from_dicts,
)

#: 落盘格式版本。改结构时 +1，并在 `Plan.from_dict` 里决定「旧版怎么读/直接丢弃」。
PLAN_FORMAT_VERSION = 1

#: ⭐ **抢课**单项的尝试次数上限（2026-10-01 由 50 下调到 20）。
#:
#: 唯一权威定义处：`PlanItem.max_attempts` 的默认值、`ui/app.py::PlanItemBody` 的默认值
#: 都取这里。前端 `ui/static/app.js` 有一份镜像常量（纯前端零构建，没法 import），
#: **改这里必须同步改那一份**。
#:
#: 为什么要有「上限」这个东西：铁律 #5 —— 盲提交必须有次数闸，否则教务一直
#: 含糊其辞（既不明确拒绝也不给成功）时，一项会把整轮吃光。
#:
#: ⚠️ 它**只管「抢课」**。将来「蹲课」（长时间蹲守等退课名额）不该跟着这个数 ——
#: 蹲课的时间尺度是「小时」级，要走 `budget_s` / `global_deadline_s` 那条路，
#: 或者另开一个上限常量，别偷偷把这个值调大（那会把抢课也一起拖长）。
MAX_ATTEMPTS = 20

#: 派发方式（开火相内部怎么把请求排出去）。完整论证见 `engine/runner.py` 的长注释。
#:
#:   serial       一项打到终态才轮到下一项 —— 火力集中，命中即收工
#:   round_robin  每回合给每项发最多一个请求，不等上一发回来 —— 治「队头阻塞」（默认）
#:
#: 两者都**严格单线程**（任何时刻最多一个在途请求），只是「谁先发下一发」不同。
RETRY_SERIAL = "serial"
RETRY_ROUND_ROBIN = "round_robin"
RETRY_MODES: tuple[str, ...] = (RETRY_SERIAL, RETRY_ROUND_ROBIN)

#: 用户可能手写/旧数据里出现的别名。CLI 与 API 都过这一层，省得各写一遍。
_RETRY_ALIASES: dict[str, str] = {
    "": RETRY_ROUND_ROBIN,
    "serial": RETRY_SERIAL,
    "seq": RETRY_SERIAL,
    "sequential": RETRY_SERIAL,
    "顺序": RETRY_SERIAL,
    "串行": RETRY_SERIAL,
    "一项一项": RETRY_SERIAL,
    "round_robin": RETRY_ROUND_ROBIN,
    "roundrobin": RETRY_ROUND_ROBIN,
    "round-robin": RETRY_ROUND_ROBIN,
    "rr": RETRY_ROUND_ROBIN,
    "轮流": RETRY_ROUND_ROBIN,
    "轮流发送": RETRY_ROUND_ROBIN,
    "轮询": RETRY_ROUND_ROBIN,
}


def normalize_retry_mode(value: object) -> str:
    """把任意写法归一成 `RETRY_MODES` 里的一个。认不出来就回默认（round_robin）。

    ⚠️ 为什么不报错：这个值可能来自**落盘文件**（旧版本的清单没有这个字段、
    或者用户手改过）。为一个取值把整份清单读不出来，代价远大于「退回默认 + 界面上说明」。
    """
    key = str(value or "").strip().lower()
    return _RETRY_ALIASES.get(key, RETRY_ROUND_ROBIN)


def retry_mode_label(value: object) -> str:
    """中文标签（日志/界面用），保证「显示的口径」与「实际执行的模式」同源。"""
    return "轮流发送" if normalize_retry_mode(value) == RETRY_ROUND_ROBIN else "串行"


def parse_when(spec: str, *, now: float | None = None) -> float:
    """把用户输入的开抢时刻解析成 unix 秒（服务器时刻域）。

    支持三种写法：
        "12:00:00" / "12:00"          今天该时刻（已过去则明确报错，不偷偷顺延）
        "2026-09-30 12:00:00"         指定日期（也接受 / 分隔）
        "+90" / "+1:30" / "+1:30:00"  相对现在 —— 演练与自测用

    时区假设：**学校时区 == 本地时区**（国内场景成立）。用户照学校通知上的墙上
    时间填，这里按本地时区解释；再交给 ServerClock 把「服务器时刻」换算成
    「本地该在何时动手」，从而把本地时钟偏差抵消掉。
    """
    now = time.time() if now is None else now
    s = (spec or "").strip()
    if not s:
        raise ValueError("开抢时刻为空")

    if s.startswith("+"):
        return now + _parse_duration(s[1:])

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            hm = datetime.strptime(s, fmt)
        except ValueError:
            continue
        today = datetime.fromtimestamp(now)
        cand = today.replace(
            hour=hm.hour, minute=hm.minute, second=hm.second, microsecond=0
        )
        ts = cand.timestamp()
        if ts < now - 60:
            # 不偷偷顺延到明天 —— 抢课时刻差一天是灾难，宁可报错让用户写清楚
            nxt = today + timedelta(days=1)
            raise ValueError(
                f"“{s}”今天已经过去了（现在 {today:%H:%M:%S}）；"
                f"若指明天请写完整日期，例如 “{nxt:%Y-%m-%d} {hm:%H:%M}”"
            )
        return ts

    raise ValueError(
        f"无法解析开抢时刻 “{spec}”。可用格式：12:00:00 / 2026-09-30 12:00:00 / +90"
    )


def _parse_duration(s: str) -> float:
    """把 "90" / "1:30" / "1:30:00" 解析成秒。"""
    parts = s.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"无法解析相对时长 “{s}”") from e
    if not nums or len(nums) > 3:
        raise ValueError(f"无法解析相对时长 “{s}”")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total



@dataclass
class PlanItem:
    """一个抢课目标。"""

    kch_id: str                        # 课程号（必需）
    do_id: str = ""                    # 教学班加密 id；空则运行时由 query_classes 补齐
    jxb_id: str = ""                   # 教学班**稳定** id（非加密），用于令牌过期后重新绑定同一个班
    kklxdm: str = ""                   # 所属课程类别 Tab（每个 Tab 的上下文/加密串不同）
    tab_index: int = -1                # Tab **下标**；-1 = 未知，退回按 kklxdm 找
    # ↑ 为什么两个都要：`kklxdm` 会重复 —— 我校实测「板块课(大学体育)」与
    #   「板块课(大英一)」的 kklxdm **都是 06**，只靠 kklxdm 永远只能取到第一个，
    #   加了大英一的课就会查到体育板块去（查不到或查错）。
    #   下标才是唯一可靠的定位方式，所以优先用它；只有下标缺失（旧清单/手填）
    #   时才回退到 kklxdm。
    kcmc: str = ""                     # 课程名（仅用于显示/日志）
    jsxx: str = ""                     # 教师（显示用）
    xf: str = ""                       # 学分（字符串，来自教务；空/非法按 0 计）
    priority: int = 0                  # 越小越优先
    cxbj: str = "0"                    # 重修标记（来自课程列表行，查教学班要带上）
    fxbj: str = "0"                    # 辅修标记（同上）
    slots: list[Slot] = field(default_factory=list)
    # ↑ 该教学班的时段（来自教学班行的 sksj）。只用于**界面画课表**与**加课时提前
    #   判冲突**；真正的冲突裁决权在教务服务端（提交时它自己也会查），这里的结果
    #   只做提示，绝不用它跳过提交 —— 否则抢到第一门后已选变了会误判。

    # 每项独立的重试策略
    max_attempts: int = MAX_ATTEMPTS   # 铁律 #5：盲提交必须有上限（**次数**闸）
    interval_ms: int = 800             # 两次尝试的最小间隔
    #: 单项时间预算（秒）。0 = 不限。
    #:
    #: 与 `max_attempts` 是**两把不同的闸**：次数闸管不住时间 —— 教务卡顿时
    #: 一次尝试最坏 6 秒（`TIMEOUT_CRITICAL` = 连接 2s + 读取 4s），
    #: MAX_ATTEMPTS 次就是约 2 分钟，一项足以把整轮吃光。
    #: 卡顿场景下想给「单项最多占用多久」定个死，就设它。
    budget_s: float = 0.0
    precheck: bool = True              # 是否先做时间冲突预检

    # 运行时状态
    state: TaskState = TaskState.PENDING
    attempts: int = 0
    last_kind: object | None = None
    last_msg: str = ""
    # 当前 `do_id` 是什么时候拿到的（单调钟）。0 = 未知（例如从落盘/手填的清单来）。
    #
    # 为什么必须记：`do_jxb_id` 是**每次查询都重新下发的一次性令牌**，而清单可能是
    # 上午建好的 —— 到下午开抢时那个令牌已经躺了好几个小时。引擎据此判断
    # 「这一项手里的令牌可不可疑」，可疑就派侦察队去刷新的。
    # 与落盘无关（不在 _PERSIST_FIELDS 里），恢复的清单一律 token_at=0（=可疑）。
    token_at: float = 0.0

    def __post_init__(self) -> None:
        # `budget_s` 可能来自手改过的落盘 JSON（字符串 / null）。
        # 它直接参与 `>` 比较，类型不对会在 tick 里炸 —— 这里一次性归一。
        try:
            self.budget_s = max(0.0, float(self.budget_s or 0.0))
        except (TypeError, ValueError):
            self.budget_s = 0.0

    @property
    def key(self) -> str:
        """事件与日志里标识这一项。

        刻意**只用 `kch_id`**，不用 `kch_id/do_id`：`do_id` 是 256 位的加密令牌，
        塞进日志前缀会把一整行冲掉 —— 而铁律 #10 要的是「能读懂的原因」，不是一串密文。
        何况令牌每次查询都重新下发（铁律 #5），日志里同一个目标的 key 会跳来跳去，
        前后反而对不上。铁律 #6 保证同一门课在清单里只押一个班，所以 kch_id 足够唯一。
        """
        return self.kch_id or self.do_id

    @property
    def label(self) -> str:
        parts = [self.kcmc or self.kch_id]
        if self.jsxx:
            parts.append(self.jsxx)
        return " ".join(parts)

    # -- 序列化（落盘/恢复用）-------------------------------------------------

    #: 参与落盘的字段 = 「**用户的意图**」：要抢哪些课的哪个班、什么优先级、什么重试节奏。
    #:
    #: 三类字段刻意**不**落盘：
    #:   · `do_id` —— 一次性加密令牌（每次查询重新下发）。存下来下次用必然「请求被拒」，
    #:     属于「明知会坏还留着」的坑，恢复时一律重新解析。
    #:   · `state` / `attempts` / `last_*` —— 属于「这一轮跑出来的结果」。进程重启后
    #:     它们不具备任何效力：把 `won` 存下来会让恢复后的清单显示「已抢到」，
    #:     而教务那边可能早就退回了。恢复后一律回到 `pending`，让用户自己再按一次
    #:     「开始」—— 界面显示的状态必须是可信的，这比「看起来省事」重要。
    _PERSIST_FIELDS = (
        "kch_id",
        "jxb_id",       # 稳定 id，保留它便于界面上「认回同一个班」
        "kklxdm",
        "tab_index",
        "kcmc",
        "jsxx",
        "xf",
        "priority",
        "cxbj",
        "fxbj",
        "max_attempts",
        "interval_ms",
        "budget_s",
        "precheck",
    )

    def to_dict(self) -> dict:
        d = {f: getattr(self, f) for f in self._PERSIST_FIELDS}
        d["slots"] = [s.as_dict() for s in self.slots]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PlanItem":
        """还原一项。缺字段/类型不对走默认值 —— 宁可少一项，也不要整份清单读不出来。"""
        kwargs: dict = {}
        for f in cls._PERSIST_FIELDS:
            if f in d and d[f] is not None:
                kwargs[f] = d[f]
        kwargs["slots"] = slots_from_dicts(d.get("slots"))
        return cls(**kwargs)


@dataclass
class Plan:
    """一组抢课目标 + 全局策略。"""

    items: list[PlanItem] = field(default_factory=list)

    # 全局控制
    serial: bool = True                # 铁律 #6：提交严格串行（本字段只作展示，实际语义见 retry_mode）
    stop_on_first_win: bool = False    # 抢到一门就停（还是继续抢后续）
    global_deadline_s: float = 0.0     # 0 = 不限时
    #: 派发方式：`serial` 或 `round_robin`（默认）。见 `normalize_retry_mode` 与
    #: `engine/runner.py` 的长注释。⚠️ 两种都严格单线程 —— 它不是「要不要并发」的开关，
    #: 而是「一项打到终态才换下一项，还是每项先发一发再回头」的开关。
    retry_mode: str = RETRY_ROUND_ROBIN

    # --- 定时开抢 ---------------------------------------------------------
    # start_at 是**服务器时刻**的 unix 秒（不是本地时刻）。
    # 原因见 core/clock.py：本地钟实测偏 ±0.35 s，而学校的开抢时间是按服务器钟
    # 公布的，必须用校准后的偏差换算，否则整轮提前或落后。
    # None = 立刻开始（不进预热/倒计时流程）。
    start_at: float | None = None
    warmup_s: float = 90.0             # 提前多久进入预热（解析 do_id）
    warmup_interval_ms: int = 1500     # 预热首轮间隔；之后按 1.5 倍退避
    warmup_backoff: float = 1.5
    warmup_max_interval_ms: int = 8000 # 预热间隔上限（保护服务端，也保护自己）
    fire_lead_s: float | None = None   # 提前开火量（秒）；None = 用时钟不确定度自动定

    def add(self, item: PlanItem) -> "Plan":
        self.items.append(item)
        return self

    def sorted_items(self) -> list[PlanItem]:
        """按优先级排序（稳定排序，priority 相同保持插入序）。"""
        return sorted(self.items, key=lambda it: it.priority)

    def total_credit(self) -> float:
        """清单里「待加选」课程的学分之和。

        口径与 UI / CLI 共用一处，避免两边各算一遍算歪。
        学分缺失或写错一律按 0 计 —— 宁可少算也不能因为一个空串把整条显示搞崩，
        反正另一头有「剩余可选学分」兜着，用户看得出对不对。
        """
        total = 0.0
        for it in self.items:
            try:
                total += float(it.xf)
            except (TypeError, ValueError):
                continue
        return round(total, 2)

    @property
    def pending(self) -> list[PlanItem]:
        return [it for it in self.items if it.state is TaskState.PENDING]

    @property
    def won(self) -> list[PlanItem]:
        return [it for it in self.items if it.state is TaskState.WON]

    @property
    def ready(self) -> list[PlanItem]:
        """已解析出可提交 do_id 的项。"""
        return [it for it in self.items if it.do_id]

    def internal_conflicts(self) -> list:
        """清单**内部**互相冲突的时段（同一门课的不同班不算）。

        用途：启动时给一条提示，并在界面标注「这两项互斥」。

        判定口径 = `core.schedule.is_time_conflict`（星期 + 节次有交集 + **周次有交集**），
        与执行期 `conflicts_with` 完全一致 —— 提示与执行必须同口径，否则界面说
        「不冲突」而执行期却跳过了，没法向用户解释。
        这里用 `find_conflicts`（提示口径，含周次错开的 soft）只是为了让提示更全，
        真正决定跳过与否的是 `conflicts_with`。

        只比 i 与 j>i，保证同一对冲突**只报一次**（否则 A↔B 会被从两边各数一遍）。
        """
        hits = []
        items = self.items
        for i, it in enumerate(items):
            if not it.slots:
                continue
            others = [
                ScheduleEntry(
                    kch_id=o.kch_id, kcmc=o.kcmc or o.kch_id, slots=o.slots, source="pending"
                )
                for j, o in enumerate(items)
                if j > i and o.slots and o.kch_id != it.kch_id
            ]
            if not others:
                continue
            hits.extend(find_conflicts(it.slots, others))
        return hits

    def conflicts_with(self, item: PlanItem) -> list[PlanItem]:
        """与 `item` **真的互斥**的其他清单项：星期 + 节次 + **周次**三者都重叠。

        周次必须一起看：`周二 3-5节{1-5周}` 与 `周二 3-5节{6-17周}` 永远不撞，
        抢到前者后不该把后者也跳过 —— 那等于白白放弃一门本来能上的课。

        ⭐ **同课不同班不算互斥**（`o.kch_id != item.kch_id`）：蹲课允许同一门课
        押多个班（2026-10-01 用户确认「蹲课允许加同一节课的不同班」），它们常是
        同一时段，但蹲的是**不同教学班的名额**，抢到/蹲到一个班不代表另一个班也
        满，继续提交另一个班是合理动作，不该互相跳过。
        对抢课无副作用 —— 铁律 #6「同一门课只押一个班」保证清单里本就不会出现
        同课两个班，这个过滤在抢课场景是 no-op。

        时段未知（slots 为空）的项**不参与**判定 —— 宁可按原样试，也不要凭缺失
        的信息把一项白白跳过。
        """
        if not item.slots:
            return []
        return [
            o
            for o in self.items
            if o is not item and o.slots and o.kch_id != item.kch_id
            and slots_conflict(item.slots, o.slots)
        ]

    def conflict_pairs(self) -> list[list[int]]:
        """互斥项的下标对 `[[i, j], ...]`（i < j），给界面标注抢课顺序用。

        口径同 `conflicts_with`（星期+节次+周次三者都重叠，且**同课不同班不算**）。
        走 `items` 的下标而不是 key：同一门课只押一个班（铁律 #6），
        但 key 仍可能因为 do_id 未解析而重复，下标是唯一无歧义的。
        """
        out: list[list[int]] = []
        items = self.items
        for i, it in enumerate(items):
            if not it.slots:
                continue
            for j in range(i + 1, len(items)):
                o = items[j]
                if o.slots and o.kch_id != it.kch_id and slots_conflict(it.slots, o.slots):
                    out.append([i, j])
        return out

    # -- 序列化（落盘/恢复用）-------------------------------------------------

    #: 全局策略同样属于「用户意图」，一并落盘，否则恢复后定时开抢的时间点就丢了。
    _PERSIST_FIELDS = (
        "serial",
        "retry_mode",
        "stop_on_first_win",
        "global_deadline_s",
        "start_at",
        "warmup_s",
        "warmup_interval_ms",
        "warmup_backoff",
        "warmup_max_interval_ms",
        "fire_lead_s",
    )

    def to_dict(self) -> dict:
        d = {f: getattr(self, f) for f in self._PERSIST_FIELDS}
        d["version"] = PLAN_FORMAT_VERSION
        d["items"] = [it.to_dict() for it in self.items]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        """还原清单。

        刻意**不**在这里校验版本号并抛错：清单是本地文件，读不出来只是「没有清单」，
        不该让整个服务起不来。版本不认识就只读能认的字段，读不动的项直接丢掉。
        """
        kwargs: dict = {}
        for f in cls._PERSIST_FIELDS:
            if f in d and d[f] is not None:
                kwargs[f] = d[f]
        # ⚠️ 归一放在构造**之前**：落盘里可能是旧值 / 用户手写的别名 / 整个字段缺失。
        # 直接丢给 `__init__` 会让 `Plan.retry_mode` 带着一个没人认得的值到处跑，
        # 而 runner 只在**开火那一刻**才去归一它 —— 中间的界面展示就会与执行不一致。
        kwargs["retry_mode"] = normalize_retry_mode(kwargs.get("retry_mode"))
        plan = cls(**kwargs)
        for raw in d.get("items") or []:
            if not isinstance(raw, dict) or not raw.get("kch_id"):
                continue  # 没有 kch_id 的项无法定位课程，直接丢
            try:
                plan.items.append(PlanItem.from_dict(raw))
            except Exception:
                continue
        return plan

