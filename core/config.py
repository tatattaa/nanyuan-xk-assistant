"""配置常量与目标学校参数。

设计铁律 #1：参数全部动态抓取，一个都不写死。
本文件只保留「非动态」的东西 —— 路径、功能码、常量值。
所有 xkkz_id / njdm_id / xkxnm 一类的上下文参数，一律由 init 动态获取，绝不在此硬编码。
"""

from __future__ import annotations

import json as _json
import os as _os
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# 路径常量（来自 TARGET-API.md 实测 + CROSS-ANALYSIS.md 三源交叉）
# ---------------------------------------------------------------------------

GNMKDM_XK = "N253512"  # 自主选课功能码（三家一致 + 我校实测）
GNMKDM_XYQK = "N551247"  # 学生学业情况统计查询功能码（2026-10-01 menu 实测）

# 登录相关
PATH_LOGIN_PAGE = "xtgl/login_slogin.html"
PATH_PUBLIC_KEY = "xtgl/login_getPublicKey.html"
PATH_INDEX_MENU = "xtgl/index_initMenu.html"
PATH_LOGOUT = "logout"  # 退出登录（2026-10-01 CDP 实测：GET /jwglxt/logout?t=<ts>&login_type=）

# 选课主链路（我校实测存在）
PATH_XK_INDEX = "xsxk/zzxkyzb_cxZzxkYzbIndex.html"
PATH_XK_DISPLAY = "xsxk/zzxkyzb_cxZzxkYzbDisplay.html"
PATH_COURSE_LIST = "xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html"
PATH_CLASS_INFO = "xsxk/zzxkyzbjk_cxJxbWithKchZzxkYzb.html"
PATH_SUBMIT = "xsxk/zzxkyzbjk_xkBcZyZzxkYzb.html"
PATH_SELECTED = "xsxk/zzxkyzb_cxZzxkYzbChoosedDisplay.html"
PATH_CONFLICT = "xsxk/zzxkyzb_cxCtKcZyZzxkYzb.html"
PATH_CANCEL = "xsxk/zzxkyzb_tuikBcZzxkYzb.html"  # 退课（2026-09-29 实录确认）

# 学生学业情况统计查询（2026-10-01 menu 实测，功能码 N551247）
PATH_XYQK_KCXZ = "xyyjgl/xyyj_cxJhyqKcxzList.html"        # 课程性质要求学分列表
PATH_XYQK_QUERY = "xyyjgl/xyyj_cxXsxyqkglIndex.html"      # 学业情况主查询（?doType=query）


# ---------------------------------------------------------------------------
# 提交选课字段（2026-09-29 选课开放期实测确认，共 19 个）
# 来源：zzxkYzbChoosedZy.js 的 saveCourse() / 选课确认两处 POST 组装，
#       两处字段完全一致 → 19 个字段即为最终清单（不再是推测）
# ---------------------------------------------------------------------------

SUBMIT_FIELDS: tuple[str, ...] = (
    "jxb_ids",   # 必须是 do_jxb_id（加密长串），不是 jxb_id
    "kch_id",    # 课程号
    "kcmc",      # 课程名称（明文）
    "rwlx",      # 任务类型
    "rlkz",      # 容量控制开关
    "cdrlkz",    # 重叠容量控制
    "rlzlkz",    # 容量总量控制
    "sxbj",      # 是否筛选标记（由 rlkz/cdrlkz/rlzlkz 推导：任一为 "1" → "1"）
    "xxkbj",     # 选修课标记（按课程 id 取 #xxkbj_<kch_id>）
    "qz",        # 志愿值
    "cxbj",      # 重修标记（按课程 id 取 #cxbj_<kch_id>）
    "xkkz_id",   # 选课开课轮次 id（= firstXkkzId，动态）
    "njdm_id",   # 年级代码
    "zyh_id",    # 专业号
    "kklxdm",    # 开课类型代码（01 主修 / 08 等）
    "xklc",      # 选课轮次序号
    "xkxnm",     # 选课学年码
    "xkxqm",     # 选课学期码
    "jcxx_id",   # 教学班子信息 id，多个用逗号连接
)

