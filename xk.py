"""命令行入口（第一阶段验收工具）。

HANDOFF 第 1 阶段验收标准：「能用命令行选上一门课并打印耗时」。

用法：
    # 1) 探活：确认 Cookie 有效、打印 init 抽到的上下文
    python -m xk probe --cookie "JSESSIONID=xxx; route=yyy"

    # 2) 查课程类别 Tab / 查课程 / 查教学班（拿 do_id）
    python -m xk tabs    --cookie "..."
    python -m xk courses --cookie "..." --keyword 高等数学
    python -m xk classes --cookie "..." --kch 2024010001

    # 3) 校准与本教务服务器的时钟偏差（零副作用、零配额消耗）
    python -m xk clock --cookie "..."

    # 4) 选课（⚠️ 真实提交）—— 两种模式
    python -m xk grab --cookie "..." --kch 2024010001                    # 立即提交
    python -m xk grab --cookie "..." --kch 2024010001 --at 12:00:00       # 定时卡点开抢
    python -m xk grab --cookie "..." --kch A B C --at "2026-09-30 12:00:00"

    也可以用 CDP 自动抓 Cookie（需浏览器开调试端口）
    python -m xk probe --cdp-port 9666

凭据来源三选一：--cookie / --cdp-port / --user+--pass
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

# 打包成 exe 后 Windows 控制台默认 GBK，打印中文/emoji 会崩（同 serve.py 的处理）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core import ZfClient, get_credential
from core.errors import KIND_LABEL, FailureKind, XKError
from engine.plan import Plan, PlanItem, parse_when
from engine.runner import GrabbingRunner


def _setup_log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # 压掉 requests 的噪音
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _credential_from_args(a):
    try:
        if a.cookie:
            return get_credential(cookie=a.cookie)
        if a.cdp_port:
            return get_credential(cdp_port=a.cdp_port)
        if a.user and a.password:
            return get_credential(username=a.user, password=a.password)
    except Exception as e:
        print(f"取凭据失败：{e}", file=sys.stderr)
        sys.exit(2)
    print("错误：必须提供 --cookie / --cdp-port / --user+--pass 之一", file=sys.stderr)
    sys.exit(2)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_probe(client: ZfClient, a) -> int:
    """探活 + 打印 init 结果。"""
    t0 = time.monotonic()
    store = client.init()
    cost = int((time.monotonic() - t0) * 1000)

    print(f"init 耗时: {cost} ms")
    print(f"选课是否开放: {'是' if client.is_open else '否'}")

    print(f"\n课程类别 Tab 共 {len(client.tabs)} 个（每个 Tab 有独立加密串）：")
    for t in client.tabs:
        print(f"  [{t.kklxdm}] {t.name:24s} xkkz_id={t.xkkz_id[:16]}… 加密串={len(t.xkkz_xh)}位")

    # 展示关键上下文（隐去可能的敏感值）
    interesting = [
        k for k in store
        if not k.startswith("_") and k not in ("token", "csrftoken")
    ]
    print(f"\ninit 抽到 {len(interesting)} 个上下文字段：")
    for k in sorted(interesting):
        v = store[k]
        v_show = v if len(str(v)) <= 40 else str(v)[:40] + "…"
        print(f"  {k:24s} = {v_show}")
    return 0


def cmd_tabs(client: ZfClient, a) -> int:
    """列出全部课程类别 Tab。"""
    client.init()
    print(f"共 {len(client.tabs)} 个 Tab：")
    for t in client.tabs:
        print(f"  kklxdm={t.kklxdm:4s} {t.name:26s} njdm_id={t.njdm_id} zyh_id={t.zyh_id}")
        print(f"         xkkz_id = {t.xkkz_id}")
        print(f"         加密串   = {t.xkkz_xh[:48]}…（{len(t.xkkz_xh)}位）")
    return 0


def cmd_courses(client: ZfClient, a) -> int:
    """查课程列表。不指定 --tab 时遍历全部 Tab，并会逐 Tab 重载 Display 上下文。"""
    client.init()
    tabs = client.tabs
    wanted = [t for t in tabs if not a.tab or t.kklxdm == a.tab]
    if a.tab and not wanted:
        print(f"未找到 kklxdm={a.tab} 的 Tab", file=sys.stderr)
        return 1

    total = 0
    for t in wanted:
        rows, meta = client.query_courses(
            a.keyword, tab=t, page=a.page, page_size=a.size
        )
        total += len(rows)
        print(f"\n[{t.kklxdm}] {t.name} → {len(rows)} 条（rwlx={client.store.get('rwlx')}）")
        for r in rows[: a.size]:
            kch = r.get("kch") or r.get("kch_id") or "?"
            kcmc = r.get("kcmc") or ""
            jxbmc = r.get("jxbmc") or ""
            print(f"   {kch:14s} {kcmc:28s} {jxbmc}  已选 {r.get('yxzrs','?')}")
    print(f"\n合计 {total} 条")
    return 0


def cmd_classes(client: ZfClient, a) -> int:
    client.init()
    tab = client.find_tab(a.tab) if a.tab else None
    classes = client.query_classes(a.kch, tab=tab)
    print(f"课程 {a.kch} 有 {len(classes)} 个教学班：")
    for j in classes:
        flag = "【满】" if j.is_full else "    "
        print(f"  {flag} {j.jsxx}  {j.sksj}  {j.yxzrs}/{j.jxbrl}")
        print(f"        do_id = {j.do_id[:24]}…")
    return 0


def cmd_selected(client: ZfClient, a) -> int:
    rows = client.query_selected()
    print(f"已选 {len(rows)} 门：")
    for r in rows:
        print(f"  {r.get('kch_id','?')}  {r.get('kcmc','')}")
    return 0


def cmd_clock(client: ZfClient, a) -> int:
    """校准并打印与教务服务器的时钟偏差（零副作用、零配额消耗）。"""
    client.sync_clock(a.samples)
    c = client.clock
    print(f"采样 {c.samples} 次（最小 RTT {c.rtt * 1000:.0f} ms）")
    print(f"  本地时钟偏差 : {c.offset * 1000:+.0f} ms（服务器 - 本地）" if c.synced else "  未校准")
    if c.synced:
        lo, hi = c.window
        print(f"  偏差可行区间 : [{lo * 1000:+.0f}, {hi * 1000:+.0f}] ms")
        print(f"  不确定度     : ±{c.uncertainty * 1000:.0f} ms")
        print(f"  建议提前开火 : {c.lead_seconds() * 1000:.0f} ms（把误差推向「早到」这一侧）")
        print(f"  服务器当前时刻: {_fmt_ts(c.server_now())}")
    print()
    print("  ⚠️ HTTP Date 只到整秒，单次采样的约束区间就有 1+RTT 秒宽；")
    print("     别指望靠时钟精度翻盘 —— 真正管用的是把 T0 时的请求数压到 1 个。")
    return 0


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def cmd_grab(client: ZfClient, a) -> int:
    """选课（真实提交）。

    带 --at 时走「预热 → 倒计时 → 开火」的定时流程（引擎层的 GrabbingRunner）；
    不带则做一次直接提交，用于快速验证单次调用是否通。
    """
    # --kch 支持多值；单次提交路径沿用第一个
    a.kch_list = [str(x) for x in (a.kch if isinstance(a.kch, list) else [a.kch])]
    a.kch = a.kch_list[0]
    if a.at:
        return _grab_scheduled(client, a)
    return _grab_once(client, a)


def _grab_once(client: ZfClient, a) -> int:
    do_id = a.do_id
    if not do_id:
        client.init()
        tab = client.find_tab(a.tab) if a.tab else None
        classes = client.query_classes(a.kch, tab=tab)
        if not classes:
            print("未查到教学班，无法选课", file=sys.stderr)
            return 1
        do_id = classes[0].do_id
        print(f"自动选定教学班 do_id={do_id[:16]}…（{classes[0].sksj}）")

    t0 = time.monotonic()
    try:
        res = client.submit(a.kch, do_id, kcmc=a.kcmc)
    except XKError as e:
        cost = int((time.monotonic() - t0) * 1000)
        print(f"❌ 提交异常（{cost} ms）：{e}")
        if e.kind is FailureKind.UNKNOWN and e.raw:
            print("原始返回:", e.raw[:500])
        return 1
    cost = int((time.monotonic() - t0) * 1000)

    if res["success"]:
        print(f"✅ 选课成功（{cost} ms）flag={res['flag']}")
        return 0
    kind = res.get("kind")
    label = KIND_LABEL.get(kind, "未知") if kind else "未知"
    print(f"❌ 选课失败（{cost} ms）{label}")
    print(f"   flag={res['flag']}  msg={res['msg']}")
    return 1


# 事件 → 控制台一行（保持安静：只在关键节点说话）
_EVENT_MARK = {
    "clock": "⏱ ",
    "prewarm_start": "🔥 ",
    "prewarm_ready": "✅ ",
    "countdown": "⏳ ",
    "fire": "🚀 ",
    "attempt": "→  ",
    "success": "🎉 ",
    "give_up": "✋ ",
    "retry_wait": "…  ",
    "need_login": "🔑 ",
    "plan_done": "🏁 ",
    "log": "   ",
}


def _grab_scheduled(client: ZfClient, a) -> int:
    """定时开抢：预热 → 倒计时 → 卡点开火。"""
    # 先解析时刻（此时还没任何网络请求，报错也不用等）
    try:
        start_at = parse_when(a.at)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    now = time.time()
    print(f"目标开抢时刻：{_fmt_ts(start_at)}（{start_at - now:+.1f}s 后）")

    items = [
        PlanItem(
            kch_id=k,
            kcmc=a.kcmc,
            kklxdm=a.tab,
            do_id=a.do_id if len(a.kch_list) == 1 else "",
            max_attempts=a.attempts,
            interval_ms=a.interval,
            precheck=not a.no_precheck,
        )
        for k in a.kch_list
    ]
    plan = Plan(
        items=items,
        start_at=start_at,
        warmup_s=a.warmup,
        fire_lead_s=a.lead,
    )

    def on_event(ev) -> None:
        ts = time.strftime("%H:%M:%S", time.localtime())
        mark = _EVENT_MARK.get(ev.type.value, "   ")
        # attempt 事件太啰嗦，只留首尾；countdown 同理靠 message 自带秒数
        print(f"[{ts}] {mark}{ev.message}", flush=True)

    runner = GrabbingRunner(client, plan, on_event=on_event)
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.stop()
        print("\n已中断", file=sys.stderr)
        return 130

    won = len(plan.won)
    print()
    if won:
        print(f"✅ 抢到 {won}/{len(plan.items)} 门")
        return 0
    print(f"❌ 未抢到（共 {len(plan.items)} 项）")
    for it in plan.items:
        print(f"   {it.label}: {it.state.value}  尝试 {it.attempts} 次  {it.last_msg}")
    return 1


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xk", description="正方教务选课工具（核心层 CLI）")
    _add_cred_args(p)

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="探活并打印 init 上下文")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("tabs", help="列出全部课程类别 Tab（含各自加密串）")
    sp.set_defaults(func=cmd_tabs)

    sp = sub.add_parser("courses", help="查课程列表（默认遍历所有 Tab）")
    sp.add_argument("--keyword", "-k", default="", help="关键词")
    sp.add_argument("--tab", default="", help="只查指定 kklxdm（如 10=公共选修课）")
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--size", type=int, default=20)
    sp.set_defaults(func=cmd_courses)

    sp = sub.add_parser("classes", help="查某课程的教学班")
    sp.add_argument("--kch", required=True, help="课程号")
    sp.add_argument("--tab", default="", help="指定 kklxdm（默认沿用当前 Tab）")
    sp.set_defaults(func=cmd_classes)

    sp = sub.add_parser("selected", help="查已选课程")
    sp.set_defaults(func=cmd_selected)

    sp = sub.add_parser("clock", help="校准与教务服务器的时钟偏差（零副作用）")
    sp.add_argument("--samples", type=int, default=6, help="采样次数（默认 6）")
    sp.set_defaults(func=cmd_clock)

    sp = sub.add_parser("grab", help="选课（真实提交）；带 --at 则定时卡点开抢")
    sp.add_argument("--kch", required=True, nargs="+", help="课程号（可给多个）")
    sp.add_argument("--do-id", default="", help="教学班加密 id（不填则自动取）")
    sp.add_argument("--kcmc", default="", help="课程名（可选，仅用于日志）")
    sp.add_argument("--tab", default="", help="指定 kklxdm")
    g = sp.add_argument_group("定时开抢")
    g.add_argument("--at", default="", help='开抢时刻：12:00:00 / "2026-09-30 12:00:00" / +90')
    g.add_argument("--lead", type=float, default=None,
                   help="提前开火毫秒的秒数；默认按时钟不确定度自动定")
    g.add_argument("--warmup", type=float, default=90.0, help="提前多少秒进入预热（默认 90）")
    g = sp.add_argument_group("提交策略")
    g.add_argument("--attempts", type=int, default=50, help="每项最大尝试次数（默认 50）")
    g.add_argument("--interval", type=int, default=800, help="两次尝试间隔毫秒（默认 800）")
    g.add_argument("--no-precheck", action="store_true", help="跳过时间冲突预检")
    sp.set_defaults(func=cmd_grab)

    return p


def _add_cred_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("凭据")
    g.add_argument("--cookie", "-c", default="", help='形如 "JSESSIONID=xxx; route=yyy"')
    g.add_argument("--cdp-port", type=int, default=0, help="浏览器调试端口（自动抓 Cookie）")
    g.add_argument("--user", default="", help="学号（需配 --pass）")
    g.add_argument("--pass", dest="password", default="", help="密码（不推荐，可能需验证码）")
    g.add_argument("--verbose", "-v", action="store_true", help="打印调试日志")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_log(args.verbose)

    cred = _credential_from_args(args)
    client = ZfClient(cred)
    try:
        return args.func(client, args)
    except XKError as e:
        # 核心层语义异常：打印可读原因，不吐 traceback（铁律 #10）
        label = KIND_LABEL.get(e.kind, e.kind.value)
        print(f"\n❌ {label}", file=sys.stderr)
        print(f"   详情：{e}", file=sys.stderr)
        if e.kind is FailureKind.UNKNOWN and e.raw:
            print(f"   原始返回：{e.raw[:400]}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
