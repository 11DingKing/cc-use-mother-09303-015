"""领域错误类型。

所有违反业务规则的情况都抛出 ``RegistryError`` 的子类，
服务层不依赖 HTTP 状态码，接口层再统一映射。
"""
from __future__ import annotations


class RegistryError(Exception):
    """版本库领域错误基类。"""


class ValidationError(RegistryError):
    """需求内容不完整或字段不合法。"""


class NotFoundError(RegistryError):
    """引用的对象不存在。"""


class StateError(RegistryError):
    """当前状态不允许该操作（历史一致性）。"""


class ConfirmationError(RegistryError):
    """双方确认未齐备或确认方无效。"""


class ImmutableHistoryError(StateError):
    """试图修改已经定格的历史记录。"""


class CycleError(RegistryError):
    """关联会形成循环。"""


class ConflictError(RegistryError):
    """并发冲突：乐观锁失败或唯一活动版本被突破。"""
