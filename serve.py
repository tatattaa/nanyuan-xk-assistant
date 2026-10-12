"""启动本地 Web 界面。

用法：
    python serve.py                 # 只听本机（127.0.0.1:8720）
    python serve.py --lan           # 同时监听局域网（手机可访问）
    python serve.py --port 9000     # 换端口
    python serve.py --mock          # 模拟模式（本地假教务，不碰真实教务）

设计取舍：
    默认只听 127.0.0.1 —— 避免把「能操作你教务账号的界面」暴露到整个局域网。
    需要手机访问时才显式加 --lan，并在控制台明确提示风险。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from pathlib import Path

# 允许从项目根直接运行
sys.path.insert(0, str(Path(__file__).parent))

# ⚠️ 打包成 exe 后，Windows 控制台默认是 GBK，打印启动横幅里的 emoji（⛔/🟢/🟡）
# 会抛 UnicodeEncodeError 直接崩掉（2026-10-01 打包验收踩到）。这里把 stdout/stderr
# 强制切成 UTF-8（失败则降级为忽略编码错误），保证 exe 和源码两种跑法都稳。
#
# ⭐ 窗口模式（console=False，无黑窗）下 sys.stdout/sys.stderr 是 None，
#   print 会直接崩。所以先判断：None 时把 stdout/stderr 重定向到日志文件，
#   让横幅/口令/报错都写进文件（方案 B：后台静默运行 + 日志文件）。
def _runtime_log_path() -> Path:
    """日志文件放哪：默认 exe/脚本同目录下的 logs/（通用化，别人机器也能用）。

    可用环境变量 XK_LOG_DIR 覆盖成任意目录（如 C:\\qiangke\\logs）。
    目录不存在会自动建；建失败则降级到 exe 或脚本同目录。
    """
    # 1) 用户显式指定（XK_LOG_DIR）
    env = os.environ.get("XK_LOG_DIR")
    if env:
        try:
            d = Path(env)
            d.mkdir(parents=True, exist_ok=True)
            return d / "运行日志.log"
        except OSError:
            pass
    # 2) 默认：exe/脚本同目录的 logs/
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    d = base / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = base
    return d / "运行日志.log"


# ⭐ 是否窗口模式（console=False 打包，无黑窗）：在重定向前先记下。
#   窗口模式下 sys.stdout 原本是 None，需要日志文件兜底 + 托盘图标提供退出。
_WINDOWED = (sys.stdout is None) or (sys.stderr is None)

# ⭐ 日志轮转：单文件上限 1MB，最多保留 3 份（.log → .log.1 → .log.2，最旧的删掉），
#   总占用封顶约 3MB，避免「运行日志.log」追加写无限增长（2026-10-01 用户提出占空间担忧）。
_MAX_LOG_BYTES = 1 * 1024 * 1024
_MAX_LOG_FILES = 3


def _open_runtime_log() -> object:
    """打开轮转日志文件：超过上限先把最旧的滚掉，再以追加方式打开。"""
    path = _runtime_log_path()
    if path.exists() and path.stat().st_size >= _MAX_LOG_BYTES:
        oldest = path.with_name(f"{path.name}.{_MAX_LOG_FILES - 1}")
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass
        for i in range(_MAX_LOG_FILES - 2, -1, -1):
            src = path if i == 0 else path.with_name(f"{path.name}.{i}")
            dst = path.with_name(f"{path.name}.{i + 1}")
            if src.exists():
                try:
                    src.replace(dst)
                except OSError:
                    pass
    return open(path, "a", encoding="utf-8", buffering=1)


if sys.stdout is None or sys.stderr is None:
    _logf = _open_runtime_log()
    if sys.stdout is None:
        sys.stdout = _logf
    if sys.stderr is None:
        sys.stderr = _logf

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ===========================================================================
# 单实例：**同一个端口只允许一个进程**
# ===========================================================================
# 需求（2026-10-12 用户提出）：exe 重复双击时不要再起一个实例，而是提示
# 「已经启动了」并把页面带出来。旧行为是第二个实例闷头去抢端口 / 再开一遍
# 浏览器 / 多出一个托盘图标，用户看到的是「点了没反应 + 浏览器多开」。
#
# 🔴 **必须按端口锁，不能做成「全局只准一个」**：本程序本来就支持多实例并存
#    —— 落盘目录按端口隔离（见 `pick_state_dir`），自检脚本会另起 8721/8731 的
#    隔离实例。全局锁会把它们一并锁死，等于把测试体系拆掉。**真正的冲突源是端口**。
#
# ⚠️ 为什么不拿「连一下端口通不通」当判据：那样分不清「是**本程序**在跑」还是
#    「别的程序恰好占了这个端口」。后者应当由 uvicorn 老实报「端口被占用」，
#    而不是弹一句「已启动」把用户引到一个**根本不是我们的**页面上。
#
# 🔴 任何异常都返回 True（放行）：单实例是**体验优化**，绝不能因为它自己
#    出问题就让程序起不来。
_SINGLE_INSTANCE_HANDLE: object | None = None   # ⚠️ 必须留引用，被 GC 掉锁就没了
_ERROR_ALREADY_EXISTS = 183                     # ERROR_ALREADY_EXISTS


def _try_acquire_single_instance(port: int) -> bool:
    """占住这个端口的单实例锁。True = 可以启动；False = 该端口已有实例在跑。"""
    global _SINGLE_INSTANCE_HANDLE

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
            # 「Local\」= 当前登录会话；换用户登录各持一份（桌面程序够用，
            # 且不需要创建全局对象所需的额外权限）
            name = f"Local\\nanyuan-xk-assistant-port-{port}"
            handle = k32.CreateMutexW(None, False, name)
            err = ctypes.get_last_error()
            if not handle:
                return True                      # 建不出来就放行，别拦着自己
            if err == _ERROR_ALREADY_EXISTS:
                k32.CloseHandle(handle)
                return False
            _SINGLE_INSTANCE_HANDLE = handle     # 持有到进程结束
            return True
        except Exception:
            return True

    # 非 Windows（Linux / macOS / Git Bash 里的非 Windows Python）：lock 文件 + flock。
    # 进程退出（含被 kill）时由内核自动释放。
    try:
        import fcntl
        import tempfile
    except ImportError:
        return True
    try:
        f = open(Path(tempfile.gettempdir()) / f"nanyuan-xk-assistant-{port}.lock", "w")
    except Exception:
        return True
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    except Exception:
        f.close()
        return True
    _SINGLE_INSTANCE_HANDLE = f
    return True


def _open_url(url: str) -> None:
    """尽力打开浏览器；失败不抛（打不开页面不该让程序崩）。"""
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


def _ask_running_instance_to_notify(port: int, title: str, text: str) -> bool:
    """请**已经在跑的那个实例**用它自己的托盘图标弹一条气泡。True = 已送达。

    ⭐ 为什么不自己弹（2026-10-12 用户反馈后改的）：
        pystray 的 `Icon` 就是「通知区里的一个图标」。第二个实例为了弹一次气泡
        而**新建一个图标**，通知区就会**多出一个重复图标**留在那儿（原来的实现要
        空转 30s 才收摊）。改成借主实例的图标弹 —— 这里 POST 完就能立刻退出：
        通知区不多图标、也不留一个空转进程。

    ⚠️ 失败不抛：主实例可能是**旧版本**（没这个接口）、也可能压根没托盘。
       返回 False，由调用方自己决定怎么退化。
    """
    try:
        import json
        import urllib.request

        body = json.dumps({"title": title, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/notify",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # ⚠️ 显式绕开代理：打 127.0.0.1 走代理会被拦成 502（本机踩过）。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            return bool(json.loads(resp.read() or b"{}").get("ok"))
    except Exception:
        return False


def _notify_already_running(port: int, *, open_page: bool) -> None:
    """第二次启动：**请主实例弹托盘气泡**提示「已经在跑了」，自己立刻退出。

    ⚠️ 为什么不用 MessageBox（2026-10-12 用户要求改掉）：模态框会**打断**用户 ——
       他只是想再打开一次页面，却得先点掉一个对话框。
    ⚠️ 为什么**不自己建托盘图标**（2026-10-12 用户反馈后改）：pystray 的 `Icon`
       就是「通知区里的一个图标」，自建一个会让通知区**多出一个重复图标**；
       而且为了显示它还得让这个进程空转。正解 = 借主实例**已有**的图标弹。
    ⚠️ 为什么是「单击**托盘图标**」而不是「点气泡本体」：pystray 的 Win32 后端
       只转发 `WM_LBUTTONUP` / `WM_RBUTTONUP`（`pystray/_win32.py::_on_notify`），
       **不转发 `NIN_BALLOONUSERCLICK`** —— 「点气泡」在这条技术路线上做不到。
       所以气泡文案写的是「单击托盘图标」，**不撒谎**。
    """
    text = "南苑抢课助手已经在运行了，无需重复启动。"
    url = f"http://127.0.0.1:{port}"
    tip = f"{text}\n单击托盘图标即可打开页面。"

    if not open_page:
        print(f"{text}（--no-browser：不打开页面）")
        return

    if _ask_running_instance_to_notify(port, "南苑抢课助手 · 已启动", tip):
        return                                    # 主实例已经把气泡弹出来了，收工

    # 主实例没有托盘（终端启动 / pystray 不可用）/ 是没这个接口的旧版本 →
    # 退化成最直接的做法：把这页面给他打开。绝不因为「提示方式」失败就什么都不做。
    print(text)
    print(f"  页面地址：{url}（正在为你打开）")
    _open_url(url)

import uvicorn  # noqa: E402  —— 放在单实例代码之后，让「第二次启动」路径尽量早返回


def local_ip() -> str:
    """取本机在局域网中的 IP（不发实际流量，靠 UDP connect 探测出口）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def open_browser_when_ready(urls: list[str], port: int, wait_s: float = 8.0) -> None:
    """等服务起来后用系统默认浏览器打开页面（后台线程，不阻塞主进程）。

    - 先探测本机端口是否可连（uvicorn 起好后端口就 LISTENING 了）；
    - 超时仍连不上也照开（万一探测方式在个别环境失灵，别让用户白等）；
    - 只开一次，异常静默吞掉（开浏览器失败不该拖垮服务本身）。
    """
    import threading
    import webbrowser

    def _run() -> None:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        for u in urls:
            try:
                webbrowser.open(u)
            except Exception as e:
                print(f"  ⚠️ 自动打开浏览器失败（{u}）：{e}")

    threading.Thread(target=_run, daemon=True).start()


