"""迷你假教务：验证两件「只能看真实形态」的事。

只用标准库。**两个独立的开关**，靠控制口切换：

选课期（「📂 上次数据」按钮只在未开放期露面 —— 见 ui/static/app.js）：
    GET /__close    → 切成**未开放期**（返回 23304 字节那种只剩通用隐藏域的页面）
    GET /__open     → 切回已开放期（默认）
    GET /__closed   → 查询当前是 'open' 还是 'closed'

登录态（「顶栏胶囊会不会自己发现用户去教务网站点了退出」）：
    GET /__login    → 进入「已登录」模式（返回一份看起来正常的选课首页）
    GET /__logout   → 进入「已退出」模式（302 回登录页，和真实教务一模一样）
    GET /__mode     → 查询当前模式（'ok' / 'logged_out'）

⚠️ 真实教务在 Cookie 失效时就是 302 → 登录页；requests 会跟随重定向，
最终落在登录页 HTML 上，由 `core.errors.is_login_page` 判出来（实测确认过）。
这里刻意复刻这条路径，而不是直接返 401 —— 测的必须是真实形态。

⚠️ 两个开关**互相独立**：`/__logout` 时先判登录态（302 → 登录页），
登录态正常时才按选课期开关给 OK_HTML / CLOSED_HTML。
"""
import http.server
import socketserver
import sys
import urllib.parse

MODE = {"logged_out": False, "closed": False}

#: 看起来正常的选课首页（够 init 跑完、不抛异常）
OK_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>选课</title></head><body>
<form>
  <input type="hidden" name="iskxk" value="1">
  <input type="hidden" name="xkkz_id" value="FAKEKZ">
  <input type="hidden" name="xkxnm" value="2026">
  <input type="hidden" name="xkxqm" value="1">
</form>
<div id="displayBox">选课首页</div>
</body></html>"""

#: 未开放期的选课首页。复刻实测形态：权威判据是 `iskxk=0`，且
#: `xkkz_id` / `xkxnm` / `zxfs` / `xklcmc` 这些**选课上下文整个消失**
#: （真实页面 23304 字节、只剩通用隐藏域）→ `page_is_open()` 判 False。
#: ⚠️ 别在这里塞任何 Tab 锚点：Tab 只在开放期出现，塞了会让「未开放」判错。
CLOSED_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>选课</title></head><body>
<form>
  <input type="hidden" name="iskxk" value="0">
  <input type="hidden" name="xnm" value="2026">
  <input type="hidden" name="xqm" value="1">
  <input type="hidden" name="gnmkdm" value="N253512">
</form>
<div id="displayBox">当前不属于选课阶段</div>
</body></html>"""

#: 登录页。⚠️ 必须让 `is_login_page()` 认出来 —— 它匹配 "login_slogin" /
#: 'id="frmLogin"' / 开头 2000 字里的「用户登录」。真实教务页面里这三样都有。
LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>用户登录</title></head><body>
<div id="frmLogin">用户登录</div>
<script src="/jwglxt/js/login_slogin.js"></script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, headers=None):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _handle(self):
        path = urllib.parse.urlparse(self.path).path
        # -- 控制口（登录态） --
        if path == "/__logout":
            MODE["logged_out"] = True
            return self._send(200, "bye")
        if path == "/__login":
            MODE["logged_out"] = False
            return self._send(200, "hi")
        if path == "/__mode":
            return self._send(200, "logged_out" if MODE["logged_out"] else "ok")
        # -- 控制口（选课期）--
        if path == "/__close":
            MODE["closed"] = True
            return self._send(200, "closed")
        if path == "/__open":
            MODE["closed"] = False
            return self._send(200, "open")
        if path == "/__closed":
            return self._send(200, "closed" if MODE["closed"] else "open")
        # 登录页本身（重定向的落点）永远给登录页，否则会无限重定向
        if "login_slogin" in path or "slogin" in path:
            return self._send(200, LOGIN_HTML)
        if MODE["logged_out"]:
            return self._send(302, "", {"Location": "/jwglxt/login_slogin.html"})
        return self._send(200, CLOSED_HTML if MODE["closed"] else OK_HTML)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"迷你假教务 @ http://127.0.0.1:{port}/jwglxt/", flush=True)
        httpd.serve_forever()
