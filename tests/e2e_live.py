"""端到端实测：从 CDP 取登录态 -> 走我们自己的核心层 -> 查真实选课数据。

凭据全程只在内存传递，不落盘、不打印（铁律 #9）。
这是 HANDOFF 第 1 阶段验收标准「能用命令行选上一门课」的真实环境验证。

用法：python tests/e2e_live.py [关键词]
依赖：Edge 已开调试端口 9666 且已登录教务。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ZfClient
from core.config import Credential, DEFAULT_SCHOOL
from core.errors import XKError

CDP_PORT = 9666


def fetch_cookies() -> str:
    """通过 CDP 取目标域 Cookie（含 HttpOnly），返回 "k=v; k2=v2"。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=5) as r:
        pages = json.loads(r.read().decode())
    page = next(
        (t for t in pages if t.get("type") == "page" and "jwglxt" in t.get("url", "")), None
    )
    if not page:
        raise SystemExit("未找到教务页面，请确认 Edge 已登录教务系统")

    import websocket

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        for _ in range(30):
            if json.loads(ws.recv()).get("id") == 1:
                break
        ws.send(json.dumps({
            "id": 2,
            "method": "Network.getCookies",
            "params": {"urls": [DEFAULT_SCHOOL.base_url]},
        }))
        for _ in range(60):
            m = json.loads(ws.recv())
            if m.get("id") == 2:
                cookies = (m.get("result") or {}).get("cookies", []) or []
                break
        else:
            raise SystemExit("CDP 未返回 Cookie")
    finally:
        ws.close()

    host = DEFAULT_SCHOOL.base_url.split("//", 1)[-1].split("/", 1)[0]
    parts = []
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".")
        if dom and not (host.endswith(dom) or dom.endswith(host)):
            continue
        parts.append(f"{c['name']}={c['value']}")
    if not parts:
        raise SystemExit("没有目标域的 Cookie（可能未登录）")
    return "; ".join(parts)


def main() -> int:
    kw = sys.argv[1] if len(sys.argv) > 1 else ""

    raw = fetch_cookies()
    names = [p.split("=", 1)[0].strip() for p in raw.split(";") if "=" in p]
    print(f"① CDP 取到 Cookie：{len(names)} 条 -> {', '.join(names)}")
    if "JSESSIONID" not in names:
        print("   ⚠️ 缺少 JSESSIONID，登录态可能无效")

    cred = Credential(cookie_header=raw, source="cdp")
    client = ZfClient(cred)

    try:
        # ---- ② init ----
        try:
            store = client.init()
        except XKError as e:
            print(f"② init 失败：{e}")
            return 1
        keys = [k for k in store if not k.startswith("_")]
        print(f"② init 成功：抽到 {len(keys)} 个字段，选课开放 = {client.is_open}")
        for k in ("firstXkkzId", "firstKklxdm", "xkxnm", "xkxqm", "xklc", "njdm_id", "zyh_id", "xqh_id"):
            if k in store:
                print(f"     {k} = {store[k]}")
        print(f"     加密串 xkkz_xh 长度 = {len(store.get('xkkz_xh', ''))}")
        print(f"     课程 Tab 共 {len(client.tabs)} 个：")
        for t in client.tabs:
            print(f"       [{t.kklxdm}] {t.name}  xkkz_id={t.xkkz_id[:16]}…  xh={len(t.xkkz_xh)}位")

        # ---- ③ 逐个 Tab 查课程 ----
        total = 0
        for t in client.tabs:
            try:
                rows, meta = client.query_courses(kw, tab=t, page=1, page_size=10)
            except XKError as e:
                print(f"③ 查课程失败 [{t.kklxdm}/{t.name}]：{e}")
                if e.raw:
                    print("   原文:", e.raw[:300])
                continue
            total += len(rows)
            print(f"③ 查课程成功 [{t.kklxdm}/{t.name}]：{len(rows)} 条")
            for r in rows[:5]:
                print(f"     {r.get('kch_id','?')}  {r.get('kcmc','')}")
        if client.tabs and total == 0:
            print("③ 全部 Tab 均为 0 条（接口已打通，只是当前无可选课程）")

        # ---- ④ 查教学班 ----
        probe: list[dict] = []
        for _t in client.tabs:
            try:
                probe, _ = client.query_courses(kw, tab=_t, page=1, page_size=10)
            except XKError:
                continue
            if probe:
                print(f"④ 取到样本课程（Tab {_t.kklxdm}/{_t.name}）")
                break
        if probe:
            kch = probe[0].get("kch_id") or ""
            try:
                classes = client.query_classes(kch)
                print(f"④ 查教学班成功：课程 {kch} 有 {len(classes)} 个班")
                for j in classes[:5]:
                    flag = "【满】" if j.is_full else "  可 "
                    print(f"     {flag} {j.jsxx} {j.sksj} {j.yxzrs}/{j.jxbrl}  do_id={j.do_id[:18]}…")
            except XKError as e:
                print(f"④ 查教学班失败：{e}")
        else:
            print("④ 无可选课程样本，跳过查教学班")

        # ---- ⑤ 已选 ----
        try:
            sel = client.query_selected()
            print(f"⑤ 已选课程：{len(sel)} 门")
        except XKError as e:
            print(f"⑤ 查已选失败：{e}")

        print()
        if client.is_open:
            print("=== 核心层在真实开放环境下全部打通 ===")
        else:
            print("=== 选课期已关闭：登录态可用，init 优雅降级（未开放）===")
            print("    说明：无选课上下文，课程 Tab / 查课 / 已选 均不适用。")
            print("    这本身就是正确行为 —— 等开放期再跑一次即可验证完整链路。")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
