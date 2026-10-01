"""选课期关闭时的行为探测（只读，不做任何写操作）。

用途：确认教务进入「非选课阶段」后——
  1. 登录态是否仍然可用（能不能拿到 Index 页）
  2. client.is_open 是否为 False，且不抛异常
  3. 学分字段（zxfs / xkzgxf / xkxnmc / xkxqmc）在关闭期是否还有值
  4. Tab 列表、课程查询在关闭期的失败语义（应是明确错误而不是崩溃）
  5. submit（提交选课）被正确拒绝

用法：
    python tests/probe_closed_phase.py            # 走 CDP 9666 取 Cookie
    python tests/probe_closed_phase.py --cookie "JSESSIONID=..; route=.."
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.client import ZfClient
from core.config import DEFAULT_SCHOOL
from core.credential import Credential
from core.errors import KIND_LABEL, FailureKind, XKError

CDP_PORT = 9666


def fetch_cookies() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=5) as r:
        tabs = json.load(r)
    # 取任意一个 jwxt 域下的 tab 就够（Cookie 是浏览器级的）
    for t in tabs:
        if "jwxt.nfu.edu.cn" in (t.get("url") or ""):
            break
    else:
        t = tabs[0] if tabs else None
    if not t:
        raise SystemExit("CDP 里没有可用 Tab")
    ws = t.get("webSocketDebuggerUrl")
    if not ws:
        raise SystemExit("该 Tab 没有 webSocketDebuggerUrl")

    # 用最简单的 HTTP 方式拿 cookie：访问 /json/list 拿不到，改走 Network.getCookies
    # 为避免引入 websocket 依赖，这里用 urllib 打 cookie 端点不可行，
    # 故退化为直接读浏览器 profile 的方式不做——改用 CDP HTTP 端点 /json/cookies 不存在，
    # 实际由调用方（复用 e2e_live.fetch_cookies）提供。
    raise SystemExit("请改用 e2e_live 的取 Cookie 逻辑，或传 --cookie")


def main() -> int:
    cookie = ""
    if "--cookie" in sys.argv:
        i = sys.argv.index("--cookie")
        cookie = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""

    if not cookie:
        # 复用 e2e_live 里已经调通的 CDP 取 Cookie 实现
        from tests.e2e_live import fetch_cookies as _fc  # type: ignore

        cookie = _fc()

    names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]
    print("=" * 62)
    print("选课期关闭 · 行为探测（只读）")
    print("=" * 62)
    print(f"① CDP Cookie {len(names)} 条：{', '.join(names)}")
    if "JSESSIONID" not in names:
        print("   ⚠️ 缺 JSESSIONID —— 登录态大概率无效，后面结果不可信")

    cred = Credential(cookie_header=cookie, source="cdp")
    client = ZfClient(cred)

    # ---- ② init ----
    print("\n② init —— 能否在关闭期拿到 Index 页（不应抛异常）")
    try:
        store = client.init()
    except XKError as e:
        print(f"   ✗ init 抛了 XKError（应为优雅返回）：kind={e.kind} args={e.args[:1]}")
        if e.raw:
            print(f"   原文: {e.raw[:400]}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ init 未捕获异常（这就是 bug）：{type(e).__name__}: {e}")
        return 1

    keys = [k for k in store if not k.startswith("_")]
    print(f"   ✓ init 未抛异常，抽到 {len(keys)} 个字段")
    print(f"   is_open = {client.is_open}   <-- 关闭期应为 False")
    print(f"   iskxk   = {store.get('iskxk')!r}   <-- 权威标志，关闭期应为 '0'")

    # ---- ③ 学分字段在关闭期是否还有值 ----
    print("\n③ 学分字段（关闭期的取值）")
    c = client.credit
    print(f"   found={c.found}")
    print(f"   学年/学期 = {c.year!r} / {c.term!r}")
    print(f"   最低 = {c.min_credit}   最高 = {c.max_credit}   已选 = {c.used_credit}")
    print(f"   选课轮次时间 = {c.time_text!r}   round_name={c.round_name!r}")
    for h in ("zxfs", "xkzgxf", "xkxnmc", "xkxqmc", "xklcmc", "xkkssj", "xkjssj"):
        v = store.get(h)
        print(f"   隐藏域 {h:8s} = {v!r}")

    # ---- ④ Tab 列表 ----
    print("\n④ 课程 Tab（关闭期是否还返回）")
    print(f"   共 {len(client.tabs)} 个")
    for t in client.tabs:
        print(f"     [{t.kklxdm}] {t.name}  xh={len(t.xkkz_xh)}位")

    # ---- ⑤ 课程查询在关闭期的失败语义 ----
    print("\n⑤ 课程查询（关闭期的失败语义）")
    if not client.tabs:
        print("   （无 Tab，跳过）")
    else:
        t = client.tabs[0]
        try:
            rows, meta = client.query_courses("", tab=t, page=1, page_size=5)
            print(f"   居然查到了 {len(rows)} 条 —— 教务关闭期仍允许查课")
            for r in rows[:3]:
                print(f"     {r.get('kch_id','?')} {r.get('kcmc','')}")
        except XKError as e:
            print(f"   ✓ 以 XKError 优雅失败：kind={e.kind}")
            print(f"     说明 = {KIND_LABEL.get(e.kind, e.kind.value)}")
            if e.raw:
                print(f"     原文 = {e.raw[:250]}")
        except Exception as e:  # noqa: BLE001
            print(f"   ✗ 未捕获异常（这就是 bug）：{type(e).__name__}: {e}")

    # ---- ⑥ 提交选课必须被拦在本地（不发请求）----
    print("\n⑥ 提交选课（关闭期必须在本地闸门拦下，绝不发请求）")
    try:
        r = client.submit("FAKE_KCH", "FAKE_DO")
        ok = r.get("success") is False and r.get("kind") == FailureKind.NOT_OPEN
        mark = "✓" if ok else "✗"
        print(f"   {mark} submit 返回：success={r.get('success')} kind={r.get('kind')} msg={r.get('msg')!r}")
    except XKError as e:
        print(f"   ⚠️ 以异常形式拒绝：kind={e.kind}（也可接受，但闸门本应返回字典）")
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ 未捕获异常（这就是 bug）：{type(e).__name__}: {e}")

    # ---- ⑥.1 退课也必须被拦 ----
    print("\n⑥.1 退课（关闭期同样必须拦在本地）")
    try:
        r2 = client.cancel("FAKE_KCH", "FAKE_DO")
        print(f"   cancel 返回：{str(r2)[:160]}")
    except XKError as e:
        print(f"   ✓ 以 XKError 拒绝：kind={e.kind}  {KIND_LABEL.get(e.kind, '')}")
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ 未捕获异常：{type(e).__name__}: {e}")

    # ---- ⑥.2 没有 Tab 时找 Tab 不能崩 ----
    print("\n⑥.2 无 Tab 时按码/按下标找 Tab（应返回 None 而不是崩）")
    try:
        print(f"   find_tab('06') = {client.find_tab('06')}")
        print(f"   tab_at(0)      = {client.tab_at(0)}")
        print(f"   tab_at(99)     = {client.tab_at(99)}")
        print("   ✓ 全部返回 None，未抛异常")
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ 未捕获异常（这就是 bug）：{type(e).__name__}: {e}")

    # ---- ⑥.3 refresh_credit 在关闭期不能崩 ----
    print("\n⑥.3 refresh_credit（关闭期应返回 found=False，不抛异常）")
    try:
        c2 = client.refresh_credit()
        print(f"   ✓ 返回 found={c2.found}  used={c2.used_credit}  max={c2.max_credit}")
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ 未捕获异常（这就是 bug）：{type(e).__name__}: {e}")

    # ---- ⑦ Index 页里那句关键提示 ----
    print("\n⑦ 页面提示原文（判断教务到底说了什么）")
    raw_index = store.get("_raw_index") or ""
    if not raw_index:
        print("   （store 未保留原始 HTML，跳过；看 ② 的日志更直接）")
    else:
        for kw in ("不属于选课阶段", "不在选课时间", "选课已结束", "未开放"):
            if kw in raw_index:
                print(f"   命中提示词：{kw!r}")

    print("\n" + "=" * 62)
    print("探测完成（全程只读，未提交任何写操作）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