# 提交返回的 flag 语义（2026-09-29 实测确认）
FLAG_SUCCESS: tuple[str, ...] = ("1", "3")  # "1"=成功；"3"=成功但免费学分已达上限
# ⭐ flag="6" = 「**该**教学班已选中」（官方前端 `zzxkYzbChoosedZy.js:1367` 原文注释）。
#   语义是「我们押的这个班已经在名下」→ 判 `FailureKind.ALREADY_IN_CLASS`（按已抢到处理），
#   **不是**「同课程已有别的班」（那是文案「只能选一个教学班」→ ALREADY_TAKEN → 判失败）。
FLAG_ALREADY_TAKEN = "6"                    # 该教学班已选中 → ALREADY_IN_CLASS
FLAG_FULL = "-1"                            # 容量超出（满员）

# 满员时 msg 的格式：逗号分隔 4 段
#   辅教学班标志, 教学班id, 已选人数, 本轮已选人数
FULL_MSG_PARTS = 4

# 时间冲突预检（zzxkyzb_cxCtKcZyZzxkYzb.html）的 flag 语义
#   来源：zzxkYzbChoosedZy.js:1196-1229
CT_FLAG_OK = "1"          # 无冲突
CT_FLAG_CONFLICT = "2"    # 与已选教学班上课时间冲突
CT_FLAG_CROSS_CAMPUS = "3"  # 与已选教学班存在同半天跨校区情况
CT_FLAG_BOTH = "4"        # 2 与 3 同时成立
CT_FLAG_MANUAL = "5"      # 需申请教务处理

# 退课（zzxkyzb_tuikBcZzxkYzb.html）返回的纯字符串码
CANCEL_OK = "1"
CANCEL_MSG: dict[str, str] = {
    "1": "退课成功",
    "2": "退课失败：服务器繁忙",
    "3": "退课失败：出现未知异常",
    "4": "退课失败：非法访问",
    "5": "退课失败：校验不通过，请刷新网页后重试",
}


# ---------------------------------------------------------------------------
# 课程列表查询字段（2026-09-29 选课开放期 CDP 实录确认，共 45 个）
# 来源：录真实 XHR —— 在 Index 页点 searchBox「查询」
#       → POST zzxkyzb_cxZzxkYzbPartDisplay.html，48 个参数
#       48 = 45（下表）+ kspage + jspage + jxbzb（条件字段）
#
# ⚠️ 血泪教训：xkkz_xh 是 256 位「加密串」，缺它服务端直接返回
#    {"flag":"0","msg":"加密串错误，可以清除浏览器缓存后刷新网页重试！"}
#    这也是最初 query_courses() 一直失败的唯一原因。
# ---------------------------------------------------------------------------

QUERY_FIELDS: tuple[str, ...] = (
    "rwlx", "xklc", "xkly", "bklx_id", "sfkkjyxdxnxq", "kzkcgs",
    "xqh_id", "jg_id", "njdm_id_1", "zyh_id_1", "gnjkxdnj",
    "zyh_id", "zyfx_id", "njdm_id", "bh_id", "bjgkczxbbjwcx",
    "xbm", "xslbdm", "mzm", "xz", "ccdm", "xsbj",
    "sfkknj", "sfkkzy", "kzybkxy", "sfznkx", "zdkxms", "sfkxq",
    "bhbcyxkjxb", "sfkcfx", "kkbk", "kkbkdj", "bklbkcj",
    "sfkgbcx", "sfrxtgkcxd", "xkkz_xh",      # ← 加密串，绝不能漏
    "tykczgxdcs", "xkxnm", "xkxqm", "kklxdm", "bbhzxjxb", "zxgbxkkg",
    "xkkz_id", "rlkz", "xkzgbj",
)

