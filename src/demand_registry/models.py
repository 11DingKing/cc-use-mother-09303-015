"""领域模型：需求版本、确认、关联与替代链。

设计要点：

* ``DemandVersion`` 一经发布即不可变（immutable）。撤回/更正只新建
  版本并通过 ``supersedes`` 串成替代链，引用方始终指向具体版本。
* 每个版本有单调递增的 ``revision``；同一需求线（``demand_code``）
  在任一时刻至多有一个生效版本。
* 确认需要企业与院校双方独立完成，双方都确认后版本才可发布。
* 失效版本的历史完整保留；新版本只是链上的后继，永不原地改写。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .errors import StateError


def utcnow() -> datetime:
    """统一的时间来源（UTC，感知时区）。"""
    return datetime.now(timezone.utc)


class VersionState(str, enum.Enum):
    """需求版本生命周期。

    状态迁移由服务层守卫，合法迁移见 ``ALLOWED_TRANSITIONS``：

    采集 → 核验 → 待确认 → 已发布 → 已撤回
                       └──────── 已更正（后继版本发布时自动进入）
    已撤回 → 已发布（恢复）；已更正版本存在生效后继，不可恢复。
    """

    DRAFT = "采集"
    VERIFIED = "核验"
    PENDING_CONFIRM = "待确认"
    PUBLISHED = "发布"
    WITHDRAWN = "已撤回"
    CORRECTED = "已更正"


# 合法状态迁移；恢复（失效后恢复）是其中的显式边。
ALLOWED_TRANSITIONS: dict[VersionState, frozenset[VersionState]] = {
    VersionState.DRAFT: frozenset({VersionState.VERIFIED}),
    VersionState.VERIFIED: frozenset({VersionState.PENDING_CONFIRM}),
    VersionState.PENDING_CONFIRM: frozenset({VersionState.PUBLISHED}),
    VersionState.PUBLISHED: frozenset(
        {VersionState.WITHDRAWN, VersionState.CORRECTED}
    ),
    VersionState.WITHDRAWN: frozenset({VersionState.PUBLISHED}),
    VersionState.CORRECTED: frozenset(),
}


class Party(str, enum.Enum):
    """共同确认的两方。"""

    ENTERPRISE = "企业"
    COLLEGE = "院校"


class RelationKind(str, enum.Enum):
    """需求版本与培养方案对象之间的关联类型。"""

    COURSE = "课程"
    TRAINING_PROJECT = "实训项目"
    SKILL_STANDARD = "技能标准"


@dataclass(frozen=True)
class Evidence:
    """证据来源：需求主张必须可追溯。"""

    source: str  # 文件名 / 链接 / 文号
    reference: str  # 章节、条款或页码
    content_hash: str  # 证据内容指纹，防止引用漂移
    captured_at: datetime


@dataclass(frozen=True)
class ApplicableRegion:
    """适用地区。"""

    code: str  # 行政区划或区域编码
    name: str


@dataclass
class DemandVersion:
    """一条企业岗位能力需求的不可变版本（发布后）。"""

    version_id: str
    demand_code: str  # 同一需求线的稳定标识
    revision: int  # 该需求线下单调递增
    title: str
    competency_statement: str  # 岗位能力说明正文
    publisher: str  # 发布方
    enterprise: str  # 提出需求的合作企业
    regions: list[ApplicableRegion]
    evidence: list[Evidence]
    valid_from: datetime
    valid_until: datetime | None  # None 表示长期有效
    state: VersionState
    supersedes_id: str | None  # 替代链上的直接前驱
    version: int  # 乐观锁版本（并发发布守卫）
    created_at: datetime
    published_at: datetime | None = None
    withdrawn_at: datetime | None = None

    @property
    def is_effective(self) -> bool:
        """当前是否生效（状态为已发布）。"""
        return self.state is VersionState.PUBLISHED

    @property
    def is_frozen(self) -> bool:
        """历史是否已定格：进入确认流程后，正文不再允许修改。"""
        return self.state in (
            VersionState.PENDING_CONFIRM,
            VersionState.PUBLISHED,
            VersionState.WITHDRAWN,
            VersionState.CORRECTED,
        )

    def snapshot(self) -> dict[str, Any]:
        """生成可持久化/序列化的稳定快照。"""
        return {
            "version_id": self.version_id,
            "demand_code": self.demand_code,
            "revision": self.revision,
            "title": self.title,
            "competency_statement": self.competency_statement,
            "publisher": self.publisher,
            "enterprise": self.enterprise,
            "regions": [
                {"code": r.code, "name": r.name} for r in self.regions
            ],
            "evidence": [
                {
                    "source": e.source,
                    "reference": e.reference,
                    "content_hash": e.content_hash,
                    "captured_at": e.captured_at.isoformat(),
                }
                for e in self.evidence
            ],
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat()
            if self.valid_until
            else None,
            "state": self.state.value,
            "supersedes_id": self.supersedes_id,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "published_at": self.published_at.isoformat()
            if self.published_at
            else None,
            "withdrawn_at": self.withdrawn_at.isoformat()
            if self.withdrawn_at
            else None,
        }


@dataclass
class Confirmation:
    """一方对某需求版本的确认记录（不可撤销、不可重复）。"""

    version_id: str
    party: Party
    confirmer: str
    confirmed_at: datetime


@dataclass
class ReferenceTarget:
    """培养方案侧被关联的对象（课程/实训项目/技能标准）。

    被关联对象本身也可以引用需求版本，因此关联图可能成环，
    循环检测在服务层进行。
    """

    target_id: str
    kind: RelationKind
    name: str
    version: int = 0


@dataclass
class DemandLink:
    """需求版本与培养方案对象之间的有向关联。"""

    link_id: str
    version_id: str
    target_id: str
    kind: RelationKind
    note: str
    created_at: datetime


@dataclass
class TargetEdge:
    """培养方案对象之间的关联边（如课程依赖实训项目）。

    与 DemandLink 共同构成可能成环的有向图；新增任何边之前，
    服务层统一做循环检测。
    """

    edge_id: str
    from_target_id: str
    to_target_id: str
    created_at: datetime


@dataclass
class ConfirmationAggregate:
    """确认聚合：汇总两方确认并裁定是否可发布。"""

    version_id: str
    parties: dict[Party, Confirmation] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return Party.ENTERPRISE in self.parties and Party.COLLEGE in self.parties

    def add(self, confirmation: Confirmation) -> None:
        if confirmation.party in self.parties:
            raise StateError(f"{confirmation.party.value}方已确认，不能重复确认")
        self.parties[confirmation.party] = confirmation


def bump_version(entity: DemandVersion) -> DemandVersion:
    """乐观锁版本号递增，返回新实例（不就地修改）。"""
    return replace(entity, version=entity.version + 1)
