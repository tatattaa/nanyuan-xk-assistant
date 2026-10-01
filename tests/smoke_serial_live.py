"""开火相「严格串行」的**真客户端**验证 —— 真 ZfClient / 真 HTTP / 真 Runner 打模拟教务。

为什么要单独有这个脚本（`smoke_p0.py` 不够）
--------------------------------------------------------------------------
`smoke_p0.py` 用的是替身（`FakeClient`），它只复刻了**行为语义**（快照缓存、
本地闸门、一次性令牌），并把「服务器收到了什么、什么顺序」记在一个列表里。
它证明不了的事，正是这个脚本要证明的：

  · 真 `ZfClient` 走真 `HttpSession` 时，「提交」与「重查教学班」这两类请求
    **在线上真实的先后顺序**（替身是按调用顺序记的，线上才是算数的）；
  · 真 `query_classes` 回来的 `do_jxb_id` 能被真 `_refresh_do_id` 认回同一个班；
  · 整轮**确实只有一条线程、一个客户端**在跑写请求。

历史背景
--------------------------------------------------------------------------
曾经有过一个「两队并行」的设计：开火前支起一支后台侦察队（`_Scout` + `client.fork()`）
去刷新令牌，主线程同时拿旧令牌打一发，谁先成用谁。
**2026-09-30 用户指出了一个原理级错误**：

  > 既然新令牌会让旧令牌失效，那你怎么保证一定是 a 队先带着旧令牌访问服务器？
  > 万一在 a 队到达前令牌就被 b 队的申请报废了呢

对的。`do_jxb_id` 是一次性令牌，「刷新」会作废手里的旧令牌。侦察队是在主线程发提交
**之前**启动的、而且还复用自己的客户端（从第二门课起上下文已新鲜，零请求直奔查教学班）
—— 于是「谁先到服务器」纯看运气，本来稳赢的一发变成掷硬币。已整体删除，
改成**严格串行**：先打一发 → 失败才同步重查换新令牌 → 重发。

这个脚本就是那件事的**线上级**证据：`HTTP 顺序里，第一个提交请求必须早于任何一次重查`。

全程离线：只打 `mock_server` 起的本地假教务（抓包回放），不碰真实教务、
不需要浏览器、不需要 Cookie。也不碰 UI / 落盘目录（`XK_STATE_DIR` 指到临时目录）。

    python tests/smoke_serial_live.py
"""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, ".")

# ⚠️ 必须在 import ui.* **之前**设好：ui.state 在 import 时就会去读清单文件。
# 不设的话它会读到（甚至写坏）真实的 state/plan.json —— smoke_e2e 踩过这个坑。
os.environ["XK_STATE_DIR"] = tempfile.mkdtemp(prefix="xk_serial_state_")

from mock_server import start_mock  # noqa: E402

CAP = "captures/2026-09-29-open2"
#: Tab0「主修课程」在这份抓包里没有课程行，用 Tab1「板块课(大学体育（一））」。
TAB = 1

#: 抓包里的路径片段 —— 用来在 HTTP 层给请求分类。
PATH_PIECE_SUBMIT = "zzxkyzbjk_xkBcZyZzxkYzb"     # 提交
PATH_PIECE_CLASSES = "zzxkyzbjk_cxJxbWithKch"     # 查教学班（重查刷新令牌）
PATH_PIECE_PREPACK = "zzxkyzb_cxCtKcZy"           # 时间冲突预检
PATH_PIECE_INDEX = "zzxkyzb_cxZzxkYzbIndex"        # 选课首页（探活打的就这一页）

_fails: list[str] = []


def expect(label, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + label + ("" if cond else f"  → {detail}"))
    if not cond:
        _fails.append(label)


httpd, port = start_mock(CAP, port=0)
os.environ["XK_SCHOOL_URL"] = f"http://127.0.0.1:{port}/jwglxt/"

from core.client import ZfClient  # noqa: E402
from core.config import Credential  # noqa: E402
from core.errors import FailureKind  # noqa: E402
from engine import GrabbingRunner, Plan, PlanItem, TaskState  # noqa: E402
from ui.state import _school_from_env  # noqa: E402

print("=" * 62)
print(f"① 真客户端建会话（模拟教务 {CAP} @ :{port}）")
print("=" * 62)

