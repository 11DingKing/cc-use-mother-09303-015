"""领域值对象与实体定义。

状态机与领域契约 ``domain/contract.json`` 对齐：

    采集 DRAFT → 核验 REVIEW → 发布 PUBLISHED → 关联 LINKED → 替代 SUPERSEDED
                       │
                       └→ 驳回 REJECTED（可修订后重新提交核验，仍在采集语义内）

所有实体均为不可变快照；状态变化通过追加事件（见 store.EventStore）表达。
"""
from __future__ import annotations

import dataclasses
import enum
from datetime import date, datetime
from typing import Mapping


class DemandStatus(str, enum.Enum):
    DRAFT = "采集"
    REVIEW = "核验"
    PUBLISHED = "发布"
    LINKED = "关联"
    SUPERSEDED = "替代"
    REJECTED = "驳回"


class NodeKind(str, enum.Enum):
    DEMAND = "demand"          # 需求版本
    COURSE = "course"          # 课程
    TRAINING = "training"      # 实训项目
    STANDARD = "standard"      # 技能标准
    PROGRAM = "program"        # 培养方案（影响分析追溯的顶层容器）


class EdgeKind(str, enum.Enum):
    REFERENCES = "references"      # 课程/实训/标准 → 需求版本（钉住具体版本）
    INCLUDES = "includes"          # 培养方案 → 课程/实训/标准（包含关系）
    RELATES_TO = "relates_to"      # 需求版本之间的一般关联（受循环检测约束）


@dataclasses.dataclass(frozen=True, slots=True)
class Evidence:
    """证据来源：为什么采用这份需求。"""

    source: str            # 来源名称，如企业岗位说明书、行指委文件
    document_ref: str      # 文号 / URL / 文档定位
    issued_on: date | None  # 证据本身的出具日期


@dataclasses.dataclass(frozen=True, slots=True)
class DemandVersion:
    """需求版本：发布后内容不可变。

    每次更正、撤回或失效后恢复都产生新的版本号，旧版本原样保留，
    既有培养方案通过 references 边钉住其当时采用的版本。
    """

    id: str
    family_id: str          # 同一需求的版本族 id（链上各版本共享）
    version_no: int         # 族内从 1 递增
    title: str
    content: str
    region: str             # 适用地区
    publisher: str          # 发布方
    valid_from: date
    valid_to: date | None   # None 表示长期有效
    evidence: Evidence
    status: DemandStatus
    supersedes_id: str | None  # 直接前驱版本 id，构成替代链
    change_kind: str           # collect / correct / withdraw / restore
    reason: str                # 采集/更正/撤回/恢复的理由（为什么采用/变更）
    created_at: datetime


@dataclasses.dataclass(frozen=True, slots=True)
class Confirmation:
    """双方确认记录。发布需要企业与院校两方确认齐备。"""

    demand_version_id: str
    party: str              # "enterprise" | "college"
    confirmer: str          # 确认人/账号
    confirmed_at: datetime


@dataclasses.dataclass(frozen=True, slots=True)
class VersionNode:
    """通用版本节点：课程、实训项目、技能标准、培养方案。"""

    id: str
    kind: NodeKind
    code: str
    title: str
    revision: int
    created_at: datetime


@dataclasses.dataclass(frozen=True, slots=True)
class VersionEdge:
    """节点间有向关联。

    关联本身不可变；解除关联采用追加删除标记（事件溯源），不物理删除，
    以保证历史培养方案可回溯当时的关联结构。
    """

    id: str
    kind: EdgeKind
    src_id: str
    dst_id: str
    created_at: datetime
    active: bool


@dataclasses.dataclass(frozen=True, slots=True)
class Event:
    """追加式审计事件。回答“当时为什么采用那份需求”。"""

    seq: int
    event_type: str
    aggregate_id: str
    actor: str
    at: datetime
    payload: Mapping[str, object]
