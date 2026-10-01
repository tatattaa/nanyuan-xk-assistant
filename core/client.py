"""正方教务客户端：init / query / submit。

核心层 4 函数的后三个。约束（HANDOFF 5.2）：核心层不许认识界面。

设计要点（全部有实测/交叉分析依据）：
- init 分两步：GET Index → POST Display（我校实测，lnuElytra 同构）
- 两步都必须带 layout=default（不带时 23 个隐藏域掉到 1 个）
- store 缓存 init 结果，提交时复用（GCCTool 的性能优化）
- 提交严格串行（本类不做并发，由引擎层保证）
- 铁律 #1：参数全部动态抓取，一个都不写死
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser

from core.clock import ServerClock
from core.config import (
    CLASS_FIELDS,
    DEFAULT_PAGE_STEP,
    DEFAULT_SCHOOL,
    DISPLAY_FIELDS,
    FIELD_ALIAS,
    FLAG_ALREADY_TAKEN,
    FLAG_FULL,
    FLAG_SUCCESS,
    FULL_MSG_PARTS,
    PATH_CANCEL,
    PATH_CLASS_INFO,
    PATH_CONFLICT,
    PATH_COURSE_LIST,
    PATH_SELECTED,
    PATH_SUBMIT,
    PATH_XK_DISPLAY,
    PATH_XK_INDEX,
    PATH_XYQK_KCXZ,
    PATH_XYQK_QUERY,
    QUERY_FIELDS,
    Credential,
    SchoolProfile,
)
from core.credit import CreditInfo, parse_credit
from core.errors import FailureKind, XKError
from core.http import (
    TIMEOUT_CRITICAL,
    TIMEOUT_NORMAL,
    TIMEOUT_QUICK,
    HttpSession,
    RawResponse,
)

logger = logging.getLogger("xk.client")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Jxb:
    """教学班。

    字段名对齐 `zzxkyzbjk_cxJxbWithKchZzxkYzb.html` 实测返回（35 列）。
    ⚠️ `do_id`（= do_jxb_id，256 位加密串）才是提交时用的 id，`jxb_id` 不是。
    """

    jxb_id: str
    do_id: str  # = do_jxb_id，提交时必须用这个
    kcmc: str = ""
    jsxx: str = ""      # 教师，格式 "工号/姓名/职称;..."（多人用 ; 分隔）
    sksj: str = ""      # 上课时间
    jxdd: str = ""      # 教室
    jxbrl: str = ""     # 容量（课程列表首屏硬编码为 0，此处才是真值）
    yxzrs: str = ""     # 已选人数
    jxms: str = ""      # 教学模式
    kkxymc: str = ""    # 开课学院
    xf: str = ""        # 学分
    dsfrl: str = ""     # 待释放容量
    sjsfsj: str = ""    # 随机释放时间

    @property
    def is_full(self) -> bool:
        try:
            return int(self.jxbrl or 0) <= int(self.yxzrs or 0)
        except ValueError:
            return False


@dataclass
class Course:
    """课程及其教学班。"""

    kch_id: str
    kcmc: str = ""
    xkkz_id: str = ""
    kklxdm: str = ""
    jxb: list[Jxb] = field(default_factory=list)


@dataclass
class Tab:
    """课程类别 Tab（开课类型）。

    2026-09-29 实测：Index 页每个 Tab 都带**自己独立的** xkkz_id 与
    xkkz_xh（加密串）。跨 Tab 混用必然触发「加密串错误」，所以必须成对使用。
    来源：Index 页 `<a onclick="queryCourse(this,'01','<xkkz_id>','<njdm_id>','<zyh_id>','<xkkz_xh>')">`
    """

    kklxdm: str
    xkkz_id: str
    njdm_id: str
    zyh_id: str
    xkkz_xh: str
    name: str = ""


# `queryCourse(this,'01','...','2024','117','...')` —— 单双引号都要容错
_TAB_PARAM_RE = re.compile(
    r"""queryCourse\(\s*this\s*,"""
    r"""\s*['"](?P<kklxdm>[^'"]*)['"]\s*,\s*"""
    r"""['"](?P<xkkz_id>[^'"]*)['"]\s*,\s*"""
    r"""['"](?P<njdm_id>[^'"]*)['"]\s*,\s*"""
    r"""['"](?P<zyh_id>[^'"]*)['"]\s*,\s*"""
    r"""['"](?P<xkkz_xh>[^'"]*)['"]\s*\)""",
    re.S,
)
_TAB_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bonclick\s*=\s*(?P<q>[\"'])(?P<code>.*?queryCourse\(.*?)(?P=q)[^>]*>(?P<label>.*?)</a>",
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_tabs(html: str) -> list[Tab]:
    """从 Index 页解析全部课程类别 Tab。解析不到 → 空列表（未开放期正常）。"""
    out: list[Tab] = []
    seen: set[tuple[str, str]] = set()
    for m in _TAB_ANCHOR_RE.finditer(html or ""):
        pm = _TAB_PARAM_RE.search(m.group("code") or "")
        if not pm:
            continue
        key = (pm.group("kklxdm"), pm.group("xkkz_xh"))
        if key in seen:  # 页面上同一 Tab 可能出现多次
            continue
        seen.add(key)
        label = _TAG_RE.sub("", m.group("label") or "").strip()
        out.append(
            Tab(
                kklxdm=pm.group("kklxdm"),
                xkkz_id=pm.group("xkkz_id"),
                njdm_id=pm.group("njdm_id"),
                zyh_id=pm.group("zyh_id"),
                xkkz_xh=pm.group("xkkz_xh"),
                name=label,
            )
        )
    return out


# ---------------------------------------------------------------------------
# HTML 解析（零依赖）
# ---------------------------------------------------------------------------


class _HiddenInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "input":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if a.get("type", "").lower() == "hidden" and a.get("name"):
            self.hidden[a["name"]] = a.get("value", "")


class _IdValueParser(HTMLParser):
    """抽所有带 id 的元素（GMU 用 #field 取 Display 页的值）。"""

    def __init__(self):
        super().__init__()
        self.by_id: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        _id = a.get("id")
        if _id:
            self.by_id.setdefault(_id, a.get("value", ""))


def page_is_open(text: str, hidden: dict[str, str]) -> bool:
    """选课首页（Index）这一次响应是否处于「已开放」状态。

    权威判据是隐藏域 `iskxk`：关闭期页面里 `iskxk=0`，且 `xkkz_id` / `xkxnm` 等
    选课上下文**整个消失**（只剩 21 个通用隐藏域）。中文提示语只作兜底 ——
    提示语文案可能随教务版本变化，标志位更稳。

    抽成模块级纯函数，是为了让 `init()` 与 `ping()` 用**同一套判据** ——
    两边各写一遍迟早会分叉，届时「探活说已开放、init 说没开放」会自相矛盾。
    """
    if hidden.get("iskxk") == "0":
        return False
    return not ("不属于选课阶段" in text or "不在选课时间" in text)


def parse_hidden_inputs(html: str) -> dict[str, str]:
    p = _HiddenInputParser()
    p.feed(html or "")
    return p.hidden


