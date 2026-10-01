"""登录态（会话）状态的自检 —— 顶部状态栏为什么曾一直显示「已登录」。

背景（这是用户 2026-09-30 报的真 bug）
--------------------------------------------------------------------------
用户在**教务网站点了退出登录**，但本工具顶部状态栏仍然显示「已登录」。

根因是一句语义偷换：`/api/state` 的 `has_session` 只看
`Runtime._session is not None` —— 它回答的是「**本进程内存里还攥着一份凭据吗**」，
而不是「**教务还认这个登录态吗**」。凭据躺在内存里不会自己过期，
但教务那边的会话会（退出登录 / Cookie 到期 / 被挤下线）。

更糟的是：前端每 3 秒轮询 `/api/state`，而这个接口**一个请求都不打教务**，
所以这件事永远没人发现 —— 界面会一直显示「已登录」，直到用户真去点一次操作、
撞回一个「登录态失效」才发现；而那时也只是记一条日志，顶部栏照样绿着。

修法（三件事，缺一不可）
--------------------------------------------------------------------------
1. 服务端多报一个**诚实的**状态 `session_state` ∈ none/expired/ok/unverified，
   并带上「上次联网核实的时刻」与「失效原因」；
2. **被动发现**：任何一次请求被判成登录失效（`_fail`）或引擎推出 NEED_LOGIN 事件，
   都立刻把会话标记为失效（`Runtime.mark_session_invalid`）；
3. **主动核实**：新增 `GET /api/session/check`（只发一个 GET，不碰选课上下文），
   前端每 ~45 秒调一次 —— 登录态这件事只能真的打一次教务才知道。

    python tests/smoke_session.py
"""

import os
import sys
import tempfile

sys.path.insert(0, ".")

# ⚠️ 必须早于 `import ui.*`：ui.state 在 import 时就把落盘目录定下来。
os.environ["XK_STATE_DIR"] = tempfile.mkdtemp(prefix="xk_session_state_")

from fastapi.testclient import TestClient  # noqa: E402

from core.config import Credential  # noqa: E402
from core.errors import FailureKind, XKError  # noqa: E402
from engine.events import Event, EventType  # noqa: E402
from ui.app import create_app  # noqa: E402
from ui.state import RUNTIME  # noqa: E402

_fails: list[str] = []


def expect(label, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + label + ("" if cond else f"  → {detail}"))
    if not cond:
        _fails.append(label)


class FakeClient:
    """假客户端：只需要支撑 `/api/state` 的几个属性 + 一个可开关的 `ping()`。"""

    def __init__(self) -> None:
        self.tabs = []
        self.store = {"xkkz_id": "K1", "_open": "1"}
        self.pings = 0
        self.ping_error: XKError | None = None
        self.ping_is_open = True

    # -- 供 /api/state 用 --
    def init(self, force=False):
        return dict(self.store)

    @property
    def is_open(self):
        return True

    @property
    def store_age_s(self):
        return 0.0

    @property
    def semester_key(self):
        return "2026-2027|1"

    # -- 探活 --
    def ping(self):
        """默认成功；把 `ping_error` 设上就模拟「教务把你踢回登录页」。"""
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error
        return {"is_open": self.ping_is_open, "bytes": 1234}


def attach(client: FakeClient):
    """建一个会话并把假客户端塞进去（init 视作已成功）。"""
    sess = RUNTIME.attach_session(
        Credential(cookie_header="JSESSIONID=x; route=y", source="manual")
    )
    sess.client = client
    sess.inited = True
    sess.is_open = True
    # 建会话本来就会走一次 init（真实网络往返）→ 这里补记账，与 _do_init 对齐
    RUNTIME.note_session_verified(is_open=True)
    return sess


def state():
    return client.get("/api/state").json()


client = TestClient(create_app())

print("=" * 62)
print("① 四个状态值：none / unverified / ok / expired")
print("=" * 62)

RUNTIME.detach_session()
d = state()
expect("没会话 → session_state == 'none'",
       d.get("session_state") == "none", f"{d.get('session_state')!r}")
expect("  且 has_session 也是 False", d.get("has_session") is False)

fk = FakeClient()
sess = attach(fk)
d = state()
expect("建会话（走完一次 init）→ session_state == 'ok'",
       d.get("session_state") == "ok", f"{d.get('session_state')!r}")
expect("  且带回「上次联网核实时刻」", isinstance(d.get("session_checked_at"), (int, float))
       and d["session_checked_at"] > 0, f"{d.get('session_checked_at')!r}")

# 「建会话之后还没核实过」这一档：把记账抹掉即可复现
sess.verified_at = 0.0
d = state()
expect("有凭据但还没联网核实过 → 'unverified'（不许冒充已核实）",
       d.get("session_state") == "unverified", f"{d.get('session_state')!r}")
RUNTIME.note_session_verified(is_open=True)

print()
print("=" * 62)
print("② 被动发现：引擎推 need_login → 会话立刻记失效")
print("=" * 62)

# 抢课跑在后台线程里，撞上登录失效只会推一条 NEED_LOGIN 事件
# （engine/runner.py::_handle_fatal）—— Runtime.push_event 里挂了钩子。
n_before = len(RUNTIME.events_since(0))
RUNTIME.push_event(Event(type=EventType.NEED_LOGIN, message="登录态失效，需重新提供 Cookie"))
d = state()
expect("⭐ 引擎推 need_login → session_state 立刻变 'expired'",
       d.get("session_state") == "expired", f"{d.get('session_state')!r}")
