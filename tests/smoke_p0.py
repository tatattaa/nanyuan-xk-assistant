"""P0 回归：陈旧上下文的修复 + 开火相「严格串行」抢课结构（2026-09-30）。

背景
--------------------------------------------------------------------------
教务上午是「查看可选课程」期，中间夹一段**未开放期**，下午才真正开放抢课。
而 `client.init()` 带快照缓存 —— `if self._inited and not force: return self.store`，
第二次起**一个请求都不发**，直接把上午那份 xkkz_id / do_jxb_id 原样返回。

于是「上午建会话 → 下午一键抢课」= 100% 全灭（离线实证：新增请求 0 / submit 0 /
全部 aborted）。用户观测到的现象完全同构：**不刷新浏览器，抢课按钮不会自动变回
抢课，点击依然无效**。

修法
--------------------------------------------------------------------------
1) 计划一开始就 `init(force=True)` —— 真抓一次，不接受快照；
2) 预热相**每一轮**先查上下文岁龄，超龄/未开放就重抓 —— 教务一恢复开放，
   下一轮立刻拿到真上下文，而不是等 T0 才第一次问服务器；
3) 开火相**严格串行**（2026-09-30 用户指出的竞态）：
     ① 拿着手里的令牌先打一发（此刻没人和它抢令牌）；
     └─ 失败（令牌类）→ ② 同步重查教学班、换新令牌 → 用新令牌重发；
    ⚠️ **绝不并行刷新**：`do_jxb_id` 是一次性令牌，「刷新」会**作废手里那个旧令牌**，
       只要刷新请求早于提交到达服务器，那一发提交就是被自己人废掉的。
       所以不支后台队、不 fork 客户端、整轮只有一条线程在发写请求。
4) 提交若回 NOT_OPEN，按**时间**给一段耐心（`NOT_OPEN_GRACE_S`），而且
   **判死之前必须先刷最后一次上下文** —— 教务恰好在那一刻开放就不会被丢掉。
   ⭐ 「未开放」的唯一正解是**重读上下文**，不是硬打一发赌它受理（2026-09-30 用户否决盲发）。

    python tests/smoke_p0.py
"""

import sys
import time

sys.path.insert(0, ".")

import engine.runner as R  # noqa: E402
from core import XKError  # noqa: E402
from core.errors import FailureKind  # noqa: E402
from engine import GrabbingRunner, Plan, PlanItem, TaskState  # noqa: E402

_fails = []


