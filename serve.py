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

import uvicorn


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

    menu = pystray.Menu(
        pystray.MenuItem("打开助手", _open, default=True),
        pystray.MenuItem("停止服务", _stop),
    )
    icon = pystray.Icon("nan_yuan_xk", img, "南苑抢课助手", menu)
    return icon


def pick_state_dir(port: int, mock: bool) -> Path | None:
    """给这个实例挑一个**隔离的**清单落盘目录；返回 None = 用户自己指定了，别动。

    为什么要按端口/模式隔离：会写清单的接口是 `POST /api/plan`，而自检脚本
    （tests/smoke_ui.py）也会调它。2026-09-29 出过「跑测试把用户手工攒的清单清空」
    的事故；清单落盘之后，同样的手滑会从「内存被清」升级成「磁盘文件被覆盖」。
    所以默认值必须取安全的那一侧：

        · 8720（用户日常端口）        → <项目根>/state/plan.json
        · 其它端口（隔离测试实例）    → <项目根>/state/p<端口>/plan.json
        · --mock（模拟模式）          → <项目根>/state/mock/plan.json

    想完全关掉落盘，把 `XK_STATE_DIR` 显式设成 `off`。
    """
    if os.environ.get("XK_STATE_DIR", "").strip():
        return None  # 用户显式指定（含 off）→ 一律尊重
    root = Path(__file__).resolve().parent
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
        try:
            _server.should_exit = True
        except Exception:
            pass
        # 稍等让 uvicorn 停，然后退出进程
        threading.Timer(1.0, lambda: os._exit(0)).start()

    icon = _make_tray_icon(a.port, _stop_all)
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
