"""定时开抢（预热相 / 倒计时 / 开火相）的回归测试。

不依赖真实教务，也不依赖真实墙钟精度：
    - FakeClock 把「服务器时刻」锁成一个可预测的偏移量
    - 每次事件同时记单调时刻与墙钟时刻，判定「相序」与「开火精度」

重点验三件事（也是本次重构的设计主张）：
    1. 开火前 do_id 必须就绪 —— 预热真的把「切 Tab / 查课 / 查班」全提前做完了
    2. 开火时刻命中目标（±50ms），且正确补偿了本地时钟偏差
       （把 start_at 从【本地挂钟】推出，若代码忽略 offset，就会晚 OFFSET 秒开火 → 断言必挂）
    3. **预检发生在开火相**，而不是预热相 —— 抢到第一门后已选就变了，
       早预检会把「自己刚选上的课」造成的冲突漏判
"""

import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from core.client import Jxb
from core.errors import FailureKind
from engine.plan import Plan, PlanItem, parse_when
from engine.runner import GrabbingRunner

_fails = []


def expect(label, cond, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        _fails.append(label)
        print(f"  FAIL {label}  {detail}")


# ---------------------------------------------------------------------------
# 确定性假时钟：server_now = 本地墙钟 + offset
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, offset=0.0, uncertainty=0.25):
        self._offset = offset
        self._unc = uncertainty
        self.synced = True

    @property
    def offset(self):
        return self._offset

    @property
    def uncertainty(self):
        return self._unc

    @property
    def rtt(self):
        return 0.25

    def server_now(self):
        return time.time() + self._offset

    def monotonic_at(self, ts):
        return time.monotonic() + (ts - self.server_now())

    def lead_seconds(self, minimum=0.2, cap=1.5):
        return max(minimum, min(cap, round(self._unc * 10) / 10 + 0.05))

    def describe(self):
        return f"FakeClock offset={self._offset:+.2f}s"

    def as_dict(self):
        return {"synced": True, "offset_ms": self._offset * 1000}


class RecordingClient:
    """记录每次调用及其单调时刻的假客户端。calls 的顺序即真实相序。"""

    def __init__(self, clock, resolvable=True, win_from_attempt=1):
        self.clock = clock
        self.calls = []            # (name, mono, detail)
        self.tabs = []
        self._resolvable = resolvable
        self._win_from = win_from_attempt
        self._submits = 0

    def init(self, force=False):
        self.calls.append(("init", time.monotonic(), ""))
        return {"_open": "1"}

    @property
    def is_open(self):
        return True

    def find_tab(self, kklxdm):
        return None

    def query_classes(self, kch_id, *, tab=None, kklxdm=None, cxbj="0", fxbj="0"):
        self.calls.append(("query_classes", time.monotonic(), kch_id))
        if not self._resolvable:
            return []
        return [
            Jxb(jxb_id="j1", do_id=f"do-{kch_id}", kcmc=f"课{kch_id}", jsxx="张老师",
                sksj="星期三第3-4节", jxbrl="60", yxzrs="10")
        ]

    def precheck_conflict(self, kch_id, do_id):
        self.calls.append(("precheck", time.monotonic(), kch_id))
        return {}

    def submit(self, kch_id, do_id, **kw):
        self.calls.append(("submit", time.monotonic(), kch_id))
        self._submits += 1
        if self._submits >= self._win_from:
            return {"success": True, "flag": "1", "msg": "", "kind": None}
        # ⚠️ 2026-10-01：这里原本返回 FULL（满员）。满员已改成**终态失败、不重试**，
        # 拿它当「先失败几次再成功」的填充物会让场景 E 第一发就结束。
        # 改用 TOO_FREQUENT：仍可重试、不触发令牌重查、也没有网络阶梯退避，
        # 所以本文件原有的时间假设（按 interval_ms 走）不变。
        return {"success": False, "flag": "-1", "msg": "选课频率过高，请稍后再试",
                "kind": FailureKind.TOO_FREQUENT}

    def sync_clock(self, samples=4):
        return self.clock

    def close(self):
        pass


