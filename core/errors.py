"""错误语义映射。

铁律 #10：日志必须能解释「为什么没抢到」。
本模块把教务的各种返回（HTTP 状态 / JSON / HTML 片段）翻译成明确的语义枚举，
让上层永远不需要解析中文提示。

来源：
- 我校实测错误文案（TARGET-API.md）
- 三家交叉的错误分类（CROSS-ANALYSIS.md）
- GCCTool 的三态会话判定（不跟随 302）
"""

from __future__ import annotations

from enum import Enum


class FailureKind(str, Enum):
    """失败原因的分类。每个值都对应一种明确的处置策略。"""

    # --- 会话类：需要重新取凭据 ---
    SESSION_EXPIRED = "session_expired"      # 302 跳登录页 / 返回登录表单
    CONTEXT_INVALID = "context_invalid"      # 「加密串错误」→ 需重新 init

    # --- 状态类：属于正常业务态，不是错误 ---
    NOT_OPEN = "not_open"                    # 「当前不属于选课阶段」
    MAINTENANCE = "maintenance"              # 「系统维护」

    # --- 抢课结果类 ---
    # 满员。
    #
    # 🔴 **2026-10-01 起：满员 = 终态失败，一次都不重试**（用户明确要求）。
    # 理由：名额不会在 800ms 内自己冒出来，重试同一发只是白烧尝试额度与配额
    # （教务对「选课频率过高」是会计数的）；而「等有人退课再抢」是**另一个时间尺度**
    # 的事 —— 那是「蹲课」功能的职责（小时级、走 `budget_s`），不该由抢课循环兼任。
    # 所以这里只保留「解释为什么没抢到」的职责：把服务端下发的真实人数写进 last_msg
    # （见 `engine/runner.py::_tick_submit` + `core/client.py::parse_full_msg`）。
    #
    # ⚠️ 它与 `TOO_FREQUENT`（频率过高）的处理**刻意不同**：后者是「教务让我慢点」，
    # 慢一点再试是有意义的，所以仍然可重试。
    FULL = "full"
    CONFLICT = "conflict"                    # 时间冲突
    ALREADY_TAKEN = "already_taken"          # 同课程只能选一个教学班
    # ⭐ 「**这个**教学班已经在你名下」（提交返回 `flag="6"`）。
    #
    # ⚠️ 它看起来像 ALREADY_TAKEN，语义却**正好相反**，绝对不能合并：
    #   · `ALREADY_IN_CLASS`（flag=6）= 「**我们押的那个班**已经在选课结果里」。
    #     官方前端 JS（`zzxkYzbChoosedZy.js:1367`）对它的处理就是一句注释：
    #     「该教学班已选中，刷新页面可见！」—— 既不是错误、也不是「换了别的班」，
    #     而是**这一项的目标已经达成**。
    #     它还是「提交超时但实际生效」的唯一发现路径：写请求超时后重试同一发，
    #     教务发现该班已在名下，回的就是 6。所以必须按「已抢到」处理。
    #   · `ALREADY_TAKEN` = 文案「只能选一个教学班」，指的是**同一门课的别的班**
    #     已经选上了 —— 那一项并没有达成目标，该判失败。
    #
    # 合并两者 → 就会在「提交其实成功、只是响应没回来」时对用户说「失败」（撒谎）。
    # 2026-10-01 拆开，见 `core/client.py::_classify_submit_failure` 的注释。
    ALREADY_IN_CLASS = "already_in_class"
    QUOTA_EXCEEDED = "quota_exceeded"        # 超出类别门次限制
    TOO_FREQUENT = "too_frequent"            # 选课频率过高
    # 前置条件不满足（例如教务当前不开放退课 / 缺少必要上下文）。
    # 属于「请求根本没资格发出去」，不是教务给了一个失败结果。
    PRECONDITION = "precondition"

    # --- 传输类：请求根本没走到教务 ---
    # ⚠️ 必须与 UNKNOWN 分开（2026-09-30）：两者的**处置完全相反**。
    #   · NETWORK = 连接超时 / 读超时 / 连接被重置 —— 纯粹是「这次没问到」，
    #     对端多半只是卡了一下，**重试就有机会**；
    #   · UNKNOWN = 教务**明确回了一个我们没归类的原因**
    #     （例如「超过本学期最高选课学分限制，不可选！」）—— 再试一百次也是同一句话，
    #     重试只会白烧配额。
    # 以前两者都归 UNKNOWN，于是「一次网络抖动」会和「学分超限」享受同等待遇：
    # 整项直接判死、一次都不重试。抢课窗口里丢一门课的代价太大。
    NETWORK = "network"

    UNKNOWN = "unknown"                      # 未识别，需原文落盘

    def retryable(self) -> bool:
        """是否值得重试（会话类、频率类、网络类可恢复；满员与配额类不可）。

        ⚠️ `ALREADY_IN_CLASS` 刻意**不在**这里：它是「已经拿到了」，重试毫无意义
        （而且上层会把它当成功处理，根本走不到「该不该重试」这一步）。
        它不可重试的另一个含义是安全的 —— 万一将来有人漏判了这条分支，
        落到 `not retryable()` 那一步也只是「停下来」，不会变成无限重发。

        🔴 `FULL` **也不在**（2026-10-01 用户要求）：满员是**终态失败**。
        它在 `_tick_submit` 里会走到「不可重试 → GIVE_UP（state=FAILED）」那条路，
        日志文案是「不可重试：教学班已满（已选 N 人）」。
        历史上它曾经可重试（当「蹲守」用），改动原因见 `FailureKind.FULL` 的注释。
        ⚠️ 别因为「万一有人退课呢」又把它加回来 —— 那个诉求属于**蹲课**功能，
        在这里加回去等于让抢课循环替蹲课值班，会把尝试额度烧在不会变的事情上。
        """
        return self in {
            FailureKind.SESSION_EXPIRED,
            FailureKind.CONTEXT_INVALID,
            FailureKind.CONFLICT,
            FailureKind.TOO_FREQUENT,
            FailureKind.MAINTENANCE,
            FailureKind.NETWORK,
        }


