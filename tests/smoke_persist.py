"""离线自检：课程类别缓存 + 抢课清单落盘（2026-09-30 新增能力）。

两件事分头钉死，全程**不发一个真实请求**：

A. 课程类别（Tab）缓存
   未开放期的 Index 页一个 Tab 都没有。旧行为是「解析到什么就是什么」，
   于是每次刷新（脚本每次 force init）界面上的课程类别都会被清空。
   新行为：解析到内容才替换，解析为空则沿用缓存 —— 但缓存**只准展示**，
   绝不许拿去发请求。

B. 抢课清单落盘（单槽）
   「每次有新清单就把上一次的清掉」＝ 全项目只有一个 plan.json、一次
   `os.replace` 原子覆盖。恢复时只恢复「意图」，运行状态一律重置。

    python tests/smoke_persist.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

# ⚠️ 必须在 import ui.state **之前**把落盘目录指到临时目录：
#    ui.state 在 import 时就把 STATE_DIR 定下来并尝试读回清单。
_TMP = tempfile.mkdtemp(prefix="xk_persist_")
os.environ["XK_STATE_DIR"] = _TMP

import ui.state as S  # noqa: E402
from core import Credential, FailureKind, XKError, ZfClient  # noqa: E402
from core.clock import ServerClock  # noqa: E402
from core.http import RawResponse  # noqa: E402
from engine.events import TaskState  # noqa: E402
from engine.plan import Plan, PlanItem  # noqa: E402

_fails = []


def expect(label, cond, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        _fails.append(label)
        print(f"  FAIL {label} {detail}")


# ---------------------------------------------------------------------------
# 假 HTTP：只认「Index 页 / Display 页」两种响应，并数清发了几次请求
# ---------------------------------------------------------------------------

OPEN_INDEX = (
    '<input type="hidden" name="iskxk" id="iskxk" value="1"/>'
    '<input type="hidden" name="xkkz_id" id="xkkz_id" value="ROUND1"/>'
    '<input type="hidden" name="xkxnm" id="xkxnm" value="2026"/>'
    '<input type="hidden" name="xkxqm" id="xkxqm" value="1"/>'
    '<input type="hidden" name="xkxnmc" id="xkxnmc" value="2026-2027"/>'
    '<input type="hidden" name="xkxqmc" id="xkxqmc" value="1"/>'
    '<input type="hidden" name="zxfs" id="zxfs" value="28"/>'
    '<input type="hidden" name="xkzgxf" id="xkzgxf" value="30"/>'
    '<a onclick="queryCourse(this,\'01\',\'K1\',\'2024\',\'117\',\'ENC_MAJOR\')">主修课程</a>'
    '<a onclick="queryCourse(this,\'06\',\'K6\',\'2024\',\'117\',\'ENC_PE\')">板块课(大学体育)</a>'
)

# 关闭期：只剩通用隐藏域（xkkz_id / xkxnm 等整个消失），且 iskxk=0
CLOSED_INDEX = (
    '<div class="nodata"><span>对不起，当前不属于选课阶段，如有需要，请与管理员联系！</span></div>'
    '<input type="hidden" name="iskxk" id="iskxk" value="0"/>'
    '<input type="hidden" name="csrftoken" id="csrftoken" value="ABC"/>'
)


class FakeHttp:
    """最小假 HTTP。`calls` 用来证明「某些路径一个请求都没发」。"""

    def __init__(self, index_html: str):
        self.index_html = index_html
        self.display_html = "<html><body>display</body></html>"
        self.calls: list[tuple[str, str]] = []
        self.clock = ServerClock()

    def get(self, url, **_kw):
        self.calls.append(("GET", url))
        return RawResponse(status=200, text=self.index_html, url=url)

    def post(self, url, _data=None, **_kw):
        self.calls.append(("POST", url))
        return RawResponse(status=200, text=self.display_html, url=url)

    def close(self):
        pass

    def sync_clock(self, *_a, **_k):  # pragma: no cover - 本用例不校时
        return self.clock


def make_client(index_html: str) -> tuple[ZfClient, FakeHttp]:
    c = ZfClient(Credential(cookie_header="JSESSIONID=x; route=y", source="test"))
    fh = FakeHttp(index_html)
    c.http = fh  # 项目既有做法：直接替换 http 会话
    return c, fh


print("=" * 62)
print("A. 课程类别（Tab）缓存")
print("=" * 62)

client, http = make_client(OPEN_INDEX)
store_open = client.init()
expect("开放期：解析到 2 个课程类别", len(client.tabs) == 2, f"实际 {len(client.tabs)}")
expect("开放期：is_open=True", client.is_open is True)
expect("开放期：tabs_stale=False（这批是刚抓的）", client.tabs_stale is False)
expect("开放期：学期键 = 2026-2027|1", client.semester_key == "2026-2027|1",
       f"实际 {client.semester_key!r}")
n_after_open = len(http.calls)
names_open = [t.name for t in client.tabs]

# --- 切到「未开放期」，force init（模拟脚本在开放前反复重抓）-----------------
http.index_html = CLOSED_INDEX
client.init(force=True)
expect("未开放期：is_open 变 False", client.is_open is False)
expect("⭐ 未开放期：课程类别**保留**（不再被清空）", len(client.tabs) == 2,
       f"实际 {len(client.tabs)}")
expect("⭐ 未开放期：同一批类别（名字未变）", [t.name for t in client.tabs] == names_open)
expect("未开放期：tabs_stale=True（这批是缓存，只能展示）", client.tabs_stale is True)
expect("未开放期：cache 仍带着原加密串（可展示、不可发请求）",
       client.tabs[0].xkkz_xh == "ENC_MAJOR")
expect("未开放期：store 里的学期字段仍在（update 是累加语义）",
       store_open is client.store and client.store.get("xkxnm") == "2026")

# --- 关键：未开放期不许发查询请求 -------------------------------------------
http.calls.clear()
err = None
try:
    client.query_courses("体育")
except XKError as e:
    err = e
expect("未开放期查课程 → NOT_OPEN", err is not None and err.kind is FailureKind.NOT_OPEN,
       f"实际 {getattr(err, 'kind', None)}")
expect("⭐ 未开放期查课程发出的请求数 = 0（缓存的 Tab 没被拿去用）", len(http.calls) == 0,
       f"实际 {http.calls}")

http.calls.clear()
err = None
try:
    client.query_classes("K1", tab_index=0)
except XKError as e:
    err = e
expect("未开放期查教学班 → NOT_OPEN", err is not None and err.kind is FailureKind.NOT_OPEN,
       f"实际 {getattr(err, 'kind', None)}")
expect("⭐ 未开放期查教学班发出的请求数 = 0", len(http.calls) == 0, f"实际 {http.calls}")

http.calls.clear()
res = client.submit("K1", "TOKEN")
expect("未开放期提交 → success=False / NOT_OPEN",
       res.get("success") is False and res.get("kind") is FailureKind.NOT_OPEN)
expect("未开放期提交发出的请求数 = 0", len(http.calls) == 0, f"实际 {http.calls}")

# --- 再刷一次（模拟预热相每轮重抓）仍然保留 ---------------------------------
client.init(force=True)
expect("未开放期连刷两次，类别依然在", len(client.tabs) == 2)

# --- 恢复开放：Tab 被重新抓取（缓存被替换掉）--------------------------------
http.index_html = OPEN_INDEX.replace("ENC_MAJOR", "ENC_MAJOR_NEW").replace("主修课程", "主修课程V2")
client.init(force=True)
expect("重新开放：is_open 变 True", client.is_open is True)
expect("重新开放：tabs_stale 归 False", client.tabs_stale is False)
expect("重新开放：缓存被新抓的覆盖（加密串已更新）",
       client.tabs[0].xkkz_xh == "ENC_MAJOR_NEW", f"实际 {client.tabs[0].xkkz_xh}")
expect("重新开放：类别名称同步更新", "主修课程V2" in [t.name for t in client.tabs])
expect("重新开放：tabs_at 有值（能报「抓取于何时」）", client.tabs_at > 0)

# --- 跨学期：缓存的 Tab 必须作废（加密串与学期轮次绑定）----------------------
old_client, old_http = make_client(OPEN_INDEX)
old_client.init()
expect("跨学期前：有 2 个类别", len(old_client.tabs) == 2)
old_http.index_html = (
    '<input type="hidden" name="iskxk" id="iskxk" value="0"/>'
    '<input type="hidden" name="xkxnmc" id="xkxnmc" value="2027-2028"/>'
    '<input type="hidden" name="xkxqmc" id="xkxqmc" value="1"/>'
)
old_client.init(force=True)
expect("⭐ 学期一变：缓存的上学期类别被丢弃", len(old_client.tabs) == 0,
       f"实际 {len(old_client.tabs)}")

# --- 缓存只用于展示：源码层钉死「switch_tab 只认本次解析到的 Tab」-----------
_src = (Path(__file__).resolve().parents[1] / "core" / "client.py").read_text(encoding="utf-8")
expect("init 的开放判定只认本次解析到的 Tab（不用 self.tabs）",
       "if not new_tabs:" in _src and "self.switch_tab(new_tabs[0])" in _src)
expect("query_courses / query_classes 都有无条件的 is_open 闸门",
       _src.count("if not self.is_open:\n            raise XKError") >= 2)

print()
print("=" * 62)
print("B. 抢课清单落盘（单槽覆盖）")
print("=" * 62)

expect("落盘目录取到了临时目录（测试不碰真实清单）",
       S.STATE_DIR is not None and str(S.STATE_DIR) == _TMP, f"实际 {S.STATE_DIR}")
plan_file = S.plan_path()

# 把源文件里的常量钉住：单槽 = 只有一个文件名
_state_src = (Path(__file__).resolve().parents[1] / "ui" / "state.py").read_text(encoding="utf-8")
expect("清单文件名是常量 plan.json（不按时间戳/哈希分文件 → 不会堆积历史）",
       '_PLAN_FILE = "plan.json"' in _state_src)
expect("落盘用 os.replace 原子覆盖（不会留下半截 JSON）", "os.replace(tmp, p)" in _state_src)
expect("空清单 = 删文件", "p.unlink()" in _state_src)

rt = S.Runtime()
expect("全新启动：没有清单", rt.plan is None and rt.plan_meta == {})

p1 = Plan()
p1.add(PlanItem(kch_id="K1", kcmc="高等数学", xf="4.0"))
rt.set_plan(p1)
expect("写入第 1 份 → 文件出现", plan_file.exists())
expect("写入第 1 份 → meta 有项数与路径",
       rt.plan_meta.get("count") == 1 and rt.plan_meta.get("path") == str(plan_file))

p2 = Plan()
p2.add(PlanItem(kch_id="K2", kcmc="线性代数", xf="3.0"))
p2.add(PlanItem(kch_id="K3", kcmc="大学英语", xf="2.0"))
rt.set_plan(p2)
files = sorted(f.name for f in S.STATE_DIR.iterdir())
expect("⭐ 写入第 2 份 → 目录里仍然只有 plan.json（上一次的清单已清掉）",
       files == ["plan.json"], f"实际 {files}")
on_disk = json.loads(plan_file.read_text(encoding="utf-8"))
expect("⭐ 文件里只剩第 2 份的内容", [i["kcmc"] for i in on_disk["items"]] == ["线性代数", "大学英语"],
       f"实际 {[i['kcmc'] for i in on_disk['items']]}")
expect("落盘带版本号与存入时刻", on_disk.get("version") == 1 and on_disk.get("saved_at"))

# 往清单里塞一些「跑过一轮才有」的运行结果：加载时必须全部丢掉，
# 但 jxb_id 这种**稳定 id** 要保留（界面靠它认回同一个班）。
_it = p2.items[0]
_it.do_id = "TOKEN_FROM_LAST_RUN"   # 一次性令牌
_it.jxb_id = "JXB_STABLE"
_it.state = TaskState.WON
_it.attempts = 9
_it.last_msg = "疑似抢到"
p2.stop_on_first_win = True
p2.start_at = 1790000000.0
rt.set_plan(p2)

# --- 重启：**不再自动恢复**，要用户主动加载（2026-09-30 用户要求）----------
# 旧行为是启动就 load_plan_file() 把上次的清单塞回内存。问题是用户不知道它
# 什么时候会冒出来（可能是上一学期的、可能是上轮已经抢完的），界面上突然就有
# 一份「不是我现在攒的」清单，随手点「开始」就拿着旧目标去打教务了。
# 现在磁盘那份只当**素材**，用户主动点「加载上次数据」才进内存。
rt2 = S.Runtime()  # 等价于「进程重启」
expect("⭐ 重启后**不自动**把清单塞回内存", rt2.plan is None and rt2.plan_meta == {},
       f"实际 plan={rt2.plan!r}")

snap = rt2.snapshot_meta
expect("⭐ 但磁盘上那份「上次保存的数据」是看得见的（只读元信息，不加载）",
       snap.get("count") == 2 and snap.get("path") == str(plan_file), f"实际 {snap}")
expect("快照元信息带存入时刻与学期（界面要显示「什么时候存的」）",
       bool(snap.get("saved_at")) and "semester" in snap)
expect("⭐ 快照元信息带课程名（让用户在点「加载」之前就能确认是哪几门）",
       snap.get("courses") == ["线性代数", "大学英语"], f"实际 {snap.get('courses')}")

rev_before = rt2.plan_rev
res_load = rt2.load_snapshot()
expect("⭐ 主动加载才进内存", res_load.get("ok") is True and rt2.plan is not None
       and len(rt2.plan.items) == 2, f"实际 {res_load}")
expect("加载让版本号 +1（前端手里那个号随之作废 → 走「重新载入」那条安全路径）",
       rt2.plan_rev == rev_before + 1, f"{rev_before} → {rt2.plan_rev}")
expect("加载后内存里就有清单了 → 快照元信息随之清空（它就是内存这份自己）",
       rt2.snapshot_meta == {}, f"实际 {rt2.snapshot_meta}")

# --- 只恢复「意图」，运行状态一律重置 ---------------------------------------
i2 = rt2.plan.items[0]
expect("⭐ 恢复时清掉一次性令牌 do_id", i2.do_id == "", f"实际 {i2.do_id!r}")
expect("⭐ 恢复时状态重置为 pending", i2.state is TaskState.PENDING, f"实际 {i2.state}")
expect("⭐ 恢复时重试次数清零", i2.attempts == 0)
expect("恢复时清掉上一轮的失败原因", i2.last_msg == "")
expect("稳定的 jxb_id 保留（便于界面认回同一个班）", i2.jxb_id == "JXB_STABLE")
expect("全局策略一并恢复（定时开抢时间点不能丢）",
       rt2.plan.stop_on_first_win is True and rt2.plan.start_at == 1790000000.0)
expect("meta 标记「这份清单是用户主动从本地文件加载的」", rt2.plan_meta.get("restored") is True)

# --- 没有快照时加载：明确失败，且**绝不清空**已有清单 ------------------------
rt5 = S.Runtime()
p5 = Plan()
p5.add(PlanItem(kch_id="KB", kcmc="我正攒的课"))
rt5.set_plan(p5)
plan_file.unlink(missing_ok=True)     # 造一个「磁盘上没有快照」的世界
res5 = rt5.load_snapshot()
expect("⭐ 磁盘上没有快照 → 加载明确失败（ok=False，不抛异常）", res5.get("ok") is False,
       f"实际 {res5}")
expect("⭐⭐ 加载失败**绝不清空**内存里已有的清单", rt5.plan is not None
       and len(rt5.plan.items) == 1, f"实际 {rt5.plan}")


# --- 空清单：文件必须被删掉 --------------------------------------------------
rt2.clear_plan()
expect("清空清单 → 文件被删除", not plan_file.exists())
expect("清空清单 → plan 与 meta 都空", rt2.plan is None and rt2.plan_meta == {})

# --- 跨学期：清单作废并告知用户 ---------------------------------------------
rt3 = S.Runtime()
p3 = Plan()
p3.add(PlanItem(kch_id="K9", kcmc="上学期选的课"))
rt3.set_plan(p3)
rt3._plan_meta = dict(rt3.plan_meta, semester="2025-2026|2")   # 伪造「上学期的清单」


class _FakeSess:
    class client:  # noqa: N801 - 只要有个 semester_key 属性即可
        semester_key = "2026-2027|1"


rt3._session = _FakeSess()
verdict = rt3.reconcile_plan_semester()
expect("⭐ 跨学期：清单被判为过期并作废", verdict.get("dropped") is True, f"实际 {verdict}")
expect("跨学期：内存里的清单已清空", rt3.plan is None)
expect("跨学期：落盘文件也一并清掉", not plan_file.exists())
msgs = [e["message"] for e in rt3.events_since(0)]
expect("跨学期：推了一条能读懂原因的日志事件",
       any("上一学期" in m for m in msgs), f"实际 {msgs}")

# 判据从宽：有一端学期未知就不作废（宁可留清单，也不要误删）
rt4 = S.Runtime()
p4 = Plan()
p4.add(PlanItem(kch_id="KA", kcmc="待选课"))
rt4.set_plan(p4)
rt4._plan_meta = dict(rt4.plan_meta, semester="")   # 旧文件没有学期标记
rt4._session = _FakeSess()
expect("两端学期未知 → 不作废（不误删用户攒的清单）",
       rt4.reconcile_plan_semester().get("dropped") is False and rt4.plan is not None)

# --- 跨学期：**磁盘上那份没人加载的快照**也要对账 ---------------------------
# ⚠️ 这是换成「手动加载」之后新增的必要检查：快照躺在磁盘上没被加载，
# 内存里没有清单 —— 对账若只看内存那份，用户点「加载」时就会拿到一份
# 上一学期的废清单（课程 / 教学班 / Tab 下标全对不上），而界面看起来一切正常。
rt6 = S.Runtime()
p6 = Plan()
p6.add(PlanItem(kch_id="KC", kcmc="上学期的课"))
S.save_plan_file(p6, semester="2025-2026|2")   # 直接写盘：模拟「上次留下的数据」
expect("磁盘上有上一学期的快照（只读看得到）", rt6.snapshot_meta.get("count") == 1,
       f"实际 {rt6.snapshot_meta}")
rt6._session = _FakeSess()
v6 = rt6.reconcile_plan_semester()
expect("⭐ 磁盘快照跨学期 → 也要作废（否则用户点「加载」会拿到一份废清单）",
       v6.get("dropped") is True, f"实际 {v6}")
expect("磁盘文件被删掉", not plan_file.exists())
expect("内存本来就没有清单，不受影响", rt6.plan is None)
msgs6 = [e["message"] for e in rt6.events_since(0)]
expect("日志说清作废的是「本地保存的清单」而不是「内存里的清单」",
       any("本地保存的清单" in m and "上一学期" in m for m in msgs6), f"实际 {msgs6}")

# --- 坏文件容错 -------------------------------------------------------------
plan_file.parent.mkdir(parents=True, exist_ok=True)
plan_file.write_text("{ 这不是 JSON", encoding="utf-8")
bad_plan, bad_meta = S.load_plan_file()
expect("清单文件损坏 → 当作「无清单」，不抛异常", bad_plan is None and bad_meta == {})
expect("⭐ 快照元信息读到坏文件也当作「没有快照」，不抛异常", S.plan_snapshot() == {},
       f"实际 {S.plan_snapshot()!r}")
plan_file.unlink(missing_ok=True)

print()
print("=" * 62)
print("C. 课程数据落盘（「上次搜到的课程」，按课程类别分桶）")
print("=" * 62)

_courses_file = S.courses_path()
_rows_a = [{"kch_id": "K1", "kcmc": "空手道", "xf": "1.0"},
           {"kch_id": "K1", "kcmc": "空手道", "xf": "1.0"},
           {"kch_id": "K2", "kcmc": "篮球", "xf": "1.0"}]
_rows_b = [{"kch_id": "K9", "kcmc": "高等数学", "xf": "4.0"}]

expect("课程快照与清单分开存（两个文件，互不干扰）",
       _courses_file is not None and _courses_file.name != plan_file.name,
       f"{_courses_file} vs {plan_file}")

expect("空搜索结果**不**写盘（一次失败的搜索不能冲掉已有数据）",
       S.save_courses_bucket(kklxdm="06", kklxmc="体育", tab_index=1,
                             keyword="", rows=[]) is False
       and not _courses_file.exists())

S.save_courses_bucket(kklxdm="06", kklxmc="板块课(大学体育（一）)", tab_index=1,
                      keyword="", rows=_rows_a, semester="2026-2027|1")
S.save_courses_bucket(kklxdm="01", kklxmc="主修课程", tab_index=0,
                      keyword="数学", rows=_rows_b, semester="2026-2027|1")
summ = S.courses_snapshot_summary()
print(f"   摘要：total={summ.get('total')} 桶={[(b['kklxmc'], b['count']) for b in summ.get('buckets', [])]}")

expect("⭐ 按类别分桶：两次搜索各留一份（单槽会把前一个类别整个盖掉）",
       len(summ.get("buckets") or []) == 2
       and {(b["kklxmc"], b["count"]) for b in summ["buckets"]}
       == {("板块课(大学体育（一）)", 3), ("主修课程", 1)},
       f"{summ.get('buckets')}")
expect("摘要带总行数", summ.get("total") == 4, f"{summ.get('total')}")
expect("⭐ 摘要带课程名（去重后的前几门），点按钮之前就能看清内容",
       sorted((summ["buckets"][0].get("courses") or []))
       in (["空手道", "篮球"], ["高等数学"]),
       f"{[b.get('courses') for b in summ['buckets']]}")
expect("摘要带保存时刻与学期", bool(summ.get("saved_at"))
       and all(b.get("semester") for b in summ["buckets"]))

# 三级匹配：精确 → 类别名 → kklxdm
expect("匹配① 精确（kklxdm+类别名都对）",
       (S.pick_courses_bucket(summ, kklxdm="06", kklxmc="板块课(大学体育（一）)") or {}).get("count") == 3)
expect("⭐ 匹配② 类别名回退（教务改过下标也还能认出来）",
       (S.pick_courses_bucket(summ, kklxdm="", kklxmc="主修课程") or {}).get("keyword") == "数学")
expect("匹配③ kklxdm 回退（类别名也变了时的最后一道）",
       (S.pick_courses_bucket(summ, kklxdm="06") or {}).get("kklxmc") == "板块课(大学体育（一）)")
expect("⭐ 一个都对不上 → None（宁可如实说没有，也不要给别的类别的课）",
       S.pick_courses_bucket(summ, kklxdm="99", kklxmc="不存在") is None)

_picked = S.pick_courses_bucket(summ, kklxdm="06", kklxmc="板块课(大学体育（一）)")
_full = S.load_courses_bucket(_picked["key"])
expect("按 key 取回完整内容（含全部 rows）", len(_full.get("rows") or []) == 3,
       f"{len(_full.get('rows') or [])}")
expect("同一门课的多个教学班都原样留着（不在这里去重）",
       [r["kcmc"] for r in _full["rows"]].count("空手道") == 2)

# 同类别再搜一次 → 只覆盖它自己那一桶
S.save_courses_bucket(kklxdm="06", kklxmc="板块课(大学体育（一）)", tab_index=1,
                      keyword="瑜伽", rows=[{"kch_id": "K3", "kcmc": "瑜伽"}],
                      semester="2026-2027|1")
summ2 = S.courses_snapshot_summary()
_by_name = {b["kklxmc"]: b for b in summ2["buckets"]}
expect("⭐ 同一类别再搜一次 → 只覆盖该桶，别的类别不受影响",
       _by_name["板块课(大学体育（一）)"]["count"] == 1
       and _by_name["板块课(大学体育（一）)"]["keyword"] == "瑜伽"
       and _by_name["主修课程"]["count"] == 1,
       f"{ {k: (v['count'], v['keyword']) for k, v in _by_name.items()} }")

_expect_clear = S.clear_courses_snapshot()
expect("clear_courses_snapshot() → 删文件且摘要变空",
       _expect_clear and S.courses_snapshot_summary() == {})

# 跨学期：课程数据要**独立**对账 —— 用户完全可能只搜过课、一门清单都没建，
# 那种情况下清单那条分支的 stored 是空的，课程数据就会活到新学期。
rt7 = S.Runtime()
S.save_courses_bucket(kklxdm="06", kklxmc="板块课(大学体育（一）)", tab_index=1,
                      keyword="", rows=_rows_a, semester="2025-2026|2")
rt7._session = _FakeSess()          # 当前学期 = 2026-2027|1
v7 = rt7.reconcile_plan_semester()
expect("⭐ 只搜过课、没建过清单 → 上一学期的课程数据也要作废",
       v7.get("dropped") is True and S.courses_snapshot_summary() == {},
       f"{v7} / {S.courses_snapshot_summary()}")
expect("日志说清作废的是课程数据，并带上它原来的学期",
       any("课程数据" in e["message"] and "2025-2026|2" in e["message"]
           for e in rt7.events_since(0)),
       f"{[e['message'] for e in rt7.events_since(0)]}")

_courses_file.parent.mkdir(parents=True, exist_ok=True)
_courses_file.write_text("{ 坏 JSON", encoding="utf-8")
expect("课程快照文件损坏 → 当作「没有快照」，不抛异常",
       S.read_courses_snapshot() == {} and S.courses_snapshot_summary() == {})
_courses_file.unlink(missing_ok=True)

print()
print("=" * 62)
if _fails:
    print(f"✗ {len(_fails)} 条未通过：")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("✓ 全部通过")
