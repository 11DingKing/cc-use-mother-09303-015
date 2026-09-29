"""领域服务：需求版本库的全部业务规则。

事务边界：每个写操作一个 ``BEGIN IMMEDIATE`` 事务，业务表与审计事件
原子提交。调用方捕获异常时事务已回滚，存储状态不变。
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime
from typing import Callable, Iterable, Mapping

from .errors import (
    ChainForkError,
    ConflictError,
    CycleDetectedError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from .models import (
    DemandStatus,
    DemandVersion,
    EdgeKind,
    Event,
    Evidence,
    NodeKind,
    VersionEdge,
    VersionNode,
)
from .store import EventStore

# 变更类型：采集 / 更正 / 撤回（墓碑）/ 失效后恢复
COLLECT = "collect"
CORRECT = "correct"
WITHDRAW = "withdraw"
RESTORE = "restore"

_PARTIES = ("enterprise", "college")
_HEAD_STATUSES = (DemandStatus.PUBLISHED, DemandStatus.LINKED)
_DRAFT_STATUSES = (DemandStatus.DRAFT, DemandStatus.REJECTED)
_REFERENCER_KINDS = (NodeKind.COURSE, NodeKind.TRAINING, NodeKind.STANDARD)


class _DemandEndpoint:
    """关联端点为需求版本时的轻量适配（复用节点校验/事件载荷的 .id/.code）。"""

    __slots__ = ("id", "code")

    def __init__(self, v: DemandVersion) -> None:
        self.id = v.id
        self.code = f"DEMAND:{v.family_id[:8]}#v{v.version_no}"


def _new_id() -> str:
    return uuid.uuid4().hex


class DemandRepository:
    def __init__(
        self,
        store: EventStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or datetime.now

    # ================================================================
    # 采集：创建需求版本族（v1，采集态）
    # ================================================================

    def collect_demand(
        self,
        *,
        title: str,
        content: str,
        region: str,
        publisher: str,
        valid_from: date,
        valid_to: date | None,
        evidence: Evidence,
        reason: str,
        actor: str,
    ) -> DemandVersion:
        """采集一份新需求，建立版本族。reason 记录采用理由。"""
        self._validate_fields(title, content, region, publisher, reason,
                              valid_from, valid_to, evidence)
        family_id = _new_id()
        version = DemandVersion(
            id=_new_id(), family_id=family_id, version_no=1,
            title=title.strip(), content=content.strip(), region=region.strip(),
            publisher=publisher.strip(), valid_from=valid_from,
            valid_to=valid_to, evidence=evidence, status=DemandStatus.DRAFT,
            supersedes_id=None, change_kind=COLLECT,
            reason=reason.strip(),
            created_at=self.clock(),
        )
        self._tx(lambda: self._insert_version(version, COLLECT, actor))
        return version

    def revise_draft(self, version_id: str, actor: str, **fields: object) -> DemandVersion:
        """驳回后修订草稿（仅采集/驳回态可改）。"""
        def action() -> DemandVersion:
            v = self._require_version(version_id)
            if v.status not in _DRAFT_STATUSES:
                raise InvalidStateError(f"当前状态 {v.status.value} 不可修订")
            allowed = {
                "title", "content", "region", "publisher", "valid_from",
                "valid_to", "evidence", "reason",
            }
            unknown = set(fields) - allowed
            if unknown:
                raise ValidationError("不可修订字段：" + "、".join(sorted(unknown)))
            merged: dict = {
                "title": v.title, "content": v.content, "region": v.region,
                "publisher": v.publisher, "valid_from": v.valid_from,
                "valid_to": v.valid_to, "evidence": v.evidence,
                "reason": v.reason,
            }
            merged.update(fields)
            self._validate_fields(
                merged["title"], merged["content"], merged["region"],
                merged["publisher"], str(merged["reason"]),
                merged["valid_from"], merged["valid_to"], merged["evidence"])
            self.store.update_draft(version_id, merged)
            self._event("DEMAND_REVISED", v.family_id, actor,
                        {"version_id": v.id, "fields": sorted(fields)})
            return self._require_version(version_id)

        return self._tx(action)

    # ================================================================
    # 核验：提交 / 驳回
    # ================================================================

    def submit_for_review(self, version_id: str, actor: str) -> DemandVersion:
        def action() -> DemandVersion:
            v = self._require_version(version_id)
            if v.status not in _DRAFT_STATUSES:
                raise InvalidStateError(f"当前状态 {v.status.value} 不可提交核验")
            self.store.set_status(v.id, DemandStatus.REVIEW)
            self._event("SUBMITTED_FOR_REVIEW", v.family_id, actor,
                        {"version_id": v.id})
            return self._require_version(version_id)

        return self._tx(action)

    def reject(self, version_id: str, actor: str, reason: str) -> DemandVersion:
        def action() -> DemandVersion:
            v = self._require_version(version_id)
            if v.status != DemandStatus.REVIEW:
                raise InvalidStateError(f"当前状态 {v.status.value} 不可驳回")
            self.store.set_status(v.id, DemandStatus.REJECTED)
            # 释放对前驱版本的替代意图：被驳回的后继不进入发布链，
            # 允许重新发起替代；事实仍完整保留在事件日志中。
            self.store.set_supersedes(v.id, None)
            self.store.clear_confirmations(v.id)
            self._event("REVIEW_REJECTED", v.family_id, actor,
                        {"version_id": v.id, "reason": reason})
            return self._require_version(version_id)

        return self._tx(action)

    def abandon_draft(self, version_id: str, actor: str,
                      reason: str = "") -> DemandVersion:
        """放弃从未发布的后继草稿（含更正/撤回/恢复草稿），释放链占位。"""
        def action() -> DemandVersion:
            v = self._require_version(version_id)
            if v.status not in (DemandStatus.DRAFT, DemandStatus.REVIEW,
                                DemandStatus.REJECTED):
                raise InvalidStateError("已发布版本不可放弃")
            self.store.set_status(v.id, DemandStatus.REJECTED)
            self.store.set_supersedes(v.id, None)
            self.store.clear_confirmations(v.id)
            self._event("DEMAND_ABANDONED", v.family_id, actor,
                        {"version_id": v.id, "reason": reason})
            return self._require_version(version_id)

        return self._tx(action)

    # ================================================================
    # 双方确认与发布
    # ================================================================

    def confirm(self, version_id: str, party: str, confirmer: str) -> bool:
        """企业或院校确认。同方重复确认幂等，返回是否新增。"""
        if party not in _PARTIES:
            raise ValidationError("确认方必须是 enterprise 或 college")
        if not confirmer or not confirmer.strip():
            raise ValidationError("确认人不能为空")

        def action() -> bool:
            v = self._require_version(version_id)
            if v.status != DemandStatus.REVIEW:
                raise InvalidStateError("仅核验中的版本可确认")
            added = self.store.add_confirmation(
                v.id, party, confirmer.strip(), self.clock())
            if added:
                self._event("CONFIRMED", v.family_id, confirmer.strip(),
                            {"version_id": v.id, "party": party})
            return added

        return self._tx(action)

    def publish(self, version_id: str, actor: str) -> DemandVersion:
        """双方确认齐备后发布；并发发布只有一个成功。"""
        def action() -> DemandVersion:
            v = self._require_version(version_id)
            if v.status in (DemandStatus.PUBLISHED, DemandStatus.LINKED,
                            DemandStatus.SUPERSEDED):
                raise InvalidStateError(f"版本已发布（{v.status.value}）")
            if v.status != DemandStatus.REVIEW:
                raise InvalidStateError("仅核验通过的版本可发布")
            if self.store.count_confirmations(v.id) < 2:
                raise ConflictError("缺少企业或院校确认，不可发布")

            head = self._family_head(v.family_id, lock=True)
            if v.supersedes_id is None:
                if head is not None:
                    # 族内已有生效版本：初版不得再次发布
                    raise ChainForkError("版本族已存在生效版本")
            else:
                if head is None or head.id != v.supersedes_id:
                    # 并发更正 / 链已推进：拒绝分叉
                    raise ChainForkError(
                        "前驱版本已不是最新版本，替代链不允许分叉")
                predecessor = self._require_version(v.supersedes_id)

            self.store.set_status(v.id, DemandStatus.PUBLISHED)
            if v.supersedes_id is not None:
                self.store.set_status(v.supersedes_id, DemandStatus.SUPERSEDED)
            self._event("DEMAND_PUBLISHED", v.family_id, actor, {
                "version_id": v.id,
                "version_no": v.version_no,
                "supersedes_id": v.supersedes_id,
                "change_kind": v.change_kind,
            })
            if v.supersedes_id is not None:
                self._event("VERSION_SUPERSEDED", v.family_id, actor, {
                    "version_id": v.supersedes_id,
                    "successor_id": v.id,
                })
            return self._require_version(version_id)

        return self._tx(action)

    # ================================================================
    # 替代链：更正 / 撤回 / 失效后恢复
    # ================================================================

    def correct(self, predecessor_id: str, actor: str, reason: str,
                **fields: object) -> DemandVersion:
        """更正：基于最新生效版本产生后继草稿，内容可改。"""
        return self._spawn_successor(
            predecessor_id, actor, reason, CORRECT, fields)

    def withdraw(self, predecessor_id: str, actor: str,
                 reason: str) -> DemandVersion:
        """撤回：产生墓碑后继草稿；发布后该版本族无生效需求。"""
        v = self._spawn_successor(
            predecessor_id, actor, reason, WITHDRAW,
            {"content": "", "title": None})
        return v

    def restore(self, predecessor_id: str, actor: str, reason: str,
                **fields: object) -> DemandVersion:
        """失效（到期或撤回）后恢复：复制最近实质版本内容产生后继草稿。"""
        return self._spawn_successor(
            predecessor_id, actor, reason, RESTORE, fields)

    def _spawn_successor(self, predecessor_id: str, actor: str, reason: str,
                         change_kind: str,
                         overrides: Mapping[str, object]) -> DemandVersion:
        if not reason or not reason.strip():
            raise ValidationError("变更理由不能为空")

        def action() -> DemandVersion:
            pred = self._require_version(predecessor_id)
            head = self._family_head(pred.family_id, lock=True)
            if head is None or head.id != pred.id:
                raise ChainForkError("只能从最新生效版本发起替代")
            if change_kind == CORRECT and pred.change_kind == WITHDRAW:
                raise InvalidStateError("撤回版本只能恢复，不能直接更正")
            # 不得存在进行中的后继草稿（采集/核验态），保证链线性
            pending = [
                x for x in self.store.list_family(pred.family_id)
                if x.supersedes_id == pred.id
                and x.status in (DemandStatus.DRAFT, DemandStatus.REVIEW)
            ]
            if pending:
                raise ConflictError("存在进行中的替代版本，不可重复发起")

            version_no = self.store.latest_version_no(pred.family_id) + 1
            base_title = pred.title
            base_content = pred.content
            base_evidence = pred.evidence
            base_valid_from = self.clock().date()
            base_valid_to = pred.valid_to
            if change_kind == RESTORE:
                source = self._last_substantive(pred.family_id)
                base_title = source.title
                base_content = source.content
                base_evidence = source.evidence
                base_valid_to = source.valid_to

            title = str(overrides.get("title") or base_title)
            content = (str(overrides["content"]) if "content" in overrides
                       and overrides["content"] is not None else base_content)
            evidence = overrides.get("evidence", base_evidence)
            assert isinstance(evidence, Evidence)
            valid_from = overrides.get("valid_from", base_valid_from)
            valid_to = overrides.get("valid_to", base_valid_to)
            region = str(overrides.get("region", pred.region))
            publisher = str(overrides.get("publisher", pred.publisher))

            if change_kind != WITHDRAW:
                self._validate_fields(title, content, region, publisher,
                                      reason, valid_from, valid_to, evidence)
            successor = DemandVersion(
                id=_new_id(), family_id=pred.family_id, version_no=version_no,
                title=title.strip(), content=content.strip(),
                region=region.strip(), publisher=publisher.strip(),
                valid_from=valid_from, valid_to=valid_to, evidence=evidence,
                status=DemandStatus.DRAFT, supersedes_id=pred.id,
                reason=reason.strip(), created_at=self.clock(),
                change_kind=change_kind,
            )
            self._insert_version(successor, change_kind, actor)
            return successor

        return self._tx(action)

    def _last_substantive(self, family_id: str) -> DemandVersion:
        """沿替代链回溯，取最近的非撤回墓碑版本。"""
        versions = self.store.list_family(family_id)
        for v in reversed(versions):
            if v.change_kind != WITHDRAW and v.content:
                return v
        raise ConflictError("版本族无可恢复的实质内容")

    # ================================================================
    # 关联：节点（课程/实训/标准/培养方案）与有向边
    # ================================================================

    def create_node(self, kind: NodeKind, code: str, title: str,
                    actor: str) -> VersionNode:
        if not code or not code.strip():
            raise ValidationError("节点编码不能为空")
        if not title or not title.strip():
            raise ValidationError("节点名称不能为空")
        node = VersionNode(
            id=_new_id(), kind=kind, code=code.strip(), title=title.strip(),
            revision=1, created_at=self.clock())

        def action() -> VersionNode:
            self.store.insert_node(node)
            self._event("NODE_CREATED", node.id, actor,
                        {"kind": kind.value, "code": node.code})
            return node

        return self._tx(action)

    def link(self, src_id: str, dst_id: str, kind: EdgeKind,
             actor: str) -> VersionEdge:
        """建立有向关联；成环（含自环）拒绝；重复活动关联冲突。"""
        def action() -> VersionEdge:
            if kind == EdgeKind.INCLUDES:
                src = self.store.get_node(src_id)
                dst = self.store.get_node(dst_id)
            elif kind == EdgeKind.RELATES_TO:
                src_d = self.store.get_demand(src_id)
                dst_d = self.store.get_demand(dst_id)
                src = None if src_d is None else _DemandEndpoint(src_d)
                dst = None if dst_d is None else _DemandEndpoint(dst_d)
            else:  # REFERENCES：源是节点，目标是需求版本
                src = self.store.get_node(src_id)
                dst_d = self.store.get_demand(dst_id)
                dst = None if dst_d is None else _DemandEndpoint(dst_d)
            if src is None or dst is None:
                raise NotFoundError("关联端点不存在")
            self._check_edge_rules(kind, src, dst)
            if src_id == dst_id:
                raise CycleDetectedError("节点不能关联自身")
            if self.store.active_edge_exists(kind, src_id, dst_id):
                raise ConflictError("关联已存在")
            # 循环检测：在全量活动边构成的有向图上，若已存在 dst→src 路径，
            # 新增 src→dst 即成环。
            if self._reaches(dst_id, src_id, {kind}):
                raise CycleDetectedError(
                    f"新增关联 {src.code}→{dst.code} 将形成循环")
            edge = VersionEdge(
                id=_new_id(), kind=kind, src_id=src_id, dst_id=dst_id,
                created_at=self.clock(), active=True)
            # 若曾解除过，重新激活原边而不是插入（唯一约束）
            reactivated = self.store.set_edge_active(kind, src_id, dst_id, True)
            if not reactivated:
                self.store.insert_edge(edge)
            if kind == EdgeKind.REFERENCES:
                target = self._require_version(dst_id)
                if target.status == DemandStatus.PUBLISHED:
                    self.store.set_status(dst_id, DemandStatus.LINKED)
                self._event("EDGE_LINKED", target.family_id, actor,
                            self._edge_payload(edge, src, dst))
            else:
                self._event("EDGE_LINKED", src_id, actor,
                            self._edge_payload(edge, src, dst))
            return edge

        return self._tx(action)

    def unlink(self, src_id: str, dst_id: str, kind: EdgeKind,
               actor: str) -> None:
        """解除关联：追加删除标记，不物理删除，历史结构可回溯。"""
        def action() -> None:
            changed = self.store.set_edge_active(kind, src_id, dst_id, False)
            if not changed:
                raise NotFoundError("关联不存在或已解除")
            payload = {"kind": kind.value, "src_id": src_id,
                       "dst_id": dst_id}
            if kind == EdgeKind.REFERENCES:
                target = self.store.get_demand(dst_id)
                aggregate = target.family_id if target is not None else dst_id
            else:
                aggregate = src_id
            self._event("EDGE_UNLINKED", aggregate, actor, payload)

        self._tx(action)

    def _check_edge_rules(self, kind: EdgeKind, src: VersionNode,
                          dst: VersionNode) -> None:
        if kind == EdgeKind.REFERENCES:
            if src.kind not in _REFERENCER_KINDS:
                raise ValidationError("只有课程/实训/标准可引用需求版本")
            target = self.store.get_demand(dst.id)
            if target is None:
                raise ValidationError("引用目标必须是需求版本")
            if target.status not in (DemandStatus.PUBLISHED, DemandStatus.LINKED):
                raise InvalidStateError("只能引用已发布且未被替代的版本")
            if target.change_kind == WITHDRAW:
                raise InvalidStateError("不能引用已撤回（墓碑）版本")
            if target.valid_to is not None and target.valid_to < self.clock().date():
                raise InvalidStateError("不能引用已过有效期的版本")
        elif kind == EdgeKind.INCLUDES:
            if src.kind != NodeKind.PROGRAM:
                raise ValidationError("只有培养方案可以包含课程/实训/标准")
            if dst.kind not in _REFERENCER_KINDS:
                raise ValidationError("培养方案只能包含课程/实训/标准")
        elif kind == EdgeKind.RELATES_TO:
            if self.store.get_demand(src.id) is None or \
                    self.store.get_demand(dst.id) is None:
                raise ValidationError("需求关联的两端必须是需求版本")

    def _reaches(self, start: str, target: str, kinds: set[EdgeKind]) -> bool:
        """活动有向图上 start 是否可达 target（DFS）。"""
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in self.store.list_edges(active_only=True):
            if edge.kind in kinds:
                adjacency[edge.src_id].append(edge.dst_id)
        stack = [start]
        seen = {start}
        while stack:
            node = stack.pop()
            if node == target:
                return True
            for nxt in adjacency.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return False

    # ================================================================
    # 影响分析
    # ================================================================

    def impact_analysis(self, family_id: str | None = None,
                        as_of: date | None = None) -> dict:
        """列出钉住过期版本的课程/实训/标准，及其所属培养方案。

        过期判定：版本已被替代（SUPERSEDED）、为撤回墓碑、或超过有效期。
        既有引用保持钉住原版本（不自动迁移），由此得到“未来调整范围”。
        """
        as_of = as_of or self.clock().date()

        stale: dict[str, dict] = {}
        for edge in self.store.list_edges(EdgeKind.REFERENCES, active_only=True):
            v = self.store.get_demand(edge.dst_id)
            if v is None:
                continue
            if family_id is not None and v.family_id != family_id:
                continue
            reasons = self._stale_reasons(v, as_of)
            if not reasons:
                continue
            item = stale.setdefault(v.id, {
                "version_id": v.id, "family_id": v.family_id,
                "version_no": v.version_no, "reasons": reasons,
                "referenced_by": [],
            })
            node = self.store.get_node(edge.src_id)
            if node is not None:
                item["referenced_by"].append(self._node_dict(node))

        # 反向包含闭包：培养方案 → 包含 → 课程/实训/标准
        includes = self.store.list_edges(EdgeKind.INCLUDES, active_only=True)
        parents: dict[str, list[str]] = defaultdict(list)
        for e in includes:
            parents[e.dst_id].append(e.src_id)

        families_out = []
        for fid in sorted({item["family_id"] for item in stale.values()}):
            head = self._family_head(fid)
            nodes_to_adjust: dict[str, dict] = {}
            items = [it for it in stale.values() if it["family_id"] == fid]
            for item in items:
                programs: dict[str, dict] = {}
                for ref in item["referenced_by"]:
                    nodes_to_adjust.setdefault(ref["id"], ref)
                    for pid in self._ancestors(ref["id"], parents):
                        pnode = self.store.get_node(pid)
                        if pnode is not None:
                            programs.setdefault(pid, self._node_dict(pnode))
                item["affected_programs"] = sorted(programs.values(),
                                                   key=lambda x: x["code"])
                item["referenced_by"].sort(key=lambda x: x["code"])
            families_out.append({
                "family_id": fid,
                "current_head": None if head is None
                else ("withdrawn" if head.change_kind == WITHDRAW
                      else head.id),
                "current_version_no": None if head is None
                else head.version_no,
                "stale_references": sorted(items,
                                           key=lambda x: x["version_no"]),
                "nodes_to_adjust": sorted(nodes_to_adjust.values(),
                                          key=lambda x: x["code"]),
            })
        return {"as_of": as_of.isoformat(), "families": families_out}

    @staticmethod
    def _stale_reasons(v: DemandVersion, as_of: date) -> list[str]:
        reasons: list[str] = []
        if v.status == DemandStatus.SUPERSEDED:
            reasons.append("superseded")
        if v.change_kind == WITHDRAW and v.status in _HEAD_STATUSES:
            reasons.append("withdrawn")
        if v.valid_to is not None and v.valid_to < as_of:
            reasons.append("expired")
        return reasons

    @staticmethod
    def _ancestors(node_id: str,
                   parents: Mapping[str, list[str]]) -> list[str]:
        result: list[str] = []
        stack = list(parents.get(node_id, ()))
        seen: set[str] = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            result.append(pid)
            stack.extend(parents.get(pid, ()))
        return result

    # ================================================================
    # 查询与审计历史
    # ================================================================

    def get_version(self, version_id: str) -> DemandVersion:
        return self._require_version(version_id)

    def list_family(self, family_id: str) -> list[DemandVersion]:
        return self.store.list_family(family_id)

    def family_chain(self, family_id: str) -> list[DemandVersion]:
        """按替代顺序返回已发布链（v1 → … → 当前版本）。"""
        versions = [v for v in self.store.list_family(family_id)
                    if v.status in (DemandStatus.PUBLISHED, DemandStatus.LINKED,
                                    DemandStatus.SUPERSEDED)]
        by_pred = {v.supersedes_id: v for v in versions if v.supersedes_id}
        first = next((v for v in versions if v.supersedes_id is None), None)
        chain: list[DemandVersion] = []
        cur = first
        while cur is not None:
            chain.append(cur)
            cur = by_pred.get(cur.id)
        return chain

    def history(self, aggregate_id: str | None = None) -> list[Event]:
        """审计时间线。按版本族查询即可回答“当时为什么采用这份需求”。"""
        return self.store.list_events(aggregate_id)

    # ---- 内部辅助 ----------------------------------------------------

    def _family_head(self, family_id: str,
                     lock: bool = False) -> DemandVersion | None:
        versions = self.store.list_family(family_id)
        heads = [v for v in versions if v.status in _HEAD_STATUSES]
        if not heads:
            return None
        return max(heads, key=lambda v: v.version_no)

    def _insert_version(self, v: DemandVersion, change_kind: str,
                        actor: str) -> None:
        self.store.insert_demand(v)
        self._event("DEMAND_COLLECTED" if change_kind == COLLECT
                    else "SUCCESSOR_DRAFTED", v.family_id, actor, {
                        "version_id": v.id,
                        "family_id": v.family_id,
                        "version_no": v.version_no,
                        "supersedes_id": v.supersedes_id,
                        "change_kind": change_kind,
                        "title": v.title,
                        "region": v.region,
                        "publisher": v.publisher,
                        "valid_from": v.valid_from.isoformat(),
                        "valid_to": None if v.valid_to is None
                        else v.valid_to.isoformat(),
                        "evidence_source": v.evidence.source,
                        "evidence_ref": v.evidence.document_ref,
                        "reason": v.reason,
                    })

    @staticmethod
    def _node_dict(node: VersionNode) -> dict:
        return {"id": node.id, "kind": node.kind.value, "code": node.code,
                "title": node.title, "revision": node.revision}

    @staticmethod
    def _edge_payload(edge: VersionEdge, src: VersionNode,
                      dst: VersionNode) -> dict:
        return {"kind": edge.kind.value, "src_id": src.id, "dst_id": dst.id,
                "src_code": src.code, "dst_code": dst.code}

    def _event(self, event_type: str, aggregate_id: str, actor: str,
               payload: Mapping[str, object]) -> None:
        self.store.append_event(event_type, aggregate_id, actor,
                                self.clock(), payload)

    def _require_version(self, version_id: str) -> DemandVersion:
        v = self.store.get_demand(version_id)
        if v is None:
            raise NotFoundError(f"需求版本不存在：{version_id}")
        return v

    @staticmethod
    def _validate_fields(title: object, content: object, region: object,
                         publisher: object, reason: object,
                         valid_from: object, valid_to: object,
                         evidence: object) -> None:
        for name, value in (("title", title), ("content", content),
                            ("region", region), ("publisher", publisher),
                            ("reason", reason)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{name} 不能为空")
        if not isinstance(valid_from, date):
            raise ValidationError("valid_from 必须是日期")
        if valid_to is not None:
            if not isinstance(valid_to, date):
                raise ValidationError("valid_to 必须是日期或为空")
            if valid_to < valid_from:
                raise ValidationError("有效期结束不得早于开始")
        if not isinstance(evidence, Evidence):
            raise ValidationError("证据来源缺失")
        if not evidence.source.strip() or not evidence.document_ref.strip():
            raise ValidationError("证据来源与文号不能为空")

    def _tx(self, action: Callable[[], object]):
        self.store.begin()
        try:
            result = action()
        except Exception:
            self.store.rollback()
            raise
        else:
            self.store.commit()
            return result
