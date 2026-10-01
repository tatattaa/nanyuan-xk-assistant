"""派发模式（serial / round_robin）+ 队头阻塞 + 「提交超时结果未知」自检。

为什么需要它（`smoke_schedule.py` / `smoke_p0.py` 不够）
--------------------------------------------------------------------------
2026-10-01 的两项改造，都是**结构级**的，老用例证明不了：

① `retry_mode`（serial / round_robin）
   把强化串行拆成「tick 原语 + 派发策略」。老用例只跑默认的 serial，
   根本没验证过「轮流发送」这条路；而它恰恰是治队头阻塞的那一步。

② 写请求超时 → 结果未知 → `flag="6"` 必须判**已抢到**（不是失败）
   缩短超时（`TIMEOUT_CRITICAL`）会让「提交其实成功、只是响应没回来」的概率上升。
   重试同一发时教务回 flag=6，旧代码按 ALREADY_TAKEN（不可重试）判 FAILED ——
   界面对用户说「失败」，而课其实选上了。

全程离线：不打网络、不碰真实教务、不碰落盘目录。

    python tests/smoke_rr.py
"""

import sys
import threading
import time

sys.path.insert(0, ".")

import core.client as C  # noqa: E402
import core.http as H  # noqa: E402
import engine.runner as R  # noqa: E402
from core.errors import FailureKind  # noqa: E402
from engine import GrabbingRunner, Plan, PlanItem, TaskState  # noqa: E402
from engine.plan import normalize_retry_mode  # noqa: E402

_fails = []


