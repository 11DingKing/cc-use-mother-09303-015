"""应用服务：版本库全部用例与一致性不变量。

关键不变量：

1. 需求版本：正文随版本冻结，修订只能新建版本（revision 单调递增）。
2. 双方确认：企业与院校各自独立确认，齐备后才可发布。
3. 并发发布：乐观锁 + 事务内"唯一生效版本"复查。
4. 替代链：撤回/更正不删除、不改写旧版本；后继经 ``supersedes_id``
   串联，既有培养方案仍稳定引用原版本。
5. 关联无环：新增任意关联边之前做循环检测。
6. 失效后恢复：仅"已撤回"可恢复；"已更正"版本因生效后继存在
   不可恢复，避免一条需求线同时出现两个生效版本。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable, Iterable

from .errors import (
    ConflictError,
    CycleError,
    ImmutableHistoryError,
    NotFoundError,
    StateError,
    ValidationError,
)
from .models import (
    ALLOWED_TRANSITIONS,
    ApplicableRegion,
    Confirmation,
    ConfirmationAggregate,
    DemandLink,
    DemandVersion,
    Evidence,
    Party,
    ReferenceTarget,
    RelationKind,
    TargetEdge,
    VersionState,
    bump_version,
    utcnow,
)
from .ports import Repository


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


class DemandRegistryService:
    """版本库用例编排。所有写操作在单个串行化事务内完成。"""

    def __init__(self, repo: Repository):
        self.repo = repo

    # ============ 需求采集与核验 ============

    def register_demand(self, data: dict[str, Any]) -> DemandVersion:
        """合作企业采集一条新需求，生成需求线的第 1 版（采集态）。"""
        now = utcnow()
        with self.repo.transaction():
            demand_code = str(data.get("demand_code") or "").strip()
            if not demand_code:
                raise ValidationError("demand_code 不能为空")
            revision = self.repo.next_revision(demand_code)
            version = DemandVersion(
                version_id=_new_id("ver"),
                demand_code=demand_code,
                revision=revision,
                title=self._require_text(data, "title"),
                competency_statement=self._require_text(data, "competency_statement"),
                publisher=self._require_text(data, "publisher"),
                enterprise=self._require_text(data, "enterprise"),
                regions=self._parse_regions(data.get("regions")),
                evidence=self._parse_evidence(data.get("evidence"), now),
                valid_from=_parse_dt(data.get("valid_from")) or now,
                valid_until=_parse_dt(data.get("valid_until")),
                state=VersionState.DRAFT,
                supersedes_id=None,
                version=1,
                created_at=now,
            )
            self._validate_window(version)
            self.repo.save_version(version)
            return version

    def update_draft(self, version_id: str, patch: dict[str, Any]) -> DemandVersion:
        """修改采集态草稿；进入核验后历史冻结，拒绝修改。"""
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            if version.state is not VersionState.DRAFT:
                raise ImmutableHistoryError(
                    f"版本当前为{version.state.value}态，正文已冻结，"
                    "请通过更正生成新版本"
                )
            data = version.snapshot()
            data.update(patch)
            updated = DemandVersion(
                version_id=version.version_id,
                demand_code=version.demand_code,
                revision=version.revision,
                title=data["title"],
                competency_statement=data["competency_statement"],
                publisher=data["publisher"],
                enterprise=data["enterprise"],
                regions=self._parse_regions(data["regions"]),
                evidence=self._parse_evidence(
                    data["evidence"], version.created_at
                ),
                valid_from=_parse_dt(data["valid_from"]),
                valid_until=_parse_dt(data["valid_until"]),
                state=version.state,
                supersedes_id=version.supersedes_id,
                version=version.version,
                created_at=version.created_at,
            )
            self._validate_window(updated)
            self.repo.save_version(updated, expected_version=version.version)
            return updated

    def verify(self, version_id: str) -> DemandVersion:
        """采集 → 核验。"""
        return self._transition(
            version_id,
            VersionState.VERIFIED,
            guard=lambda v: self._validate_content(v),
        )

    def request_confirmation(self, version_id: str) -> DemandVersion:
        """核验 → 待确认，随后等待双方确认。"""
        return self._transition(version_id, VersionState.PENDING_CONFIRM)

    # ============ 双方确认与发布 ============

    def confirm(
        self, version_id: str, party: str | Party, confirmer: str
    ) -> Confirmation:
        """企业或院校独立确认；重复确认同一方被拒绝。"""
        party = Party(party) if isinstance(party, str) else party
        if not confirmer or not confirmer.strip():
            raise ValidationError("确认人不能为空")
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            if version.state is not VersionState.PENDING_CONFIRM:
                raise StateError(
                    f"只有待确认版本可确认，当前为{version.state.value}态"
                )
            aggregate = self._load_aggregate(version_id)
            confirmation = Confirmation(
                version_id=version_id,
                party=party,
                confirmer=confirmer.strip(),
                confirmed_at=utcnow(),
            )
            aggregate.add(confirmation)  # 重复确认在此抛 StateError
            self.repo.add_confirmation(confirmation)
            return confirmation

    def publish(
        self, version_id: str, expected_version: int | None = None
    ) -> DemandVersion:
        """双方确认齐备后发布；并发发布由乐观锁与唯一生效复查守卫。

        若发布的是更正后继版本，则在同一事务内把仍生效的前驱
        置为"已更正"，保证替代链切换原子完成。
        """
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            if expected_version is not None and version.version != expected_version:
                raise ConflictError(
                    f"版本已被其他操作更新（期望修订标记 {expected_version}，"
                    f"当前 {version.version}），请刷新后重试"
                )
            if version.state is not VersionState.PENDING_CONFIRM:
                raise StateError(
                    f"只有待确认版本可发布，当前为{version.state.value}态"
                )
            aggregate = self._load_aggregate(version_id)
            if not aggregate.is_complete:
                missing = [
                    p.value
                    for p in Party
                    if p not in aggregate.parties
                ]
                raise StateError("双方确认尚未齐备，缺少：" + "、".join(missing))

            # 唯一生效版本复查（事务内的权威检查，防止并发双发布）。
            active = self.repo.find_active_by_demand_code(version.demand_code)
            predecessor = (
                self.repo.get_version(version.supersedes_id)
                if version.supersedes_id
                else None
            )
            if active is not None and active.version_id != version_id:
                if predecessor is not None and active.version_id == predecessor.version_id:
                    pass  # 即前驱，下面随后继发布原子切换
                else:
                    raise ConflictError(
                        f"需求线 {version.demand_code} 已存在生效版本 "
                        f"{active.version_id}，不能并发发布"
                    )

            now = utcnow()
            if predecessor is not None and predecessor.state is VersionState.PUBLISHED:
                predecessor = bump_version(predecessor)
                predecessor.state = VersionState.CORRECTED
                self.repo.save_version(
                    predecessor, expected_version=predecessor.version - 1
                )

            published = bump_version(version)
            published.state = VersionState.PUBLISHED
            published.published_at = now
            self.repo.save_version(published, expected_version=version.version)
            return published

    # ============ 撤回 / 更正 / 恢复（替代链） ============

    def withdraw(self, version_id: str, reason: str = "") -> DemandVersion:
        """生效版本撤回；引用与历史全部保留，只改变状态。"""
        if not reason.strip():
            raise ValidationError("撤回必须说明原因")
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            withdrawn = self._transition_locked(
                version, VersionState.WITHDRAWN
            )
            withdrawn.withdrawn_at = utcnow()
            self.repo.save_version(
                withdrawn, expected_version=withdrawn.version - 1
            )
            return withdrawn

    def correct(
        self, version_id: str, patch: dict[str, Any] | None = None
    ) -> DemandVersion:
        """对生效版本发起更正：不改动旧版本，新建后继草稿串入替代链。

        后继发布前，前驱继续生效；后继发布时前驱在同一事务内
        自动进入"已更正"。
        """
        patch = patch or {}
        with self.repo.transaction():
            predecessor = self.repo.get_version(version_id)
            if predecessor.state is not VersionState.PUBLISHED:
                raise StateError(
                    "只能更正生效版本；失效版本请走恢复或重新采集，"
                    f"当前为{predecessor.state.value}态"
                )
            existing_successor = self.repo.find_successor(version_id)
            if existing_successor is not None:
                raise ConflictError(
                    "该版本已存在更正后继 "
                    f"{existing_successor.version_id}（{existing_successor.state.value}），"
                    "替代链必须保持线性，请先完成或撤回该后继"
                )
            base = predecessor.snapshot()
            base.update({k: v for k, v in patch.items() if v is not None})
            now = utcnow()
            successor = DemandVersion(
                version_id=_new_id("ver"),
                demand_code=predecessor.demand_code,
                revision=self.repo.next_revision(predecessor.demand_code),
                title=base["title"],
                competency_statement=base["competency_statement"],
                publisher=base["publisher"],
                enterprise=base["enterprise"],
                regions=self._parse_regions(base["regions"]),
                evidence=self._parse_evidence(base["evidence"], now),
                valid_from=_parse_dt(base.get("valid_from")) or now,
                valid_until=_parse_dt(base.get("valid_until")),
                state=VersionState.DRAFT,
                supersedes_id=predecessor.version_id,
                version=1,
                created_at=now,
            )
            self._validate_window(successor)
            self.repo.save_version(successor)
            return successor

    def restore(self, version_id: str) -> DemandVersion:
        """失效后恢复：仅已撤回版本可恢复，已更正版本不可恢复。"""
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            if version.state is VersionState.CORRECTED:
                raise StateError(
                    "已更正版本存在生效后继，恢复会造成同一需求线两个生效版本，"
                    "历史一致性不允许"
                )
            successor = self.repo.find_successor(version_id)
            if successor is not None and successor.is_effective:
                raise StateError(
                    f"替代链下游存在生效版本 {successor.version_id}，不能恢复"
                )
            active = self.repo.find_active_by_demand_code(version.demand_code)
            if active is not None:
                raise ConflictError(
                    f"需求线 {version.demand_code} 已存在生效版本 "
                    f"{active.version_id}，不能恢复"
                )
            restored = self._transition_locked(version, VersionState.PUBLISHED)
            self.repo.save_version(
                restored, expected_version=restored.version - 1
            )
            return restored

    # ============ 培养方案对象与关联 ============

    def register_target(self, kind: str | RelationKind, name: str) -> ReferenceTarget:
        """登记课程 / 实训项目 / 技能标准。"""
        kind = RelationKind(kind) if isinstance(kind, str) else kind
        if not name or not name.strip():
            raise ValidationError("对象名称不能为空")
        target = ReferenceTarget(
            target_id=_new_id("tgt"),
            kind=kind,
            name=name.strip(),
        )
        with self.repo.transaction():
            self.repo.save_target(target)
            return target

    def link_demand(
        self,
        version_id: str,
        target_id: str,
        kind: str | RelationKind,
        note: str = "",
    ) -> DemandLink:
        """把需求版本关联到课程/实训项目/技能标准，拒绝成环。"""
        kind = RelationKind(kind) if isinstance(kind, str) else kind
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            target = self.repo.get_target(target_id)
            if kind is not target.kind:
                raise ValidationError(
                    f"关联类型 {kind.value} 与对象类型 {target.kind.value} 不一致"
                )
            if not version.is_effective:
                raise StateError(
                    "只有生效版本可建立培养方案关联，"
                    f"当前为{version.state.value}态"
                )
            existing = {
                (l.version_id, l.target_id)
                for l in self.repo.demand_links_for_target(target_id)
            }
            if (version_id, target_id) in existing:
                raise ConflictError("该关联已存在")
            link = DemandLink(
                link_id=_new_id("lnk"),
                version_id=version_id,
                target_id=target_id,
                kind=kind,
                note=note,
                created_at=utcnow(),
            )
            self._assert_no_cycle(
                new_edge=(version_id, target_id),
                extra_nodes=[version_id, target_id],
            )
            self.repo.add_demand_link(link)
            return link

    def add_target_edge(self, from_target_id: str, to_target_id: str) -> TargetEdge:
        """在培养方案对象间建立有向依赖（如 课程→实训项目），拒绝成环。"""
        if from_target_id == to_target_id:
            raise CycleError("对象不能依赖自身")
        with self.repo.transaction():
            self.repo.get_target(from_target_id)
            self.repo.get_target(to_target_id)
            edge = TargetEdge(
                edge_id=_new_id("edg"),
                from_target_id=from_target_id,
                to_target_id=to_target_id,
                created_at=utcnow(),
            )
            self._assert_no_cycle(
                new_edge=(from_target_id, to_target_id),
                extra_nodes=[from_target_id, to_target_id],
            )
            self.repo.add_target_edge(edge)
            return edge

    # ============ 影响分析 ============

    def impact_analysis(self, version_id: str) -> dict[str, Any]:
        """列出某需求版本调整时的未来影响范围。

        返回直接引用该版本的对象，以及沿依赖边可达的全部下游对象
        （去重、稳定排序）。即使该版本已撤回/已更正，历史引用仍
        完整列出——这正是"说不清当时引用了什么"的解决手段。
        """
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            direct = self.repo.demand_links_for_version(version_id)
            direct_targets = sorted(
                (self.repo.get_target(link.target_id) for link in direct),
                key=lambda t: t.target_id,
            )

            # 反向邻接：谁依赖我（需求调整时，依赖方需要同步调整）。
            reverse: dict[str, set[str]] = {}
            for edge in self.repo.target_edges():
                reverse.setdefault(edge.to_target_id, set()).add(
                    edge.from_target_id
                )

            scope_ids: list[str] = []
            seen_ids: set[str] = set()
            for target in direct_targets:
                if target.target_id not in seen_ids:
                    seen_ids.add(target.target_id)
                    scope_ids.append(target.target_id)

            visited: set[str] = set()
            order: list[str] = []
            stack = [t.target_id for t in direct_targets]
            while stack:
                node = stack.pop()
                for dependent in sorted(reverse.get(node, ())):
                    if dependent not in visited:
                        visited.add(dependent)
                        order.append(dependent)
                        stack.append(dependent)

            for tid in order:
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    scope_ids.append(tid)
            scope = [self.repo.get_target(tid) for tid in scope_ids]
            active = self.repo.find_active_by_demand_code(version.demand_code)
            chain = self.chain(version_id)
            return {
                "version": version.snapshot(),
                "linked_is_effective": version.is_effective,
                "effective_version_id": active.version_id if active else None,
                "chain": chain,
                "directly_linked": [t.target_id for t in direct_targets],
                "impact_scope": [
                    {
                        "target_id": t.target_id,
                        "kind": t.kind.value,
                        "name": t.name,
                        "directly": t.target_id
                        in {d.target_id for d in direct_targets},
                    }
                    for t in scope
                ],
            }

    def chain(self, version_id: str) -> dict[str, Any]:
        """返回替代链：从需求线首版到给定版本的祖先，以及当前最新后继。"""
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            ancestors: list[DemandVersion] = []
            cursor = version
            while cursor.supersedes_id is not None:
                cursor = self.repo.get_version(cursor.supersedes_id)
                ancestors.append(cursor)
            ancestors.reverse()
            successor = self.repo.find_successor(version_id)
            latest = version
            seen: set[str] = set()
            while latest is not None and latest.version_id not in seen:
                seen.add(latest.version_id)
                nxt = self.repo.find_successor(latest.version_id)
                if nxt is None:
                    break
                latest = nxt
            return {
                "demand_code": version.demand_code,
                "root_id": ancestors[0].version_id if ancestors else version.version_id,
                "ancestors": [v.snapshot() for v in ancestors],
                "this_id": version.version_id,
                "successor_id": successor.version_id if successor else None,
                "latest_id": latest.version_id,
            }

    # ============ 查询 ============

    def get(self, version_id: str) -> DemandVersion:
        return self.repo.get_version(version_id)

    def list_versions(self, demand_code: str | None = None) -> list[DemandVersion]:
        return self.repo.list_versions(demand_code)

    def confirmations(self, version_id: str) -> list[Confirmation]:
        return self.repo.get_confirmations(version_id)

    # ============ 内部辅助 ============

    def _transition(
        self,
        version_id: str,
        target: VersionState,
        guard: Callable[[DemandVersion], None] | None = None,
    ) -> DemandVersion:
        with self.repo.transaction():
            version = self.repo.get_version(version_id)
            moved = self._transition_locked(version, target, guard)
            self.repo.save_version(
                moved, expected_version=moved.version - 1
            )
            return moved

    @staticmethod
    def _transition_locked(
        version: DemandVersion,
        target: VersionState,
        guard: Callable[[DemandVersion], None] | None = None,
    ) -> DemandVersion:
        """状态机守卫；必须在事务内调用。"""
        allowed = ALLOWED_TRANSITIONS.get(version.state, frozenset())
        if target not in allowed:
            raise StateError(
                f"{version.state.value}态不能迁移到{target.value}态"
            )
        if guard is not None:
            guard(version)
        moved = bump_version(version)
        moved.state = target
        return moved

    def _load_aggregate(self, version_id: str) -> ConfirmationAggregate:
        aggregate = ConfirmationAggregate(version_id=version_id)
        for confirmation in self.repo.get_confirmations(version_id):
            aggregate.parties[confirmation.party] = confirmation
        return aggregate

    def _assert_no_cycle(
        self,
        new_edge: tuple[str, str],
        extra_nodes: Iterable[str],
    ) -> None:
        """在现有图上试加一条边，若成环则拒绝。

        图节点为需求版本与培养方案对象；版本→对象为 DemandLink，
        对象→对象为 TargetEdge。
        """
        adjacency: dict[str, set[str]] = {}
        for edge in self.repo.target_edges():
            adjacency.setdefault(edge.from_target_id, set()).add(edge.to_target_id)
        for link in _all_links(self.repo):
            adjacency.setdefault(link.version_id, set()).add(link.target_id)
        for node in extra_nodes:
            adjacency.setdefault(node, set())

        src, dst = new_edge
        if src == dst:
            raise CycleError("关联不能指向自身")
        # 成环条件：加边后 dst 能沿现有边到达 src。
        stack = [dst]
        reachable: set[str] = set()
        while stack:
            current = stack.pop()
            if current == src:
                raise CycleError(
                    f"新增关联 {src} → {dst} 会形成循环关联，已拒绝"
                )
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(adjacency.get(current, ()))
        adjacency.setdefault(src, set()).add(dst)

    @staticmethod
    def _require_text(data: dict[str, Any], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{key} 不能为空")
        return value.strip()

    @staticmethod
    def _parse_regions(raw: Any) -> list[ApplicableRegion]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("适用地区至少包含一项")
        regions: list[ApplicableRegion] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValidationError("适用地区格式不正确")
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if not code or not name:
                raise ValidationError("适用地区的 code 与 name 均不能为空")
            if code in seen:
                raise ValidationError(f"适用地区重复：{code}")
            seen.add(code)
            regions.append(ApplicableRegion(code=code, name=name))
        return regions

    @staticmethod
    def _parse_evidence(raw: Any, fallback_time: datetime) -> list[Evidence]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("证据来源至少包含一项")
        evidence: list[Evidence] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValidationError("证据格式不正确")
            source = str(item.get("source") or "").strip()
            reference = str(item.get("reference") or "").strip()
            content_hash = str(item.get("content_hash") or "").strip()
            if not source or not content_hash:
                raise ValidationError("证据的 source 与 content_hash 不能为空")
            evidence.append(
                Evidence(
                    source=source,
                    reference=reference,
                    content_hash=content_hash,
                    captured_at=_parse_dt(item.get("captured_at")) or fallback_time,
                )
            )
        return evidence

    @staticmethod
    def _validate_window(version: DemandVersion) -> None:
        if version.valid_until is not None:
            if version.valid_until <= version.valid_from:
                raise ValidationError("失效时间必须晚于生效时间")

    @staticmethod
    def _validate_content(version: DemandVersion) -> None:
        DemandRegistryService._validate_window(version)
        if not version.regions or not version.evidence:
            raise ValidationError("核验要求适用地区与证据来源齐备")


def _all_links(repo: Repository):
    """收集仓储中全部需求关联（循环检测用）。"""
    links = []
    for target in repo.list_targets():
        links.extend(repo.demand_links_for_target(target.target_id))
    return links