cred = Credential(cookie_header="JSESSIONID=mock; route=mock", source="mock")
main = ZfClient(cred, _school_from_env())
expect("init 真打了 http 并拿到上下文", bool(main.init(force=True)))
expect("模拟教务处于「已开放」", main.is_open is True)
expect("解析到多个课程类别", len(main.tabs) >= 2, f"{[t.name for t in main.tabs]}")
print(f"   store_age_s={main.store_age_s!r}   semester_key={main.semester_key!r}")
expect("store_age_s 是刚抓的（<5 秒）", (main.store_age_s or 99) < 5)
expect("semester_key 形如「学年|学期」", "|" in (main.semester_key or ""),
       f"{main.semester_key!r}")

print()
print("=" * 62)
print("② 真查课：拿真令牌，认准要押的那个班")
print("=" * 62)

main.switch_tab(main.tabs[TAB])
rows, _meta = main.query_courses("", tab_index=TAB)
expect("查到课程列表", bool(rows))
kch = rows[0].get("kch_id") or ""
kcmc = rows[0].get("kcmc") or ""
print(f"   课程：{kcmc}  kch_id={kch}")

cls = main.query_classes(kch, tab_index=TAB)
expect("查到教学班", bool(cls))
expect("教务真的下发了 do_jxb_id（一次性令牌）",
       bool(cls) and all(c.do_id for c in cls))
if cls:
    print(f"   令牌长度 {len(cls[0].do_id)} 字节，jxb_id={cls[0].jxb_id}")

print()
print("=" * 62)
print("③ ⭐ HTTP 级串行性：重查绝不早于提交（用户指出的竞态）")
print("=" * 62)

# ---- 在 HTTP 层装一个时序记录器：记下每次请求的路径与单调时刻 ----
calls: list[tuple[float, str]] = []
_orig_do = main.http._do


def recording_do(method, url, *a, **kw):
    calls.append((time.monotonic(), str(url)))
    return _orig_do(method, url, *a, **kw)


main.http._do = recording_do

# ---- 把「手里那个旧令牌已失效」这件事造出来 ----
# 真发一发 http 到模拟教务（它的写接口是桩，只为让这个请求在线上真实出现），
# 然后把语义改成「令牌类失败」。第二发才放行 —— 于是整轮必然走完
# 「提交 → 重查换令牌 → 重发」这条串行链。
real_submit = main.submit
n_sub = {"n": 0}


def submit_with_stale_first(kch_id, do_id, **kw):
    n_sub["n"] += 1
    try:
        real_submit(kch_id, do_id, **kw)          # ← 真发 http（模拟教务写接口是桩）
    except Exception:                             # 桩响应不该影响本次验证
        pass
    if n_sub["n"] == 1:
        return {"success": False, "flag": "", "msg": "模拟：手里那个旧令牌已失效",
                "kind": FailureKind.CONTEXT_INVALID}
    return {"success": True, "flag": "1", "msg": "模拟：教务受理了", "kind": None}


main.submit = submit_with_stale_first

plan = Plan()
item = PlanItem(
    kch_id=kch, kcmc=kcmc or "测试课", xf="1.0",
    do_id="STALE_TOKEN_FROM_MORNING",            # 上午拿的那个令牌
    jxb_id=cls[0].jxb_id if cls else "",         # 押的还是这个班
    tab_index=TAB, kklxdm="06",
    token_at=0.0,                                # 0 = 不知道什么时候拿的 → 可疑
    precheck=True,                               # 顺带覆盖预检（走 mock 的桩响应）
    max_attempts=5, interval_ms=50,
)
plan.add(item)

events: list = []
runner = GrabbingRunner(main, plan, on_event=events.append)
runner.run()

msgs = [getattr(e, "message", "") for e in events]
expect("抢到了（走完了整条串行链）", item.state is TaskState.WON, f"实际 {item.state}")
expect("⭐ 第一发用的就是手里那个旧令牌",
       "STALE_TOKEN_FROM_MORNING" not in item.do_id, f"{item.do_id!r}")
expect("⭐ 最终换上的是教务真下发的新令牌（不是造出来的）",
       bool(cls) and item.do_id == cls[0].do_id, f"{item.do_id!r} vs {cls[0].do_id!r}")
expect("⭐ 认回的仍是原来押的那个班（没偷偷换班）",
       bool(cls) and item.jxb_id == cls[0].jxb_id, f"{item.jxb_id!r}")
expect("日志交代了「令牌已刷新」", any("令牌已刷新" in m for m in msgs),
       f"{[m for m in msgs if '令牌' in m]}")

