"""离线自检：单实例锁 —— 同一个端口只允许一个进程（2026-10-12 用户需求）。

需求原文：**exe 重复双击时不要起第二个，而是提示「已启动请稍后」**。

要验三件事：

A. 源码层
   · 锁**按端口**命名 —— 🔴 绝不能做成「全局只准一个」，否则会把 8721/8731 的
     隔离测试实例一起锁死（那等于把测试体系拆掉）；
   · 提示**既不弹框、也不自建托盘图标**（2026-10-12 用户第二次反馈）：
     pystray 的 `Icon` 就是「通知区里的一个图标」，第二个实例为了弹一次气泡而
     新建一个，通知区就会**多出一个重复图标** → 改成 POST `/api/notify`
     **借主实例已有的那个图标**弹，第二个实例立刻退出；
   · 第二个实例返回**非 0** 退出码；
   · 任何异常都放行（锁机制本身不许把程序拦死）。

B. 函数级
   `_try_acquire_single_instance(port)`：首次 True，**同端口再抢 False**，
   换个端口又 True（多实例支持没被破坏）。

C. 端到端（真起进程，最硬的一条）
   起一个真实例占住端口
     → **本进程**去抢同一端口应当抢不到（证明锁跨进程生效）
     → 再起第二个进程，它应当**自己退出**（且很快）、退出码非 0、输出里有「已经运行」
     → 杀掉第一个之后锁要能**重新抢到**
       （🔴 这条最容易被漏掉：锁若不随进程释放，「关了再开」会被自己拦住）。

D. /api/notify 通道（console 实例没有托盘 → 必须**如实**回 no_tray）
   ⚠️ 有托盘那一侧（回 ok=true、且**不多出图标**）在 exe 端到端里验。

    python tests/smoke_single_instance.py
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, ".")

# 用两个冷门端口，避开 8720（用户实例）与 8721/8723/8731（别的验收脚本）。
_PORT_FN = int(os.environ.get("XK_SI_PORT", "8747"))        # B 阶段（进程内）
_PORT_E2E = _PORT_FN + 10                                   # C 阶段（真起进程）

import serve  # noqa: E402  —— 只 import，不会跑 main()

_fails = []


def expect(label, cond, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        _fails.append(label)
        print(f"  FAIL {label} {detail}")


_src = (Path(__file__).resolve().parents[1] / "serve.py").read_text(encoding="utf-8")
_app_src = (Path(__file__).resolve().parents[1] / "ui" / "app.py").read_text(encoding="utf-8")
_fn = _src.split("def _try_acquire_single_instance")[1].split("\ndef ")[0]
#: 「第二次启动的提示」这一整段 = 这两个函数的正文（⚠️ 边界要卡死，
#:  否则会把后面 `_stop_all` 里的 `threading.Timer` 也算进来 → 断言假红）
_dup = _src.split("def _ask_running_instance_to_notify")[1].split(
    "\ndef _notify_already_running")[0]
_dup += _src.split("def _notify_already_running")[1].split("\nimport uvicorn")[0]

print("=" * 62)
print("A. 源码层：锁的粒度与提示方式")
print("=" * 62)

expect("⭐ 锁名里**带端口**（按端口锁，不是全局单锁）",
       "anyuan-xk-assistant-port-{port}" in _src)
expect("🔴 没写成「全局只准一个」的锁名（那会锁死隔离测试实例）",
       "anyuan-xk-assistant-port-{port}" in _src
       and 'CreateMutexW(None, False, "Local\\\\nanyuan-xk-assistant")' not in _src)
expect("用 CreateMutexW（Windows 标准做法，进程退出内核自动释放）",
       "CreateMutexW" in _fn)
expect("🔴 确认已经**没有** MessageBoxW（用户明确要求不弹框）",
       "MessageBoxW" not in _src)
expect("提示文案里说清「已经在运行」", "已经在运行" in _src)
expect("⭐ 气泡文案写的是「单击**托盘图标**」——"
       "pystray 不转发 NIN_BALLOONUSERCLICK，写成「点气泡」就是撒谎",
       "单击托盘图标即可打开页面" in _src and "点气泡打开" not in _src)

print("  --- 第二个实例**不许自建托盘图标**（会在通知区多留一个图标）---")
expect("🔴 没有第二个图标（`nanyuan_xk_dup` 已删）—— 主实例的图标名是 `nan_yuan_xk`",
       "nanyuan_xk_dup" not in _src)
expect("🔴 那段提示里**不建图标、不起消息循环**（不出现 `pystray.Icon(` / `icon.run(`）",
       "pystray.Icon(" not in _dup and "icon.run(" not in _dup)
expect("🔴 也不再有「空转 30s 再收摊」那套（`threading.Timer` 不在提示路径里）",
       "threading.Timer" not in _dup)
expect("⭐⭐ 改成**请主实例借它已有的图标弹气泡**：POST /api/notify",
       "/api/notify" in _src and "urllib.request" in _dup)

print("  --- 主实例那一侧：接口 + 出口 ---")
expect("⭐ ui/app.py 里真的有 POST /api/notify",
       '@app.post("/api/notify")' in _app_src)
expect("⭐ ui/app.py 提供 set_notify_sink（serve.py 用它把气泡接到主图标上）",
       "def set_notify_sink" in _app_src and "set_notify_sink" in _src)
expect("⭐ serve.py 把 sink 接到 icon.notify 上",
       "set_notify_sink(lambda title, text: icon.notify(text, title))" in _src)
expect("🔴 /api/notify 只接受**本机**来源（--lan 绑 0.0.0.0 时别人不能拿它刷气泡）",
       "_LOOPBACK_HOSTS" in _app_src and "_LOOPBACK_HOSTS" in
       _app_src.split('@app.post("/api/notify")')[1].split("@app.")[0])
expect("⭐ 主实例没托盘时明确回 no_tray（让第二个实例自己退化，不静默）",
       '"no_tray"' in _app_src)
expect("⭐ 拿不到主实例的提示时退化：直接打开页面（绝不什么都不做）",
       "_open_url(url)" in _dup)

print("  --- 公共纪律 ---")
expect("第二个实例返回**非 0** 退出码（启动器 / 脚本能感知到）", "return 3" in _src)
expect("⭐ 锁机制任何异常都放行（不许因为它自己出问题就起不来）",
       _fn.count("return True") >= 3, f"return True x{_fn.count('return True')}")
expect("非 Windows 有兜底（lock 文件 + flock）", "fcntl.flock" in _src)
expect("⭐ 通知失败不抛（主实例可能是没有该接口的旧版本）",
       "return False" in _dup)

print()
print("=" * 62)
print("B. 函数级：同端口抢两次")
print("=" * 62)

expect(f"首次抢 {_PORT_FN} → 成功", serve._try_acquire_single_instance(_PORT_FN) is True)
expect("⭐ 同端口再抢 → **失败**（这就是「第二次启动」在进程内的表现）",
       serve._try_acquire_single_instance(_PORT_FN) is False)
expect("⭐ 换个端口 → 照样能起（多实例并存没被破坏）",
       serve._try_acquire_single_instance(_PORT_FN + 1) is True)

print()
print("=" * 62)
print("C. 端到端：真起两个进程")
print("=" * 62)


def _up(port: int, secs: int = 60) -> bool:
    """等 /api/state 能通。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(secs * 2):
        try:
            op.open(f"http://127.0.0.1:{port}/api/state", timeout=1).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


