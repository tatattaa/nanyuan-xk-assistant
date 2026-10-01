"""端到端验证：抢课任务的事件能否完整流到 SSE 客户端。

不依赖真实教务 —— 用一个假 ZfClient 替换真客户端，走完整引擎流程：
    /api/session（假 client 注入）→ /api/plan → /api/start → SSE 收事件 → done

必须在服务进程内执行，所以本脚本用 TestClient 同进程跑。

⚠️ 2026-09-30：抢课清单会落盘，而本脚本会真的 `POST /api/plan` + 启动任务
   （`RUNTIME.start()` 也会写盘）。所以必须在 import ui.* **之前**把落盘目录
   指到临时目录，否则会覆盖 `<项目根>/state/plan.json` 里用户真正的清单。
"""
import json
import os
import inspect
import sys
import tempfile
import threading
import time

sys.path.insert(0, ".")

# ⚠️ 必须早于 `import ui.*`：ui.state 在 import 时就把落盘目录定下来。
os.environ["XK_STATE_DIR"] = tempfile.mkdtemp(prefix="xk_e2e_state_")

from fastapi.testclient import TestClient  # noqa: E402

from core.config import Credential  # noqa: E402
from engine.events import Event, EventType  # noqa: E402
from ui.app import create_app  # noqa: E402
from ui.state import RUNTIME, save_plan_file  # noqa: E402
from engine.plan import Plan, PlanItem  # noqa: E402


class FakeClient:
    """假客户端：第 3 次提交成功，前两次报「频率过高」。

    ⚠️ 2026-10-01 起这里**不能再用满员**：满员已改成**终态失败、不重试**
    （见 `core/errors.py::FailureKind.FULL`），拿它做「重试两次然后成功」的
    场景会第一次就判 failed，整段用例全崩。
    改用 TOO_FREQUENT —— 它同样「教务正常回了话」→ 不触发令牌重查、
    也没有网络阶梯退避，所以本段原来的时间假设原样成立。
    """

    def __init__(self):
        self.n = 0
        self.tabs = []

    def init(self, force=False):
        return {"firstXkkzId": "FAKE1", "xkxnm": "2026", "_open": "1"}

    @property
    def is_open(self):
        return True

    def find_tab(self, kklxdm):
        return None

    def tab_at(self, index):
        """默认没有 Tab，任何下标都取不到（runner 会回落 find_tab）。"""
        return None

    def query_classes(self, kch_id, *, tab=None, kklxdm=None, cxbj="0", fxbj="0"):
        from core.client import Jxb

        return [Jxb(jxb_id="j1", do_id="do1", kcmc="假课", jsxx="张老师",
                    sksj="星期三第3-4节", jxbrl="60", yxzrs="59")]

    def precheck_conflict(self, kch_id, do_id):
        return {}

    def submit(self, kch_id, do_id, **kw):
        from core.errors import FailureKind

        self.n += 1
        if self.n < 3:
            return {"success": False, "flag": "-1", "msg": "选课频率过高，请稍后再试",
                    "kind": FailureKind.TOO_FREQUENT}
        return {"success": True, "flag": "1", "msg": "选课成功", "kind": None}

    def close(self):
        pass


app = create_app()
client = TestClient(app)

# 注入假会话
sess = RUNTIME.attach_session(Credential(cookie_header="JSESSIONID=x; route=y", source="manual"))
sess.client = FakeClient()
sess.inited = True
sess.is_open = True

print("1) 设置清单")
r = client.post("/api/plan", json={"items": [{"kch_id": "FAKE01", "kcmc": "模拟课程",
                                              "max_attempts": 10, "interval_ms": 100}]})
print("   ", r.status_code, r.json())

print("2) 启动任务")
r = client.post("/api/start")
print("   ", r.status_code, r.json())

print("3) 收集事件（轮询 /api/events）")
seen = []
# ⚠️ 窗口给宽一点：判据是「runner 跑完 + 事件全部收到」，而这是**墙钟**计时 ——
# 多套用例并发跑（本项目的回归就是循环跑 9 个套件）时 CPU 争抢会让它偶尔超时。
# 曾经在 12s 窗口下偶发丢过 success 事件，单独跑又必然通过，就是这个问题。
# 下面把「实际用时 + 到点时还在不在跑」打出来，下次真出问题能一眼看出是不是窗口不够。
_t_start = time.time()
deadline = _t_start + 20
last = 0
while time.time() < deadline:
    r = client.get(f"/api/events?since={last}")
    d = r.json()
    for ev in d["events"]:
        last = max(last, ev["seq"])
        seen.append(ev)
    if not RUNTIME.running and seen:
        # ⚠️ 这里有个 TOCTOU：上面那次读取发生在 T，而 running 是在 T+ε 才看的。
        # runner 可能刚好在两者之间推完最后几条（success / plan_done）并结束 ——
        # 于是「running 已经 False、事件却没读全」，表现为偶发丢 success。
        # 所以判定结束后**再补读一次**，把最后那批事件捞干净再退出。
        d = client.get(f"/api/events?since={last}").json()
        for ev in d["events"]:
            last = max(last, ev["seq"])
            seen.append(ev)
        break
    time.sleep(0.1)
print(f"   收集用时 {time.time() - _t_start:.1f}s；到点时 running={RUNTIME.running}"
      + ("（窗口可能不够！）" if RUNTIME.running else ""))

types = [e["type"] for e in seen]
print("   事件序列:", types)