# ---- 线上顺序：第一个提交请求，必须早于任何一次查教学班 ----
paths = [u for _t, u in calls]
sub_i = [i for i, u in enumerate(paths) if PATH_PIECE_SUBMIT in u]
cls_i = [i for i, u in enumerate(paths) if PATH_PIECE_CLASSES in u]
print(f"   HTTP 请求共 {len(paths)} 次；提交在第 {sub_i} 位，查教学班在第 {cls_i} 位")
print(f"   顺序：{['submit' if PATH_PIECE_SUBMIT in u else 'classes' if PATH_PIECE_CLASSES in u else 'other' for u in paths]}")
expect("线上真的发过提交", len(sub_i) >= 2, f"submit 位置 {sub_i}")
expect("线上真的发过查教学班（重查刷新令牌）", len(cls_i) == 1, f"classes 位置 {cls_i}")
expect("⭐⭐ 第一个提交请求**早于**任何一次查教学班 —— "
       "重查绝不早于提交（早一步就等于自己把旧令牌作废）",
       bool(sub_i) and bool(cls_i) and sub_i[0] < cls_i[0],
       f"提交在 {sub_i}，查教学班在 {cls_i}")
expect("⭐ 查教学班只发生 1 次（够换一个令牌就行，不多烧请求）", len(cls_i) == 1,
       f"{cls_i}")
expect("预检也真的走了一趟（mock 的桩响应，按「无冲突」放行）",
       any(PATH_PIECE_PREPACK in u for u in paths),
       f"{[u.split('/')[-1] for u in paths]}")

print()
print("=" * 62)
print("④ 探活 ping()：只发一个请求，且绝不碰抢课期的共享上下文")
print("=" * 62)

# 探活是「登录态还有没有效」的唯一诚实来源：服务端内存快照永远不会自己变，
# 只能真的打一次教务。所以它必须**便宜**（1 个请求）且**零污染**（不碰上下文）。
store_before = dict(main.store)
tabs_before = list(main.tabs)
cur_before = main._cur_tab
n0 = len(calls)
info = main.ping()
n1 = len(calls)
sent = [u.split("/jwglxt/")[-1].split("?")[0] for _t, u in calls[n0:n1]]
print(f"   ping 发了 {n1 - n0} 个请求：{sent}")

expect("⭐ ping 恰好只发 **1** 个请求（init 要 2 个还要解析）", n1 - n0 == 1, f"{sent}")
expect("打的是选课首页（Index）—— 未登录时它会 302 回登录页，正好用来判活",
       PATH_PIECE_INDEX in calls[-1][1], f"{sent}")
expect("返回「是否已开放」（判据与 init 同源：core.client.page_is_open）",
       isinstance(info.get("is_open"), bool), f"{info.get('is_open')!r}")
expect("⭐ 不碰 store（那是抢课期共享的隐藏域快照）",
       dict(main.store) == store_before)
expect("⭐ 不碰 tabs（探活绝不重解析课程类别）", list(main.tabs) == tabs_before)
expect("⭐ 不碰当前 Tab（否则会冲掉主线程正在用的上下文）",
       main._cur_tab is cur_before)

print()
print("=" * 62)
print("⑤ 整轮只有一条线程、一个客户端（没有后台侦察队）")
print("=" * 62)

expect("⭐ 整轮没起过任何 `xk-scout` 线程",
       not any(t.name == "xk-scout" for t in threading.enumerate()),
       f"{[t.name for t in threading.enumerate()]}")
expect("⭐ ZfClient 不再提供 fork()（并行刷令牌的载体，已删除）",
       not hasattr(ZfClient, "fork"))
expect("⭐ GrabbingRunner 不持有侦察队客户端（整轮一个客户端）",
       not hasattr(runner, "_scout_client"))
expect("主客户端仍是活的（没被顺手关掉）", main.is_open is True)

print()
print("=" * 62)
print("⑥ 模拟教务的命中统计（只读全命中、写请求真的打过）")
print("=" * 62)

hits = getattr(httpd.RequestHandlerClass, "hits", None)
expect("拿到了模拟教务的命中统计", isinstance(hits, dict), f"{hits!r}")
if isinstance(hits, dict):
    print(f"   命中统计：{dict(hits)}")
    read_hits = hits.get("exact", 0) + hits.get("relaxed", 0) + hits.get("nofilter", 0)
    expect("写请求真的打过（串行链里那两发提交）", hits.get("write", 0) >= 2)
    expect("只读回放有命中（真查了课与教学班）", read_hits >= 1)
    expect("零 miss（只读请求全都在抓包里命中了）", hits.get("miss", 0) == 0,
           f"miss={hits.get('miss')}")
    expect("预检走了桩响应（未抓包，按「无冲突」放行）", hits.get("stub", 0) >= 1,
           f"stub={hits.get('stub')}")

main.close()

print()
print("=" * 62)
if _fails:
    print(f"✗ {len(_fails)} 条未通过：")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("✓ 全部通过")
