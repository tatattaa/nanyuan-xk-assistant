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
    excludes=["tests", "captures"],
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
