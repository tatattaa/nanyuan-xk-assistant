import sys
import time
from email.utils import formatdate
from pathlib import Path

sys.path.insert(0, ".")
from core import ZfClient, Credential, get_credential, FailureKind, XKError
from core.credit import parse_credit
from core.client import parse_full_msg, parse_hidden_inputs, parse_tabs
from core.clock import ServerClock, parse_http_date
from core.config import (
    CLASS_FIELDS,
    DEFAULT_SCHOOL,
    DISPLAY_FIELDS,
    GNMKDM_XK,
    PATH_CANCEL,
    PATH_SELECTED,
    PATH_XK_INDEX,
    QUERY_FIELDS,
    SUBMIT_FIELDS,
)
from core.errors import KIND_LABEL, classify_state, classify_text, classify_response
from core.drop import drop_state
from engine import GrabbingRunner, Plan, PlanItem, Event, EventType, TaskState

# ---- 离线断言（不碰网络）--------------------------------------------------
_fails = []


def expect(label, cond, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        _fails.append(label)
        print(f"  FAIL {label} {detail}")

print("URL 检查:")
print("  Index   :", DEFAULT_SCHOOL.url("xsxk/zzxkyzb_cxZzxkYzbIndex.html", with_gnmkdm=True, with_layout=True))
print("  Submit  :", DEFAULT_SCHOOL.url("xsxk/zzxkyzbjk_xkBcZyZzxkYzb.html"))
print("  Conflict:", DEFAULT_SCHOOL.url("xsxk/zzxkyzb_cxCtKcZyZzxkYzb.html", with_gnmkdm=False))

c = get_credential(cookie="Cookie: JSESSIONID=abc; route=node1")
print()
print("凭据解析:", c)
print("  header():", c.header())

print()
print("语义分类:")
for t in ["当前不属于选课阶段", "加密串错误", "人数已满", "时间冲突", "系统维护中", "只能选一个教学班"]:
    print("  %-12s -> %s" % (t, classify_text(t).value))
print("  302->login ->", classify_response(302, "", "https://jwxt.nfu.edu.cn/jwglxt/xtgl/login_slogin.html"))

p = Plan()
p.add(PlanItem(kch_id="A", kcmc="高等数学", priority=2))
p.add(PlanItem(kch_id="B", kcmc="线性代数", priority=1))
print()
print("计划排序:", [i.kcmc for i in p.sorted_items()])
print("DISPLAY_FIELDS 数量:", len(DISPLAY_FIELDS))
print("可重试语义:", [k.value for k in FailureKind if k.retryable()])

# ---- 不可重试的语义必须真的不重试（用户确认的行为，别改坏）------------------
# UNKNOWN = 教务给了一个我们没归类的原因（例如「超过本学期最高选课学分限制，不可选！」）。
# 它必须**直接终止**，不能一路重试到上限 —— 教务已经说明原因了，重试毫无意义。
expect("UNKNOWN 不可重试（未知返回直接终止，不撞到上限）", not FailureKind.UNKNOWN.retryable())
# ⚠️ NETWORK 与 UNKNOWN 必须分开（2026-09-30）：前者是「教务没说话」（重试有机会），
# 后者是「教务说了话但没归类」（再试一百次也是同一句话）。混在一起的话，
# 一次网络抖动会和「学分超限」享受同等待遇：整项判死、一次都不重试。
expect("⭐ NETWORK 可重试（网络抖动只是这次没问到，值得再来一次）",
       FailureKind.NETWORK.retryable())
expect("⭐ NETWORK 不是 UNKNOWN（两者处置完全相反，绝不能合并）",
       FailureKind.NETWORK is not FailureKind.UNKNOWN
       and FailureKind.NETWORK.value != FailureKind.UNKNOWN.value)
expect("NETWORK 有中文标签（界面/日志要能读懂）",
       "网络" in KIND_LABEL[FailureKind.NETWORK])
expect("QUOTA_EXCEEDED 不可重试（配额/门次类失败，重试无意义）",
       not FailureKind.QUOTA_EXCEEDED.retryable())
expect("ALREADY_TAKEN 不可重试（同课重复提交）",
       not FailureKind.ALREADY_TAKEN.retryable())
expect("CONTEXT_INVALID 可重试（加密串错，重查刷新令牌能救）",
       FailureKind.CONTEXT_INVALID.retryable())
# 🔴 2026-10-01（用户要求）：满员 = **终态失败**，一次都不重试。
# 名额不会在 800ms 内自己冒出来，重试只是白烧尝试额度与配额；
# 「等有人退课再抢」是另一个时间尺度的事，归将来的「蹲课」功能。
# ⚠️ 别因为「万一有人退课呢」又把它改回可重试 —— 那等于让抢课循环替蹲课值班。
expect("🔴 FULL（教学班已满）不可重试 → 直接判定为失败",
       not FailureKind.FULL.retryable())
# ⚠️ 与上面那条刻意不同：TOO_FREQUENT 是「教务让我慢点」，慢一点再试是有意义的。
# 两条被混在一起的话，会被误以为「反正满员也不重试，那频率类也不用」。
expect("TOO_FREQUENT 仍可重试（频率过高 = 让我慢点，不是没名额）",
       FailureKind.TOO_FREQUENT.retryable())
expect("学分上限文案刻意归 UNKNOWN 而非门次类（门次≠学分，别硬套）",
       classify_text("超过本学期最高选课学分限制，不可选！") is FailureKind.UNKNOWN)

# ---- 实测字段清单规模（一旦被误改会立刻暴露）-------------------------------
print()
print("字段清单规模:")
print(f"  QUERY_FIELDS  = {len(QUERY_FIELDS)}  (实测 45)")
print(f"  CLASS_FIELDS  = {len(CLASS_FIELDS)}  (实测 46)")
print(f"  SUBMIT_FIELDS = {len(SUBMIT_FIELDS)}  (实测 19)")
expect("QUERY_FIELDS 含加密串 xkkz_xh", "xkkz_xh" in QUERY_FIELDS)
expect("SUBMIT_FIELDS 含 jxb_ids", "jxb_ids" in SUBMIT_FIELDS)

# ---- parse_tabs：从 Index 页锚点抽 Tab（含加密串）------------------------
print()
print("parse_tabs 解析:")
IDX = """
<ul id="nav_tab">
 <li class="active"><a id="tab_kklx_01_AAA_2024_117" href="javascript:void(0)"
   onclick="queryCourse(this,'01','AAA','2024','117','XH01')" role="tab">主修课程</a></li>
 <li><a id="tab_kklx_10_BBB_2024_117" href="javascript:void(0)"
   onclick="queryCourse(this,'10','BBB','2024','117','XH10')" role="tab">公共选修课</a></li>
 <li><a id="tab_kklx_06_BBB_2024_117" href="javascript:void(0)"
   onclick='queryCourse(this,"06","BBB","2024","117","XH06")' role="tab">板块课(大英一)</a></li>
</ul>
"""
tabs = parse_tabs(IDX)
for t in tabs:
    print(f"  [{t.kklxdm}] {t.name}  xkkz_id={t.xkkz_id}  xh={t.xkkz_xh}")
expect("解析出 3 个 Tab", len(tabs) == 3, f"实际 {len(tabs)}")
expect("Tab 字段正确", tabs[0].kklxdm == "01" and tabs[0].xkkz_xh == "XH01")
expect("Tab 名称正确", tabs[1].name == "公共选修课")
expect("兼容单引号/双引号 onclick", tabs[2].kklxdm == "06" and tabs[2].xkkz_xh == "XH06")
expect("空 HTML 不炸", parse_tabs("") == [])

# ---- parse_full_msg：满员返回 4 段解析 -----------------------------------
print()
print("parse_full_msg 解析:")
REAL = "0,5B8FE9E7058748A6E06586160CCC45E0,46,"   # 2026-09-29 实测原文
fi = parse_full_msg(REAL)
print("  ", fi)
expect("满员 msg 解析成功", fi is not None and fi["yxzrs"] == "46")
expect("辅教学班标志正确", fi["fzjxb"] == "0")
expect("坏格式返回 None", parse_full_msg("bad") is None and parse_full_msg("") is None)

# ---- ServerClock：区间交集法校准（合成样本，离线可跑）--------------------
print()
print("ServerClock 时钟校准（合成样本）:")
TRUE_OFFSET = 0.37          # 假定的真实偏差：本地钟比服务器慢 370ms
ck = ServerClock()
for rtt in (0.25, 0.41, 0.33, 0.28, 0.62, 0.30):
    t1 = time.monotonic()
    l_star = ck.now() - rtt / 2          # 服务器在往返中点生成 Date 头
    d = int(l_star + TRUE_OFFSET)        # HTTP Date 向下截断到整秒
    ck.observe(formatdate(d, usegmt=True), t1 - rtt, t1)

win = ck.window
print(f"  可行区间 [{win[0]:+.3f}, {win[1]:+.3f})  估计 {ck.offset:+.3f}s  "
      f"不确定度 ±{ck.uncertainty * 1000:.0f}ms")
print("  ", ck.describe())
expect("解析真实 Date 头", parse_http_date("Tue, 29 Sep 2026 05:43:31 GMT") is not None)
expect("坏 Date 返回 None", parse_http_date("not a date") is None and parse_http_date(None) is None)
expect("真值落在估计区间内", win[0] <= TRUE_OFFSET < win[1], f"win={win}")
# 确定性恒等式：中心点误差不可能超过半宽（不要断言「误差 < X ms」——那是碰运气）
expect("估计误差 <= 不确定度", abs(ck.offset - TRUE_OFFSET) <= ck.uncertainty + 1e-9,
       f"误差 {abs(ck.offset - TRUE_OFFSET) * 1000:.0f}ms vs 半宽 {ck.uncertainty * 1000:.0f}ms")
# 单样本的约束区间宽度 = 1 + rtt（Date 只到整秒 + 往返不确定）。
# 交集只会更窄或持平，绝不会更宽 —— 这是可断言的方向。
expect("区间宽度不超过最窄样本的宽度",
       (ck.window[1] - ck.window[0]) <= 1.0 + ck.rtt + 1e-9,
       f"宽度 {ck.window[1] - ck.window[0]:.3f}s vs 最窄样本 {1 + ck.rtt:.3f}s")
# 注意：**不能**断言宽度 >= 1 + 最小RTT。交集的下界可能来自某次样本、上界来自
# 另一次，两次「秒内相位」错开时交集比最窄的单样本还窄（实测踩过这个错误断言）。
expect("样本数正确", ck.samples == 6, f"实际 {ck.samples}")
expect("最小 RTT 被记录", abs(ck.rtt - 0.25) < 1e-9)

# 样本只会让区间变窄（交集单调性质）
_prev = ck.uncertainty
for _ in range(6):
    t1 = time.monotonic()
    l_star = ck.now() - 0.2
    ck.observe(formatdate(int(l_star + TRUE_OFFSET), usegmt=True), t1 - 0.4, t1)
expect("样本增多区间不扩大", ck.uncertainty <= _prev + 1e-9,
       f"{_prev * 1000:.0f}ms -> {ck.uncertainty * 1000:.0f}ms")
expect("真值始终在区间内", ck.window[0] <= TRUE_OFFSET < ck.window[1])

# 未校准的时钟要有明确语义，不能假装准
fresh = ServerClock()
expect("未校准时 synced=False", not fresh.synced)
expect("未校准时 offset=None", fresh.offset is None)
expect("未校准时 server_now 退化为本地挂钟", abs(fresh.server_now() - time.time()) < 0.05)
expect("未校准时 describe 不炸", "未校准" in fresh.describe())

# 矛盾样本（差 100 秒）必须触发重置，而不是留下一个错的偏差
ck2 = ServerClock()
t1 = time.monotonic()
ck2.observe(formatdate(int(ck2.now()), usegmt=True), t1 - 0.3, t1)
ck2.observe(formatdate(int(ck2.now()) + 100, usegmt=True), t1 - 0.3, t1)
expect("矛盾样本触发重置", ck2.samples == 2 and ck2.window is not None)
expect("重置后区间为单样本宽度", (ck2.window[1] - ck2.window[0]) < 2.0,
       f"宽度 {ck2.window[1] - ck2.window[0]:.2f}s")

# 卡点换算：server_now() + N 必须恰好等于 monotonic() + N
expect("monotonic_at 换算无损",
       abs((ck.monotonic_at(ck.server_now() + 5.0) - time.monotonic()) - 5.0) < 0.01)
expect("as_dict 字段齐备",
       {"synced", "offset_ms", "uncertainty_ms", "rtt_ms", "samples", "text"}
       <= set(ck.as_dict()))
# 提前开火量必须有下界（宁可早、不可晚），且未校准时也要给的出
expect("提前开火量在合理区间", 0.2 <= ck.lead_seconds() <= 1.5, f"{ck.lead_seconds():.2f}s")
expect("未校准时提前量取下限", fresh.lead_seconds() == 0.2)
# 回归：必须向上取整。曾用 round（银行家舍入）→ round(0.25*10)==2，把保守量舍小了
expect("提前量不劣于不确定度（宁可早）",
       ck.lead_seconds() >= ck.uncertainty + 0.05 - 1e-9,
       f"lead={ck.lead_seconds():.3f} vs 不确定度 {ck.uncertainty:.3f}")
# 不确定度极大时被上限夹住（RTT 5s 的样本 → 半宽 3s）
big = ServerClock()
_t = time.monotonic()
big.observe(formatdate(int(big.now()) + 10, usegmt=True), _t - 5.0, _t)
expect("超上限被夹到 1.5s", big.lead_seconds() == 1.5,
       f"不确定度 {big.uncertainty:.2f}s → lead {big.lead_seconds():.2f}s")

# ---- core.schedule：上课时间解析与时间冲突（2026-09-29 实测格式）---------
print()
print("课表 / 冲突解析:")
from core.schedule import (  # noqa: E402
    build_entry,
    entries_from_rows,
    find_conflicts,
    is_hard_conflict,
    is_soft_conflict,
    max_jie,
    parse_jxdd,
    parse_sksj,
    parse_teachers,
    slot_from_dict,
    slots_from_dicts,
    sxbj_text,
    teacher_names,
    teacher_titles,
    zixf_text,
)

# 原文来自已选课程列表的真实返回
REAL_SKSJ = "星期三第3-5节{1-17周}<br/>星期六第3-5节{4周}"
sl = parse_sksj(REAL_SKSJ)
expect("多段 sksj 解析成 2 个时段", len(sl) == 2, f"实际 {len(sl)}")
expect("星期解析正确", sl[0].weekday == 3 and sl[1].weekday == 6)
expect("节次范围解析正确", sl[0].start == 3 and sl[0].end == 5)
expect("周次区间展开正确", sl[0].weeks == tuple(range(1, 18)), f"实际 {sl[0].weeks}")
expect("单周次解析正确", sl[1].weeks == (4,))
expect("时段文本可读", sl[0].text == "周三 3-5节 · 1-17周", f"实际 {sl[0].text!r}")
expect("单节次不加连字符", parse_sksj("星期二第7节")[0].jie_text == "7节")

# 周次压缩显示（连续段合并）
expect("周次压缩连续段",
       parse_sksj("星期一第1-2节{1,2,3,5,6,7,9周}")[0].weeks_text == "1-3,5-7,9周",
       f"实际 {parse_sksj('星期一第1-2节{1,2,3,5,6,7,9周}')[0].weeks_text!r}")

# 容错：解析不出「星期+节次」的片段必须被丢掉，而不是画错格子
expect("无周次也能解析", parse_sksj("星期三第3-5节")[0].weeks is None)
expect("垃圾片段被丢弃", len(parse_sksj("待定<br/>星期三第1-2节{1周}")) == 1)
expect("完全解析不出时返回空", parse_sksj("时间待定") == [] and parse_sksj("") == [])

# 冲突判定：周次是必须参与的一维
a = parse_sksj("星期三第8-10节{7周}")[0]
b = parse_sksj("星期三第8-10节{10周}")[0]
c = parse_sksj("星期三第8-10节{1-17周}")[0]
expect("同节次不同周次 = 软冲突（不是硬冲突）",
       (not is_hard_conflict(a, b)) and is_soft_conflict(a, b))
expect("同节次周次重叠 = 硬冲突",
       is_hard_conflict(a, c) and not is_soft_conflict(a, c))
expect("不同星期不冲突", not is_hard_conflict(c, parse_sksj("星期四第8-10节{1周}")[0]))
expect("相邻节次不重叠", not is_hard_conflict(
    parse_sksj("星期三第1-2节{1周}")[0], parse_sksj("星期三第3-5节{1周}")[0]))
# 周次未知 → 保守按重叠处理（宁可多提醒，不可漏判）
expect("周次缺失时保守判为硬冲突",
       is_hard_conflict(parse_sksj("星期三第3-5节")[0], parse_sksj("星期三第3-5节{9周}")[0]))

# 教师解析：真实原文 "工号/姓名/职称"，多人用 ; 分隔
expect("单教师解析", parse_teachers("13059/杨小松/讲师") == [("13059", "杨小松", "讲师")])
expect("多教师解析", parse_teachers("1/甲/教授;2/乙/无") == [("1", "甲", "教授"), ("2", "乙", "无")])
expect("教师姓名串", teacher_names("1/甲/教授;2/乙/讲师") == "甲、乙")
expect("职称串去重", teacher_titles("1/甲/教授;2/乙/教授") == "教授")
expect("空教师不炸", parse_teachers("") == [] and parse_teachers(None) == [])

# 「自选否」列：1=自选上 / 0=系统调整（反查页面 <p class="zixf"> 得到）
expect("自选否 1 → 自选上", zixf_text("1") == "自选上")
expect("自选否 0 → 系统调整", zixf_text("0") == "系统调整")
expect("自选否未知取值 → 空串", zixf_text("9") == "" and zixf_text(None) == "")
expect("选上否文案", sxbj_text("1") == "已选上")

# 地点分段（与 sksj 各段按下标对应）
expect("地点按 br 分段", parse_jxdd("11-403<br/>9-401") == ["11-403", "9-401"])

# build_entry：直接吃真实的一行已选数据
ROW = {
    "kch_id": "1120", "kcmc": "马克思主义基本原理", "jxbmc": "马克思主义基本原理-0008",
    "jsxx": "13059/杨小松/讲师", "sksj": REAL_SKSJ, "jxdd": "11-403<br/>9-401",
    "xf": "3.0", "zixf": "0", "sxbj": "1", "kklxdm": "01", "kklxmc": "主修课程",
}
e = build_entry(ROW)
expect("build_entry 抽到教师+职称", e.teacher_text == "杨小松（讲师）", f"实际 {e.teacher_text!r}")
expect("build_entry 无职称时只显示姓名",
       build_entry({**ROW, "jsxx": "1/张三/无"}).teacher_text == "张三")
expect("build_entry 时间转单行", e.sksj_text == "星期三第3-5节{1-17周} / 星期六第3-5节{4周}")
expect("build_entry 地点转单行", e.jxdd_text == "11-403 / 9-401")
expect("build_entry 自选否文案", e.zixf_cn == "系统调整" and e.sxbj_cn == "已选上")
expect("build_entry 生成 2 个时段", len(e.slots) == 2)

# 冲突检索：同一门课自己不算冲突（铁律 #6 同一门课只押一个班）
other = build_entry({"kch_id": "999", "kcmc": "别人的课", "sksj": "星期三第3-5节{1-17周}"})
expect("撞上别的课 → 命中硬冲突",
       len(find_conflicts(e.slots, [other])) == 1
       and find_conflicts(e.slots, [other])[0].level == "hard")
expect("exclude_kch 排除自身", find_conflicts(e.slots, [e], exclude_kch="1120") == [])
expect("冲突描述含双方时间", "←→" in find_conflicts(e.slots, [other])[0].detail)
expect("max_jie 取最大节次", max_jie([e, other]) == 5)

# Slot ↔ dict 往返（前端把时段带回后端用）
_sd = e.slots[0].as_dict()
_rt = slot_from_dict(_sd)
expect("Slot dict 往返无损", _rt == e.slots[0], f"{_rt} vs {e.slots[0]}")
expect("非法 dict 返回 None",
       slot_from_dict({"weekday": 9, "start": 1, "end": 2}) is None
       and slot_from_dict({}) is None and slot_from_dict("x") is None)
expect("批量还原时静默丢弃非法项",
       len(slots_from_dicts([_sd, {"weekday": 0}, None, "junk"])) == 1)

# 清单内部冲突检测（界面标注 + 执行期互斥跳过，两者同口径）
_p = Plan()
_p.add(PlanItem(kch_id="A", kcmc="甲", slots=parse_sksj("星期三第1-2节{1-9周}")))
_p.add(PlanItem(kch_id="B", kcmc="乙", slots=parse_sksj("星期三第2-3节{1-9周}")))
_p.add(PlanItem(kch_id="C", kcmc="丙", slots=parse_sksj("星期四第1-2节{1-9周}")))
_ic = _p.internal_conflicts()
expect("清单内部冲突被发现", len(_ic) == 1 and _ic[0].level == "hard", f"实际 {len(_ic)} 条")
expect("无时段的清单项不参与判定",
       Plan().add(PlanItem(kch_id="D", kcmc="丁")).internal_conflicts() == [])

# 互斥口径：**星期 + 节次有交集 + 周次有交集** 才算真撞
from core import is_time_conflict, slots_conflict  # noqa: E402

_a = parse_sksj("星期三第3-5节{1-17周}")[0]
_a2 = parse_sksj("星期三第4-5节{6-17周}")[0]    # 节次部分重叠 + 周次重叠
_a3 = parse_sksj("星期三第5节{20周}")[0]        # 只重叠一个节次，但周次完全错开
_a4 = parse_sksj("星期四第4-5节{1-17周}")[0]    # 换天
_a5 = parse_sksj("星期三第8-10节{1周}")[0]      # 同天但节次不沾
expect("节次有交集 + 周次有交集 → 真撞", is_time_conflict(_a, _a2))
expect("只重叠一个节次也算（3-5节 vs 4-5节）", is_time_conflict(_a2, parse_sksj("星期三第5-6节{1-17周}")[0]))
expect("节次重叠但周次错开 → **不算真撞**（不该被跳过）", not is_time_conflict(_a, _a3))
expect("用户举的例子：周二3-5节{1-5周} vs 周二3-5节{6-17周} → 永远不撞",
       not is_time_conflict(parse_sksj("星期二第3-5节{1-5周}")[0],
                            parse_sksj("星期二第3-5节{6-17周}")[0]))
expect("不同星期 → 不冲突", not is_time_conflict(_a, _a4))
expect("同天但节次不沾 → 不冲突", not is_time_conflict(_a, _a5))
expect("周次未知 → 保守视为重叠", is_time_conflict(parse_sksj("星期三第3-5节")[0], _a2))
expect("两组时段批量判定", slots_conflict([_a], [_a4, _a3]) is False
       and slots_conflict([_a], [_a4, _a2]) is True)

# 硬/软两级仍然保留（界面提示要区分「真撞」与「只是节次占位重叠」）
expect("周次也重叠 = 硬冲突", find_conflicts([_a], [build_entry(
    {"kch_id": "X", "kcmc": "真撞", "sksj": "星期三第4-5节{6-17周}"})])[0].level == "hard")
expect("周次错开 = 软冲突", find_conflicts([_a], [build_entry(
    {"kch_id": "Y", "kcmc": "错开", "sksj": "星期三第5节{20周}"})])[0].level == "soft")

# Plan.conflicts_with / conflict_pairs：执行期「抢到一门 → 跳过真撞的其他项」的依据
expect("conflicts_with 只挑出真撞的项",
       [o.kch_id for o in _p.conflicts_with(_p.items[0])] == ["B"])
expect("conflict_pairs 给的是下标对", _p.conflict_pairs() == [[0, 1]])
expect("无时段项不参与互斥", Plan().add(PlanItem(kch_id="E")).conflict_pairs() == [])

# 同节次但周次错开的两个清单项**不是**互斥（抢到一门不该跳过另一门）
_pw = Plan()
_pw.add(PlanItem(kch_id="W1", kcmc="前半学期", slots=parse_sksj("星期二第3-5节{1-5周}")))
_pw.add(PlanItem(kch_id="W2", kcmc="后半学期", slots=parse_sksj("星期二第3-5节{6-17周}")))
expect("同节次但周次错开 → 不算互斥", _pw.conflict_pairs() == []
       and _pw.conflicts_with(_pw.items[0]) == [])
expect("同节次且周次重叠 → 算互斥，且只报一次",
       Plan().add(PlanItem(kch_id="X1", slots=parse_sksj("星期二第3-5节{1-17周}")))
              .add(PlanItem(kch_id="X2", slots=parse_sksj("星期二第4-5节{9周}")))
              .conflict_pairs() == [[0, 1]])

# 同课不同班（蹲课允许，2026-10-01）：时间重叠但**不算互斥**，不该互相跳过
_sc = Plan()
_sc.add(PlanItem(kch_id="SC1", do_id="doA", kcmc="同课甲班", slots=parse_sksj("星期二第3-5节{1-17周}")))
_sc.add(PlanItem(kch_id="SC1", do_id="doB", kcmc="同课乙班", slots=parse_sksj("星期二第3-5节{1-17周}")))
expect("⭐ 同课不同班 → 不算互斥（conflict_pairs 空）", _sc.conflict_pairs() == [])
expect("⭐ 同课不同班 → conflicts_with 不挑出（执行期不跳过）",
       _sc.conflicts_with(_sc.items[0]) == [])

# ---- 退课资格（core/drop.py）---------------------------------------------
# 判据必须与教务 zzxkYzbChoosedZy.js 的 isktk 完全一致：
# 教务显示「退课」按钮才可退，显示「已选」一律不可退。
print()
print("退课资格（复刻教务 isktk）:")

# 我校 2026-09-29 实测的一行：sfxkbj=0 → 教务已选列表显示的是「已选」
_DROP_ROW = {
    "kch_id": "1120", "kcmc": "马克思主义基本原理",
    "sfktk": "1", "zntgpk": "0", "yxzrs": "61", "tktjrs": "0",
    "isInxksj": "1", "sfxkbj": "0", "zckz": "0", "bdzcbj": "2",
    "bhbcyxkjxb": "0",
}
_DROP_PAGE = {"xxdm": "12619", "tkdxyzms": "0", "tkzgcs_jb": "-1", "tkzgcs_qt": "-1"}


def _ds(**over):
    """按需覆盖某个字段。`page=` 单独走一个关键字，不进 row。"""
    page = over.pop("page", None) or _DROP_PAGE
    row = dict(_DROP_ROW)
    row.update(over)
    return drop_state(row, page)


expect("我校实测行（sfxkbj=0）→ 不可退，理由指向「教务显示的是已选」",
       not _ds().allowed and "sfxkbj=0" in _ds().reason, _ds().reason)
expect("把 sfxkbj 改成 1 → 可退（其余条件都满足）", _ds(sfxkbj="1").allowed)
expect("sfktk=0 且 zntgpk=0 → 不可退（教务未开放）",
       not _ds(sfktk="0", zntgpk="0", sfxkbj="1").allowed)
expect("zntgpk=1 单独也能放行（教务 JS 是 or）",
       _ds(sfktk="0", zntgpk="1", sfxkbj="1").allowed)
expect("yxzrs<=tktjrs → 不可退（退课后会低于开课门槛）",
       not _ds(yxzrs="0", tktjrs="5", sfxkbj="1").allowed)
expect("yxzrs>tktjrs 是严格大于", _ds(yxzrs="5", tktjrs="5", sfxkbj="1").allowed is False)
expect("isInxksj=0 → 不可退（不在选课时间内）",
       not _ds(isInxksj="0", sfxkbj="1").allowed)
expect("zckz=1 且 bdzcbj=1 → 不可退（系统统一控制）",
       not _ds(zckz="1", bdzcbj="1", sfxkbj="1").allowed)
expect("zckz=1 但 bdzcbj=2 → 教务放行，可退",
       _ds(zckz="1", bdzcbj="2", sfxkbj="1").allowed)
expect("zckz=1 但 bdzcbj=3 → 教务放行，可退",
       _ds(zckz="1", bdzcbj="3", sfxkbj="1").allowed)
expect("bhbcyxkjxb=1 → 强制放开 sfxkbj",
       _ds(bhbcyxkjxb="1").allowed)
expect("xxdm=10511（教务 JS 里的特判学校）→ 强制放开 sfxkbj",
       _ds(page=dict(_DROP_PAGE, xxdm="10511")).allowed)

# 第二道闸门：教务流程里有我们复刻不了的步骤时，一律不代办
expect("tkdxyzms>0（要短信验证）→ 不可退",
       not _ds(sfxkbj="1", page=dict(_DROP_PAGE, tkdxyzms="1")).allowed)
expect("tkzgcs_jb>0（退课前有规则确认）→ 不可退",
       not _ds(sfxkbj="1", page=dict(_DROP_PAGE, tkzgcs_jb="1")).allowed)
expect("tkzgcs_qt>0（退课前有规则确认）→ 不可退",
       not _ds(sfxkbj="1", page=dict(_DROP_PAGE, tkzgcs_qt="2")).allowed)
expect("tkzgcs=-1（我校实测值）不触发闸门", _ds(sfxkbj="1").allowed)

# 缺字段时保守判不可退，绝不能默认放行
expect("完全空行 → 不可退（保守兜底）", not drop_state({}).allowed)
expect("教学班行（没有 sfktk 字段）→ 不可退", not drop_state({"kch_id": "1", "kcmc": "x"}).allowed)
expect("人数缺失 → 不可退而不是当成 0 放行",
       not _ds(sfxkbj="1", yxzrs="", tktjrs="").allowed)
expect("checks 记录了每条判定的实际取值", set(_ds().checks) >= {
    "sfktk", "zntgpk", "yxzrs", "tktjrs", "isInxksj", "sfxkbj", "zcxkbj", "xxdm"})

# 挂到 ScheduleEntry 上（界面就是从这里拿 can_drop / drop_block 的）
_e_drop = build_entry(_DROP_ROW, source="selected", page=_DROP_PAGE)
expect("ScheduleEntry 从行里算出 can_drop=False", _e_drop.can_drop is False)
expect("ScheduleEntry.drop_block 带上教务给的原因", "sfxkbj=0" in _e_drop.drop_block)
expect("as_dict 把 can_drop/drop_block 一并带出界面",
       _e_drop.as_dict()["can_drop"] is False
       and "sfxkbj=0" in _e_drop.as_dict()["drop_block"])
_e_drop2 = build_entry(dict(_DROP_ROW, sfxkbj="1"), source="selected", page=_DROP_PAGE)
expect("可退的行 can_drop=True 且 drop_block 为空",
       _e_drop2.can_drop is True and _e_drop2.drop_block == "")
expect("不传 page 时退课特判不生效、结果与传 page 一致（我校不命中 10511）",
       build_entry(dict(_DROP_ROW, sfxkbj="1")).can_drop is True)

# ---- parse_credit：本学期学分要求（Index 页顶部）----------------------------
# 片段按我校 2026-09-29 实录结构手抄：最低/最高是**裸 <font>**（没有 id），
# 只有已选学分带 id="yxxfs" —— 所以这里重点验「按文案定位 + 作用域收缩」够不够稳。
print()
print("学分要求解析:")
_IDX = """
<div><font id="xkxn">2026-2027</font> 学年 <font id="xkxq">1</font> 学期&nbsp;
<font id="txt_xklc"><font color="red">第1轮</font></font>
<span id="sysj">（<b><font size="3px">选课时间：2026-09-29 13:00:00 - 2026-09-29 19:00:00</font></b>）</span>
&nbsp;&nbsp;&nbsp;<b>本学期选课要求</b>&nbsp;总学分最低&nbsp;<font color="red">0</font>
&nbsp;&nbsp;最高&nbsp;<font color="red">30</font>
&nbsp;&nbsp;&nbsp;
本学期已选学分&nbsp;&nbsp;<font color="red" id="yxxfs">29.0</font>
</h5></div>
<!-- 干扰项：页面别处也出现「最高」，作用域没收紧就会被带偏 -->
<p>最高学历要求 本科</p>
"""
_c = parse_credit(_IDX)
expect("found=True", _c.found is True)
expect("学年/学期/轮次", (_c.year, _c.term, _c.round_name) == ("2026-2027", "1", "第1轮"))
expect("总学分最低 = 0", _c.min_credit == 0.0)
expect("总学分最高 = 30（没被页面别处的「最高」带偏）", _c.max_credit == 30.0)
expect("本学期已选学分 = 29.0（不传隐藏域时退化为读页面文本，仅浏览器 dump 场景可用）",
       _c.used_credit == 29.0)
expect("剩余可选 = 1.0", _c.remain_credit == 1.0)
expect("选课时间去掉包裹括号", _c.time_text == "2026-09-29 13:00:00 - 2026-09-29 19:00:00")

expect("解析不到时 found=False 且各项为 None（不拿 0 冒充）",
       parse_credit("<html>什么都没有</html>").found is False
       and parse_credit("").max_credit is None
       and parse_credit("").remain_credit is None)

# ⭐ 关键：真值在**隐藏域**里，页面上那几个 <font> 是空壳。
# 我校实测原始 HTTP 响应里 <font id="yxxfs">0</font> 恒为 0，
# 而 <input name="zxfs" value="28.0"/> 才是真的已选学分。
_HID = {"zxfs": "28.0", "xkzgxf": "30", "xkxnmc": "2026-2027", "xkxqmc": "1"}
_rich = parse_credit(_IDX, _HID)
expect("隐藏域优先：zxfs 覆盖页面上的 yxxfs", _rich.used_credit == 28.0)
expect("最高学分来自隐藏域 xkzgxf", _rich.max_credit == 30.0)
expect("学年/学期来自隐藏域 xkxnmc / xkxqmc",
       (_rich.year, _rich.term) == ("2026-2027", "1"))
expect("剩余额度按隐藏域算 = 2.0", _rich.remain_credit == 2.0)

# 页面占位 0 绝不能被当成真实值（这正是第一版读到「已选 0 学分」的原因）
_PLACE = ('<font id="yxxfs">0</font><b>本学期选课要求</b> 总学分最低 '
          '<font color="red">0</font> 最高 <font color="red">30</font>')
_pc2 = parse_credit(_PLACE, {"zxfs": "31.5", "xkzgxf": "40"})
expect("页面占位 0 被隐藏域真值覆盖（31.5，不是 0）", _pc2.used_credit == 31.5)
expect("已选 31.5 > 上限 40 时余量为正", _pc2.remain_credit == 8.5)

# 已选超上限：刻意**不**把负的剩余截成 0，否则会掩盖「你已经超了」
_over = parse_credit(_IDX, {"zxfs": "31.0", "xkzgxf": "30"})
expect("已选超过上限时 remain 为负（不截断成 0）", _over.remain_credit == -1.0)

# ---- Plan.total_credit：清单待加选学分 --------------------------------------
_pc = Plan()
_pc.add(PlanItem(kch_id="A", xf="1.0"))
_pc.add(PlanItem(kch_id="B", xf="2.5"))
_pc.add(PlanItem(kch_id="C", xf=""))        # 空学分按 0 计，不能把整条搞崩
_pc.add(PlanItem(kch_id="D", xf="乱写"))    # 非法值同样按 0
expect("待加选学分 = 3.5（空/非法学分按 0 计）", _pc.total_credit() == 3.5)
expect("空清单待加选学分 = 0", Plan().total_credit() == 0.0)

# ---- 选课期关闭时的语义（2026-09-29 实测）-----------------------------------
# 教务关闭期返回 HTTP 200 + 「当前不属于选课阶段」+ 隐藏域 iskxk=0。
# 坑：HttpSession._do 的三态判定会把这种页面判成 NOT_OPEN 并**抛异常**，
# 导致 client.init() 里那段优雅降级成了永远走不到的死代码。
# 解法：Index 请求传 expect_states=(NOT_OPEN,)，让响应穿透回 init 自行处理。
_COLD = ('<div class="nodata"><span>对不起，当前不属于选课阶段，'
         '如有需要，请与管理员联系！</span></div>'
         '<input type="hidden" name="iskxk" id="iskxk" value="0"/>')

_k_cold = classify_state(_COLD)
expect("关闭期页面被判为 NOT_OPEN", _k_cold is FailureKind.NOT_OPEN)
expect("NOT_OPEN 属于业务态而非可重试错误", _k_cold.retryable() is False)
expect("关闭期页面不含任何学分字段",
       parse_credit(_COLD).found is False)
expect("关闭期学分不拿 0 冒充（各项为 None）",
       parse_credit(_COLD).used_credit is None
       and parse_credit(_COLD).max_credit is None)
expect("iskxk=0 是权威的关闭标志",
       parse_hidden_inputs(_COLD).get("iskxk") == "0")
# 提示语只作兜底：即便文案变了，iskxk 仍要能判出来
_COLD_NO_TEXT = '<input type="hidden" name="iskxk" value="0"/>'
expect("即便没有提示语，iskxk=0 仍可判关闭",
       parse_hidden_inputs(_COLD_NO_TEXT).get("iskxk") == "0")

# expect_states 机制本身：命中的状态不抛异常，未命中照抛
_http_src = Path(__file__).resolve().parents[1] / "core" / "http.py"
_http_txt = _http_src.read_text(encoding="utf-8")
expect("http 层支持 expect_states 穿透（关闭期不再抛异常）",
       "expect_states" in _http_txt and "kind in expect_states" in _http_txt)
_client_src = Path(__file__).resolve().parents[1] / "core" / "client.py"
_client_txt = _client_src.read_text(encoding="utf-8")
expect("init 的 Index 请求声明了 expect_states",
       _client_txt.count("expect_states=(FailureKind.NOT_OPEN,)") >= 2)
expect("submit 有 is_open 本地闸门",
       "if not self.is_open:" in _client_txt)
expect("cancel 也有 is_open 本地闸门（退课不可逆，宁可不给机会）",
       _client_txt.count("if not self.is_open:") >= 2)

# 已选接口的 xkly 必须写死 "0"（2026-09-29 抓包对账：ChoosedDisplay 恒为 0，
# 而 xkly 会被 switch_tab 的 Display 页刷成主修课程的 "1" → 一旦从 store 取
# 就会发出 xkly=1，服务端查不到已选。这是 mock 回放里稳定 miss 的唯一根因）。
expect("⭐ query_selected 的 xkly 写死为 '0'（不能被 Tab 级字段污染）",
       'body["xkly"] = "0"' in _client_txt)

# ---- 真实教务参数不被误改 + mock/真实切换防呆 --------------------------------
# 用户明确要求：「一定要记住现实教务系统的参数，以便我能切回随时使用」。
# 这几条把「真实环境的锚点」钉死，任何一处被改都会立刻红。
_SCHOOL_ANCHORS = {
    "学校名": (DEFAULT_SCHOOL.name, "广州南方学院"),
    "基址": (DEFAULT_SCHOOL.base_url, "https://jwxt.nfu.edu.cn/jwglxt/"),
    "功能码": (GNMKDM_XK, "N253512"),
    "layout 必需": (DEFAULT_SCHOOL.require_layout, True),
    "JSESSIONID 是 HttpOnly": (DEFAULT_SCHOOL.cookie_is_httponly, True),
    "附加 Cookie": (tuple(DEFAULT_SCHOOL.extra_cookies), ("route",)),
    "选课入口路径": (PATH_XK_INDEX, "xsxk/zzxkyzb_cxZzxkYzbIndex.html"),
    "已选路径": (PATH_SELECTED, "xsxk/zzxkyzb_cxZzxkYzbChoosedDisplay.html"),
    "退课路径": (PATH_CANCEL, "xsxk/zzxkyzb_tuikBcZzxkYzb.html"),
}
for _label, (_got, _want) in _SCHOOL_ANCHORS.items():
    expect(f"⭐ 真实教务锚点 · {_label} 未被改动", _got == _want, f"实际 {_got!r}")

expect("⭐ 真实教务锚点 · 提交字段 19 个",
       len(SUBMIT_FIELDS) == 19, f"实际 {len(SUBMIT_FIELDS)}")
expect("⭐ 真实教务锚点 · 课程查询字段 45 个",
       len(QUERY_FIELDS) == 45, f"实际 {len(QUERY_FIELDS)}")
expect("⭐ 真实教务锚点 · 教学班查询字段 46 个",
       len(CLASS_FIELDS) == 46, f"实际 {len(CLASS_FIELDS)}")
expect("⭐ 真实教务锚点 · Display 字段 29 个",
       len(DISPLAY_FIELDS) == 29, f"实际 {len(DISPLAY_FIELDS)}")

# serve.py 的切换防呆：--real 必须能清掉残留环境变量，且「教务目标」按实际生效值打印
_serve_txt = (Path(__file__).resolve().parents[1] / "serve.py").read_text(encoding="utf-8")
expect("serve.py 提供 --real 开关（清残留 XK_SCHOOL_URL）",
       '"--real"' in _serve_txt and 'os.environ.pop("XK_SCHOOL_URL"' in _serve_txt)
expect("serve.py 对残留环境变量会大字告警",
       "检测到环境变量 XK_SCHOOL_URL" in _serve_txt)
expect("serve.py 的「教务目标」按实际生效值判断（不是按 --mock 猜）",
       "_eff = _school_from_env()" in _serve_txt)
expect("--real 与 --mock 互斥",
       "a.real and a.mock" in _serve_txt)

# ---- 作息表（节次 → 上下课时间）--------------------------------------------
# 用户 2026-09-29 提供、**2026-10-01 补齐 6、7 节**。编号连续 1-15，
# 但**相邻编号的间隔并不均匀**（午休夹在 5 和 6 之间），所以必须能查表、不许递增算。
#
# ⚠️ 之前这里断言的是「**缺 6、7 节**」—— 那是**错的**（表里缺号 ≠ 学校没有这两节），
#    它把"我方数据缺失"固化成了"校历事实"，还让前端专门做了"缺号行压扁"的逻辑。
#    教务数据里真的排了「星期一第6-9节{7周}」，于是那两节画不出来。
_jt = DEFAULT_SCHOOL.jie_time_map()
expect("作息表 15 个节次（1-15 连续）", len(_jt) == 15, f"实际 {len(_jt)}")
expect("第 1 节 = 08:00-08:40", _jt.get("1") == "08:00-08:40")
expect("第 5 节 = 11:20-12:00", _jt.get("5") == "11:20-12:00")
expect("⭐ 第 6 节 = 12:50-13:30（午间第一节，2026-10-01 补齐）",
       _jt.get("6") == "12:50-13:30", f"实际 {_jt.get('6')}")
expect("⭐ 第 7 节 = 13:40-14:20（午间第二节）",
       _jt.get("7") == "13:40-14:20", f"实际 {_jt.get('7')}")
expect("第 8 节 = 14:30-15:10", _jt.get("8") == "14:30-15:10")
expect("第 15 节 = 21:05-21:45", _jt.get("15") == "21:05-21:45")
expect("⭐ 节次号 1-15 中间**没有缺号**（缺号会被前端当成「没有这节课」）",
       all(str(i) in _jt for i in range(1, 16)),
       f"缺 {[i for i in range(1, 16) if str(i) not in _jt]}")
expect("⭐ 午间两节夹在上午与下午之间：5 节 12:00 下课 → 6 节 12:50 → 7 节 14:20 → 8 节 14:30",
       _jt.get("5") == "11:20-12:00" and _jt.get("6") == "12:50-13:30"
       and _jt.get("7") == "13:40-14:20" and _jt.get("8") == "14:30-15:10")
expect("时间格式统一为 HH:MM-HH:MM",
       all(len(v.split("-")) == 2 and len(v.split("-")[0]) == 5 for v in _jt.values()),
       str([v for v in _jt.values() if len(v.split("-")[0]) != 5]))
# key 必须是字符串（JSON 序列化后给前端用）
expect("作息表 key 全为字符串（JSON 友好）",
       all(isinstance(k, str) for k in _jt.keys()))

print()
if _fails:
    print(f"=== 自检失败 {len(_fails)} 项：{_fails} ===")
    raise SystemExit(1)
print("=== 核心层 + 引擎层 导入与自检全部通过 ===")