def run_plan(client, plan, timeout=15.0):
    """同步跑完一个计划，返回事件列表（含单调时刻与墙钟时刻）。"""
    events = []

    def sink(ev):
        events.append({
            "type": ev.type.value,
            "mono": time.monotonic(),
            "wall": time.time(),
            "msg": ev.message,
            "kind": ev.kind.value if ev.kind else None,
        })

    runner = GrabbingRunner(client, plan, on_event=sink)
    th = threading.Thread(target=runner.run, daemon=True)
    th.start()
    th.join(timeout)
    return events


def net_order(labels, *names):
    return [(n, d) for n, _, d in labels.calls if n in names]


# ===========================================================================
print("场景 A：定时开抢正常路径（2 项，本地钟比服务器慢 500ms）")
# ===========================================================================
OFFSET = 0.5                      # 服务器比本地快 0.5s
ck = FakeClock(offset=OFFSET, uncertainty=0.25)
LEAD = ck.lead_seconds()          # 从时钟对象取，别硬编码（曾因硬编码 0.30 误判过）
cli = RecordingClient(ck)
print(f"   偏差 {OFFSET:+.2f}s，不确定度 ±{ck.uncertainty * 1000:.0f}ms → 提前开火 {LEAD * 1000:.0f}ms")

# 目标从【本地挂钟】推出：本地再过 1.6s 就是学校的开抢时刻。
# 若 runner 忽略 offset，会在本地 2.1s 处才开火 → 精度断言直接挂。
t_setup = time.time()
START_AT = t_setup + 1.6 + OFFSET
# 场景 A 验证的是「串行」派发下的相序（A 提交成功后才预检 B），
# 必须显式指定 serial —— Plan 默认已改为 round_robin，否则相序会变。
plan = Plan(start_at=START_AT, warmup_s=10.0, global_deadline_s=10.0, retry_mode="serial")
plan.add(PlanItem(kch_id="A", kcmc="甲课", priority=0))
plan.add(PlanItem(kch_id="B", kcmc="乙课", priority=1))

events = run_plan(cli, plan)
types = [e["type"] for e in events]
print("   事件序列:", types)

expect("首尾事件正确", types[0] == "plan_start" and types[-1] == "plan_done")
expect("有 clock 校准事件", "clock" in types)
expect("有 prewarm_start", "prewarm_start" in types)
expect("有 prewarm_ready（预热成功就绪）", "prewarm_ready" in types)
expect("有 countdown", "countdown" in types)
expect("有 fire 开火事件", "fire" in types)

fire = next(e for e in events if e["type"] == "fire")
fire_mono = fire["mono"]

# --- 相序 1：教学班查询都在开火前完成 ---
queries = [m for n, m, _ in cli.calls if n == "query_classes"]
expect("预热期就查完全部教学班", len(queries) == 2, f"实际 {len(queries)} 次")
expect("查询全部早于开火", all(m < fire_mono for m in queries))

# --- 相序 2（关键设计决策）：预检必须在开火之后 ---
pre_checks = [m for n, m, _ in cli.calls if n == "precheck"]
expect("预检全部晚于开火（避免预检结果过期）",
       bool(pre_checks) and all(m > fire_mono for m in pre_checks),
       f"precheck {len(pre_checks)} 次，最早 {min(pre_checks) - fire_mono:+.3f}s 相对开火")

# --- 相序 3：A 提交成功后才预检 B，否则「自己刚选上的课」造成的冲突会被漏判 ---
order = net_order(cli, "precheck", "submit")
print("   网络调用相序:", order)
expect("A预检→A提交→B预检→B提交",
       order == [("precheck", "A"), ("submit", "A"), ("precheck", "B"), ("submit", "B")],
       f"实际 {order}")