expect("  失效原因被记下来（不能只说一句「失效了」）",
       "重新提供 Cookie" in (d.get("session_invalid_msg") or ""),
       f"{d.get('session_invalid_msg')!r}")
expect("  ⚠️ 但凭据**仍在**（不 detach）：has_session 仍为 True —— "
       "这正是旧代码显示「已登录」的由来，界面必须看 session_state",
       d.get("has_session") is True)

n_after = len(RUNTIME.events_since(0))
expect("  只补记一次，不会把日志刷屏", n_after == n_before + 1, f"{n_before} → {n_after}")

# 再推一次不该再刷一条
RUNTIME.push_event(Event(type=EventType.NEED_LOGIN, message="又失效了"))
expect("  重复 need_login 不再重复记账",
       len(RUNTIME.events_since(0)) == n_before + 2
       and state().get("session_invalid_msg") == d.get("session_invalid_msg"))

print()
print("=" * 62)
print("③ 主动核实：/api/session/check 真的打一次教务")
print("=" * 62)

RUNTIME.note_session_verified(is_open=True)      # 先恢复
fk.pings = 0
r = client.get("/api/session/check")
d = r.json()
expect("探活成功 → 200", r.status_code == 200, f"{r.status_code} {d}")
expect("  且恰好只发了 **1** 个请求（这是它能被频繁调用的原因）",
       fk.pings == 1, f"pings={fk.pings}")
expect("  核实结果写回状态", d.get("session_state") == "ok" and d.get("checked_at"),
       f"{d}")

# 探活顺带更新「是否已开放」：判据与 init 同源（core.client.page_is_open）
fk.ping_is_open = False
client.get("/api/session/check")
d = state()
expect("探活顺带把「选课已开放」也刷新了（同一份 HTML，不白跑）",
       d.get("is_open") is False, f"is_open={d.get('is_open')}")
fk.ping_is_open = True
client.get("/api/session/check")

print()
print("=" * 62)
print("④ 退出登录：探活撞回登录页 → 409 + 状态翻成 expired")
print("=" * 62)

fk.ping_error = XKError(FailureKind.SESSION_EXPIRED, "302 → 登录页")
r = client.get("/api/session/check")
d = r.json()
detail = d.get("detail") if isinstance(d.get("detail"), dict) else {}
expect("⭐ 探活撞回登录页 → 409", r.status_code == 409, f"{r.status_code} {d}")
expect("  detail.kind == session_expired（前端据此把胶囊翻红）",
       detail.get("kind") == "session_expired", f"{detail.get('kind')!r}")

d = state()
expect("⭐⭐ /api/state 的 session_state 变 'expired'",
       d.get("session_state") == "expired", f"{d.get('session_state')!r}")
expect("  并带回失效原因", bool(d.get("session_invalid_msg")),
       f"{d.get('session_invalid_msg')!r}")
expect("  ⚠️ has_session 仍为 True —— 这就是旧代码「一直显示已登录」的原因",
       d.get("has_session") is True)

print()
print("=" * 62)
print("⑤ 恢复：重新核实成功 → 立刻摘掉「已失效」")
print("=" * 62)

fk.ping_error = None
r = client.get("/api/session/check")
d = r.json()
expect("重新核实成功 → 200 且状态回到 'ok'",
       r.status_code == 200 and d.get("session_state") == "ok", f"{r.status_code} {d}")
d = state()
expect("  /api/state 里的失效原因被清空",
       not d.get("session_invalid_msg"), f"{d.get('session_invalid_msg')!r}")

print()
print("=" * 62)
print("⑥ 抢课运行中不掺和：探活直接跳过（别往同一个客户端上叠请求）")
print("=" * 62)


class FakeRunner:
    stopped = False
    done = False


RUNTIME._runner = FakeRunner()
fk.pings = 0
r = client.get("/api/session/check")
d = r.json()
expect("抢课运行中 → 探活跳过，且一个请求都没发",
       r.status_code == 200 and d.get("skipped") == "running" and fk.pings == 0,
       f"{r.status_code} {d} pings={fk.pings}")
expect("  跳过时说清了理由", bool(d.get("reason")), f"{d.get('reason')!r}")
RUNTIME._runner = None

print()
print("=" * 62)
print("⑦ 建会话 / 清会话 会把状态重置（别把旧的「已失效」带进新会话）")
print("=" * 62)

RUNTIME.mark_session_invalid("上一次的失效")
expect("先制造一个「已失效」", state().get("session_state") == "expired")
attach(FakeClient())
d = state()
expect("⭐ 重新建会话 → 状态立刻干净（'ok'，旧的失效原因不再残留）",
       d.get("session_state") == "ok" and not d.get("session_invalid_msg"),
       f"{d.get('session_state')!r} / {d.get('session_invalid_msg')!r}")

RUNTIME.detach_session()
d = state()
expect("清会话 → 回到 'none' 且 has_session=False",
       d.get("session_state") == "none" and d.get("has_session") is False)

print()
print("=" * 62)
if _fails:
    print(f"✗ {len(_fails)} 条未通过：")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("✓ 全部通过")
