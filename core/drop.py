"""退课资格判定：决定「这门课能不能在我们这边退」。

## 一句话原则

**教务显示「退课」按钮，我们才显示退课按钮；教务显示「已选」，我们绝不提供退课。**
判定不是我方发明的规则，而是**逐字复刻教务自己的判定**。

## 第一道闸门：教务的 `isktk`（唯一权威依据）

教务选课首页的 `zzxkYzbChoosedZy.js`（我校实录副本见
`C:/workbuddy/2026-09-28-22-35-06/zzxkYzbChoosedZy.js`）渲染「已选课程」列表时，
对每一行先算一个 `isktk`，然后：

    // zzxkYzbChoosedZy.js:199-203
    if(isktk=="1"){
        渲染红色「退课」按钮 → cancelCourseZzxk(...)
    }else{
        渲染蓝色「已选」二字（txt-yx）
    }

`isktk` 的算法（`zzxkYzbChoosedZy.js:142-154`，原文照抄）：

    var zcxkbj = "1";
    if(modelA.zckz=="1" && modelA.bdzcbj!="2" && modelA.bdzcbj!="3"){ zcxkbj = "0"; }
    var sfxkbj = modelA.sfxkbj;
    if($("#xxdm").val()=="10511" || modelA.bhbcyxkjxb=="1"){ sfxkbj = "1"; }
    if((modelA.sfktk=="1" || modelA.zntgpk=="1")
       && parseInt(modelA.yxzrs) > parseInt(modelA.tktjrs)
       && modelA.isInxksj=="1"
       && sfxkbj=="1"
       && zcxkbj=="1"){
        isktk = "1";
    }

即**五个条件全为真**才可退：

| # | 条件 | 不满足时 |
|---|---|---|
| 1 | `sfktk=="1"` 或 `zntgpk=="1"` | 教务未开放退课 |
| 2 | `yxzrs > tktjrs` | 退课后人数会低于开课门槛 |
| 3 | `isInxksj=="1"` | 不在选课时间内 |
| 4 | `sfxkbj=="1"`（受 `xxdm`/`bhbcyxkjxb` 特判影响） | 教务当前不提供退课入口 |
| 5 | `zcxkbj=="1"`（由 `zckz`/`bdzcbj` 推出） | 课程由系统统一控制 |

前四个字段是**行级**的（来自已选列表 `zzxkyzb_cxZzxkYzbChoosedDisplay.html` 的每行），
`xxdm` 是**页面级**的（Index 页隐藏域）。

> 铁律 #1：`10511` 是教务 JS 里写死的特判学校代码，我方**不写死**——`xxdm` 动态取，
> 判断按同一逻辑跑。我校 `xxdm=12619`，不命中该特判。

## 第二道闸门：教务流程里我们复刻不了的步骤

教务退课链路是 `cancelCourseZzxk` → `tuikeCheck_30` → `delCourse`。
在特定页面开关下中间会插进我们做不到的事：

| 页面字段 | 非默认值时的教务行为 | 本模块的处理 |
|---|---|---|
| `tkdxyzms > 0` | 退课要求**短信验证码**（`common_cxCheckXsxkYzm.html`） | 判不可退，请用户去教务退 |
| `tkzgcs_jb > 0` / `tkzgcs_qt > 0` | 先拉退课规则文案并弹 confirm 让用户确认 | 判不可退，请用户去教务退 |

我校实测 `tkdxyzms=0`、`tkzgcs_jb=-1`、`tkzgcs_qt=-1` → 教务无附加步骤，直接退。
但**开关一变就必须停下**：宁可让用户去教务网站退，也不能替他绕过学校的规则确认。

## 依赖

本模块**不 import core 内任何其他模块**，是纯函数，可被 `core.schedule` / `ui` 自由引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 第二道闸门：会引入「我们做不到的步骤」的页面级开关
#   (字段名, 不可退理由)  —— 取值 > 0 即触发
# ---------------------------------------------------------------------------

_PAGE_GATES: tuple[tuple[str, str], ...] = (
    ("tkdxyzms", "教务要求退课前做短信验证码校验，本工具不做验证，请到教务网站退课"),
    ("tkzgcs_jb", "教务退课前会先弹出退课规则让你确认，本工具不代你确认，请到教务网站退课"),
    ("tkzgcs_qt", "教务退课前会先弹出退课规则让你确认，本工具不代你确认，请到教务网站退课"),
)

# 教务 JS 里 sfxkbj 的特判：这些学校代码 / 标志下强制视为「向学生开放选退课」
_SFXKBJ_FORCE_XXDM = "10511"


def _s(v: object) -> str:
    """转字符串并去空白（教务字段可能给 None / 数字 / 带空格）。"""
    return "" if v is None else str(v).strip()


def _i(v: object) -> int | None:
    """转 int；转不了返回 None（不猜，也不当成 0）。"""
    s = _s(v)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


@dataclass
class DropState:
    """退课资格判定的结果。"""

    allowed: bool = False
    reason: str = ""  # 不可退时的原因（可直接展示给用户）
    checks: dict = field(default_factory=dict)  # 每条判定的实际取值，供排查

    @property
    def text(self) -> str:
        return "可退课" if self.allowed else f"不可退课：{self.reason}"

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "checks": self.checks}


def drop_state(row: dict | None, page: dict | None = None) -> DropState:
    """给定一行已选课程（+ 页面级隐藏域），判断教务是否允许退课。

    `row`  —— 已选列表的原始行（55 列那套），需含 sfktk / zntgpk / yxzrs /
              tktjrs / isInxksj / sfxkbj / zckz / bdzcbj / bhbcyxkjxb。
    `page` —— 页面级隐藏域（Index 页那套），至少含 xxdm；缺省则该特判不生效。

    判定顺序与教务 JS 完全一致：**任一条件不满足即不可退**，并明确指出是哪一条。
    """
    row = row or {}
    page = page or {}
    checks: dict = {}

    def fail(reason: str) -> DropState:
        return DropState(allowed=False, reason=reason, checks=dict(checks))

    # --- 条件 5 的前置：zcxkbj（教务 JS 原文）---
    zckz = _s(row.get("zckz"))
    bdzcbj = _s(row.get("bdzcbj"))
    zcxkbj = "0" if (zckz == "1" and bdzcbj not in ("2", "3")) else "1"

    # --- 条件 4 的前置：sfxkbj（教务 JS 里对特定学校/标志强制放开）---
    sfxkbj = _s(row.get("sfxkbj"))
    bhbcyxkjxb = _s(row.get("bhbcyxkjxb"))
    xxdm = _s(page.get("xxdm"))
    if xxdm == _SFXKBJ_FORCE_XXDM or bhbcyxkjxb == "1":
        sfxkbj = "1"

    sfktk = _s(row.get("sfktk"))
    zntgpk = _s(row.get("zntgpk"))
    is_inxksj = _s(row.get("isInxksj"))
    yxzrs = _i(row.get("yxzrs"))
    tktjrs = _i(row.get("tktjrs"))

    checks.update(
        sfktk=sfktk,
        zntgpk=zntgpk,
        yxzrs=yxzrs,
        tktjrs=tktjrs,
        isInxksj=is_inxksj,
        sfxkbj=sfxkbj,
        zcxkbj=zcxkbj,
        zckz=zckz,
        bdzcbj=bdzcbj,
        bhbcyxkjxb=bhbcyxkjxb,
        xxdm=xxdm,
    )

    # --- 条件 1：教务是否开放退课 ---
    if not (sfktk == "1" or zntgpk == "1"):
        return fail(
            f"教务未开放该课的退课（sfktk={sfktk or '空'}、zntgpk={zntgpk or '空'}）"
        )

    # --- 条件 2：退课后人数是否仍满足开课门槛 ---
    if yxzrs is None or tktjrs is None:
        return fail("教务未给出人数信息（yxzrs/tktjrs），无法确认退课后是否仍满足开课门槛")
    if yxzrs <= tktjrs:
        return fail(f"退课后人数会低于开课门槛（已选 {yxzrs} ≤ 退课统计 {tktjrs}）")

    # --- 条件 3：是否在选课/退课时间内 ---
    if is_inxksj != "1":
        return fail(f"当前不在该课的选课/退课时间内（isInxksj={is_inxksj or '空'}）")

    # --- 条件 4：教务是否给了退课入口 ---
    if sfxkbj != "1":
        return fail(
            f"教务当前未向该课提供退课入口（sfxkbj={sfxkbj or '空'}，"
            "教务已选列表此时显示的是「已选」而非「退课」）"
        )

    # --- 条件 5：是否被系统统一控制 ---
    if zcxkbj != "1":
        return fail(f"该课程由系统统一控制（zckz={zckz}、bdzcbj={bdzcbj}），教务不开放学生退课")

    # --- 第二道闸门：教务会走我们复刻不了的流程时，一律不代办 ---
    for key, why in _PAGE_GATES:
        v = _i(page.get(key))
        checks[key] = v
        if v is not None and v > 0:
            return fail(why)

    return DropState(allowed=True, reason="", checks=checks)
