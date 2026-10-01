"""拉起一个受控浏览器，用 CDP 注入登录态（Cookie）后导航到教务主页。

为什么需要它：程序后端的登录态（JSESSIONID 等 Cookie）存在**后端进程**里，
和用户日常浏览器的 Cookie 是两套隔离的。普通 `window.open` 打开教务主页时，
那个浏览器里没有登录态，教务会要求重新登录。

要让浏览器「免密直接进入」，唯一可靠的路是：程序自己拉起一个**受控浏览器**
（独立 profile + 调试端口），用 CDP 的 `Network.setCookie` 把 Cookie 注入进去，
再导航到教务主页。浏览器带着注入的 Cookie，自然就是已登录状态。

⚠️ 安全铁律：
- Cookie 值只在内存里传递（CDP 报文），不落盘、不打日志。
- 独立 `--user-data-dir`，不碰用户日常浏览器。
- 只注入目标域（教务）的 Cookie，绝不动其它域。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("xk.browser")

# 教务域名（Cookie 注入目标）。从 base_url 拆出来，不写死。
DEFAULT_SCHOOL_HOST = "jwxt.nfu.edu.cn"

# CDP 调试端口范围：避免和已占用的端口撞车。
_DEBUG_PORT_START = 9666
_DEBUG_PORT_MAX = 9696


def _find_browser() -> str | None:
    """找 Edge / Chrome 可执行文件。Edge 优先（Windows 自带）。"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Users\32768\AppData\Local\Google\Chrome\Application\chrome.exe",
        r"C:\Users\32768\AppData\Local\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _free_port() -> int:
    """在调试端口范围内挑一个空闲端口。"""
    import socket

    for port in range(_DEBUG_PORT_START, _DEBUG_PORT_MAX + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用的 CDP 调试端口")


def _cdp_http(port: int, path: str) -> dict:
    """读 CDP 的 HTTP 端点（/json/list 等），绕过系统代理。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _wait_cdp(port: int, timeout: float = 15.0) -> None:
    """轮询直到 CDP 就绪（/json/version 能通）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _cdp_http(port, "/json/version")
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"CDP 调试端口 {port} 在 {timeout}s 内未就绪")


def _inject_and_navigate(
    port: int, host: str, cookies: dict[str, str], target_url: str, timeout: float = 15.0
) -> None:
    """通过 CDP WebSocket 注入 Cookie 并导航到教务主页。"""
    import websocket  # websocket-client

    pages = _cdp_http(port, "/json/list")
    page = next((p for p in pages if p.get("type") == "page"), None)
    if not page:
        raise RuntimeError("浏览器未就绪（找不到 page target）")
    ws_url = page["webSocketDebuggerUrl"]

    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        def _call(mid: int, method: str, params: dict | None = None) -> dict:
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            for _ in range(200):
                m = json.loads(ws.recv())
                if m.get("id") == mid:
                    return m
            raise RuntimeError(f"CDP {method} 未返回")

        _call(1, "Network.enable")

        # 注入 Cookie（含 HttpOnly 的 JSESSIONID）。domain 不带前导点。
        for name, value in cookies.items():
            _call(2, "Network.setCookie", {
                "name": name,
                "value": value,
                "url": f"https://{host}/",
            })

        # 导航到教务主页
        _call(3, "Page.navigate", {"url": target_url})
    finally:
        ws.close()


def open_school_browser(cookie_header: str, school_url: str) -> bool:
    """登录成功后：拉起受控浏览器，注入登录态并打开教务主页。

    返回 True 表示成功拉起并注入；False 表示浏览器注入这条路径不可用
    （找不到浏览器 / 注入失败），调用方应退回「普通 window.open」。

    ⚠️ 本函数是「尽力而为」的辅助能力：失败不抛异常，只记日志并返回 False，
    绝不影响主登录流程已经成功的事实。
    """
    if not cookie_header:
        return False

    browser = _find_browser()
    if not browser:
        logger.warning("未找到 Edge/Chrome，无法注入登录态到浏览器")
        return False

    # 解析 cookie_header → dict
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k:
                cookies[k.strip()] = v.strip()
    if not cookies:
        return False

    host = DEFAULT_SCHOOL_HOST
    target_url = school_url or f"https://{host}/"

    # 独立 profile（不碰用户日常浏览器）
    profile = Path(tempfile_dir()) / "xk_browser_profile"
    try:
        port = _free_port()
    except RuntimeError as e:
        logger.warning("无法分配调试端口：%s", e)
        return False

    try:
        # 清理旧的隔离 profile（若上次异常退出残留）
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)

        cmd = [
            browser,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeDevToolsWdpRemoteDebugging",
            "--proxy-server=direct://",
            "--proxy-bypass-list=*",
            "about:blank",
        ]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        _wait_cdp(port)
        _inject_and_navigate(port, host, cookies, target_url)
        logger.info("已拉起受控浏览器并注入登录态，打开 %s", target_url)
        return True
    except Exception as e:
        logger.warning("注入登录态到浏览器失败：%s", e)
        return False


def tempfile_dir() -> str:
    """临时目录（独立 profile 存这里）。"""
    return str(Path(tempfile.gettempdir()))