# --- 开火精度：目标 = 本地 t_setup + 1.6 - LEAD，且必须补偿掉 OFFSET ---
expected_wall = t_setup + 1.6 - LEAD
err = fire["wall"] - expected_wall
print(f"   目标开火墙钟 {expected_wall:.3f}，实际 {fire['wall']:.3f}，误差 {err * 1000:+.0f}ms")
expect("开火时刻命中目标 ±50ms", abs(err) < 0.05, f"误差 {err * 1000:+.0f}ms")
expect("确实补偿了本地时钟偏差（未补偿会晚 500ms）", err < 0.2, f"误差 {err * 1000:+.0f}ms")

expect("两项都抢到", len(plan.won) == 2, f"实际 {len(plan.won)}")
expect("每项只提交一次（一次往返定胜负）",
       sum(1 for n, _, _ in cli.calls if n == "submit") == 2)
expect("do_id 由预热阶段补齐", plan.items[0].do_id == "do-A" and plan.items[1].do_id == "do-B")

# ===========================================================================
print()
print("场景 B：预热取不到教学班（开放前），仍要准点开火并说清原因")
# ===========================================================================
ck2 = FakeClock(offset=0.0)
cli2 = RecordingClient(ck2, resolvable=False)
plan2 = Plan(start_at=ck2.server_now() + 1.2, warmup_s=10.0, global_deadline_s=10.0)
plan2.add(PlanItem(kch_id="X", kcmc="未开放课"))

events2 = run_plan(cli2, plan2)
types2 = [e["type"] for e in events2]
msgs2 = [e["msg"] for e in events2]
print("   事件序列:", types2)

expect("没有 prewarm_ready（预热失败）", "prewarm_ready" not in types2)
expect("仍然准点开火", "fire" in types2)
expect("日志点明「未取到教学班」", any("未取到教学班" in m for m in msgs2))
expect("该项标记 SKIPPED", plan2.items[0].state.value == "skipped",
       f"实际 {plan2.items[0].state.value}")
expect("未取到班时一次都不提交（绝不盲发空 do_id）",
       not any(n == "submit" for n, _, _ in cli2.calls))

# ===========================================================================
print()
print("场景 C：倒计时期间被用户中止")
# ===========================================================================
ck3 = FakeClock(offset=0.0)
cli3 = RecordingClient(ck3)
plan3 = Plan(start_at=ck3.server_now() + 3.0, warmup_s=10.0, global_deadline_s=10.0)
plan3.add(PlanItem(kch_id="Y", kcmc="中止课"))

events3 = []


def sink3(ev):
    events3.append(ev.type.value)


runner3 = GrabbingRunner(cli3, plan3, on_event=sink3)
th3 = threading.Thread(target=runner3.run, daemon=True)
th3.start()
time.sleep(0.4)
t_stop = time.monotonic()
runner3.stop()
th3.join(10)
stop_cost = time.monotonic() - t_stop

expect("中止后没有开火", "fire" not in events3)
expect("中止后一次都没提交", not any(n == "submit" for n, _, _ in cli3.calls))
expect("项状态为 aborted", plan3.items[0].state.value == "aborted",
       f"实际 {plan3.items[0].state.value}")
expect("仍然推了 plan_done（前端不会卡在转圈）", events3[-1] == "plan_done",
       f"实际 {events3[-1]}")
expect("中止立即生效（不用等 T0）", stop_cost < 1.5, f"耗时 {stop_cost:.2f}s")
expect("中止发生在倒计时阶段", "countdown" in events3)

# ===========================================================================
print()
print("场景 D：立即模式（start_at=None）不受两相重构影响")
# ===========================================================================
ck4 = FakeClock(offset=0.0)
cli4 = RecordingClient(ck4)
plan4 = Plan()
plan4.add(PlanItem(kch_id="Z", kcmc="即刻课"))

events4 = run_plan(cli4, plan4)
types4 = [e["type"] for e in events4]
print("   事件序列:", types4)
expect("无预热/倒计时/开火事件",
       not {"prewarm_start", "countdown", "fire"} & set(types4),
       f"实际 {types4}")
