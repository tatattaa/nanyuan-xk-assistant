"""引擎层：抢课执行器。

把核心层的 4 个函数组合成一条可停止、可观测、有上限的抢课流水线。

两种执行模式
--------------------------------------------------------------------------
1) 立即模式（plan.start_at is None）
   逐项「解析 do_id → 预检 → 提交循环」，和最初的行为完全一致。

2) 定时模式（plan.start_at 有值）—— 这才是抢课的正确姿势
   实测本校单次请求 RTT ≈ 250 ms（复用连接）/ 410 ms（新建连接）。
   若等到 T0 才开始「切 Tab → 查课 → 查班 → 预检 → 提交」，那是 5 个请求
   ≈ 1.25 s，等于起跑就落后 5 个身位。所以拆成两相：

       预热相（T0 - warmup_s 起）：反复解析 do_id，直到全部就绪
       开火相（T0 - lead 起）    ：只剩一个 submit，一次往返定胜负

   预热阶段的采样间隔按 1.5 倍退避（1.5s → 8s 封顶）——因为开放前查不到教学班，
   密集轮询毫无收益还容易被服务端盯上；退避后采样天然向 T0 附近收拢。

   ⚠️ 预检刻意**留在开火相**而不是预热相：预检判的是「与已选课程是否冲突」，
   而抢到第一门之后已选就变了，早早预检的结果会过期（自己造出来的冲突漏判）。

两种派发方式（开火相内部）
--------------------------------------------------------------------------
`plan.retry_mode` 决定「下一个 tick 给谁」，两种模式共用同一套 tick 状态机：
    serial        一项打到终态才轮到下一项（默认，旧的严格串行）
    round_robin   每回合给每项发最多一个请求，不等上一发回来（治队头阻塞）
完整论证见下方「派发模式：serial / round_robin」注释块。

安全边界（对应设计铁律）：
    #2 预检零副作用：预热阶段只做 GET/POST 查询，不发任何 submit
    #5 盲提交有上限：max_attempts / budget_s / global_deadline_s 三道闸
    #6 严格单线程：任何时刻最多一个在途写请求，绝不并发提交
       （serial 与 round_robin 都满足；区别只是「谁先发下一发」，见下方长注释）
    #7 预检在前：开火前先查冲突，冲突则 SKIPPED
    #10 每次失败都推 GIVE_UP/RETRY_WAIT 事件，带上语义 kind 与文案

互斥跳过（省配额的关键）
--------------------------------------------------------------------------
清单顺序 = 抢课优先顺序。某一项 WON 之后，清单里与它**真的撞时间**的其他待选课
（星期 + 节次 + **周次**三者都重叠）立刻标记 SKIPPED（`_skip_mutex`），一项请求都不再发。
这些课不可能同时上成，继续提交只会白烧配额、还可能招来「选课频率过高」的限流。
只节次占位重叠、周次错开的（`is_soft_conflict`）**不算**，照常提交。

本模块不认识界面，只通过 on_event 回调往外推 Event。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from core.client import ZfClient
from core.errors import KIND_LABEL, FailureKind, XKError
from engine.events import Event, EventType, TaskState
from engine.plan import Plan, PlanItem, normalize_retry_mode

logger = logging.getLogger("xk.runner")

EventSink = Callable[[Event], None]

# 开火瞬间的忙等窗口：太短会因 sleep 粒度过冲，太长就是白烧 CPU
SPIN_WINDOW_S = 0.02

# ---------------------------------------------------------------------------
# 铁律 #5：令牌过期要重查刷新
#
# `do_jxb_id`（提交用的 `jxb_ids`）是**每次查询都会重新生成的一次性加密令牌**
# （2026-09-29 实测：连查 3 次已选列表，12 门课 36 个串，0/36 相同）。
# 所以「连续失败」不能一律当成「没抢到」——也可能是手里这个令牌已经不被接受了。
#
# 但**不能一失败就重查**，那会把请求量翻倍、还可能撞上「选课频率过高」限流。
# 关键在「失败的语义」：
#
#   CONFLICT / TOO_FREQUENT
#       → 服务端**正常处理了**这次请求，令牌是有效的，重查纯属浪费 → 不刷新
#   CONTEXT_INVALID（加密串错误）/ UNKNOWN
#       → 请求本身被拒，很可能就是令牌过期 → 值得重查
#
# ⚠️ `FULL`（满员）原本也在这个「不刷新」之列，但 2026-10-01 起它已经是
# **终态失败**（见 `core/errors.py::FailureKind.FULL`）→ 在 `_tick_submit` 的
# 「不可重试」那一步就 return 了，**根本走不到这张表**。留着这一行说明是为了
# 防止有人看到「满员不刷新」就去猜「那它肯定在重试」。
#
# 只有后一类连续失败到 `REFRESH_AFTER_STALE` 次，才去重查并换上新令牌。
# ---------------------------------------------------------------------------

#: 连续「请求被拒」多少次后重查刷新令牌。
#:
#: ⚠️ **2026-09-30 从 3 改成 1**，理由两条：
#:
#: ① 能走到这里的只有 `CONTEXT_INVALID`（`UNKNOWN` 不可重试，见 `_TOKEN_STALE_KINDS`），
#:    而「加密串错误」是**明确的状态问题**，不是网络抖动 —— 第一次遇到就该去刷，
#:    再撞两发纯属浪费（每发都占配额、还拖慢后面的课）；
#: ② 更重要的是：**刷新令牌这件事本身是有副作用的**（见下方「为什么不能并行」），
#:    所以「先打一发、失败即刷」必须严格串行，不能靠并行去省时间。
REFRESH_AFTER_STALE = 1

#: 一轮任务里最多重查几次（防止服务端一直报错时无限重查，把配额烧光）
MAX_TOKEN_REFRESH = 3

#: 「补齐教学班（do_id）」那一步自己的重试预算（2026-09-30）。
#:
#: ⚠️ **独立于 `item.attempts`**：后者是**提交**的次数额度，别让解析把它花光
#: （解析最多 3 次、每次 1 个请求，与提交是两回事）。
#: 为什么这一步也要重试：解析走的 `query_classes` 同样会撞网络抖动 / 超时；
#: 一次失败就判死的话，「网络类可重试」这条策略在这条路径上完全失效。
RESOLVE_TRIES_MAX = 3

#: 这些失败语义意味着「请求根本没被正常受理」，值得重查刷新令牌
#:
#: ⚠️ 注意 `UNKNOWN` **目前是不可达的**：UNKNOWN.retryable() 为 False，
#: `_tick_submit` 在「不可重试就 return」那一步（见 `不可重试：`）就已经终止，
#: 根本走不到累加 stale_streak 的地方。这里仍然列上它，是因为语义上「未识别」
#: 确实可能只是教务换了个措辞的令牌失效 —— 将来若把 UNKNOWN 放开为可重试，
#: 这条就会立刻生效，不用再回来改。
#:
#: ⚠️ `NETWORK`（网络超时/连接被拒）**刻意不在这里**（2026-09-30 拆分后）：
#: 它虽然可重试，但语义是「请求没走到教务」—— 跟令牌新旧毫无关系，
#: 触发重查只会白烧一个请求还作废手里的令牌。它走普通的退避重试就够了。
#:
#: 取舍（2026-09-29 用户确认）：**未知返回一律不重试、直接终止**。
#: 理由：未知错误重试通常毫无意义（教务已经明确告诉你原因了），
#: 白白重试到上限只会浪费配额、拖慢其它课程。
#: 例：教务返回「超过本学期最高选课学分限制，不可选！」→ 归 UNKNOWN →
#: 直接判 failed 并终止，这是**期望行为**，别为它加关键词规则。
#: （与 NETWORK 的区别要看清楚：前者是「教务说了话但没归类」，后者是「教务没说话」。）
_TOKEN_STALE_KINDS: frozenset[FailureKind] = frozenset(
    {FailureKind.CONTEXT_INVALID, FailureKind.UNKNOWN}
)


# ---------------------------------------------------------------------------
# P0：「陈旧上下文」是这条流水线最大的单点故障
#
# 2026-09-30 离线实证：**上午建会话 → 下午一键抢课 = 100% 全灭**。
# 根因是 `client.init()` 带快照缓存（`if self._inited and not force: return self.store`）——
# 第二次起一个请求都不发，直接把上午那份 xkkz_id / do_jxb_id 原样返回。
# 而教务上午（查课期）与下午（抢课期）之间夹着一段「未开放期」，上下文会失效。
#
# 用户观测到的现象与之完全同构：「不刷新浏览器，抢课按钮不会自动变回抢课，
# 点击依然无效，必须刷新一下才行」。
#
# 所以本模块的纪律是：
#   1) 计划一开始就 `init(force=True)`（真抓一次，不接受快照）；
#   2) 预热相**每一轮**先检查上下文岁龄，超龄就重抓 —— 教务一恢复开放，
#      下一轮立刻拿到真上下文，而不是等 T0 才第一次问；
#   3) 开火相**严格串行**：拿着手里的令牌先打一发，失败了才去重查换新令牌再重发
#      （见下方「为什么刷新令牌不能和打一发并行」—— 并行刷新会作废手里那个旧令牌）。
# ---------------------------------------------------------------------------

#: 上下文「超龄」阈值（秒）。超过就认为手里的 xkkz_id / do_jxb_id 可能已被作废。
#:
#: 45 秒的来历：实测单次请求 RTT 250 ms（复用）/ 410 ms（新建），一次完整重抓
#: （Index + Display）≈ 0.8 s。阈值定太小 → 反复 force init，白白给一台本来就
#: 吃紧的服务器加压；太大 → 教务恢复开放后还抱着旧上下文干等。
#: 45 s 是「预热相每轮至多重抓一次」与「最迟 45 秒内一定用上新上下文」的折中。
CONTEXT_MAX_AGE_S = 45.0


# ---------------------------------------------------------------------------
# 🔴 为什么「刷新令牌」不能和「打一发」并行（2026-09-30 用户指出）
#
# 教务的 `do_jxb_id` 是**一次性令牌**：每次查某门课的教学班，都会**重新下发**一个
# 新的，并**作废旧的**。所以「去刷新令牌」这个动作本身就带着副作用 ——
# 它会让手里那个旧令牌立刻失效。
#
# 于是有一个绕不过去的矛盾：
#
#     只要「刷新令牌」的请求比「用旧令牌提交」的那一发**先到服务器**，
#     旧令牌就已经是废的了 —— 那一发提交注定被拒，而且是被自己人废掉的。
#
# ⚠️ 这不是「概率小」的问题。曾经真的按「两队并行」写过：开火前先支起一支后台
# 侦察队去重查教学班（刷出新令牌备用），主线程同时拿旧令牌打一发，谁先成用谁。
# 那是**原理性错误**：
#   · 侦察队是在主线程发提交**之前**启动的，它天然先出网；
#   · 侦察队复用自己的客户端，从第二门课起上下文已是新鲜的，
#     `_grab_context` 零请求，它会**直奔** `query_classes`；
#   · 于是「谁先到服务器」纯看运气 —— 一旦刷新先到，主线程那一发就被自己人废了。
# 更糟的是它把一个**确定性**的东西变成了**随机**的：本来稳赢的一发，变成掷硬币。
#
# 结论：**「并行刷新」与「拿旧令牌打一发」在原理上互斥**，只能二选一。
# 而「先用旧令牌打一发」明显更划算：
#   · 命中就是 1 个请求结束战斗（省掉整个刷新周期 ≈ 1 s）；
#   · 打不着也才 250 ms，之后照样刷新；
#   · 反过来「先刷新」则永远多花一个刷新周期，还白白废掉一次命中机会。
#
# 所以最终形态是**严格串行**（也就是用户最初描述的那个模型）：
#
#     ① 拿着手里的令牌先打一发          ← 此刻没有任何人跟它抢令牌
#     └─ 成功 → 收工
#        └─ 令牌类失败 → ② 同步重查教学班换新令牌 → 用新令牌重发（回到 ① 的循环）
#
# 好处不止是正确性：**首发就成功时，一次刷新请求都不用发**。
#
# ⚠️ 别再想着「支一支后台队并行刷」把这点时间省回来 —— 那 250 ms 换来的是
# 首发命中率归零。整轮只有一条线程在发写请求（铁律 #6）。
# ---------------------------------------------------------------------------

#: 提交被**本地闸门**挡下（NOT_OPEN）后，最多这样等多久（秒）。
#:
#: 为什么不能一次就判死：`submit()` 的 NOT_OPEN 是本地闸门给的（一个请求都没发），
#: 只说明「我们手里的上下文还**认为**没开放」，而教务很可能就在这一刻刚开放。
#: 判死等于把「卡在开放瞬间」这种情况整项丢掉 —— 而那恰恰是最常见的一种失手：
#: 用户提前十几秒点了「开始抢课」，教务到点才开。
#:
#: ⚠️ 为什么按**时间**而不是按次数：这里的判据本质是「还要等多久教务才开放」，
#: 而节奏由 `NOT_OPEN_RETRY_WAIT_S` 决定 —— 用次数当上限，实际时长会跟着等待
#: 节拍一起漂。30 秒 = 「提前半分钟点开始」也能等到开放，又不会无限期占着
#: 清单里后面的项（整轮的硬闸门仍是 `item.max_attempts`，每轮循环都消耗一次）。
NOT_OPEN_GRACE_S = 30.0

#: 上面那条重试之间的等待（秒）。教务开放是「秒级」事件，不是毫秒级，等 2 秒足够。
NOT_OPEN_RETRY_WAIT_S = 2.0


# ---------------------------------------------------------------------------
# 派发模式：serial / round_robin（2026-10-01）
#
# 起因（用户实测痛点）：教务高峰期很卡，一个 submit 可能迟迟不回。旧的「强化串行」
# 是「一项跑到终态（抢到 / 判死）才轮到下一项」，于是一发卡住的请求会把后面**所有**
# 课一起冻住 —— 典型的队头阻塞。定量算一下：单请求最坏 30 s（`requests` 的
# connect/read 各算一次 15 s）× 每项的尝试次数上限（`plan.MAX_ATTEMPTS`），
# 两项清单最坏能拖十几分钟，
# 而且 `global_deadline_s` 还拦不住（它原先只在「项与项之间」检查一次）。
#
# 解法：把「推进一项一轮」拆成**每次最多发一个请求**的原语（`GrabbingRunner._tick`），
# 于是可以有两种派发方式：
#
#   serial       （默认）一项一项来。第 1 项打到终态才轮到第 2 项 —— 旧的严格串行语义。
#                优点：火力集中于第一优先项，命中即收工。
#   round_robin  轮流来。第 1 项发一个请求 → 第 2 项发一个请求 → … → 回头再第 1 项。
#                优点：**不等任何一发回来**，每个课程申请都能在最短时间内先提交一遍；
#                某一发卡死时，别的课照样在推进（队头阻塞消失）。
#
# ⭐⭐ 两种模式都**严格保持单线程同步**，任何时候最多只有一个在途请求。
# 这不是偷懒，而是「响应必须与课程正确配对」这条要求的实现方式：
# 同步调用里 `submit()` 的返回值**天然属于当前正在 tick 的那一项** ——
# 从发出到收回之间我们压根没发过别人的请求，所以不存在「这个响应是 A 的还是 B 的」
# 这种问题。任何异步 / 并发都会凭空造出这个错配风险（而且 `do_jxb_id` 是一次性令牌，
# 并发刷新还会作废别人手里的令牌），所以这里**明确禁止**（铁律 #6）。
#
# 「每一项自己的进度」放在 `_Tick` 里（尝试次数、连续被拒、已重查次数、下次可 tick 时刻），
# 与 `PlanItem` 上的 `attempts/state` 一起构成这一项的完整状态 —— 轮流派发时
# A、B 两家的状态各记各的，不会串。
# ---------------------------------------------------------------------------

#: NETWORK 类失败（超时/连接被拒）的阶梯退避上限（秒）。
#:
#: 为什么单独给网络类加退避：它跟「教务拒绝了这次请求」不一样 —— 它表示**这次没问到**。
#: 而「没问到」的常见原因是教务正被挤爆，此时密集重发只会火上浇油（还容易吃限流）。
#: 阶梯退避（1× → 2× → 4× → 8× 封顶）让它在最 congested 的时刻主动让路。
NETWORK_BACKOFF_MAX_S = 8.0


class _Phase(Enum):
    """一项在开火相里的推进阶段（阶段之间是一条单向链）。"""

    RESOLVE = "resolve"      # 解析 do_id（预热没解析出来的走这条）
    PRECHECK = "precheck"    # 时间冲突预检（零副作用）
    SUBMIT = "submit"        # 提交（写请求）
    DONE = "done"


#: `_tick*` 的三种收尾方式
_NEXT = 1      # 本 tick 到此为止（发了请求 / 或要等退避）—— 下次再来
_ADVANCE = 2   # 零请求，可以立刻推进到下一相位
_FINISH = 3    # 这一项到终态了


@dataclass
class _Tick:
    """一项的**分步推进进度**。

    与 `PlanItem` 的分工：`PlanItem` 存「用户意图 + 对外可见的结果」（落盘、给界面看），
    `_Tick` 存「这一次派发过程中的中间态」（纯运行期、不落盘、重启即丢）。
    """

    phase: _Phase = _Phase.RESOLVE
    #: 下次允许 tick 这一项的最早时刻（单调钟）。退避等待靠它，而不是睡在项里 ——
    #: 轮流模式下「等」必须是可以让出去的：A 在退避，B 要能继续发。
    ready_at: float = 0.0
    started_at: float = 0.0     # 第一次 tick 的时刻（算 `budget_s` 用）
    resolve_tries: int = 0      # 解析教学班已试几次（独立预算，不吃 attempts）
    stale_streak: int = 0       # 连续「请求被拒」次数（铁律 #5）
    net_streak: int = 0         # 连续网络异常次数（阶梯退避用）
    refreshes: int = 0          # 已重查刷新令牌几次
    ctx_refreshes: int = 0      # 已「重抓上下文再试」几次（未开放耐心，只作日志）
    waited_closed: float = 0.0  # 已在「教务未开放」里耗了多少秒
    finished: bool = False


class GrabbingRunner:
    """执行一个 Plan。支持后台线程运行 + 手动停止。"""

    def __init__(
        self,
        client: ZfClient,
        plan: Plan,
        on_event: EventSink | None = None,
    ):
        self.client = client
        self.plan = plan
        self._sink = on_event
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        # 每项的分步推进进度（见 `_Tick`）。key 用 `id(item)`（对象身份）——
        # ⚠️ 不能用 `item.key`（= kch_id）：蹲课允许「同课不同班」（2026-10-01），
        # 同一门课会在清单里出现多个班，kch_id 相同 → key 撞车 → 两个班共享同一个
        # _Tick，第一个班推进到终态后第二个班被误判 finished、从此不再提交。
        # `id(item)` 在单次 runner 生命周期内稳定且唯一，规避同课多班撞键。
        self._ticks: dict[int, _Tick] = {}
        # 本轮开始时刻（单调钟）。`global_deadline_s` 现在**在 tick 内部**也检查，
        # 就是为了「一发卡住拖穿总时限」这件事不再可能。
        self._t0: float = 0.0
        # ⭐ 蹲课的全局节流：两条提交请求之间的最小间隔（单调钟时刻）。
        # 抢课 round_robin 是「一轮内所有项背靠背各发一发」（T0 火力摊开，刻意同时）；
        # 蹲课要的是「发完一门隔 interval_ms 再发下一门」，项与项之间也要错开。
        # 为 0 时表示不节流（抢课）。蹲课由 `_dispatch` 依据 `max_attempts <= 0` 打开。
        self._global_interval_s: float = 0.0
        self._global_ready_at: float = 0.0
        # ⚠️ 整轮只有一个客户端、一条线程在跑写请求。
        # 曾有过「侦察队专用客户端」（`fork()`）用来并行刷令牌，2026-09-30 已删除 ——
        # 刷新令牌会作废手里的旧令牌，跟「先用旧令牌打一发」互斥，见文件头的长注释。


    # -- 事件 ---------------------------------------------------------------

    def _emit(self, ev: Event) -> None:
        if self._sink:
            try:
                self._sink(ev)
            except Exception:  # 界面回调异常不能拖垮引擎
                logger.exception("事件回调异常")
        elif ev.type is EventType.SUCCESS:
            logger.info("[%s] %s", ev.item_key, ev.message)

    def _log(self, ev_type: EventType, item: PlanItem | None, msg: str, **kw) -> None:
        self._emit(
            Event(
                type=ev_type,
                item_key=item.key if item else "",
                message=msg,
                **kw,
            )
        )

    # -- 生命周期 -----------------------------------------------------------

    def start_background(self) -> threading.Thread:
        """在后台线程跑，立即返回。界面层用它保持不阻塞。"""
        self._thread = threading.Thread(target=self.run, name="xk-runner", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        """请求停止（下一轮循环检查后退出）。"""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def done(self) -> bool:
        """整轮是否已经跑完（抢完 / 放弃 / 中止都算）。

        与 `stopped` 的区别：`stopped` 只表示「被要求停止」，而抢到全部目标、
        或达到上限自然结束时 `stopped` 仍为 False。UI 的「是否还在忙」必须看
        `done`，否则一轮跑完后按钮会永远停在禁用态。
        """
        return self._finished.is_set()

    # -- 主循环 -------------------------------------------------------------

    def run(self) -> Plan:
        """同步执行整个计划，返回执行后的 plan。"""
        try:
            return self._run_inner()
        finally:
            # 无论正常结束还是异常退出，都要让上层知道「这一轮完了」
            self._finished.set()

    def _run_inner(self) -> Plan:
        t0 = time.monotonic()
        self._t0 = t0
        self._log(EventType.PLAN_START, None, f"开始执行计划：{len(self.plan.items)} 项")

        # 统一 init。⚠️ **必须 force=True**（P0）：
        # `client.init()` 带快照缓存，不带 force 时第二次起**一个请求都不发**，
        # 直接把上午那份上下文原样返回 —— 那正是「上午建会话 → 下午一键抢课
        # 100% 全灭」的根因。这里宁可多花 2 个请求，也要拿到当下这一刻的上下文。
        try:
            self._grab_context(force=True, reason="计划开始")
        except XKError as e:
            self._handle_fatal(e)
            return self.plan
        self._log(
            EventType.LOG,
            None,
            f"上下文就绪：选课{'已开放' if self.client.is_open else '未开放'}"
            f"（{len(getattr(self.client, 'tabs', []) or [])} 个课程类别，"
            f"抓取于 {self._age_text()}）",
        )

        if self.plan.start_at:
            self._run_scheduled(t0)
        else:
            self._run_immediate(t0)

        elapsed = int((time.monotonic() - t0) * 1000)
        self._emit(
            Event(
                type=EventType.PLAN_DONE,
                message=f"计划结束：成功 {len(self.plan.won)} / 共 {len(self.plan.items)}",
                elapsed_ms=elapsed,
            )
        )
        return self.plan

    # -- 上下文新鲜度（P0）--------------------------------------------------

    def _store_age(self) -> float | None:
        """当前客户端「上次真实抓取上下文」距现在多少秒；不支持则该字段为 None。"""
        try:
            return self.client.store_age_s
        except Exception:
            return None

    def _age_text(self) -> str:
        age = self._store_age()
        if age is None:
            return "未知"
        return f"{age:.0f} 秒前" if age >= 1 else "刚刚"

    @staticmethod
    def _client_suspect(cli) -> bool:
        """这个客户端手里的上下文是否可疑（未开放 / 超龄）。详见 `_context_suspect`。"""
        try:
            if not cli.is_open:
                return True
            age = cli.store_age_s
        except Exception:
            # 测试替身没有这两个属性 → 不下判断，当作「不可疑」。
            # 把「不知道」当成「可疑」会让每轮都白抓一遍上下文。
            return False
        return age is not None and age > CONTEXT_MAX_AGE_S

    def _context_suspect(self) -> bool:
        """手里的上下文「可能已经不新鲜」—— 值得重新抓一遍。

        判据两条，任一成立即算可疑：
          · `not is_open`：教务处于未开放期（或本地闸门还没放开）。此时手里的
            do_jxb_id 是上一阶段拿到的，多半已经不被受理；
          · 岁龄 > `CONTEXT_MAX_AGE_S`：上下文抓得太久，轮次/令牌都可能过期。

        ⚠️ **岁龄未知（None）不算可疑**。None 只出现在「客户端不支持该字段」或
        「从没抓过上下文」两种情形：前者（测试替身）不该被强行重抓，后者
        `init()` 本来就会真抓一次。
        """
        return self._client_suspect(self.client)

    def _grab_context(self, *, reason: str = "", force: bool = False) -> bool:
        """把选课上下文刷成「当下这一刻的」。返回 True 表示确实重抓了。

        `force=False` 时只在**上下文可疑**时才真抓（未开放 / 超龄），
        所以正常开放、刚抓过的情况下调用它是零请求的 —— 可以放心地每轮都调。

        只把 `SESSION_EXPIRED` 上抛：登录态没了是**不可自愈**的，必须让上层
        立刻停下来提示重新登录；其它错误（网络抖动、临时的加密串错误）在预热相
        下一轮再试就是了，没必要整轮判死。
        """
        if not force and not self._context_suspect():
            return False

        try:
            self.client.init(force=True)
        except XKError as e:
            if e.kind is FailureKind.SESSION_EXPIRED:
                raise
            self._log(
                EventType.LOG,
                None,
                f"重抓选课上下文失败（{e.kind.value}）：{e} —— 沿用现有上下文",
            )
            return False

        self._log(
            EventType.LOG,
            None,
            f"重抓选课上下文（{reason or '超龄'}）："
            f"选课{'已开放' if self.client.is_open else '未开放'}，"
            f"{len(getattr(self.client, 'tabs', []) or [])} 个课程类别",
        )
        return True

    # -- 模式 1：立即执行（原行为，逐项 解析→预检→提交）----------------------

    def _run_immediate(self, t0: float) -> None:
        self._dispatch(self.plan.sorted_items())

    # -- 模式 2：定时开抢（预热相 → 倒计时 → 开火相）------------------------

    def _run_scheduled(self, t0: float) -> None:
        clock = self.client.clock
        plan = self.plan

        # 时钟没校准就先补采几次 —— 卡点全靠它，宁可在 T0 前多花 1 秒
        if not clock.synced:
            self._log(EventType.CLOCK, None, "时钟尚未校准，先主动采样…")
            try:
                self.client.sync_clock(4)
            except Exception as e:  # 采样失败不致命，退化为按本地挂钟推进
                logger.warning("主动时钟采样失败：%s", e)
        self._emit(
            Event(
                type=EventType.CLOCK,
                message=clock.describe(),
                extra=clock.as_dict(),
            )
        )
        if not clock.synced:
            self._log(
                EventType.LOG,
                None,
                "⚠️ 时钟未校准（服务器未回 Date 头？），只能按本地挂钟推进，"
                "误差可能达数秒",
            )

        lead = plan.fire_lead_s if plan.fire_lead_s is not None else clock.lead_seconds()
        target = plan.start_at - lead
        deadline = clock.monotonic_at(target)
        self._log(
            EventType.LOG,
            None,
            f"定时开抢：目标服务器时刻 {_fmt_server(target)}，"
            f"提前 {lead * 1000:.0f} ms 开火（校准不确定度决定的保守量）",
        )

        # --- 预热相 ---
        warm_start_mono = max(
            time.monotonic(), clock.monotonic_at(plan.start_at - plan.warmup_s)
        )
        self._emit(
            Event(
                type=EventType.PREWARM_START,
                message=f"进入预热：在 {_fmt_delta(deadline - time.monotonic())} 内解析全部教学班",
                extra={"deadline_monotonic": deadline},
            )
        )
        self._prewarm(warm_start_mono, deadline)
        if self._stop.is_set():
            self._mark_remaining(TaskState.ABORTED)
            return

        # --- 倒计时 ---
        self._wait_until(deadline)
        if self._stop.is_set():
            self._mark_remaining(TaskState.ABORTED)
            return

        # --- 开火相 ---
        self._emit(
            Event(
                type=EventType.FIRE,
                message=f"开火（服务器时刻 {_fmt_server(clock.server_now())}，"
                        f"已就绪 {len(plan.ready)}/{len(plan.items)} 项，"
                        f"派发方式 {'轮流发送' if plan.retry_mode == 'round_robin' else '串行'}）",
            )
        )
        self._dispatch(plan.sorted_items())

    def _prewarm(self, start_mono: float, deadline: float) -> None:
        """反复尝试解析 do_id，直到全部就绪或到点。

        采样间隔按 warmup_backoff 退避（首轮 warmup_interval_ms，封顶
        warmup_max_interval_ms）。开放前教学班查不到，密集轮询既无收益又招眼，
        退避后采样点自然向 T0 收拢。

        ⭐ **每一轮都先把上下文刷成当下的**（P0）：这是「教务一恢复开放就能立刻
        用上真上下文」的唯一保证 —— 否则整段预热相都在拿上午那份上下文空转，
        到 T0 才第一次真问服务器，等于把 3 跳 ≈ 0.9 s 的刷新挂在开火后面。
        """
        plan = self.plan
        items = plan.sorted_items()
        interval = plan.warmup_interval_ms / 1000.0
        rounds = 0
        announced_closed = False

        while not self._stop.is_set():
            now = time.monotonic()
            if now >= deadline:
                break
            if now < start_mono:  # 还没到预热起点（warmup_s 比剩余时间还长）
                self._sleep_until(min(start_mono, deadline))
                continue

            rounds += 1

            # ① 上下文刷成当下的。教务一恢复开放，这一轮就拿到真上下文。
            try:
                self._grab_context(reason="预热")
            except XKError as e:
                self._handle_fatal(e)
                self._stop.set()
                return

            # ② 未开放期**不去查教学班**：查了也会被本地闸门挡下（零请求），
            #    只会刷一屏没有信息量的失败日志。安静等下一轮。
            if not self.client.is_open:
                if not announced_closed:
                    announced_closed = True
                    self._log(
                        EventType.LOG,
                        None,
                        "教务尚未开放选课，预热相转为「静候开放」：每轮重抓一次上下文，"
                        "一开放立刻解析教学班",
                    )
            else:
                if announced_closed:
                    announced_closed = False
                    self._log(EventType.LOG, None, "教务已开放选课，开始解析教学班")

                # 「待解析」= 没有令牌的；**手里的令牌可疑的也要重解析** ——
                # 清单可能是上午建的，那个 do_jxb_id 到下午已经躺了好几个小时。
                pending = [it for it in items if self._token_suspect(it)]
                if not pending:
                    self._emit(
                        Event(
                            type=EventType.PREWARM_READY,
                            message=f"预热完成：{len(items)} 项教学班全部就绪，静候到点",
                            extra={"rounds": rounds},
                        )
                    )
                    return

                for it in pending:
                    if self._stop.is_set() or time.monotonic() >= deadline:
                        break
                    try:
                        self._resolve_do_id(it)
                    except XKError as e:
                        if e.kind is FailureKind.SESSION_EXPIRED:
                            self._handle_fatal(e)
                            self._stop.set()
                            return
                        self._log(EventType.LOG, it,
                                  f"预热未取到教学班（{e.kind.value}），稍后重试")
                    else:
                        if it.do_id:
                            self._log(EventType.LOG, it, f"预热就绪：{it.label}")

                if all(it.do_id for it in items):
                    self._emit(
                        Event(
                            type=EventType.PREWARM_READY,
                            message=f"预热完成：{len(items)} 项教学班全部就绪，静候到点",
                            extra={"rounds": rounds},
                        )
                    )
                    return

            # 退避等待（可被 stop 打断），但不越过 deadline
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            if self._sleep_until(min(deadline, time.monotonic() + interval)):
                return  # 被 stop 打断
            interval = min(
                interval * plan.warmup_backoff,
                plan.warmup_max_interval_ms / 1000.0,
            )

        left = [it.label for it in items if not it.do_id]
        if left:
            self._log(
                EventType.LOG,
                None,
                f"预热结束，仍有 {len(left)} 项未取到教学班（{', '.join(left)}），"
                f"开火时再试一次",
            )

    def _wait_until(self, deadline: float) -> None:
        """等到单调钟 deadline。最后 SPIN_WINDOW_S 忙等，避免 sleep 粒度过冲。"""
        last_announced: int | None = None
        while not self._stop.is_set():
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            if remain <= SPIN_WINDOW_S:
                while time.monotonic() < deadline:
                    pass  # 忙等：这是唯一能把精度压到毫秒的办法
                return

            sec = int(remain)
            if sec != last_announced and (sec <= 10 or sec % 30 == 0):
                last_announced = sec
                self._emit(
                    Event(
                        type=EventType.COUNTDOWN,
                        message=f"距开火还有 {sec} 秒",
                        extra={"remain_s": round(remain, 1)},
                    )
                )
            # 留出忙等窗口，剩下的时间交给 sleep
            if remain > 1.0:
                self._sleep_until(min(deadline - SPIN_WINDOW_S, time.monotonic() + 1.0))
            else:
                self._sleep_until(deadline - SPIN_WINDOW_S)

    def _sleep_until(self, target: float) -> bool:
        """睡到单调钟 target。返回 True 表示被 stop 打断。"""
        remain = target - time.monotonic()
        return self._stop.wait(remain) if remain > 0 else False

    def _mark_remaining(self, state: TaskState) -> None:
        for it in self.plan.items:
            if it.state in (TaskState.PENDING, TaskState.RUNNING):
                it.state = state

    # -- 派发：把 tick 串起来 -------------------------------------------------
    #
    # ⭐ 这一段是 2026-10-01 改造的重点。原来 `_fire_item` 里那个
    #       while item.attempts < item.max_attempts and not self._stop.is_set():
    # 是「一项跑到死」的循环 —— 只要一发 submit 卡住（教务高峰期是常态），
    # 后面**所有**课都得干等它，而且没有任何时间预算能把这一项撬开（队头阻塞）。
    #
    # 现在拆成「tick 原语 + 派发策略」两层：
    #   · `_tick(item)`：推进这一项**最多发一个请求**，然后立刻交还控制权；
    #   · `_drive_serial` / `_drive_round_robin`：决定下一个 tick 给谁。
    #
    # 两种模式**共用同一批 tick 实现**，所以判定、日志、状态全部一致，
    # 差别只在「谁先发下一发」。因此换模式不会改变「什么算抢到、什么算失败」。

    def _dispatch(self, items: list[PlanItem]) -> None:
        """开火相 / 立即模式的统一入口：按 `plan.retry_mode` 选派发方式。"""
        mode = normalize_retry_mode(self.plan.retry_mode)
        if mode != self.plan.retry_mode:
            # 落盘 / API 里可能有旧值或别名，这里统一一次，并**如实说**改成了什么
            self._log(
                EventType.LOG,
                None,
                f"派发方式“{self.plan.retry_mode}”无法识别，按“{mode}”执行",
            )
            self.plan.retry_mode = mode
        if len(items) > 1:
            self._log(
                EventType.LOG,
                None,
                "派发方式：轮流发送（每项每回合发 1 个请求，不等上一发回来）"
                if mode == "round_robin"
                else "派发方式：串行（一项打到终态才轮到下一项）",
            )
        if mode == "round_robin":
            # ⭐ 蹲课（所有项 max_attempts <= 0）→ 打开全局节流：发完一门隔
            # interval_ms 再发下一门，而不是一轮内背靠背全发。用户明确要求
            # 「一节课发完隔 1s/0.8s 再发第二节课，而不是按批次来」。
            # 抢课 round_robin 保持原样（T0 火力摊开，刻意同时发）。
            if items and all(it.max_attempts <= 0 for it in items):
                self._global_interval_s = (items[0].interval_ms or 1000) / 1000.0
            self._drive_round_robin(items)
        else:
            self._drive_serial(items)

    def _drive_serial(self, items: list[PlanItem]) -> None:
        """串行：一项打到终态，才轮到下一项（旧的严格串行语义）。"""
        for item in items:
            if self._stop.is_set():
                break
            if self._over_round_deadline():
                self._log(EventType.LOG, item, "已达总时限，停止")
                break
            tk = self._tick_of(item)
            while not self._tick(item):
                # 这一项在等自己的退避间隔（或等教务开放）—— 串行模式下就老实等
                if not self._idle_until(self._tick_of(item).ready_at):
                    break
                if self._over_round_deadline():
                    break
            if not tk.finished:
                # 兜底：被 stop / 总时限打断在中间
                self._give_up(
                    item,
                    tk,
                    "用户中止" if self._stop.is_set() else "已达总时限，停止",
                )
            if self.plan.stop_on_first_win and item.state is TaskState.WON:
                self._log(EventType.LOG, None, "已抢到目标，按策略停止剩余项")
                break
        if self._stop.is_set():
            self._mark_remaining(TaskState.ABORTED)

    def _drive_round_robin(self, items: list[PlanItem]) -> None:
        """轮流发送：每回合给每一项发**最多一个请求**，谁都不等谁。

        ⭐ 这就是「队头阻塞」的解药：某项那一发卡住（教务高峰常态），本回合它反正
        已经交还控制权了，下一项立刻发自己的那一发。于是「每个课程申请都能在最短
        时间内先提交一遍」。

        ⚠️ 代价（如实说明，不藏着）：T0 那一刻不再是「只发一个请求而是连发 N 个」——
        火力被摊开了。所以两种模式各有用处：
          · 清单里只有一门势在必得 → `serial`：火力集中，命中即收工；
          · 清单里好几门都想抢、怕被一发卡住 → `round_robin`：先普遍占一遍位。

        ⭐ 蹲课（`_global_interval_s > 0`）走**节流轮转**：发完一门隔 interval_ms
        再发下一门（用户「发一门隔 1s/0.8s 再发下一门，不是按批次」），而不是一轮
        内背靠背全发。用轮转指针保证 A→B→C→A 的公平顺序，而不是排前面的项独占。
        """
        alive = [it for it in items if it.state in (TaskState.PENDING, TaskState.RUNNING)]
        if self._global_interval_s:
            self._drive_throttled(alive)
            return
        rounds = 0
        while alive and not self._stop.is_set():
            if self._over_round_deadline():
                for it in alive:
                    self._give_up(it, self._tick_of(it), "已达总时限，停止")
                break
            rounds += 1
            progressed = False
            for item in list(alive):
                tk = self._tick_of(item)
                if tk.finished or item.state not in (TaskState.PENDING, TaskState.RUNNING):
                    alive.remove(item)      # 已被互斥跳过 / 已在别处判死
                    continue
                if time.monotonic() < tk.ready_at:
                    continue                # 还在自己的退避里 → 先让别人发，不阻塞
                progressed = True
                if self._tick(item):
                    alive.remove(item)
                    if self.plan.stop_on_first_win and item.state is TaskState.WON:
                        self._log(EventType.LOG, None, "已抢到目标，按策略停止剩余项")
                        return
            if not alive or self._stop.is_set():
                break
            if not progressed:
                # 全体都在退避：睡到最早那个之前（此刻没有任何请求在飞，不会误事）
                nxt = min(self._tick_of(it).ready_at for it in alive)
                if not self._idle_until(nxt):
                    break
        if self._stop.is_set():
            self._mark_remaining(TaskState.ABORTED)

    def _drive_throttled(self, alive: list[PlanItem]) -> None:
        """蹲课专用：**全局节流**的轮流发送。

        发一门课的请求后，隔 `interval_ms` 再发下一门课（不管是不是同一门）。
        用轮转指针保证公平：A→B→C→A→B→C，谁都不独占。

        与抢课 round_robin 的区别：抢课要「T0 火力摊开、一轮内背靠背全发」；
        蹲课是小时级长任务，密集连发只会吃「选课频率过高」限流，还让日志糊成
        一坨（同一秒多条）。所以这里**每一发之间都严格隔 interval_ms**。
        """
        idx = 0  # 轮转指针：下一发优先看 alive[idx]
        while alive and not self._stop.is_set():
            if self._over_round_deadline():
                for it in alive:
                    self._give_up(it, self._tick_of(it), "已达总时限，停止")
                break
            # 清理已到终态的项（互斥跳过 / 判死），避免它们一直占着轮转位
            alive = [it for it in alive
                     if not self._tick_of(it).finished
                     and it.state in (TaskState.PENDING, TaskState.RUNNING)]
            if not alive:
                break
            n = len(alive)
            idx %= n
            now = time.monotonic()
            chosen = None
            # 从 idx 起轮转一圈，找第一个「自己的退避已到、且全局节流也到」的项
            for step in range(n):
                item = alive[(idx + step) % n]
                tk = self._tick_of(item)
                if now < tk.ready_at:
                    continue
                if now < self._global_ready_at:
                    continue
                chosen = (item, (idx + step) % n)
                break
            if chosen is None:
                # 这一圈没有能发的：睡到最近的「退避结束」或「全局节流结束」
                targets = [self._tick_of(it).ready_at for it in alive]
                targets.append(self._global_ready_at)
                if not self._idle_until(min(targets)):
                    break
                continue
            item, pos = chosen
            if self._tick(item):
                # 这一项到终态了 → 从 alive 摘掉，指针回拨到它原来的位置
                alive.remove(item)
                idx = pos % len(alive) if alive else 0
            else:
                idx = (pos + 1) % n
            # ⭐ 这一发打出去了，下一发（不管哪门）都要再隔 interval_ms
            self._global_ready_at = time.monotonic() + self._global_interval_s
        if self._stop.is_set():
            self._mark_remaining(TaskState.ABORTED)

    def _idle_until(self, target: float) -> bool:
        """分段睡到单调钟 target。返回 False = 被打断（停止 / 已达总时限）。

        ⚠️ 为什么要分段而不是一把 `_stop.wait(remain)`：总时限必须在**等待期间**
        也能生效。旧代码只在「项与项之间」检查总时限，于是一发卡住的请求能把
        `global_deadline_s` 拖穿（实测：两项清单最坏十几分钟）。
        """
        while True:
            if self._stop.is_set():
                return False
            now = time.monotonic()
            remain = target - now
            if remain <= 0:
                return True
            if self.plan.global_deadline_s:
                left = self.plan.global_deadline_s - (now - self._t0)
                if left <= 0:
                    return False
                remain = min(remain, left)
            if self._stop.wait(remain):
                return False

    # -- tick：一项一次，最多一个请求 -----------------------------------------

    def _tick_of(self, item: PlanItem) -> _Tick:
        """取（必要时建）这一项的分步进度。"""
        tk = self._ticks.get(id(item))
        if tk is None:
            tk = self._ticks[id(item)] = _Tick(started_at=time.monotonic())
        return tk

    def _over_round_deadline(self) -> bool:
        d = self.plan.global_deadline_s
        return bool(d) and self._t0 > 0 and (time.monotonic() - self._t0) > d

    def _tick(self, item: PlanItem) -> bool:
        """推进一项。返回 True = 这一项已到终态（抢到 / 失败 / 跳过 / 中止）。

        **保证：一次调用最多发一个请求**（唯一的例外是「教务未开放」那条路要重抓
        上下文，一次 Index+Display ≈ 2 个请求 —— 它本身就是「读一遍最新状态」，
        拆成两半反而会拿半个上下文去发请求）。这条保证是轮流发送能成立的前提：
        每一项每回合只占一个 RTT，卡住也只会卡一个 RTT。
        """
        tk = self._tick_of(item)
        if tk.finished:
            return True
        if item.state not in (TaskState.PENDING, TaskState.RUNNING):
            # 已经被「互斥跳过」（同时间段别的课先抢到了）/ 已在别处判死 ——
            # 一项请求都不发，这才是省服务端配额的地方
            tk.finished = True
            return True

        now = time.monotonic()
        if now < tk.ready_at:
            return False

        # 单项时间预算（0 = 不限）。防的是「一项在卡顿里反复超时，把整轮时间吃光」：
        # max_attempts 只数次数，不数时间，而卡顿时一次尝试最坏 6 s
        # （TIMEOUT_CRITICAL 的 2 + 4），跑满上限就是好几分钟。
        if item.budget_s and (now - tk.started_at) > item.budget_s:
            self._give_up(item, tk, f"单项时限 {item.budget_s:g} 秒内未抢到")
            return True
        # 整轮总时限：在 tick 内部也查（不再是「项与项之间」才查）
        if self._over_round_deadline():
            self._give_up(item, tk, "已达总时限，停止")
            return True

        item.state = TaskState.RUNNING
        while True:
            phase = tk.phase
            if phase is _Phase.RESOLVE:
                r = self._tick_resolve(item, tk)
            elif phase is _Phase.PRECHECK:
                r = self._tick_precheck(item, tk)
            elif phase is _Phase.SUBMIT:
                r = self._tick_submit(item, tk)
            else:
                tk.finished = True
                return True

            if r is _FINISH:
                tk.finished = True
                return True
            if r is _NEXT:
                return False
            # _ADVANCE：这一相位零请求走完，立刻进入下一相位
            # ⚠️ 每个 _ADVANCE 都必须把 tk.phase 改成**新的**相位，
            #    否则这里会原地打转（曾经想过用 for 循环限制次数，但那是治标 ——
            #    真正的不变式是「_ADVANCE 必换相位」，写在这里当契约）。

    def _tick_resolve(self, item: PlanItem, tk: _Tick) -> int:
        """相位 1：补齐 do_id（预热没成功 / 立即模式下必走这条路）。

        单独相位 + 独立重试预算（`RESOLVE_TRIES_MAX`），理由与旧 `_ensure_do_id` 相同：
        解析走 `query_classes`，同样会撞网络抖动，**不能**让它一次失败就判死
        （那会让「网络类可重试」在这条路径上完全失效），也不能让它吃掉
        `item.attempts`（那是留给提交的额度）。
        """
        if item.do_id:
            tk.phase = _Phase.PRECHECK
            return _ADVANCE

        tk.resolve_tries += 1
        try:
            self._resolve_do_id(item)
        except XKError as e:
            label = KIND_LABEL.get(e.kind, e.kind.value)
            if e.kind is FailureKind.SESSION_EXPIRED:
                # 登录态没了不可自愈 → 立刻停（上层会提示重新登录）
                self._handle_fatal(e)
                item.state = TaskState.ABORTED
                return _FINISH
            if e.kind is FailureKind.NOT_OPEN:
                # 「提前点了开始」的**常态**，不是失败：带着空令牌进提交相位。
                # ⚠️ 安全性：NOT_OPEN 只可能在未开放时抛出，此刻 `is_open` 必为 False，
                # 所以下一相位里的 submit 会被本地闸门挡下（**零请求**），不会盲发。
                # 之后靠 `_tick_submit` 的 NOT_OPEN 分支每轮重抓上下文，教务一开放
                # 就会自己把教学班解析出来（空 do_id 必被判为「令牌可疑」）。
                self._log(
                    EventType.LOG,
                    item,
                    "教务尚未开放，先带着空令牌进提交循环等它开放"
                    "（开放后会自动解析教学班）",
                )
                tk.phase = _Phase.PRECHECK
                return _ADVANCE
            if not e.kind.retryable() or tk.resolve_tries >= RESOLVE_TRIES_MAX:
                if e.kind.retryable():
                    item.state = TaskState.SKIPPED
                    self._log(
                        EventType.GIVE_UP,
                        item,
                        f"试了 {tk.resolve_tries} 次仍未解析到教学班（{label}），"
                        f"放弃该项：{e}",
                        kind=e.kind,
                    )
                else:
                    self._handle_fatal(e)
                    item.state = TaskState.ABORTED
                return _FINISH
            self._log(
                EventType.LOG,
                item,
                f"解析教学班没成功（{label}，第 {tk.resolve_tries}/{RESOLVE_TRIES_MAX} 次），"
                f"稍后重试：{e}",
            )
            tk.ready_at = time.monotonic() + item.interval_ms / 1000.0
            return _NEXT

        if item.do_id:
            tk.phase = _Phase.PRECHECK
            return _ADVANCE
        if tk.resolve_tries >= RESOLVE_TRIES_MAX:
            item.state = TaskState.SKIPPED
            self._log(
                EventType.GIVE_UP,
                item,
                "未解析到可用教学班（可能已选满，或本轮该类别没有可选班级）",
                kind=FailureKind.NOT_OPEN,
            )
            return _FINISH
        self._log(
            EventType.LOG,
            item,
            f"这次没挑到可用教学班（第 {tk.resolve_tries}/{RESOLVE_TRIES_MAX} 次），稍后重试",
        )
        tk.ready_at = time.monotonic() + item.interval_ms / 1000.0
        return _NEXT

    def _tick_precheck(self, item: PlanItem, tk: _Tick) -> int:
        """相位 2：时间冲突预检（零副作用，铁律 #2/#7）。

        刻意留在开火相而不是预热相：预检判的是「与**已选**是否冲突」，而抢到第一门
        之后已选就变了，早早预检的结果会过期（自己造出来的冲突漏判）。
        """
        # ⚠️ do_id 为空时**不发预检**：预检 body 要带 jxb_ids，空令牌打过去只会
        # 换回一个「加密串错误」，白白吃掉开火瞬间的一个 RTT —— 而这一刻每一跳都值钱。
        # （旧写法这里会盲发一发、必然失败、再降级成直接提交，白烧一个请求。）
        if not item.precheck or not item.do_id:
            tk.phase = _Phase.SUBMIT
            return _ADVANCE

        try:
            ct = self.client.precheck_conflict(item.kch_id, item.do_id)
            if _conflict(ct):
                item.state = TaskState.SKIPPED
                self._log(
                    EventType.GIVE_UP, item, "时间冲突，跳过该项", kind=FailureKind.CONFLICT
                )
                return _FINISH
        except XKError as e:
            # 预检失败不致命，降级为直接提交
            self._log(EventType.LOG, item, f"预检失败，改为直接提交：{e}")
        tk.phase = _Phase.SUBMIT
        return _NEXT

    def _tick_submit(self, item: PlanItem, tk: _Tick) -> int:
        """相位 3：提交（写请求）。一次 tick 只发这一发。

        ---- 严格串行 / 轮流派发共同的前提 ----
        教务的 `do_jxb_id` 是**一次性令牌**：查一次教学班就重新下发一个、作废旧的。
        所以「刷新令牌」这个动作本身带着副作用（会当场废掉手里那个），
        绝不能和「用旧令牌打一发」并行（文件头有完整论证）。

        本相位遵守的纪律（两种派发模式下**完全一致**）：
          ① 先拿手里的令牌打一发（此刻没人和它抢令牌）；
          ② 只有这一发被打回来（且属于「请求被拒」语义）才去重查换新令牌；
          ③ 首发就成功时，一个刷新请求都不用发。

        ⭐ 响应与课程的配对：本方法是**同步调用** —— `submit()` 返回时就地判定，
        期间没有第二个在途请求，所以这个响应**一定**属于 `item`（就是正在 tick 的这一项）。
        轮流派发靠的是「tick 之间让出控制权」，不是「多个请求并行」，因此不会出现
        「响应回来了不知道是哪门课的」这种错配。
        """
        # max_attempts <= 0 表示「不限次数」（蹲课按时间不按次数），跳过次数闸。
        if item.max_attempts > 0 and item.attempts >= item.max_attempts:
            self._give_up(item, tk, "达尝试上限未成功")
            return _FINISH

        item.attempts += 1
        t_attempt = time.monotonic()
        self._emit(
            Event(
                type=EventType.ATTEMPT,
                item_key=item.key,
                message=(f"第 {item.attempts} 次尝试"
                         if item.max_attempts <= 0
                         else f"第 {item.attempts}/{item.max_attempts} 次尝试"),
                attempt=item.attempts,
            )
        )
        try:
            res = self.client.submit(item.kch_id, item.do_id, kcmc=item.kcmc)
        except XKError as e:
            res = {"success": False, "flag": "", "msg": str(e), "kind": e.kind}

        cost = int((time.monotonic() - t_attempt) * 1000)
        item.last_kind = res.get("kind")
        item.last_msg = res.get("msg", "")
        # 铁律 #10：满员时把服务端下发的真实人数写进消息，解释「为什么没抢到」。
        # ⚠️ 2026-10-01 起满员是**终态失败、只发这一发** —— 这条消息就是面板上
        # 用户能看到的**唯一**解释，所以更要写全（人数 + 本轮已选），不能只留原文。
        fi = res.get("full_info")
        if fi:
            item.last_msg = (
                f"教学班已满（已选 {fi['yxzrs']} 人"
                + (f"，本轮已选 {fi['blyxrs']}" if fi.get("blyxrs") else "")
                + "）"
            )

        if res.get("success"):
            return self._win(item, tk, cost)

        kind = res.get("kind")

        # ⭐⭐ 「该教学班已选中」（flag="6"）—— 这是**成功**，不是失败。
        #
        # 为什么必须单独拦下：`TIMEOUT_CRITICAL` 把提交超时压到 2+4 秒之后，
        # 「请求其实成功了、只是响应没及时回来」的概率明显上升。此时重试同一发，
        # 教务发现这个班已在名下，回的就是 6。
        # 旧代码把它归 ALREADY_TAKEN（不可重试）→ 判 FAILED → 界面对用户说「失败」，
        # 而课其实已经选上了 —— 撒谎，而且用户会去重复操作。
        #
        # 与「只能选一个教学班」（ALREADY_TAKEN）**必须分开**：后者说的是这门课
        # 已经有**别的**班了，本项并没有达成目标。两者只差一个字，结论正好相反。
        # 官方前端 `zzxkYzbChoosedZy.js:1367` 对 flag=6 的处理也是一句
        # 「该教学班已选中，刷新页面可见！」—— 根本不当作错误。
        if kind is FailureKind.ALREADY_IN_CLASS:
            # 把「凭什么判定抢到」写进 last_msg —— 界面与日志都读它。
            # ⚠️ 不能把这一句只放在事件消息里：last_msg 是**面板上显示的那一句**，
            # 只写原文（"0,J1,46,"）等于什么都没解释（铁律 #10）。
            item.last_msg = (
                f"教务回复「该教学班已选中」（flag=6）：该班已在你名下"
                + (f"（原文 {item.last_msg}）" if item.last_msg else "")
            )
            return self._win(item, tk, cost, note="教务确认该教学班已在你名下")

        # ⚠️ NOT_OPEN 单独一条路（P0 的最后一环）。
        # 它不是「教务拒绝了这次提交」，而是**本地闸门**挡下的 —— submit 一个
        # 请求都没发，只说明「我们手里的上下文还认为没开放」。
        # 而教务很可能恰好就在这一刻开放，所以必须「重抓上下文再试」，
        # 绝不能像其它不可重试错误那样直接判死：判死就等于把
        # 「卡在开放瞬间」这种情况整项丢掉。
        #
        # ⭐ 这就是「未开放」唯一的正解：**重新读一遍上下文**，而不是硬打一发赌它受理。
        #
        # 耐心按**时间**算（`NOT_OPEN_GRACE_S`）。⚠️ 关键：判死之前**必须先刷一次**。
        # 旧写法是「先看次数够不够，够了就判死，不够才去刷」—— 于是最后一次重抓
        # 之后到判死之间那段等待（`NOT_OPEN_RETRY_WAIT_S`）里教务开了也没人知道：
        # 实测「提前 8.3 秒点、教务第 9 秒开」会被整项丢掉。
        if kind is FailureKind.NOT_OPEN:
            if tk.waited_closed >= NOT_OPEN_GRACE_S:
                item.state = TaskState.FAILED
                self._log(
                    EventType.GIVE_UP,
                    item,
                    f"等了 {tk.waited_closed:.0f} 秒教务仍未开放"
                    f"（重抓上下文 {tk.ctx_refreshes} 次），判定本项失败：{item.last_msg}",
                    kind=kind,
                )
                return _FINISH
            tk.ctx_refreshes += 1
            self._log(
                EventType.LOG,
                item,
                f"提交被本地闸门挡下（教务未开放），重抓上下文后重试"
                f"（第 {tk.ctx_refreshes} 次，还剩 "
                f"{max(NOT_OPEN_GRACE_S - tk.waited_closed, 0):.0f} 秒耐心）",
            )
            was_open = self.client.is_open
            # ⚠️ 手里这个上下文必须由**本线程自己**刷：闸门看的是
            # `self.client.is_open`，其它任何客户端刷出「已开放」都不等于我们这边开了。
            try:
                self._grab_context(force=True, reason="提交被本地闸门挡下")
            except XKError as e:
                self._handle_fatal(e)
                item.state = TaskState.ABORTED
                return _FINISH
            if self.client.is_open and not was_open:
                # 刚刚才翻成「已开放」→ 开放前攒的那些等待都是旧账，一笔勾销
                tk.waited_closed = 0.0
                self._log(
                    EventType.LOG,
                    item,
                    "教务已开放，耐心计时清零，转入正常抢课循环",
                )
            # ⚠️ 上下文刚由「未开放」翻成「已开放」时，手里那个令牌是上一阶段
            # 拿的（很可能就是上午的），必须顺手重解析一次 —— 否则下一发拿着
            # 旧令牌打过去，白白多烧一个来回才发现要刷新。
            if self.client.is_open and self._token_suspect(item):
                try:
                    self._resolve_do_id(item)
                except XKError as e:
                    self._log(EventType.LOG, item, f"重解析教学班失败：{e}")
            tk.waited_closed += NOT_OPEN_RETRY_WAIT_S
            tk.ready_at = time.monotonic() + NOT_OPEN_RETRY_WAIT_S
            return _NEXT

        # ⭐ 蹲课（max_attempts <= 0）下，满员**可重试**：蹲的恰恰就是「有人退课」
        #   腾出来的名额，所以 FULL 不是终态，而是「继续等下一发」。
        #   ⚠️ 与抢课（max_attempts > 0）严格区分：抢课里 FULL 是终态失败（名额不会
        #   在 800ms 内自己冒出来，重试白烧配额）；蹲课是按时间框定的长任务，
        #   满员正是它要持续试探的状态，必须走退避重试直到有人退课或到点。
        #   判据用 `item.max_attempts <= 0`：这是「蹲课按时间不按次数」的既有信号，
        #   与次数闸（上面第 1042 行）用同一个判据，不会引入第二个口径。
        if kind is FailureKind.FULL and item.max_attempts <= 0:
            self._emit(
                Event(
                    type=EventType.RETRY_WAIT,
                    item_key=item.key,
                    message=(item.last_msg or "教学班已满") + "，继续蹲退课名额",
                    attempt=item.attempts,
                    kind=kind,
                    elapsed_ms=cost,
                )
            )
            tk.ready_at = time.monotonic() + self._backoff_s(item, tk, kind)
            return _NEXT

        if kind is not None and not kind.retryable():
            item.state = TaskState.FAILED
            self._log(EventType.GIVE_UP, item, f"不可重试：{item.last_msg}", kind=kind)
            return _FINISH

        # 铁律 #5：连续「请求被拒」到阈值 → 重查刷新令牌
        #
        # 「教务正常回了话」的失败（冲突 / 频率过高）**不算数**（令牌没问题），
        # 所以先把 streak 清零，避免「拒了几十次 → 忽然去重查」这种无意义动作。
        # ⚠️ 满员走不到这里（终态失败已在上面 return）；别以为这里还替它清 streak。
        if kind in _TOKEN_STALE_KINDS:
            tk.stale_streak += 1
        else:
            tk.stale_streak = 0

        # 连续到阈值 → 同步重查教学班、换一个新令牌（铁律 #5）。
        # ⚠️ 这一步是**串行**的：它会产生新令牌、作废手里那个旧的，
        # 所以只能在「刚才那一发已经打出去了、并且被打回来了」之后做。
        if tk.stale_streak >= REFRESH_AFTER_STALE and tk.refreshes < MAX_TOKEN_REFRESH:
            streak = tk.stale_streak
            tk.refreshes += 1
            tk.stale_streak = 0
            self._log(
                EventType.LOG,
                item,
                f"连续 {streak} 次请求被拒（{item.last_msg or kind}），刷新令牌"
                f"（第 {tk.refreshes}/{MAX_TOKEN_REFRESH} 次）",
            )
            if not self._refresh_do_id(item):
                item.state = TaskState.FAILED
                self._log(
                    EventType.GIVE_UP,
                    item,
                    f"令牌刷新失败且无法重新解析教学班：{item.last_msg}",
                    kind=kind,
                )
                return _FINISH

        self._emit(
            Event(
                type=EventType.RETRY_WAIT,
                item_key=item.key,
                message=item.last_msg or "未成功，准备重试",
                attempt=item.attempts,
                kind=kind,
                elapsed_ms=cost,
            )
        )
        tk.ready_at = time.monotonic() + self._backoff_s(item, tk, kind)
        return _NEXT

    @staticmethod
    def _backoff_s(item: PlanItem, tk: _Tick, kind) -> float:
        """两次提交之间的等待（秒）。

        · 网络类（NETWORK）：阶梯退避 1× → 2× → 4× → 8× 封顶。
          理由见 `NETWORK_BACKOFF_MAX_S`：它表示「这次没问到」，而没问到的常见原因是
          教务正被挤爆 —— 此时密集重发只会火上浇油，还可能吃「选课频率过高」限流。
          这也是「轮流发送」不至于把教务打爆的安全带：N 项清单的自然节拍会被拉开。
        · 其它可重试失败（冲突 / 频率过高）：教务**正常回了话**，
          按用户设定的 `interval_ms` 走就行（不必刻意拉长）。
          满员不在此列 —— 它不重试，走不到这个函数。
        """
        base = item.interval_ms / 1000.0
        if kind is FailureKind.NETWORK:
            tk.net_streak += 1
            return min(base * (2 ** min(tk.net_streak - 1, 3)), NETWORK_BACKOFF_MAX_S)
        tk.net_streak = 0
        return base

    # -- 终态的收束 -----------------------------------------------------------

    def _win(self, item: PlanItem, tk: _Tick, cost_ms: int, *, note: str = "") -> int:
        """这一项抢到了（含「教务确认该班已在名下」这种情况）。"""
        item.state = TaskState.WON
        tk.finished = True
        if note:
            # 铁律 #10：日志要能解释「凭什么判定成抢到」——
            # 尤其是「提交超时后靠 flag=6 确认」这条路径，不写清楚就会像在瞎报成功。
            self._log(
                EventType.LOG,
                item,
                f"{note} —— 本项按「已抢到」处理，不再重发（避免重复占位）",
            )
        self._emit(
            Event(
                type=EventType.SUCCESS,
                item_key=item.key,
                message=(
                    f"选课成功（{note}）：{item.label}"
                    if note
                    else f"选课成功：{item.label}"
                ),
                attempt=item.attempts,
                elapsed_ms=cost_ms,
            )
        )
        self._skip_mutex(item)
        return _FINISH

    def _give_up(self, item: PlanItem, tk: _Tick, reason: str, *, kind=None) -> None:
        """把一项收成失败 / 中止。

        ⚠️ 「一次都没试过」的项**不标 FAILED**：那会说谎（它没失败，是根本没轮到）。
        保持 PENDING，只推一条日志说明为什么没轮到。
        """
        if tk.finished or item.state is TaskState.WON:
            return
        tk.finished = True
        if item.state in (TaskState.SKIPPED, TaskState.FAILED, TaskState.ABORTED):
            return
        if item.state is TaskState.PENDING and item.attempts == 0 and not self._stop.is_set():
            self._log(EventType.LOG, item, f"{reason}（本项尚未开始，保持待选）")
            return
        item.state = TaskState.ABORTED if self._stop.is_set() else TaskState.FAILED
        self._log(
            EventType.GIVE_UP,
            item,
            f"{reason}：{item.last_msg}" if item.last_msg else reason,
            kind=item.last_kind if kind is None else kind,
        )

    def _fire_item(self, item: PlanItem) -> None:
        """把**一项**一直推进到终态（旧接口）。

        内部已经换成 tick 状态机 —— 这里只是「只派发这一项」的串行模式，
        保留它是因为直接调它是最直观的「就抢这一门」用法。
        """
        self._drive_serial([item])

    # -- 辅助 ---------------------------------------------------------------

    def _skip_mutex(self, winner: PlanItem) -> None:
        """抢到一项后，把清单里与它**真的撞时间**的其他待选课全部跳过。

        口径 = `core.schedule.is_time_conflict`：星期 + 节次有交集 + **周次有交集**。
        周次必须一起看 —— `周二 3-5节{1-5周}` 与 `周二 3-5节{6-17周}` 永远不撞，
        抢到前者后把后者也跳过，等于白白放弃一门本来能上的课。

        真正互斥的两门课不可能同时上成，继续提交纯属白烧配额 —— 既可能触发
        「选课频率过高」的限流，也会挤占后续真正有机会的目标的提交窗口。

        与界面标注同口径（`Plan.conflict_pairs`）。

        只跳过 PENDING 的项 —— 已经在跑的当前项、以及已 WON 的项都不动。
        """
        losers = [
            o for o in self.plan.conflicts_with(winner)
            if o.state is TaskState.PENDING
        ]
        if not losers:
            return
        reason = f"与「{winner.kcmc or winner.kch_id}」时间真的撞了，不再提交"
        for o in losers:
            o.state = TaskState.SKIPPED
            o.last_msg = reason
            self._log(EventType.GIVE_UP, o, reason, kind=FailureKind.CONFLICT)
        self._log(
            EventType.LOG,
            None,
            f"互斥跳过 {len(losers)} 项（与「{winner.label}」时间冲突）："
            + "、".join(o.kcmc or o.kch_id for o in losers),
        )

    def _tab_for(self, item: "PlanItem"):
        """取 item 所属的 Tab：**下标优先**，kklxdm 只是兜底。

        `kklxdm` 会重复（我校两个「板块课」都是 06），只靠它永远命中第一个，
        加了大英一的课就会查到体育板块去。所以：

            tab_index >= 0  → tab_at(tab_index)  精确
            tab_index <  0  → find_tab(kklxdm)   兜底（可能命中同名 Tab 的第一个）

        下标越界也回退到 kklxdm —— 清单是跨会话恢复的（备份 JSON），
        教务这学期要是少了/多了个 Tab，下标会整体错位，此时宁可用 kklxdm 蒙一个，
        也不要直接返回 None 让整项抢不了。
        """
        if item.tab_index >= 0:
            tab = self.client.tab_at(item.tab_index)
            if tab is not None:
                return tab
            self._log(
                EventType.LOG,
                item,
                f"Tab 下标 {item.tab_index} 越界（本次会话只有 "
                f"{len(self.client.tabs)} 个板块），改按 kklxdm={item.kklxdm or '空'} 查找",
            )
        return self.client.find_tab(item.kklxdm) if item.kklxdm else None

    def _resolve_do_id(self, item: PlanItem) -> None:
        """给定 kch_id，取第一个未满的教学班作为 do_id。

        必须先按 item 的 Tab 切回对应板块 —— 每个 Tab 的 rwlx / xkly / bklx_id
        不同，且加密串不同，跨 Tab 查询会拿不到或拿错教学班。
        """
        tab = self._tab_for(item)
        classes = self.client.query_classes(
            item.kch_id, tab=tab, cxbj=item.cxbj, fxbj=item.fxbj
        )
        picked, note = _pick_class(classes, want_jxb_id=item.jxb_id)
        if picked is None:
            return
        item.do_id = picked.do_id
        item.jxb_id = picked.jxb_id
        item.token_at = time.monotonic()
        item.jsxx = item.jsxx or picked.jsxx
        self._log(
            EventType.LOG,
            item,
            f"已解析教学班（{note}）：{picked.sksj or ''} {picked.jsxx or ''} "
            f"({picked.yxzrs}/{picked.jxbrl})",
        )

    def _ensure_do_id(self, item: PlanItem) -> bool:
        """确保 item 手里有可用的 `do_jxb_id`。返回 True = 可以继续往下走（去提交）。

        三种结局：

        | 情形 | 返回 | 说明 |
        |---|---|---|
        | 拿到令牌 | True | 正常去提交 |
        | 教务**未开放**（NOT_OPEN） | True，但令牌仍为空 | 带着空令牌进提交循环 |
        | 其它明确失败 / 重试预算耗尽 | False | 已写好 `item.state`，调用方直接 return |

        ⚠️ 为什么要自带重试：解析靠 `query_classes`，它同样会撞上网络抖动 / 超时。
        旧写法一次失败就 `ABORTED` —— 那会让「网络类可重试」这条策略在
        **这条路径上完全失效**（提交循环根本轮不到）。
        预算用独立常量，**不吃** `item.attempts`（那是留给提交的，别让解析花光）。

        ⭐ 「未开放」为什么不当失败：这是「提前点了开始」的**常态**，定时模式下
        预热相本来就一个教学班都解析不到、开火时必然走到这里。
        在提交循环里，NOT_OPEN 分支有 `NOT_OPEN_GRACE_S` 的耐心 + 每轮重抓上下文，
        教务一开放就会自己把教学班解析出来（空 `do_id` 必被判为「令牌可疑」）。
        在这里判死，等于把整项丢掉。
        ⚠️ 安全性：NOT_OPEN 只可能在**未开放**时抛出，此刻 `is_open` 必为 False，
        所以进循环后第一次 `submit` 会被本地闸门挡下（**零请求**），不会盲发空令牌。
        """
        if item.do_id:
            return True
        tries = 0
        while not item.do_id:
            tries += 1
            try:
                self._resolve_do_id(item)
            except XKError as e:
                label = KIND_LABEL.get(e.kind, e.kind.value)
                if e.kind is FailureKind.SESSION_EXPIRED:
                    # 登录态没了不可自愈 → 立刻停（上层会提示重新登录）
                    self._handle_fatal(e)
                    item.state = TaskState.ABORTED
                    return False
                if e.kind is FailureKind.NOT_OPEN:
                    self._log(
                        EventType.LOG, item,
                        "教务尚未开放，先带着空令牌进重试循环等它开放"
                        "（开放后会自动解析教学班）",
                    )
                    return True
                if not e.kind.retryable() or tries >= RESOLVE_TRIES_MAX:
                    if e.kind.retryable():
                        item.state = TaskState.SKIPPED
                        self._log(
                            EventType.GIVE_UP, item,
                            f"试了 {tries} 次仍未解析到教学班（{label}），放弃该项：{e}",
                            kind=e.kind,
                        )
                    else:
                        self._handle_fatal(e)
                        item.state = TaskState.ABORTED
                    return False
                self._log(
                    EventType.LOG, item,
                    f"解析教学班没成功（{label}，第 {tries}/{RESOLVE_TRIES_MAX} 次），"
                    f"稍后重试：{e}",
                )
            else:
                if item.do_id:
                    return True
                if tries >= RESOLVE_TRIES_MAX:
                    break
                self._log(
                    EventType.LOG, item,
                    f"这次没挑到可用教学班（第 {tries}/{RESOLVE_TRIES_MAX} 次），稍后重试",
                )
            if self._stop.wait(item.interval_ms / 1000.0):
                item.state = TaskState.ABORTED
                return False

        item.state = TaskState.SKIPPED
        self._log(
            EventType.GIVE_UP, item,
            "未解析到可用教学班（可能已选满，或本轮该类别没有可选班级）",
            kind=FailureKind.NOT_OPEN,
        )
        return False

    def _refresh_do_id(self, item: PlanItem) -> bool:
        """重查教学班、给**同一个班**换上新令牌（铁律 #5），成功返回 True。

        为什么按 `jxb_id` 回绑、而不是重新挑一个班：
        `do_jxb_id` 每次都变，但 `jxb_id` 是稳定的。用户押的是那个班，
        重查只是为了让令牌变新 —— **不能顺手把目标换成别的班**，那等于
        擅自改了用户的选择。

        - 原班还在 → 换上它的新令牌，返回 True
        - 原班不见了（撤班/合并）→ 明确记日志，再按常规策略重新挑一个
        - 完全解析不到 → 返回 False，由调用方决定放弃

        ⚠️ 先刷上下文再重查（P0）：请求被拒的原因常常不是「令牌本身」，
        而是**上下文整个过期**（未开放期 / 跨轮次）。不先重抓的话，
        `query_classes` 会被本地闸门挡下（零请求、永远失败），
        看起来像「重查也没用」，实际是根本没查成。
        """
        try:
            self._grab_context(reason="刷新令牌前")
        except XKError as e:
            self._log(EventType.LOG, item, f"刷新令牌前重抓上下文失败（会话失效）：{e}")
            return False

        old = item.do_id
        try:
            tab = self._tab_for(item)
            classes = self.client.query_classes(
                item.kch_id, tab=tab, cxbj=item.cxbj, fxbj=item.fxbj
            )
        except XKError as e:
            self._log(EventType.LOG, item, f"重查教学班失败，继续用旧令牌重试：{e}")
            return False

        picked, note = _pick_class(classes, want_jxb_id=item.jxb_id)
        if picked is None:
            self._log(EventType.LOG, item, f"重查教学班返回空（{note}），继续用旧令牌重试")
            return False

        # 铁律 #10：日志要能解释「为什么没抢到」—— 得让看日志的人一眼看出
        # 「这次换令牌有没有偷偷换班」。所以**成功换令牌时也必须明说**，
        # 不能只在换班时才提一句（那样「日志里没提」是两种含义，没法读）。
        same_class = not item.jxb_id or picked.jxb_id == item.jxb_id
        if not same_class:
            self._log(
                EventType.LOG,
                item,
                f"原教学班（jxb_id={item.jxb_id}）已不在列表里，重新挑一个：{note}",
            )
        item.do_id = picked.do_id
        item.jxb_id = picked.jxb_id
        item.token_at = time.monotonic()
        changed = "令牌已换新" if item.do_id != old else "令牌串未变"
        self._log(
            EventType.LOG,
            item,
            f"令牌已刷新（{changed}，"
            + ("仍是原来那个教学班" if same_class else "已改挂新教学班")
            + f"）：{picked.sksj or ''} {picked.jsxx or ''} "
            f"({picked.yxzrs}/{picked.jxbrl})",
        )
        return True

    # -- 令牌新鲜度 -----------------------------------------------------------

    def _token_suspect(self, item: PlanItem) -> bool:
        """这一项**手里的令牌**可不可疑（值得重新解析一次）。

        三种情况算可疑：
          · 还没有令牌；
          · 教务现在显示未开放（令牌是上一阶段拿的）；
          · 令牌拿到的太久了（> `CONTEXT_MAX_AGE_S`）—— 清单可能是上午建的，
            那个 do_jxb_id 到下午已经躺了好几个小时。

        ⚠️ 「可疑」**不等于**「必须先刷新再用」：开火相的正解是**先拿它打一发**
        （见 `_fire_item` 的串行说明）—— 「刷新令牌」会作废手里这个旧令牌，
        所以只能在那一发已经打出去、并且被打回来之后才做。这个方法只用来决定：
          · 预热相要解析哪些项的令牌（`_prewarm`）；
          · 上下文刚由「未开放」翻成「已开放」时，要顺手重解析哪些（`_fire_item`）。
        """
        if not item.do_id:
            return True
        if not self.client.is_open:
            return True
        if not item.token_at:
            return True
        return (time.monotonic() - item.token_at) > CONTEXT_MAX_AGE_S

    def _handle_fatal(self, e: XKError) -> None:
        """处理致命错误（会话失效 / 维护）。"""
        ev_type = EventType.NEED_LOGIN if e.kind is FailureKind.SESSION_EXPIRED else EventType.LOG
        self._emit(Event(type=ev_type, message=f"{e}", kind=e.kind, extra={"raw": e.raw or ""}))



def _pick_class(classes, *, want_jxb_id: str = "") -> tuple[object | None, str]:
    """从教学班列表里挑一个，优先认回 `want_jxb_id` 那个班。

    抽成模块级纯函数，是因为「首解析令牌」与「重查刷新令牌」必须同口径 ——
    两边各写一遍迟早会分叉（一边优先未满、一边优先原班），届时日志会自相矛盾。

    返回 `(Jxb | None, 说明)`。
    """
    if not classes:
        return None, "没有查到教学班"
    if want_jxb_id:
        same = [c for c in classes if c.jxb_id == want_jxb_id]
        if same:
            return same[0], f"认回原教学班（jxb_id={want_jxb_id}）"
    for c in classes:
        if not c.is_full:
            return c, f"挑了未满的班（{c.yxzrs}/{c.jxbrl}）"
    return classes[0], "所有教学班均已满，仍挑第一个提交一发"


def _conflict(data: dict) -> bool:
    """判断预检返回是否表示冲突（兼容不同返回形状）。"""
    if not isinstance(data, dict) or not data:
        return False
    for key in ("sfct", "hasConflict", "ct"):
        v = data.get(key)
        if v in (True, "1", 1, "true", "True"):
            return True
    flag = str(data.get("flag", ""))
    return flag not in ("", "1")


def _fmt_server(ts: float) -> str:
    """把服务器时刻格式化成「本地时区的墙上时间」。

    plan.start_at 由用户输入的裸时间经 mktime 得到，隐含假设「本地时区 == 学校时区」
    （国内场景成立）。因此这里用 localtime 显示，用户看到的和他输入的一致。
    """
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return f"{ts:.3f}"


def _fmt_delta(seconds: float) -> str:
    """把秒数说成人话：'1 分 30 秒' / '800 毫秒'。"""
    if seconds <= 0:
        return "0 秒"
    if seconds < 1:
        return f"{seconds * 1000:.0f} 毫秒"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    m, s = divmod(int(seconds), 60)
    return f"{m} 分 {s} 秒"

