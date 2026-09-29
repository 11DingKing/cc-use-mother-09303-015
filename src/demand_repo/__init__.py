"""产教需求版本库后端。

核心组件：

* :class:`~demand_repo.models.Evidence` / :class:`~demand_repo.models.DemandVersion`：领域模型
* :class:`~demand_repo.store.EventStore`：SQLite 持久化与追加事件日志
* :class:`~demand_repo.repository.DemandRepository`：版本、确认、替代链、关联与影响分析规则
"""
from .errors import (
    ChainForkError,
    ConflictError,
    CycleDetectedError,
    DomainError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from .models import (
    DemandStatus,
    DemandVersion,
    EdgeKind,
    Evidence,
    NodeKind,
    VersionEdge,
    VersionNode,
)
from .repository import COLLECT, CORRECT, RESTORE, WITHDRAW, DemandRepository
from .store import EventStore

__all__ = [
    "DemandRepository", "EventStore", "Evidence", "DemandVersion",
    "VersionNode", "VersionEdge", "NodeKind", "EdgeKind", "DemandStatus",
    "DomainError", "ValidationError", "NotFoundError", "ConflictError",
    "InvalidStateError", "CycleDetectedError", "ChainForkError",
    "COLLECT", "CORRECT", "WITHDRAW", "RESTORE",
]