# 字段 → store 取值键的别名（JS 用 #jg_id_1 的元素值填 jg_id 参数）
FIELD_ALIAS: dict[str, str] = {
    "jg_id": "jg_id_1",
}

# 分页：JS 逻辑 kspage = 已加载数+1，jspage = 已加载数+step（step=10，取自 #xkmcjzxskcs）
DEFAULT_PAGE_STEP = 10


# ---------------------------------------------------------------------------
# 教学班详情查询字段（2026-09-29 CDP 实录确认，共 46 个）
# 来源：录真实 XHR —— 展开课程 → POST zzxkyzbjk_cxJxbWithKchZzxkYzb.html
# 响应字段（35 个）中最关键的三项：
#   do_jxb_id —— 256 位加密串，提交选课时必须用它（不是 jxb_id）
#   jxbrl     —— 容量（首屏课程列表里硬编码为 0，只有这里才是真值）
#   yxzrs     —— 已选人数
# ⚠️ 该接口**不需要** xkkz_xh，但需要 Tab 级的 rwlx / xkly / bklx_id，
#    这些由「切 Tab 时重新加载 Display 页」下发。
# ---------------------------------------------------------------------------

CLASS_FIELDS: tuple[str, ...] = (
    "rwlx", "xkly", "bklx_id", "sfkkjyxdxnxq", "kzkcgs",
    "xqh_id", "jg_id", "zyh_id", "zyfx_id", "txbsfrl",
    "njdm_id", "bh_id", "xbm", "xslbdm", "mzm", "xz", "ccdm", "xsbj",
    "sfkknj", "gnjkxdnj", "sfkkzy", "kzybkxy", "sfznkx", "zdkxms", "sfkxq",
    "bhbcyxkjxb", "sfkcfx", "bbhzxjxb", "kkbk", "kkbkdj", "bklbkcj",
    "xkxnm", "xkxqm", "xkxskcgskg", "rlkz", "cdrlkz", "cxcykclxxskg",
    "rlzlkz", "kklxdm", "kch_id", "jxbzcxskg", "zxgbxkkg",
    "xklc", "xkkz_id", "cxbj", "fxbj",
)


# Display 第二步必须提取的字段
# 来源：CROSS-ANALYSIS.md（GMU 18 个）+ 2026-09-29 实测补充
# ⚠️ 这些字段**随 Tab 变**（rwlx: 主修=1 / 公选=2 / 板块课=3；
#    bklx_id / kkbk / kkbkdj 等同样随 Tab 变），所以每次切 Tab 都要重新加载
#    Display 页并合并这些字段，否则查询会少条件或漏课程。
DISPLAY_FIELDS: tuple[str, ...] = (
    "rwlx",
    "xkly",
    "bklx_id",
    "sfkknj",
    "sfkkzy",
    "kzybkxy",
    "sfznkx",
    "zdkxms",
    "sfkxq",
    "sfkcfx",
    "bhbcyxkjxb",
    "kkbk",
    "kkbkdj",
    "bklbkcj",
    "rlkz",
    "cdrlkz",
    "rlzlkz",
    "sfkgbcx",
    "sfrxtgkcxd",
    "tykczgxdcs",
    "txbsfrl",
    "xkxskcgskg",
    "cxcykclxxskg",
    "jxbzcxskg",
    "zxgbxkkg",
    "gnjkxdnj",
    "bbhzxjxb",
    "xkzgbj",
    "xklc",
)