#: 用户日常使用的端口。只有它才共享项目根下的 `state/` 清单。
MAIN_PORT = 8720


def _tray_image():
    """托盘图标：优先加载 assets/icon-64.png；找不到退回现场画的简易方块。

    ⚠️ 必须保留 RGBA（透明通道）——托盘图标圆角外是透明的，转成 RGB 会把透明区
    压成黑色，托盘就出现一圈黑边（2026-10-02 用户反馈）。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    # exe 模式：图标文件打进包里（sys._MEIPASS/assets/）；源码模式：项目根 assets/
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    png = base / "assets" / "icon-64.png"
    if png.exists():
        try:
            img = Image.open(png)
            return img if img.mode == "RGBA" else img.convert("RGBA")
        except Exception:
            pass
    # 兜底：透明底 + 蓝底圆角方块 + 书本（没有文件也能有图标，且无黑边）
    from PIL import ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(47, 111, 235, 255))
    d.polygon([(16, 16), (29, 12), (29, 50), (16, 52)], fill=(255, 255, 255, 255))
    d.polygon([(48, 16), (35, 12), (35, 50), (48, 52)], fill=(235, 240, 255, 255))
    return img


def _make_tray_icon(port: int, stop_cb) -> object:
    """创建一个系统托盘图标（窗口模式下用，提供「打开助手 / 停止服务」）。

    `stop_cb`：点「停止服务」时调用，负责关掉 uvicorn 并让进程退出。
    返回 pystray.Icon；若 pystray 不可用则返回 None（不弹托盘，退化回纯后台）。
    """
    try:
        import pystray
    except Exception as e:
        print(f"  ⚠️ 托盘图标不可用（{e}），将仅后台运行（停止请用任务管理器）")
        return None

    img = _tray_image()
    if img is None:
        print("  ⚠️ Pillow 不可用，无法加载托盘图标")
        return None

    def _open(_icon, _item):
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")

    def _stop(_icon, _item):
        _icon.stop()
        stop_cb()

    def _terminal(_icon, _item):
        _open_log_console()

    menu = pystray.Menu(
        pystray.MenuItem("打开助手", _open, default=True),
        pystray.MenuItem("显示终端", _terminal),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("停止服务", _stop),
    )
    icon = pystray.Icon("nan_yuan_xk", img, "南苑抢课助手", menu)
    return icon


# ⭐ 「显示终端」：托盘菜单开一个**实时跟随日志**的控制台窗口（2026-10-02 用户要求）。
#   双击 exe 是窗口模式（console=False），**看不到任何输出** —— 出问题时无从排查。
#   这里用 Windows 自带的 PowerShell 新开一个控制台：先打两行说明，再
#   `Get-Content -Wait` 实时跟随日志文件（tail -f 效果），并保持交互（-NoExit）。
#
#   ⚠️ 必须 `CREATE_NEW_CONSOLE`：exe 是 **GUI 子系统**进程，自身没有控制台可供继承，
#      不显式新开控制台的话，子进程会被静默创建却永远不出现窗口。
#   ⚠️ 必须 `chcp 65001` + `[Console]::OutputEncoding = UTF8`：日志里有 emoji
#      （🟢/🟡/⛔），控制台默认代码页 GBK 打不出来会变成 `?`。
#   ⚠️ 日志文件**只有窗口模式才写**（`sys.stdout is None` 时把 stdout 重定向到文件），
#      而托盘也只存在于窗口模式 → 有托盘就一定有日志文件，不必担心空窗。
_CONSOLE_PROC: object = None


def _open_log_console() -> None:
    """打开一个实时跟随运行日志的终端窗口（已开着就不重复开）。"""
    global _CONSOLE_PROC

    if sys.platform != "win32":
        print("  ⚠️ 「显示终端」目前只支持 Windows；日志文件路径见启动横幅「日志文件」一行")
        return
    if _CONSOLE_PROC is not None and _CONSOLE_PROC.poll() is None:
        print("  日志终端已在运行（未重复打开）")
        return

    path = _runtime_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass
    lit = str(path).replace("'", "''")   # PowerShell 单引号串内的转义写法：' → ''

    ps = ";".join([
        "$Host.UI.RawUI.WindowTitle = '南苑抢课助手 · 运行日志'",
        "chcp 65001 | Out-Null",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "Write-Host '── 南苑抢课助手 · 运行日志（实时跟随）──' -ForegroundColor Cyan",
        f"Write-Host '日志文件：{lit}' -ForegroundColor DarkGray",
        "Write-Host 'Ctrl+C 可停止跟随，回到提示符后本窗口可当普通终端用。' -ForegroundColor DarkGray",
        "Write-Host ''",
        f"Get-Content -LiteralPath '{lit}' -Wait -Tail 300 -Encoding UTF8",
    ])

    import subprocess

    try:
        _CONSOLE_PROC = subprocess.Popen(
            ["powershell", "-NoLogo", "-NoExit", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            close_fds=True,
        )
        print("  已打开日志终端窗口")
    except Exception as e:
        _CONSOLE_PROC = None
        print(f"  ⚠️ 打开日志终端失败：{e}")


def _close_log_console() -> None:
    """停止服务时把日志终端一并关掉（否则会留下一个孤儿窗口）。"""
    global _CONSOLE_PROC
    proc, _CONSOLE_PROC = _CONSOLE_PROC, None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass


def _browser_watchdog(
    stop_cb,
    icon_getter,
    grace_s: float = 20.0,
    notify_lead_s: float = 8.0,
) -> None:
    """窗口模式专用：浏览器全部关闭一段时间后，自动退出后端。

    ⭐ 2026-10-02 用户要求「关闭浏览器时同步关闭后端」。设计要点：

      · **信号** = 前端页面对 `/api/presence` 的 SSE 长连接。关页/关浏览器时它
        **真实断开**，且不受「浏览器后台标签节流定时器」的影响 —— 比轮询可靠。
      · **宽限期** `grace_s`：断开后要持续这么久仍无人重连才动手。刷新页面、
        网络抖动都会在这段时间内自动重连，从而避免误退。
      · `presence_idle == 0` 也包含「从来没连过」（例如 --no-browser 起服务、
        或前端尚未建连）→ **绝不退出**，防止把后端自己关掉。
      · **有抢课/蹲课在跑时**：先弹托盘通知提醒，再给 `notify_lead_s` 秒「最后机会」
        （期间浏览器若回来则取消退出），之后照退 —— 用户 2026-10-02 明确选定。
    """
    from ui.state import RUNTIME

    notified_at: float | None = None
    while True:
        time.sleep(1.5)
        idle = RUNTIME.presence_idle
        if idle <= 0:
            notified_at = None          # 有人在线（或从未离线）→ 清掉提醒状态
            continue
        if idle < grace_s:
            continue                    # 还在宽限期内（刷新 / 抖动）

        if RUNTIME.has_active_task:
            if notified_at is None:
                icon = icon_getter()
                msg = "检测到浏览器已关闭，但抢课仍在进行中，后端将在几秒后退出。"
                if icon is not None:
                    try:
                        icon.notify(msg, "南苑抢课助手")
                    except Exception:
                        pass            # 通知失败不影响退出流程
                print(f"  ⚠️ 浏览器已关闭，但仍有任务在跑 → {int(notify_lead_s)} 秒后退出后端")
                notified_at = time.time()
                continue
            if time.time() - notified_at < notify_lead_s:
                continue                # 「最后机会」窗口内，等用户可能回来

        print("  浏览器已关闭（且无人在线），后端自动退出。")
        # ⚠️ 必须先让托盘图标干净消失再退：`_stop_all` 最终走 `os._exit(0)` 强杀进程，
        #    而强杀不会触发 pystray 的移除逻辑 → 通知区会留下「残影图标」，要等鼠标
        #    划过才消失。（托盘点「停止服务」那条路不经过这里，它自己会 icon.stop()。）
        icon = icon_getter()
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        stop_cb()
        return


def _app_root() -> Path:
    """应用根目录：`state/`（落盘）相对它定位。

    🔴 不能用 `Path(__file__).parent` 了事 —— **exe（onefile）模式下 `__file__`
    指向 PyInstaller 的临时解压目录 `%TEMP%\\_MEIxxxxx\\`**，而那个目录
    「**每次启动都是新的随机名**、进程退出就被删」。拿它当落盘根，等于每次
    都从零开始：清单 / 蹲课清单 / 课程快照**全都活不过一次重启**，界面上的
    「📂 上次数据」也永远是空的。

    （2026-10-12 实测确认：跑 `dist/南苑抢课助手.exe` 时，state 被建到了
     `%TEMP%\\_MEI000013c02\\state\\`，而用户真正的数据在 `<项目根>\\state\\`。）

    规则：
      · 源码模式 → `serve.py` 所在目录（= 项目根）
      · exe 模式 → exe 自己所在目录；若它处在 `<项目根>/dist/xxx.exe` 这种打包
                   产物布局（上一级还留着 `serve.py`），则回到 `<项目根>` ——
                   好让「同一份代码，源码跑 / exe 跑」看到的是**同一批数据**。
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    exe_dir = Path(sys.executable).resolve().parent
    if (exe_dir.parent / "serve.py").exists():
        return exe_dir.parent
    return exe_dir


