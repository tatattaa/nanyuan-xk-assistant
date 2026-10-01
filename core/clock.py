"""服务器时钟校准。

抢课的本质是「卡点」。但**先把期待放低** —— 2026-09-29 对本校实测：

    单次请求 RTT      250 ms（keep-alive 复用连接）/ 410 ms（每次新建连接）
    Date 头精度       1 秒（HTTP 标准，无法更细）
    本地时钟偏差      单次估算抖动 ±350 ms 量级
    校准后            8 次采样 → +251 ms ±144 ms（偏差可行区间 [+107, +395] ms）

所以「毫秒级卡点」在本校没有意义。精度上有两条硬事实：

    1. 单个样本给出的约束区间宽度 = 1 + RTT
       ∵ Date 只到整秒（宽度 1），再叠加请求往返的不确定（宽度 RTT）
    2. 交集能明显比单样本更窄 —— 下界来自某次样本、上界来自另一次，
       两次的「秒内相位」错开时交集会收窄。实测 8 次采样从 1.2 s 收到 0.29 s，
       比「1 + 最小RTT」还窄（所以不要写「不可能优于 1+最小RTT」这类断言，
       它会被真实数据打脸）。

即便如此，也不要指望时钟精度翻盘。提高胜率的真正杠杆是「T0 前把一切准备好、
T0 只发一个 submit」——那省下的是数百毫秒。时钟校准的作用有两个：
  1. 保证 T0 不会因为本地时钟偏差（实测数百毫秒）而整轮提前或落后
  2. 给出不确定度，让上层知道该**提前**多少开火（见 `lead_seconds()`），
     把残余误差推向「早到」这一无害的一侧

估计方法：区间交集
--------------------------------------------------------------------------
设 O = 服务器时刻 - 本地时刻（待估的常量），本地时刻取「单调钟派生的稳定挂钟」，
以免系统调时（NTP 校正）把估计带偏。

一次响应的 Date 头 d 由服务器在某个瞬间生成，该瞬间的本地挂钟读数为 l*，
而 l* 必然落在 [l0, l1] 内（l0 = 发出请求前，l1 = 收到响应后）。
又因为 HTTP Date 向下截断到整秒：

    d  ≤  l* + O  <  d + 1

代入 l* ∈ [l0, l1]，得到该样本对 O 的约束：

    O ∈ [ d - l1 ,  d + 1 - l0 )

多个样本取**交集**：样本越多，区间越窄；取中点作为估计，半宽即不确定度。
这比「只取 RTT 最小的那一次」更优 —— 不丢弃信息，且直接给出误差范围。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger("xk.clock")


def parse_http_date(value: str | None) -> float | None:
    """把 HTTP Date 头解析成 unix 秒。失败返回 None。"""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:  # 极少数服务器不带 GMT，按 UTC 处理
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass(frozen=True)
class ClockSample:
    """一次时钟采样给出的约束区间。"""

    lo: float          # O 的下界
    hi: float          # O 的上界
    rtt: float         # 该次请求的往返耗时（秒）
    date_raw: str = ""

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0


class ServerClock:
    """用响应 Date 头持续收窄「服务器 - 本地」偏差估计。

    线程安全说明：`observe` 只做 max/min 更新，CPython 的 GIL 足以保证单条语句
    的原子性；本类不做多线程共享写以外的假设（每次请求各采样一次）。
    """

    def __init__(self) -> None:
        # 稳定挂钟：以单调钟为基准，免疫系统调时导致的跳变
        self._ref_wall = time.time()
        self._ref_mono = time.monotonic()
        self._lo = float("-inf")
        self._hi = float("inf")
        self._rtt_min = float("inf")
        self._count = 0
        self._resets = 0
        self._last_raw = ""

    # -- 稳定挂钟 -----------------------------------------------------------

    def now(self) -> float:
        """本地挂钟读数（单调推进，不受系统调时影响）。"""
        return self._ref_wall + (time.monotonic() - self._ref_mono)

    # -- 采样 ---------------------------------------------------------------

    def observe(
        self,
        date_header: str | None,
        t0_mono: float,
        t1_mono: float,
    ) -> ClockSample | None:
        """喂一次响应头。

        t0_mono / t1_mono 是发出请求前 / 收到响应后的 time.monotonic() 读数。
        返回本次样本；Date 头缺失或不可解析时返回 None（不影响已有估计）。
        """
        d = parse_http_date(date_header)
        if d is None:
            return None

        rtt = max(0.0, t1_mono - t0_mono)
        # 把单调时刻换算到稳定挂钟域
        l1 = self.now() - (time.monotonic() - t1_mono)
        l0 = l1 - rtt

        sample = ClockSample(lo=d - l1, hi=d + 1.0 - l0, rtt=rtt, date_raw=date_header or "")
        self._count += 1
        self._last_raw = sample.date_raw

        new_lo = max(self._lo, sample.lo)
        new_hi = min(self._hi, sample.hi)
        if new_lo >= new_hi:
            # 交集为空 → 样本互相矛盾（网络长尾延迟 / 系统刚被调时）
            # 舍弃历史，以本次为新起点；宁可重新收敛，也不要一个错的偏差。
            self._resets += 1
            logger.warning(
                "时钟样本交集为空（疑似网络抖动或系统调时），重置估计（第 %d 次）",
                self._resets,
            )
            new_lo, new_hi = sample.lo, sample.hi

        self._lo, self._hi = new_lo, new_hi
        self._rtt_min = min(self._rtt_min, rtt)
        return sample

    # -- 查询 ---------------------------------------------------------------

    @property
    def samples(self) -> int:
        """累计采样次数（含被重置丢弃的）。"""
        return self._count

    @property
    def synced(self) -> bool:
        return self._count > 0 and self._lo < self._hi

    @property
    def offset(self) -> float | None:
        """服务器时刻 - 本地时刻（秒）。未校准返回 None。"""
        if not self.synced:
            return None
        return (self._lo + self._hi) / 2.0

    @property
    def window(self) -> tuple[float, float] | None:
        """当前 O 的可行区间。未校准返回 None。"""
        return (self._lo, self._hi) if self.synced else None

    @property
    def uncertainty(self) -> float:
        """偏差估计的半宽（秒）。未校准为 inf。"""
        if not self.synced:
            return float("inf")
        return (self._hi - self._lo) / 2.0

    @property
    def rtt(self) -> float:
        """观测到的最小 RTT（秒）。未采样为 inf。"""
        return self._rtt_min

    # -- 换算 ---------------------------------------------------------------

    def server_now(self) -> float:
        """当前服务器时刻（unix 秒）。未校准时退化为本地挂钟。"""
        off = self.offset
        return self.now() if off is None else self.now() + off

    def monotonic_at(self, server_ts: float) -> float:
        """服务器时刻 server_ts 对应的 time.monotonic() 读数。

        可直接用于 `time.monotonic() >= monotonic_at(t)` 判断是否到点。
        由于 server_now() 与 monotonic() 只差一个常量，这里是无损换算。
        """
        return time.monotonic() + (server_ts - self.server_now())

    def lead_seconds(self, minimum: float = 0.2, cap: float = 1.5) -> float:
        """建议的「提前开火」量（秒）。

        若严格瞄 T0，真实开火时刻可能偏晚整整一个半宽（实测约 600 ms），白丢；
        偏早则最多被服务端回一句「不在选课时间」，重试循环立刻补上，几乎无成本。
        所以宁可早：取不确定度并**向上**取整到 0.1 s，夹在 [minimum, cap] 内。

        ⚠️ 这里必须用 math.ceil 而不是 round：Python 的 round 是银行家舍入，
        round(0.25 * 10) == 2（向偶数舍），会把「保守量」反而舍小 —— 与意图相反。
        """
        if not self.synced:
            return minimum
        return max(minimum, min(cap, math.ceil(self.uncertainty * 10) / 10 + 0.05))

    def describe(self) -> str:
        """给人看的一句话说明（日志/界面）。"""
        if not self.synced:
            return "时钟未校准（尚未收到带 Date 头的响应）"
        off = self.offset or 0.0
        # off > 0 表示服务器时刻大于本地 → 本地钟慢了
        state = "慢" if off > 0 else "快"
        return (
            f"本地时钟比服务器{state} {abs(off) * 1000:.0f} ms"
            f"（不确定度 ±{self.uncertainty * 1000:.0f} ms，样本 {self._count}，"
            f"最小 RTT {self.rtt * 1000:.0f} ms）"
        )

    def as_dict(self) -> dict:
        """给界面用的快照。"""
        off = self.offset
        return {
            "synced": self.synced,
            "offset_ms": round(off * 1000) if off is not None else None,
            "uncertainty_ms": (
                round(self.uncertainty * 1000) if self.synced else None
            ),
            "rtt_ms": round(self.rtt * 1000) if self._rtt_min != float("inf") else None,
            "samples": self._count,
            "resets": self._resets,
            "server_now": self.server_now() if self.synced else None,
            "lead_ms": round(self.lead_seconds() * 1000),
            "text": self.describe(),
        }


# 进程级默认时钟（命令行一次性场景用；每个 HttpSession 自带独立实例）
DEFAULT_CLOCK = ServerClock()