class XKError(Exception):
    """核心层统一异常。永远携带 kind，方便上层决策。"""

    def __init__(self, kind: FailureKind, message: str, raw: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.raw = raw  # 铁律 #8：首次出现的异常原文落盘

    def __str__(self) -> str:
        return f"[{self.kind.value}] {super().__str__()}"


# ---------------------------------------------------------------------------
# 文案 → 语义 的映射表
# ---------------------------------------------------------------------------

# 关键词 → 语义（按优先级从上到下匹配，越靠前越优先）
_KEYWORD_RULES: tuple[tuple[str, FailureKind], ...] = (
    # 会话
    ("加密串错误", FailureKind.CONTEXT_INVALID),
    ("JAS-02", FailureKind.CONTEXT_INVALID),
    # 状态
    ("不属于选课阶段", FailureKind.NOT_OPEN),
    ("不在选课时间", FailureKind.NOT_OPEN),
    ("当前未开放选课", FailureKind.NOT_OPEN),
    ("系统维护", FailureKind.MAINTENANCE),
    ("系统正在维护", FailureKind.MAINTENANCE),
    # 结果
    ("频率过高", FailureKind.TOO_FREQUENT),
    ("只能选一个教学班", FailureKind.ALREADY_TAKEN),
    ("不可再选", FailureKind.ALREADY_TAKEN),
    # 兜底：正常路径上「这个班已选中」是**靠 flag="6"** 认出来的（结构化信号优先），
    # 万一教务把它塞进 flag=0 的 msg 里（文案类失败都走 flag=0），这里得接得住 ——
    # 否则它会落进 UNKNOWN → 不可重试 → 把「其实已经抢到」宣判成「失败」。
    ("该教学班已选中", FailureKind.ALREADY_IN_CLASS),
    ("教学班已选中", FailureKind.ALREADY_IN_CLASS),
    ("最高选课门次限制", FailureKind.QUOTA_EXCEEDED),
    ("门次限制", FailureKind.QUOTA_EXCEEDED),
    ("时间冲突", FailureKind.CONFLICT),
    ("冲突", FailureKind.CONFLICT),
    ("人数已满", FailureKind.FULL),
    ("已满", FailureKind.FULL),
    ("容量", FailureKind.FULL),
    # ⚠️ 刻意**不**收录「超过本学期最高选课学分限制」一类的**学分**上限文案。
    # 它归 UNKNOWN → 不可重试 → 直接终止，与期望行为一致（2026-09-29 用户确认）。
    # 别看到「未识别的返回」就顺手补一条规则 —— 教务已经明确说了原因，重试没意义；
    # 而且上面两条「门次限制」讲的是**门数**，与**学分**不是一回事，硬套会让日志说谎。
)


def classify_text(text: str) -> FailureKind:
    """从任意返回体（JSON 串 / HTML 片段 / 纯文本）判断失败语义。"""
    if not text:
        return FailureKind.UNKNOWN
    for keyword, kind in _KEYWORD_RULES:
        if keyword in text:
            return kind
    return FailureKind.UNKNOWN


def is_login_page(html: str) -> bool:
    """判断返回体是不是登录页（会话失效的最可靠标志）。"""
    if not html:
        return False
    return "login_slogin" in html or 'id="frmLogin"' in html or "用户登录" in html[:2000]


# 只用于「整页响应」判定的关键词。
#
# ⚠️ 这里**刻意不含**业务结果类关键词（冲突 / 满员 / 门次 / 频率）。
# 原因：选课页面上「时间冲突」往往只是列表里的一个列标题或提示文案，
# 对整页做关键词匹配会把正常页面误判成失败（实测踩过：36KB 的 Index 页
# 因为含「冲突」二字被判成 CONFLICT）。
# 业务结果只应在**提交/查询的 JSON 返回**上判定，走 classify_text。
_STATE_RULES: tuple[tuple[str, FailureKind], ...] = (
    ("加密串错误", FailureKind.CONTEXT_INVALID),
    ("JAS-02", FailureKind.CONTEXT_INVALID),
    ("不属于选课阶段", FailureKind.NOT_OPEN),
    ("不在选课时间", FailureKind.NOT_OPEN),
    ("当前未开放选课", FailureKind.NOT_OPEN),
    ("系统维护", FailureKind.MAINTENANCE),
    ("系统正在维护", FailureKind.MAINTENANCE),
)


def classify_state(body: str) -> FailureKind | None:
    """只看「会话/状态类」关键词，用于整页响应判定。"""
    if not body:
        return None
    for keyword, kind in _STATE_RULES:
        if keyword in body:
            return kind
    return None


def classify_response(status: int, body: str, location: str | None = None) -> FailureKind | None:
    """综合判断一次响应的语义。

    返回 None 表示「没有被判定为失败」，即看起来是正常响应。

    判定顺序（GCCTool 三态思路，我校实测吻合）：
      - 3xx 且 Location 指向登录页 → SESSION_EXPIRED
      - 5xx → MAINTENANCE（我校缺 gnmkdm 时即 500 + 系统维护页）
      - 200 且含状态类关键词（不属于选课阶段 / 系统维护 / 加密串错误）→ 对应语义
      - 200 且是登录页 HTML → SESSION_EXPIRED
      - 其余 → None（正常）
    """
    if status in (301, 302, 303, 307, 308):
        if location and ("login_slogin" in location or "slogin" in location):
            return FailureKind.SESSION_EXPIRED
        return None

    if status >= 500:
        # 我校特例：缺 gnmkdm 时返回 500 + 「系统维护页面」
        return FailureKind.MAINTENANCE if "维护" in (body or "") else FailureKind.UNKNOWN

    kind = classify_state(body)
    if kind is not None:
        return kind

    if is_login_page(body):
        return FailureKind.SESSION_EXPIRED

    return None


# 给用户看的中文说明（日志/界面用）
KIND_LABEL: dict[FailureKind, str] = {
    FailureKind.SESSION_EXPIRED: "登录态失效，需重新提供 Cookie",
    FailureKind.CONTEXT_INVALID: "选课上下文失效（加密串错误），需重新初始化",
    FailureKind.NOT_OPEN: "当前不在选课阶段",
    FailureKind.MAINTENANCE: "教务系统维护中",
    FailureKind.FULL: "教学班已满",
    FailureKind.CONFLICT: "时间冲突",
    FailureKind.ALREADY_TAKEN: "同一门课只能选一个教学班",
    # 注意措辞：不能简写成「已选中」—— 它说的是**这一个教学班**已经在你的选课结果里，
    # 而不是「这门课已经有了别的班」。界面上这两句话的意思完全不同。
    FailureKind.ALREADY_IN_CLASS: "该教学班已在你名下（目标已达成）",
    FailureKind.QUOTA_EXCEEDED: "超出本类别选课门次限制",
    FailureKind.TOO_FREQUENT: "选课频率过高，需降速",
    FailureKind.PRECONDITION: "前置条件不满足，请求未发出",
    FailureKind.NETWORK: "网络异常或超时（请求没走到教务），可重试",
    FailureKind.UNKNOWN: "未识别的返回（原文已留存）",
}