def pick_state_dir(port: int, mock: bool) -> Path | None:
    """给这个实例挑一个**隔离的**清单落盘目录；返回 None = 用户自己指定了，别动。

    为什么要按端口/模式隔离：会写清单的接口是 `POST /api/plan`，而自检脚本
    （tests/smoke_ui.py）也会调它。2026-09-29 出过「跑测试把用户手工攒的清单清空」
    的事故；清单落盘之后，同样的手滑会从「内存被清」升级成「磁盘文件被覆盖」。
    所以默认值必须取安全的那一侧：

        · 8720（用户日常端口）        → <应用根>/state/plan.json
        · 其它端口（隔离测试实例）    → <应用根>/state/p<端口>/plan.json
        · --mock（模拟模式）          → <应用根>/state/mock/plan.json

    ⚠️「应用根」怎么取见 `_app_root()` —— exe 模式下**必须**避开 PyInstaller 的
    临时解压目录，否则落盘等于没落（2026-10-12 修）。

    想完全关掉落盘，把 `XK_STATE_DIR` 显式设成 `off`。
    """
    if os.environ.get("XK_STATE_DIR", "").strip():
        return None  # 用户显式指定（含 off）→ 一律尊重
    root = _app_root()
    if mock:
        return root / "state" / "mock"
    if port == MAIN_PORT:
        return root / "state"
    return root / "state" / f"p{port}"


