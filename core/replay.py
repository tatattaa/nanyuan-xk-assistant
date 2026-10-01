"""离线回放：用 tests/capture_open_period.py 抓下来的包重建一个「假教务」。

用途：系统关闭后，仍可用**真实的开放期响应**跑核心层 / 引擎层 / UI 测试。

原理：
- 抓包时每个请求按 `(method, url, body)` 落盘（manifest.jsonl + raw/NNNN_*.txt）
- ReplaySession 实现与 HttpSession 相同的接口（get/post/clock/sync_clock/close），
  收到请求时按规范化指纹 `(method, path+排序query, 排序body)` 查表，
  返回当时实录的 RawResponse（状态码 / 原文 / 服务器 Date / RTT）

匹配规则：
- 同一指纹录到多次（如重复切 Tab、轮询）→ 按录制顺序 FIFO 出队；
  队列耗尽后**复用最后一包**（轮询场景友好）
- 默认严格模式：指纹查不到 → 抛 XKError 并列出同路径候选，便于发现「参数变了」
- strict=False：退化为「同 method+path 最近一包」，适合探索性脚本

依赖边界：本模块属于核心层（HttpSession 的测试替身），不认识界面与引擎。
"""

from __future__ import annotations

import json
import logging
from collections import deque
from email.utils import formatdate
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.clock import ServerClock
from core.errors import FailureKind, XKError
from core.http import RawResponse

logger = logging.getLogger("xk.replay")


def _norm_url(url: str) -> str:
    """URL 规范化：query 排序，去 fragment。路径与主机保持原样。"""
    p = urlsplit(url)
    q = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    return urlunsplit((p.scheme, p.netloc, p.path, q, ""))


def _norm_body(body: dict | None) -> str:
    if not body:
        return ""
    items = sorted((str(k), str(v)) for k, v in body.items())
    return urlencode(items)


def fingerprint(method: str, url: str, body: dict | None) -> tuple[str, str, str]:
    return (method.upper(), _norm_url(url), _norm_body(body))


class ReplaySession:
    """HttpSession 的离线替身。接口对齐：get/post/clock/sync_clock/with_credential/close。"""

    def __init__(self, capture_dir: str | Path, *, strict: bool = True, feed_clock: bool = True):
        self.dir = Path(capture_dir)
        self.strict = strict
        self.clock = ServerClock()
        self.hits = 0
        self.misses = 0
        self._queues: dict[tuple[str, str, str], deque[RawResponse]] = {}
        self._last: dict[tuple[str, str, str], RawResponse] = {}
        self._by_path: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
        self._load(feed_clock)

    # -- 装载 ---------------------------------------------------------------

    def _load(self, feed_clock: bool) -> None:
        manifest = self.dir / "manifest.jsonl"
        if not manifest.exists():
            raise FileNotFoundError(f"抓包清单不存在：{manifest}（先跑 tests/capture_open_period.py）")
        n = 0
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("error"):
                continue  # 异常包不入队（真实跑会抛错，回放价值在原文，已在 raw/ 里）
            body_file = self.dir / e["file"]
            text = body_file.read_text(encoding="utf-8", errors="replace") if body_file.exists() else ""
            resp = RawResponse(
                status=e.get("status") or 0,
                text=text,
                url=e["url"],
                server_date=e.get("server_date"),
                rtt=(e.get("rtt_ms") or 0) / 1000.0,
            )
            key = fingerprint(e["method"], e["url"], e.get("body"))
            self._queues.setdefault(key, deque()).append(resp)
            self._by_path.setdefault((key[0], key[1]), []).append(key)
            n += 1
            if feed_clock and resp.server_date:
                # 用实录的 Date 头喂时钟：回放时 ServerClock 估计与抓包当天一致
                t1 = __import__("time").monotonic()
                t0 = t1 - max(resp.rtt, 0.001)
                self.clock.observe(formatdate(resp.server_date, usegmt=True), t0, t1)
        logger.info("ReplaySession 装载 %d 个包（%s）", n, self.dir)

    # -- HttpSession 兼容接口 -------------------------------------------------

    def with_credential(self, credential) -> "ReplaySession":
        return self

    def sync_clock(self, samples: int = 4, url: str | None = None) -> ServerClock:
        return self.clock  # 回放无需采样：时钟已由实录 Date 头喂好

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(
        self,
        url: str,
        *,
        referer: str | None = None,
        allow_redirects: bool = True,
        expect_states: tuple = (),
        timeout=None,
    ) -> RawResponse:
        # expect_states / timeout 仅为与 HttpSession 接口对齐（回放不打网络，
        # 超时值在这里没有意义）；回放的是**开放期**抓包，不会出现 NOT_OPEN
        # 这类状态类响应，故此处无需任何特殊处理。
        return self._do("GET", url)

    def post(
        self,
        url: str,
        data: dict | None = None,
        *,
        referer: str | None = None,
        allow_redirects: bool = True,
        expect_states: tuple = (),
        timeout=None,
    ) -> RawResponse:
        return self._do("POST", url, data)

    # -- 查表 ---------------------------------------------------------------

    def _do(self, method: str, url: str, data: dict | None = None) -> RawResponse:
        key = fingerprint(method, url, data)
        q = self._queues.get(key)
        if q:
            resp = q[0]
            if len(q) > 1:
                q.popleft()  # FIFO 出队；只剩最后一包时原地复用（轮询友好）
            self._last[key] = resp
            self.hits += 1
            return resp

        self.misses += 1
        path_key = (key[0], key[1])
        candidates = self._by_path.get(path_key, [])
        if not self.strict and candidates:
            # 宽松模式：同 method+path 最近一包
            for cand in reversed(candidates):
                if cand in self._last:
                    logger.debug("宽松命中（body 不同）：%s", url)
                    self.hits += 1
                    return self._last[cand]
            ck = candidates[-1]
            cq = self._queues.get(ck)
            if cq:
                logger.debug("宽松命中（body 不同，队列首包）：%s", url)
                self.hits += 1
                return cq[0]

        hint = ""
        if candidates:
            ck = candidates[-1]
            want = dict(parse_qsl(key[2], keep_blank_values=True))
            got = dict(parse_qsl(ck[2], keep_blank_values=True))
            diff = sorted({*want, *got} - {k for k in want if want.get(k) == got.get(k)})
            hint = f"；同路径候选 {len(candidates)} 个，差异字段：{diff[:12]}"
        raise XKError(
            FailureKind.UNKNOWN,
            f"回放未命中：{method} {url}{hint}",
            raw=f"body={dict(parse_qsl(key[2], keep_blank_values=True))}",
        )
