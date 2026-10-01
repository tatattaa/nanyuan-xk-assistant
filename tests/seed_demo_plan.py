"""往运行中的界面灌一份「看起来像真的」课表，便于直观检查排版。

用途：选课期关闭时教务没有数据，课表是空的，看不出效果。
本脚本通过 /api/plan 写入若干课程（持久化在服务端，刷新页面不丢）。

⚠️ 会**覆盖**当前清单 —— 2026-09-30 起还会把落盘文件
   `<项目根>/state/plan.json` 一起覆盖掉（目标服务若是非 8720 端口，
   落盘在 `state/p<端口>/plan.json`，不会碰到你日常那份）。
   要看回真实数据：界面上点清空，或重启 serve.py。

   顺带说明：本脚本**不带 `base_version`**（乐观锁版本号），所以是「无条件覆盖」。
   这是刻意的 —— 一次性灌输脚本就是要盖掉现状。界面（app.js）则永远带版本号，
   对不上会拿到 409 而不是静默覆盖。

用法：
    python tests/seed_demo_plan.py                 # 默认打 8720
    python tests/seed_demo_plan.py --base http://127.0.0.1:8721
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8720"
if "--base" in sys.argv:
    BASE = sys.argv[sys.argv.index("--base") + 1]

WD = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


def slot(weekday: int, start: int, end: int, weeks: list[int]) -> dict:
    """按 Slot 的结构造一个时段。字段与后端 Slot.as_dict() 对齐。"""
    if len(weeks) == 1:
        wt = f"{weeks[0]}周"
    elif weeks == list(range(weeks[0], weeks[-1] + 1)):
        wt = f"{weeks[0]}-{weeks[-1]}周"
    else:
        wt = ",".join(str(w) for w in weeks) + "周"
    return {
        "weekday": weekday,
        "weekday_cn": WD[weekday],
        "start": start,
        "end": end,
        "weeks": weeks,
        "weeks_text": wt,
        "jie_text": f"{start}-{end}节",
        "text": f"{WD[weekday]} {start}-{end}节 · {wt}",
        "raw": f"星期{WD[weekday][1]}第{start}-{end}节{{{wt}}}",
    }


W1_16 = list(range(1, 17))
W1_8 = list(range(1, 9))
ODD = [w for w in range(1, 17) if w % 2 == 1]
EVEN = [w for w in range(1, 17) if w % 2 == 0]

# 一份典型的大数据管理与应用专业课表：周一到周五排满，含上午/下午/晚上、
# 单双周、连堂、以及一门跨午休的课（用来验证跨午休的连堂不错位）。
COURSES = [
    {
        "kch_id": "DEMO001", "kcmc": "数据挖掘", "jsxx": "张明", "xf": "3.0",
        "kklxdm": "01", "tab_index": 0,
        "slots": [slot(1, 1, 2, W1_16), slot(3, 3, 4, W1_16)],
    },
    {
        "kch_id": "DEMO002", "kcmc": "机器学习", "jsxx": "李伟", "xf": "4.0",
        "kklxdm": "01", "tab_index": 0,
        "slots": [slot(2, 1, 2, W1_16), slot(4, 1, 2, W1_16)],
    },
    {
        "kch_id": "DEMO003", "kcmc": "大数据技术原理", "jsxx": "王芳", "xf": "3.0",
        "kklxdm": "01", "tab_index": 0,
        "slots": [slot(1, 8, 9, W1_16), slot(3, 8, 9, W1_16)],
    },
    {
        "kch_id": "DEMO004", "kcmc": "社会网络分析", "jsxx": "陈静", "xf": "2.0",
        "kklxdm": "10", "tab_index": 3,
        "slots": [slot(2, 8, 9, ODD)],          # 单周
    },
    {
        "kch_id": "DEMO005", "kcmc": "数据库系统", "jsxx": "刘强", "xf": "3.5",
        "kklxdm": "01", "tab_index": 0,
        "slots": [slot(5, 3, 4, W1_16), slot(2, 10, 11, EVEN)],   # 双周
    },
    {
        "kch_id": "DEMO006", "kcmc": "Python 数据分析", "jsxx": "赵敏", "xf": "2.5",
        "kklxdm": "10", "tab_index": 3,
        "slots": [slot(4, 10, 11, W1_8)],        # 前 8 周（半学期）
    },
    {
        "kch_id": "DEMO007", "kcmc": "数据可视化", "jsxx": "孙涛", "xf": "2.0",
        "kklxdm": "01", "tab_index": 0,
        "slots": [slot(3, 12, 13, W1_16)],       # 晚上
    },
    {
        # 跨午休：5-8 节连堂（借这里的 rowspan=4 顺带目视验证缺号行没错位）
        "kch_id": "DEMO008", "kcmc": "专业综合实践", "jsxx": "周晓", "xf": "2.0",
        "kklxdm": "09", "tab_index": 5,
        "slots": [slot(5, 5, 8, W1_8)],
    },
    {
        # 晚上最后一堂：14-15 节（21:05 才下课）。用来把课表的行数推到最大，
        # 顺便验证 JIE_TIME 里最晚那两节的时间显示。
        "kch_id": "DEMO009", "kcmc": "学术前沿讲座", "jsxx": "郑凯", "xf": "1.0",
        "kklxdm": "09", "tab_index": 5,
        "slots": [slot(1, 14, 15, W1_16), slot(4, 14, 15, EVEN)],   # 周一全周 + 周四双周
    },
]


def main() -> int:
    st = json.load(urllib.request.urlopen(f"{BASE}/api/state", timeout=8))
    if not st.get("has_session"):
        print(f"✗ {BASE} 上没有会话。先 POST /api/session/cdp 建会话。")
        return 1

    items = []
    for i, c in enumerate(COURSES):
        items.append({
            "kch_id": c["kch_id"], "do_id": "", "jxb_id": "",
            "kklxdm": c["kklxdm"], "tab_index": c["tab_index"],
            "kcmc": c["kcmc"], "jsxx": c["jsxx"], "xf": c["xf"],
            "cxbj": "0", "fxbj": "0", "priority": i,
            # 与 engine/plan.py::MAX_ATTEMPTS 保持一致（演示数据要跟真实界面同款）
            "max_attempts": 20, "interval_ms": 800, "precheck": True,
            "slots": c["slots"],
        })

    body = json.dumps({"items": items}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/plan", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    r = json.load(urllib.request.urlopen(req, timeout=15))
    print(f"✓ 已写入 {r.get('count')} 门课（互斥项对：{r.get('mutex') or '无'}）")

    # 学分不在 /api/plan 的返回里，得回查 /api/state 的 credit.plan_credit
    st2 = json.load(urllib.request.urlopen(f"{BASE}/api/state", timeout=8))
    cred = st2.get("credit") or {}
    print(f"  待加选学分：{cred.get('plan_credit')}")
    print(f"  教务学分上限：{cred.get('max')}（未开放期读不到，正常）")
    for c in COURSES:
        s = " + ".join(x["text"] for x in c["slots"])
        print(f"  {c['kcmc']:22s} {c['xf']:>4s} 学分  {s}")
    print("\n打开界面即可看到课表。要清空：界面点「清空清单」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