@dataclass(frozen=True)
class SchoolProfile:
    """目标学校的静态特征。动态参数一律不进这里。

    ⚠️ 这里是**真实教务参数的唯一权威定义处**。改了这里的任何一项，
    请同步更新项目根目录的 `REAL-SCHOOL-PARAMS.md`（人读快照，
    用于在 mock / 真实环境之间来回切换时对照，以及代码被改坏后的恢复依据）。

    想切到本地模拟教务：用 `python serve.py --mock`（走 XK_SCHOOL_URL 环境变量，
    不改这个类）；想确保切回真实教务：用 `python serve.py --real`。
    """

    name: str = "广州南方学院"
    base_url: str = "https://jwxt.nfu.edu.cn/jwglxt/"
    # layout=default 必需（不带时索引页从 23 个隐藏域掉到 1 个）—— 我校实测
    require_layout: bool = True
    # JSESSIONID 是 HttpOnly，document.cookie 读不到，必须 CDP Network.getCookies
    cookie_is_httponly: bool = True
    # 必须与 JSESSIONID 一起带的负载均衡路由 Cookie
    extra_cookies: tuple[str, ...] = ("route",)
    # 选课功能码 / 学业情况功能码（不同学校可能不同，故下沉为可配置字段；
    # 默认值即模块级常量 GNMKDM_XK / GNMKDM_XYQK，向后兼容）。
    gnmkdm_xk: str = GNMKDM_XK
    gnmkdm_xyqk: str = GNMKDM_XYQK
    # 课表节次 → 上下课时间（"HH:MM-HH:MM"）。用户 2026-09-29 提供，2026-10-01 补齐 6、7 节。
    #
    # ⚠️ 编号是**连续**的 1-15，但**相邻编号之间的间隔并不均匀**：5 节 12:00 下课、
    #    6 节 12:50 才上（午休）；7 节 14:20 下课、8 节 14:30 就上。
    #    所以**必须查表，绝不能按节次递增去算时间**。
    #
    # 🔴 历史坑（2026-10-01 踩过）：这张表原先缺 6、7 节，被当成了
    #    「我校没有这两节课」，前端还专门为"缺号行"做了压扁成细线的逻辑。
    #    结果教务数据里**真的**排了「星期一第6-9节{7周}」—— 那两节课画不出来，
    #    而且因为缺号行被压扁，整表行号也跟着错。
    #    **教训：表里缺号只能说明"我不知道它的时间"，不能推出"学校没有这两节"。**
    jie_time: tuple[tuple[int, str], ...] = (
        (1, "08:00-08:40"),
        (2, "08:50-09:30"),
        (3, "09:45-10:25"),
        (4, "10:35-11:15"),
        (5, "11:20-12:00"),
        (6, "12:50-13:30"),
        (7, "13:40-14:20"),
        (8, "14:30-15:10"),
        (9, "15:15-15:55"),
        (10, "16:10-16:50"),
        (11, "16:55-17:35"),
        (12, "18:45-19:25"),
        (13, "19:30-20:10"),
        (14, "20:15-20:55"),
        (15, "21:05-21:45"),
    )

    def jie_time_map(self) -> dict[str, str]:
        """给前端用的 dict（JSON 的 key 只能是字符串）。"""
        return {str(k): v for k, v in self.jie_time}

    def url(
        self,
        path: str,
        *,
        with_gnmkdm: bool = True,
        with_layout: bool = False,
        gnmkdm: str | None = None,
    ) -> str:
        """拼出完整 URL。layout / gnmkdm 按我校实测的必需性自动附加。

        `gnmkdm` 默认取 self.gnmkdm_xk（选课功能码）；学业情况等其它模块传各自的码
        （如 self.gnmkdm_xyqk = N551247）。
        """
        if gnmkdm is None:
            gnmkdm = self.gnmkdm_xk
        base = self.base_url.rstrip("/") + "/"
        u = base + path.lstrip("/")
        params: list[str] = []
        if with_gnmkdm:
            params.append(f"gnmkdm={gnmkdm}")
        if with_layout and self.require_layout:
            params.append("layout=default")
        if params:
            u += ("&" if "?" in u else "?") + "&".join(params)
        return u


DEFAULT_SCHOOL = SchoolProfile()