def parse_id_values(html: str) -> dict[str, str]:
    p = _IdValueParser()
    p.feed(html or "")
    return p.by_id


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class ZfClient:
    """核心层客户端。只认识 HTTP 与业务，不认识界面。"""

    def __init__(
        self,
        credential: Credential,
        school: SchoolProfile | None = None,
        timeout: float | tuple[float, float] | None = None,
    ):
        self.school = school or DEFAULT_SCHOOL
        # timeout=None → 由 HttpSession 用 `TIMEOUT_NORMAL`。
        # 关键路径（submit / precheck / query_classes）**不靠这里**，它们逐请求覆盖成
        # `TIMEOUT_CRITICAL` —— 见各方法的 `timeout=` 参数。
        self.http = HttpSession(credential, self.school, timeout)
        # init 结果缓存（GCCTool 优化：避免每次提交重跑 init）
        self.store: dict[str, str] = {}
        # Index 页隐藏域的**单独快照**。
        # 为什么不能只看 self.store：init 第二步会用 Display 页的隐藏域 update 进来，
        # 而 Display 页把 sfktk / tktjrs / txbsfrl / tkzgcs_jb 都发成**空串**，
        # 于是这些页面级开关在 store 里会被抹掉。退课资格要用 Index 页那份原始值，
        # 所以这里单独留一份，只增不改。
        self.index_store: dict[str, str] = {}
        # 课程类别 Tab（每个 Tab 独立持有 xkkz_id + xkkz_xh）。
        #
        # ⚠️ 这一份是「**最近一次成功解析到**的 Tab」，不等于「最近一次 Index 响应里的
        #    Tab」。未开放期的 Index 页一个 Tab 都没有（`parse_tabs` → []），
        #    若这里照抄「解析到什么就是什么」，界面上的「课程类别」下拉会在
        #    每次刷新后凭空消失，要等下一轮开放期才补回来。所以改成：
        #    **解析到内容才替换，解析为空则沿用旧值**（见 `_adopt_tabs`）。
        #
        # ⚠️ 缓存的 Tab **只用于界面展示**，绝不拿去发请求 —— 它的 xkkz_id /
        #    xkkz_xh 与学期轮次绑定，未开放期拿它 switch_tab / 查课只会换回
        #    框架页或「加密串错误」。判断依据见 `tabs_stale`。
        self.tabs: list[Tab] = []
        self.tabs_at: float = 0.0      # 上面这份 Tab 的抓取时刻（本地挂钟 unix 秒）
        self.tabs_stale: bool = False  # True = 本次 init 没解析到 Tab，用的是缓存
        self._tabs_semester: str = ""  # 缓存所属学期；学期一变立刻作废（防跨学期串用）
        # 本学期学分要求（从 Index 页解析：最低 / 最高 / 已选）。
        # 已选学分是**实时值**，选上一门/退掉一门都会变 —— 要最新值就重跑 refresh_credit()。
        self.credit: CreditInfo = CreditInfo()
        self._cur_tab: Tab | None = None
        self._inited = False
        # 上一次**真正抓取**上下文（打了一趟 Index 页）的单调钟时刻。
        # 0 = 从没抓过。用途见 `store_age_s`：引擎要据此判断手里的
        # xkkz_id / do_jxb_id 是不是已经「超龄」，该重抓了。
        self.store_at: float = 0.0

    # -- 生命周期 -----------------------------------------------------------

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- 时钟（供定时开抢换算卡点）------------------------------------------

    @property
    def clock(self) -> ServerClock:
        """服务器时钟估计器。由 HttpSession 在每次响应时自动喂样本。"""
        return self.http.clock

    def server_now(self) -> float:
        """当前服务器时刻（unix 秒）。未校准时退化为本地挂钟。"""
        return self.clock.server_now()

    def sync_clock(self, samples: int = 4) -> ServerClock:
        """主动收窄时钟偏差（打登录页，零副作用、零配额消耗）。"""
        return self.http.sync_clock(samples)

    # -------------------------------------------------------------------
    # 核心函数 2 / 4：init
    # -------------------------------------------------------------------

    def init(self, force: bool = False) -> dict[str, str]:
        """初始化选课上下文。两步走，结果缓存进 self.store。

        步骤 1：GET  Index 页（带 gnmkdm + layout）→ 抽隐藏域
        步骤 2：POST Display 页（带 gnmkdm + layout）→ 抽隐藏域 + 指定字段

        Display 第二步的 body 需带 xkkz_id；若 Index 没给出（未开放期），
        则用 gnmkdm 值兜底（我校实测该调用不报错，返回框架页）。
        """
        if self._inited and not force:
            return self.store

        # --- 步骤 1：Index ---
        index_url = self.school.url(PATH_XK_INDEX, with_gnmkdm=True, with_layout=True)
        logger.debug("init 步骤1 GET %s", index_url)
        # ⚠️ 必须允许 NOT_OPEN 穿透：关闭期这一页是 HTTP 200 + 「当前不属于选课阶段」，
        # 属于**正常业务状态**而非错误。若让 http 层直接抛异常，下面第 3 步那段
        # 优雅降级就成了永远走不到的死代码（2026-09-29 实测踩到）。
        r1 = self.http.get(index_url, expect_states=(FailureKind.NOT_OPEN,))
        hidden1 = parse_hidden_inputs(r1.text)
        self.store.update(hidden1)
        # 记下「上下文是什么时候抓的」。引擎靠它判断该不该重抓（见 store_age_s）。
        # 刻意放在**这一次真实请求之后**：即便教务处于关闭期（拿不到选课上下文），
        # 这次也确实问过服务器了，岁龄就该归零。
        self.store_at = time.monotonic()
        # Index 页那份单独留档（见 __init__ 注释：Display 会把这些字段抹成空串）
        self.index_store.update(hidden1)
        logger.info("init 步骤1：抽到 %d 个隐藏域", len(hidden1))

        # 解析课程类别 Tab（每个 Tab 带独立的 xkkz_id + xkkz_xh 加密串）。
        # ⚠️ new_tabs 是「本次响应里真实解析到的」，self.tabs 是「接管后的结果」——
        #    未开放期 new_tabs 为空，self.tabs 会沿用上一次的缓存（只供界面展示）。
        new_tabs = parse_tabs(r1.text)
        self._adopt_tabs(new_tabs)
        if new_tabs:
            logger.info(
                "init 步骤1：解析到 %d 个课程 Tab → %s",
                len(new_tabs),
                ", ".join(f"{t.kklxdm}/{t.name}" for t in new_tabs),
            )

        # 本学期学分要求（最低/最高/已选）。
        # 同一份 Index 页里，不用多打一次请求；**必须**把隐藏域传进去 ——
        # 页面上显示的 `<font id="yxxfs">` 是空壳（恒为 0），真值在隐藏域 zxfs 里。
        self.credit = parse_credit(r1.text, hidden1)
        if self.credit.found:
            logger.info(
                "init 步骤1：本学期选课要求 → 最低 %s / 最高 %s / 已选 %s",
                self.credit.min_credit,
                self.credit.max_credit,
                self.credit.used_credit,
            )

        # 未开放期检查。
        # 权威判据是隐藏域 iskxk（见 page_is_open 的说明）。
        if not page_is_open(r1.text, hidden1):
            self.store["_open"] = "0"
            self._inited = True
            logger.warning(
                "当前不属于选课阶段（iskxk=%s）；已缓存 Index 参数，但无选课上下文。"
                "学分/课程 Tab 等依赖选课上下文的字段本次均不可用。",
                self.store.get("iskxk", "缺失"),
            )
            return self.store

        # --- 步骤 2：切到第一个 Tab（等价于 Display 加载） ---
        # ⚠️ 这里只认**本次响应**解析到的 Tab（`new_tabs`），不用 `self.tabs`：
        #    缓存的 Tab 在未开放期发出去只会换回框架页 / 「加密串错误」。
        #    解析不到就老实报「上下文不可用」，把缓存的 Tab 留给界面展示。
        if not new_tabs:
            self.store["_open"] = "0"
            self._inited = True
            logger.warning(
                "Index 页本次未解析到课程 Tab，选课上下文不可用"
                "（缓存里尚有 %d 个，仅供界面展示，不用于发请求）",
                len(self.tabs),
            )
            return self.store

        self.switch_tab(new_tabs[0])
        self.store["_open"] = "1"
        self._inited = True
        return self.store

    def ping(self) -> dict:
        """轻量探活：**只发一个 GET**，确认登录态（Cookie）还有效；顺带读一眼是否已开放。

        为什么必须有它：`/api/state` 是纯内存快照，前端每 3 秒轮询它**一个请求都不打教务**，
        所以「在教务网站点了退出登录 / Cookie 过期」这件事**永远不会被发现** ——
        界面会一直显示「已登录」，直到用户真的去点一次操作才发现。见 `ui/state.py`
        里关于「会话状态」的注释。

        与 `init()` 的区别（这正是它能被频繁调用的原因）：
          · 只发**一个**请求（`init()` 是 Index + Display 两个，还要解析 Tab / 学分）；
          · ⚠️ **不碰** `self.store` / `self.tabs` / `self._cur_tab` —— 那些是抢课期的
            共享上下文，探活绝不掺和进去（否则就重蹈「陈旧上下文」那类坑）；
          · 唯一的写入是 `self.clock`（每个响应本来就都会喂一次样本）。

        登录态失效时由 http 层直接抛 `SESSION_EXPIRED`（302 → 登录页），调用方据此判死。
        未开放期（200 + 「不属于选课阶段」）是**正常业务状态**，用 `expect_states` 放行。
        """
        url = self.school.url(PATH_XK_INDEX, with_gnmkdm=True, with_layout=True)
        # 探活要「快给结论」：卡住时宁可让界面早点显示连不上，也别挂着
        r = self.http.get(url, expect_states=(FailureKind.NOT_OPEN,), timeout=TIMEOUT_QUICK)
        hidden = parse_hidden_inputs(r.text)
        return {
            "is_open": page_is_open(r.text, hidden),
            "bytes": len(r.text),
        }

    def refresh_credit(self) -> CreditInfo:
        """重新拉一次选课首页，只更新学分要求（零副作用，纯 GET）。

        为什么不能只靠 init 那一次：「本学期已选学分」是**实时值** ——
        在教务网站选上一门、或退掉一门，它立刻就变。init 只在建会话时跑一次，
        拿它当最新值会一直显示旧数字。

        刻意**不**碰 self.store / self.tabs：这里只要学分那一段，
        顺手重解析会把这些上下文一起刷新，没必要（也可能引入意外）。
        """
        url = self.school.url(PATH_XK_INDEX, with_gnmkdm=True, with_layout=True)
        # 关闭期这份页面只剩提示语、没有任何学分字段，所以要允许 NOT_OPEN 穿透
        # （否则此处直接抛异常，调用方看不到「found=False」这个正常结果）。
        r = self.http.get(url, expect_states=(FailureKind.NOT_OPEN,))
        self.credit = parse_credit(r.text, parse_hidden_inputs(r.text))
        logger.info(
            "刷新学分要求：最低 %s / 最高 %s / 已选 %s（found=%s）",
            self.credit.min_credit,
            self.credit.max_credit,
            self.credit.used_credit,
            self.credit.found,
        )
        return self.credit

    def switch_tab(self, tab: Tab) -> dict[str, str]:
        """切到某课程类别 Tab。

        真实流程（Index 页 JS `queryCourse()`）：把 Tab 的 5 个参数塞进隐藏域，
        再 `$("#displayBox").load(zzxkyzb_cxZzxkYzbDisplay.html, {...})`。
        该 Display 响应会**重新下发**一批随 Tab 变化的字段：

            rwlx      —— 主修=1 / 公选=2 / 板块课=3（不一致会导致查不到课）
            xkly, bklx_id, kkbk, kkbkdj, txbsfrl, xkxskcgskg …

        所以切 Tab == 重新加载 Display == 刷新这些字段，缺一不可。
        """
        display_url = self.school.url(PATH_XK_DISPLAY, with_gnmkdm=True, with_layout=True)
        body = {
            "xkkz_id": tab.xkkz_id,
            "xszxzt": self.store.get("xszxzt", "1"),
            "kklxdm": tab.kklxdm,
            "njdm_id": tab.njdm_id or self.store.get("njdm_id", ""),
            "zyh_id": tab.zyh_id or self.store.get("zyh_id", ""),
            "kspage": "0",
            "jspage": "0",
        }
        logger.debug("switch_tab POST %s kklxdm=%s", display_url, tab.kklxdm)
        r2 = self.http.post(display_url, body)

        hidden2 = parse_hidden_inputs(r2.text)
        self.store.update(hidden2)

        # 轮次名 + 选课时间只在 **Display 页**（第 2 步）下发，Index 页没有：
        #   xklcmc = 第1轮，xkkssj / xkjssj = 选课起止时间（2026-09-30 实测，见
        #   captures/2026-09-30-retake/phase_diff.json 的「来源页=Display(第2步)」）。
        # 这里顺手把 self.credit 里缺的这两项补上 —— 顶部「学期信息」胶囊靠它显示
        # 「学年/学期/轮次（选课时间）」。
        if self.credit and self.credit.found:
            if not self.credit.round_name:
                self.credit.round_name = hidden2.get("xklcmc", "").strip()
            if not self.credit.time_text:
                _s, _e = hidden2.get("xkkssj", "").strip(), hidden2.get("xkjssj", "").strip()
                if _s or _e:
                    self.credit.time_text = f"{_s} - {_e}".strip(" -")

        # Display 专属字段（含随 Tab 变化的 rwlx / xkly 等）
        by_id = parse_id_values(r2.text)
        got = 0
        for f in DISPLAY_FIELDS:
            v = by_id.get(f) or hidden2.get(f)
            if v:
                self.store[f] = v
                got += 1

        # Tab 级字段（Display 页本身不含，必须由 Tab 参数回填）
        self.store["kklxdm"] = tab.kklxdm
        self.store["kklxmc"] = tab.name
        self.store["xkkz_id"] = tab.xkkz_id
        self.store["xkkz_xh"] = tab.xkkz_xh
        if tab.njdm_id:
            self.store["njdm_id"] = tab.njdm_id
        if tab.zyh_id:
            self.store["zyh_id"] = tab.zyh_id
        self._cur_tab = tab

        logger.info(
            "switch_tab → [%s] %s：Display %d 字节，隐藏域 %d，专属字段命中 %d/%d，rwlx=%s",
            tab.kklxdm,
            tab.name,
            len(r2.text),
            len(hidden2),
            got,
            len(DISPLAY_FIELDS),
            self.store.get("rwlx", "?"),
        )
        return self.store

    def _ensure_tab(self, tab: Tab | None) -> Tab:
        """保证当前上下文与目标 Tab 一致；不一致则重载 Display。"""
        cur = self._cur_tab
        if tab is None:
            if cur is not None:
                return cur
            if not self.tabs:
                # 同 query_courses：关闭期没有 Tab 是正常的，不该报「加密串错误」
                if not self.is_open:
                    raise XKError(FailureKind.NOT_OPEN, "当前不属于选课阶段")
                raise XKError(FailureKind.CONTEXT_INVALID, "无可用的课程 Tab")
            tab = self.tabs[0]
        if cur is None or (cur.kklxdm, cur.xkkz_xh) != (tab.kklxdm, tab.xkkz_xh):
            self.switch_tab(tab)
        return tab

    @property
    def is_open(self) -> bool:
        """选课是否已开放（由 init 判定）。"""
        return self.store.get("_open") == "1"

    @property
    def store_age_s(self) -> float | None:
        """距上一次**真正抓取**上下文的秒数；从没抓过返回 None。

        为什么要有它（P0 的核心判据）：
        `init()` 带快照缓存 —— 第二次起 `return self.store` 一个请求都不发。
        于是「上午建会话时抓的上下文」到下午依然是「当前上下文」，
        连年龄都没人问过。引擎据此判断：超过 `CONTEXT_MAX_AGE_S` 就必须
        `init(force=True)` 重抓，否则手里的 xkkz_id / do_jxb_id 可能早已作废。

        返回 None（而不是 0 或 inf）是为了让「不支持该字段的客户端」与
        「刚抓过」区分开：None = 不知道，调用方自行决定要不要保守处理。
        """
        return None if not self.store_at else time.monotonic() - self.store_at

    @property
    def semester_key(self) -> str:
        """当前选课上下文的学年学期标识，形如 `2026-2027|1`；缺失时返回 ""。

        用途：把「上一学期留下的缓存」识别出来 —— 课程 Tab 的 xkkz_id /
        xkkz_xh、清单里的课程与教学班，全都与学期轮次绑定，跨学期复用会带着
        旧加密串去查，必报「加密串错误」。宁可判为过期重新抓，也不要串用。

        名称字段（`xkxnmc`/`xkxqmc`）优先：人可读、且与页面显示一致；
        没有名称才退回内码（`xkxnm`/`xkxqm`）。
        """
        s = self.store
        year = s.get("xkxnmc") or s.get("xkxnm") or ""
        term = s.get("xkxqmc") or s.get("xkxqm") or ""
        return f"{year}|{term}" if (year or term) else ""

    def _adopt_tabs(self, new_tabs: list[Tab]) -> None:
        """接管本次 Index 页解析到的 Tab；本次解析为空则沿用缓存（不覆盖）。

        为什么要「解析为空就沿用」：Tab 只在**开放期**的 Index 页出现，
        未开放期一个都解析不到。若照抄「解析到什么就是什么」，用户每次刷新
        （或脚本每次 force init）都会看到「课程类别」被清空。

        为什么必须校验学期：Tab 内的 xkkz_id / xkkz_xh 与学期轮次绑定，
        跨学期沿用会带错加密串，所以学期一变就把缓存丢掉。

        为什么要有 `tabs_stale`：调用方（init / 界面）需要知道「这份 Tab 是
        刚抓的还是旧的」—— 旧的**只能展示**，绝不能拿去发请求。
        """
        sem = self.semester_key

        if new_tabs:
            self.tabs = new_tabs
            self.tabs_at = time.time()
            self.tabs_stale = False
            self._tabs_semester = sem
            return

        if self.tabs and self._tabs_semester and sem and self._tabs_semester != sem:
            logger.warning(
                "学期已变（%s → %s），丢弃上一学期的 %d 个课程 Tab",
                self._tabs_semester,
                sem,
                len(self.tabs),
            )
            self.tabs = []
            self.tabs_at = 0.0
            self.tabs_stale = False
            self._tabs_semester = ""
            return

        if self.tabs:
            self.tabs_stale = True
            logger.info(
                "本次 Index 页未解析到课程 Tab（未开放期属正常），"
                "沿用上次缓存的 %d 个：%s（仅供界面展示）",
                len(self.tabs),
                ", ".join(f"{t.kklxdm}/{t.name}" for t in self.tabs),
            )
        else:
            self.tabs_stale = False

    def find_tab(self, kklxdm: str) -> Tab | None:
        """按开课类型代码取 Tab。

        ⚠️ `kklxdm` **会重复**（实测两个「板块课」都是 `06`），
        所以按 kklxdm 只能取到第一个；要精确定位请用 `tab_at(index)`。
        """
        for t in self.tabs:
            if t.kklxdm == kklxdm:
                return t
        return None

    def tab_at(self, index: int) -> Tab | None:
        """按下标取 Tab（唯一可靠的方式，因为 kklxdm 会重复）。"""
        if 0 <= index < len(self.tabs):
            return self.tabs[index]
        return None

    # -------------------------------------------------------------------
    # 核心函数 3 / 4：query
    # -------------------------------------------------------------------

    def _ctx_body(self, tab: Tab) -> dict[str, str]:
        """组装查询上下文（45 个字段，全部来自 init 动态抓取）。

        铁律 #1：任何字段都不写死。store 里没有的（如条件开关字段）才用空串兜底，
        并由调用方在日志里暴露，便于定位「为什么服务端不认」。
        """
        body: dict[str, str] = {}
        missing: list[str] = []
        for f in QUERY_FIELDS:
            src = FIELD_ALIAS.get(f, f)
            v = self.store.get(src)
            if v is None:
                missing.append(f)
                v = ""
            body[f] = v
        # Tab 级覆盖：每个 Tab 有独立的 xkkz_id / xkkz_xh / 年级 / 专业
        body["kklxdm"] = tab.kklxdm
        body["xkkz_id"] = tab.xkkz_id
        body["xkkz_xh"] = tab.xkkz_xh
        if tab.njdm_id:
            body["njdm_id"] = tab.njdm_id
        if tab.zyh_id:
            body["zyh_id"] = tab.zyh_id
        if missing:
            logger.debug("查询上下文缺失字段（已用空串兜底）：%s", missing)
        return body

    def query_courses(
        self,
        keyword: str = "",
        *,
        tab: Tab | None = None,
        tab_index: int | None = None,
        kklxdm: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_STEP,
        filters: dict[str, str | list[str]] | None = None,
    ) -> tuple[list[dict], dict]:
        """查课程列表（只读）。返回 (课程行列表, meta)。

        2026-09-29 实测打通：45 个上下文字段 + kspage/jspage，
        其中 `xkkz_xh`（256 位加密串）是必需项，缺它必报「加密串错误」。

        keyword 走 `filter_list[0]`（全字段模糊搜索，可输入课程号/课程名/教师名）。

        ⭐ `filters` = **教务的高级查询条件**（就是选课页那个筛选器）。
        键就是教务 `searchBox` 插件里 conditions 的 `index`，原样作为请求字段发出去：
          · `sksj_list`   上课星期（1=周一 … 7=周日）
          · `skjc_list`   上课节次（1-15）
          · `xf_list`     学分
          · `cxbj_list`   是否重修（1=是 / 0=否）
          · `yl_list`     有无余量（1=有 / 0=无）
          · `sksjct_list` 是否与**自己的课表**冲突（1=只看冲突 / 0=只看不冲突）
          · `kcxzdm_list` 课程性质 / `kclb_id_list` 课程类别 / `jg_id_list` 学院 …
        ⚠️ 这些条件**全部由教务服务端执行**，我方一个字段都不算 ——
           因为课程列表接口根本**不返回**上课时间/教师/学院这些字段
           （见 `REAL-SCHOOL-PARAMS.md`：那 34 列里没有 sksj / jsxx / kkxymc）。
           想让客户端筛，得逐门课去查教学班，那是一次几十个请求，不可接受。
        ⚠️ 值可以是 `str`（单值）或 `list[str]`（多值）；本层负责把它编成教务要的
           **下标数组**形态（`sksj_list[0]=1&sksj_list[1]=3`），调用方**不要**自己拼。

        ⚠️ 返回的 `tmpList` 是**教学班级别**的行（同一门课的多个班是多行），
        且**不含容量** —— 容量要另外调 `query_classes()` 拿。
        """
        self.init()
        # ⚠️ 未开放期一律不发查询请求。两个理由：
        #   1) 业务上毫无收益：教务这时只会给框架页 / 「加密串错误」，查也白查；
        #   2) 高峰期的教务本来就吃紧，脚本不该往里再灌无意义的请求。
        # 为什么这条闸门必须**无条件**（不能只在「没有 Tab」时判）：
        # `self.tabs` 现在会跨未开放期保留缓存（见 `_adopt_tabs`），
        # 若把判断挂在「Tab 是否为空」上，缓存一存在就会绕过闸门直奔网络。
        # 与 submit() / cancel() 保持同一道闸门，语义一致。
        if not self.is_open:
            raise XKError(FailureKind.NOT_OPEN, "当前不属于选课阶段，无法查询课程")
        tb = (
            tab
            or (self.tab_at(tab_index) if tab_index is not None else None)
            or (self.find_tab(kklxdm) if kklxdm else None)
            or (self.tabs[0] if self.tabs else None)
        )
        if tb is None:
            # 走到这里说明**选课是开放的**，却一个 Tab 都没有 —— 那是真的丢了
            # 加密串（需要重新 init），不是「教务还没开放」。两者必须分开报，
            # 否则会把用户误导去重新登录（铁律 #10：日志要能解释原因）。
            raise XKError(
                FailureKind.CONTEXT_INVALID,
                "Index 页未解析到课程 Tab（缺少 xkkz_xh 加密串），无法查询课程",
            )
        tb = self._ensure_tab(tb)

        body = self._ctx_body(tb)
        if keyword:
            body["filter_list[0]"] = keyword
        # 分页：与页面 JS 一致（已加载数+1 / 已加载数+step）
        offset = max(page - 1, 0) * page_size
        body["kspage"] = str(offset + 1)
        body["jspage"] = str(offset + page_size)
        # 条件字段（页面开关开启时才带，实测我校 jxbzbkg=1）
        if self.store.get("jxbzbkg") == "1":
            body["jxbzb"] = self.store.get("jxbzb", "")

        # ⭐ 高级查询条件（教务筛选器）—— 原样透传，空值一律不发。
        #    ⚠️ 一定要跳过空串：教务对「参数存在但为空」和「参数不存在」的处理不同，
        #       发一个空串过去可能被当成「筛出空结果」，把整张列表清掉。
        #
        # 🔴 **多值必须用「下标数组」形式，逗号拼接会把教务打成 500**（2026-10-01 实测）：
        #    · `sksj_list=1,3`            → 教务返回「错误提示」HTML 页（HTTP 200 但非 JSON）
        #    · `sksj_list[0]=1&sksj_list[1]=3` → ✅ 正常筛选
        #    权威依据 = 教务 `jquery.searchbox.contact-min.js::getConditions()`：
        #        `E[G.index + "[" + H + "]"] = K.key`
        #    它是**顺序下标**（0,1,2…），不是 jQuery 默认的 `a[]=1&a[]=2`，两者别混。
        #    单值用裸键 `sksj_list=1` 实测同样可用（Spring 会把它绑成单元素 List），
        #    但为与教务前端保持一致、也免得多一套分支，这里**统一走 `[i]` 下标**。
        #    ⚠️ `filter_list[0]`（关键字）已经是下标形式，别在这里重复处理。
        applied: dict[str, str] = {}
        for k, v in (filters or {}).items():
            if v in (None, ""):
                continue
            vals = [str(x) for x in v if str(x) != ""] if isinstance(v, (list, tuple)) else [str(v)]
            if not vals:
                continue
            for i, x in enumerate(vals):
                body[f"{k}[{i}]"] = x
            applied[k] = ",".join(vals)

        url = self.school.url(PATH_COURSE_LIST)
        resp = self.http.post(url, body)
        data = resp.json_or_none()

        if not isinstance(data, dict):
            k = _classify_plain(resp)
            raise XKError(k, f"课程查询返回非 JSON（HTTP {resp.status}）", raw=resp.text[:800])

        flag = str(data.get("flag", ""))
        msg = str(data.get("msg") or "")
        if flag != "1":
            from core.errors import classify_text

            kind = classify_text(msg) or (FailureKind.CONTEXT_INVALID if not msg else FailureKind.UNKNOWN)
            logger.warning("query_courses 失败：flag=%s msg=%r", flag, msg)
            raise XKError(kind, msg or f"课程查询失败（flag={flag}）", raw=resp.text[:800])

        rows = data.get("tmpList") or data.get("rows") or []
        meta = {
            "kklxdm": tb.kklxdm,
            "kklxmc": tb.name,
            "xkkz_id": tb.xkkz_id,
            "sfxsjc": data.get("sfxsjc", ""),
            "kspage": body["kspage"],
            "jspage": body["jspage"],
            # 回显本次真正生效的筛选条件（空 dict = 没筛）——
            # 界面上要能说清「这个结果是被什么条件筛出来的」，否则用户看到
            # 列表突然变少会以为教务出问题了。
            "filters": applied,
        }
        logger.info(
            "query_courses(%r) Tab=%s/%s：%d 条%s",
            keyword,
            tb.kklxdm,
            tb.name,
            len(rows),
            f"｜筛选 {applied}" if applied else "",
        )
        return rows, meta

    def query_classes(
        self,
        kch_id: str,
        *,
        tab: Tab | None = None,
        tab_index: int | None = None,
        kklxdm: str | None = None,
        cxbj: str = "0",
        fxbj: str = "0",
    ) -> list[Jxb]:
        """查某课程的教学班（只读）。返回带 do_id 的教学班列表。

        2026-09-29 实测：46 个字段（见 config.CLASS_FIELDS），不需要 xkkz_xh，
        但需要 Tab 级的 rwlx / xkly / bklx_id —— 所以先 `_ensure_tab()` 对齐 Tab。

        `cxbj` / `fxbj` 来自课程列表行的同名字段（重修标记 / 辅修标记），
        传错会导致服务端按错误视角返回。
        """
        self.init()
        # 同 query_courses：未开放期无条件不发请求（缓存的 Tab 会绕过「无 Tab」判断）
        if not self.is_open:
            raise XKError(FailureKind.NOT_OPEN, "当前不属于选课阶段，无法查询教学班")
        tb = (
            tab
            or (self.tab_at(tab_index) if tab_index is not None else None)
            or (self.find_tab(kklxdm) if kklxdm else None)
            or self._cur_tab
        )
        if tb is None:
            if not self.tabs:
                # 选课开放却一个 Tab 都没有 = 真的丢了加密串，不是「教务没开放」
                raise XKError(FailureKind.CONTEXT_INVALID, "无可用的课程 Tab，无法查询教学班")
            tb = self.tabs[0]
        tb = self._ensure_tab(tb)

        body: dict[str, str] = {}
        for f in CLASS_FIELDS:
            src = FIELD_ALIAS.get(f, f)
            body[f] = self.store.get(src, "")
        body["kch_id"] = kch_id
        body["cxbj"] = cxbj
        body["fxbj"] = fxbj
        # Tab 级兜底覆盖
        body["kklxdm"] = tb.kklxdm
        body["xkkz_id"] = tb.xkkz_id
        if tb.njdm_id:
            body["njdm_id"] = tb.njdm_id
        if tb.zyh_id:
            body["zyh_id"] = tb.zyh_id

        url = self.school.url(PATH_CLASS_INFO)
        # ⭐ 关键路径：这一发直接决定「能不能拿到可提交的令牌」，卡住就是干等。
        resp = self.http.post(url, body, timeout=TIMEOUT_CRITICAL)
        data = resp.json_or_none()
        if not isinstance(data, list):
            if isinstance(data, dict) and data.get("msg"):
                from core.errors import classify_text

                raise XKError(
                    classify_text(str(data["msg"])) or FailureKind.UNKNOWN,
                    str(data["msg"]),
                    raw=resp.text[:600],
                )
            logger.warning("query_classes(%s)：返回非列表 %s", kch_id, resp.text[:120])
            return []

        out = []
        for it in data:
            do_id = str(it.get("do_jxb_id", "") or "")
            if not do_id or do_id == "undefined":
                # gdep 踩坑：偶尔下发字面量 "undefined"
                continue
            out.append(
                Jxb(
                    jxb_id=str(it.get("jxb_id", "")),
                    do_id=do_id,
                    # ⚠️ 该接口不返回 kcmc，课程名要靠课程列表补齐
                    kcmc=str(it.get("kcmc", "")),
                    jsxx=str(it.get("jsxx", "")),
                    sksj=str(it.get("sksj", "")),
                    jxdd=str(it.get("jxdd", "")),
                    jxbrl=str(it.get("jxbrl", "")),
                    yxzrs=str(it.get("yxzrs", "")),
                    jxms=str(it.get("jxms", "")),
                    kkxymc=str(it.get("kkxymc", "")),
                    xf=str(it.get("xf", "")),
                    dsfrl=str(it.get("dsfrl", "")),
                    sjsfsj=str(it.get("sjsfsj", "")),
                )
            )
        logger.info(
            "query_classes(%s) Tab=%s：%d 个有效教学班（原始 %d 行）",
            kch_id,
            tb.kklxdm,
            len(out),
            len(data),
        )
        return out

    def query_selected(self) -> list[dict]:
        """查已选课程（只读）。

        2026-09-29 CDP 实录：`zzxkyzb_cxZzxkYzbChoosedDisplay.html`，
        11 个参数（jg_id / zyh_id / njdm_id / zyfx_id / bh_id / xz / ccdm /
        xqh_id / xkxnm / xkxqm / xkly），前 10 个来自 Index 页动态抓取。

        ⚠️ 唯独 `xkly`（选课来源）**必须写死 "0"**，不能从 store 取。
        2026-09-29 抓包对账结论（三接口横向比对同一份抓包）：
            PartDisplay（课程列表）      xkly = 0 和 1  ← 真·随 Tab 变
            cxJxbWithKch（教学班）       xkly = 0 仅此
            ChoosedDisplay（本接口）     xkly = 0 仅此 ← 恒为 0，与 Tab 无关
        「已选课程」是跨全部来源的汇总视图，JS 里就是一个固定值；而 `xkly`
        在 switch_tab() 时会被 Display 页刷成主修课程的 "1"（我校默认 Tab），
        一旦此前切过任意 Tab，从 store 取就会发出 xkly=1 → 服务端查不到已选。
        这也是本项目在 mock 回放里稳定 miss 的唯一根因。
        """
        self.init()
        url = self.school.url(PATH_SELECTED)
        body = {
            f: self.store.get(FIELD_ALIAS.get(f, f), "")
            for f in (
                "jg_id", "zyh_id", "njdm_id", "zyfx_id", "bh_id",
                "xz", "ccdm", "xqh_id", "xkxnm", "xkxqm",
            )
        }
        body["xkly"] = "0"  # 见 docstring：恒为 0，绝不能被 Tab 级字段污染
        # 关键路径：退课（`ui/app.py::/api/drop`）要在**同一次查询的响应内部**取刚下发的
        # 一次性令牌再立刻提交，所以这一发也在关键路径上。
        resp = self.http.post(url, body, timeout=TIMEOUT_CRITICAL)
        data = resp.json_or_none()
        if isinstance(data, list):
            logger.info("query_selected：%d 门", len(data))
            return data
        rows = (data or {}).get("tmpList") if isinstance(data, dict) else None
        if isinstance(rows, list):
            logger.info("query_selected：%d 门", len(rows))
            return rows
        logger.warning("query_selected：返回非列表 %s", resp.text[:120])
        return []

    # -------------------------------------------------------------------
    # 学生学业情况统计查询（独立模块 N551247，与选课 N253512 无关）
    # -------------------------------------------------------------------

    def query_academic(self) -> dict:
        """查「学生学业情况统计」（只读，零副作用）。

        2026-10-01 CDP 实录（menu → 学生学业情况统计查询，gnmkdm=N551247）：
        两个接口，学生端（jsdm=xs）请求体都为空 `{}`，服务端按登录态返回本人：

        1. `POST xyyjgl/xyyj_cxJhyqKcxzList.html` → {"kcxzList": [...]}
           每项 = 课程性质的「要求学分」口径：KCXZMC(性质名) / KCXZDM(代码) /
           XXXF(要求学分) / RN(序号，1 起)。列表顺序即前端列的排列顺序。

        2. `POST xyyjgl/xyyj_cxXsxyqkglIndex.html?doType=query` → 标准 jqGrid 分页：
           {"items":[{ ... }], "totalCount":N}
           每个学生一条，字段含：
             · 基础：XH 学号 / XM 姓名 / XBMC 性别 / JGMC 学院 / ZYMC 专业 /
                     NJMC 年级 / BJMC 班级 / XQMC 校区
             · 学分：YQZDXF 毕业要求学分 / HDXF 获得总学分
             · 分课程性质：KCXZYQXF{n} 要求学分 / KCXZXF{n} 获得学分 /
                          KCXZZXXF{n} 在修学分（n 对应 kcxzList 的 RN）

        返回结构（规整后，供 ui 层下发）：
            {"kcxz": [{name, code, rn, require}], "students": [student_row...]}
        """
        base = self.school
        kcxz_url = base.url(PATH_XYQK_KCXZ, gnmkdm=base.gnmkdm_xyqk)
        query_url = base.url(PATH_XYQK_QUERY, gnmkdm=base.gnmkdm_xyqk) + "&doType=query"

        kcxz: list[dict] = []
        try:
            r1 = self.http.post(kcxz_url, {}, timeout=TIMEOUT_NORMAL)
            d1 = r1.json_or_none()
            if isinstance(d1, dict) and isinstance(d1.get("kcxzList"), list):
                kcxz = [
                    {
                        "name": (it.get("KCXZMC") or "").strip(),
                        "code": (it.get("KCXZDM") or "").strip(),
                        "rn": int(it.get("RN") or 0),
                        "require": it.get("XXXF"),
                    }
                    for it in d1["kcxzList"]
                ]
                logger.info("学业情况：解析到 %d 个课程性质", len(kcxz))
        except Exception as e:  # 课程性质列表失败不致命，主查询仍可给基础信息
            logger.warning("学业情况：课程性质列表读取失败 %s", e)

        students: list[dict] = []
        try:
            r2 = self.http.post(query_url, {}, timeout=TIMEOUT_NORMAL)
            d2 = r2.json_or_none()
            if isinstance(d2, dict) and isinstance(d2.get("items"), list):
                students = d2["items"]
                logger.info("学业情况：查询到 %d 名学生", len(students))
        except Exception as e:
            logger.warning("学业情况：主查询失败 %s", e)

        return {"kcxz": kcxz, "students": students}

    # -------------------------------------------------------------------
    # 核心函数 4 / 4：submit
    # -------------------------------------------------------------------

    def precheck_conflict(self, kch_id: str, do_id: str) -> dict:
        """时间冲突预检（零副作用）。

        2026-09-29 实录：POST `zzxkyzb_cxCtKcZyZzxkYzb.html`
        body = {jxb_ids, xkxnm, xkxqm, kch_id, sfyxsksjct}

        flag 语义（来自 `zzxkYzbChoosedZy.js:1196-1229`）：
            "1" 无冲突
            "2" 与已选教学班上课时间冲突
            "3" 与已选教学班存在同半天跨校区情况
            "4" 2 与 3 同时成立
            "5" 需申请教务处理
        """
        self.init()
        url = self.school.url(PATH_CONFLICT, with_gnmkdm=False)
        body = {
            "jxb_ids": do_id,
            "xkxnm": self.store.get("xkxnm", ""),
            "xkxqm": self.store.get("xkxqm", ""),
            "kch_id": kch_id,
            "sfyxsksjct": self.store.get("sfyxsksjct", "0"),
        }
        resp = self.http.post(url, body, timeout=TIMEOUT_CRITICAL)
        data = resp.json_or_none()
        if isinstance(data, dict):
            return data
        return {"flag": "", "msg": resp.text[:200]}

    def submit(
        self,
        kch_id: str,
        do_id: str,
        *,
        kcmc: str = "",
        kklxdm: str | None = None,
        qz: str = "0",
        xkbj: str = "0",
        cxbj: str = "0",
        jcxx_id: str = "",
    ) -> dict:
        """正式选课（⚠️ 真实占位）。

        字段清单 2026-09-29 选课开放期实测确认，共 19 个（见 config.SUBMIT_FIELDS）。
        来源：模块 JS `saveCourse()` 与「选课确认」两处 POST，组装字段完全一致。

        返回：{"success": bool, "flag": str, "msg": str, "kind": FailureKind|None}
        """
        self.init()
        # ---- 本地闸门（**只有一个入口，不提供绕过开关**）----
        # 关闭期一个请求都不发：教务这时只会给框架页/加密串错误，白跑一趟；
        # 而且退课侧是不可逆的写，宁可不给机会（见 cancel 的同款闸门）。
        #
        # ❌ 曾经有过一个 `blind=True` 口子（跳过本闸门），**2026-09-30 用户否决并删除**。
        # 当时的理由听起来很美：「教务未开放时按钮是禁的，但那是前端 JS 的旧状态，
        # 手里的旧令牌说不定还能被受理，重抓上下文要 3 跳 ≈ 0.9 s，不如赌一发」。
        # 拆掉的真实原因：
        #   · **立即模式下会提前很久打出去** —— 用户 12:00 点「一键抢课」、教务 12:30 才开，
        #     那一发就是白打，还照样要等重抓路径；
        #   · **能「中」的窗口窄到没有意义** —— 它在「本地认为未开放、服务端其实已开放」
        #     时才有收益。而定时模式的预热相**每一轮都刷上下文**（见 engine/runner.py
        #     `_prewarm`），一旦服务端开放，`is_open` 立刻变 True、令牌也会被重解析，
        #     那时闸门本来就放行，盲发根本不会触发；
        #   · 代价却是每次未开放都吃一发注定被拒的写请求（还可能被教务记一笔）。
        # 所以「卡在开放瞬间」这件事，**只靠重新读一遍上下文来解决**：
        # `submit()` 返回 NOT_OPEN 时上层会重抓上下文再试（runner 的 NOT_OPEN 分支），
        # 那才是正确且唯一的路径。
        if not self.is_open:
            return {
                "success": False,
                "flag": "",
                "msg": "当前不属于选课阶段",
                "kind": FailureKind.NOT_OPEN,
            }

        url = self.school.url(PATH_SUBMIT)

        # ⚠️ 必须用**当前 Tab**的 xkkz_id / kklxdm。
        # 不能优先取 firstXkkzId —— 那是第一个 Tab 的值，提交非首个 Tab 的课会带错轮次 id。
        xkkz = self.store.get("xkkz_id") or self.store.get("firstXkkzId", "")
        kklx = kklxdm or self.store.get("kklxdm") or self.store.get("firstKklxdm", "")
        if not xkkz:
            raise XKError(
                FailureKind.CONTEXT_INVALID,
                "缺少 xkkz_id（选课轮次 id），请先重新初始化",
            )

        # sxbj 由三个容量控制开关推导（模块 JS 的逻辑：任一为 "1" 则为 "1"）
        rlkz = self.store.get("rlkz", "0")
        cdrlkz = self.store.get("cdrlkz", "0")
        rlzlkz = self.store.get("rlzlkz", "0")
        sxbj = "1" if "1" in (rlkz, cdrlkz, rlzlkz) else "0"

        body = {
            # ---- 核心：必须是 do_jxb_id ----
            "jxb_ids": do_id,
            "kch_id": kch_id,
            "kcmc": kcmc,
            # ---- 容量/规则开关（动态） ----
            "rwlx": self.store.get("rwlx", ""),
            "rlkz": rlkz,
            "cdrlkz": cdrlkz,
            "rlzlkz": rlzlkz,
            "sxbj": sxbj,
            "xxkbj": xkbj,
            "cxbj": cxbj,
            "qz": qz,
            # ---- 轮次与学生上下文（动态） ----
            "xkkz_id": xkkz,
            "njdm_id": self.store.get("njdm_id", ""),
            "zyh_id": self.store.get("zyh_id", ""),
            "kklxdm": kklx,
            "xklc": self.store.get("xklc", ""),
            "xkxnm": self.store.get("xkxnm", ""),
            "xkxqm": self.store.get("xkxqm", ""),
            "jcxx_id": jcxx_id,
        }

        logger.info("submit：kch_id=%s do_id=%s… 字段数=%d", kch_id, do_id[:24], len(body))
        # ⭐ 关键路径里最关键的一发：它是**写请求**，卡住既拖时间又让结果未知。
        # 超时值取 `TIMEOUT_CRITICAL` —— 「结果未知」由上层用 flag="6" 兜（见文件头注释）。
        resp = self.http.post(url, body, timeout=TIMEOUT_CRITICAL)
        data = resp.json_or_none()

        if not isinstance(data, dict):
            # 非 JSON：可能是加密串错误或 HTML
            kind = _classify_plain(resp)
            return {"success": False, "flag": "", "msg": resp.text[:200], "kind": kind}

        flag = str(data.get("flag", ""))
        msg = str(data.get("msg") or "")
        success = flag in FLAG_SUCCESS

        if success:
            logger.info("submit 成功：flag=%s%s", flag, "（免费学分已达上限）" if flag == "3" else "")
            return {"success": True, "flag": flag, "msg": msg, "kind": None}

        kind = _classify_submit_failure(flag, msg)
        info = parse_full_msg(msg) if kind is FailureKind.FULL else None
        # 铁律 #10：日志必须能解释「为什么没抢到」
        if info:
            logger.warning(
                "submit 满员：flag=%s 教学班=%s 已选=%s 本轮已选=%s（辅教学班标志=%s）",
                flag, info["jxb_id"], info["yxzrs"], info["blyxrs"], info["fzjxb"],
            )
        else:
            logger.warning("submit 失败：flag=%s msg=%r kind=%s", flag, msg, kind)
        return {
            "success": False,
            "flag": flag,
            "msg": msg,
            "kind": kind,
            "full_info": info,
        }

    def cancel(self, kch_id: str, do_id: str) -> dict:
        """退课（⚠️ 不可逆）。

        2026-09-29 实录：POST `zzxkyzb_tuikBcZzxkYzb.html`
        body = {kch_id, jxb_ids(=do_jxb_id), xkxnm, xkxqm, txbsfrl}

        返回**纯字符串**（不是 JSON 对象）：
            "1" 成功 / "2" 服务器繁忙 / "3" 未知异常 / "4" 非法访问 / "5" 校验不通过（需刷新重试）
        """
        self.init()
        # 铁律 #2 + 退课是本项目**唯一不可逆**的写操作：没有选课上下文时宁可不给机会。
        # 关闭期 store 里没有 xkxnm / xkxqm，硬发出去只会得到教务的 "3"（未知异常），
        # 既污染日志也白跑一次不可逆请求。与 submit() 保持同一道闸门。
        if not self.is_open:
            return {
                "success": False,
                "code": "",
                "msg": "当前不属于选课阶段，退课请求未发出",
            }
        url = self.school.url(PATH_CANCEL)
        body = {
            "kch_id": kch_id,
            "jxb_ids": do_id,
            "xkxnm": self.store.get("xkxnm", ""),
            "xkxqm": self.store.get("xkxqm", ""),
            "txbsfrl": self.store.get("txbsfrl", "0"),
        }
        # ⚠️ 退课是本项目**唯一不可逆**的写操作，同样走关键路径超时：
        # 卡住时快速放掉，让用户立刻看到「结果未知，请刷新确认」，而不是干等 30 秒。
        resp = self.http.post(url, body, timeout=TIMEOUT_CRITICAL)
        raw = (resp.text or "").strip().strip('"')
        MSG = {
            "1": "退课成功",
            "2": "退课失败：服务器繁忙",
            "3": "退课失败：出现未知异常",
            "4": "退课失败：非法访问",
            "5": "退课失败：校验不通过，请刷新网页后重试",
        }
        return {"success": raw == "1", "code": raw, "msg": MSG.get(raw, raw[:200])}


