"""领域异常类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域错误的基类。"""


class ValidationError(DomainError):
    """输入不满足领域规则，对应 HTTP 400。"""


class NotFoundError(DomainError):
    """对象不存在，对应 HTTP 404。"""


class ConflictError(DomainError):
    """状态冲突或并发冲突，对应 HTTP 409。"""


class InvalidStateError(ConflictError):
    """当前状态不允许该操作。"""


class CycleDetectedError(ConflictError):
    """新增关联会形成循环。"""


class ChainForkError(ConflictError):
    """替代链不允许分叉：只能从最新版本发起替代。"""
