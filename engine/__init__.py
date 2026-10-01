"""引擎层：把核心层的原子操作组合成「抢课策略」。

核心层不认识界面；引擎层不认识网络细节，只调用 ZfClient 的 4 个函数。

职责：
    - 重试与降速（铁律 #5：盲提交必须有次数上限）
    - 并发调度（铁律 #6：提交严格串行）
    - 事件流（把每一次尝试的结果推给界面层）
    - 停止条件（选中 / 达到上限 / 用户手动停止）

本层不 import ui.*，只通过回调/事件队列对外说话。
"""

from engine.events import Event, EventType, TaskState
from engine.plan import Plan, PlanItem
from engine.runner import GrabbingRunner

__all__ = [
    "GrabbingRunner",
    "Plan",
    "PlanItem",
    "Event",
    "EventType",
    "TaskState",
]
