"""离线自检：单实例锁 —— 同一个端口只允许一个进程（2026-10-12 用户需求）。

需求原文：**exe 重复双击时不要起第二个，而是提示「已启动请稍后」**。

要验三件事：

A. 源码层
   · 锁**按端口**命名 —— 🔴 绝不能做成「全局只准一个」，否则会把 8721/8731 的
     隔离测试实例一起锁死（那等于把测试体系拆掉）；
   · 提示走**托盘气泡**（⭐ 用户明确要求「不弹框、别打断」），
     单击托盘图标打开已运行实例的页面；第二个实例返回**非 0** 退出码；
   · 任何异常都放行（锁机制本身不许把程序拦死）。

B. 函数级
   `_try_acquire_single_instance(port)`：首次 True，**同端口再抢 False**，
   换个端口又 True（多实例支持没被破坏）。

C. 端到端（真起进程，最硬的一条）
   起一个真实例占住端口
     → **本进程**去抢同一端口应当抢不到（证明锁跨进程生效）
     → 再起第二个进程，它应当**自己退出**、退出码非 0、输出里有「已经运行」
     → 杀掉第一个之后锁要能**重新抢到**
       （🔴 这条最容易被漏掉：锁若不随进程释放，「关了再开」会被自己拦住）。

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
_fn = _src.split("def _try_acquire_single_instance")[1].split("\ndef ")[0]

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
expect("⭐⭐ 双击场景改用**托盘气泡**提示（不再用模态框打断用户）",
       "pystray" in _src and "ic.notify(" in _src)
expect("🔴 确认已经**没有** MessageBoxW（用户明确要求不弹框）",
       "MessageBoxW" not in _src)
expect("提示文案里说清「已经在运行」", "已经在运行" in _src)
expect("⭐ 提示会把已运行实例的页面带出来（webbrowser.open）",
       "webbrowser.open" in _src)
expect("⭐ 气泡文案写的是「单击**托盘图标**」——"
       "pystray 不转发 NIN_BALLOONUSERCLICK，写成「点气泡」就是撒谎",
       "单击托盘图标即可打开页面" in _src and "点气泡打开" not in _src)
expect("⭐ 托盘提示有超时收摊（用户不理它，别在通知区留个没人管的图标）",
       "icon.stop()" in _src and "threading.Timer(" in _src)
expect("⚠️ 超时用 Timer 而不是在 setup 里 wait（setup 跑在消息循环之前，"
       "在那儿阻塞会让托盘图标点不动）",
       "def _setup(ic):" in _src and "done.wait(" not in _src)
expect("第二个实例返回**非 0** 退出码（启动器 / 脚本能感知到）", "return 3" in _src)
expect("⭐ 锁机制任何异常都放行（不许因为它自己出问题就起不来）",
       _fn.count("return True") >= 3, f"return True x{_fn.count('return True')}")
expect("非 Windows 有兜底（lock 文件 + flock）", "fcntl.flock" in _src)

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
        rc, out = None, ""
        try:
            out, _ = p2.communicate(timeout=40)
            rc = p2.returncode
        except subprocess.TimeoutExpired:
            p2.kill()
            out, _ = p2.communicate()
        expect("⭐⭐ 第二个实例**自己退出**了（没赖着不听）", rc is not None, f"rc={rc}")
        expect("⭐⭐ 第二个实例退出码非 0", bool(rc), f"rc={rc}")
        expect("⭐⭐ 第二个实例说清了「已经运行」", "已经在运行" in (out or ""),
               f"输出尾部：{(out or '')[-240:]!r}")
        expect("第二个实例**没有**去抢端口（它是被锁拦下的，不是端口报错）",
               "address already in use" not in (out or "").lower(),
               f"输出尾部：{(out or '')[-240:]!r}")
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
