"""UI 层端到端自检：起服后直接打本地 HTTP 接口。

⚠️ 本套用例会**写状态**（POST /api/plan）且**末尾会 DELETE /api/session 清掉会话和清单**。
所以默认打**隔离端口 8721**，并且显式拒绝打 8720（那是用户正在操作的实例）。

⚠️ 2026-09-30 起抢课清单会**落盘**，所以「清掉清单」现在还会**删掉/覆盖磁盘上的清单文件**。
   serve.py 已按端口隔离落盘目录，照下面的方式起隔离实例就不会碰到 8720 那份：

    python serve.py --port 8721 &          # → 落盘到 <项目根>/state/p8721/plan.json
    python tests/smoke_ui.py               # 默认就是 8721
    XK_BASE=http://127.0.0.1:8722 python tests/smoke_ui.py

   想连落盘一起隔离到别处（或直接关掉）：先设 XK_STATE_DIR

    XK_STATE_DIR=/tmp/xk_test python serve.py --port 8721 &
    XK_STATE_DIR=off           python serve.py --port 8721 &   # 纯内存，不碰磁盘

真要在 8720 上跑（会清掉当前会话与**磁盘上的那份清单**！）得显式放行：

    XK_ALLOW_MAIN=1 python tests/smoke_ui.py

> 2026-09-29 踩过：直接跑这个脚本打了 8720，把用户手工攒的清单清空了，
> 而用户页面还开着、一操作就用空清单覆盖了后端。所以加了这道防呆。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("XK_BASE", "http://127.0.0.1:8721").rstrip("/")

# 防呆：绝不允许「忘了设 XK_BASE」就默认打到用户正在用的 8720。
if ":8720" in BASE and not os.environ.get("XK_ALLOW_MAIN"):
    print("拒绝执行：XK_BASE 指向 8720（用户正在操作的实例）。")
    print("  本脚本会 POST /api/plan，并在末尾 DELETE /api/session。")
    print("  2026-09-30 起清单会落盘 —— 打 8720 会连磁盘上的清单一起清掉。")
    print("  请先起隔离实例：  python serve.py --port 8721")
    print("  确实要在 8720 上跑（会清空会话与清单文件）：XK_ALLOW_MAIN=1 python tests/smoke_ui.py")
    sys.exit(2)

# 绕过系统代理（本机此前踩过 http_proxy 拦 localhost 的坑）
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 连通性预检：连不上就一句话说清，别让几十条用例逐条报 HTTP 0（看不出真正原因）
try:
    opener.open(urllib.request.Request(BASE + "/"), timeout=3).close()
except Exception as e:
    print(f"连不上 {BASE} —— {e}")
    print("  请先起隔离实例：  python serve.py --port 8721")
    sys.exit(2)


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"CONN_FAIL: {e}"


def show(label, status, text, limit=260):
    ok = "✓" if 200 <= status < 300 else "✗"
    print(f"{ok} {label}  HTTP {status}")
    body = text if len(text) <= limit else text[:limit] + "…"
    print("    " + body.replace("\n", "\n    "))
    print()


ok_count = 0
fail_count = 0
skip_count = 0


def skip(label, why=""):
    """本实例的前置条件不满足 → 记一条**显式跳过**，不计失败也不计通过。

    为什么要有它：本套用例刻意从「无会话」起步（开头就 DELETE /api/session），
    所以凡是要「活会话」才能跑的正向用例（探活 /api/session/check），
    在无会话实例上**必然**拿不到 200。以前这些用例被算成失败，于是每次
    换环境跑都要人工解释一遍「这几条不是真失败」—— 那种噪音会掩盖真问题。
    现在改成显式跳过：噪声没了，且理由写在输出里。
    需要真会话的探活类用例，正确做法是另起一个带活会话的实例跑
    （见 tests/smoke_drop_live.py 的写法）。
    """
    global skip_count
    skip_count += 1
    print(f"– {label}  【跳过】{why}")


def check(label, cond, detail=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"✓ {label} {detail}")
    else:
        fail_count += 1
        print(f"✗ {label} {detail}")


# 0. 前置清理：确保从「无会话」这个干净状态开始
#    （否则上一次手工调试留下的会话会污染第 4/5 步，导致假失败）
call("POST", "/api/stop")
call("DELETE", "/api/session")

# 1. 首页
s, t = call("GET", "/")
check("GET / 返回前端页面", s == 200 and "<title>南苑抢课助手</title>" in t, f"(HTTP {s}, {len(t)}B)")

# 2. 静态资源
s, t = call("GET", "/static/app.js")
check("GET /static/app.js", s == 200 and "function api" in t, f"(HTTP {s}, {len(t)}B)")
s, t = call("GET", "/static/style.css")
check("GET /static/style.css", s == 200 and "--accent" in t, f"(HTTP {s}, {len(t)}B)")

# 3. 目录穿越防护
s, t = call("GET", "/static/../../serve.py")
check("静态目录穿越被拦截", s == 404, f"(HTTP {s})")

# 4. 无会话时的状态
s, t = call("GET", "/api/state")
d = json.loads(t) if s == 200 else {}
check("GET /api/state 无会话", s == 200 and d.get("has_session") is False, f"(HTTP {s})")

# 5. 无会话时查课程 → 应 400
s, t = call("GET", "/api/courses")
check("无会话查课程被拒", s == 400, f"(HTTP {s})")

# 5.1 无会话时查课程类别 → 应 400
s, t = call("GET", "/api/tabs")
check("无会话查课程类别被拒", s == 400, f"(HTTP {s})")

# 5.2 无会话时打已选缓存 → 应 400（它属于「教务数据」，必须先有会话）
s, t = call("GET", "/api/selected?cached=1")
check("无会话读已选缓存被拒", s == 400, f"(HTTP {s})")

# 5.3 冲突判定是纯本地计算，无会话也应当能用（只是「对手方」为空）
s, t = call("POST", "/api/conflict", {"items": [{
    "key": "K1", "kch_id": "X1", "kcmc": "测试课",
    "slots": [{"weekday": 3, "start": 3, "end": 5, "weeks": [1, 2, 3]}],
}]})
d = json.loads(t) if t.strip().startswith("{") else {}
r1 = (d.get("results") or {}).get("K1") or {}
check("无会话也能做冲突判定（空对手方 → none）",
      s == 200 and d.get("known") is False and r1.get("level") == "none",
      f"(HTTP {s}, known={d.get('known')}, level={r1.get('level')})")
check("冲突判定回带解析后的时段（前端要用来画格子）",
      bool(r1.get("slots")) and r1["slots"][0]["weekday"] == 3,
      f"(slots={r1.get('slots')})")

# 5.35 退课：**无会话一律发不出去**（本项目最不可逆的写操作，宁可不给机会）
#      带活会话的正向/反向用例在 tests/smoke_drop_live.py 里单独跑。
s, t = call("POST", "/api/drop", {"kch_id": "1120"})
check("无会话退课被拒（退课是本项目唯一不可逆写操作）", s == 400, f"(HTTP {s})")

# 5.4 分层容错：
#     - 「形状对但数值非法」的时段 → 静默丢弃，请求照常成功
#     - 「根本不是 dict」的时段     → Pydantic 在边界就 422，响亮失败
#       （这是刻意的：那是调用方的编程错误，不该被默默吞掉）
s, t = call("POST", "/api/conflict", {"items": [{
    "key": "K2", "kch_id": "X2", "slots": [{"weekday": 99}, {}, {"weekday": 3, "start": 0, "end": 5}],
}]})
d = json.loads(t) if t.strip().startswith("{") else {}
r2 = (d.get("results") or {}).get("K2") or {}
check("非法数值的时段被丢弃且请求不挂",
      s == 200 and r2.get("slots") == [] and r2.get("level") == "none",
      f"(HTTP {s}, slots={r2.get('slots')})")

s, t = call("POST", "/api/conflict", {"items": [{"key": "K3", "kch_id": "X3", "slots": ["垃圾"]}]})
check("非 dict 的时段被边界校验挡下（422）", s == 422, f"(HTTP {s})")

# 6. 空清单启动 → 应 400
s, t = call("POST", "/api/start")
check("空清单启动被拒", s == 400, f"(HTTP {s})")

# 7. 空 Cookie → 应 400
s, t = call("POST", "/api/session", {"cookie": ""})
check("空 Cookie 被拒", s == 400, f"(HTTP {s})")

# 8. 假 Cookie → 打真网络，应 409 session_expired
print("… 正在用假 Cookie 打真实教务服务器（验证错误语义链路）")
s, t = call("POST", "/api/session", {"cookie": "JSESSIONID=fake_e2e; route=abc"})
d = json.loads(t) if t.strip().startswith("{") else {}
kind = (d.get("detail") or {}).get("kind")
check("假 Cookie → 409 + session_expired", s == 409 and kind == "session_expired",
      f"(HTTP {s}, kind={kind})")

# 9. 失败后会话应被自动清除
s, t = call("GET", "/api/state")
d = json.loads(t) if s == 200 else {}
check("失效后会话被自动清除", d.get("has_session") is False, f"(has_session={d.get('has_session')})")

# 10. 事件接口
s, t = call("GET", "/api/events?since=0")
check("GET /api/events", s == 200 and "events" in t, f"(HTTP {s})")

# 11. 计划设置（不启动，只验证写入）
s, t = call("POST", "/api/plan", {"items": [{"kch_id": "TEST001", "kcmc": "测试课程",
                                             "kklxdm": "10", "interval_ms": 900}]})
check("POST /api/plan 写入清单", s == 200 and '"count": 1' in t.replace(" ", " ") or (s == 200), f"(HTTP {s})")

s, t = call("GET", "/api/state")
d = json.loads(t) if s == 200 else {}
items = d.get("items") or []
check("清单在读回状态中可见", len(items) == 1 and items[0]["kch_id"] == "TEST001",
      f"(items={len(items)})")
# 回归：快照必须给全「前端重建清单」所需的字段。
# 曾因缺 kklxdm，前端刷新页面后清单一度变空（后台有计划、界面显示 0 项）。
need = {"kch_id", "kcmc", "jsxx", "do_id", "kklxdm", "cxbj", "fxbj",
        "priority", "interval_ms", "precheck", "state", "attempts",
        "max_attempts", "last_msg", "slots", "sksj"}
missing = need - set(items[0]) if items else need
check("快照含重建清单所需的全部字段", not missing, f"(缺 {sorted(missing)})")
check("kklxdm 被如实带出（决定能否重新解析教学班）",
      bool(items) and items[0].get("kklxdm") == "10", f"(kklxdm={items[0].get('kklxdm') if items else None})")
check("interval_ms 被如实带出（决定重试节奏）",
      bool(items) and items[0].get("interval_ms") == 900,
      f"(interval_ms={items[0].get('interval_ms') if items else None})")

# 12. 无会话时时钟接口 → 应 400
s, t = call("GET", "/api/clock")
check("无会话查时钟被拒", s == 400, f"(HTTP {s})")

# 13. 定时开抢：合法时刻应被接受并回显
s, t = call("POST", "/api/plan", {
    "items": [{"kch_id": "TEST001", "kcmc": "测试课程"}],
    "start_at": "+120",
})
d = json.loads(t) if t.strip().startswith("{") else {}
check("定时计划被接受且回显 start_at",
      s == 200 and d.get("scheduled") is True and d.get("start_at"),
      f"(HTTP {s}, start_at={d.get('start_at')})")

s, t = call("GET", "/api/state")
d = json.loads(t) if s == 200 else {}
sch = d.get("schedule") or {}
check("状态快照带出定时信息", sch.get("start_at") is not None and sch.get("warmup_s") is not None,
      f"(schedule={sch})")
check("状态快照带出时钟字段（未校准也不许缺键）", "clock" in d, f"(keys={sorted(d)[:6]}…)")

# 14. 非法时刻必须被挡在「开始」之前 → 400，而不是等到开抢才炸
for bad, desc in [("昨天", "乱写"), ("25:99", "非法时间"), ("", "空串视作立即")]:
    s, t = call("POST", "/api/plan", {
        "items": [{"kch_id": "TEST001"}], "start_at": bad,
    })
    if bad == "":
        d = json.loads(t) if t.strip().startswith("{") else {}
        check(f"空 start_at 视作立即模式（{desc}）",
              s == 200 and d.get("scheduled") is False, f"(HTTP {s})")
    else:
        check(f"非法时刻被拒：{desc}", s == 400, f"(HTTP {s})")

# 15. 清单项携带时段：往返必须无损
#     （否则刷新页面后，课表上「待选」那一层会消失）
s, t = call("POST", "/api/plan", {"items": [{
    "kch_id": "TEST002", "kcmc": "带时段的课", "kklxdm": "06",
    "slots": [{"weekday": 1, "start": 1, "end": 2, "weeks": [1, 2, 3],
               "raw": "星期一第1-2节{1-3周}"}],
}]})
check("POST /api/plan 接受 slots", s == 200, f"(HTTP {s})")

s, t = call("GET", "/api/state")
d = json.loads(t) if s == 200 else {}
_sl = ((d.get("items") or [{}])[0].get("slots") or [])
check("清单时段往返无损",
      len(_sl) == 1 and _sl[0]["weekday"] == 1 and _sl[0]["weeks"] == [1, 2, 3]
      and _sl[0]["raw"] == "星期一第1-2节{1-3周}",
      f"(slots={_sl})")
check("清单时段带可读文本", bool(_sl) and _sl[0].get("text") == "周一 1-2节 · 1-3周",
      f"(text={_sl[0].get('text') if _sl else None})")

# 16. 状态快照必须能告诉前端「已选有没有缓存」（决定要不要去拉一次）
check("快照含 selected 缓存标记",
      isinstance(d.get("selected"), dict) and "loaded" in d["selected"],
      f"(selected={d.get('selected')})")

# 17. 清单互斥：**星期 + 节次有交集 + 周次有交集** 的两项要能被告知
#     「只能上成其中一门」（界面据此标注抢课顺序；执行期按同一份 Plan 跳过）
#     第 4 项与第 1 项同天同节次但周次完全错开 → 永远不撞，**不该**算互斥
s, t = call("POST", "/api/plan", {"items": [
    {"kch_id": "M1", "kcmc": "甲", "slots": [{"weekday": 2, "start": 3, "end": 5,
                                             "weeks": list(range(1, 18)),
                                             "raw": "星期二第3-5节{1-17周}"}]},
    {"kch_id": "M2", "kcmc": "乙", "slots": [{"weekday": 2, "start": 4, "end": 5,
                                             "weeks": [9],
                                             "raw": "星期二第4-5节{9周}"}]},
    {"kch_id": "M3", "kcmc": "丙", "slots": [{"weekday": 4, "start": 1, "end": 2,
                                             "weeks": [1], "raw": "星期四第1-2节{1周}"}]},
    {"kch_id": "M4", "kcmc": "丁（同节次但周次错开）",
     "slots": [{"weekday": 2, "start": 3, "end": 5, "weeks": [18, 19, 20],
                "raw": "星期二第3-5节{18-20周}"}]},
]})
d2 = json.loads(t) if s == 200 else {}
check("POST /api/plan 回传互斥项对（周次错开的同节次项不算）",
      d2.get("mutex") == [[0, 1]], f"(mutex={d2.get('mutex')})")

s, t = call("GET", "/api/state")
d3 = json.loads(t) if s == 200 else {}
check("快照带出互斥项对（刷新页面后仍在）",
      d3.get("mutex") == [[0, 1]], f"(mutex={d3.get('mutex')})")

# 17b. 冲突来源标记：`vs`（真撞）与 `vs_soft`（只占位重叠、周次错开）
#      界面靠这两者分档：与已选真撞→红、与清单内真撞→互斥、其余不报警
s, t = call("POST", "/api/conflict", {"items": [
    {"key": "C1", "kch_id": "C1", "slots": [{"weekday": 2, "start": 3, "end": 5,
                                            "weeks": [9], "raw": "星期二第3-5节{9周}"}]},
    # 21/22 周与清单里 M1{1-17} / M2{9} / M4{18-20} **全都不重叠** → 只能算 soft
    {"key": "C2", "kch_id": "C2", "slots": [{"weekday": 2, "start": 3, "end": 5,
                                            "weeks": [21, 22], "raw": "星期二第3-5节{21-22周}"}]},
]})
d5 = json.loads(t) if t.strip().startswith("{") else {}
rc = (d5.get("results") or {})
r1, r2 = rc.get("C1") or {}, rc.get("C2") or {}
check("真撞（周次重叠）计入 vs、来源是清单内",
      r1.get("level") == "hard" and "pending" in (r1.get("vs") or []),
      f"(C1={r1.get('level')}/{r1.get('vs')}/{r1.get('vs_soft')})")
check("周次全错开的同节次只计入 vs_soft、不进 vs（界面不该报警）",
      r2.get("level") == "soft" and r2.get("vs") == [] and r2.get("vs_soft") == ["pending"],
      f"(C2={r2.get('level')}/{r2.get('vs')}/{r2.get('vs_soft')})")

# 18. 无时段（教学班未解析）的项不参与互斥判定，避免凭缺失信息乱跳过
s, t = call("POST", "/api/plan", {"items": [
    {"kch_id": "N1", "kcmc": "有时段", "slots": [{"weekday": 3, "start": 1, "end": 2,
                                                 "weeks": [1], "raw": "星期三第1-2节{1周}"}]},
    {"kch_id": "N2", "kcmc": "无时段"},
]})
d4 = json.loads(t) if s == 200 else {}
check("无时段项不参与互斥", d4.get("mutex") == [], f"(mutex={d4.get('mutex')})")

# 19. 学分：待加选学分来自清单里的 xf，断会话也照样算得出来
s, t = call("POST", "/api/plan", {"items": [
    {"kch_id": "C1", "kcmc": "二学分课", "xf": "2.0"},
    {"kch_id": "C2", "kcmc": "一学分课", "xf": "1.0"},
]})
s, t = call("GET", "/api/credit")
d5 = json.loads(t) if s == 200 else {}
check("无会话也能取学分（待加选来自本地清单）", s == 200, f"(HTTP {s})")
check("待加选学分 = 3.0", d5.get("plan_credit") == 3.0, f"(plan_credit={d5.get('plan_credit')})")
check("无会话时教务那几项是 None 而不是 0（不拿 0 冒充）",
      d5.get("used") is None and d5.get("max") is None and d5.get("remain") is None,
      f"(used={d5.get('used')} max={d5.get('max')})")
check("无会话时不报超额（没有对照基准就别下结论）", d5.get("over") is False)

# refresh=1 必须要有会话 —— 它要真去打教务的选课首页
s, t = call("GET", "/api/credit?refresh=1")
check("无会话时 refresh=1 → 400（不允许空打教务）", s == 400, f"(HTTP {s} {t[:60]})")

# 清单里的 xf 空/非法 → 按 0 计，不能把整条搞崩
call("POST", "/api/plan", {"items": [
    {"kch_id": "C3", "xf": ""}, {"kch_id": "C4", "xf": "乱写"}, {"kch_id": "C5", "xf": "2.5"},
]})
s, t = call("GET", "/api/credit")
check("空/非法学分按 0 计（合计 2.5）",
      json.loads(t).get("plan_credit") == 2.5, f"(t={t[:80]})")

# 12. 清单乐观锁（真实 HTTP）：陈旧副本不许覆盖服务端清单
#     这是「前端拿陈旧本地副本把用户手工攒的清单整个盖掉」那个 bug 的回归守卫。
s, t = call("GET", "/api/state")
d = json.loads(t) if s == 200 else {}
rev0 = d.get("plan_rev")
check("快照下发 plan_rev（乐观锁版本号）", isinstance(rev0, int), f"(plan_rev={rev0!r})")

s, t = call("POST", "/api/plan", {"items": [{"kch_id": "LOCK_A", "kcmc": "第一版"}]})
d = json.loads(t) if s == 200 else {}
rev1 = d.get("plan_rev")
check("不带 base_version → 200 且 lock=forced（脚本兼容）",
      s == 200 and d.get("lock") == "forced", f"(HTTP {s} lock={d.get('lock')!r})")

s, t = call("POST", "/api/plan", {"base_version": rev1,
                                  "items": [{"kch_id": "LOCK_B", "kcmc": "第二版"}]})
d = json.loads(t) if s == 200 else {}
rev2 = d.get("plan_rev")
check("带正确 base_version → 200 且 lock=checked 且版本号递增",
      s == 200 and d.get("lock") == "checked" and rev2 == (rev1 + 1),
      f"(HTTP {s} rev {rev1} → {rev2})")

s, t = call("POST", "/api/plan", {"base_version": rev1,   # ← 已过期
                                  "items": [{"kch_id": "LOCK_STALE", "kcmc": "陈旧副本"}]})
d = json.loads(t) if s else {}
detail = d.get("detail") if isinstance(d.get("detail"), dict) else {}
check("⭐ 带陈旧 base_version → 409 且 kind=plan_conflict",
      s == 409 and detail.get("kind") == "plan_conflict",
      f"(HTTP {s} {t[:110]})")

s, t = call("GET", "/api/state")
d = json.loads(t) if s == 200 else {}
keys = [i["kch_id"] for i in (d.get("items") or [])]
check("⭐⭐ 409 时一个字节都没写（清单与版本号都没动）",
      keys == ["LOCK_B"] and d.get("plan_rev") == rev2,
      f"(items={keys} rev={d.get('plan_rev')} vs {rev2})")

s, t = call("POST", "/api/plan", {"base_version": d.get("plan_rev"),
                                  "items": [{"kch_id": "LOCK_C", "kcmc": "重载后重做"}]})
check("重载后用最新版本号重做 → 成功", s == 200, f"(HTTP {s})")

# 前端契约：那两个 POST /api/plan 的调用点都必须带 base_version。
# 服务端这道锁只有在前端真的把版本号带回来时才起作用 —— 漏一个调用点，
# 那一条路径（加课/删课 vs 点「开始」）就又变成「静默覆盖」了。
s, t = call("GET", "/static/app.js")
check("前端 app.js 真的把 base_version 带上了（乐观锁不能只做一半）",
      s == 200 and "base_version" in t and "planBody" in t, f"(HTTP {s})")

# 13. 登录态状态（真实 HTTP）：状态栏不许拿「内存里有凭据」冒充「登录还有效」
#     用户 2026-09-30 报的 bug：在教务网站点了退出登录，顶部却一直显示「已登录」。
#
# ⚠️ 这一组**需要活会话**才能跑：本套用例开头就 DELETE 掉了会话（那是它自己的
#    设计），所以「探活成功 → session_state=ok」在这里天然跑不了 —— 除非实例
#    恰好带着一个活会话（例如直接打 8720，或像 8722 那样手动注入过登录态）。
#    无会话时**显式跳过**，不再伪报失败（见 skip() 的说明）。
s, t = call("GET", "/api/state")
d = json.loads(t) if s == 200 else {}
check("快照下发 session_state（诚实状态，而不是只有 has_session）",
      d.get("session_state") in ("none", "expired", "ok", "unverified"),
      f"(session_state={d.get('session_state')!r})")

if d.get("has_session"):
    s, t = call("GET", "/api/session/check")
    d = json.loads(t) if s == 200 else {}
    check("GET /api/session/check 探活成功 → 200 且状态 ok",
          s == 200 and d.get("session_state") == "ok", f"(HTTP {s} {t[:80]})")
    check("探活带回「上次核实时刻」", bool(d.get("checked_at")), f"(checked_at={d.get('checked_at')!r})")

    s, t = call("GET", "/api/state")
    d = json.loads(t) if s == 200 else {}
    check("探活之后 /api/state 也反映出已核实",
          d.get("session_state") == "ok" and bool(d.get("session_checked_at")),
          f"(state={d.get('session_state')!r} checked_at={d.get('session_checked_at')!r})")
else:
    skip("GET /api/session/check 探活成功 → 200 且状态 ok",
         "本实例此刻无会话（本套用例开头自己清掉的）")
    skip("探活带回「上次核实时刻」", "同上：无会话就无从探活")
    skip("探活之后 /api/state 也反映出已核实", "同上：无会话就无从探活")

# 收尾：恢复成立即模式的空清单，别给后续调试留定时任务
call("POST", "/api/plan", {"items": []})
call("POST", "/api/stop")
call("DELETE", "/api/session")

print()
print(f"===== 通过 {ok_count} / 失败 {fail_count}"
      + (f" / 跳过 {skip_count}（前置不满足，见上文理由）" if skip_count else "")
      + " =====")
sys.exit(1 if fail_count else 0)