print("4) 校验")
checks = [
    ("收到 plan_start", "plan_start" in types),
    ("收到多次 attempt", types.count("attempt") >= 2),
    ("收到 retry_wait（前两次失败后重试）", "retry_wait" in types),
    ("最终 success", "success" in types),
    ("success 带耗时", any(e["type"] == "success" and e["elapsed_ms"] >= 0 for e in seen)),
    ("可重试失败语义 kind=too_frequent", any(e.get("kind") == "too_frequent" for e in seen)),
]
ok = 0
for name, cond in checks:
    print(("   ✓ " if cond else "   ✗ ") + name)
    ok += 1 if cond else 0

# 状态汇总
s = client.get("/api/state").json()
print("5) 汇总:", json.dumps(s["counts"], ensure_ascii=False))
print("   任务项:", [(i["kcmc"], i["state"], i["attempts"]) for i in s["items"]])


# ---------------------------------------------------------------------------
# 6) 清单互斥：同一时间段只能上成一门 —— 第一项抢到后，同节次的其他项不再提交
# ---------------------------------------------------------------------------

print()
print("6) 互斥跳过：三项清单，第 1、2 项节次冲突，第 3 项独立")


class QuickWinClient(FakeClient):
    """一提交就成功，并记下每门课被提交了几次。"""

    def __init__(self):
        super().__init__()
        self.calls = []

    def submit(self, kch_id, do_id, **kw):
        self.calls.append(kch_id)
        return {"success": True, "flag": "1", "msg": "选课成功", "kind": None}


def _slot(weekday, start, end, weeks, raw):
    return {"weekday": weekday, "start": start, "end": end, "weeks": weeks, "raw": raw}


q = QuickWinClient()
sess2 = RUNTIME.attach_session(
    Credential(cookie_header="JSESSIONID=x; route=y", source="manual")
)
sess2.client = q
sess2.inited = True
sess2.is_open = True

items = [
    {"kch_id": "MA1", "do_id": "doA", "kcmc": "甲课", "max_attempts": 3, "interval_ms": 10,
     "slots": [_slot(2, 3, 5, list(range(6, 18)), "星期二第3-5节{6-17周}")]},
    {"kch_id": "MB1", "do_id": "doB", "kcmc": "乙课（与甲同节次且周次重叠）", "max_attempts": 3,
     "interval_ms": 10,
     "slots": [_slot(2, 4, 5, list(range(6, 18)), "星期二第4-5节{6-17周}")]},
    {"kch_id": "MC1", "do_id": "doC", "kcmc": "丙课（时间独立）", "max_attempts": 3,
     "interval_ms": 10,
     "slots": [_slot(4, 6, 7, [1, 2, 3], "星期四第6-7节{1-3周}")]},
    # 与甲课同天同节次，但周次完全错开 → 永远不撞，**不该**被跳过
    {"kch_id": "MD1", "do_id": "doD", "kcmc": "丁课（同节次但周次错开）", "max_attempts": 3,
     "interval_ms": 10,
     "slots": [_slot(2, 3, 5, [1, 2, 3, 4, 5], "星期二第3-5节{1-5周}")]},
]
# 本组验证的是「串行」派发下的互斥跳过语义（甲先终态、乙因冲突被跳过），
# 必须显式指定 serial —— 默认已改为 round_robin，若省略会被轮流模式改变行为。
r = client.post("/api/plan", json={"items": items, "retry_mode": "serial"})
mutex = r.json().get("mutex")
print("   /api/plan →", r.status_code, "mutex =", mutex)

# 上一轮跑完后 running 必须自动变 False（runner 结束即置 done），
# 否则这里会 400「已有抢课任务在运行」
r = client.post("/api/start")
print("   /api/start →", r.status_code, r.json())
start_ok = r.status_code == 200

seen2 = []
last2 = 0
deadline = time.time() + 12
while time.time() < deadline:
    d = client.get(f"/api/events?since={last2}").json()
    for ev in d["events"]:
        last2 = max(last2, ev["seq"])
        seen2.append(ev)
    if not RUNTIME.running and seen2:
        break
    time.sleep(0.1)

st2 = {i["kch_id"]: i for i in client.get("/api/state").json()["items"]}
types2 = [e["type"] for e in seen2]
skips = [e for e in seen2 if e["type"] == "give_up" and e.get("kind") == "conflict"]
print("   提交过的课:", q.calls)
print("   状态:", {k: v["state"] for k, v in st2.items()})

