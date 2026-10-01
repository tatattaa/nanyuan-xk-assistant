"""UI 层运行时状态：把「会话凭据 + 抢课任务」集中管理。

设计要点：
    - 凭据只存内存（铁律 #9），本对象生命周期 = 服务进程生命周期
    - 同一时刻只允许一个抢课任务（铁律 #6：提交串行）
    - 事件用环形缓冲保留最近 N 条，供前端断线重连后补齐
    - 不 import 任何 UI 框架，方便单测
    - **抢课清单会落盘**（唯一豁免于「不落盘」的数据）：它不含凭据，
      只是「用户想抢哪些课」的意图；进程重启就丢清单对定时开抢是致命的。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from core.client import ZfClient
from core.config import Credential, SchoolProfile
from core.schedule import ScheduleEntry
from engine.events import Event, EventType, TaskState
from engine.plan import (
    PLAN_FORMAT_VERSION,
    RETRY_ROUND_ROBIN,
    RETRY_SERIAL,
    Plan,
    PlanItem,
    normalize_retry_mode,
)
from engine.runner import GrabbingRunner

logger = logging.getLogger("xk.state")


# ---------------------------------------------------------------------------
# 清单乐观锁
# ---------------------------------------------------------------------------

#: 清单「版本号」：每被**整体替换**一次就 +1（进程内计数，不落盘）。
#:
#: 它存在的唯一理由：前端 `S.plan` 是**本地编辑副本**，而清单会被多个来源改写
#: （另一个浏览器标签页、自检脚本、`seed_demo_plan.py`、进程重启后的自动恢复……）。
#: 只要前端手里的副本是陈旧的，任何一次「加课 / 删课 / 调序」的提交都会把
#: 服务端那份**整个盖掉** —— 2026-09-29/30 就这样把用户手工攒的清单清空过两次。
#:
#: 所以每次 `POST /api/plan` 都必须带上「我看到的版本号」，对不上就 **409**：
#: 不写一个字节，让前端重新载入后再操作。
#:
#: ⚠️ 只有「整体替换清单」才 +1。抢课运行中 runner 改 `item.state` **不算** ——
#: 否则页面上正在看的清单会被自己触发的刷新反复重载，反而变成新的 bug。
class PlanConflict(RuntimeError):
    """乐观锁冲突：前端手里的清单版本号已经过期。"""

    def __init__(self, expected: int, actual: int, count: int = 0) -> None:
        self.expected = expected
        self.actual = actual
        self.count = count
        super().__init__(
            f"清单已被改动（你手上是第 {expected} 版，服务器已是第 {actual} 版）"
        )

# ---------------------------------------------------------------------------
# 清单落盘（单槽）
# ---------------------------------------------------------------------------

_STATE_DIR_ENV = "XK_STATE_DIR"
_PLAN_FILE = "plan.json"
#: 蹲课清单（独立于抢课清单）。2026-10-01 用户要求蹲课用**独立清单**。
_WAIT_FILE = "wait.json"
#: 课程数据快照（「上次搜到的课程」）。与清单分开存，见下面的说明。
_COURSES_FILE = "courses.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 「磁盘快照元信息」的缓存时长（秒）。见 `Runtime.snapshot_meta`。
_SNAPSHOT_CACHE_S = 2.0


def resolve_state_dir(raw: str | None = None) -> Path | None:
    """解析「清单落盘目录」。返回 None = 关闭落盘（纯内存）。

    环境变量 `XK_STATE_DIR`：
      · 未设置              → `<项目根>/state`（正常启动的默认值）
      · `off`/`none`/`0`    → 关闭落盘
      · 其它路径            → 用该路径

    为什么要做成可覆盖的：本项目的自检脚本会 `POST /api/plan` 写清单。
    2026-09-29 就发生过「跑测试把用户手工攒的清单清空」。落盘之后这个坑会
    从「内存被清」升级成「文件被覆盖」，所以隔离测试实例必须能把落盘目录
    也隔离出去（serve.py 已按端口/模式自动分配，见 serve.py::_state_dir_for）。
    """
    v = (os.environ.get(_STATE_DIR_ENV) if raw is None else raw) or ""
    v = v.strip()
    if v.lower() in ("off", "none", "0", "false", "no"):
        return None
    if not v:
        return _PROJECT_ROOT / "state"
    return Path(v).expanduser()


#: 进程级生效的落盘目录（import 时定下来，之后不再变）
STATE_DIR: Path | None = resolve_state_dir()


def plan_path() -> Path | None:
    """清单文件的完整路径；关闭落盘时为 None。"""
    return None if STATE_DIR is None else STATE_DIR / _PLAN_FILE


def wait_path() -> Path | None:
    """蹲课清单文件的完整路径；关闭落盘时为 None。"""
    return None if STATE_DIR is None else STATE_DIR / _WAIT_FILE


def load_plan_file() -> tuple[Plan | None, dict]:
    """读回上次落盘的清单。返回 `(plan|None, meta)`。

    任何异常（文件损坏 / JSON 不合法 / 权限）都当作「没有清单」——
    读不出清单只是少了个便利，不该让服务起不来。
    """
    p = plan_path()
    if p is None or not p.exists():
        return None, {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("顶层不是 JSON 对象")
        plan = Plan.from_dict(raw)
        if not plan.items:
            return None, {}
        return plan, {
            "path": str(p),
            "count": len(plan.items),
            "saved_at": raw.get("saved_at"),
            "semester": raw.get("semester") or "",
            "version": raw.get("version"),
        }
    except Exception as e:
        logger.warning("清单文件读取失败，按「无清单」处理：%s", e)
        return None, {}


def plan_snapshot() -> dict:
    """只读地看一眼「磁盘上有没有上次留下的清单快照」，**不加载**。

    为什么不复用 `load_plan_file()`：那个会把整份清单构造成 `Plan` 对象。
    这里只想要元信息（有几项、什么时候存的、哪个学期、都是些什么课），
    给界面那条「上次保存的数据 …… [加载查看]」用。
    真正加载走 `Runtime.load_snapshot()`（= `POST /api/plan/load`）。

    ⚠️ 这个函数**会读盘**，但它只在「内存里没有清单」时才会被调用
    （见 `Runtime.snapshot_meta`），所以稳态下几乎零成本。

    返回 `{}` = 没有可用快照。字段：path / count / saved_at / semester / version / courses
    """
    p = plan_path()
    if p is None or not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("顶层不是 JSON 对象")
        items = raw.get("items")
        if not isinstance(items, list) or not items:
            return {}
        return {
            "path": str(p),
            "count": len(items),
            "saved_at": raw.get("saved_at"),
            "semester": raw.get("semester") or "",
            "version": raw.get("version"),
            # 课程名：让用户在**点「加载」之前**就能确认「这确实是我上次攒的那几门」，
            # 而不是闭着眼睛点一个有副作用的按钮。
            "courses": [
                str(it.get("kcmc") or it.get("kch_id") or "?")
                for it in items[:12]
                if isinstance(it, dict)
            ],
        }
    except Exception as e:
        logger.warning("清单快照读取失败，按「没有快照」处理：%s", e)
        return {}


# ---------------------------------------------------------------------------
# 课程数据落盘（「上次搜到的课程」）
#
# 与清单分开存第二个文件、且**按课程类别分桶**（每桶 = 该类别最近一次搜索的结果）。
# 为什么不单槽：用户的用法是「上午把几个类别各搜一遍，未开放期再逐个回看」——
# 单槽会让后一次搜索把前一个类别整个盖掉，「所有数据」就名不副实了。
#
# key 用 `kklxdm|kklxmc`：⚠️ 不能只用 kklxdm —— 我校两个「板块课」的 kklxdm 都是 06，
# 单用它两桶会互相覆盖。类别名（kklxmc）在我校是唯一的。
# 匹配时按「精确 → 类别名 → kklxdm」三级回退（教务改过类别名也还能认出来）。
# ---------------------------------------------------------------------------

#: 单个课程桶最多留多少行。前端搜索固定 `size=200`，正常一桶就这么多；
#: 加个上限是防止某天有人把 size 调到几千，让落盘文件变成几 MB。
COURSES_BUCKET_MAX = 400

#: 一个文件里最多留几个类别桶（防止类别异常多时文件无限长大）
COURSES_MAX_BUCKETS = 12

_COURSES_LOCK = threading.Lock()


def courses_path() -> Path | None:
    """课程数据快照文件的完整路径；关闭落盘时为 None。"""
    return None if STATE_DIR is None else STATE_DIR / _COURSES_FILE


def courses_bucket_key(kklxdm: str, kklxmc: str) -> str:
    """桶 key。⚠️ 必须带类别名：kklxdm 会重复（两个板块课都是 06）。"""
    return f"{(kklxdm or '').strip()}|{(kklxmc or '').strip()}"


def read_courses_snapshot() -> dict:
    """把课程快照文件整个读出来（`{"version", "buckets", "saved_at"}`）。

    任何异常都当「没有快照」—— 读不出来只是少了个便利，不该让功能挂掉。
    """
    p = courses_path()
    if p is None or not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("buckets"), dict):
            raise ValueError("结构不对")
        return raw
    except Exception as e:
        logger.warning("课程快照读取失败，按「没有快照」处理：%s", e)
        return {}


def save_courses_bucket(
    *, kklxdm: str, kklxmc: str, tab_index: int, keyword: str,
    rows: list, semester: str = "",
) -> bool:
    """把**一次搜索结果**存进它那个类别的桶（该桶旧内容被覆盖）。

    ⚠️ 只在 rows 非空时调用：一次失败的 / 空的搜索绝不能把已有的好数据冲掉。

    读-改-写整个文件 → 用一把模块级锁串起来（并发搜索时不会互相丢桶）。
    写盘仍是「先写 .tmp 再 os.replace」的原子替换。
    """
    p = courses_path()
    if p is None or not rows:
        return False
    rows = rows[:COURSES_BUCKET_MAX]
    key = courses_bucket_key(kklxdm, kklxmc)
    bucket = {
        "kklxdm": kklxdm or "",
        "kklxmc": kklxmc or "",
        "tab_index": tab_index,
        "keyword": keyword or "",
        "saved_at": time.time(),
        "semester": semester or "",
        "count": len(rows),
        "rows": rows,
    }
    try:
        with _COURSES_LOCK:
            doc = read_courses_snapshot()
            buckets: dict = dict(doc.get("buckets") or {})
            buckets[key] = bucket
            # 桶太多就丢掉最旧的（按 saved_at），别让文件无限长大
            if len(buckets) > COURSES_MAX_BUCKETS:
                keep = sorted(
                    buckets.items(), key=lambda kv: kv[1].get("saved_at") or 0, reverse=True
                )[:COURSES_MAX_BUCKETS]
                buckets = dict(keep)
            out = {
                "version": 1,
                "saved_at": max(b.get("saved_at") or 0 for b in buckets.values()),
                "buckets": buckets,
            }
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        logger.info(
            "课程数据已落盘：Tab=%s/%s 共 %d 行（keyword=%r）",
            kklxdm, kklxmc, len(rows), keyword or "",
        )
        return True
    except Exception as e:
        logger.warning("课程数据落盘失败（不影响本次搜索）：%s", e)
        return False


def courses_snapshot_summary() -> dict:
    """课程快照的**摘要**（不含 rows，给界面用）。

    返回 `{}` = 没有快照。否则：
      exists / path / saved_at / total / buckets[{key,kklxdm,kklxmc,tab_index,
      keyword,count,saved_at,courses(前几门课名)}]
    """
    doc = read_courses_snapshot()
    buckets = doc.get("buckets") or {}
    if not buckets:
        return {}
    items = []
    for key, b in buckets.items():
        rows = b.get("rows") or []
        names = []
        seen = set()
        for r in rows:                      # 同一门课有多个教学班 → 去重后列前几个
            nm = str((r or {}).get("kcmc") or (r or {}).get("kch") or "").strip()
            if nm and nm not in seen:
                seen.add(nm)
                names.append(nm)
            if len(names) >= 8:
                break
        items.append({
            "key": key,
            "kklxdm": b.get("kklxdm") or "",
            "kklxmc": b.get("kklxmc") or "",
            "tab_index": b.get("tab_index", -1),
            "keyword": b.get("keyword") or "",
            "count": len(rows),
            "saved_at": b.get("saved_at"),
            "semester": b.get("semester") or "",
            "courses": names,
        })
    items.sort(key=lambda x: x.get("saved_at") or 0, reverse=True)
    return {
        "exists": True,
        "path": str(courses_path()),
        "saved_at": doc.get("saved_at"),
        "total": sum(i["count"] for i in items),
        "buckets": items,
    }


def pick_courses_bucket(summary: dict, *, kklxdm: str = "", kklxmc: str = "") -> dict | None:
    """在摘要里挑出「当前这个类别」对应的桶。

    三级回退：精确 key → 类别名 → kklxdm。
    ⚠️ 为什么要回退：桶是**上次**存的，教务可能改过类别名或下标；
    只按最严的判据匹配，用户就会看到「上次明明存了、现在却说没有」。
    全都匹配不上返回 None（调用方据此明确告知，而不是给一份别的类别的课）。
    """
    buckets = (summary or {}).get("buckets") or []
    if not buckets:
        return None
    exact = courses_bucket_key(kklxdm, kklxmc)
    for b in buckets:
        if b.get("key") == exact:
            return b
    if kklxmc:
        for b in buckets:
            if (b.get("kklxmc") or "").strip() == kklxmc.strip():
                return b
    if kklxdm:
        for b in buckets:
            if (b.get("kklxdm") or "").strip() == kklxdm.strip():
                return b
    return None


def load_courses_bucket(key: str) -> dict:
    """按 key 取出某个桶的完整内容（含 rows）。取不到返回 {}。"""
    doc = read_courses_snapshot()
    b = (doc.get("buckets") or {}).get(key)
    return b if isinstance(b, dict) else {}


def snap_semester_text(sems: set) -> str:
    """把一组学期标记说成人话（日志/事件里用）。"""
    vals = sorted(v for v in sems if v)
    return "、".join(vals) if vals else "学期未知"


def clear_courses_snapshot() -> bool:
    """删掉课程快照文件（换学期作废时用）。"""
    p = courses_path()
    if p is None:
        return False
    with _COURSES_LOCK:
        try:
            if p.exists():
                p.unlink()
            return True
        except Exception as e:
            logger.warning("课程快照删除失败：%s", e)
            return False


def _save_plan_to(path: Path | None, plan: Plan | None, *, semester: str = "") -> bool:
    """把清单写进指定文件（原子覆盖）。`plan is None` 或空则删文件。

    这是「落盘 = 清单最后一次状态」这条不变式的底层实现，抢课清单与蹲课清单
    共用（只是文件名不同）。先写 `.tmp` 再 `os.replace`：进程写一半被杀，
    磁盘上要么是完整的旧文件、要么是完整的新文件，绝不半截 JSON。
    """
    if path is None:
        return False
    try:
        if plan is None or not plan.items:
            if path.exists():
                path.unlink()
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        data = plan.to_dict()
        data["version"] = PLAN_FORMAT_VERSION
        data["saved_at"] = time.time()
        data["semester"] = semester or ""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning("清单落盘失败（内存中的清单不受影响）：%s", e)
        return False


def save_plan_file(plan: Plan | None, *, semester: str = "") -> bool:
    """把清单写进**唯一那个**槽位：`<STATE_DIR>/plan.json`。

    「每一次有新清单出现就把上一次的清掉」在这里落地：全项目只有一个文件名、
    一次 `os.replace()` 原子覆盖。不按时间戳、不按哈希分文件，所以历史清单
    **不会堆积**；`plan is None` 或空清单则直接删文件，语义最直白。
    """
    return _save_plan_to(plan_path(), plan, semester=semester)


def save_wait_file(plan: Plan | None, *, semester: str = "") -> bool:
    """把蹲课清单写进独立槽位：`<STATE_DIR>/wait.json`（2026-10-01 立）。

    与抢课清单 `plan.json` 分开存 —— 蹲课是独立清单，不能和抢课互相覆盖。
    同样单槽原子覆盖，不堆积。
    """
    return _save_plan_to(wait_path(), plan, semester=semester)


def _school_from_env() -> SchoolProfile | None:
    """`XK_SCHOOL_URL` 环境变量：把客户端指向本地模拟教务（serve.py --mock 设置）。

    返回 None 表示用默认真实学校（广州南方学院 https://jwxt.nfu.edu.cn/jwglxt/）。
    只对「往哪里发请求」生效，凭据逻辑不变。

    ⚠️ 切回真实教务的正确姿势（三选一，推荐第 1 个）：
      1) 不带 --mock 直接 `python serve.py`（全新进程，天然没有该变量）
      2) `python serve.py --real`  ← 显式清掉残留的 XK_SCHOOL_URL
      3) 手动 `unset XK_SCHOOL_URL`（Windows: `set XK_SCHOOL_URL=`）后再启动
    之所以要强调：`XK_SCHOOL_URL` 是**进程级环境变量**，若它被留在
    当前 shell 里（例如手工 export 过、或某些 launcher 会继承），
    那么不加 --mock 启动也会**悄悄打到本地假教务**。`--real` 就是为这个兜底。
    """
    url = os.environ.get("XK_SCHOOL_URL", "").strip()
    if not url:
        return None
    return SchoolProfile(name="本地模拟教务", base_url=url)

# 事件环形缓冲容量（够前端断线重连补齐，也不至于占内存）
EVENT_BUFFER = 500


@dataclass
class Session:
    """当前登录会话。凭据在内存，不落盘。

    ⚠️ 「持有凭据」≠「登录态有效」。凭据只在内存里躺着，教务那边随时可能因为
    「用户在网站点了退出登录 / Cookie 过期 / 会话被挤下线」而作废，而本进程
    **不会自己知道**。所以这里额外记「最后一次联网核实」的结果，
    界面必须看 `Runtime.session_state`，不能只看 `has_session`。
    """

    credential: Credential
    client: ZfClient
    created_at: float = field(default_factory=time.time)
    store: dict = field(default_factory=dict)
    is_open: bool = False
    inited: bool = False

    #: 最后一次**联网核实**登录态的时刻（epoch 秒）。0 = 建会话后还没核实过。
    verified_at: float = 0.0
    #: 是否已确认**登录态失效**（教务回登录页 / 302）。一旦为 True，
    #: 只有「重新建会话」或「一次成功的探活/init」才能把它清掉。
    invalid: bool = False
    invalid_at: float = 0.0
    invalid_msg: str = ""

    # 已选课程缓存（规范化后的「占时间记录」）。
    # None = 从未拉取过；[] = 拉取过但确实一门没选。
    # 缓存的意义：课表与「加课判冲突」都要用它，而用户明确要求**不要为它反复
    # 请求教务**（怕拖慢抢课）。所以拉一次就存在这里，之后前端刷新页面也只从
    # 本服务读缓存，不再打教务。
    selected: list["ScheduleEntry"] | None = None
    selected_at: float = 0.0


class Runtime:
    """全局运行时。线程安全（UI 层可能被浏览器并发调用）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 写操作串行锁（铁律 #6：写入串行）。抢课由 runner 内部自己串行，
        # 退课这类「一次性的写」走这把锁，保证同一个客户端同时只有一个写在飞。
        self.write_lock = threading.Lock()
        self._session: Session | None = None
        self._runner: GrabbingRunner | None = None
        self._plan: Plan | None = None
        # ⭐ 蹲课（2026-10-01 立）：独立清单 + 独立 runner，与抢课互不干扰。
        #   蹲课 = 长时间蹲「已满」课的退课名额，小时级，走 round_robin + 按时间，
        #   不复用抢课的 MAX_ATTEMPTS 次数闸。落盘到独立的 wait.json。
        self._wait_runner: GrabbingRunner | None = None
        self._wait_plan: Plan | None = None
        self._wait_meta: dict = {}
        self._wait_rev = 0
        # 内存这份清单的描述（给界面看）：{path, count, saved_at, semester, version, restored}
        # ⚠️ `restored=True` 现在的含义是「这份清单是用户**主动加载**的上次数据」，
        # 不再是「进程启动时自动恢复的」——见 __init__ 末尾的说明。
        self._plan_meta: dict = {}
        # 清单版本号（乐观锁）：每被整体替换一次 +1，见 PlanConflict。
        self._plan_rev = 0
        # 「磁盘快照」元信息的小缓存（(单调钟, dict)）。磁盘快照只在内存没有清单时
        # 才有意义，而 /api/state 是 3 秒一次轮询 —— 加个 TTL 免得白读盘。
        self._snap_cache: tuple[float, dict] = (0.0, {})
        # 「上次搜到的课程」摘要的小缓存，理由同上
        self._courses_cache: tuple[float, dict] = (0.0, {})
        self._events: deque[Event] = deque(maxlen=EVENT_BUFFER)
        self._seq = 0  # 事件序号，供前端增量拉取

        # ⚠️ 启动**不自动恢复**清单（2026-09-30 用户要求）。
        #
        # 旧行为是启动就 `load_plan_file()` 把上次的清单塞回内存。问题在于：
        # 用户并不知道它会在什么时候冒出来 —— 可能是上一学期的、可能是上一轮
        # 已经抢完的，界面上突然就有了一份「不是我现在攒的」清单，
        # 随手点一下「开始抢课」就拿着旧目标去打教务了。
        #
        # 现在磁盘上那份只当**素材**：用户主动点「加载上次数据」才进内存
        # （`Runtime.load_snapshot` ← `POST /api/plan/load`）。
        # 想看一眼它是什么、有几项，走 `Runtime.snapshot_meta`（只读，不构造 Plan）。
        # 典型使用时机：查课期与抢课期之间那段「未开放期」——
        # 那时教务什么都查不到，把上次存的数据拿出来正好看课。
        _snap = plan_snapshot()
        logger.info(
            "启动完成（不自动恢复清单）；磁盘快照：%s",
            (f"有 {_snap['count']} 项，存于 {_snap.get('path')}") if _snap else "无",
        )

    # -- 会话 ---------------------------------------------------------------

    @property
    def session(self) -> Session | None:
        return self._session

    def has_session(self) -> bool:
        return self._session is not None

    def attach_session(self, credential: Credential) -> Session:
        """（重新）建立会话。会关闭旧客户端。"""
        with self._lock:
            if self._session:
                try:
                    self._session.client.close()
                except Exception:
                    pass
            client = ZfClient(credential, _school_from_env())
            self._session = Session(credential=credential, client=client)
            return self._session

    def require_session(self) -> Session:
        if not self._session:
            raise RuntimeError("尚未提供凭据，请先粘贴 Cookie 或使用 CDP 抓取")
        return self._session

    # -- 登录态是否还有效 ---------------------------------------------------

    @property
    def session_state(self) -> str:
        """会话状态，给界面用。四个取值：

              none       没有会话
              expired    有会话，但已确认**登录态失效**（教务把你踢回登录页了）
              ok         有会话，且**联网核实过**还有效
              unverified 有会话，但建会话之后还没核实过（或核实结果已经太旧）

        ⚠️ 为什么不能只看「有没有会话对象」：凭据躺在内存里不会自己过期，
        但教务那边的会话会（用户在网站点了退出登录 / Cookie 到期 / 被挤下线）。
        只报「持有凭据」的话，界面会一直显示「已登录」，与实际完全脱节。
        """
        sess = self._session
        if sess is None:
            return "none"
        if sess.invalid:
            return "expired"
        return "ok" if sess.verified_at else "unverified"

    def note_session_verified(self, *, is_open: bool | None = None) -> None:
        """记下「刚刚联网核实过，登录态还有效」。顺带更新「是否已开放」。

        为什么允许顺带更新 `is_open`：探活打的正是选课首页那一份 HTML，
        判据和 `init()` 完全同源（`core.client.page_is_open`），不用白不用 ——
        而且「选课已开放」这个徽章同样会因为长期不刷新而失真。
        """
        with self._lock:
            sess = self._session
            if sess is None:
                return
            sess.verified_at = time.time()
            was_invalid = sess.invalid
            sess.invalid = False
            sess.invalid_at = 0.0
            sess.invalid_msg = ""
            if is_open is not None:
                sess.is_open = bool(is_open)
            if was_invalid:
                logger.info("登录态已恢复有效（重新核实成功）")

    def mark_session_invalid(self, msg: str = "", *, notify: bool = True) -> None:
        """确认登录态失效。

        `notify=True` 时额外推一条 NEED_LOGIN 事件，让界面**立刻**翻牌
        （不必等下一次 3 秒轮询，也不必等用户去点一下操作才发现）。

        ⚠️ 不去动 `self._session`（**不** detach）：保留它才能告诉用户
        「你之前用的是哪种来源的凭据、什么时候建的」，也方便界面给出「重新登录」
        而不是「什么都没有」。真正需要丢弃时走 `detach_session()`。
        """
        with self._lock:
            sess = self._session
            if sess is None or sess.invalid:
                return  # 已经记过一次就别反复刷屏
            sess.invalid = True
            sess.invalid_at = time.time()
            sess.invalid_msg = msg or "登录态已失效"
            detail = sess.invalid_msg
        logger.warning("登录态已失效：%s", detail)
        if notify:
            self.push_event(
                Event(
                    type=EventType.NEED_LOGIN,
                    message=f"登录态已失效（{detail}）；请重新建立会话后再操作",
                )
            )

    def detach_session(self) -> None:
        with self._lock:
            if self._session:
                try:
                    self._session.client.close()
                except Exception:
                    pass
            self._session = None

    # -- 计划 ---------------------------------------------------------------

    @property
    def plan(self) -> Plan | None:
        return self._plan

    @property
    def wait_plan(self) -> Plan | None:
        return self._wait_plan

    @property
    def wait_meta(self) -> dict:
        return dict(self._wait_meta)

    @property
    def wait_running(self) -> bool:
        """蹲课任务是否还在忙（同一时刻只允许一个蹲课任务）。"""
        r = self._wait_runner
        return r is not None and not r.stopped and not r.done

    @property
    def plan_meta(self) -> dict:
        """内存这份清单的描述（路径 / 项数 / 存入时刻 / 学期 / 是否用户主动加载的）。"""
        return dict(self._plan_meta)

    @property
    def snapshot_meta(self) -> dict:
        """**磁盘上**「上次保存的清单快照」的元信息（只读，不加载）。

        ⚠️ 内存里已经有清单时一律返回 `{}`：那种情况下磁盘上那份要么不存在
        （内存清单每次变化都会覆盖/删除它），要么就是内存这份自己的副本 ——
        报出来只会让界面多一条「上次保存的数据……」的误导提示。

        带 2 秒 TTL 缓存：`/api/state` 是 3 秒一轮询，而它只在「内存没有清单」时
        才会走到读盘那一步。
        """
        with self._lock:
            has_mem = self._plan is not None and bool(self._plan.items)
            now = time.monotonic()
            ts, cached = self._snap_cache
        if has_mem:
            return {}
        if now - ts < _SNAPSHOT_CACHE_S:
            return dict(cached)
        val = plan_snapshot()
        with self._lock:
            self._snap_cache = (now, val)
        return dict(val)

    @property
    def courses_snapshot(self) -> dict:
        """**磁盘上**「上次搜到的课程」的摘要（只读，不含 rows）。

        与 `snapshot_meta` 不同，这个**不随内存状态清空** —— 课程数据是
        「查课期攒下来的素材」，即使清单已经加载进内存，用户照样可能想回看
        「上次这个类别下有哪些课」。带 2 秒 TTL 缓存（`/api/state` 是 3 秒轮询）。
        """
        with self._lock:
            now = time.monotonic()
            ts, cached = self._courses_cache
        if now - ts < _SNAPSHOT_CACHE_S:
            return dict(cached)
        val = courses_snapshot_summary()
        with self._lock:
            self._courses_cache = (now, val)
        return dict(val)

    def invalidate_courses_cache(self) -> None:
        """刚写过课程快照 → 摘要缓存作废（下一次 `/api/state` 立刻反映出来）。"""
        with self._lock:
            self._courses_cache = (0.0, {})

    @property
    def plan_rev(self) -> int:
        """当前清单版本号（乐观锁）。前端每次 `/api/state` 都会拿到它。"""
        with self._lock:
            return self._plan_rev

    def load_snapshot(self) -> dict:
        """把磁盘上的快照**加载进内存**，成为当前抢课清单 —— 用户主动点击才走这里。

        返回 `{"ok": bool, "count": int, "meta": dict, "reason": str}`。

        ⚠️ 刻意**不**回写磁盘：加载前后磁盘内容一模一样，回写只是白写一次文件。
        落盘交给后续任何一次清单变化（`set_plan` / `start` / `clear_plan`）。

        ⚠️ 打开的这个口子**绕过**了乐观锁（它本身就是「整份替换」的显式指令），
        所以版本号要自增 —— 前端手里那个版本号随之作废，下次 `/api/plan`
        提交会拿到 409 并重新载入（安全的那一侧）。
        前端也可以直接靠「服务端 rev 变大了」触发重新 hydrate（见 app.js）。
        """
        plan, meta = load_plan_file()
        with self._lock:
            if plan is None:
                return {"ok": False, "count": 0, "meta": {}, "reason": "磁盘上没有可用的清单快照"}
            self._plan = plan
            # restored=True：告诉界面「这份清单是从本地文件来的」，与本次会话里
            # 一门一门加上去的区分开（界面据此说明来源、并提示状态已重置为「待选」）
            self._plan_meta = dict(meta, restored=True)
            self._plan_rev += 1
            self._snap_cache = (0.0, {})   # 内存有清单了 → 快照元信息随之失效
            n = len(plan.items)
            path = self._plan_meta.get("path", "?")
        logger.info("已加载落盘快照：%d 项（来自 %s）", n, path)
        return {"ok": True, "count": n, "meta": dict(self._plan_meta), "reason": ""}

    def clear_snapshot(self) -> bool:
        """删掉磁盘上的快照（不影响内存里的清单）。

        唯一用途：换学期对账时作废一份属于上一学期的快照。
        """
        with self._lock:
            self._snap_cache = (0.0, {})
        return save_plan_file(None)

    def semester_key(self) -> str:
        """当前选课上下文所属学期（`2026-2027|1`）。没会话/不认识该字段时返回 ""。"""
        sess = self._session
        client = getattr(sess, "client", None) if sess else None
        if client is None:
            return ""
        try:
            return client.semester_key or ""
        except Exception:
            return ""

    def set_plan(self, plan: Plan, *, expect_rev: int | None = None) -> int:
        """写入清单并**立刻落盘**（单槽覆盖 → 上一次的清单就此清掉）。返回新版本号。

        乐观锁：`expect_rev` 非 None 时，必须等于「前端看到的那个版本号」；
        对不上就抛 `PlanConflict`，**一个字节都不写**。

        为什么检查和写入必须在**同一把锁**里：两个页面同时提交时，
        若「读版本」与「写清单」不在同一个临界区，两边都会读到旧版本、都通过检查，
        后写的那个照样把先写的盖掉 —— 锁就白加了。

        `expect_rev=None` = 前端没带版本号（脚本 / CLI 这类一次性写入者），
        按「无条件覆盖」处理。⚠️ 界面**必须**带，否则这道保护形同虚设。
        """
        with self._lock:
            if expect_rev is not None and expect_rev != self._plan_rev:
                raise PlanConflict(
                    expect_rev, self._plan_rev, len(self._plan.items) if self._plan else 0
                )
            self._plan = plan
            self._persist_plan()
            self._plan_rev += 1
            return self._plan_rev

    def clear_plan(self) -> None:
        """清空清单，连同落盘文件一起删掉。"""
        with self._lock:
            self._plan = None
            self._plan_meta = {}
            save_plan_file(None)
            self._snap_cache = (0.0, {})
            self._plan_rev += 1

    def _persist_plan(self) -> None:
        """把当前清单写进单槽文件；空清单 = 删文件。调用方需已持锁。

        这是「落盘 = 内存清单的最后一次状态」这条不变式的**唯一实现点**：
        内存清单每变一次（`set_plan` / `start` / 清空），磁盘就跟着变成同一份。
        """
        plan = self._plan
        self._snap_cache = (0.0, {})   # 磁盘刚变过 → 快照元信息缓存作废
        if plan is None or not plan.items:
            save_plan_file(None)
            self._plan_meta = {}
            return
        sem = self.semester_key()
        ok = save_plan_file(plan, semester=sem)
        self._plan_meta = (
            {
                "path": str(plan_path()),
                "count": len(plan.items),
                "saved_at": time.time(),
                "semester": sem,
                "version": PLAN_FORMAT_VERSION,
                "restored": False,
            }
            if ok
            else {}
        )

    def reconcile_plan_semester(self) -> dict:
        """把**本地保存的数据**与当前会话的学期对账；跨学期的作废并清掉落盘文件。

        为什么要做：清单 / 课程数据里的课程号、教学班、Tab 下标全都与学期轮次绑定。
        上一学期的东西拿到这一学期去用，只会换回一串「课程不存在 / 加密串错误」，
        而且还会把「已解析的教学班」这种假象带进界面。宁可在建会话时就明确作废、
        并且**告诉用户**（推一条日志事件），也不要留一份看起来完好的废数据。

        ⚠️ 对账对象有**三个**，而且**各自独立判断**（别指望一个分支清掉全部）：
          1. 内存里那份清单（用户正在用的）—— 对它的 `_plan_meta.semester`；
          2. **磁盘上的清单快照**（还没被加载的那份）—— 对文件里记的 `semester`；
          3. **磁盘上的课程数据**（「上次搜到的课程」）—— 对每个桶自己的 `semester`。
        ② 必须查：否则用户点「加载上次数据」会拿到一份上一学期的废清单。
        ③ 更要独立查：用户完全可能**只搜过课、一门清单都没建** ——
        那种情况下 ①② 的 `stored` 是空的，若把清课程数据挂在清单那个分支里，
        上一学期的课程数据就会活到新学期，而未开放期界面看起来一切正常。

        判据从宽：两端有一个学期未知就不作废 —— 宁可多留一份可用数据，
        也不要因为教务偶尔不发学期字段就误删用户辛辛苦苦攒的东西。
        """
        dropped: list[str] = []
        stored = ""
        n = 0
        with self._lock:
            current = self.semester_key().strip()
            if not current:
                return {"dropped": False, "from": "", "to": ""}

            # ---- ① / ② 清单（内存优先，其次磁盘快照）----
            in_mem = self._plan is not None and bool(self._plan.items)
            if in_mem:
                stored = (self._plan_meta.get("semester") or "").strip()
                n = len(self._plan.items)
            else:
                snap = plan_snapshot()
                stored = (snap.get("semester") or "").strip()
                n = int(snap.get("count") or 0)
            if stored and stored != current:
                self._plan = None
                self._plan_meta = {}
                save_plan_file(None)
                self._snap_cache = (0.0, {})
                if in_mem:
                    # 只有「内存那份被作废」才需要让前端的版本号跟着跳
                    # （否则开着页面的浏览器会继续拿着上一学期那份清单去提交）。
                    # 只是删掉一份没人加载的磁盘快照时，界面本来没显示它，不必惊动前端。
                    self._plan_rev += 1
                dropped.append("内存里的清单" if in_mem else "本地保存的清单")

            # ---- ③ 课程数据（看它自己的学期标记，独立判断）----
            csnap = courses_snapshot_summary()
            if csnap:
                sems = {b.get("semester") for b in csnap.get("buckets") or []}
                sems.discard("")
                if sems and current not in sems:
                    clear_courses_snapshot()
                    self._courses_cache = (0.0, {})
                    dropped.append(f"本地保存的课程数据（{snap_semester_text(sems)}）")

        if not dropped:
            return {"dropped": False, "from": stored, "to": current}
        what = "、".join(dropped)
        logger.warning(
            "本地数据所属学期已变（→ %s），已作废：%s", current, what
        )
        self.push_event(
            Event(
                type=EventType.LOG,
                message=(
                    f"{what}属于上一学期，当前是 {current}，已作废"
                    + (f"那 {n} 项清单" if stored else "")
                    + "；请在本学期重新搜索课程 / 重新挑选"
                ),
            )
        )
        return {"dropped": True, "from": stored, "to": current, "what": what}

    # -- 已选课程缓存 -------------------------------------------------------

    @property
    def selected(self) -> list["ScheduleEntry"] | None:
        """已选课程的规范化记录；None 表示还没拉过。"""
        return self._session.selected if self._session else None

    def set_selected(self, entries: list["ScheduleEntry"]) -> None:
        """写入已选缓存（由 /api/selected 调用，是唯一写入点）。"""
        with self._lock:
            if self._session:
                self._session.selected = list(entries)
                self._session.selected_at = time.time()

    def invalidate_selected(self) -> None:
        """把已选缓存作废，逼下一次 `/api/selected` 重新问教务。

        退课后必须调用 —— 否则课表、冲突判定、退课按钮都还停留在退课前的状态。
        """
        with self._lock:
            if self._session:
                self._session.selected = None
                self._session.selected_at = 0.0

    def index_store(self) -> dict:
        """Index 页隐藏域快照（退课资格判定要用它取 xxdm 等页面级开关）。"""
        sess = self._session
        if not sess:
            return {}
        return dict(getattr(sess.client, "index_store", {}) or {})

    def known_entries(self) -> list["ScheduleEntry"]:
        """判断冲突时「已知会占时间」的全部记录 = 已选 + 当前清单。

        清单项算进来是必要的：同一轮里待选的两门课互相撞时间，也得在加课时就拦住。
        """
        out: list[ScheduleEntry] = list(self.selected or [])
        plan = self._plan
        if plan:
            for it in plan.items:
                if not it.slots:
                    continue
                out.append(
                    ScheduleEntry(
                        kch_id=it.kch_id,
                        kcmc=it.kcmc or it.kch_id,
                        jxbmc=it.kcmc or "",
                        slots=list(it.slots),
                        source="pending",
                    )
                )
        return out

    # -- 任务 ---------------------------------------------------------------

    @property
    def runner(self) -> GrabbingRunner | None:
        return self._runner

    @property
    def running(self) -> bool:
        """是否真的还在忙。

        不能只看 `stopped` —— 抢完目标自然结束时 `stopped` 仍是 False，
        那样一轮结束后界面会一直以为任务在运行（按钮禁用、无法再开一轮）。
        """
        r = self._runner
        return r is not None and not r.stopped and not r.done

    def start(self, plan: Plan) -> None:
        """启动抢课。同一时刻只允许一个任务。"""
        with self._lock:
            if self.running:
                raise RuntimeError("已有抢课任务在运行，请先停止")
            sess = self.require_session()
            self._plan = plan
            # 落盘一次：保证「self._plan 有内容 ⇒ 磁盘上也有」这条不变式恒成立
            self._persist_plan()
            # ⚠️ 只清**事件缓冲**（日志面板只显示本轮），**绝不清 `_seq`**。
            #
            # `_seq` 一旦归零，前端的游标立刻「超车」：前端那份 `S.seq` 还停在上
            # 一轮的最后一号，于是 `seq > since` 永远不成立 —— **实时日志一片空白**。
            # 2026-10-01 用户报的「串行模式实时日志里怎么不显示了」就是这个：
            # 只要点过第二次「开始」，第二轮的日志就整段不显示（第一轮正常）。
            # 序号必须**在整个进程生命周期内单调**，它标识的是「事件在时间上的位置」，
            # 不是「本轮第几条」。要清只清 `_events`。
            self._events.clear()
            runner = GrabbingRunner(sess.client, plan, on_event=self.push_event)
            self._runner = runner
            runner.start_background()

    def stop(self) -> bool:
        with self._lock:
            if not self._runner:
                return False
            self._runner.stop()
            return True

    # -- 蹲课任务（2026-10-01）----------------------------------------------

    def set_wait_plan(self, plan: Plan, *, expect_rev: int | None = None) -> int:
        """写入蹲课清单并落盘（独立 wait.json）。返回新版本号。

        与 `set_plan` 同构的乐观锁：`expect_rev` 非 None 时必须等于前端看到的版本号，
        对不上抛 `PlanConflict` 一个字节不写。
        """
        with self._lock:
            if expect_rev is not None and expect_rev != self._wait_rev:
                raise PlanConflict(
                    expect_rev, self._wait_rev, len(self._wait_plan.items) if self._wait_plan else 0
                )
            self._wait_plan = plan
            self._persist_wait_plan()
            self._wait_rev += 1
            return self._wait_rev

    def clear_wait_plan(self) -> None:
        with self._lock:
            self._wait_plan = None
            self._wait_meta = {}
            save_wait_file(None)
            self._wait_rev += 1

    def _persist_wait_plan(self) -> None:
        plan = self._wait_plan
        if plan is None or not plan.items:
            save_wait_file(None)
            self._wait_meta = {}
            return
        sem = self.semester_key()
        ok = save_wait_file(plan, semester=sem)
        self._wait_meta = (
            {
                "path": str(wait_path()),
                "count": len(plan.items),
                "saved_at": time.time(),
                "semester": sem,
                "version": PLAN_FORMAT_VERSION,
            }
            if ok
            else {}
        )

    def start_wait(self, plan: Plan) -> None:
        """启动蹲课。同一时刻只允许一个蹲课任务（也不和抢课互斥，但各自只一个）。"""
        with self._lock:
            if self.wait_running:
                raise RuntimeError("已有蹲课任务在运行，请先停止")
            sess = self.require_session()
            self._wait_plan = plan
            self._persist_wait_plan()
            # 事件缓冲与抢课共用；蹲课开始同样只清缓冲、不清 _seq（见 start() 的长注释）
            self._events.clear()
            runner = GrabbingRunner(sess.client, plan, on_event=self.push_event)
            self._wait_runner = runner
            runner.start_background()

    def stop_wait(self) -> bool:
        with self._lock:
            if not self._wait_runner:
                return False
            self._wait_runner.stop()
            return True

    # -- 事件 ---------------------------------------------------------------

    def push_event(self, ev: Event) -> None:
        """引擎回调入口。"""
        with self._lock:
            self._seq += 1
            setattr(ev, "_seq", self._seq)
            self._events.append(ev)
        if ev.type is EventType.NEED_LOGIN:
            # 抢课跑在后台线程里，它撞上「登录态失效」只会推一条 NEED_LOGIN 事件
            # （见 engine/runner.py::_handle_fatal）。这里顺手把会话标记为失效 ——
            # 否则界面顶部会继续显示「已登录」，与实际完全相反。
            # ⚠️ notify=False：事件本身就是通知，不必再补一条。
            self.mark_session_invalid(
                getattr(ev, "message", "") or "登录态已失效", notify=False
            )

    def clamp_since(self, since: object) -> int:
        """把「可能是上一次服务进程留下的」陈旧游标夹回 0。

        ⚠️ 为什么必须有它：`_seq` 是**进程内**单调的，而前端那份游标活在浏览器里、
        可以跨进程存活（服务重启过、页面没刷新）。一旦 `since > self._seq`，
        `seq > since` 就永远不成立 —— **前端再也收不到任何事件，实时日志一片空白**。
        此时唯一的正确解读是「这份游标说的是上一个进程的事」，从最早一条重放。

        正常情况（`0 <= since <= _seq`）原样返回，增量语义不受影响。
        """
        try:
            s = int(since)
        except (TypeError, ValueError):
            return 0
        if s < 0 or s > self._seq:
            return 0
        return s

    def events_since(self, since: int = 0) -> list[dict]:
        """取序号 > since 的事件（前端增量拉取）。

        ⭐ **必须用 `clamp_since()` 过一道**：调用方常把 `since` 直接当成下一次的
        起点（`last = max(last, ev["seq"])`），所以陈旧游标要在**入口**夹掉，
        只在这里夹是不够的 —— 调用方那个 `last` 会一直是大数。

        ⚠️ 缓冲是「只保留本轮」（`start()` 清过）而 `_seq` 是全程单调的，
        所以两者不相等是**正常**的：`since=0` 拿到的就是本轮已有的事件。
        """
        since = self.clamp_since(since)
        with self._lock:
            out = []
            for ev in self._events:
                if getattr(ev, "_seq", 0) > since:
                    d = ev.as_dict()
                    d["seq"] = getattr(ev, "_seq", 0)
                    out.append(d)
            return out

    @property
    def seq(self) -> int:
        return self._seq

    # -- 汇总 ---------------------------------------------------------------

    def clock_snapshot(self) -> dict | None:
        """当前会话的时钟校准快照（未建会话 / 假客户端时返回 None）。"""
        sess = self._session
        if not sess:
            return None
        clk = getattr(sess.client, "clock", None)
        if clk is None:
            return None
        try:
            return clk.as_dict()
        except Exception:  # 界面快照绝不能因为时钟异常而整体挂掉
            logger.exception("读取时钟快照失败")
            return None

    def credit_snapshot(self) -> dict:
        """学分要求 + 待加选学分。

        两个来源分开对待：
        - **待加选学分**来自本地清单 → 断线也照样算得出来
        - **最低/最高/已选**来自教务 Index 页 → 没会话就只能是 None

        这样没会话时界面仍能显示「你要抢的课共 N 学分」，只是没有对照基准。
        """
        plan_credit = self._plan.total_credit() if self._plan else 0.0
        out: dict = {"found": False, "plan_credit": plan_credit, "projected": None, "over": False}
        sess = self._session
        info = getattr(sess.client, "credit", None) if sess else None
        if info is None:
            return out
        try:
            out.update(info.as_dict())
            # projected：全抢到之后的总学分。已选未知就留 None，别硬算成 plan_credit。
            out["projected"] = (
                round(info.used_credit + plan_credit, 2)
                if info.used_credit is not None
                else None
            )
            remain = info.remain_credit
            # over=True 表示「照这份清单去抢，会撞上本学期最高学分上限」。
            # 加 1e-9 是为了抹掉浮点误差（30.0 - 29.0 可能算出 1.0000000000000009）。
            out["over"] = remain is not None and plan_credit > remain + 1e-9
            return out
        except Exception:  # 同 clock：快照不能因为学分解析异常整体挂掉
            logger.exception("读取学分快照失败")
            return out

    def summary(self) -> dict:
        """给前端的状态快照。"""
        with self._lock:
            p = self._plan
            items = []
            if p:
                for it in p.sorted_items():
                    # 字段要给全 —— 前端刷新页面后要能据此重建整份清单
                    # （缺 kklxdm 就没法重新解析教学班，缺 interval_ms 就丢了重试节奏）
                    items.append(
                        {
                            "kch_id": it.kch_id,
                            "kcmc": it.kcmc or it.kch_id,
                            "jsxx": it.jsxx,
                            # 学分：前端据此汇总「待加选学分」
                            "xf": it.xf,
                            "do_id": it.do_id,
                            # 教学班的稳定 id：do_id 是会过期的一次性加密令牌，
                            # 刷新后 do_id 变了，界面要能靠 jxb_id 认回同一个班
                            "jxb_id": it.jxb_id,
                            "kklxdm": it.kklxdm,
                            # Tab 下标：kklxdm 会重复，回传它前端才能原样带回来
                            "tab_index": it.tab_index,
                            "cxbj": it.cxbj,
                            "fxbj": it.fxbj,
                            "priority": it.priority,
                            "interval_ms": it.interval_ms,
                            "precheck": it.precheck,
                            # 单项时间预算（0 = 不限）。前端刷新重建清单时要带上，
                            # 否则一经刷新这一项的时间闸就悄悄丢了。
                            "budget_s": it.budget_s,
                            # 清单项也要能画到课表上、也要能参与冲突判定，
                            # 否则刷新页面后课表上「待选」那一层就没了
                            "slots": [s.as_dict() for s in it.slots],
                            "sksj": " / ".join(s.text for s in it.slots),
                            "state": it.state.value,
                            "attempts": it.attempts,
                            "max_attempts": it.max_attempts,
                            "last_msg": it.last_msg,
                        }
                    )
            clock = self.clock_snapshot()
            sel = self.selected
            sess = self._session
            return {
                "has_session": self.has_session(),
                # ⚠️ 光有 has_session 是不够的：「内存里攥着一个凭据」和「教务还认这个登录态」
                # 是两件事。凭据不会自己过期，但教务那边会（退出登录 / Cookie 到期 / 被挤下线）。
                # 界面必须看 session_state，别只看 has_session（否则会一直显示「已登录」）。
                "session_state": self.session_state,
                # 上一次**联网核实**登录态的时刻（epoch 秒）；0 = 从没核实过。
                # 前端据此决定「该不该去探活了」（见 ui/app.py::/api/session/check）。
                "session_checked_at": (sess.verified_at if sess else 0.0) or None,
                # 已确认失效时的原因（给界面显示，别只说一句「失效了」）
                "session_invalid_msg": (sess.invalid_msg if sess and sess.invalid else ""),
                "source": sess.credential.source if sess else None,
                "inited": bool(sess and sess.inited),
                "is_open": bool(sess and sess.is_open),
                "running": self.running,
                "seq": self._seq,
                "items": items,
                "counts": _count_states(p),
                # 清单版本号（乐观锁）：前端每次 POST /api/plan 都要原样带回来。
                # 前端据此判断「服务端这份是不是我手上那份」—— 对不上就重新载入，
                # 绝不拿陈旧副本去覆盖（见 PlanConflict）。
                "plan_rev": self._plan_rev,
                # 清单内部互斥的项对（下标）：界面据此标注「先抢到哪个就跳过哪个」。
                # 口径与执行期一致（星期+节次+周次三者都重叠），所以不会出现
                # 「界面说不冲突、执行期却跳过」这种没法解释的情况。
                "mutex": p.conflict_pairs() if p else [],
                # 派发方式（serial / round_robin）。前端据此回填下拉框 ——
                # 它是**落盘的**，刷新页面后界面必须显示服务端真正会用的那个模式，
                # 否则会出现「界面选着轮流发送、实际在跑串行」这种自相矛盾。
                #
                # ⚠️ 这里**必须永远给一个字符串**，清单为空时也不能给 None。
                # 前端同时拿它当「后端支不支持这个开关」的探针
                # （`typeof s.retry_mode === 'string'`），而 `typeof null === 'object'`
                # —— 一旦返回 None，「新后端 + 空清单」（刚启动、还没加载落盘清单）
                # 就会被前端误判成「后端版本较旧」，把下拉禁用并强拨回串行，
                # 然后提示用户去重启一个本来就够新的服务。2026-10-01 在
                # 「新后端 + 真实教务 + 空清单」的隔离实例上实测复现过。
                # 清单不存在时用默认值 RETRY_ROUND_ROBIN —— 这本来也就是新建 Plan 的取值。
                "retry_mode": normalize_retry_mode(p.retry_mode) if p else RETRY_ROUND_ROBIN,
                # 内存这份清单的描述：界面据此说明「这份清单是从本地文件来的」，
                # 以及「这个实例的清单存在哪个文件」——多实例并存时这点很重要。
                "plan_meta": dict(self._plan_meta),
                # **磁盘上**「上次保存的数据」的元信息（只读，不加载）。
                # ⚠️ 内存里已经有清单时它是空的（见 snapshot_meta）。
                # 界面据此在清单区显示一条「上次保存的数据：N 项 …… [加载查看]」——
                # 启动**不再自动恢复**清单，用户想用上次那份得自己点（2026-09-30 用户要求）。
                "snapshot": self.snapshot_meta,
                # **磁盘上**「上次搜到的课程」的摘要（按课程类别分桶）。
                # 未开放期教务查不了课，界面靠它给出「📂 上次数据」按钮，
                # 点一下就把对应类别的离线课程列表拉出来看。
                "courses_snapshot": self.courses_snapshot,
                "clock": clock,
                # 本学期学分要求（最低/最高/已选）+ 清单待加选学分。
                # 「已选 + 待选 > 最高」就是教务驳回的头号原因，所以提前摆出来。
                # 已选是实时值，要最新的走 GET /api/credit?refresh=1。
                "credit": self.credit_snapshot(),
                # 只报「有没有缓存」，不在这里塞全量已选 ——
                # 前端要数据走 /api/selected?cached=1，避免 3 秒轮询反复搬运同一份数据
                "selected": {
                    "loaded": sel is not None,
                    "count": len(sel) if sel else 0,
                    "at": (self._session.selected_at if self._session else 0.0) or None,
                },
                "schedule": {
                    "start_at": p.start_at if p else None,
                    "warmup_s": p.warmup_s if p else None,
                    "fire_lead_s": p.fire_lead_s if p else None,
                    # 服务器当前时刻：前端据此做本地倒计时，不必再问一次后端
                    "server_now": (clock or {}).get("server_now"),
                },
                # ⭐ 蹲课（2026-10-01）：独立清单 + 独立任务。
                #   `wait_items` 结构与 items 同构（蹲课项也是 PlanItem）；
                #   `wait_retry_mode` 恒为 round_robin（蹲课固定轮流发送）；
                #   `wait_interval_ms` = 用户选的间隔档（800 / 1000）；
                #   `wait_start_at` / `wait_deadline` = 开始 / 结束时刻（unix 秒）。
                "wait_running": self.wait_running,
                "wait_items": _wait_items(self._wait_plan),
                "wait_rev": self._wait_rev,
                "wait_retry_mode": normalize_retry_mode(self._wait_plan.retry_mode)
                    if self._wait_plan else RETRY_ROUND_ROBIN,
                "wait_interval_ms": self._wait_plan.items[0].interval_ms
                    if (self._wait_plan and self._wait_plan.items) else 1000,
                "wait_start_at": self._wait_plan.start_at if self._wait_plan else None,
                "wait_deadline_s": self._wait_plan.global_deadline_s if self._wait_plan else 0.0,
                "wait_meta": dict(self._wait_meta),
            }