expect("直接提交成功", "success" in types4 and len(plan4.won) == 1)

# ===========================================================================
print()
print("场景 E：开火后提交失败仍要重试（可重试失败 → 第 3 次成功）")
# ===========================================================================
ck5 = FakeClock(offset=0.0)
cli5 = RecordingClient(ck5, win_from_attempt=3)
plan5 = Plan(start_at=ck5.server_now() + 0.6, warmup_s=10.0, global_deadline_s=10.0)
item5 = PlanItem(kch_id="W", kcmc="重试课", max_attempts=5, interval_ms=80)
plan5.add(item5)

events5 = run_plan(cli5, plan5)
types5 = [e["type"] for e in events5]
print("   事件序列:", types5)
expect("开火后持续重试", types5.count("attempt") >= 3, f"实际 {types5.count('attempt')} 次")
expect("出现 retry_wait（可重试失败语义）", "retry_wait" in types5)
expect("最终抢到且次数正确", item5.state.value == "won" and item5.attempts == 3,
       f"{item5.state.value}/{item5.attempts}")

# ===========================================================================
print()
print("场景 F：开抢时刻解析（parse_when）")
# ===========================================================================
NOW = 1790660000.0          # 固定基准时刻，测试才可复现
base = datetime.fromtimestamp(NOW)

expect("相对秒 +90", abs(parse_when("+90", now=NOW) - (NOW + 90)) < 1e-6)
expect("相对分:秒 +1:30", abs(parse_when("+1:30", now=NOW) - (NOW + 90)) < 1e-6)
expect("相对时:分:秒 +1:30:00", abs(parse_when("+1:30:00", now=NOW) - (NOW + 5400)) < 1e-6)

# 今天的某个未来时刻
future = base + timedelta(hours=2)
ts = parse_when(future.strftime("%H:%M:%S"), now=NOW)
expect("HH:MM:SS 落到今天", abs(ts - future.replace(microsecond=0).timestamp()) < 1e-6)
# HH:MM 不带秒 → 解析结果的秒必须归零（不是沿用基准时刻的秒）
expect("HH:MM 落到今天且秒归零",
       abs(parse_when(future.strftime("%H:%M"), now=NOW)
           - future.replace(second=0, microsecond=0).timestamp()) < 1e-6)

# 完整日期（两种分隔符）
expect("YYYY-MM-DD HH:MM:SS",
       abs(parse_when("2026-12-31 08:00:00", now=NOW) - datetime(2026, 12, 31, 8, 0, 0).timestamp()) < 1e-6)
expect("YYYY/MM/DD HH:MM",
       abs(parse_when("2026/12/31 08:00", now=NOW) - datetime(2026, 12, 31, 8, 0, 0).timestamp()) < 1e-6)

# 过去的时刻必须报错，绝不偷偷顺延到明天（抢课差一天是灾难）
for bad, desc in [("", "空串"), ("昨天", "乱写"), ("25:00", "非法小时")]:
    try:
        parse_when(bad, now=NOW)
        expect(f"非法输入应报错：{desc}", False, "居然解析成功了")
    except ValueError:
        expect(f"非法输入报错：{desc}", True)

past = (base - timedelta(hours=3)).strftime("%H:%M:%S")
try:
    parse_when(past, now=NOW)
    expect("过去的时刻应报错", False, "居然解析成功了")
except ValueError as e:
    expect("过去的时刻报错且提示写日期", "完整日期" in str(e), str(e)[:70])

# 刚过去不到 1 分钟算「立即」，容忍用户手速慢
fresh = (base - timedelta(seconds=5)).strftime("%H:%M:%S")
expect("刚过去 <60s 仍可解析（视作立即）", parse_when(fresh, now=NOW) < NOW)

print()
if _fails:
    print(f"=== 定时开抢自检失败 {len(_fails)} 项：{_fails} ===")
    raise SystemExit(1)
print("=== 定时开抢（预热/倒计时/开火）自检全部通过 ===")