for name, cond in [
    ("上一轮结束后可以立刻开下一轮", start_ok),
    ("/api/plan 报出 1 对互斥（同节次但周次错开的不算）", mutex == [[0, 1]]),
    ("甲/丙/丁都提交了，乙一次都没发", set(q.calls) == {"MA1", "MC1", "MD1"}),
    ("甲课 won", st2["MA1"]["state"] == "won"),
    ("乙课 skipped 且 0 次尝试（周次重叠，真撞）",
     st2["MB1"]["state"] == "skipped" and st2["MB1"]["attempts"] == 0),
    ("丙课 won（时间独立不受影响）", st2["MC1"]["state"] == "won"),
    ("丁课 won（同节次但周次错开，照常抢）", st2["MD1"]["state"] == "won"),
    ("收到互斥跳过事件且说明原因",
     any("真的撞了" in e["message"] for e in skips)),
    ("事件序列含 3 次 success", types2.count("success") == 3),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# ---------------------------------------------------------------------------
# 6b) 同课不同班不互斥跳过（2026-10-01 用户确认「蹲课允许同课不同班」）
#
# 抢课铁律 #6「同一门课只押一个班」下本不会出现同课两班；但蹲课打破了这条，
# 允许同课不同班共存。它们的时段常相同，若沿用「时间重叠即互斥跳过」，
# 抢到/蹲到第一个班就会把同课另一个班也跳过 —— 那是错的：它们是不同教学班，
# 一个班有名额不代表另一个班也满。
# ---------------------------------------------------------------------------

print()
print("6b) 同课不同班：时间重叠但不互斥跳过，两个班都提交")

q2 = QuickWinClient()
sess_b = RUNTIME.attach_session(
    Credential(cookie_header="JSESSIONID=x; route=y", source="manual")
)
sess_b.client = q2
sess_b.inited = True
sess_b.is_open = True

r = client.post("/api/plan", json={"items": [
    {"kch_id": "SC1", "do_id": "doA", "kcmc": "同课甲班", "max_attempts": 3, "interval_ms": 10,
     "slots": [_slot(2, 3, 5, list(range(6, 18)), "星期二第3-5节{6-17周}")]},
    {"kch_id": "SC1", "do_id": "doB", "kcmc": "同课乙班", "max_attempts": 3, "interval_ms": 10,
     "slots": [_slot(2, 3, 5, list(range(6, 18)), "星期二第3-5节{6-17周}")]},
]})
print("   /api/plan →", r.status_code, "mutex =", r.json().get("mutex"))
client.post("/api/start")

seen_b = []
last_b = 0
deadline = time.time() + 12
while time.time() < deadline:
    d = client.get(f"/api/events?since={last_b}").json()
    for ev in d["events"]:
        last_b = max(last_b, ev["seq"])
        seen_b.append(ev)
    if not RUNTIME.running and seen_b:
        break
    time.sleep(0.1)

st_b = {i["kch_id"]: i for i in client.get("/api/state").json()["items"]}
print("   提交过的课:", q2.calls, "| 状态:", {k: v["state"] for k, v in st_b.items()})

# 注意：同课两班 key 都是 SC1，state 汇总按 kch_id 去重后只剩一条；用提交次数判
for name, cond in [
    ("同课不同班 → /api/plan 不报互斥（mutex 空）", r.json().get("mutex") == []),
    ("⭐ 两个班都提交了（不被互斥跳过）", sorted(q2.calls) == ["SC1", "SC1"]),
    ("⭐ 没有因时间重叠发互斥 skip", not any(e["type"] == "give_up" and e.get("kind") == "conflict" for e in seen_b)),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# ---------------------------------------------------------------------------
# 7) 铁律 #5：令牌过期要重查刷新 —— 且必须刷新回「原来那个教学班」
#
# 背景（2026-09-29 实测）：提交用的 do_id（= do_jxb_id）是**每次查询都会重新下发
# 的一次性加密令牌**，连查 3 次已选列表，36 个串一个都复现不了。
# 所以连续失败不能一律当成「没抢到」—— 也可能是手里的令牌已经不被接受。
# 但也不能一失败就重查（请求量翻倍）。判据是「失败的语义」：
#   FULL / CONFLICT / TOO_FREQUENT → 服务端正常处理了请求，令牌有效 → 不刷新
#   CONTEXT_INVALID / UNKNOWN       → 请求被拒，可能就是令牌过期 → 值得重查
# ---------------------------------------------------------------------------

print()
print("7) 令牌刷新：连续被拒 → 重查换新令牌（仍绑同一个班）")


class StaleTokenClient(FakeClient):
    """模拟「一次性令牌」：每次查教学班都下发一个新串，旧串一律被拒。

    前端传进来的 do_id 是**旧**串（stale），只有重查拿到新串才能提交成功。
    """

    def __init__(self):
        super().__init__()
        self.queries = 0      # 查了几次教学班
        self.submits = []     # 每次提交用的 do_id
        # 前端手里那个（"do_old"）一开始就是过期的 —— 只有重查才能拿到有效串
        self.current = "do_0"

    def query_classes(self, kch_id, *, tab=None, kklxdm=None, cxbj="0", fxbj="0"):
        from core.client import Jxb

        self.queries += 1
        # 模拟「每次查询重新下发令牌」：串里带上查询次数
        self.current = f"do_{self.queries}"
        return [Jxb(jxb_id="j1", do_id=self.current, kcmc="假课", jsxx="张老师",
                    sksj="星期三第3-4节", jxbrl="60", yxzrs="59")]

    def submit(self, kch_id, do_id, **kw):
        from core.errors import FailureKind

        self.submits.append(do_id)
        if do_id != self.current:
            return {"success": False, "flag": "", "msg": "加密串错误，可以清除浏览器缓存后刷新网页重试！",
                    "kind": FailureKind.CONTEXT_INVALID}
        return {"success": True, "flag": "1", "msg": "选课成功", "kind": None}


st = StaleTokenClient()
sess3 = RUNTIME.attach_session(Credential(cookie_header="JSESSIONID=x; route=y", source="manual"))
sess3.client = st
sess3.inited = True
sess3.is_open = True

# 刻意传一个**过期**的 do_id —— 只有重查换成新令牌才可能成功
r = client.post("/api/plan", json={"items": [{
    "kch_id": "TOK1", "do_id": "do_old", "jxb_id": "j1", "kcmc": "令牌测试课",
    "max_attempts": 10, "interval_ms": 10,
}]})
print("   /api/plan →", r.status_code)
client.post("/api/start")

seen3 = []
last3 = 0
deadline = time.time() + 12
while time.time() < deadline:
    d = client.get(f"/api/events?since={last3}").json()
    for ev in d["events"]:
        last3 = max(last3, ev["seq"])
        seen3.append(ev)
    if not RUNTIME.running and seen3:
        break
    time.sleep(0.1)

st3 = {i["kch_id"]: i for i in client.get("/api/state").json()["items"]}
msgs3 = [e["message"] for e in seen3]
refresh_log = [m for m in msgs3 if "令牌已刷新" in m]
print("   重查次数:", st.queries, "| 提交用的令牌:", st.submits)
print("   最终状态:", st3["TOK1"]["state"], "| 尝试次数:", st3["TOK1"]["attempts"])
for m in refresh_log:
    print("   刷新日志:", m)

for name, cond in [
    ("踩过一次「加密串错误」但没有一路撞到上限", st3["TOK1"]["attempts"] < 10),
    ("重查过教学班（铁律 #5：连续无果则重查刷新令牌）", st.queries >= 1),
    ("刷新后用的是**新**令牌，不是前端给的旧串",
     st.submits and st.submits[-1] != "do_old" and st.submits[-1] == st.current),
    ("刷新只发生一次就解决了（阈值生效、没反复重查）", st.queries == 1),
    ("最终抢到了", st3["TOK1"]["state"] == "won"),
    ("日志说清了「令牌已刷新」且仍是原来那个班",
     bool(refresh_log) and "仍是原来那个教学班" in refresh_log[0]),
    ("重查次数被记在事件里（几分之几）",
     any("/" in m and "刷新令牌（第" in m for m in msgs3)),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# ---------------------------------------------------------------------------
# 8) 满员 = 终态失败（2026-10-01 用户要求）
#
# 旧行为：满员可重试 → 一直重试到 max_attempts 才判 failed。
# 新行为：**只发这一发**就判 failed，一次都不重试、一次都不重查。
#
# 两个理由：
#   · 名额不会在 800ms 内自己冒出来 —— 重试同一发只是白烧尝试额度与配额
#     （教务对「选课频率过高」是会计数的）；
#   · 「等有人退课再抢」是**另一个时间尺度**的事（小时级），归将来的「蹲课」功能。
#
# 这条同时兜住两个方向：改回可重试（次数 != 1）会红，退化成「沉默卡住」也会红。
# ---------------------------------------------------------------------------

print()
print("8) 满员 = 终态失败：只发 1 发、判 failed、零重查")

class AlwaysFullClient(StaleTokenClient):
    """一直满员（令牌有效，服务端正常处理了请求）。

    ⚠️ 返回体刻意**照抄 `core/client.py::submit` 的形状**（含 `full_info`）：
    满员现在是终态失败、只发这一发，那条 give_up 就是用户能看到的唯一解释，
    所以这里必须连「已选 46 人」一起验，不能用简化假返回把它绕过去。
    """

    def submit(self, kch_id, do_id, **kw):
        from core.client import parse_full_msg
        from core.errors import FailureKind

        self.submits.append(do_id)
        msg = "0,J1,46,"                      # 实测格式：辅教学班标志,教学班id,已选人数,本轮已选
        return {"success": False, "flag": "-1", "msg": msg,
                "kind": FailureKind.FULL,
                "full_info": parse_full_msg(msg)}


af = AlwaysFullClient()
sess4 = RUNTIME.attach_session(Credential(cookie_header="JSESSIONID=x; route=y", source="manual"))
sess4.client = af
sess4.inited = True
sess4.is_open = True
client.post("/api/plan", json={"items": [{
    "kch_id": "FULL1", "do_id": "do_old", "jxb_id": "j1", "kcmc": "一直满员的课",
    "max_attempts": 12, "interval_ms": 5,
}]})
client.post("/api/start")

seen4 = []
last4 = 0
deadline = time.time() + 12
while time.time() < deadline:
    d = client.get(f"/api/events?since={last4}").json()
    for ev in d["events"]:
        last4 = max(last4, ev["seq"])
        seen4.append(ev)
    if not RUNTIME.running and seen4:
        break
    time.sleep(0.1)

st4 = {i["kch_id"]: i for i in client.get("/api/state").json()["items"]}
types4 = [e["type"] for e in seen4]
print("   重查次数:", af.queries, "| 提交次数:", len(af.submits))
print("   事件序列:", types4)
print("   面板文案:", st4["FULL1"].get("last_msg"))

for name, cond in [
    ("⭐⭐ 满员只发 1 发（不再重试到 max_attempts=12）", len(af.submits) == 1),
    ("   满员时**一次都不重查**（令牌有效，重查纯属浪费）", af.queries == 0),
    ("   判定为 failed（不是 won、也不是永远 running）", st4["FULL1"]["state"] == "failed"),
    ("   没有任何 retry_wait（它压根没进重试路径）", "retry_wait" not in types4),
    ("   发了一条 give_up，且说明「不可重试」",
     any(e["type"] == "give_up" and "不可重试" in (e.get("message") or "") for e in seen4)),
    ("   ⭐ 面板文案解释了「为什么没抢到」（含真实人数 46，铁律 #10）",
     "已满" in (st4["FULL1"].get("last_msg") or "")
     and "46" in (st4["FULL1"].get("last_msg") or "")),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# ---------------------------------------------------------------------------
# 9) 板块定位：kklxdm 重复时，只能靠 tab_index 命中正确板块
#    我校实测「板块课(大学体育)」与「板块课(大英一)」的 kklxdm 都是 06，
#    只存 kklxdm 的话，查大英一的课会打到体育板块去。
# ---------------------------------------------------------------------------

print()
print("9) 板块定位：两个 Tab 的 kklxdm 都是 06，靠下标区分")


class DupTabClient(FakeClient):
    """两个 Tab 同名 kklxdm。记录每次查询实际落到了哪个板块。"""

    def __init__(self):
        super().__init__()
        from core.client import Tab

        self.tabs = [
            Tab(kklxdm="06", xkkz_id="A", njdm_id="n", zyh_id="z",
                xkkz_xh="xh1", name="板块课(大学体育)"),
            Tab(kklxdm="06", xkkz_id="B", njdm_id="n", zyh_id="z",
                xkkz_xh="xh2", name="板块课(大英一)"),
        ]
        self.hits = {}   # kch_id -> 实际查询到的板块名

    def tab_at(self, index):
        return self.tabs[index] if 0 <= index < len(self.tabs) else None

    def find_tab(self, kklxdm):
        # 复刻真 client：kklxdm 重复时只能取到第一个
        for t in self.tabs:
            if t.kklxdm == kklxdm:
                return t
        return None

    def query_classes(self, kch_id, *, tab=None, kklxdm=None, cxbj="0", fxbj="0"):
        from core.client import Jxb

        self.hits[kch_id] = tab.name if tab else "(无板块)"
        return [Jxb(jxb_id="j_" + kch_id, do_id="do_" + kch_id, kcmc="课",
                    jsxx="老师", sksj="星期三第3-4节", jxbrl="60", yxzrs="10")]

    def submit(self, kch_id, do_id, **kw):
        from core.errors import FailureKind

        return {"success": True, "flag": "1", "msg": "选课成功",
                "kind": None}


dt = DupTabClient()

# 每一节都要**重新 attach**一个新会话：前面几节各自 attach 过（sess3 / sess4），
# RUNTIME.session 早已不是脚本开头那个 sess —— 沿用旧变量的话 client 换不进去，
# 本节就会拿上一节的假 client 跑（表现为「教学班人数已满 ×50」这种假象）。
sess5 = RUNTIME.attach_session(
    Credential(cookie_header="JSESSIONID=x; route=y", source="manual")
)
sess5.client = dt
sess5.inited = True
sess5.is_open = True

r = client.post("/api/plan", json={"items": [
    {"kch_id": "TAB0", "kcmc": "体育课", "kklxdm": "06", "tab_index": 0, "interval_ms": 10},
    {"kch_id": "TAB1", "kcmc": "大英一", "kklxdm": "06", "tab_index": 1, "interval_ms": 10},
    # 故意不给下标 —— 旧清单/手填场景，必须能回落到 kklxdm
    {"kch_id": "TABX", "kcmc": "旧清单", "kklxdm": "06", "interval_ms": 10},
]})
print("   /api/plan →", r.status_code)
print("   /api/start →", client.post("/api/start").status_code)

# 启动是异步的：先等它真的跑起来，再等它结束。
# 直接 `while ... and RUNTIME.running` 会在线程还没置位时第一次检查就退出。
time.sleep(0.3)
deadline = time.time() + 12
while time.time() < deadline and RUNTIME.running:
    time.sleep(0.1)
print("   任务已结束:", not RUNTIME.running, "| 剩余等待:", round(deadline - time.time(), 1))
st5 = {i["kch_id"]: i for i in client.get("/api/state").json()["items"]}
print("   实际查到的板块:", dt.hits)

for name, cond in [
    ("下标 0 → 命中「大学体育」", dt.hits.get("TAB0") == "板块课(大学体育)"),
    ("下标 1 → 命中「大英一」（kklxdm 同为 06，靠下标才分得开）",
     dt.hits.get("TAB1") == "板块课(大英一)"),
    ("不给下标 → 回落到 kklxdm，命中第一个「大学体育」",
     dt.hits.get("TABX") == "板块课(大学体育)"),
    ("三项都抢到了（板块没查错）",
     all(st5[k]["state"] == "won" for k in ("TAB0", "TAB1", "TABX"))),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# ---------------------------------------------------------------------------
# ⑩ 蹲课下满员**可重试**：蹲的正是「有人退课」腾出来的名额（2026-10-01）
#
# 与第 8 组（抢课下满员 = 终态失败、只发 1 发）严格对照：
#   · 抢课（max_attempts > 0）：FULL 名额不会在 800ms 内自己冒出来 → 终态 failed
#   · 蹲课（max_attempts = -1，按时间不按次数）：FULL 正是它要持续试探的状态，
#     必须走退避重试，直到有人退课或到点 —— 绝不能第一发就 give_up 结束任务。
#
# 之前 bug：蹲课复用抢课 runner，FULL 被当终态失败 → 第一发满员课就 give_up
#          → done → wait_running=false，表现为「开始蹲课后停止按钮立刻变暗」。
# ---------------------------------------------------------------------------

print()
print("### ⑩ 蹲课下满员可重试：持续蹲退课名额，不因 FULL 终态失败")

wf = AlwaysFullClient()
sess6 = RUNTIME.attach_session(
    Credential(cookie_header="JSESSIONID=x; route=y", source="manual")
)
sess6.client = wf
sess6.inited = True
sess6.is_open = True

# 先停掉可能残留的蹲课（上一组/上一轮）
client.post("/api/wait/stop")
time.sleep(0.2)

# 蹲课清单：max_attempts 由后端强制 -1，deadline 用相对 90 秒（足够观察多轮重试）
r = client.post("/api/wait/plan", json={
    "interval_ms": 1000,
    "start_at": "",
    "deadline_at": "+90",
    "items": [{
        "kch_id": "WAITFULL", "do_id": "do_old", "jxb_id": "j1",
        "kcmc": "一直满员的蹲课", "interval_ms": 1000,
    }],
})
print("   /api/wait/plan →", r.status_code, r.json().get("ok"), "deadline =",
      r.json().get("deadline_at"))

r_start = client.post("/api/wait/start")
print("   /api/wait/start →", r_start.status_code, r_start.json())
start_ok = r_start.status_code == 200

# 收集一段时间的事件：蹲课会持续对满员课退避重试，这里观察 ~4 秒应能看到多次 retry_wait
seen_w = []
last_w = 0
_deadline = time.time() + 4.0
while time.time() < _deadline:
    d = client.get(f"/api/events?since={last_w}").json()
    for ev in d["events"]:
        last_w = max(last_w, ev["seq"])
        seen_w.append(ev)
    time.sleep(0.1)

types_w = [e["type"] for e in seen_w]
state_w = client.get("/api/state").json()
wait_running = bool(state_w.get("wait_running"))
wait_items = {i["kch_id"]: i for i in state_w.get("wait_items", [])}
give_up_full = [e for e in seen_w if e["type"] == "give_up"]
print("   提交次数:", len(wf.submits), "| 事件:", types_w)
print("   wait_running =", wait_running, "| 蹲课项状态:", wait_items.get("WAITFULL"))

for name, cond in [
    ("蹲课启动成功", start_ok),
    ("⭐⭐ 满员没有判终态失败：wait_running 仍为 true（任务还在蹲）", wait_running),
    ("⭐⭐ 满员**持续重试**（提交次数 > 1，不是只发 1 发）", len(wf.submits) > 1),
    ("⭐⭐ 事件含 retry_wait 且说明「继续蹲退课名额」",
     any(e["type"] == "retry_wait" and "继续蹲退课名额" in (e.get("message") or "")
         for e in seen_w)),
    ("没有因 FULL 发 give_up（蹲课不把满员当终态）", len(give_up_full) == 0),
    ("蹲课项状态是 running（蹲守中，不是 failed / won）",
     wait_items.get("WAITFULL", {}).get("state") == "running"),
    ("没有触发令牌重查（FULL 令牌有效，重查纯属浪费）", wf.queries == 0),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# 显式停止，确认 stop 能正常收尾（wait_running → false）
client.post("/api/wait/stop")
time.sleep(0.3)
wait_running_after = bool(client.get("/api/state").json().get("wait_running"))
print("   显式 stop 后 wait_running =", wait_running_after)
_stopped = not wait_running_after
print(("   ✓ " if _stopped else "   ✗ ") + "显式 stop 后任务正常收尾（wait_running → false）")
checks.append(("显式 stop 后任务正常收尾（wait_running → false）", _stopped))
ok += 1 if _stopped else 0

# ---------------------------------------------------------------------------
# ⑦ 乐观锁：陈旧副本不许覆盖服务端清单
#
# 这一段是「把用户手工攒的清单清空两次」那个 bug 的回归守卫。
# 前端 `S.plan` 只是本地副本，清单却会被别的来源改写（另一个标签页 / 脚本 /
# 换学期作废）。没有乐观锁时，前端任何一次提交都会用陈旧副本把服务端整个盖掉。
# ---------------------------------------------------------------------------
print()
print("### ⑦ 清单乐观锁（base_version）")

st0 = client.get("/api/state").json()
rev0 = st0.get("plan_rev")
print("   /api/state plan_rev =", repr(rev0))

# ① 不带版本号 = 无条件覆盖（脚本 / CLI 这类一次性写入者走这条）
r1 = client.post("/api/plan", json={"items": [{"kch_id": "LOCK_NOVER", "kcmc": "没带版本号"}]})
d1 = r1.json()
rev1 = d1.get("plan_rev")
print("   不带版本号 →", r1.status_code, "lock =", d1.get("lock"), "rev =", rev1)

# ② 带**正确**的版本号 → 通过，版本号 +1
r2 = client.post("/api/plan", json={
    "base_version": rev1,
    "items": [{"kch_id": "LOCK_OK", "kcmc": "带了正确版本号"}],
})
d2 = r2.json()
rev2 = d2.get("plan_rev")
print("   带正确版本号 →", r2.status_code, "lock =", d2.get("lock"), "rev =", rev2)

# ③ 带**陈旧**版本号 → 409，而且**一个字节都不许写**
r3 = client.post("/api/plan", json={
    "base_version": rev1,           # ← 中间已经被 ② 改成 rev2 了，这个是旧的
    "items": [{"kch_id": "LOCK_STALE", "kcmc": "陈旧副本不该覆盖成功"}],
})
d3 = r3.json()
detail3 = d3.get("detail") if isinstance(d3.get("detail"), dict) else {}
after = client.get("/api/state").json()
after_keys = [i["kch_id"] for i in after["items"]]
print("   带陈旧版本号 →", r3.status_code, d3.get("detail"))
print("   冲突后服务端清单：", after_keys, "rev =", after.get("plan_rev"))

# ④ 409 之后，用**最新**版本号重做一次 → 应该成功（这就是界面走的恢复路径）
r4 = client.post("/api/plan", json={
    "base_version": after.get("plan_rev"),
    "items": [{"kch_id": "LOCK_RETRY", "kcmc": "重载后重做"}],
})
d4 = r4.json()
final_keys = [i["kch_id"] for i in client.get("/api/state").json()["items"]]
print("   重载后重做 →", r4.status_code, "rev =", d4.get("plan_rev"))

for name, cond in [
    ("/api/state 下发 plan_rev（整数）", isinstance(rev0, int)),
    ("不带 base_version → 200 且 lock=forced（脚本兼容）",
     r1.status_code == 200 and d1.get("lock") == "forced"),
    ("带正确 base_version → 200 且 lock=checked",
     r2.status_code == 200 and d2.get("lock") == "checked"),
    ("每次写入都让 plan_rev 递增",
     isinstance(rev1, int) and isinstance(rev2, int) and rev2 == rev1 + 1),
    ("⭐ 带陈旧 base_version → 409（不是静默覆盖）", r3.status_code == 409),
    ("⭐ 409 的 detail.kind == plan_conflict（前端据此走重载分支）",
     detail3.get("kind") == "plan_conflict"),
    ("409 带回服务端当前版本号（前端要拿它重做）",
     detail3.get("plan_rev") == rev2),
    ("⭐⭐ 409 时**一个字节都没写**：清单仍是上一版、版本号也没动",
     after_keys == ["LOCK_OK"] and after.get("plan_rev") == rev2),
    ("重载后用最新版本号重做 → 成功",
     r4.status_code == 200 and final_keys == ["LOCK_RETRY"]),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# ---------------------------------------------------------------------------
# ⑧ 落盘快照：启动**不自动恢复**，要用户主动点「加载」
#
# 用户 2026-09-30 的要求：落盘的数据平时不用调用，只在「查课和抢课之间」那段
# 未开放期给一个按钮，让用户自由选择是否加载上次的数据来查看课程。
# 关键点两条：① 内存空清单时才看得见磁盘那份（只读元信息）；
#             ② 加载**失败时绝不清空**已有清单，加载成功才进内存。
# ---------------------------------------------------------------------------
print()
print("### ⑧ 落盘快照：用户主动「加载」才进内存")

RUNTIME.clear_plan()
snap_plan = Plan()
snap_plan.add(PlanItem(kch_id="SNAP1", kcmc="上次存的课", xf="1.5"))
# 直接写盘：模拟「上次运行留下的数据」躺在那里、而进程是刚起来的
save_plan_file(snap_plan, semester=RUNTIME.semester_key() or "")

d0 = client.get("/api/state").json()
rev0 = d0.get("plan_rev")
print("   /api/state snapshot =", d0.get("snapshot"))
print("   内存里 items =", [i["kch_id"] for i in d0["items"]])

r1 = client.post("/api/plan/load")
d1 = r1.json()
print("   POST /api/plan/load →", r1.status_code, {k: d1.get(k) for k in ("count", "plan_rev")})

d2 = client.get("/api/state").json()
snap_after = d2.get("snapshot")
keys2 = [i["kch_id"] for i in d2["items"]]

# 清掉内存与磁盘（clear_plan 会连文件一起删）→ 再加载应当明确失败
RUNTIME.clear_plan()
r3 = client.post("/api/plan/load")
d3 = r3.json()
print("   清空后再加载 →", r3.status_code, d3.get("detail"))

for name, cond, *_detail in [
    ("/api/state 下发 snapshot（磁盘上「上次保存的数据」的只读元信息）",
     isinstance(d0.get("snapshot"), dict) and d0["snapshot"].get("count") == 1,
     f"{d0.get('snapshot')}"),
    ("⭐ 启动后内存里**没有**清单（不自动恢复）",
     d0.get("items") == [] and d0.get("plan_meta") == {}, f"{d0.get('items')}"),
    ("快照元信息带课程名（点加载前就能看清是哪几门）",
     (d0.get("snapshot") or {}).get("courses") == ["上次存的课"],
     f"{(d0.get('snapshot') or {}).get('courses')}"),
    ("⭐ POST /api/plan/load → 200 且把快照加载进内存",
     r1.status_code == 200 and d1.get("count") == 1, f"{r1.status_code} {d1}"),
    ("⭐ 加载后清单里就是那份数据（状态重置为待选）", keys2 == ["SNAP1"], f"{keys2}"),
    ("加载让版本号递增（前端手里那个号随之作废 → 走安全的重载路径）",
     isinstance(d1.get("plan_rev"), int) and d1["plan_rev"] > rev0,
     f"{rev0} → {d1.get('plan_rev')}"),
    ("⭐ 加载后 snapshot 变空（内存有清单了，磁盘那份就是它自己）",
     not snap_after, f"{snap_after}"),
    ("⭐ 磁盘上没有快照时加载 → 400（明确失败，不是静默无事发生）",
     r3.status_code == 400, f"{r3.status_code} {d3}"),
    ("⭐⭐ 加载失败**绝不清空**已有清单（这里是清空后的空清单，仍不该报成功）",
     r3.status_code != 200),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# ---------------------------------------------------------------------------
# ⑨ 课程数据落盘：搜索顺手存一份，未开放期能离线回看（2026-09-30 用户要求）
#
# ⚠️ 这一节必须用**真**假 client（带 query_courses）—— 前面几节的假 client 没有
# 这个方法，走不出落盘那一段。
# ---------------------------------------------------------------------------
print()
print("### ⑨ 课程数据落盘 + 离线加载")


class CourseClient:
    """只会一件事：返回两条课程行。用来触发 /api/courses 的落盘分支。"""

    def __init__(self):
        self.tabs = []

    def init(self, force=False):
        return {}

    @property
    def is_open(self):
        return True

    @property
    def store_age_s(self):
        return 0.0

    @property
    def semester_key(self):
        return "2026-2027|1"

    def query_courses(self, keyword="", **kw):
        return (
            [{"kch_id": "CK1", "kcmc": "空手道", "xf": "1.0"},
             {"kch_id": "CK2", "kcmc": "篮球", "xf": "1.0"}],
            {"kklxdm": "06", "kklxmc": "板块课(大学体育（一）)", "xkkz_id": "X",
             "sfxsjc": "", "kspage": "1", "jspage": "200"},
        )


sess9 = RUNTIME.attach_session(
    Credential(cookie_header="JSESSIONID=x; route=y", source="manual")
)
sess9.client = CourseClient()
sess9.inited = True
sess9.is_open = True

d_before = client.get("/api/state").json()
r_c = client.get("/api/courses?tab_index=1&size=200")
print("   /api/courses →", r_c.status_code, "count =", r_c.json().get("count"))

d_c = client.get("/api/state").json()
snap_c = d_c.get("courses_snapshot") or {}
print("   /api/state courses_snapshot =",
      {k: snap_c.get(k) for k in ("exists", "total")}, snap_c.get("buckets"))

r_load = client.get("/api/courses/snapshot/load?kklxdm=06&kklxmc=板块课(大学体育（一）)")
d_load = r_load.json()
print("   离线加载 →", r_load.status_code, "count =", d_load.get("count"))

r_miss = client.get("/api/courses/snapshot/load?kklxdm=99&kklxmc=不存在")
d_miss = r_miss.json()
print("   加载不存在的类别 →", r_miss.status_code, d_miss.get("reason"))

for name, cond, *_detail in [
    ("⭐ 搜索成功后课程数据自动落盘（不用前端额外调一个『存一下』的接口）",
     snap_c.get("exists") is True and snap_c.get("total") == 2, f"{snap_c}"),
    ("摘要按类别分桶，带类别名与课程名",
     any(b.get("kklxmc") == "板块课(大学体育（一）)" and b.get("count") == 2
         and "空手道" in (b.get("courses") or [])
         for b in snap_c.get("buckets") or []),
     f"{snap_c.get('buckets')}"),
    ("⭐ 离线加载取回全部行（未开放期就靠它看课）",
     r_load.status_code == 200 and d_load.get("ok") is True
     and [x["kch_id"] for x in d_load.get("rows") or []] == ["CK1", "CK2"],
     f"{r_load.status_code} {d_load.get('count')}"),
    ("加载结果带回类别信息（界面据此切下拉 / 说明来源）",
     (d_load.get("bucket") or {}).get("kklxmc") == "板块课(大学体育（一）)",
     f"{d_load.get('bucket')}"),
    ("⭐ 类别对不上 → ok=False + 列出可用的类别（不丢一个空列表让人以为坏了）",
     r_miss.status_code == 200 and d_miss.get("ok") is False
     and (d_miss.get("available") or []), f"{r_miss.status_code} {d_miss}"),
    ("课程快照独立于清单（清单为空也照样有课程数据）",
     d_c.get("plan_meta") == {} and snap_c.get("exists") is True),
]:
    print(("   ✓ " if cond else "   ✗ ") + name)
    checks.append((name, cond))
    ok += 1 if cond else 0

# ---------------------------------------------------------------------------
# ⑩ 事件序号与游标：实时日志不许「第二轮就空白」
#
# 用户 2026-10-01 报：「串行模式实时日志里怎么不显示了」。**根因不是串行**，
# 是**事件序号被重置**：
#   · `RUNTIME.start()` 里曾有一句 `self._seq = 0` —— 于是每点一次「开始」序号都
#     从 1 重来，而浏览器的游标 `S.seq` 还停在上一次的最后一号。`seq > since`
#     从此永不成立 → **第二轮日志整段不显示**（第一轮正常，所以很难联想到序号）。
#   · 服务重启同理：`_seq` 是**进程内**的，而前端的游标能跨进程活下来。
# 修法两条：
#   ① `_seq` 全程单调 —— `start()` 只清 `_events`（面板只显示本轮），不清 `_seq`；
#   ② 陈旧游标（`since > seq`）在**入口**夹回 0 → 重放，而不是默默吞掉。
#      ⚠️ 只夹在 `events_since()` 里是不够的：调用方会 `last = max(last, ev.seq)`
#      把那个大数一直带着走，所以 `/api/events` 与 `_sse` 的入口都要夹。
# ---------------------------------------------------------------------------
print()
print("### ⑩ 事件序号与游标（实时日志不许第二轮就空白）")

# 先确保没有任务在后台推事件，否则下面的「空增量」断言会偶发失败
client.post("/api/stop")
time.sleep(0.3)

RUNTIME.push_event(Event(type=EventType.LOG, message="游标自检：第一条"))
_c1 = RUNTIME.seq
RUNTIME.push_event(Event(type=EventType.LOG, message="游标自检：第二条"))
_c2 = RUNTIME.seq

_d_stale = client.get(f"/api/events?since={_c2 + 999}").json()
_d_inc = client.get(f"/api/events?since={_c1}").json()
_d_none = client.get(f"/api/events?since={_c2}").json()
print(f"   序号 {_c1} → {_c2}；陈旧游标(since={_c2 + 999}) 取回 "
      f"{len(_d_stale['events'])} 条；增量(since={_c1}) 取回 {len(_d_inc['events'])} 条")

for name, cond, *_detail in [
    ("事件序号严格递增", _c2 > _c1, f"{_c1} → {_c2}"),
    ("⭐ 陈旧游标（大于服务端 seq）夹回 0 —— 重放，而不是永远收不到事件",
     RUNTIME.clamp_since(_c2 + 999) == 0, f"{RUNTIME.clamp_since(_c2 + 999)}"),
    ("⭐ 陈旧游标经 /api/events 仍拿得到事件（服务重启后日志照样出来）",
     len(_d_stale["events"]) > 0, f"{len(_d_stale['events'])} 条"),
    ("正常游标原样保留（增量语义没被改坏）",
     RUNTIME.clamp_since(_c1) == _c1, f"{RUNTIME.clamp_since(_c1)} vs {_c1}"),
    ("增量取回的正好是那之后推的那条",
     [e.get("message") for e in _d_inc["events"]] == ["游标自检：第二条"],
     f"{[e.get('message') for e in _d_inc['events']]}"),
    ("游标 == 当前 seq → 空增量（不多推、不重推）",
     _d_none["events"] == [], f"{_d_none['events'][:2]}"),
    ("负数 / 非法游标 → 夹回 0",
     RUNTIME.clamp_since(-5) == 0 and RUNTIME.clamp_since("x") == 0
     and RUNTIME.clamp_since(None) == 0),
    ("⭐ start() 不再把 _seq 归零（「第二轮日志空白」的根因，源码级守卫）",
     "self._seq = 0" not in inspect.getsource(type(RUNTIME).start),
     "start() 里又出现了 self._seq = 0"),
]:
    print(("   ✓ " if cond else "   ✗ ") + name + ("" if cond else f"   {_detail}"))
    checks.append((name, cond))
    ok += 1 if cond else 0

print()
print(f"===== 通过 {ok}/{len(checks)} =====")
sys.exit(0 if ok == len(checks) else 1)
