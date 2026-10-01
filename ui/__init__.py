"""UI 层：本地 Web 界面。

HANDOFF 5.3 阶段 4：先 CLI（已做，xk.py），再本地网页（FastAPI + 原生前端）。

为什么是本地 Web：
    localhost 天然绕开云服务器 / 域名 / 备案 / 小程序白名单全部障碍
    → 手机浏览器连同一局域网即可访问，手机端免安装。

约束（HANDOFF 5.2）：
    UI 层只调引擎层与核心层，不出现 HTTP 业务细节（不直接拼 Cookie、不解析返回体）。
"""

from ui.app import create_app

__all__ = ["create_app"]
