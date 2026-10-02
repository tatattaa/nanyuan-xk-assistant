"""UI 层 HTTP 接口（FastAPI）。

分层约束：本文件只做「请求 → 引擎/核心层调用 → 响应」的转译。
不出现任何正方教务的 HTTP 细节（不拼 URL、不解析 HTML、不读 Cookie 值）。

接口一览：
    GET  /                    前端页面
    GET  /api/state           当前状态快照（含时钟校准与定时信息）
    POST /api/session         提交 Cookie 建立会话（并自动 init）
    POST /api/session/cdp     通过 CDP 抓取 Cookie 建立会话
    POST /api/session/password  账号密码登录建立会话（密码仅内存，不落盘）
    DEL  /api/session         清除会话
    GET  /api/session/check   探活：只发一个 GET 确认登录态还有效（顶部状态栏靠它不撒谎）
    POST /api/probe           重新 init 并返回上下文摘要
    GET  /api/clock           时钟校准状态（?samples=N 先采样）
    GET  /api/courses         查课程（query 只读，可指定 kklxdm；结果会顺手落盘）
    GET  /api/courses/snapshot/load  取回上次搜索落盘的课程数据（离线，未开放期用来回看）
    GET  /api/classes         查某课程教学班（可指定 kklxdm）
    GET  /api/tabs            课程类别 Tab 列表（每个 Tab 独立加密串）
    GET  /api/selected        查已选课程（?cached=1 只读内存缓存，不打教务）
    GET  /api/academic        学生学业情况统计（只读；课程性质学分对照 + 基础信息）
    POST /api/drop            退课（不可逆；资格由教务定，见 core/drop.py）
    POST /api/conflict        批量判定候选教学班是否与已选/清单时间冲突
    POST /api/plan            设置抢课清单（可带 start_at 定时开抢；带 base_version 走乐观锁）
    POST /api/plan/load       把磁盘上「上次保存的清单快照」加载进内存成为当前清单
    POST /api/start           启动抢课
    POST /api/stop            停止抢课
    GET  /api/events          增量拉取事件（?since=N）
    GET  /api/events/stream   SSE 实时事件流
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from core import (
    build_entry,
    drop_state,
    entries_from_rows,
    find_conflicts,
    get_credential,
    slots_from_dicts,
)
from core.errors import KIND_LABEL, FailureKind, XKError
from engine.events import Event, EventType, TaskState
from engine.plan import MAX_ATTEMPTS, Plan, PlanItem, normalize_retry_mode, parse_when, RETRY_ROUND_ROBIN
from ui.state import (
    RUNTIME,
    PlanConflict,
    load_courses_bucket,
    pick_courses_bucket,
    save_courses_bucket,
)

logger = logging.getLogger("xk.ui")

STATIC_DIR = Path(__file__).parent / "static"

# PyInstaller 打包后，`__file__` 指向临时解压目录（sys._MEIPASS），静态文件
# 被打包进了那个目录。这里做一次「冻结环境」适配：打包态下从 _MEIPASS 找静态文件。
if getattr(sys, "frozen", False):  # noqa: 仅打包态进入
    _meipass = getattr(sys, "_MEIPASS", None)
    if _meipass:
        STATIC_DIR = Path(_meipass) / "ui" / "static"


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class CookieBody(BaseModel):
    cookie: str = Field(..., description='形如 "JSESSIONID=xxx; route=yyy"')


class CdpBody(BaseModel):
    port: int = Field(9666, description="浏览器调试端口")


class PasswordBody(BaseModel):
    username: str = Field(..., description="学号")
    password: str = Field(..., description="密码（仅本机内存使用，不落盘、不上传）")
    # ⭐ 2026-10-02：登录与 init 拆开。with_init=False 时只登录（建会话）不拉上下文，
    #   让前端能「先切主界面、再异步加载」，把登录卡上的等待从 ~2.5s 降到 ~1.5s。
    #   默认 True = 保持旧行为（登录 + init 一次返回），向后兼容。
    with_init: bool = Field(True, description="是否紧接着执行 init（拉选课上下文）")


class PlanItemBody(BaseModel):
    kch_id: str
    do_id: str = ""
    # 教学班的**稳定** id（非加密）。前端从教学班列表带出来，用于令牌过期后
    # 「重新绑定同一个班」（铁律 #5）：do_id 是每次查询都换的加密令牌，
    # 而 jxb_id 不变，所以刷新令牌时只能靠它认回原来那个班。
    jxb_id: str = ""
    kklxdm: str = ""
    # Tab 下标；-1 = 未知。`kklxdm` 会重复（两个板块课都是 06），
    # 所以定位板块必须优先用下标，见 engine/runner.py::_tab_for。
    tab_index: int = -1
    kcmc: str = ""
    jsxx: str = ""
    # 学分。用来在界面上汇总「待加选学分」，让用户提前知道会不会撞上
    # 「本学期最高学分」上限（撞上了教务会直接驳回，见 core/errors.py）。
    xf: str = ""
    cxbj: str = "0"
    fxbj: str = "0"
    priority: int = 0
    # ⭐ 默认取 `engine/plan.py::MAX_ATTEMPTS`（= 20）—— 别在这里写字面量，
    #    否则又变成「改一处漏一处」。前端的镜像常量在 `ui/static/app.js`。
    max_attempts: int = MAX_ATTEMPTS
    interval_ms: int = 800
    # 单项时间预算（秒），0 = 不限。与 max_attempts 是两把不同的闸：
    # 次数闸管不住时间 —— 卡顿时一次尝试最坏 6 秒（TIMEOUT_CRITICAL 2+4），
    # MAX_ATTEMPTS 次就是约 2 分钟。
    budget_s: float = 0.0
    precheck: bool = True
    # 该教学班的时段（Slot.as_dict() 列表）。让清单项在页面刷新后仍能画到课表上。
    slots: list[dict] = Field(default_factory=list)


class ConflictCandidate(BaseModel):
    """一个待判定的候选教学班。key 由前端给，方便回填。"""

    key: str = ""
    kch_id: str = ""
    kcmc: str = ""
    slots: list[dict] = Field(default_factory=list)


class ConflictBody(BaseModel):
    items: list[ConflictCandidate]


class PlanBody(BaseModel):
    items: list[PlanItemBody]
    stop_on_first_win: bool = False
    global_deadline_s: float = 0.0
    # 派发方式：serial / round_robin（默认）。见 engine/plan.py::normalize_retry_mode。
    # ⚠️ 它**不是**「要不要并发」的开关 —— 两种模式都严格单线程（最多一个在途请求），
    # 区别是「一项打到终态才换下一项」还是「每项先发一发再回头」。
    retry_mode: str = "round_robin"

    # --- 乐观锁 ---
    # 前端从 `/api/state` 拿到的 `plan_rev`，提交时**原样带回来**。
    # 对不上 → 409：说明这份清单已经被别人改过（另一个标签页 / 脚本 / 换学期作废），
    # 而前端的本地副本是陈旧的 —— 一旦提交就会把别人的改动整个盖掉。
    #
    # ⚠️ 不传 = **无条件覆盖**（脚本 / CLI 这类一次性写入者用）。
    # 界面永远要传；不传的话这道保护就不存在了。
    base_version: int | None = None

    # --- 定时开抢 ---
    # start_at 是**用户输入的墙上时间字符串**（如 "12:00:00" 或
    # "2026-09-30 12:00:00"），由引擎层的 parse_when 解释。
    # 空字符串 / 不传 = 立即开始。
    start_at: str = ""
    warmup_s: float = 90.0
    fire_lead_s: float | None = None


class WaitBody(BaseModel):
    """蹲课请求（2026-10-01）。

    ⭐ 蹲课 = 长时间蹲「已满」课的退课名额。与抢课的关键差异：
      · 派发方式**固定** `round_robin`（轮流发送，每回合给每项各发一发）；
      · **按时间不按次数**：没有 `max_attempts`，用「开始时间 + 结束时间」框定区间；
      · 间隔由用户选 `interval_ms`（800 / 1000 两档）。
    """
    items: list[PlanItemBody]
    # 每条申请请求的间隔（毫秒）。两档：800 / 1000。
    interval_ms: int = 1000
    # 开始时刻（墙上时间字符串，同 PlanBody.start_at，走 parse_when）。空 = 立即开始。
    start_at: str = ""
    # 结束时刻（墙上时间字符串）。**必填** —— 蹲课按时间，没有终点会无限跑。
    # 后端据此算 `global_deadline_s = deadline_at - start_at`。
    deadline_at: str = ""
    # 乐观锁版本号，同 PlanBody.base_version。
    base_version: int | None = None


class DropBody(BaseModel):
    """退课请求。

    ⚠️ 刻意**不接受 `do_id`**。2026-09-29 实测：已选列表里的 `do_jxb_id`（= do_id）
    是**每次查询都会重新生成的一次性加密令牌** —— 连着查 3 次，12 门课 12/12 全都变，
    一个都复现不了。所以任何从上一层缓存里带过来的 do_id 都**必然对不上**，
    拿它做校验只会把正常请求全部误拒。

    正确做法只有一个：**在这一次查询的响应内部完成退课** ——
    先查已选、取该行刚下发的令牌、立刻提交。见 `drop()` 里的实现。

    同一门课在教务里有多个已选教学班时（主 + 辅），用教学班名称消歧。
    """

    kch_id: str
    jxbmc: str = ""


# ---------------------------------------------------------------------------
# 错误 → HTTP
# ---------------------------------------------------------------------------


def _fail(e: Exception) -> HTTPException:
    """把核心层/引擎层的异常翻译成前端可读的 HTTP 错误。"""
    if isinstance(e, XKError):
        # ⚠️ 登录态失效要**立刻记账**，不能只是把错误丢回前端。
        # 否则界面顶部会一直显示「已登录」——因为 `/api/state` 看的是内存里
        # 那个会话对象，而它并不会自己知道教务已经把这次登录踢掉了。
        if e.kind is FailureKind.SESSION_EXPIRED:
            RUNTIME.mark_session_invalid(str(e) or KIND_LABEL.get(e.kind, "登录态失效"))
        label = KIND_LABEL.get(e.kind, e.kind.value)
        return HTTPException(
            status_code=409,
            detail={
                "kind": e.kind.value,
                "message": label,
                "detail": str(e),
                "raw": (e.raw or "")[:1500],
                "retryable": e.kind.retryable(),
            },
        )
    # 会话缺失 / 业务前置条件不满足 → 400，不是服务端故障
    if isinstance(e, (RuntimeError, ValueError)):
        return HTTPException(
            status_code=400, detail={"kind": "precondition", "message": str(e)}
        )
    logger.exception("UI 接口未预期异常")
    return HTTPException(status_code=500, detail={"kind": "unknown", "message": str(e)})


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------


def _access_key() -> str:
    """访问口令（防同网段他人访问）。进程级，来自环境变量 XK_ACCESS_KEY。

    空串 = 不开启口令校验（本机 localhost 场景）。`serve.py` 在 import 本模块前
    就设好这个变量（与 XK_SCHOOL_URL 同风格），所以这里 import 时读一次即可。
    """
    return os.environ.get("XK_ACCESS_KEY", "").strip()


def create_app() -> FastAPI:
    app = FastAPI(title="南苑抢课助手", version="0.1.0", docs_url="/api/docs")

    # -- 访问口令校验中间件（2026-10-01）-----------------------------------
    # 开启口令时：所有 /api/* 请求必须带正确口令（X-Access-Key 头或 ?key=），
    # 否则 401。放行：/api/access/status（前端先查"要不要口令"）、
    # /api/access/verify（校验口令本身）、静态文件与首页（前端要先加载）。
    # ⭐ 另放行 /api/presence（2026-10-02）：它是「页面是否还开着」的在线信号，
    #    不回传任何数据、无泄露风险；放行才能让「停在访问码页时关浏览器」也触发
    #    后端自动退出（EventSource 无法自定义请求头，带口令很别扭）。
    _key = _access_key()

    @app.middleware("http")
    async def _guard(request, call_next):
        path = request.url.path
        if _key and path.startswith("/api/"):
            if path not in ("/api/access/status", "/api/access/verify", "/api/presence"):
                supplied = request.headers.get("x-access-key", "")
                if not supplied:
                    supplied = request.query_params.get("key", "")
                if supplied != _key:
                    from fastapi.responses import JSONResponse
                    return JSONResponse(
                        status_code=401,
                        content={"kind": "access_denied", "message": "访问口令错误或缺失"},
                    )
        return await call_next(request)

    @app.get("/api/access/status")
    def access_status():
        return {"required": bool(_key), "key_len": len(_key)}

    @app.post("/api/access/verify")
    def access_verify(body: dict):
        supplied = str(body.get("key", "")).strip()
        if _key and supplied == _key:
            return {"ok": True}
        return {"ok": False}

    # -- 页面 ---------------------------------------------------------------

    # 前端是**按需读盘**的（改了静态文件不用重启后端），但浏览器那侧的协商缓存
    # 会拿 ETag 换 304，用户不强制刷新就一直在跑旧 JS/旧 HTML。
    # 加 no-cache：仍然走 ETag 校验（没改就 304，不浪费带宽），但改了立刻生效。
    _NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/", include_in_schema=False)
    def index():
        f = STATIC_DIR / "index.html"
        if not f.exists():
            raise HTTPException(500, "前端文件缺失")
        return FileResponse(f, headers=_NO_CACHE)

    @app.get("/static/{path:path}", include_in_schema=False)
    def static_files(path: str):
        # 防目录穿越
        target = (STATIC_DIR / path).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists():
            raise HTTPException(404, "not found")
        return FileResponse(target, headers=_NO_CACHE)

    # -- 状态 ---------------------------------------------------------------

    @app.get("/api/state")
    def state():
        from core.config import DEFAULT_SCHOOL  # 与 _school_name/_school_url 同风格

        s = RUNTIME.summary()
        s["school"] = {
            "name": _school_name(),
            "url": _school_url(),
            # 作息表随状态一起下发，前端课表左侧栏据此显示上下课时间。
            # 放在这里而不是前端写死：多学校复用同一套前端时，只改后端配置即可。
            "jie_time": DEFAULT_SCHOOL.jie_time_map(),
        }
        return s

    # -- 会话 ---------------------------------------------------------------

    @app.post("/api/session")
    def create_session(body: CookieBody):
        try:
            cred = get_credential(cookie=body.cookie)
        except Exception as e:
            raise HTTPException(400, f"Cookie 解析失败：{e}")
        sess = RUNTIME.attach_session(cred)
        # 立刻 init，把「登录态是否有效」第一时间暴露给用户（HANDOFF 阶段 2 验收点）
        return _do_init(sess)

    @app.post("/api/session/cdp")
    def create_session_cdp(body: CdpBody):
        try:
            cred = get_credential(cdp_port=body.port)
        except Exception as e:
            raise HTTPException(400, f"CDP 抓取失败：{e}")
        sess = RUNTIME.attach_session(cred)
        return _do_init(sess)

    @app.post("/api/session/password")
    def create_session_password(body: PasswordBody):
        """账号密码登录（⚠️ 密码只在本机内存走一遍，绝不落盘）。

        依赖 core/credential.py::from_password：GET 登录页 → 取 RSA 公钥 →
        加密提交 → 验证会话。若学校强制 SSO（authJwglxtLoginURL 非空）或
        需要验证码，会在这里如实报错，让用户改用手动 Cookie。

        ⭐ 登录成功后**停留在南苑抢课助手**，不再拉起受控浏览器跳教务
        （2026-10-01 用户明确要求）。
        """
        u = (body.username or "").strip()
        p = body.password or ""
        if not u or not p:
            raise HTTPException(400, "学号和密码都不能为空")
        try:
            cred = get_credential(username=u, password=p)
        except Exception as e:
            raise HTTPException(400, f"账号密码登录失败：{e}")
        sess = RUNTIME.attach_session(cred)
        # ⭐ with_init=False：登录成功即返回（会话状态 = unverified，前端据此即可切进主界面）。
        #   前端随后另行调用 POST /api/init 拉取上下文，做到「先见界面、后填数据」。
        if not body.with_init:
            return {
                "ok": True,
                "inited": False,
                "source": sess.credential.source,
            }
        return _do_init(sess)

    @app.delete("/api/session")
    def drop_session():
        # ⭐ 退出联动（2026-10-01 用户要求）：助手退出时，顺带真正注销教务那边的会话。
        # 教务退出接口 = GET {base}/logout?t={ts}&login_type=（CDP 实测）。
        # 这是「尽力而为」的辅助动作：失败/超时都不影响助手本地清除会话，
        # 更不该因为教务网络抖动就把用户卡在退出这一步。
        try:
            from core.config import PATH_LOGOUT

            sess = RUNTIME._session
            if sess is not None and sess.client is not None:
                base = sess.client.school.url(PATH_LOGOUT, with_gnmkdm=False)
                logout_url = f"{base}?t={int(time.time() * 1000)}&login_type="
                sess.client.http.get(logout_url, allow_redirects=False, timeout=8)
                logger.info("已向教务发送退出登录请求")
        except Exception as e:  # noqa: BLE001 —— 注销失败不拖垮本地退出
            logger.warning("注销教务会话失败（不影响本地退出）：%s", e)
        RUNTIME.stop()
        RUNTIME.detach_session()
        return {"ok": True}

    @app.get("/api/session/check")
    def check_session():
        """探活：**只发一个 GET**，确认登录态还有效。

        为什么需要它（这是一个真 bug 的修法）：
        `/api/state` 是纯内存快照，前端每 3 秒轮询它**一个请求都不打教务**。
        于是「用户在教务网站点了退出登录 / Cookie 过期」这件事**永远不会被发现** ——
        顶部状态栏会一直显示「已登录」，直到用户真去点一次操作才发现。
        现在前端每 ~45 秒来这里核实一次，`session_state` 就不再是一句空话。

        ⚠️ 抢课运行中**直接跳过**：runner 自己撞上失效会推 `need_login` 事件
        （`Runtime.push_event` 里已挂钩子），而这时往同一个客户端上叠请求只会添乱。
        """
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        if RUNTIME.running:
            return {
                "ok": True,
                "skipped": "running",
                "session_state": RUNTIME.session_state,
                "reason": "抢课运行中，探活让位（失效由引擎自己上报）",
            }
        try:
            info = sess.client.ping()
        except XKError as e:
            raise _fail(e)          # _fail 里已把 SESSION_EXPIRED 记为失效
        RUNTIME.note_session_verified(is_open=info.get("is_open"))
        return {
            "ok": True,
            "session_state": RUNTIME.session_state,
            "is_open": info.get("is_open"),
            "checked_at": RUNTIME.session.verified_at if RUNTIME.session else None,
        }

    @app.post("/api/probe")
    def probe():
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        return _do_init(sess, force=True)

    @app.post("/api/init")
    def do_init():
        """初始化选课上下文（拉 Tab / 隐藏域 / 加密串）。

        ⭐ 2026-10-02 与 `/api/session/password`（with_init=False）配套：
        前端登录成功后先切主界面，再调本接口补齐上下文 —— 避免用户盯着登录卡等。
        force=False：无缓存时同样会真打教务；已有当日缓存则直接复用（省一次往返）。
        """
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        return _do_init(sess)

    @app.get("/api/presence")
    async def presence_stream():
        """常驻在线通道（SSE）：页面开着就保持连接，页面/浏览器关闭即断开。

        ⭐ 2026-10-02：为「关闭浏览器 → 后端自动退出」提供可靠信号（见 serve.py 守护线程）。
        为什么用 SSE 而不是定时心跳：**浏览器会把后台标签的定时器节流**（可能降到每分钟
        才跑一次），用它判「人在不在」会误判；而连接断开是浏览器关页时的**真实动作**，
        不受节流影响。刷新页面会短暂断开并自动重连 —— 由调用方留宽限期兜住。
        """
        import asyncio

        async def gen():
            RUNTIME.presence_enter()
            try:
                while True:
                    # 后端要退出了 → 推 shutdown 事件，页面据此提示/尝试关闭标签页
                    # （服务器无法强制关标签页，这是浏览器的安全限制，只能「尽力」）。
                    if RUNTIME.shutdown_requested:
                        yield "event: shutdown\ndata: bye\n\n"
                        break
                    yield ": keepalive\n\n"
                    await asyncio.sleep(1.5)
            finally:
                RUNTIME.presence_leave()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- 查询（只读，可并发） -----------------------------------------------

    @app.get("/api/clock")
    def clock(samples: int = Query(0, ge=0, le=20)):
        """时钟校准状态。samples>0 时先主动采样若干次再返回。

        打的是登录页（未登录也有 Date 头），所以这一步零副作用、零配额消耗。
        """
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        clk = getattr(sess.client, "clock", None)
        if clk is None:
            raise HTTPException(400, "当前会话不支持时钟校准")
        if samples:
            try:
                sess.client.sync_clock(samples)
            except XKError as e:
                raise _fail(e)
        return clk.as_dict()

    @app.get("/api/credit")
    def credit(refresh: int = Query(0, ge=0, le=1)):
        """本学期学分要求（最低 / 最高 / 已选）+ 清单待加选学分。

        refresh=1 时重新拉一次选课首页 —— 纯 GET、零副作用，用来取**最新的**
        「已选学分」（在教务网站选了一门或退了一门，这个数立刻就变）。

        注意：待加选学分来自本地清单，没会话也能算；
        最低/最高/已选来自教务，没会话就是 None（不是 0）。
        """
        sess = RUNTIME.session
        if refresh:
            if not sess:
                raise HTTPException(400, "尚未建立会话")
            try:
                sess.client.refresh_credit()
            except XKError as e:
                raise _fail(e)
        return RUNTIME.credit_snapshot()

    @app.get("/api/tabs")
    def tabs():
        """课程类别 Tab 列表。每个 Tab 有独立加密串，查询时必须指定。

        `cached=True` 表示**这批 Tab 来自缓存**（本次 init 没解析到 —— 未开放期
        的 Index 页一个 Tab 都没有）。列表照常给出，用户可以继续看着；但要让它
        知道这不是「现在能查课」。所以顺带回传抓取时刻，前端据此提示。
        """
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        client = sess.client
        return {
            "cached": bool(getattr(client, "tabs_stale", False)),
            "at": getattr(client, "tabs_at", 0.0) or None,
            "items": [
                {
                    "kklxdm": t.kklxdm,
                    "name": t.name,
                    "njdm_id": t.njdm_id,
                    "zyh_id": t.zyh_id,
                }
                for t in client.tabs
            ],
        }

    @app.get("/api/courses")
    def courses(
        keyword: str = "",
        kklxdm: str = "",
        tab_index: int = Query(-1, ge=-1),
        page: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=200),
        # ⭐ 高级筛选（复刻教务选课页那个「筛选」面板）。留空 / 不传 = 该条件不生效。
        #    这些条件**全部由教务服务端执行** —— 课程列表接口不返回上课时间/教师/学院，
        #    客户端想筛也筛不了（详见 client.query_courses 的 docstring）。
        #
        #    ⚠️ 多选条件（星期 / 节次）用**重复参数**传：`?sksj=1&sksj=3`。
        #       **不要用逗号拼**：逗号是「教务线上」的雷区（见 client 里的实测说明），
        #       我方边界也不留这个坑，免得以后有人照着抄。
        sksj: list[str] | None = Query(None),   # 上课星期，多选：1=周一 … 7=周日
        skjc: list[str] | None = Query(None),   # 上课节次，多选：1-15
        xf: str = "",       # 学分（自由输入）
        cx: str = "",       # 是否重修 "1"/"0"
        yl: str = "",       # 有无余量 "1"/"0"
        sksjct: str = "",   # 是否与自己的课表冲突 "1"/"0" ← 「只看时间冲突」
    ):
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        # 界面参数名 → 教务 searchBox 的 conditions.index
        # （唯一权威 = 教务 `zzxkYzb.js` 里的 conditions.push({"index": ...})）
        # 值统一成 list —— 教务线上下标数组的形态，单值也多包一层（见 client）。
        _raw: dict[str, list[str]] = {
            "sksj_list": sksj or [],
            "skjc_list": skjc or [],
            "xf_list": [xf] if xf else [],
            "cxbj_list": [cx] if cx else [],
            "yl_list": [yl] if yl else [],
            "sksjct_list": [sksjct] if sksjct else [],
        }
        filters = {k: v for k, v in _raw.items() if v}

        try:
            rows, meta = sess.client.query_courses(
                keyword,
                tab_index=tab_index if tab_index >= 0 else None,
                kklxdm=kklxdm or None,
                page=page,
                page_size=size,
                filters=filters,
            )
        except XKError as e:
            raise _fail(e)
        # ⭐ 顺手把这次搜索的结果落盘（按课程类别分桶）。
        #
        # 为什么明知「查询接口带写副作用」还是要在这里做：用户要的是
        # 「未开放期能回看上次搜到的课程」，而**未开放期根本发不出查询** ——
        # 数据只能来自开放期那几次搜索。放到前端另调一个「存一下」的接口也行，
        # 但那样只要有一次前端忘了调，用户就会在未开放期发现「什么都没有」，
        # 而且失败原因极难排查。写在这里是「无论如何都会存」。
        #
        # ⚠️ 只写 rows 非空时：一次失败 / 空结果的搜索绝不能把已有的好数据冲掉。
        # 🔴 带筛选时**也不写**（2026-10-01 立）。理由：这份快照的用途是
        #    「**未开放期**回看上次能搜到哪些课」，而快照一旦被覆盖就**再也搜不回来**
        #    （未开放期根本发不出查询）。如果让「只看冲突」这种筛出 5 行的搜索去覆盖，
        #    用户就永久丢掉了那 57 行的完整目录 —— 而且是不可逆的。
        #    ⚠️ 清空筛选后的那次搜索**仍然会正常落盘**，所以想刷新快照随时可以。
        # ⚠️ 落盘失败只记日志，绝不影响本次搜索返回（铁律：落盘是锦上添花）。
        if rows and not filters:
            ok = save_courses_bucket(
                kklxdm=str(meta.get("kklxdm") or ""),
                kklxmc=str(meta.get("kklxmc") or ""),
                tab_index=tab_index,
                keyword=keyword or "",
                rows=rows,
                semester=RUNTIME.semester_key(),
            )
            if ok:
                # 摘要缓存作废：下一次 /api/state 就能让「📂 上次数据」按钮立刻亮起来
                RUNTIME.invalidate_courses_cache()
        return {"rows": rows, "meta": meta, "count": len(rows)}

    @app.get("/api/courses/snapshot/load")
    def load_courses_snapshot(kklxdm: str = "", kklxmc: str = ""):
        """取回「上次搜索保存下来的课程数据」（离线，不打教务）。

        典型用法 = 未开放期：教务查不了课，把上次搜到的拿出来看。
        匹配按「精确 → 类别名 → kklxdm」三级回退（教务可能改过类别名/下标）。

        没能匹配上时**不报 5xx、也不给别的类别的数据**，而是如实返回
        `ok=False` + `available`（有哪些类别可看）——
        让界面能说清「这个类别上次没存过，但下面这些有」，而不是丢一个空列表让人以为坏了。
        """
        summ = RUNTIME.courses_snapshot
        if not summ:
            raise HTTPException(400, "本地还没有保存过课程数据（先在有会话时搜索一次）")
        bucket = pick_courses_bucket(summ, kklxdm=kklxdm, kklxmc=kklxmc)
        if bucket is None:
            return {
                "ok": False,
                "rows": [],
                "count": 0,
                "reason": f"上次没有保存「{kklxmc or kklxdm or '该类别'}」的课程数据",
                "available": summ.get("buckets") or [],
            }
        full = load_courses_bucket(bucket.get("key") or "")
        rows = full.get("rows") or []
        logger.info(
            "加载课程快照：%s 共 %d 行（保存于 %s）",
            bucket.get("kklxmc") or bucket.get("key"), len(rows),
            time.strftime("%m-%d %H:%M", time.localtime(bucket.get("saved_at") or 0)),
        )
        return {"ok": True, "rows": rows, "count": len(rows),
                "bucket": bucket, "available": summ.get("buckets") or []}

    @app.get("/api/classes")
    def classes(kch_id: str, kklxdm: str = "", tab_index: int = Query(-1, ge=-1)):
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        try:
            lst = sess.client.query_classes(
                kch_id,
                tab_index=tab_index if tab_index >= 0 else None,
                kklxdm=kklxdm or None,
            )
        except XKError as e:
            raise _fail(e)

        items = []
        for j in lst:
            entry = build_entry(
                {
                    "kch_id": kch_id,
                    "kcmc": j.kcmc,
                    "jsxx": j.jsxx,
                    "sksj": j.sksj,
                    "jxdd": j.jxdd,
                    "xf": j.xf,
                    "jxbrs": j.jxbrl,
                    "yxzrs": j.yxzrs,
                    "do_id": j.do_id,
                    "jxb_id": j.jxb_id,
                    "kklxmc": j.kkxymc,
                },
                source="pending",
            )
            items.append(
                {
                    "jxb_id": j.jxb_id,
                    "do_id": j.do_id,
                    "kcmc": j.kcmc,
                    "jsxx": j.jsxx,
                    "sksj": j.sksj,
                    "jxdd": j.jxdd,
                    "jxbrl": j.jxbrl,
                    "yxzrs": j.yxzrs,
                    "kxkymc": j.kkxymc,
                    "xf": j.xf,
                    "is_full": j.is_full,
                    # --- 课表与冲突判定用（由 core.schedule 解析，界面不重复实现）---
                    "teacher_text": entry.teacher_text,
                    "sksj_text": entry.sksj_text,
                    "jxdd_text": entry.jxdd_text,
                    "jxdd_list": entry.jxdd,
                    "slots": [s.as_dict() for s in entry.slots],
                }
            )
        return {"items": items}

    @app.get("/api/selected")
    def selected(cached: int = Query(0, ge=0, le=1)):
        """查已选课程。

        `cached=1` 只用本服务的内存缓存（**不再请求教务**）——前端刷新页面时走这条，
        避免用户说的「影响抢课速度」。缓存由上一次不带 cached 的调用写入。
        仍返回 `rows`（原始 55 列）供调试对照。
        """
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")

        if cached:
            entries = RUNTIME.selected
            if entries is None:
                return {"count": 0, "courses": [], "rows": [], "cached": True, "stale": True}
            return {
                "count": len(entries),
                "courses": [e.as_dict() for e in entries],
                "rows": [],
                "cached": True,
                "stale": False,
            }

        try:
            rows = sess.client.query_selected()
        except XKError as e:
            raise _fail(e)
        entries = entries_from_rows(rows, source="selected", page=RUNTIME.index_store())
        RUNTIME.set_selected(entries)
        return {
            "count": len(entries),
            "courses": [e.as_dict() for e in entries],
            "rows": rows,
            "cached": False,
            "stale": False,
        }

    # -- 学业情况统计 ---------------------------------------------------------

    @app.get("/api/academic")
    def academic():
        """学生学业情况统计（只读，零副作用）。

        来自教务「学生学业情况统计查询」模块（gnmkdm=N551247，与选课 N253512 无关）。
        学生端查询无需任何参数，服务端按登录态返回本人一条记录。

        返回结构：
            kcxz: [{name, code, rn, require}]  课程性质列表（列头 + 要求学分口径）
            students: [student_row]            学生行（含基础信息 + 各性质学分）
        """
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        try:
            return sess.client.query_academic()
        except XKError as e:
            raise _fail(e)

    # -- 退课 ---------------------------------------------------------------

    @app.post("/api/drop")
    def drop(body: DropBody):
        """退课（⚠️ 不可逆）。

        三道关口，缺一不可：

        1. **抢课任务运行中一律拒绝** —— 退课和抢课都是写操作。退掉的可能正好是
           抢课清单里的目标，两边同时飞必然互相踩踏。铁律 #6：写入串行。
        2. **预检零副作用** —— 现拉一次已选，用教务**刚给的**那一行数据重算退课
           资格（`core/drop.py` 逐字复刻教务的 `isktk`）。不通过就 400 说明是哪一条
           不满足，**一个写请求都不发**。铁律 #2。
        3. **通过才提交** —— 全程持写锁，提交后立刻作废已选缓存，让课表、冲突
           判定、按钮状态一起刷新。铁律 #10：结果（含原始返回码）必须落日志。

        ⚠️ **令牌时效**：`do_jxb_id` 是每次查询重新下发的一次性加密令牌
        （实测连查 3 次 12/12 全变），所以这里**必须先查、再立刻用刚查到的令牌提交**，
        两步之间不能隔任何其他查询。也正因如此，接口不接收上一层传来的 do_id。
        """
        sess = RUNTIME.session
        if not sess:
            raise HTTPException(400, "尚未建立会话")
        if RUNTIME.running:
            raise HTTPException(
                400, "抢课任务正在运行，为避免与提交互相踩踏，请先停止任务再退课"
            )

        kch_id = (body.kch_id or "").strip()
        if not kch_id:
            raise HTTPException(400, "缺少课程号")

        def _row_do_id(r: dict) -> str:
            # 已选列表里这个字段叫 do_jxb_id；教学班级列表里叫 do_id
            return str(r.get("do_jxb_id") or r.get("do_id") or "").strip()

        with RUNTIME.write_lock:
            # --- 预检：现拉已选（零副作用），用教务刚给的数据重算资格 + 取新令牌 ---
            try:
                rows = sess.client.query_selected()
            except XKError as e:
                raise _fail(e)

            RUNTIME.set_selected(
                entries_from_rows(rows, source="selected", page=RUNTIME.index_store())
            )

            cands = [r for r in rows if str(r.get("kch_id") or "").strip() == kch_id]
            if not cands:
                raise HTTPException(
                    400, "教务已选列表里没有这门课（可能你已在教务网站退掉），页面已刷新"
                )

            if len(cands) == 1:
                target = cands[0]
            else:
                # 同一门课有多个已选教学班（主 + 辅）—— 按教学班名称消歧
                want = (body.jxbmc or "").strip()
                hit = [r for r in cands if str(r.get("jxbmc") or "").strip() == want]
                if len(hit) != 1:
                    names = "、".join(str(r.get("jxbmc") or "?") for r in cands)
                    raise HTTPException(
                        400,
                        f"这门课在教务里有 {len(cands)} 个已选教学班（{names}），"
                        "无法确定退哪一个，请刷新页面后从卡片上再点一次",
                    )
                target = hit[0]

            st = drop_state(target, RUNTIME.index_store())
            if not st.allowed:
                msg = f"教务当前不允许退这门课：{st.reason}"
                RUNTIME.push_event(
                    Event(
                        type=EventType.LOG,
                        item_key=kch_id,
                        message=f"退课被拦下（未发任何写请求）· {st.reason}",
                        kind=FailureKind.PRECONDITION,
                        extra={"checks": st.checks},
                    )
                )
                raise HTTPException(400, msg)

            do_id = _row_do_id(target)
            if not do_id:
                raise HTTPException(
                    400, "教务这一行没有下发教学班令牌（do_jxb_id），无法提交退课"
                )
            kcmc = str(target.get("kcmc") or "").strip() or kch_id
            logger.info("退课请求：%s %s（令牌 %s…）", kch_id, kcmc, do_id[:16])

            # --- 提交（不可逆）---
            try:
                res = sess.client.cancel(kch_id, do_id)
            except XKError as e:
                RUNTIME.push_event(
                    Event(
                        type=EventType.LOG,
                        item_key=kch_id,
                        message=f"退课请求异常：{e}",
                        kind=e.kind,
                    )
                )
                raise _fail(e)

            RUNTIME.invalidate_selected()  # 退没退成，缓存都不再可信

            warning = ""
            plan = RUNTIME.plan
            if res["success"] and plan:
                for idx, it in enumerate(plan.items, start=1):
                    if it.kch_id == kch_id:
                        warning = (
                            f"「{kcmc}」还在抢课清单里（第 {idx} 项），"
                            "下一轮会把它重新抢回来。不想再选就先把它从清单里移除。"
                        )
                        break

            RUNTIME.push_event(
                Event(
                    type=EventType.LOG,
                    item_key=kch_id,
                    message=(
                        f"退课{'成功' if res['success'] else '失败'}：{kcmc} · "
                        f"教务返回码 {res['code']!r} · {res['msg']}"
                    ),
                    kind=None if res["success"] else FailureKind.UNKNOWN,
                    extra={"code": res["code"], "raw": res["msg"], "do_id": do_id},
                )
            )
            return {
                "ok": bool(res["success"]),
                "code": res["code"],
                "msg": res["msg"],
                "kch_id": kch_id,
                "kcmc": kcmc,
                "do_id": do_id,
                "warning": warning,
            }

    @app.post("/api/conflict")
    def conflict(body: ConflictBody):
        """批量判定候选教学班与「已知会占时间的课」是否冲突。

        对手方 = 已选课程缓存 + 当前抢课清单（都在本进程内存里，不发任何外部请求）。

        返回四样东西，口径不同，别混用：
          `level`   —— **提示口径**：`hard`（周次也重叠，真撞）/ `soft`（节次占位重叠
                       但周次错开）/ `none`。界面据此决定红还是黄。
          `vs`      —— 只统计 `hard` 命中的来源：`selected`（已选）/ `pending`（清单里
                       其他待选）。这是**互斥口径**，与 `Plan.conflict_pairs` 一致，
                       界面据此标「⇄ 互斥」，执行期据此跳过提交。
          `vs_soft` —— 只统计 `soft` 命中的来源。界面靠它区分
                       「与已选节次占位重叠」（要提醒）与
                       「只与清单内其他待选占位重叠、周次错开」（不算冲突，不该报警）。
          `hits`    —— 前 6 条明细，供界面说清「跟谁撞了」。
        """
        known = RUNTIME.known_entries()
        loaded = RUNTIME.selected is not None
        results: dict = {}
        for it in body.items:
            key = it.key or it.kch_id
            slots = slots_from_dicts(it.slots)
            hits = find_conflicts(slots, known, exclude_kch=it.kch_id)
            levels = {h.level for h in hits}
            hard_sources = {h.source for h in hits if h.level == "hard" and h.source}
            soft_sources = {h.source for h in hits if h.level == "soft" and h.source}
            results[key] = {
                "level": "hard" if "hard" in levels else ("soft" if "soft" in levels else "none"),
                "count": len(hits),
                # 真撞（周次也重叠）撞到了谁；含周次错开的 soft 不算，否则会出现
                # 「界面标互斥、清单却不认」或反过来的不一致
                "vs": sorted(hard_sources),
                # 只占位重叠、周次错开的撞到了谁
                "vs_soft": sorted(soft_sources),
                "hits": [h.as_dict() for h in hits[:6]],
                "slots": [s.as_dict() for s in slots],
            }
        return {"known": loaded, "results": results}

    # -- 计划与任务 ---------------------------------------------------------

    @app.post("/api/plan")
    def set_plan(body: PlanBody):
        # 先把时刻解析掉：格式错就立刻 400，不要等用户点了「开始」才发现
        start_at = None
        if body.start_at.strip():
            try:
                start_at = parse_when(body.start_at)
            except ValueError as e:
                raise _fail(e)

        plan = Plan(
            stop_on_first_win=body.stop_on_first_win,
            global_deadline_s=body.global_deadline_s,
            retry_mode=normalize_retry_mode(body.retry_mode),
            start_at=start_at,
            warmup_s=body.warmup_s,
            fire_lead_s=body.fire_lead_s,
        )
        for it in body.items:
            plan.add(
                PlanItem(
                    kch_id=it.kch_id,
                    do_id=it.do_id,
                    jxb_id=it.jxb_id,
                    kklxdm=it.kklxdm,
                    tab_index=it.tab_index,
                    kcmc=it.kcmc,
                    jsxx=it.jsxx,
                    xf=it.xf,
                    cxbj=it.cxbj,
                    fxbj=it.fxbj,
                    priority=it.priority,
                    max_attempts=it.max_attempts,
                    interval_ms=it.interval_ms,
                    budget_s=it.budget_s,
                    precheck=it.precheck,
                    slots=slots_from_dicts(it.slots),
                )
            )
        try:
            RUNTIME.set_plan(plan, expect_rev=body.base_version)
        except PlanConflict as e:
            # 409：清单已被别人改过，前端的本地副本是陈旧的。
            # **不写一个字节** —— 这正是乐观锁的意义：宁可让用户重做一步操作，
            # 也不要让陈旧副本把「别人刚写进去的东西（甚至用户手工攒的清单）」整个盖掉。
            raise HTTPException(
                409,
                {
                    "kind": "plan_conflict",
                    "message": (
                        f"清单已被其它页面或脚本改动（你手上是第 {e.expected} 版，"
                        f"服务器已是第 {e.actual} 版，共 {e.count} 项），"
                        f"本次提交没有生效。请重新载入后再操作。"
                    ),
                    "plan_rev": e.actual,
                    "count": e.count,
                },
            )
        return {
            "ok": True,
            "count": len(plan.items),
            # 乐观锁：把服务端的新版本号回给前端，前端拿它继续后续提交。
            # 不回的话前端只能靠再拉一次 /api/state 才拿到，
            # 而中间那一刻它手上仍是旧版本 → 自己把自己挡在 409 上。
            "plan_rev": RUNTIME.plan_rev,
            # checked = 校验过版本号；forced = 没带版本号，无条件覆盖
            "lock": "forced" if body.base_version is None else "checked",
            "start_at": start_at,
            "scheduled": start_at is not None,
            # 互斥项下标对：界面用来标注抢课优先顺序的取舍
            "mutex": plan.conflict_pairs(),
        }

    @app.post("/api/plan/load")
    def load_plan_snapshot():
        """把磁盘上「上次保存的抢课清单」**加载进内存**，成为当前抢课清单。

        ⭐ 为什么需要这个显式动作（2026-09-30 用户要求）：
        以前服务进程一启动就自动把上次的清单塞回内存。问题是用户**不知道它会在
        什么时候冒出来** —— 可能是上一学期的、可能是上一轮已经抢完的，
        界面上突然多出一份「不是我现在攒的」清单，随手点一下「开始抢课」
        就拿着旧目标去打教务了。

        现在改成：磁盘那份只当**素材**，用户主动点「加载上次数据」才进内存。
        典型时机就是查课期与抢课期之间那段「未开放期」——
        那时教务什么都查不到（查课/查已选都会被 `is_open` 闸门挡下），
        把上次存的数据拿出来正好看课、顺便确认下午要抢哪几门。

        ⚠️ 这是「整份替换」的显式指令，会走 `Runtime.load_snapshot()` 把版本号 +1；
        前端手里那个版本号随之作废（下次 `/api/plan` 提交会拿到 409 并重新载入，
        也就是安全的那一侧）。本接口**不**接受 `base_version`：它就是用来覆盖的。
        """
        try:
            res = RUNTIME.load_snapshot()
        except Exception as e:
            raise _fail(e)
        if not res.get("ok"):
            raise HTTPException(400, res.get("reason") or "没有可加载的清单快照")
        plan = RUNTIME.plan
        return {
            "ok": True,
            "count": res["count"],
            # 版本号要回给前端：它下一次提交得用这个新号，否则会被自己刚才
            # 这次加载挡在 409 上（或者更省事——前端就是靠它变大来触发重新载入的）
            "plan_rev": RUNTIME.plan_rev,
            "plan_meta": RUNTIME.plan_meta,
            "mutex": plan.conflict_pairs() if plan else [],
        }

    @app.post("/api/start")
    def start():
        if not RUNTIME.plan or not RUNTIME.plan.items:
            raise HTTPException(400, "抢课清单为空，请先添加课程")
        try:
            RUNTIME.start(RUNTIME.plan)
        except Exception as e:
            raise _fail(e)
        return {"ok": True}
    @app.post("/api/stop")
    def stop():
        return {"ok": RUNTIME.stop()}

    # -- 蹲课（2026-10-01）-------------------------------------------------

    @app.post("/api/wait/plan")
    def set_wait_plan(body: WaitBody):
        """写入蹲课清单（独立 wait.json）。返回版本号 + 已解析的开始/结束时刻。"""
        # 先把开始/结束解析掉，格式错立刻 400
        start_at = None
        if body.start_at.strip():
            try:
                start_at = parse_when(body.start_at)
            except ValueError as e:
                raise _fail(e)
        deadline_at = None
        if body.deadline_at.strip():
            try:
                deadline_at = parse_when(body.deadline_at)
            except ValueError as e:
                raise _fail(e)
        # 按时间：结束必填，且必须晚于开始
        if deadline_at is None:
            raise HTTPException(400, "蹲课必须指定结束时刻（按时间不按次数）")
        base = start_at if start_at is not None else time.time()
        if deadline_at <= base:
            raise HTTPException(400, "蹲课结束时刻必须晚于开始时刻")
        # 间隔档归一：只认 800 / 1000，其它按最近档收敛
        interval_ms = 800 if int(body.interval_ms) <= 900 else 1000

        plan = Plan(
            retry_mode=RETRY_ROUND_ROBIN,   # 蹲课固定轮流发送
            global_deadline_s=deadline_at - base,
            start_at=start_at,
            warmup_s=0.0,                    # 蹲课不做开火预热（小时级，无卡点）
            fire_lead_s=None,
        )
        for it in body.items:
            plan.add(
                PlanItem(
                    kch_id=it.kch_id,
                    do_id=it.do_id,
                    jxb_id=it.jxb_id,
                    kklxdm=it.kklxdm,
                    tab_index=it.tab_index,
                    kcmc=it.kcmc,
                    jsxx=it.jsxx,
                    xf=it.xf,
                    cxbj=it.cxbj,
                    fxbj=it.fxbj,
                    priority=it.priority,
                    # 蹲课按时间，不设次数上限（-1 = 不限；runner 对负数按不限处理）
                    max_attempts=-1,
                    interval_ms=interval_ms,
                    budget_s=0.0,
                    precheck=it.precheck,
                    slots=slots_from_dicts(it.slots),
                )
            )
        try:
            rev = RUNTIME.set_wait_plan(plan, expect_rev=body.base_version)
        except PlanConflict as e:
            raise HTTPException(
                409,
                {
                    "kind": "plan_conflict",
                    "message": f"蹲课清单已被改动（你手上第 {e.expected} 版，服务器第 {e.actual} 版）",
                    "plan_rev": e.actual,
                    "count": e.count,
                },
            )
        return {
            "ok": True,
            "count": len(plan.items),
            "wait_rev": rev,
            "interval_ms": interval_ms,
            "start_at": start_at,
            "deadline_at": deadline_at,
        }

    @app.post("/api/wait/start")
    def start_wait():
        wp = RUNTIME.wait_plan
        if not wp or not wp.items:
            raise HTTPException(400, "蹲课清单为空，请先添加要蹲的课程")
        try:
            RUNTIME.start_wait(wp)
        except Exception as e:
            raise _fail(e)
        return {"ok": True}

    @app.post("/api/wait/stop")
    def stop_wait():
        return {"ok": RUNTIME.stop_wait()}

    @app.post("/api/wait/clear")
    def clear_wait():
        RUNTIME.clear_wait_plan()
        return {"ok": True, "wait_rev": RUNTIME._wait_rev}

    # -- 事件 ---------------------------------------------------------------

    @app.get("/api/events")
    def events(since: int = 0):
        # `clamp_since` 把「上一次服务进程留下的陈旧游标」夹回 0；
        # 不做这一步的话 `since > seq` 会让调用方永远收不到事件（日志空白）。
        since = RUNTIME.clamp_since(since)
        return {"events": RUNTIME.events_since(since), "seq": RUNTIME.seq}

    @app.get("/api/events/stream")
    async def stream(since: int = 0):
        return StreamingResponse(
            _sse(since),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 禁止中间层缓冲
            },
        )

    return app


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _do_init(sess, force: bool = False) -> dict:
    """执行 init 并把结果写回 session。异常转成 HTTP 错误。"""
    try:
        store = sess.client.init(force=force)
    except XKError as e:
        # 会话失效 → 清掉，避免前端拿着一个坏 session 继续操作
        if e.kind is FailureKind.SESSION_EXPIRED:
            RUNTIME.detach_session()
        raise _fail(e)
    sess.store = store
    sess.inited = True
    sess.is_open = sess.client.is_open
    # init 是一次真实的网络往返 → 顺带把「登录态已核实」记账、并把 invalid 清掉。
    # （否则用户重新登录成功后，界面顶部还会挂着「登录已失效」。）
    RUNTIME.note_session_verified(is_open=sess.is_open)

    # 现在才知道「本学期是哪个学期」→ 拿它对账一遍本地清单：
    # 上一学期留下的清单拿到这一学期去抢只会报错，宁可此刻明确作废并告知用户。
    semester = RUNTIME.reconcile_plan_semester()

    keys = [k for k in store if not k.startswith("_")]
    return {
        "ok": True,
        "is_open": sess.is_open,
        "field_count": len(keys),
        "source": sess.credential.source,
        "semester": sess.client.semester_key,
        "tabs": len(sess.client.tabs),
        "tabs_cached": bool(getattr(sess.client, "tabs_stale", False)),
        "plan_semester": semester,
    }


async def _sse(since: int):
    """SSE：每 500ms 推一次增量事件，25s 发一次心跳防代理断连。

    关键：必须立刻 yield 一帧，否则 StreamingResponse 会一直不 flush 响应头，
    客户端（浏览器 EventSource / curl）会以为连不上。

    退出条件（避免连接永久挂着）：
      - 任务已结束且所有计划项都到达终态 → 发 done 事件后关闭
      - 从未启动过任务且等待超过 ~20s → 直接关闭，让前端按需重连
    """
    # ⚠️ 必须夹一次：`last` 在本函数里会被 `max(last, ev["seq"])` 带着走，
    # 所以陈旧游标只能在这里处理 —— 在 `events_since()` 里夹已经晚了
    # （那样 `last` 会一直停在大数上，之后每轮 batch 都是空的）。
    last = RUNTIME.clamp_since(since)
    idle = 0
    waits = 0

    # 1) 立刻 flush 响应头，让客户端马上进入 streaming 状态
    yield ": connected\n\n"

    # 2) 补历史事件
    for ev in RUNTIME.events_since(last):
        last = max(last, ev["seq"])
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    try:
        while True:
            await asyncio.sleep(0.5)
            idle += 1
            waits += 1
            batch = RUNTIME.events_since(last)
            if batch:
                idle = 0
                for ev in batch:
                    last = max(last, ev["seq"])
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            elif idle >= 50:
                idle = 0
                yield ": ping\n\n"

            # 任务跑完 → 发终止标记
            if not RUNTIME.running and RUNTIME.plan and not batch:
                snap = RUNTIME.summary()
                if snap["items"] and all(
                    i["state"] not in ("pending", "running") for i in snap["items"]
                ):
                    yield f"event: done\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
                    return
            # 一直没有任务 → 不要让连接永久挂着
            if not RUNTIME.running and not RUNTIME.plan and waits >= 40:
                return
    except asyncio.CancelledError:
        return


def _school_name() -> str:
    from core.config import DEFAULT_SCHOOL
    from ui.state import _school_from_env

    return (_school_from_env() or DEFAULT_SCHOOL).name


def _school_url() -> str:
    from core.config import DEFAULT_SCHOOL
    from ui.state import _school_from_env

    return (_school_from_env() or DEFAULT_SCHOOL).base_url