def _count_states(plan: Plan | None) -> dict:
    base = {s.value: 0 for s in TaskState}
    if not plan:
        return base
    for it in plan.items:
        base[it.state.value] = base.get(it.state.value, 0) + 1
    return base


def _item_summary(it: PlanItem) -> dict:
    """把一项 PlanItem 序列化成给前端的字典（抢课清单与蹲课清单共用）。"""
    return {
        "kch_id": it.kch_id,
        "kcmc": it.kcmc or it.kch_id,
        "jsxx": it.jsxx,
        "xf": it.xf,
        "do_id": it.do_id,
        "jxb_id": it.jxb_id,
        "kklxdm": it.kklxdm,
        "tab_index": it.tab_index,
        "cxbj": it.cxbj,
        "fxbj": it.fxbj,
        "priority": it.priority,
        "interval_ms": it.interval_ms,
        "precheck": it.precheck,
        "budget_s": it.budget_s,
        "slots": [s.as_dict() for s in it.slots],
        "sksj": " / ".join(s.text for s in it.slots),
        "state": it.state.value,
        "attempts": it.attempts,
        "max_attempts": it.max_attempts,
        "last_msg": it.last_msg,
    }


def _wait_items(plan: Plan | None) -> list[dict]:
    if not plan:
        return []
    return [_item_summary(it) for it in plan.sorted_items()]


# 进程内单例
RUNTIME = Runtime()
