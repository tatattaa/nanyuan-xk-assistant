"""取凭据。核心层 4 函数之一：get_credential。

优先级（见 HANDOFF 5.5）：
  1. 手动贴 Cookie      ← 第一步做，零依赖，支持验证码/SSO/扫码
  2. CDP 自动抓 Cookie  ← 体验好（需浏览器调试端口）
  3. 账号密码 + RSA     ← 最后考虑（若学校跳 SSO 则此路不通）

铁律 #9：凭据不落盘。本模块只负责「取到并放进内存」。
"""

from __future__ import annotations

import json
import logging
import urllib.request

from core.config import Credential, DEFAULT_SCHOOL, SchoolProfile

logger = logging.getLogger("xk.credential")

CDP_PORT_DEFAULT = 9666


# ---------------------------------------------------------------------------
# 方案 1：手动贴 Cookie（第一步做，最稳）
# ---------------------------------------------------------------------------


def from_manual_cookie(cookie_string: str) -> Credential:
    """从用户粘贴的 Cookie 串构造凭据。

    接受多种格式（浏览器 DevTools 里怎么复制都能用）：
      - "JSESSIONID=xxx; route=yyy"
      - "Cookie: JSESSIONID=xxx; route=yyy"
      - 逐行 "JSESSIONID=xxx\\nroute=yyy"
    """
    s = (cookie_string or "").strip()
    if not s:
        raise ValueError("Cookie 为空")

    # 去掉可能带上的 "Cookie:" 前缀
    if s.lower().startswith("cookie:"):
        s = s.split(":", 1)[1].strip()

    # 统一分隔符：换行 → 分号
    s = s.replace("\r", ";").replace("\n", ";")

    # 只保留 k=v 形式
    parts = []
    for kv in s.split(";"):
        kv = kv.strip()
        if kv and "=" in kv:
            parts.append(kv)

    if not parts:
        raise ValueError("无法解析出任何 Cookie（应形如 JSESSIONID=xxx; route=yyy）")

    cookie_header = "; ".join(parts)
    names = [p.split("=", 1)[0] for p in parts]
    logger.info("手动凭据已装载：%d 条（%s）", len(parts), ", ".join(names))
    return Credential(cookie_header=cookie_header, source="manual")


# ---------------------------------------------------------------------------
# 方案 2：CDP 自动抓 Cookie
# ---------------------------------------------------------------------------


