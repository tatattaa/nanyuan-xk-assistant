"""核心层：教务接口的唯一出入口。

四件事（HANDOFF 5.2）：
    get_credential  取凭据
    init            初始化选课上下文
    query           查课程 / 查教学班 / 查已选（只读）
    submit          提交选课 / 退课（有副作用）

铁律：核心层不认识界面。上层（引擎/界面）可以调核心层，反之不行。
"""

from core.client import Course, Jxb, Tab, ZfClient, parse_full_msg, parse_tabs
from core.clock import ClockSample, ServerClock, parse_http_date
from core.config import DEFAULT_SCHOOL, GNMKDM_XK, Credential, SchoolProfile
from core.credit import CreditInfo, parse_credit
from core.credential import (
    CdpCookieFetcher,
    from_cdp,
    from_manual_cookie,
    from_password,
    get_credential,
)
from core.errors import KIND_LABEL, FailureKind, XKError, classify_response, classify_text
from core.drop import DropState, drop_state
from core.http import HttpSession, RawResponse
from core.schedule import (
    ConflictHit,
    ScheduleEntry,
    Slot,
    build_entry,
    clean_text,
    entries_from_rows,
    find_conflicts,
    is_hard_conflict,
    is_soft_conflict,
    is_time_conflict,
    max_jie,
    parse_jxdd,
    parse_sksj,
    parse_teachers,
    slot_from_dict,
    slots_conflict,
    slots_from_dicts,
    teacher_names,
    teacher_titles,
)

__all__ = [
    # client
    "ZfClient",
    "Course",
    "Jxb",
    "Tab",
    "parse_tabs",
    "parse_full_msg",
    # clock
    "ServerClock",
    "ClockSample",
    "parse_http_date",
    # config
    "SchoolProfile",
    "Credential",
    "DEFAULT_SCHOOL",
    "GNMKDM_XK",
    # credential
    "get_credential",
    "from_manual_cookie",
    "from_cdp",
    "from_password",
    "CdpCookieFetcher",
    # errors
    "FailureKind",
    "XKError",
    "KIND_LABEL",
    "classify_response",
    "classify_text",
    # drop（退课资格）
    "DropState",
    "drop_state",
    # credit（本学期学分要求）
    "CreditInfo",
    "parse_credit",
    # http
    "HttpSession",
    "RawResponse",
    # schedule（课表与时间冲突）
    "Slot",
    "ScheduleEntry",
    "ConflictHit",
    "build_entry",
    "entries_from_rows",
    "parse_sksj",
    "parse_jxdd",
    "parse_teachers",
    "teacher_names",
    "teacher_titles",
    "clean_text",
    "find_conflicts",
    "is_hard_conflict",
    "is_soft_conflict",
    "is_time_conflict",
    "slots_conflict",
    "max_jie",
    "slot_from_dict",
    "slots_from_dicts",
]