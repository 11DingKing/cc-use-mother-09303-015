"""产教需求版本库后端。

对外主要导出：

* :class:`DemandRegistryService` —— 全部用例与一致性不变量
* :class:`InMemoryRepository` / :class:`SqliteRepository` —— 仓储实现
* :mod:`demand_registry.models` —— 领域模型与状态机
"""
from .errors import (
    ConfirmationError,
    ConflictError,
    CycleError,
    ImmutableHistoryError,
    NotFoundError,
    RegistryError,
    StateError,
    ValidationError,
)
from .models import (
    ApplicableRegion,
    Confirmation,
    DemandLink,
    DemandVersion,
    Evidence,
    Party,
    ReferenceTarget,
    RelationKind,
    TargetEdge,
    VersionState,
)
from .repositories import InMemoryRepository, SqliteRepository
from .service import DemandRegistryService

__all__ = [
    "DemandRegistryService",
    "InMemoryRepository",
    "SqliteRepository",
    "DemandVersion",
    "Confirmation",
    "DemandLink",
    "ReferenceTarget",
    "TargetEdge",
    "Evidence",
    "ApplicableRegion",
    "Party",
    "RelationKind",
    "VersionState",
    "RegistryError",
    "ValidationError",
    "StateError",
    "NotFoundError",
    "ConflictError",
    "CycleError",
    "ImmutableHistoryError",
    "ConfirmationError",
]