class CdpCookieFetcher:
    """通过 CDP 从一个已打开的调试端口读取 Cookie。

    为什么必须用 CDP 而不是 document.cookie：
      我校 JSESSIONID 是 HttpOnly，JS 读不到（实测验证）。

    用法：
        f = CdpCookieFetcher(port=9666)
        cred = f.fetch()
    """

    def __init__(
        self,
        port: int = CDP_PORT_DEFAULT,
        school: SchoolProfile | None = None,
        timeout: float = 5.0,
    ):
        self.port = port
        self.school = school or DEFAULT_SCHOOL
        self.timeout = timeout

    def _http_json(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        # 关键：绕过系统代理，否则可能 502
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def list_pages(self) -> list[dict]:
        """列出所有可调试页面。"""
        return [t for t in self._http_json("/json/list") if t.get("type") == "page"]

    def find_target(self) -> dict | None:
        """找到教务系统的那个页面。"""
        for t in self.list_pages():
            if "jwglxt" in t.get("url", ""):
                return t
        return None

    def fetch(self) -> Credential:
        target = self.find_target()
        if not target:
            raise RuntimeError(
                "未找到教务页面。请确认已在调试端口的浏览器里登录教务系统。"
            )

        cookies = self._get_cookies_via_cdp(target["webSocketDebuggerUrl"])
        if not cookies:
            raise RuntimeError("CDP 未返回任何 Cookie，可能尚未登录")

        expected_host = self.school.base_url.split("//", 1)[-1].split("/", 1)[0]
        parts = []
        for c in cookies:
            dom = (c.get("domain") or "").lstrip(".")
            # 域名白名单：Cookie 域是目标域的后缀，或目标域是 Cookie 域的后缀
            if dom and not (expected_host.endswith(dom) or dom.endswith(expected_host)):
                continue
            parts.append(f"{c['name']}={c['value']}")

        if not parts:
            raise RuntimeError(f"Cookie 中不含目标域 {expected_host} 的任何凭据")

        names = [p.split("=", 1)[0] for p in parts]
        logger.info("CDP 抓到 %d 条 Cookie：%s", len(parts), ", ".join(names))

        # 我校实测：必须同时有 JSESSIONID 才有用
        if "JSESSIONID" not in names:
            logger.warning("未抓到 JSESSIONID，登录态可能无效")

        return Credential(cookie_header="; ".join(parts), source="cdp")

    def _get_cookies_via_cdp(self, ws_url: str) -> list[dict]:
        """用原生 WebSocket 调 Network.getCookies。

        这里用同步实现（websocket-client），避免给核心层引入 asyncio 复杂度。
        """
        try:
            import websocket  # websocket-client
        except ImportError as e:
            raise RuntimeError(
                "缺少 websocket-client，请先安装：pip install websocket-client"
            ) from e

        ws = websocket.create_connection(ws_url, timeout=self.timeout)
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            self._wait_id(ws, 1)

            target_url = self.school.base_url
            ws.send(
                json.dumps(
                    {
                        "id": 2,
                        "method": "Network.getCookies",
                        "params": {"urls": [target_url]},
                    }
                )
            )
            msg = self._wait_id(ws, 2)
            return (msg.get("result") or {}).get("cookies", []) or []
        finally:
            ws.close()

    @staticmethod
    def _wait_id(ws, want_id: int, max_rounds: int = 50) -> dict:
        for _ in range(max_rounds):
            m = json.loads(ws.recv())
            if m.get("id") == want_id:
                return m
        raise RuntimeError(f"CDP 未在规定轮次内返回 id={want_id}")


def from_cdp(port: int = CDP_PORT_DEFAULT, school: SchoolProfile | None = None) -> Credential:
    """便捷入口：从 CDP 抓 Cookie。"""
    return CdpCookieFetcher(port=port, school=school).fetch()


# ---------------------------------------------------------------------------
# 方案 3：账号密码 + RSA（最后考虑）
# ---------------------------------------------------------------------------


def _rsa_encrypt(password: str, modulus_b64: str, exponent_b64: str) -> str:
    """正方通用的 RSAES-PKCS1-v1_5 + base64。

    三家实现一致（GCCTool/GMU/gdep）。公钥由 base64 解码后转 hex 构造。
    """
    import base64
    import binascii

    import rsa  # pip install rsa

    rsa_n = binascii.b2a_hex(binascii.a2b_base64(modulus_b64))
    rsa_e = binascii.b2a_hex(binascii.a2b_base64(exponent_b64))
    key = rsa.PublicKey(int(rsa_n, 16), int(rsa_e, 16))
    return binascii.b2a_base64(rsa.encrypt(password.encode(), key)).decode()


def from_password(
    username: str,
    password: str,
    school: SchoolProfile | None = None,
    timeout: float = 15.0,
) -> Credential:
    """账号密码登录。

    ⚠️ 风险提示：
      1. 密码会进入本进程内存（违反"密码永不进程序"的理想）
      2. 若学校强制跳统一身份认证 SSO，此路不通（我校登录页有 authJwglxtLoginURL）
      3. 有验证码时需人工介入

    因此本方案优先级最低，仅在前两者都不可行时使用。
    """
    import requests

    sch = school or DEFAULT_SCHOOL
    s = requests.Session()
    s.headers.update({"User-Agent": DEFAULT_UA_FALLBACK})

    # 1. GET 登录页（建立会话 + 取 csrftoken 等隐藏域）
    login_page_url = sch.url("xtgl/login_slogin.html", with_gnmkdm=False)
    r = s.get(login_page_url, timeout=timeout)

    form = _parse_hidden_inputs(r.text)
    if not form:
        raise RuntimeError("登录页未解析到任何隐藏域，页面结构可能已变")

    # 检查是否被强制跳 SSO
    auth_url = form.get("authJwglxtLoginURL", "")
    if auth_url.strip():
        raise RuntimeError(f"学校强制统一身份认证（{auth_url}），账号密码方案不可用，请改用手动 Cookie")

    # 2. 取公钥
    pk_url = sch.url("xtgl/login_getPublicKey.html", with_gnmkdm=False)
    try:
        pk = s.get(pk_url, timeout=timeout).json()
        modulus, exponent = pk["modulus"], pk["exponent"]
    except Exception as e:
        raise RuntimeError(f"获取 RSA 公钥失败: {e}") from e

    # 3. 组装并提交
    form["yhm"] = username
    form["mm"] = _rsa_encrypt(password, modulus, exponent)
    resp = s.post(
        login_page_url,
        data=form,
        headers={"Referer": login_page_url},
        timeout=timeout,
        allow_redirects=True,
    )

    if "用户名或密码" in resp.text or "密码错误" in resp.text or "验证码" in resp.text:
        raise RuntimeError("登录失败：账号密码错误或需要验证码")

    # 4. 验证会话
    check = s.get(sch.url("xtgl/index_initMenu.html", with_gnmkdm=False), timeout=timeout)
    if "login_slogin" in str(check.url) or "用户登录" in check.text[:2000]:
        raise RuntimeError("登录后仍跳转登录页，登录未成功")

    cookies = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
    logger.info("密码登录成功，已装载 %d 条 Cookie", len(s.cookies))
    return Credential(cookie_header=cookies, source="password")


DEFAULT_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _parse_hidden_inputs(html: str) -> dict[str, str]:
    """抽所有 hidden input 的 name/value。HTMLParser 实现，零依赖。"""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.out: dict[str, str] = {}

        def handle_starttag(self, tag, attrs):
            if tag.lower() != "input":
                return
            a = {k.lower(): (v or "") for k, v in attrs}
            if a.get("type", "").lower() == "hidden" and a.get("name"):
                self.out[a["name"]] = a.get("value", "")

    p = P()
    p.feed(html or "")
    return p.out


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def get_credential(
    *,
    cookie: str | None = None,
    cdp_port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    school: SchoolProfile | None = None,
) -> Credential:
    """按优先级自动选择取凭据方式。

    核心层 4 函数之一。优先级：手动 Cookie > CDP > 账号密码。
    """
    if cookie:
        return from_manual_cookie(cookie)
    if cdp_port:
        return from_cdp(cdp_port, school)
    if username and password:
        return from_password(username, password, school)
    raise ValueError("必须提供 cookie / cdp_port / username+password 三者之一")