def expect(label, cond, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        _fails.append(label)
        print(f"  FAIL {label} {detail}")


# 把等待时间压到测试能接受：语义不变，只是别让用例跑十几秒
R.NOT_OPEN_RETRY_WAIT_S = 0.2
R.NOT_OPEN_GRACE_S = 1.0        # 生产 30 秒；这里压到 1 秒，够跑完「卡在开放瞬间」


# ---------------------------------------------------------------------------
# 假教务：一个可以被「开合」的服务器 + 忠实复刻快照缓存的客户端
# ---------------------------------------------------------------------------

class FakeServer:
    """假教务。`accept` 是它当前愿意受理的令牌集合。"""

    def __init__(self, *, closed: bool = True):
        self.closed = closed
        self.opens_at: float | None = None   # 单调钟；到点自动开放
        self.net: list[tuple] = []           # 服务器**真的收到**的请求
        self.accept: set[str] = set()
        self.issued = 0

    def is_open(self) -> bool:
        if self.opens_at is not None and time.monotonic() >= self.opens_at:
            return True
        return not self.closed

    def issue(self) -> str:
        """下发一个新令牌；旧的一次性令牌随即作废（模拟「每次查询重新下发」）。

        ⚠️ 这正是「并行刷新」致命的那个副作用：`accept` 被改成只有新令牌有效，
        手里那个旧令牌**当场失效**。
        """
        self.issued += 1
        tok = f"TOK{self.issued}"
        self.accept = {t for t in self.accept if t.startswith("MORNING")}
        self.accept.add(tok)
        return tok

    def submits(self) -> list[tuple]:
        return [c for c in self.net if c[0] == "submit"]

    def order(self) -> list[str]:
        """只看「提交」与「查教学班」的先后 —— 串行性就靠它钉。"""
        return [c[0] for c in self.net if c[0] in ("submit", "query_classes")]


class FakeTab:
    def __init__(self, kklxdm="01", name="主修课程"):
        self.kklxdm = kklxdm
        self.name = name
        self.xkkz_id = "K1"
        self.xkkz_xh = "E1"
        self.njdm_id = "2024"
        self.zyh_id = "117"


class FakeJxb:
    def __init__(self, jxb_id, do_id):
        self.jxb_id = jxb_id
        self.do_id = do_id
        self.kcmc = ""
        self.jsxx = "张老师"
        self.sksj = "星期三第3-4节"
        self.jxbrl = "60"
        self.yxzrs = "10"
        self.is_full = False


class FakeClient:
    """忠实复刻两个关键行为：① `init` 的快照缓存；② `submit` 的本地闸门。

    ① 是 bug 的根源：不带 force 的 `init()` 直接返回快照，**不碰网络**。
    ② 是「未开放就别打」的唯一入口 —— 注意**没有绕过开关**（`blind` 那个口子
       2026-09-30 被用户否决并删除，`tests/smoke_p0.py` 里也钉着这件事）。

    ⚠️ 刻意**不提供 `fork()`** —— 侦察队已整体移除（见下方「⑨ 结构回归」）。
    """

    def __init__(self, server: FakeServer, *, label="main"):
        self.server = server
        self.label = label
        self.tabs = [FakeTab()]
        self._inited = False
        self._open_flag = False     # 「快照里的 »选课已开放«」——可能已经陈旧
        self.store_at = 0.0
        self.init_calls: list[bool] = []   # 每次 init 的 force 取值
        self.real_fetches = 0
        self.queries = 0
        self.submits: list[str] = []
        self.gate_blocks = 0        # 被本地闸门挡下（**0 请求**）

    # -- 生命周期 ---------------------------------------------------------
    def close(self):
        pass

    def sync_clock(self, samples=4):
        return self.clock

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

    clock = _Clock()

    # -- 上下文 -----------------------------------------------------------
    def init(self, force=False):
        self.init_calls.append(force)
        if self._inited and not force:
            return {"_open": "1" if self._open_flag else "0"}   # ← 零请求的快照
        self.real_fetches += 1
        self._inited = True
        self._open_flag = self.server.is_open()
        self.store_at = time.monotonic()
        self.server.net.append(("init", self.label))
        return {"_open": "1" if self._open_flag else "0", "xkxnmc": "2026-2027"}

    @property
    def is_open(self):
        return self._open_flag        # 快照说了算 —— 陈旧的根源就在这里

    @property
    def store_age_s(self):
        return None if not self.store_at else time.monotonic() - self.store_at

    def tab_at(self, i):
        return self.tabs[i] if 0 <= i < len(self.tabs) else None

    def find_tab(self, kklxdm):
        return next((t for t in self.tabs if t.kklxdm == kklxdm), None)

    # -- 查询 -------------------------------------------------------------
    def query_classes(self, kch_id, *, tab=None, tab_index=None, kklxdm=None,
                      cxbj="0", fxbj="0"):
        if not self._open_flag:
            raise XKError(FailureKind.NOT_OPEN, "当前不属于选课阶段，无法查询教学班")
        self.queries += 1
        self.server.net.append(("query_classes", self.label, kch_id))
        return [FakeJxb("J1", self.server.issue())]

    def precheck_conflict(self, kch_id, do_id):
        return {}

    # -- 提交 -------------------------------------------------------------
    def submit(self, kch_id, do_id, *, kcmc="", **kw):
        if not self._open_flag:
            self.gate_blocks += 1
            return {"success": False, "flag": "", "msg": "当前不属于选课阶段",
                    "kind": FailureKind.NOT_OPEN}
        self.submits.append(do_id)
        self.server.net.append(("submit", self.label, do_id))
        if do_id and do_id in self.server.accept:
            return {"success": True, "flag": "1", "msg": "选课成功", "kind": None}
        if not self.server.is_open():
            return {"success": False, "flag": "", "msg": "当前不属于选课阶段",
                    "kind": FailureKind.NOT_OPEN}
        return {"success": False, "flag": "", "msg": "加密串错误",
                "kind": FailureKind.CONTEXT_INVALID}


def run(client, plan, *, max_attempts=50, interval_ms=50):
    for it in plan.items:
        it.max_attempts = max_attempts
        it.interval_ms = interval_ms
    events: list = []
    r = GrabbingRunner(client, plan, on_event=events.append)
    r.run()
    return r, events


def make_plan(*, do_id="MORNING_TOKEN", token_at=0.0, precheck=False):
    p = Plan()
    p.add(PlanItem(kch_id="K1", kcmc="篮球", xf="1.0", do_id=do_id,
                   jxb_id="J1", tab_index=0, kklxdm="01", precheck=precheck,
                   token_at=token_at))
    return p


print("=" * 62)
print("① 单元：本地闸门（未开放 → 零请求），且**没有绕过开关**")
print("=" * 62)

import inspect  # noqa: E402

from core.client import ZfClient  # noqa: E402

_sig = inspect.signature(ZfClient.submit)
expect("⭐ submit 不再有 blind 这类「绕过闸门」的参数"
       "（2026-09-30 用户否决：未开放就不该发）",
       not any(k in _sig.parameters for k in ("blind", "force", "ignore_gate")),
       f"实际参数：{list(_sig.parameters)}")

srv = FakeServer(closed=True)
cli = FakeClient(srv)
cli.init(force=True)                      # 快照里是「未开放」
n0 = len(srv.net)
r = cli.submit("K1", "MORNING_TOKEN")
expect("未开放 → NOT_OPEN", r["kind"] is FailureKind.NOT_OPEN)
expect("⭐ 未开放 → **一个请求都没发**（不拿无效提交去赌）",
       len(srv.net) == n0, f"net 多了 {len(srv.net) - n0} 条")
expect("未开放 → 记为被本地闸门挡下", cli.gate_blocks == 1)
expect("未开放 → 连 submits 计数都不该动", cli.submits == [])

# 真的开着的时候当然要发 —— 闸门只挡「未开放」，不是挡提交
srv = FakeServer(closed=False)
srv.accept = {"MORNING_TOKEN"}
cli = FakeClient(srv)
cli.init(force=True)
r = cli.submit("K1", "MORNING_TOKEN")
expect("已开放 → 请求真的发出去了", len(srv.submits()) == 1, f"实际 {srv.net}")
expect("已开放 → 提交成功", r.get("success") is True, f"{r}")

print()
print("=" * 62)
print("② 单元：可疑判据（上下文岁龄 / 手里的令牌）")
print("=" * 62)

srv = FakeServer(closed=False)
cli = FakeClient(srv)
cli.init(force=True)
rn = GrabbingRunner(cli, Plan())

expect("上下文刚抓过（岁龄≈0）→ 不可疑", rn._client_suspect(cli) is False)
cli.store_at = time.monotonic() - 7200
expect("上下文是 2 小时前抓的 → **可疑**（上午建会话、下午抢课）",
       rn._client_suspect(cli) is True)

srv.closed = True
cli._open_flag = False
expect("教务显示未开放 → 可疑（哪怕刚抓过）", rn._client_suspect(cli) is True)


class Bare:
    """没有 store_age_s / is_open 的极简替身（模拟旧测试里的假客户端）。"""

    pass


expect("缺这两个属性的客户端 → 判为不可疑（不强行重抓）",
       GrabbingRunner._client_suspect(Bare()) is False)

srv = FakeServer(closed=False)
cli = FakeClient(srv)
cli.init(force=True)
rn = GrabbingRunner(cli, Plan())
it = PlanItem(kch_id="K1", do_id="X", token_at=0.0)
expect("令牌不知道什么时候拿的（token_at=0，例如落盘恢复的清单）→ 可疑",
       rn._token_suspect(it) is True)
it.token_at = time.monotonic()
expect("令牌刚拿到 → 不可疑", rn._token_suspect(it) is False)
it.do_id = ""
expect("根本没有令牌 → 可疑", rn._token_suspect(it) is True)

print()
print("=" * 62)
print("③ 单元：挑班口径（首解析与重查刷新必须同口径）")
print("=" * 62)

_j1, _j2, _j3 = FakeJxb("J1", "d1"), FakeJxb("J2", "d2"), FakeJxb("J3", "d3")
_j2.is_full = True
picked, note = R._pick_class([_j1, _j2, _j3], want_jxb_id="J3")
expect("有指定 jxb_id 时优先认回原班（哪怕它已满）",
       picked is _j3 and "认回原教学班" in note, f"{picked} / {note}")
picked, note = R._pick_class([_j1, _j2, _j3], want_jxb_id="J9")
expect("原班不在了 → 挑第一个未满的", picked is _j1, f"{picked} / {note}")
_j1.is_full = True
picked, note = R._pick_class([_j1, _j2], want_jxb_id="J9")
# ⚠️ 断言只钉**语义**（挑的是第一个 + 说明里点明「已满」），不钉整句话：
# 2026-10-01 满员改成终态失败后，这句 note 的措辞从「仍挂上蹲守」改成了
# 「仍挑第一个提交一发」—— 钉整句就会为了一个措辞改动而静默转红。
expect("全满 → 仍挑第一个提交一发（抢课不蹲守，只打一发）",
       picked is _j1 and "已满" in note, f"{note}")
expect("空列表 → 返回 None", R._pick_class([], want_jxb_id="J1")[0] is None)

print()
print("=" * 62)
print("④ 场景：手里那个令牌还有效 → **首发定胜负**（最省请求的那条路）")
print("=" * 62)

# 上午抓过上下文（store_at 推到 2 小时前），服务器重开后仍认那个旧令牌
srv = FakeServer(closed=False)
srv.accept = {"MORNING_TOKEN"}
cli = FakeClient(srv)
cli.init(force=True)
cli.store_at = time.monotonic() - 7200
plan = make_plan()
rn, ev = run(cli, plan)
item = plan.items[0]
expect("抢到了", item.state is TaskState.WON, f"实际 {item.state}")
expect("⭐ 用的是手里那个旧令牌（没有多打一趟刷新）",
       item.do_id == "MORNING_TOKEN", f"实际 {item.do_id}")
expect("服务器只收到 1 次提交", len(srv.submits()) == 1, f"实际 {srv.submits()}")
expect("⭐ 一次刷新请求都没发（首发就中，这才是串行的收益）",
       cli.queries == 0, f"queries={cli.queries}")
expect("开工时**强制重抓过上下文**（P0：不再吃上午那份快照）",
       cli.init_calls.count(True) >= 1, f"init 调用 {cli.init_calls}")
expect("只读的 init 请求真的发出去了（不是零请求的快照）", cli.real_fetches >= 1)

print()
print("=" * 62)
print("⑤ 场景：手里那个令牌已失效 → 先打一发（失败）→ 同步重查换新 → 重发")
print("=" * 62)

srv = FakeServer(closed=False)
srv.accept = {"TOK_FRESH"}          # 上午那个令牌在重开后被作废了
cli = FakeClient(srv)
cli.init(force=True)
cli.store_at = time.monotonic() - 7200
plan = make_plan()
rn, ev = run(cli, plan)
item = plan.items[0]
expect("抢到了", item.state is TaskState.WON, f"实际 {item.state}")
expect("⭐ 第一发用的就是手里那个旧令牌（正常路径 —— 那一刻没人和它抢令牌）",
       bool(cli.submits) and cli.submits[0] == "MORNING_TOKEN",
       f"submits={cli.submits}")
expect("⭐ 最终用的不是旧令牌（重查换新了）",
       item.do_id != "MORNING_TOKEN", f"实际 {item.do_id}")
expect("服务器收到 2 次提交（旧令牌一发 + 新令牌一发）",
       len(srv.submits()) == 2, f"实际 {srv.submits()}")
expect("⭐ 只有 1 次重查（够换一个令牌就行）", cli.queries == 1, f"queries={cli.queries}")
expect("⭐ 顺序是死的：提交 → 重查 → 提交。"
       "重查**绝不早于**提交（早一步就等于自己把旧令牌作废）",
       srv.order() == ["submit", "query_classes", "submit"],
       f"实际顺序 {srv.order()}")
expect("⭐ 查教学班之前**没有任何**并发请求（整轮只有一条写线程）",
       len([c for c in srv.order() if c == "query_classes"]) == 1, f"{srv.order()}")
logs = [e.message for e in ev if "刷新令牌" in getattr(e, "message", "")]
expect("日志交代了「为什么刷新令牌」", bool(logs), f"{logs}")
logs2 = [e.message for e in ev if "令牌已刷新" in getattr(e, "message", "")]
expect("日志交代了刷新结果（换新 / 串未变 + 是不是同一个班）", bool(logs2), f"{logs2}")

print()
print("=" * 62)
print("⑥ 场景：教务整段都没开放 → 别无限烧请求（一个写请求都不发）")
print("=" * 62)

srv = FakeServer(closed=True)
cli = FakeClient(srv)
cli.init(force=True)
cli.store_at = time.monotonic() - 7200
plan = make_plan()
rn, ev = run(cli, plan)
item = plan.items[0]
expect("判定为失败（教务确实没开）", item.state is TaskState.FAILED, f"实际 {item.state}")
expect("⭐ 未开放期间**一个写请求都没打在服务器上**（全靠闸门挡在家里）",
       len(srv.submits()) == 0, f"实际 {srv.submits()}")
expect("⭐ 也没有多余的查教学班请求（未开放时查了也是零请求）",
       cli.queries == 0, f"queries={cli.queries}")
expect("⭐ 每一次尝试都被本地闸门挡下", cli.gate_blocks >= 1,
       f"gate_blocks={cli.gate_blocks}")
expect("重抓上下文有上限（按时间收口，没有无限重抓）",
       cli.init_calls.count(True) <= 12, f"force init {cli.init_calls.count(True)} 次")
expect("日志交代了「等满耐心仍未开放」",
       any("教务仍未开放" in getattr(e, "message", "") for e in ev),
       f"{[e.message for e in ev if '未开放' in getattr(e, 'message', '')]}")

print()
print("=" * 62)
print("⑦ 场景：卡在开放瞬间 → 硬打没用，重读一遍上下文就接上了（P0 的收益）")
print("=" * 62)

srv = FakeServer(closed=True)
cli = FakeClient(srv)
cli.init(force=True)
cli.store_at = time.monotonic() - 7200
srv.opens_at = time.monotonic() + 0.3    # 第一发打出去的那一刻还没开，随后就开了
plan = make_plan()
rn, ev = run(cli, plan, max_attempts=20)
item = plan.items[0]
expect("⭐ 抢到了（教务刚开放就接上）", item.state is TaskState.WON, f"实际 {item.state}")
expect("确实经历了「未开放」这一下",
       any("本地闸门挡下" in getattr(e, "message", "") for e in ev),
       f"{[e.message for e in ev if '闸门' in getattr(e, 'message', '')]}")
expect("⭐ 判死之前**先刷了最后一次**上下文，并且认出了「刚开放」"
       "（旧写法在最后一次重抓之后就直接判死，这一整项会被丢掉）",
       any("耐心计时清零" in getattr(e, "message", "") for e in ev),
       f"{[e.message for e in ev if '耐心' in getattr(e, 'message', '')]}")
st = list(srv.submits())
expect("⭐ 未开放期间一个请求都没发，只在**真正开放之后**打了那一发",
       item.state is TaskState.WON and len(st) == 1, f"实际 {st}")
expect("开放前的每一次尝试都被闸门挡在家里（零请求）",
       cli.gate_blocks >= 2, f"gate_blocks={cli.gate_blocks}")
expect("为重开拿到了新令牌", item.do_id.startswith("TOK"), f"实际 {item.do_id}")

print()
print("=" * 62)
print("⑧ 场景：上下文与令牌都新鲜 → 首发即中，不多打一个只读请求")
print("=" * 62)

srv = FakeServer(closed=False)
srv.accept = {"FRESH_TOKEN"}
cli = FakeClient(srv)
cli.init(force=True)                     # 岁龄≈0，快照=已开放
plan = make_plan(do_id="FRESH_TOKEN", token_at=time.monotonic())
rn, ev = run(cli, plan)
item = plan.items[0]
expect("抢到了", item.state is TaskState.WON, f"实际 {item.state}")
expect("没有多余的 query_classes（只打了一发提交）", cli.queries == 0,
       f"queries={cli.queries}")
expect("服务器只收到 1 发提交（开工那次强制 init 是 P0 有意为之，不算多余）",
       len(srv.submits()) == 1, f"实际 {srv.net}")
inits = [c for c in srv.net if c[0] == "init"]
expect("全部上下文请求只有 2 次：建会话 1 次 + 开工强制重抓 1 次",
       len(inits) == 2, f"实际 {inits}")

print()
print("=" * 62)
print("⑨ 结构回归：侦察队已彻底移除（2026-09-30 用户指出的竞态）")
print("=" * 62)

expect("⭐ 模块级不再有 `_Scout` 类（并行刷令牌的载体）",
       not hasattr(R, "_Scout"))
expect("⭐ GrabbingRunner 不再有 _start_scout / _apply_scout",
       not hasattr(GrabbingRunner, "_start_scout")
       and not hasattr(GrabbingRunner, "_apply_scout"))
expect("⭐ GrabbingRunner 不再持有侦察队专用客户端（整轮只有一个客户端）",
       not hasattr(GrabbingRunner, "_scout_client_get")
       and not hasattr(GrabbingRunner, "_close_scout_client")
       and not hasattr(GrabbingRunner(cli, Plan()), "_scout_client"))
expect("⭐ ZfClient 不再有 fork()（它只为「并行刷令牌」而存在）",
       not hasattr(ZfClient, "fork"))
_gsig = inspect.signature(GrabbingRunner._grab_context)
expect("⭐ _grab_context 不再有 client= 参数（那个参数只为侦察队开）",
       "client" not in _gsig.parameters, f"实际参数：{list(_gsig.parameters)}")

print()
print("=" * 62)
print("⑩ 场景：网络抖一下不该判死（NETWORK 可重试，UNKNOWN 仍不可）")
print("=" * 62)


class FlakyClient(FakeClient):
    """前 N 次提交直接抛「网络异常」，之后正常。

    ⚠️ 关键：抛异常时**不往 `server.net` 里记**，也不动 `submits` ——
    网络异常意味着**请求根本没走到教务**，服务器什么都没收到。
    这正是它与「教务回了一句我们没归类的话」（UNKNOWN）的本质区别。
    """

    def __init__(self, server: FakeServer, *, fail_times: int = 2,
                 kind: FailureKind = FailureKind.NETWORK):
        super().__init__(server)
        self.fail_left = fail_times
        self.fail_kind = kind
        self.net_errors = 0

    def submit(self, kch_id, do_id, *, kcmc="", **kw):
        if self.fail_left > 0:
            self.fail_left -= 1
            self.net_errors += 1
            raise XKError(self.fail_kind,
                          "网络异常（超时 15s 或连接被拒）：ReadTimeout(15,)")
        return super().submit(kch_id, do_id, kcmc=kcmc, **kw)


srv = FakeServer(closed=False)
srv.accept = {"MORNING_TOKEN"}
cli = FlakyClient(srv, fail_times=2)
cli.init(force=True)
cli.store_at = time.monotonic() - 7200      # 上下文超龄（本来会走刷新路径）
plan = make_plan()
rn, ev = run(cli, plan, max_attempts=10)
item = plan.items[0]

expect("⭐ 连吃两发网络异常之后仍然抢到了（旧行为：第一发就判死）",
       item.state is TaskState.WON, f"实际 {item.state}")
expect("  一共打了 3 发（2 次网络异常 + 1 次成功）",
       cli.net_errors == 2 and item.attempts == 3, f"net={cli.net_errors} attempts={item.attempts}")
expect("⭐ 网络异常那两发**服务器一次都没收到**（请求没走到教务）",
       len(srv.submits()) == 1, f"实际 {srv.submits()}")
expect("⭐⭐ 网络异常**不触发重查令牌**（跟令牌新旧无关；重查只会白烧一个请求、"
       "还顺手作废手里那个令牌）",
       cli.queries == 0, f"queries={cli.queries}")
expect("  发过带 network 语义的 retry_wait 事件（日志能解释「为什么在重试」）",
       any(e.type is R.EventType.RETRY_WAIT and e.kind is FailureKind.NETWORK for e in ev),
       f"{[(e.type.value, e.kind) for e in ev if e.type is R.EventType.RETRY_WAIT]}")

# 对照组：UNKNOWN 仍然一发收口 —— 别把「拆分网络类」误伤成「什么都重试」
srv2 = FakeServer(closed=False)
srv2.accept = {"MORNING_TOKEN"}
cli2 = FlakyClient(srv2, fail_times=99, kind=FailureKind.UNKNOWN)
cli2.init(force=True)
plan2 = make_plan()
rn2, ev2 = run(cli2, plan2, max_attempts=10)
item2 = plan2.items[0]
expect("⭐ 对照：UNKNOWN（教务说了话但没归类）仍然**一发就收口**，不撞上限",
       item2.state is TaskState.FAILED and item2.attempts == 1,
       f"state={item2.state} attempts={item2.attempts}")
expect("  UNKNOWN 也照样不去重查令牌", cli2.queries == 0, f"queries={cli2.queries}")

print()
print("=" * 62)
if _fails:
    print(f"✗ {len(_fails)} 条未通过：")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("✓ 全部通过")