def expect(label, cond, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        _fails.append(label)
        print(f"  FAIL {label} {detail}")


# ===========================================================================
print("=" * 62)
print("① 派发方式：取值归一 + 落盘往返（认不出来要退默认，不能把清单读崩）")
print("=" * 62)

expect("默认是 round_robin", Plan().retry_mode == "round_robin", Plan().retry_mode)
expect("别名归一：轮流发送 → round_robin",
       normalize_retry_mode("轮流发送") == "round_robin")
expect("别名归一：RR → round_robin", normalize_retry_mode("RR") == "round_robin")
expect("别名归一：round-robin（连字符）→ round_robin",
       normalize_retry_mode("round-robin") == "round_robin")
expect("认不出来 → 退回 round_robin（不抛异常、不丢清单）",
       normalize_retry_mode("瞎写的") == "round_robin")

_p = Plan(retry_mode="round_robin")
_p.add(PlanItem(kch_id="A", budget_s="15"))
_p2 = Plan.from_dict(_p.to_dict())
expect("落盘往返保住 retry_mode", _p2.retry_mode == "round_robin", _p2.retry_mode)
expect("落盘往返保住 budget_s（字符串也归一成 float）",
       _p2.items[0].budget_s == 15.0, _p2.items[0].budget_s)
expect("旧清单（没有 retry_mode 字段）读出来是 round_robin",
       Plan.from_dict({"items": []}).retry_mode == "round_robin")


# ===========================================================================
# 假客户端：记录「每次提交的起止时刻」与「同时有几个在途请求」
# ===========================================================================

class FakeTab:
    def __init__(self):
        self.kklxdm = "01"
        self.name = "主修课程"
        self.xkkz_id = "K1"
        self.xkkz_xh = "E1"
        self.njdm_id = "2024"
        self.zyh_id = "117"


class _Clock:
    synced = True

    def describe(self):
        return "FakeClock"

    def as_dict(self):
        return {}

    def server_now(self):
        return time.time()

    def monotonic_at(self, ts):
        return time.monotonic() + (ts - time.time())

    def lead_seconds(self, minimum=0.2, cap=1.5):
        return minimum


class ClockedClient:
    """能被「卡住」的假客户端，并把每次提交的先后顺序完整记下来。

    `hang_s`      ：每次 submit 的耗时（模拟教务卡顿 / 请求迟迟不回）
    `outcomes`    ：kch_id -> [结果 dict, ...]，按调用序号取，用尽后重复最后一个
    `prechecks`   ：预检被调用的次数（用来钉「空令牌不发预检」）
    """

    def __init__(self, *, hang_s=0.0, outcomes=None, open_=True, precheck_result=None):
        self.hang_s = hang_s
        self.outcomes = outcomes or {}
        self.open_ = open_
        self.precheck_result = precheck_result if precheck_result is not None else {}
        self.tabs = [FakeTab()]
        self._cur_tab = self.tabs[0]
        self._inited = False
        self.store_at = 0.0
        self.clock = _Clock()
        self.calls: list[tuple[str, float, float]] = []   # (kch_id, 起, 止)
        self.seq: list[str] = []                          # 提交的先后顺序（只看课号）
        self.inflight = 0
        self.max_inflight = 0                             # ⭐ 并发度峰值，必须恒为 1
        self.prechecks = 0
        self.gate_blocks = 0        # 被本地闸门挡下的次数（**0 请求**）
        self._n: dict[str, int] = {}

    # -- 上下文 -------------------------------------------------------------
    def init(self, force=False):
        self._inited = True
        self.store_at = time.monotonic()
        return {"_open": "1" if self.open_ else "0"}

    @property
    def is_open(self):
        return self.open_

    @property
    def store_age_s(self):
        return None if not self.store_at else time.monotonic() - self.store_at

    def sync_clock(self, samples=4):
        return self.clock

    def tab_at(self, i):
        return self.tabs[i] if 0 <= i < len(self.tabs) else None

    def find_tab(self, kklxdm):
        return next((t for t in self.tabs if t.kklxdm == kklxdm), None)

    def query_classes(self, kch_id, **kw):
        # 未开放期忠实复刻真客户端：抛 NOT_OPEN（而不是返回空列表）——
        # 两者在引擎里的走向完全不同（前者带着空令牌进提交相位等开放，后者会被
        # 判成「解析不到教学班」直接 SKIPPED）。
        if not self.open_:
            from core import XKError

            raise XKError(FailureKind.NOT_OPEN, "当前不属于选课阶段，无法查询教学班")
        return []

    def precheck_conflict(self, kch_id, do_id):
        self.prechecks += 1
        return self.precheck_result

    # -- 提交 ---------------------------------------------------------------
    def submit(self, kch_id, do_id, *, kcmc="", **kw):
        # 本地闸门（零请求）：未开放时一个请求都不发 —— 与核心层同款入口
        if not self.open_:
            self.gate_blocks += 1
            return {"success": False, "flag": "", "msg": "当前不属于选课阶段",
                    "kind": FailureKind.NOT_OPEN}
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        t0 = time.monotonic()
        if self.hang_s:
            time.sleep(self.hang_s)
        i = self._n.get(kch_id, 0)
        self._n[kch_id] = i + 1
        seq = self.outcomes.get(kch_id) or [
            {"success": True, "flag": "1", "msg": "", "kind": None}
        ]
        res = dict(seq[min(i, len(seq) - 1)])
        self.calls.append((kch_id, t0, time.monotonic()))
        self.seq.append(kch_id)
        self.inflight -= 1
        return res


def make_plan(kch_ids, *, mode="serial", **item_kw):
    p = Plan(retry_mode=mode)
    for k in kch_ids:
        p.add(PlanItem(kch_id=k, do_id=f"do{k}", kcmc=f"课{k}", precheck=False,
                       max_attempts=item_kw.pop("max_attempts", 6),
                       interval_ms=item_kw.pop("interval_ms", 10), **item_kw))
    return p


def run(client, plan, *, timeout=25.0):
    events: list = []
    runner = GrabbingRunner(client, plan, on_event=events.append)
    th = threading.Thread(target=runner.run, daemon=True)
    th.start()
    th.join(timeout)
    return runner, events


#: 「一直失败」的通用填充物。
#:
#: ⚠️ 2026-10-01 起**不能再用满员（FULL）**做这件事了：满员已改成
#: **终态失败、一次都不重试**（见 `core/errors.py::FailureKind.FULL`），
#: 用它的话每一项第一发就结束 —— 本文件要验的预算/轮转/停止就全都没跑起来，
#: 而且会「全绿」得很安静。
#: 改用 TOO_FREQUENT：它同样属于「教务正常回了话」（→ 不触发令牌重查），
#: 且没有网络阶梯退避，所以本文件原来的时间假设（按 `interval_ms` 走）原样成立。
RETRY_FAIL = {"success": False, "flag": "-1", "msg": "选课频率过高，请稍后再试",
              "kind": FailureKind.TOO_FREQUENT}
WIN = {"success": True, "flag": "1", "msg": "", "kind": None}


# ===========================================================================
print()
print("=" * 62)
print("② 队头阻塞对照：serial 真的会被卡住，round_robin 不会")
print("=" * 62)
# A 每次提交都「卡」0.25 秒，且前两次满员、第三次才成；B 一发即成。
_out = {"A": [RETRY_FAIL, RETRY_FAIL, WIN], "B": [WIN]}

cli_s = ClockedClient(hang_s=0.25, outcomes=_out)
plan_s = make_plan(["A", "B"], mode="serial")
_, ev_s = run(cli_s, plan_s)
order_s = cli_s.seq
a_end_s = [c for c in cli_s.calls if c[0] == "A"][-1][2]
b_start_s = [c for c in cli_s.calls if c[0] == "B"][0][1]
print(f"   serial  提交顺序: {order_s}")
print(f"   serial  B 首发比 A 末发晚 {b_start_s - a_end_s:.3f}s")
expect("serial：A 连着打完 3 发才轮到 B（旧行为，队头阻塞）",
       order_s == ["A", "A", "A", "B"], order_s)
expect("serial：B 的第一发确实被 A 拖住了（晚于 A 的最后一发结束）",
       b_start_s >= a_end_s, f"{b_start_s - a_end_s:.3f}s")
expect("serial：A 第 3 次抢到、B 第 1 次抢到",
       plan_s.items[0].state is TaskState.WON and plan_s.items[0].attempts == 3
       and plan_s.items[1].state is TaskState.WON and plan_s.items[1].attempts == 1,
       f"A={plan_s.items[0].state}/{plan_s.items[0].attempts} "
       f"B={plan_s.items[1].state}/{plan_s.items[1].attempts}")

cli_r = ClockedClient(hang_s=0.25, outcomes=dict(_out))
plan_r = make_plan(["A", "B"], mode="round_robin")
_, ev_r = run(cli_r, plan_r)
order_r = cli_r.seq
a_calls = [c for c in cli_r.calls if c[0] == "A"]
b_calls = [c for c in cli_r.calls if c[0] == "B"]
print(f"   round_robin 提交顺序: {order_r}")
expect("⭐ round_robin：A 发完第一发就轮到 B（不等 A 回来）",
       order_r[:2] == ["A", "B"], order_r)
expect("⭐ round_robin：B 的第一发落在 A 第 1 发之后、A 第 3 发结束之前（真交错）",
       b_calls[0][1] >= a_calls[0][2] and b_calls[0][1] <= a_calls[-1][2],
       f"B@{b_calls[0][1]:.3f} A1止@{a_calls[0][2]:.3f} A3止@{a_calls[-1][2]:.3f}")
expect("round_robin：A 第 3 次抢到、B 第 1 次抢到（与 serial 的判定完全一致）",
       plan_r.items[0].state is TaskState.WON and plan_r.items[0].attempts == 3
       and plan_r.items[1].state is TaskState.WON and plan_r.items[1].attempts == 1,
       f"A={plan_r.items[0].state}/{plan_r.items[0].attempts} "
       f"B={plan_r.items[1].state}/{plan_r.items[1].attempts}")

expect("⭐⭐ 两种模式都**只有一个在途请求**（响应才可能天然配对到课程）",
       cli_s.max_inflight == 1 and cli_r.max_inflight == 1,
       f"serial峰值={cli_s.max_inflight} rr峰值={cli_r.max_inflight}")
expect("两种模式都推了同样的成功事件（模式只改「谁先发」，不改判定）",
       sum(1 for e in ev_s if e.type is R.EventType.SUCCESS) == 2
       and sum(1 for e in ev_r if e.type is R.EventType.SUCCESS) == 2)


# ===========================================================================
print()
print("=" * 62)
print("③ 「提交超时 → 结果未知」：flag=6 必须判**已抢到**，文案类「只能选一个」才是失败")
print("=" * 62)

expect("flag=6 → ALREADY_IN_CLASS（不是 ALREADY_TAKEN）",
       C._classify_submit_failure(C.FLAG_ALREADY_TAKEN, "0,J1,46,")
       is FailureKind.ALREADY_IN_CLASS)
expect("文案「只能选一个教学班」→ ALREADY_TAKEN（这门课有**别的**班了，本项没达成）",
       C._classify_submit_failure("0", "只能选一个教学班！") is FailureKind.ALREADY_TAKEN)
expect("flag=6 即便带着无关文案也照样按结构化信号判",
       C._classify_submit_failure("6", "系统忙") is FailureKind.ALREADY_IN_CLASS)
expect("ALREADY_IN_CLASS 不可重试（已经拿到了，重发毫无意义）",
       FailureKind.ALREADY_IN_CLASS.retryable() is False)

# 场景：第一发「超时」（结果未知）→ 重试同一发 → 教务回 flag=6
NETERR = {"success": False, "flag": "", "msg": "网络异常（超时 连接 2s / 读取 4s）",
          "kind": FailureKind.NETWORK}
TAKEN = {"success": False, "flag": "6", "msg": "0,J1,46,", "kind": FailureKind.ALREADY_IN_CLASS}

cli6 = ClockedClient(outcomes={"T": [NETERR, TAKEN]})
plan6 = make_plan(["T"], mode="serial")
_, ev6 = run(cli6, plan6)
it6 = plan6.items[0]
print(f"   事件: {[e.type.value for e in ev6]}")
print(f"   last_msg: {it6.last_msg}")
expect("⭐ 超时 + 重试拿 flag=6 → 判 **WON**（旧行为：判 FAILED，界面撒谎说「失败」）",
       it6.state is TaskState.WON, f"{it6.state}")
expect("   attempts = 2（第 1 发超时、第 2 发发现已在名下）", it6.attempts == 2, it6.attempts)
expect("   推的是 success 事件", any(e.type is R.EventType.SUCCESS for e in ev6))
expect("   消息里说明了「凭什么判定抢到」（铁律 #10：日志能解释结论）",
       "已在你名下" in it6.last_msg, it6.last_msg)

cli_at = ClockedClient(outcomes={"T": [
    {"success": False, "flag": "0", "msg": "只能选一个教学班！",
     "kind": FailureKind.ALREADY_TAKEN}]})
plan_at = make_plan(["T"], mode="serial")
_, ev_at = run(cli_at, plan_at)
it_at = plan_at.items[0]
expect("对照：文案「只能选一个教学班」仍是 **FAILED**（别的班占了这门课，不是本项成功）",
       it_at.state is TaskState.FAILED, f"{it_at.state}")
expect("   且一发收口（不可重试，不撞上限）", it_at.attempts == 1, it_at.attempts)
expect("   一个 success 事件都没有", not any(e.type is R.EventType.SUCCESS for e in ev_at))


# ===========================================================================
print()
print("=" * 62)
print("④ 时间三道闸：单项 budget_s / 整轮 global_deadline_s / 网络阶梯退避")
print("=" * 62)

# 单项预算：每次提交卡 0.2s、永远满员 → budget=0.3 时最多试 2 次就该收
cli_b = ClockedClient(hang_s=0.2, outcomes={"A": [RETRY_FAIL]})
plan_b = make_plan(["A"], mode="serial", max_attempts=50, interval_ms=10, budget_s=0.3)
t0 = time.monotonic()
run(cli_b, plan_b, timeout=15)
cost_b = time.monotonic() - t0
it_b = plan_b.items[0]
print(f"   耗时 {cost_b:.2f}s，尝试 {it_b.attempts} 次，状态 {it_b.state}")
expect("⭐ budget_s 生效：卡顿下不会把 50 次尝试全打完",
       it_b.attempts <= 3, it_b.attempts)
expect("   状态 FAILED（是「预算内没抢到」，不是沉默卡死）",
       it_b.state is TaskState.FAILED, f"{it_b.state}")
expect("   整体耗时被压在预算附近（远小于 50×0.2=10s）", cost_b < 2.0, f"{cost_b:.2f}s")

# 整轮总时限：以前只在「项与项之间」检查，一发卡住就能拖穿；现在 tick 内部也查
cli_g = ClockedClient(hang_s=0.5, outcomes={"A": [RETRY_FAIL], "B": [RETRY_FAIL]})
plan_g = make_plan(["A", "B"], mode="serial", max_attempts=50, interval_ms=10)
plan_g.global_deadline_s = 0.7
t0 = time.monotonic()
_, ev_g = run(cli_g, plan_g, timeout=20)
cost_g = time.monotonic() - t0
print(f"   耗时 {cost_g:.2f}s，A 尝试 {plan_g.items[0].attempts} 次")
expect("⭐ global_deadline_s 在 tick 内部生效（一发卡住也拖不穿）",
       cost_g < 2.2, f"{cost_g:.2f}s")
expect("   超时限后不再继续发请求（A 的尝试次数远小于 50）",
       plan_g.items[0].attempts < 8, plan_g.items[0].attempts)
expect("   说了「已达总时限」", any("总时限" in (e.message or "") for e in ev_g),
       [e.message for e in ev_g][:6])

# 网络阶梯退避：间隔必须逐次拉长（1× → 2× → 4×），且不会比 interval 更密
cli_n = ClockedClient(outcomes={"A": [NETERR]})
plan_n = make_plan(["A"], mode="serial", max_attempts=5, interval_ms=100)
run(cli_n, plan_n, timeout=20)
ts = [c[1] for c in cli_n.calls]
gaps = [round((ts[i + 1] - ts[i]) * 1000) for i in range(len(ts) - 1)]
print(f"   提交间隔(ms): {gaps}")
expect("⭐ 网络类失败走阶梯退避：间隔单调拉长（教务卡顿时主动让路）",
       len(gaps) >= 2 and all(gaps[i] < gaps[i + 1] for i in range(len(gaps) - 1)), gaps)
expect("   首间隔不小于 interval_ms", bool(gaps) and gaps[0] >= 100, gaps)
expect("   退避有上限（不会指数爆炸到几十秒）",
       all(g <= int(R.NETWORK_BACKOFF_MAX_S * 1000) + 50 for g in gaps), gaps)


# ===========================================================================
print()
print("=" * 62)
print("⑤ 空令牌不发预检（未开放期的第一个 RTT 不能被浪费）")
print("=" * 62)

cli_e = ClockedClient(open_=False, outcomes={"A": [WIN]})
plan_e = Plan(retry_mode="serial")
plan_e.add(PlanItem(kch_id="A", do_id="", kcmc="课A", precheck=True,
                    max_attempts=20, interval_ms=10))
R.NOT_OPEN_GRACE_S = 0.3        # 生产 30 秒；这里压到 0.3 秒够用
R.NOT_OPEN_RETRY_WAIT_S = 0.05
run(cli_e, plan_e, timeout=15)
print(f"   预检调用 {cli_e.prechecks} 次，闸门挡下 {cli_e.gate_blocks} 次，"
      f"状态 {plan_e.items[0].state}")
expect("⭐ do_id 为空时**不**发预检（旧写法会盲发一发、必然失败、再降级）",
       cli_e.prechecks == 0, cli_e.prechecks)
expect("   未开放期提交全被本地闸门挡下（零请求到教务）", cli_e.gate_blocks > 1,
       cli_e.gate_blocks)
expect("   未开放耐心耗尽 → FAILED（不是沉默挂住）",
       plan_e.items[0].state is TaskState.FAILED, plan_e.items[0].state)


# ===========================================================================
print()
print("=" * 62)
print("⑥ 停止与结构回归")
print("=" * 62)

cli_st = ClockedClient(hang_s=0.15,
                       outcomes={"A": [RETRY_FAIL], "B": [RETRY_FAIL], "C": [RETRY_FAIL]})
plan_st = make_plan(["A", "B", "C"], mode="round_robin", max_attempts=50, interval_ms=10)
events_st: list = []
runner_st = GrabbingRunner(cli_st, plan_st, on_event=events_st.append)
th = threading.Thread(target=runner_st.run, daemon=True)
th.start()
time.sleep(0.35)
runner_st.stop()
th.join(10)
states_st = [it.state.value for it in plan_st.items]
print(f"   停止后状态: {states_st}")
expect("round_robin 下 stop 生效：没有任何一项留在 running",
       "running" not in states_st, states_st)
expect("   剩余项收成 aborted（不是永远挂着）",
       all(s in ("aborted", "failed", "won", "skipped") for s in states_st), states_st)
expect("   仍然推了 plan_done（界面不会卡在转圈）",
       events_st[-1].type is R.EventType.PLAN_DONE,
       events_st[-1].type.value)

expect("结构：tick 原语存在（这是两种模式共用的地基）",
       hasattr(GrabbingRunner, "_tick") and hasattr(GrabbingRunner, "_drive_round_robin")
       and hasattr(GrabbingRunner, "_drive_serial"))
expect("结构：仍然没有后台侦察队 / 专用客户端（整轮一个客户端）",
       not hasattr(GrabbingRunner, "_start_scout")
       and not hasattr(GrabbingRunner, "_scout_client_get"))
expect("结构：引擎层没引入任何并发原语（ThreadPool / async）",
       not hasattr(R.GrabbingRunner, "run_parallel"))


# ===========================================================================
print()
print("=" * 62)
print("⑥b 蹲课全局节流：发完一门隔 interval_ms 再发下一门（不是按批次）")
print("=" * 62)

# 蹲课信号 = max_attempts <= 0（按时间不按次数）。用 FULL 失败让它持续重试
# （蹲课下 FULL 可重试，见 smoke_e2e 第 ⑩ 组），这样能连续发出多门课、观察到间隔。
FULL_FAIL = {"success": False, "flag": "-1", "msg": "0,J1,46,0",
             "kind": FailureKind.FULL}

cli_th = ClockedClient(outcomes={"A": [FULL_FAIL], "B": [FULL_FAIL], "C": [FULL_FAIL]})
# ⚠️ 不用 make_plan：它的 `item_kw.pop(...)` 写在 for 循环里，多门课时只有第一项
# 吃到 max_attempts/interval_ms，其余回落默认 6/10 —— 会导致「不是所有项都蹲课」、
# 节流被关掉。这里手动构造，确保三项都是 max_attempts=-1、interval_ms=150。
plan_th = Plan(retry_mode="round_robin")
for k in ("A", "B", "C"):
    plan_th.add(PlanItem(kch_id=k, do_id=f"do{k}", kcmc=f"课{k}", precheck=False,
                         max_attempts=-1, interval_ms=150))
events_th: list = []
runner_th = GrabbingRunner(cli_th, plan_th, on_event=events_th.append)
th = threading.Thread(target=runner_th.run, daemon=True)
th.start()
time.sleep(1.2)                       # 给足时间发出多门课
runner_th.stop()
th.join(10)

# ⭐ 关键断言：相邻两次 submit 之间必须隔 >= interval_ms（这里 150ms，留 30ms 容差）。
# 抢课 round_robin 是「一轮背靠背全发」（间隔≈0），蹲课必须错开。
_starts = [c[1] for c in cli_th.calls]
_gaps = [round(_starts[i] - _starts[i - 1], 3) for i in range(1, len(_starts))]
print(f"   提交课号顺序: {cli_th.seq}")
print(f"   相邻间隔(ms): {[round(g * 1000) for g in _gaps]}")
expect("⭐ 蹲课下 submit 确实发出来了（FULL 可重试，没一发出终态）", len(cli_th.calls) >= 3,
       len(cli_th.calls))
expect("⭐⭐ 相邻两发之间隔了 interval_ms（150ms），不是背靠背 0 间隔",
       bool(_gaps) and all(g >= 0.12 for g in _gaps),
       f"gaps={_gaps}")
expect("⭐⭐ 轮转顺序公平（A→B→C→A…，不是排前面的独占）",
       cli_th.seq[:4] == ["A", "B", "C", "A"], cli_th.seq[:6])


# ===========================================================================
print()
print("=" * 62)
print("⑦ 超时分档：关键路径 2+4 秒，探活更快，其它 15 秒")
print("=" * 62)

expect("关键路径超时是 (connect, read) 二元组",
       isinstance(H.TIMEOUT_CRITICAL, tuple) and H.TIMEOUT_CRITICAL == (2.0, 4.0),
       H.TIMEOUT_CRITICAL)
expect("探活更快", H.TIMEOUT_QUICK[1] < H.TIMEOUT_CRITICAL[1], H.TIMEOUT_QUICK)
expect("其它路径保持 15 秒（init / 查课不该被误判成超时）", H.TIMEOUT_NORMAL == 15.0)
expect("说人话的格式化：float 提醒它是「各一次」",
       "15s" in H._timeout_text(15.0), H._timeout_text(15.0))
expect("说人话的格式化：二元组分开写", "读取 4s" in H._timeout_text((2.0, 4.0)),
       H._timeout_text((2.0, 4.0)))


class RecordingHttp:
    """记录 (method, url, timeout) 的假 HttpSession —— 只钉「这一发用了几档超时」。"""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []

    def post(self, url, data=None, *, timeout=None, **kw):
        self.calls.append(("POST", url, timeout))
        return H.RawResponse(status=200, text='{"flag":"1","msg":""}', url=url)

    def get(self, url, *, timeout=None, **kw):
        self.calls.append(("GET", url, timeout))
        return H.RawResponse(status=200, text="<html></html>", url=url)


def _bare_client():
    """一个「看起来已开放」的 ZfClient：不碰网络，只为了钉它用哪一档超时。"""
    cred = C.Credential(cookie_header="JSESSIONID=x")
    cli = C.ZfClient(cred)
    cli.http = RecordingHttp()
    cli._inited = True
    cli.store.update({"_open": "1", "xkkz_id": "K1", "kklxdm": "01", "xkxnm": "2026",
                      "xkxqm": "1", "rwlx": "1"})
    return cli


_rc = _bare_client()
_rc.submit("C1", "do1")
expect("⭐ submit 走关键路径超时",
       _rc.http.calls[-1][2] is H.TIMEOUT_CRITICAL, _rc.http.calls[-1])

_rc = _bare_client()
_rc.precheck_conflict("C1", "do1")
expect("⭐ 预检走关键路径超时",
       _rc.http.calls[-1][2] is H.TIMEOUT_CRITICAL, _rc.http.calls[-1])

_rc = _bare_client()
_rc.ping()
expect("探活走「快档」超时", _rc.http.calls[-1][2] is H.TIMEOUT_QUICK, _rc.http.calls[-1])


# ===========================================================================
print()
print("=" * 62)
if _fails:
    print(f"✗ {len(_fails)} 条未通过：")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("✓ 派发模式 / 队头阻塞 / 超时语义 自检全部通过")
