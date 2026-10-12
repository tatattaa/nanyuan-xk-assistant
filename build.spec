# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller 打包配置（南苑抢课助手 · 本地 Web 界面）。

入口 = serve.py（本地 Web UI，默认 127.0.0.1:8720）。

⭐⭐ **一份 spec 出两种形态**（2026-10-12 起），用环境变量 `XK_ONEFILE` 切换 ——
   两版**代码完全相同**，只有打包形态不同（下面的 excludes / hiddenimports 共用一份）：

    # ① onedir（默认，推荐）—— 目录树，启动快；配套 Inno 安装包
    pyinstaller --noconfirm --clean --distpath "dist/onedir-安装包版" build.spec
      → dist/onedir-安装包版/南苑抢课助手/{南苑抢课助手.exe, _internal/}

    # ② onefile（单文件绿色版）—— 就一个 exe，分发最省事，但启动慢
    XK_ONEFILE=1 pyinstaller --noconfirm --clean --distpath "dist/onefile-单文件版" build.spec
      → dist/onefile-单文件版/南苑抢课助手.exe

    静态文件（ui/static/*）两种形态都随包走。

⭐⭐ 为什么是 onedir 而不是 onefile（2026-10-12 改，实测依据见
   `.workbuddy/memory/2026-10-03.md` 第八十七/八十八轮）：
   onefile 每次启动都要把 ~46 MB 解压到 `%TEMP%\_MEIxxxxx\`、退出时再删掉同一批
   文件，而每一轮解压到的是**新的随机目录** → 系统文件缓存天生吃不饱，Windows 上
   每个文件还要过一次杀软扫描。
   实测（同机同刻，到 `/api/state` 返回 200）：
     · onefile 2,013 ms（1991/2026/2021）
     · onedir  1,508 ms（1501/1514/1510）→ **快 25%，省约 0.5 s，极稳定**
   `--help` 三元对比更能说明问题（同一 argparse 代码路径）：
     · onedir 1.09 s ≈ 纯「解释器 + 导入」成本
     · onefile 4.34 s → 多出来的 3.25 s **与导入无关**，全是「解压 46 MB + 退出删 46 MB」。
   ⚠️ 本机 NVMe 上只差 0.5 s，但在**慢盘 / 带杀软 / 虚拟机 / 共享文件夹**里会明显放大
      —— 那正是本项目用户实际遇到的场景（同一份 exe 在本机 2.0 s、在客户机 12–28 s）。
   ⚠️ 代价：分发从「一个 exe」变成「一个目录」（47 MB），发布时打包成 zip。

⚠️ 只打包「真实教务」链路所需的资源。模拟模式（--mock）依赖 captures/ 抓包存档，
   属开发/测试资产，**不**打进包（体积大且含原始接口流量）——打包后的 exe 仍可用
   `--real` 正常跑真实教务。
"""

import os

from PyInstaller.utils.hooks import collect_submodules

# ⭐ 形态开关：默认 onedir；`XK_ONEFILE=1` → 单文件。详见文件头。
_ONEFILE = os.environ.get("XK_ONEFILE", "").strip().lower() in ("1", "true", "yes", "on")

# uvicorn 有隐式导入的循环导入器，不显式收集会在打包后 import 失败
hiddenimports = (
    collect_submodules("uvicorn")
    + ["uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
       "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
       "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
       "uvicorn.lifespan", "uvicorn.lifespan.on"]
    + collect_submodules("pystray")
    + ["PIL", "PIL.Image", "PIL.ImageDraw"]
    + ["core.browser"]  # 函数内延迟 import，静态分析抓不到，需显式声明
)

# 🔴 `excludes` 里那一串**必须留着**，理由不是「我们用不到」，而是「它们是被别人
#    **蹭**进来的」—— 只看源码是找不到的：
#
#    · numpy ← **Pillow**。`PIL/_typing.py` 顶层写着 `import numpy.typing as npt`
#      （外面用 try/except ImportError 包着，属**纯可选**支持），可 PyInstaller 的
#      静态分析不看 try/except → 只要打包环境里装着 numpy，就把整包拖进来。
#      实测代价：`numpy.libs` 20.2 MB + `numpy` 6.1 MB ≈ **26 MB**
#      （2026-10-12 实测：23.4 MB → 33.7 MB，就是它干的）。
#      `serve.py::_tray_image()` 只用 PIL 画图、从不碰数组，排掉零副作用。
#    · tzdata ← **pydantic → zoneinfo**（Windows 上没有系统时区库，得靠它兜底）。
#      ⚠️ **只占 0.49 MB，却是全包最大的启动杀手**：它把 605 个时区文件散着塞进
#      CArchive，而我们全包一共才 685 条 —— 88% 的**文件数**、1% 的**体积**。
#      onefile 要逐文件解压到 `_MEIxxxxx\`，Windows 上每个文件还要过一次杀软
#      扫描 → 605 × ~21 ms ≈ **13 秒**。这就是「明明体积砍了 10 MB，启动却从
#      3.7 s 变成 17.3 s」的全部原因（实测 confirmed）。
#      本项目**从不调用 `ZoneInfo("Asia/Shanghai")` 这类命名时区**，只用
#      `datetime.timezone.utc` 和 `timedelta(hours=8)`（见 `core/clock.py`），
#      排掉零影响。`_zoneinfo.pyd`（标准库的 C 模块，1 个条目）照常保留。
#    · pandas / python-dateutil ← 2026-10-09 被打包环境里装上的，本项目一行不用；
#      这次打包其实没进去（xref 里 0 次），列出只为**兜底**。
#    · matplotlib / scipy / IPython ← 同理兜底，防「装着就进包」。
#
#    ⭐ **教训：打包后要看两个数，不是只看体积** ——
#       ① 体积（`--help` 三元对比 / 归档解压总量）
#       ② **归档条目数**（`pyi-archive_viewer -l -r`）。条目数暴涨 = 每个文件一次
#          解压+扫描，秒数直接乘上去。`tzdata` 就是「体积看不出来、条目数骗不了人」
#          的典型。
#
#    ⚠️ 排查手法（下次体积/启动又不对了照做）：`pyi-archive_viewer -l -r <exe>`
#       按顶层包聚合条目数与体积 → 拿大户名去 `build/<name>/xref-build.html` 搜
#       `<a name="X"></a>` 后面的 `imported by:` 找**真凶**。别硬啃源码，找不到的。
#       详见 `.workbuddy/memory/2026-10-12.md`。
#
#    --- 2026-10-12 追加的三项（纯瘦身；⭐ 已实测「裁剪 ≠ 加速」，见下）---
#    · watchfiles ← **uvicorn 的 `--reload` 监视器**（`_rust_notify.pyd` 0.6 MB）。
#      本项目从不用 `--reload`（`uvicorn.run(..., log_level="warning")` 里没有它）。
#    · setuptools ← 打进 PYZ **130 个模块 / 0.81 MB**（纯 Python 里最大一坨），
#      外加 `pyi_rth_setuptools` 钩子。全仓 grep **零** `pkg_resources` /
#      `importlib.metadata` 用法；排除后已跑完整离线回归 + exe 端到端确认无副作用。
#    · pydoc_data ← `pydoc` 的联机帮助数据，无人使用。
#      ⚠️ **这三项对「启动秒数」的贡献≈0**（PYZ 是惰性的：没被 import 的模块本来就不加载），
#         收益是**磁盘体积与首次全盘扫描的文件数**。别指望它们提速。
excludes = [
    "tests", "captures",
    # 被 Pillow / pydantic 间接蹭进来的可选依赖，见上
    "numpy", "numpy.libs", "pandas", "pandas.libs",
    "tzdata",
    "dateutil", "matplotlib", "scipy", "IPython",
    # 2026-10-12：真·无用依赖（瘦身，不提速）
    "watchfiles", "setuptools", "pydoc_data",
]

a = Analysis(
    ["serve.py"],
    pathex=["."],
    binaries=[],
    # ⚠️ `schools/` = 多学校参数快照，随源码附带供切换；`assets/` = 图标。
    datas=[("ui/static", "ui/static"), ("assets/icon-64.png", "assets"), ("schools", "schools")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

# EXE 的公共参数（两种形态共用）—— 唯一的区别是 binaries/datas 塞进 exe 还是摆成目录。
_exe_kwargs = dict(
    name="南苑抢课助手",
    icon="assets/app.ico",  # exe 文件图标（assets/ 全套图标，gen_icon.py 生成）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # 无黑窗：后台静默运行（日志写文件，托盘图标提供退出）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if _ONEFILE:
    # —— ② onefile：binaries/datas 全塞进 exe，运行时自解压到 %TEMP%\_MEIxxxxx\ ——
    #    ⚠️ exe 文件名**不带** `-onefile` 后缀是有意的：用户拿到的就是一个能双击的
    #       「南苑抢课助手.exe」；形态由它所在的目录（`dist/onefile-单文件版/`）标记。
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        runtime_tmpdir=None,   # None = 用 %TEMP%\_MEIxxxxx（每轮都是新的随机目录）
        **_exe_kwargs,
    )
else:
    # —— ① onedir：**两步**写法 ——
    #    ① `EXE(..., exclude_binaries=True)` —— 只产出启动器 exe，**不把 binaries/datas 塞进去**；
    #    ② `COLLECT(...)` —— 把 exe + binaries + datas 一起摆进 `dist/<name>/`。
    #    🔴 关键就是 ①，别漏：漏了 `exclude_binaries=True` 会打回 onefile（体积立刻回到 24 MB）。
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_kwargs)

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="南苑抢课助手",
    )