def parse_full_msg(msg: str) -> dict[str, str] | None:
    """解析满员返回的 msg（2026-09-29 实测格式）。

        辅教学班标志, 教学班id, 已选人数, 本轮已选人数
    例："0,5B8FE9E7058748A6E06586160CCC45E0,46,"
    （第 4 段「本轮已选人数」在实测中为空串）

    用途：满员时把真实人数回写进日志 —— 铁律 #10「日志必须能解释为什么没抢到」。
    """
    parts = (msg or "").split(",")
    if len(parts) < FULL_MSG_PARTS:
        return None
    return {
        "fzjxb": parts[0].strip(),    # 辅教学班标志："1" 表示满的是辅教学班
        "jxb_id": parts[1].strip(),
        "yxzrs": parts[2].strip(),    # 已选人数
        "blyxrs": parts[3].strip(),   # 本轮已选人数
        "raw": msg,
    }


def _classify_plain(resp: RawResponse):
    from core.errors import classify_text

    k = classify_text(resp.text)
    return k if k is not FailureKind.UNKNOWN else FailureKind.UNKNOWN


def _classify_submit_failure(flag: str, msg: str) -> FailureKind:
    """提交失败语义判定（2026-09-29 实测确认 flag 取值）。

    ⚠️ **`flag="6"` 必须最先判**，不能先看文案：

        「该教学班已选中」= **我们押的这个班已经在选课结果里** → `ALREADY_IN_CLASS`
                               （上层按「已抢到」处理，见 engine/runner.py）
        「只能选一个教学班」= 这门课已经有了**别的**班 → `ALREADY_TAKEN`（真失败）

    两者只差一个字，判断结果却相反。`flag` 是**结构化信号**，比文案可靠得多
    （文案会随教务版本变、还会被 msg 里其它字样干扰），所以它优先。

    这条判定还是「写请求超时但实际已生效」的**唯一发现路径**：提交超时 → 重试同一发 →
    教务发现该班已在名下 → 回 flag=6 → 判定已抢到。若不认它，就会把选上的课
    报成「失败」（2026-10-01 修）。
    """
    from core.errors import classify_text

    if flag == FLAG_ALREADY_TAKEN:
        return FailureKind.ALREADY_IN_CLASS

    if msg:
        k = classify_text(msg)
        if k is not FailureKind.UNKNOWN:
            return k

    # 满员：flag="-1"，msg = "辅教学班标志,教学班id,已选人数,本轮已选人数"
    if flag == FLAG_FULL:
        return FailureKind.FULL
    return FailureKind.UNKNOWN
