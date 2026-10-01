"""HTTP 会话封装。

只做三件事：
1. 统一带 Cookie / UA / Referer
2. 把各种响应统一成 RawResponse（状态 + 文本 + 最终 URL），便于三态判定
3. 拦截明显异常（未登录 / 维护），抛出带语义的 XKError

额外副作用（免费收益）：顺手用响应的 Date 头喂 ServerClock，
每次请求都在收窄「服务器 - 本地」时钟偏差，为定时开抢提供卡点依据。

不做任何业务判断 —— 业务判断属于 client.py。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from core.clock import ServerClock, parse_http_date
from core.config import (
    Credential,
    DEFAULT_SCHOOL,
    PATH_LOGIN_PAGE,
    SchoolProfile,
)
from core.errors import XKError, FailureKind, classify_response, KIND_LABEL

logger = logging.getLogger("xk.http")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# 超时分档（2026-10-01）
#
# ⚠️ 先说一个 `requests` 的坑：`timeout` 传**一个 float** 时，它被用于
# **connect 与 read 各一次**，所以单请求最坏耗时是 **2 × 该值**。
# 之前全项目统一 15.0 → 单个请求最坏能挂 **30 秒**。在「卡点抢课」里这是灾难级的：
# 只要有一发卡住，同一线程后面的课全都干等（队头阻塞）。
#
# 又因为本项目是**单线程串行 / 轮流派发**（一次只有一个在途写请求，
# 见 engine/runner.py），超时就是「这一轮最多被拖多久」的直接乘数，所以按用途分档：
#
#   TIMEOUT_CRITICAL  关键路径：submit / precheck / query_classes
#                     这些请求直接决定「这一发能不能抢到」，宁可可重试也不能干等。
#                     4 s 读取 ≈ 正常 RTT（250 ms）的 16 倍 —— 真超了就是教务卡死，
#                     再等也不会变快，不如放掉、让下一项先走（轮流模式）或立刻重试。
#   TIMEOUT_QUICK     探活 ping：只想知道「还登录着吗 / 开放了吗」，
#                     卡住时宁可快速给界面一个「连不上」的结论。
#   TIMEOUT_NORMAL    其它：init / 查课 / 查已选 / 时钟采样 ——
#                     这些是「用户主动点一下等结果」，慢一点没关系，别误判成失败。
#
# ⚠️ 写请求超时有个**本质**后果：结果未知（请求可能已经在教务生效了）。
# 处理方法不在这一层 —— 见 `engine/runner.py` 对 `ALREADY_IN_CLASS`（flag="6"）的判定：
# 重试同一发时教务会回「该教学班已选中」，那就是「其实抢到了」的确认。
# ---------------------------------------------------------------------------

#: 其它请求（init / 查课 / 查已选 / 时钟采样）。单请求最坏 2×15 = 30 s。
TIMEOUT_NORMAL: float = 15.0

#: 关键路径（submit / precheck / query_classes）：(connect, read)。
TIMEOUT_CRITICAL: tuple[float, float] = (2.0, 4.0)

#: 探活（ping）：(connect, read)。
TIMEOUT_QUICK: tuple[float, float] = (2.0, 3.0)


def _timeout_text(t) -> str:
    """把超时值说成人话（float = connect/read 各用一次，最坏 2 倍）。"""
    if isinstance(t, (tuple, list)) and len(t) == 2:
        return f"连接 {t[0]:g}s / 读取 {t[1]:g}s"
    try:
        return f"{float(t):g}s"
    except (TypeError, ValueError):
        return str(t)


@dataclass
class RawResponse:
    """一次响应的统一视图。"""

    status: int
    text: str
    url: str
    location: str | None = None
    server_date: float | None = None   # 响应 Date 头 → unix 秒（服务器时刻）
    rtt: float = 0.0                   # 本次往返耗时（秒）

    def json_or_none(self):
        import json

        try:
            return json.loads(self.text)
        except Exception:
            return None


class HttpSession:
    """带凭据的 HTTP 会话。核心层的唯一网络出口。"""

    def __init__(
        self,
        credential: Credential,
        school: SchoolProfile | None = None,
        timeout: float | tuple[float, float] | None = None,
    ):
        self.school = school or DEFAULT_SCHOOL
        # None = 用全局默认（`TIMEOUT_NORMAL`）。每个请求还可以单独覆盖（见 get/post）。
        self.timeout = TIMEOUT_NORMAL if timeout is None else timeout
        self.clock = ServerClock()
        self._cred = credential
        self._s = requests.Session()
        self._s.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        # 抢课是「卡点」游戏，连接复用必须打开（实测：新建连接 410ms vs 复用 250ms）
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=8, max_retries=0
        )
        self._s.mount("https://", adapter)
        self._s.mount("http://", adapter)
        self._install_cookies(credential.cookie_header)

    # -- 内部 ---------------------------------------------------------------

    def _install_cookies(self, cookie_header: str) -> None:
        """把 "k=v; k2=v2" 装进 session，并做域名白名单校验（学 zjgsu）。"""
        expected_host = self.school.base_url.split("//", 1)[-1].split("/", 1)[0]
        n = 0
        for kv in (cookie_header or "").split(";"):
            kv = kv.strip()
            if not kv or "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            # 只发到目标域，避免凭据外泄
            self._s.cookies.set(k, v, domain=expected_host)
            n += 1
        if n:
            logger.info("已装载 %d 条 Cookie（目标域 %s）", n, expected_host)
        else:
            logger.warning("Cookie 为空，后续请求预计会跳登录页")

    def _referer(self) -> str:
        return self.school.url("xtgl/index_initMenu.html", with_gnmkdm=False)

    # -- 对外 ---------------------------------------------------------------

    def with_credential(self, credential: Credential) -> "HttpSession":
        """换一份凭据，返回新的 session（用于重新登录后接管）。"""
        return HttpSession(credential, self.school, self.timeout)

    # -- 时钟校准 -----------------------------------------------------------

    def sync_clock(self, samples: int = 4, url: str | None = None) -> ServerClock:
        """主动采样若干次，收窄「服务器 - 本地」偏差区间。

        打的是**未登录也会带 Date 头**的登录页，所以这一步：
            - 不消耗任何选课配额
            - 不改变任何选课状态（满足铁律 #2：预检零副作用）
        我校实测该页面经网关直接返回 403（Tengine），但 Date 头照样有，
        403 对采样毫无影响，因此这里吞掉异常继续采样。
        """
        target = url or self.school.url(PATH_LOGIN_PAGE, with_gnmkdm=False)
        before = self.clock.samples
        for _ in range(max(0, samples)):
            try:
                self.get(target)
            except XKError as e:
                # 我校该页面经网关直接 403 —— 但 Date 头照样下发，采样已经成功。
                # 所以这里不能把「请求抛异常」当成「采样失败」。
                logger.debug("时钟采样请求异常（Date 头仍可用，忽略）：%s", e)

        gained = self.clock.samples - before
        if gained:
            logger.info(
                "时钟校准完成（新增 %d 个样本，共 %d）：%s",
                gained, self.clock.samples, self.clock.describe(),
            )
        else:
            logger.warning("时钟校准未取得任何样本（响应里没有可解析的 Date 头？）")
        return self.clock

    def get(
        self,
        url: str,
        *,
        referer: str | None = None,
        allow_redirects: bool = True,
        expect_states: tuple[FailureKind, ...] = (),
        timeout: float | tuple[float, float] | None = None,
    ) -> RawResponse:
        return self._do(
            "GET",
            url,
            referer=referer,
            allow_redirects=allow_redirects,
            expect_states=expect_states,
            timeout=timeout,
        )

    def post(
        self,
        url: str,
        data: dict | None = None,
        *,
        referer: str | None = None,
        allow_redirects: bool = True,
        expect_states: tuple[FailureKind, ...] = (),
        timeout: float | tuple[float, float] | None = None,
    ) -> RawResponse:
        return self._do(
            "POST",
            url,
            data=data,
            referer=referer,
            allow_redirects=allow_redirects,
            expect_states=expect_states,
            timeout=timeout,
        )

    def _do(
        self,
        method: str,
        url: str,
        data: dict | None = None,
        referer: str | None = None,
        allow_redirects: bool = True,
        expect_states: tuple[FailureKind, ...] = (),
        timeout: float | tuple[float, float] | None = None,
    ) -> RawResponse:
        headers = {"Referer": referer or self._referer()}
        eff_timeout = self.timeout if timeout is None else timeout
        t0 = time.monotonic()
        try:
            r = self._s.request(
                method,
                url,
                data=data,
                headers=headers,
                timeout=eff_timeout,
                allow_redirects=allow_redirects,
            )
        except requests.RequestException as e:
            # ⚠️ 归 **NETWORK（可重试）** 而不是 UNKNOWN（不可重试）。
            #
            # 这是在「教务卡顿」这个真实场景下改的（2026-09-30）：超时 / 连接被重置
            # 意味着**请求根本没走到教务**，对端多半只是卡了一下 —— 这与
            # 「教务明确回了一个我们没归类的原因」（UNKNOWN，再试也没用）**完全相反**。
            # 以前两者同归 UNKNOWN，结果一次网络抖动就把那门课整项判死、一次都不重试。
            #
            # ⚠️ 写请求（POST）多一句「结果未知」：GET 超时只是「没问到」，
            # 而 POST 超时可能是「已经生效了但响应没回来」—— 这两种情况的重试后果不同，
            # 必须在日志里说清楚（上层靠 `ALREADY_IN_CLASS`/flag=6 来确认，见 core/client.py）。
            extra = (
                "；⚠️ 这是写请求，**结果未知**（可能已在教务生效，重试时教务会回"
                "「该教学班已选中」→ 按已抢到处理）"
                if method not in ("GET", "HEAD")
                else ""
            )
            raise XKError(
                FailureKind.NETWORK,
                f"网络异常（超时 {_timeout_text(eff_timeout)}，或连接被拒）：{e}{extra}",
            ) from e
        t1 = time.monotonic()

        # 顺手校准时钟（每个响应都是一次免费的采样，不需要额外请求）
        date_raw = r.headers.get("Date")
        self.clock.observe(date_raw, t0, t1)

        resp = RawResponse(
            status=r.status_code,
            text=r.text or "",
            url=str(r.url),
            location=r.headers.get("Location"),
            server_date=parse_http_date(date_raw),
            rtt=t1 - t0,
        )

        # 三态判定（GCCTool 思路，我校实测吻合）
        kind = classify_response(resp.status, resp.text, resp.location)
        if kind is not None:
            # expect_states：调用方明确表示「这个状态我自己会处理，把原文给我」。
            # 用途是选课首页 —— 关闭期它返回 200 + 「当前不属于选课阶段」，
            # 这不是错误而是一种**正常的业务状态**，需要读页面上的 iskxk / 隐藏域来判断，
            # 若在这里抛异常，调用方就拿不到 HTML 了（曾经的死代码 bug）。
            if kind in expect_states:
                logger.info("响应被判为 %s，但调用方要求自行处理，原样返回", kind.value)
                return resp
            label = KIND_LABEL.get(kind, kind.value)
            raise XKError(
                kind,
                f"{label}（HTTP {resp.status}，{len(resp.text)} 字节）",
                raw=resp.text[:2000],  # 铁律 #8：异常原文留存
            )
        return resp

    def close(self) -> None:
        self._s.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