def main() -> int:
    p = argparse.ArgumentParser(description="南苑抢课助手 · 本地 Web 界面")
    p.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，--lan 时为 0.0.0.0）")
    p.add_argument("--port", type=int, default=8720, help="端口（默认 8720）")
    p.add_argument("--lan", action="store_true", help="允许局域网访问（手机可打开）")
    p.add_argument(
        "--mock",
        nargs="?",
        const="captures/2026-09-29-open2",
        default=None,
        metavar="抓包目录",
        help="模拟模式：起本地模拟教务（抓包回放）并自动连上，不需要浏览器/Cookie。"
        "不写目录时默认 captures/2026-09-29-open2",
    )
    p.add_argument("--mock-port", type=int, default=0, help="模拟教务端口（默认自动分配）")
    p.add_argument(
        "--real",
        action="store_true",
        help="强制使用真实教务（清掉可能残留的 XK_SCHOOL_URL 环境变量）。"
        "与 --mock 互斥；正常不带 --mock 启动时也建议加上以防环境变量残留。",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开浏览器（默认会在同一浏览器开教务系统与南苑抢课助手两个标签页，最后停留在抢课助手）。",
    )
    p.add_argument(
        "--access-key",
        default=None,
        metavar="口令",
        help="访问口令：打开页面先输口令才能进入（防同网段他人访问）。"
        "不传则 --lan 时自动生成随机口令并打印在横幅；传 none 则关闭口令校验。",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="打印调试日志")
    a = p.parse_args()

    if a.real and a.mock:
        print("✗ --real 与 --mock 互斥，二选一")
        return 2

    # -- 单实例：同一端口只准一个进程（2026-10-12 用户需求）------------------
    # 重复双击 exe 时不要再起第二个（会去抢端口、再开一遍浏览器、多一个托盘图标），
    # 而是明确告诉他「已经在跑了」并把这个页面带出来。
    # ⚠️ 放在最前面：都确定要退出了，就没必要再清理环境变量 / 建日志 / 起假教务。
    if not _try_acquire_single_instance(a.port):
        _notify_already_running(a.port, open_page=not a.no_browser)
        print(f"✗ 端口 {a.port} 上已经有一个实例在运行了，本次启动直接退出。")
        return 3

    # --real：把可能残留的「指向假教务」的环境变量清掉，确保打真实教务。
    # 这是防呆：XK_SCHOOL_URL 是进程级变量，若被留在 shell 里，
    # 不加 --mock 也会悄悄打到本地假教务（2026-09-30 补的兜底）。
    if a.real:
        _leftover = os.environ.pop("XK_SCHOOL_URL", None)
        if _leftover:
            print(f"  ℹ️ 已清除残留的 XK_SCHOOL_URL={_leftover}（--real）")
    elif not a.mock:
        # 没加 --mock 也没加 --real：检查环境里有没有人偷偷留了它。
        _stale = os.environ.get("XK_SCHOOL_URL", "").strip()
        if _stale:
            print("=" * 56)
            print(f"  ⚠️  检测到环境变量 XK_SCHOOL_URL={_stale}")
            print("      这会让程序【打到本地假教务】而不是真实教务！")
            print("      若你确实想用真实教务：请用 `python serve.py --real` 启动，")
            print("      或先执行 unset XK_SCHOOL_URL / set XK_SCHOOL_URL= 清掉它。")
            print("=" * 56)
            print()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # -- 抢课清单落盘目录：必须在 import ui.* 之前定好 -----------------------
    # ui.state 在 import 时就把 STATE_DIR 定下来并尝试读回清单，所以这一步
    # 只能放在最前面。
    _sd = pick_state_dir(a.port, bool(a.mock))
    if _sd is not None:
        os.environ["XK_STATE_DIR"] = str(_sd)

    from ui.app import create_app  # noqa: E402  —— 依赖上面的 XK_STATE_DIR
    from ui.state import RUNTIME, plan_path  # noqa: E402

    host = a.host or ("0.0.0.0" if a.lan else "127.0.0.1")

    # -- 访问口令（防同网段他人访问）---------------------------------------
    # 口令存内存，通过 XK_ACCESS_KEY 环境变量传给 app（与 XK_SCHOOL_URL 同风格，进程级）。
    # 规则：显式 --access-key 优先；否则 --lan 时自动生成随机口令（打印在横幅）；
    #       本机（非 --lan）且未显式指定 → 不开启（localhost 本身是隔离的）。
    _access_key = ""
    if a.access_key is not None:
        _access_key = "" if a.access_key.strip().lower() == "none" else a.access_key.strip()
    elif a.lan:
        import secrets
        _access_key = "".join(secrets.choice("0123456789") for _ in range(6))
    if _access_key:
        os.environ["XK_ACCESS_KEY"] = _access_key
    elif "XK_ACCESS_KEY" in os.environ:
        os.environ.pop("XK_ACCESS_KEY", None)

    # -- 模拟模式：本地起「假教务」并自动建立会话 ---------------------------
    if a.mock:
        from mock_server import start_mock
        from ui.state import RUNTIME
        from core.config import Credential

        httpd, mock_port = start_mock(a.mock, port=a.mock_port)
        mock_url = f"http://127.0.0.1:{mock_port}/jwglxt/"
        os.environ["XK_SCHOOL_URL"] = mock_url  # attach_session 会读它
        cred = Credential(cookie_header="JSESSIONID=mock; route=mock", source="mock")
        sess = RUNTIME.attach_session(cred)
        try:
            sess.store = sess.client.init()
            sess.inited = True
            sess.is_open = sess.client.is_open
        except Exception as e:
            print(f"  ⚠️ 模拟教务 init 失败：{e}")
            return 1
        print("=" * 56)
        print("  【模拟模式】本地模拟教务 + 抓包回放")
        print(f"  抓包存档:  {a.mock}")
        print(f"  模拟教务:  {mock_url}（项目所有请求都打到它，不碰真实教务）")
        print(f"  会话:      已自动建立（{len(sess.client.tabs)} 个课程 Tab，已选数据来自抓包快照）")
        print("  注意:      提交/退课会返回「写操作不支持回放」，不会真占位")
        print("=" * 56)
        print()

    print("=" * 56)
    print("  南苑抢课助手 · 本地 Web 界面")
    print("=" * 56)
    print(f"  本机访问:  http://127.0.0.1:{a.port}")
    if _access_key:
        print(f"  🔐 访问口令:  {_access_key}   （打开页面需先输这个口令）")

    # 清单落盘位置：多实例并存时一眼看清「我这个实例的清单存在哪」，
    # 免得在 8721 上攒了半天清单，回头在 8720 上找不到。
    _plan_file = plan_path()
    if _plan_file is None:
        print("  清单落盘:  ⛔ 已关闭（纯内存；进程重启清单会丢）")
    else:
        print(f"  清单落盘:  {_plan_file}")
        if a.port != MAIN_PORT and not a.mock:
            print("             ↑ 非日常端口 → 独立目录，不影响你 8720 那份清单")
    if RUNTIME.plan:
        print(f"  已恢复清单: {len(RUNTIME.plan.items)} 项（来自上次落盘）")

    # ⭐ 明确打印「现在到底打向哪个教务」——切来切去最容易在这翻车。
    # ⚠️ 必须按**实际生效的 school** 判断（即 XK_SCHOOL_URL 到底有没有生效），
    # 不能按 a.mock 猜：残留的环境变量会让「没加 --mock」也打到假教务。
    from ui.state import _school_from_env
    from core.config import DEFAULT_SCHOOL

    _eff = _school_from_env()
    if _eff is not None:
        print(f"  教务目标:  🟡 本地模拟教务  {_eff.base_url}")
        if not a.mock:
            print("             ↑ 注意：这不是 --mock 造成的，而是环境变量残留！")
    else:
        print(f"  教务目标:  🟢 真实教务  {DEFAULT_SCHOOL.name}  {DEFAULT_SCHOOL.base_url}")

    if a.lan:
        ip = local_ip()
        print(f"  手机访问:  http://{ip}:{a.port}   （需与电脑同一 WiFi）")
        print()
        print("  ⚠️  已开启局域网访问：同一网络下的任何设备都能打开此页面。")
        if _access_key:
            print(f"      🔐 已有访问口令保护（口令 {_access_key}），输对才能进入。")
        print("      请勿在不安全的公共 WiFi 下使用，用完及时关闭。")
    else:
        print("  （仅本机可访问；要手机访问请加 --lan）")
    print("=" * 56)
    print()

    # 自动打开浏览器：同一浏览器里开「教务系统」+「南苑抢课助手」两个标签页，
    # 但**抢课助手放在最后**（最后打开的标签页成为当前停留页）——用户要求跳到教务
    # 但最终停留在抢课助手页面。加 --no-browser 关闭（CI / 无头环境用）。
    if not a.no_browser:
        _urls = []
        if not a.mock:
            _urls.append(DEFAULT_SCHOOL.base_url)   # 先开教务（真实教务模式）
        _urls.append(f"http://127.0.0.1:{a.port}")   # 最后开抢课助手 → 停留在此
        open_browser_when_ready(_urls, a.port)

    if not _WINDOWED:
        # 控制台模式：正常阻塞跑，Ctrl+C / 关窗即退。
        uvicorn.run(create_app(), host=host, port=a.port, log_level="warning")
        return 0

    # 窗口模式（无黑窗）：uvicorn 在后台线程跑，主线程跑托盘图标。
    import threading

    _app = create_app()
    _server = uvicorn.Server(uvicorn.Config(_app, host=host, port=a.port, log_level="warning"))

    _svr_thread = threading.Thread(target=_server.run, daemon=True)
    _svr_thread.start()

    def _stop_all() -> None:
        # ⭐ 日志终端是本进程开的子进程，**不会随 os._exit 一起消失** → 显式关掉，
        #   否则「停止服务」之后会留下一个孤零零的日志窗口。
        _close_log_console()
        # ⭐ 先通知页面「后端要停了」：前端据此显示「服务已停止」并尝试关闭标签页。
        #   （浏览器不允许服务器强制关闭标签页，所以只能「通知 + 尽力」。）
        try:
            from ui.state import RUNTIME

            RUNTIME.request_shutdown()
        except Exception:
            pass
        try:
            _server.should_exit = True
        except Exception:
            pass
        # 留 ~2s 让页面收到 shutdown 事件并处理完，再退出进程
        # （在线通道 1.5s 一帧，2s 足够它把事件推出去）。
        threading.Timer(2.0, lambda: os._exit(0)).start()

    icon = _make_tray_icon(a.port, _stop_all)

    # ⭐ 把「弹托盘气泡」这件事接到**主实例自己这个图标**上（2026-10-12）。
    #   第二次启动的那个进程会 POST /api/notify 来这里 —— 于是通知区用的是
    #   本来就在的那个图标，**不会多出一个重复图标**。
    if icon is not None:
        try:
            from ui.app import set_notify_sink

            set_notify_sink(lambda title, text: icon.notify(text, title))
        except Exception as e:
            print(f"  ⚠️ 托盘通知出口未接上：{e}")

    # ⭐ 关闭浏览器 → 自动退出后端（仅窗口模式；源码/控制台模式不动）。
    #   见 _browser_watchdog 的设计说明：宽限期 + 有任务先提醒。
    threading.Thread(
        target=_browser_watchdog,
        args=(_stop_all, lambda: icon),
        daemon=True,
    ).start()

    if icon is not None:
        try:
            icon.run()  # 阻塞，直到托盘菜单点「停止服务」
        except Exception as e:
            print(f"  ⚠️ 托盘运行异常：{e}")
        return 0
    # 没有托盘（pystray 不可用）：纯后台跑，靠任务管理器/日志提示退出。
    print("  （无托盘图标，服务后台运行中；停止请用任务管理器结束本进程）")
    try:
        _svr_thread.join()
    except KeyboardInterrupt:
        _stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
