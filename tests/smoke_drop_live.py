"""退课接口的联调用例（需要活会话）。

    XK_BASE=http://127.0.0.1:8721 python tests/smoke_drop_live.py

## 安全约定：本脚本**永远不真退课**

退课是本项目唯一不可逆的写操作，所以这里只跑「应当被拦下」的用例：

  1. 不存在的课程号       → 400，且已选门数不变
  2. 缺课程号             → 400
  3. 真实但教务不允许退的课 → 400，理由指名是哪一条不满足，**且已选门数不变**
     —— 这条最关键：它证明「预检零副作用」真的成立（铁律 #2），
        通不过资格检查时一个写请求都不会发出去。
  4. 事件日志里留有「退课被拦下（未发任何写请求）」的痕迹（铁律 #10）

如果发现确实有课处于「可退」状态，脚本只报告数量，**不会代你退**。

跑之前请确认打的是隔离实例（见 README/记忆：任何会写状态的用例都别打用户正在用的端口）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("XK_BASE", "http://127.0.0.1:8720").rstrip("/")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ok_count = 0
fail_count = 0


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"CONN_FAIL: {e}"


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"✓ {label} {detail}")
    else:
        fail_count += 1
        print(f"✗ {label} {detail}")


def detail_of(text: str) -> str:
    try:
        d = json.loads(text).get("detail")
    except Exception:  # noqa: BLE001
        return text[:160]
    if isinstance(d, dict):
        return str(d.get("detail") or d.get("message") or d)
    return str(d)


print(f"目标：{BASE}")
s, t = call("GET", "/api/state")
if s != 200:
    raise SystemExit(f"服务不可达（HTTP {s}）。先起 serve.py —— 注意用隔离端口。")
st = json.loads(t)
if not st.get("has_session"):
    raise SystemExit("当前没有活会话，本脚本需要活会话。请先建立会话再跑。")
print(f"会话：{st.get('source')} | 选课开放={st.get('is_open')} | 运行中={st.get('running')}")
print()

# -- 0. 取一份「退课前的已选」基线 -----------------------------------------
s, t = call("GET", "/api/selected")
if s != 200:
    raise SystemExit(f"读已选失败（HTTP {s}）：{detail_of(t)}")
sel = json.loads(t)
before = sel.get("count")
courses = sel.get("courses") or []
droppable = [c for c in courses if c.get("can_drop")]
blocked = [c for c in courses if not c.get("can_drop")]
print(f"已选 {before} 门：可退 {len(droppable)} 门 / 不可退 {len(blocked)} 门")
for c in courses[:3]:
    mark = "可退" if c.get("can_drop") else "已选"
    print(f"   · [{mark}] {c.get('kcmc')}（{c.get('kch_id')}）"
          f"{'' if c.get('can_drop') else ' ← ' + str(c.get('drop_block'))[:60]}")
print()

# -- 1. 不存在的课程号 -----------------------------------------------------
s, t = call("POST", "/api/drop", {"kch_id": "___NOT_A_REAL_KCH___"})
check("不存在的课程号 → 400 且说清「教务已选里没有」",
      s == 400 and "找不到" in detail_of(t) or (s == 400 and "没有" in detail_of(t)),
      f"(HTTP {s}, {detail_of(t)[:80]})")
s, t = call("GET", "/api/selected?cached=1")
check("这一路没有改动已选（预检零副作用）",
      s == 200 and json.loads(t).get("count") == before,
      f"(count 仍为 {json.loads(t).get('count') if s == 200 else '?'}，基线 {before})")

# -- 2. 缺课程号 -----------------------------------------------------------
s, t = call("POST", "/api/drop", {"kch_id": ""})
check("缺课程号 → 400", s == 400, f"(HTTP {s}, {detail_of(t)[:60]})")

# -- 3. 真实但不可退的课（核心用例）----------------------------------------
if blocked:
    victim = blocked[0]
    s, t = call("POST", "/api/drop", {"kch_id": victim["kch_id"],
                                      "jxbmc": victim.get("jxbmc") or ""})
    why = detail_of(t)
    check(f"教务不允许退的课（{victim.get('kcmc')}）→ 400 且指名原因",
          s == 400 and "不允许退" in why, f"(HTTP {s}, {why[:100]})")
    s, t = call("GET", "/api/selected")
    check("被拦下后已选门数一门没变（确认一个写请求都没发）",
          s == 200 and json.loads(t).get("count") == before,
          f"(count={json.loads(t).get('count') if s == 200 else '?'}，基线 {before})")

    # 事件日志必须能解释「为什么没退成」（铁律 #10）
    s, t = call("GET", "/api/events?since=0")
    msgs = [e.get("message", "") for e in (json.loads(t).get("events") or [])] if s == 200 else []
    hit = [m for m in msgs if "退课被拦下" in m]
    check("事件日志留下「退课被拦下（未发任何写请求）」",
          bool(hit), f"({hit[0][:90] if hit else '没找到'})")
else:
    print("… 当前没有「不可退」的课，跳过核心用例")

# -- 4. 可退的课：只报告，绝不代退 -----------------------------------------
if droppable:
    print()
    print(f"⚠️ 检测到 {len(droppable)} 门教务允许退的课：", ", ".join(c.get("kcmc", "") for c in droppable))
    print("   本脚本**不会**替你做退课（不可逆操作必须由你在界面上点、并二次确认）。")

print()
if fail_count:
    print(f"===== 通过 {ok_count} / 失败 {fail_count} =====")
    raise SystemExit(1)
print(f"===== 通过 {ok_count} / 失败 0 =====")
