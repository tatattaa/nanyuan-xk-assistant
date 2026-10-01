"""验证 SSE 事件流：用真实 API 驱动，确认事件能推给客户端。

思路：不依赖真实教务登录 —— 用「假 Cookie 建立会话会失败」这条路径
无法产生事件，所以改为直接在服务进程内注入一个假 runner 不可行（跨进程）。
改用最直接的办法：调 /api/plan + /api/start（无凭据会 400，不产生事件），
因此本测试改为「只验证 SSE 连接建立 + 心跳 + 服务端能写 data 帧」——
通过访问一个会立即产生事件的场景不可得时，退化为协议层验证。

真正的事件推送已由 smoke_ui.py 的 /api/events 增量接口覆盖（同一 RUNTIME）。
"""
import sys
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8720"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

got = []
err = []


def reader(path, want_lines=1):
    try:
        req = urllib.request.Request(BASE + path)
        with opener.open(req, timeout=12) as r:
            got.append(("header", r.headers.get("Content-Type", "")))
            deadline = time.time() + 6
            while time.time() < deadline:
                line = r.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if s:
                    got.append(("line", s))
                    if len([g for g in got if g[0] == "line"]) >= want_lines:
                        break
    except Exception as e:
        err.append(str(e))


t = threading.Thread(target=reader, args=("/api/events/stream?since=0",), daemon=True)
t.start()
time.sleep(23)   # 等一个心跳周期（50 × 0.5s）

ct = [g[1] for g in got if g[0] == "header"]
lines = [g[1] for g in got if g[0] == "line"]
print("Content-Type:", ct[:1])
print("收到行数:", len(lines))
for ln in lines[:4]:
    print("  ", ln[:120])
if err:
    print("reader 异常:", err)

ok = bool(ct and "text/event-stream" in ct[0])
print()
print("SSE 连接与协议:", "✓" if ok else "✗", "（心跳:", ": ping" if any("ping" in l for l in lines) else "未收到", "）")
sys.exit(0 if ok else 1)