# ⚠️ XK_STATE_DIR=off：测试实例不碰磁盘（别污染 state/）
# ⚠️ 两个实例都带 --no-browser：console 模式下 webbrowser.open 会**真的弹出浏览器**，
#    测试绝不能干这事。
_env = dict(os.environ, XK_STATE_DIR="off")
_kids: list[subprocess.Popen] = []


def _spawn(port: int) -> subprocess.Popen:
    p = subprocess.Popen(
        [sys.executable, "serve.py", "--port", str(port), "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_env, text=True,
        encoding="utf-8", errors="replace",
    )
    _kids.append(p)
    return p


try:
    p1 = _spawn(_PORT_E2E)
    up1 = _up(_PORT_E2E)
    expect(f"第一个实例起来了（端口 {_PORT_E2E}）", up1)
    if up1:
        expect("⭐⭐ 本进程去抢同一端口 → **抢不到**（锁确实跨进程生效）",
               serve._try_acquire_single_instance(_PORT_E2E) is False)

        p2 = _spawn(_PORT_E2E)
        _t2 = time.time()
        rc, out = None, ""
        try:
            out, _ = p2.communicate(timeout=40)
            rc = p2.returncode
        except subprocess.TimeoutExpired:
            p2.kill()
            out, _ = p2.communicate()
        _dt2 = time.time() - _t2
        expect("⭐⭐ 第二个实例**自己退出**了（没赖着不听）", rc is not None, f"rc={rc}")
        expect("⭐⭐ 第二个实例退出码非 0", bool(rc), f"rc={rc}")
        expect("⭐⭐ 第二个实例说清了「已经运行」", "已经在运行" in (out or ""),
               f"输出尾部：{(out or '')[-240:]!r}")
        expect("第二个实例**没有**去抢端口（它是被锁拦下的，不是端口报错）",
               "address already in use" not in (out or "").lower(),
               f"输出尾部：{(out or '')[-240:]!r}")
        expect("⭐⭐ 立刻退出、**不再空转 30s**（自建图标那套已删）",
               _dt2 < 12, f"{_dt2:.1f}s")
        print(f"  第二个实例存活 {_dt2:.1f}s，rc={rc}")

        # -- D. /api/notify：主实例没有托盘时要**如实**回 no_tray（不撒谎、不静默）----
        print()
        print("=" * 62)
        print("D. /api/notify 通道（本进程用 console 实例验：应如实回 no_tray）")
        print("=" * 62)
        try:
            import json as _json

            _op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            _body = _json.dumps({"title": "t", "text": "x"}).encode()
            _req = urllib.request.Request(
                f"http://127.0.0.1:{_PORT_E2E}/api/notify", data=_body,
                headers={"Content-Type": "application/json"}, method="POST")
            _resp = _json.loads(_op.open(_req, timeout=5).read())
            expect("⭐ /api/notify 路由存在且返回 JSON", isinstance(_resp, dict), f"{_resp}")
            expect("⭐⭐ console 实例没有托盘 → 如实回 ok=false / no_tray"
                   "（第二个实例据此退化成开页面，而不是静默什么都不做）",
                   _resp.get("ok") is False and _resp.get("reason") == "no_tray", f"{_resp}")
        except Exception as e:
            expect("⭐ /api/notify 可用", False, f"{type(e).__name__}: {e}")
finally:
    for p in _kids:
        try:
            p.kill()
        except Exception:
            pass
    for p in _kids:
        try:
            p.wait(timeout=15)
        except Exception:
            pass

# 给内核一点时间释放（互斥体随进程终止释放，通常立即）
time.sleep(1.5)
expect("⭐⭐ 第一个实例被杀掉后，锁能**重新抢到**（关了还能再开，没被自己拦死）",
       serve._try_acquire_single_instance(_PORT_E2E) is True)

print()
print("=" * 62)
if _fails:
    print(f"✗ {len(_fails)} 条未通过：")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("✓ 全部通过")