@dataclass
class Credential:
    """凭据。铁律 #9：凭据不落盘（内存持有）。

    cookie_header 是可直接放进请求头的一整串，形如 "JSESSIONID=xxx; route=yyy"。
    本对象只应存在于内存，不得序列化到磁盘、不得打日志。
    """

    cookie_header: str
    source: str = "manual"  # manual | cdp
    _sealed: bool = field(default=True, repr=False)

    def __repr__(self) -> str:  # 防止凭据意外泄漏到日志
        n = len(self.cookie_header.split(";")) if self.cookie_header else 0
        return f"<Credential source={self.source} cookies={n}条 (值已隐藏)>"

    def header(self) -> dict[str, str]:
        return {"Cookie": self.cookie_header}


# ---------------------------------------------------------------------------
# 学校配置化（2026-10-02 通用化改造）
#
# 目标：让「换学校」不需要改代码。学校专属参数（名称/基址/课表作息/功能码等）
# 都可通过一个 JSON 文件覆盖。默认仍是内置的广州南方学院（DEFAULT_SCHOOL）。
#
# 用法（新学校接入者看这里）：
#   1. 复制 `schools/广州南方学院.json` 为 `schools/我的学校.json`；
#   2. 改里面的字段（base_url / jie_time / gnmkdm_xk / gnmkdm_xyqk ...）；
#   3. 启动时通过环境变量 `XK_SCHOOL_PROFILE` 指定该文件：
#        XK_SCHOOL_PROFILE=schools/我的学校.json python serve.py
#      或在 UI 里选择学校（阶段二）。
#
# 字段未出现在 JSON 里的，沿用内置默认值（partial override）。
# ---------------------------------------------------------------------------

import json as _json
import os as _os

# JSON 里的字段名 → SchoolProfile dataclass 字段名（其余字段保持默认）
_SCHOOL_JSON_FIELDS: dict[str, str] = {
    "name": "name",
    "base_url": "base_url",
    "require_layout": "require_layout",
    "cookie_is_httponly": "cookie_is_httponly",
    "extra_cookies": "extra_cookies",
    "gnmkdm_xk": "gnmkdm_xk",
    "gnmkdm_xyqk": "gnmkdm_xyqk",
    "jie_time": "jie_time",
}


def school_profile_from_json(path: str | None) -> SchoolProfile:
    """从 JSON 文件加载学校配置（partial override）。

    - path 为 None 或文件不存在/解析失败：返回内置 DEFAULT_SCHOOL（南方学院）；
    - jie_time 在 JSON 里是 {"1": "08:00-08:40", ...}（key 字符串），
      这里转成 SchoolProfile 需要的 tuple[tuple[int, str], ...]；
    - extra_cookies 在 JSON 里是字符串列表，转成 tuple。
    """
    if not path:
        return DEFAULT_SCHOOL
    try:
        raw = _json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_SCHOOL
    if not isinstance(raw, dict):
        return DEFAULT_SCHOOL

    overrides: dict = {}
    for jk, fk in _SCHOOL_JSON_FIELDS.items():
        if jk not in raw:
            continue
        val = raw[jk]
        if fk == "jie_time":
            # {"1": "08:00-08:40", ...} → ((1, "08:00-08:40"), ...)
            if isinstance(val, dict):
                try:
                    val = tuple((int(k), str(v)) for k, v in val.items())
                except Exception:
                    continue
            elif isinstance(val, list):
                try:
                    val = tuple((int(a), str(b)) for a, b in val)
                except Exception:
                    continue
            else:
                continue
        elif fk == "extra_cookies":
            if isinstance(val, list):
                val = tuple(str(x) for x in val)
            elif isinstance(val, str):
                val = (val,)
            else:
                continue
        overrides[fk] = val

    if not overrides:
        return DEFAULT_SCHOOL
    return SchoolProfile(**overrides)


def load_school_profile() -> SchoolProfile:
    """加载当前学校配置：环境变量 XK_SCHOOL_PROFILE 指定的 JSON，否则内置默认。"""
    return school_profile_from_json(_os.environ.get("XK_SCHOOL_PROFILE"))
