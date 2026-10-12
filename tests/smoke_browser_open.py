"""离线自检：打开浏览器的时机（2026-10-12 第 8 项改动）。

需求原文（用户从「启动提速候选清单」里选的第 8 项）：
    `open_browser_when_ready(wait_s=8.0)` **超时仍照开** → 慢机器上会开出一个
    「无法访问此网站」，体感「等很久还打不开」。
    → 改成：**本地页必须等到端口真能连才开**（上限 60 s，超时就不开）；
       **教务页（外网）不依赖本地服务，立刻开**。

要验四条：
  A. 源码层：`wait_s` 默认值是 `BROWSER_WAIT_S`，且 `BROWSER_WAIT_S >= 30`
     （别再退回「等 8 秒就放弃」）；本地/外网是分开处理的。
  B. 外网页**立刻**开 —— 不等本地服务（哪怕本地端口根本没人监听）。
  C. 本地服务已就绪 → 打开顺序 = [外网, 本地]（本地最后一个开 → 成为当前停留页）。
  D. 本地一直连不上 → **不开**本地页，只打一行说明。

    python tests/smoke_browser_open.py
"""
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, ".")

import serve  # noqa: E402  —— 只 import，不会跑 main()

_fails = []


def expect(label, cond, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        _fails.append(label)
        print(f"  FAIL {label} {detail}")


_src = (Path(__file__).resolve().parents[1] / "serve.py").read_text(encoding="utf-8")
_fn = _src.split("def open_browser_when_ready")[1].split("\ndef ")[0]

print("=" * 62)
print("A. 源码层：默认等待上限 & 本地/外网分流")
print("=" * 62)
expect("⭐ 默认等待上限走常量 BROWSER_WAIT_S，不再写死 8.0",
       "wait_s: float = BROWSER_WAIT_S" in _src)
expect("⭐⭐ 上限 ≥ 30 秒（8 秒在慢机器上不够，那正是用户踩的坑）",
       getattr(serve, "BROWSER_WAIT_S", 0) >= 30, f"BROWSER_WAIT_S={getattr(serve, 'BROWSER_WAIT_S', None)}")
expect("⭐ 区分「本地页」和「外网页」（本地要等就绪、外网不等）",
       "_is_local" in _fn and "remote" in _fn and "local" in _fn)
expect("🔴 超时后**不开**本地页，只提示（原来「超时照开」= 开出错误页）",
       "return" in _fn and "不**打开本地页面" in _fn)


class _Recorder:
    """拦下 webbrowser.open，记录 (url, 相对时间)。"""

    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self._orig = webbrowser.open

    def __enter__(self):
        def fake(url, *a, **kw):
            self.calls.append((url, time.perf_counter() - self.t0))
            self.gate.set()
            return True

        self.t0 = time.perf_counter()
        webbrowser.open = fake
        return self

    def __exit__(self, *exc):
        webbrowser.open = self._orig
        return False

    def wait(self, secs):
        time.sleep(secs)

    def urls(self):
        return [u for u, _ in self.calls]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


EXTERNAL = "http://jwxt.nfu.edu.cn/jwglxt/"

print()
print("=" * 62)
print("B. 外网页立刻开（本地端口没人监听也不等）")
print("=" * 62)
dead_port = _free_port()
with _Recorder() as rec:
    serve.open_browser_when_ready([EXTERNAL, f"http://127.0.0.1:{dead_port}"], dead_port, wait_s=60.0)
    rec.gate.wait(5)
    rec.wait(0.5)
    expect("⭐ 外网页被打开了", EXTERNAL in rec.urls(), f"{rec.calls}")
    t_ext = next((t for u, t in rec.calls if u == EXTERNAL), None)
    expect("⭐⭐ 而且是**立刻**开（<3s），没被本地等待拖住",
           t_ext is not None and t_ext < 3.0, f"t={t_ext}")
    expect("⭐ 此时本地页**还没**开（端口没人监听，要一直等到超时）",
           f"http://127.0.0.1:{dead_port}" not in rec.urls(), f"{rec.urls()}")

print()
print("=" * 62)
print("C. 本地服务已就绪 → 顺序 = [外网, 本地]")
print("=" * 62)
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(4)
live_port = srv.getsockname()[1]
try:
    with _Recorder() as rec:
        serve.open_browser_when_ready(
            [EXTERNAL, f"http://127.0.0.1:{live_port}"], live_port, wait_s=60.0)
        rec.wait(3.0)
        expect("⭐ 两个页面都开了", len(rec.calls) == 2, f"{rec.calls}")
        expect("⭐⭐ 顺序 = 外网在前、本地在后（本地最后开 → 成为当前停留页）",
               rec.urls() == [EXTERNAL, f"http://127.0.0.1:{live_port}"], f"{rec.urls()}")
finally:
    srv.close()

print()
print("=" * 62)
print("D. 本地一直连不上 → 不开本地页")
print("=" * 62)
dead_port2 = _free_port()
with _Recorder() as rec:
    serve.open_browser_when_ready([f"http://127.0.0.1:{dead_port2}"], dead_port2, wait_s=2.0)
    rec.wait(3.5)
    expect("🔴 本地页**没有**被打开（宁可不开，也别开出一个错误页）",
           rec.calls == [], f"{rec.calls}")

print()
print("=" * 62)
if _fails:
    print(f"✗ {len(_fails)} 条未通过：")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("✓ 全部通过")
