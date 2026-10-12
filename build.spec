# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（南苑抢课助手 · 本地 Web 界面）。

入口 = serve.py（本地 Web UI，默认 127.0.0.1:8720）。
产出：单文件 exe，静态文件（ui/static/*）打包进 _MEIPASS。

⚠️ 只打包「真实教务」链路所需的资源。模拟模式（--mock）依赖 captures/ 抓包存档，
   属开发/测试资产，**不**打进 exe（体积大且含原始接口流量）——打包后的 exe 仍可用
   `--real` 正常跑真实教务。
"""

from PyInstaller.utils.hooks import collect_submodules

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
excludes = [
    "tests", "captures",
    # 被 Pillow / pydantic 间接蹭进来的可选依赖，见上
    "numpy", "numpy.libs", "pandas", "pandas.libs",
    "tzdata",
    "dateutil", "matplotlib", "scipy", "IPython",
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
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
