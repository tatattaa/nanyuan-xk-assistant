"""引擎层事件模型。

界面层只消费这些事件，不直接接触核心层的异常与原始响应。
铁律 #10：每个事件都要能回答「为什么没抢到」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.errors import FailureKind


class EventType(str, Enum):
    """事件类型。界面层可据此决定怎么显示。"""

    PLAN_START = "plan_start"          # 开始执行某个计划项
    ATTEMPT = "attempt"                # 一次尝试（含第几次）
    SUCCESS = "success"                # 抢到了
    RETRY_WAIT = "retry_wait"          # 失败，等待重试
    GIVE_UP = "give_up"                # 放弃该项（达上限 / 不可重试）
    NEED_LOGIN = "need_login"          # 登录态失效，需要重新提供凭据
    PLAN_DONE = "plan_done"            # 整个计划结束
    LOG = "log"                        # 普通日志

    # --- 定时开抢专用 ---
    CLOCK = "clock"                    # 时钟校准结果（偏差 / 不确定度）
    PREWARM_START = "prewarm_start"    # 进入预热阶段
    PREWARM_READY = "prewarm_ready"    # 预热完成，全部目标已就绪
    COUNTDOWN = "countdown"            # 倒计时
    FIRE = "fire"                      # 到点开火


class TaskState(str, Enum):
    """单个计划项的最终状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WON = "won"                        # 成功选中
    FAILED = "failed"                  # 达上限放弃
    SKIPPED = "skipped"                # 被跳过（预检不过等）
    ABORTED = "aborted"                # 用户中止


@dataclass
class Event:
    """引擎推给界面的一条事件。"""

    type: EventType
    item_key: str = ""                 # 关联的计划项（kch_id 或 "kch_id/do_id"）
    message: str = ""
    attempt: int = 0
    kind: FailureKind | None = None    # 失败语义（仅失败类事件有）
    elapsed_ms: int = 0
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """给界面层（可能是 JSON 序列化）用的字典表示。"""
        return {
            "type": self.type.value,
            "item_key": self.item_key,
            "message": self.message,
            "attempt": self.attempt,
            "kind": self.kind.value if self.kind else None,
            "elapsed_ms": self.elapsed_ms,
            "extra": self.extra,
        }
