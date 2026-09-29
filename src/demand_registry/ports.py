"""仓储端口：领域服务只依赖这些接口，不依赖具体存储。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Protocol

from .models import (
    Confirmation,
    DemandLink,
    DemandVersion,
    ReferenceTarget,
    TargetEdge,
)


class Repository(Protocol):
    """版本库所需的全部持久化操作。"""

    # ---- 事务与并发 ----
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """串行化事务：事务内的一致性检查与写入必须原子完成。"""
        ...

    # ---- 需求版本 ----
    def next_revision(self, demand_code: str) -> int:
        """返回该需求线的下一个版本号（事务内调用）。"""
        ...

    def save_version(self, version: DemandVersion, expected_version: int | None = None) -> None:
        """插入或按乐观锁更新版本；expected_version 不匹配则冲突。"""
        ...

    def get_version(self, version_id: str) -> DemandVersion:
        """按 ID 取版本，不存在抛 NotFoundError。"""
        ...

    def list_versions(self, demand_code: str | None = None) -> list[DemandVersion]:
        """列出版本（可按需求线过滤），按创建时间升序。"""
        ...

    def find_active_by_demand_code(self, demand_code: str) -> DemandVersion | None:
        """取需求线当前唯一的生效版本，没有则 None。"""
        ...

    def find_successor(self, version_id: str) -> DemandVersion | None:
        """取直接后继（supersedes 指向该版本的版本）。"""
        ...

    # ---- 双方确认 ----
    def add_confirmation(self, confirmation: Confirmation) -> None: ...

    def get_confirmations(self, version_id: str) -> list[Confirmation]: ...

    # ---- 培养方案对象与关联 ----
    def save_target(self, target: ReferenceTarget) -> None: ...

    def get_target(self, target_id: str) -> ReferenceTarget: ...

    def list_targets(self) -> list[ReferenceTarget]: ...

    def add_demand_link(self, link: DemandLink) -> None: ...

    def demand_links_for_version(self, version_id: str) -> list[DemandLink]: ...

    def demand_links_for_target(self, target_id: str) -> list[DemandLink]: ...

    def add_target_edge(self, edge: TargetEdge) -> None: ...

    def target_edges(self) -> list[TargetEdge]: ...
