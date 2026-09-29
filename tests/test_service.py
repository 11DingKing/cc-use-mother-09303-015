"""服务层一致性不变量测试（内存仓储与 SQLite 仓储各跑一遍）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from threading import Barrier, Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from demand_registry import (
    ConflictError,
    CycleError,
    ImmutableHistoryError,
    InMemoryRepository,
    Party,
    RelationKind,
    SqliteRepository,
    StateError,
    ValidationError,
    VersionState,
)
from demand_registry.service import DemandRegistryService

DEMAND_DATA = {
    "demand_code": "JOB-DEV-001",
    "title": "后端开发岗位能力需求",
    "competency_statement": "能独立完成服务端接口设计与版本化发布",
    "publisher": "市产教联合体",
    "enterprise": "星河软件有限公司",
    "regions": [{"code": "330100", "name": "杭州市"}],
    "evidence": [
        {
            "source": "岗位说明书-2026.pdf",
            "reference": "第3章 第12条",
            "content_hash": "sha256:abc123",
        }
    ],
}


class ServiceTestBase:
    """共用用例；子类提供 make_service。"""

    def make_service(self) -> DemandRegistryService:  # pragma: no cover
        raise NotImplementedError

    # ---------- 采集 / 核验 / 双方确认 / 发布 ----------

    def test_full_publish_requires_both_parties(self) -> None:
        svc = self.make_service()
        v = svc.register_demand(DEMAND_DATA)
        self.assertIs(v.state, VersionState.DRAFT)
        self.assertEqual(v.revision, 1)

        svc.verify(v.version_id)
        svc.request_confirmation(v.version_id)

        svc.confirm(v.version_id, Party.ENTERPRISE, "企业人事总监")
        with self.assertRaises(StateError):
            svc.publish(v.version_id)  # 院校尚未确认

        # 同一方不能重复确认
        with self.assertRaises(StateError):
            svc.confirm(v.version_id, "企业", "另一位企业代表")

        svc.confirm(v.version_id, Party.COLLEGE, "专业负责人")
        published = svc.publish(v.version_id)
        self.assertIs(published.state, VersionState.PUBLISHED)
        self.assertIsNotNone(published.published_at)

    def test_illegal_transitions_are_rejected(self) -> None:
        svc = self.make_service()
        v = svc.register_demand(DEMAND_DATA)
        with self.assertRaises(StateError):
            svc.publish(v.version_id)  # 采集态不能直接发布
        with self.assertRaises(StateError):
            svc.confirm(v.version_id, Party.ENTERPRISE, "x")  # 未进入确认

    def test_draft_is_mutable_then_frozen(self) -> None:
        svc = self.make_service()
        v = svc.register_demand(DEMAND_DATA)
        updated = svc.update_draft(
            v.version_id, {"title": "后端开发岗位能力需求（修订措辞）"}
        )
        self.assertIn("修订措辞", updated.title)

        svc.verify(v.version_id)
        with self.assertRaises(ImmutableHistoryError):
            svc.update_draft(v.version_id, {"title": "不得再改"})

    def test_validation_rules(self) -> None:
        svc = self.make_service()
        bad = dict(DEMAND_DATA)
        bad["regions"] = []
        with self.assertRaises(ValidationError):
            svc.register_demand(bad)
        bad = dict(DEMAND_DATA)
        del bad["title"]
        with self.assertRaises(ValidationError):
            svc.register_demand(bad)

        v = svc.register_demand(DEMAND_DATA)
        with self.assertRaises(ValidationError):
            svc.update_draft(
                v.version_id,
                {"valid_until": v.valid_from.isoformat()},
            )

    # ---------- 替代链：撤回 / 更正 / 恢复 ----------

    def _publish(self, svc: DemandRegistryService, data: dict | None = None):
        v = svc.register_demand(data or DEMAND_DATA)
        svc.verify(v.version_id)
        svc.request_confirmation(v.version_id)
        svc.confirm(v.version_id, Party.ENTERPRISE, "企业代表")
        svc.confirm(v.version_id, Party.COLLEGE, "院校代表")
        return svc.publish(v.version_id)

    def test_correction_creates_chain_and_preserves_history(self) -> None:
        svc = self.make_service()
        v1 = self._publish(svc)
        course = svc.register_target(RelationKind.COURSE, "服务端开发综合课")
        svc.link_demand(v1.version_id, course.target_id, RelationKind.COURSE)

        # 发起更正：v1 仍生效，后继为草稿
        v2 = svc.correct(v1.version_id, {
            "competency_statement": "能设计高并发服务端接口并灰度发布",
        })
        self.assertEqual(v2.revision, 2)
        self.assertEqual(v2.supersedes_id, v1.version_id)
        self.assertIs(v2.state, VersionState.DRAFT)
        self.assertIs(svc.get(v1.version_id).state, VersionState.PUBLISHED)

        # 旧版本内容原样保留，培养方案仍引用原版本
        old = svc.get(v1.version_id)
        self.assertEqual(old.competency_statement,
                         DEMAND_DATA["competency_statement"])
        links = svc.repo.demand_links_for_version(v1.version_id)
        self.assertEqual(len(links), 1)

        # 替代链必须线性：不能分叉
        with self.assertRaises(ConflictError):
            svc.correct(v1.version_id)

        # 走完双方确认后发布后继，前驱同事务进入"已更正"
        svc.verify(v2.version_id)
        svc.request_confirmation(v2.version_id)
        svc.confirm(v2.version_id, Party.ENTERPRISE, "企业代表")
        svc.confirm(v2.version_id, Party.COLLEGE, "院校代表")
        v2p = svc.publish(v2.version_id)
        self.assertIs(v2p.state, VersionState.PUBLISHED)
        self.assertIs(svc.get(v1.version_id).state, VersionState.CORRECTED)

        # 唯一生效版本
        active = svc.repo.find_active_by_demand_code("JOB-DEV-001")
        self.assertEqual(active.version_id, v2p.version_id)

        # 已更正版本不可恢复
        with self.assertRaises(StateError):
            svc.restore(v1.version_id)

        chain = svc.chain(v2p.version_id)
        self.assertEqual(chain["root_id"], v1.version_id)
        self.assertEqual(chain["ancestors"][0]["version_id"], v1.version_id)
        self.assertEqual(chain["latest_id"], v2p.version_id)

    def test_withdraw_then_restore_preserves_links(self) -> None:
        svc = self.make_service()
        v1 = self._publish(svc)
        course = svc.register_target(RelationKind.COURSE, "课程甲")
        svc.link_demand(v1.version_id, course.target_id, RelationKind.COURSE)

        with self.assertRaises(ValidationError):
            svc.withdraw(v1.version_id, reason="  ")  # 撤回必须有原因

        withdrawn = svc.withdraw(v1.version_id, reason="企业岗位裁撤")
        self.assertIs(withdrawn.state, VersionState.WITHDRAWN)
        self.assertIsNotNone(withdrawn.withdrawn_at)
        # 历史引用原样保留
        self.assertEqual(
            len(svc.repo.demand_links_for_version(v1.version_id)), 1
        )
        # 失效版本不能再建立关联
        with self.assertRaises(StateError):
            t2 = svc.register_target(RelationKind.COURSE, "课程乙")
            svc.link_demand(v1.version_id, t2.target_id, RelationKind.COURSE)

        restored = svc.restore(v1.version_id)
        self.assertIs(restored.state, VersionState.PUBLISHED)

    def test_cannot_have_two_active_versions(self) -> None:
        svc = self.make_service()
        v1 = self._publish(svc)
        # 同一需求线的另一条独立草稿走完流程，发布必须被拒绝
        other = svc.register_demand(DEMAND_DATA)
        self.assertEqual(other.revision, 2)
        svc.verify(other.version_id)
        svc.request_confirmation(other.version_id)
        svc.confirm(other.version_id, Party.ENTERPRISE, "企业代表")
        svc.confirm(other.version_id, Party.COLLEGE, "院校代表")
        with self.assertRaises(ConflictError):
            svc.publish(other.version_id)
        # 被拒绝后 v1 仍然唯一生效
        self.assertIs(svc.get(v1.version_id).state, VersionState.PUBLISHED)

    # ---------- 循环关联 ----------

    def test_cyclic_target_edges_are_rejected(self) -> None:
        svc = self.make_service()
        v1 = self._publish(svc)
        a = svc.register_target(RelationKind.COURSE, "课程A")
        b = svc.register_target(RelationKind.TRAINING_PROJECT, "实训B")
        c = svc.register_target(RelationKind.SKILL_STANDARD, "标准C")
        svc.link_demand(v1.version_id, a.target_id, RelationKind.COURSE)

        svc.add_target_edge(a.target_id, b.target_id)  # A → B
        svc.add_target_edge(b.target_id, c.target_id)  # B → C
        with self.assertRaises(CycleError):
            svc.add_target_edge(c.target_id, a.target_id)  # C → A 成环
        with self.assertRaises(CycleError):
            svc.add_target_edge(b.target_id, b.target_id)  # 自环

        # 被拒绝后，已有边不受影响
        self.assertEqual(len(svc.repo.target_edges()), 2)

    # ---------- 影响分析 ----------

    def test_impact_analysis_lists_future_adjustment_scope(self) -> None:
        svc = self.make_service()
        v1 = self._publish(svc)
        course = svc.register_target(RelationKind.COURSE, "专业核心课")
        project = svc.register_target(RelationKind.TRAINING_PROJECT, "综合实训")
        standard = svc.register_target(RelationKind.SKILL_STANDARD, "技能等级标准")
        unrelated = svc.register_target(RelationKind.COURSE, "无关课程")

        svc.link_demand(v1.version_id, course.target_id, RelationKind.COURSE)
        svc.add_target_edge(project.target_id, course.target_id)
        svc.add_target_edge(standard.target_id, project.target_id)

        impact = svc.impact_analysis(v1.version_id)
        ids = [item["target_id"] for item in impact["impact_scope"]]
        self.assertEqual(ids[0], course.target_id)  # 直接引用排最前
        self.assertIn(project.target_id, ids)
        self.assertIn(standard.target_id, ids)
        self.assertNotIn(unrelated.target_id, ids)
        direct_marks = {
            item["target_id"]: item["directly"] for item in impact["impact_scope"]
        }
        self.assertTrue(direct_marks[course.target_id])
        self.assertFalse(direct_marks[standard.target_id])

        # v1 被更正后，针对旧版本的影响分析仍能完整列出历史引用
        v2 = svc.correct(v1.version_id, {"title": "岗位需求v2"})
        svc.verify(v2.version_id)
        svc.request_confirmation(v2.version_id)
        svc.confirm(v2.version_id, Party.ENTERPRISE, "企业代表")
        svc.confirm(v2.version_id, Party.COLLEGE, "院校代表")
        svc.publish(v2.version_id)
        stale_impact = svc.impact_analysis(v1.version_id)
        self.assertFalse(stale_impact["linked_is_effective"])
        self.assertEqual(
            stale_impact["effective_version_id"], v2.version_id
        )
        self.assertIn(
            course.target_id,
            [item["target_id"] for item in stale_impact["impact_scope"]],
        )

    # ---------- 并发 ----------

    def test_concurrent_publish_only_one_wins(self) -> None:
        svc = self.make_service()
        v = svc.register_demand(DEMAND_DATA)
        svc.verify(v.version_id)
        svc.request_confirmation(v.version_id)
        svc.confirm(v.version_id, Party.ENTERPRISE, "企业代表")
        svc.confirm(v.version_id, Party.COLLEGE, "院校代表")
        current = svc.get(v.version_id).version

        results: list[Exception | str] = []
        barrier = Barrier(4)

        def attempt() -> None:
            barrier.wait()
            try:
                svc.publish(v.version_id, expected_version=current)
                results.append("ok")
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        threads = [Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r == "ok"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(results), 4)
        self.assertIs(svc.get(v.version_id).state, VersionState.PUBLISHED)

    def test_concurrent_confirmations_do_not_duplicate(self) -> None:
        svc = self.make_service()
        v = svc.register_demand(DEMAND_DATA)
        svc.verify(v.version_id)
        svc.request_confirmation(v.version_id)

        errors: list[Exception] = []
        barrier = Barrier(2)

        def confirm_enterprise() -> None:
            barrier.wait()
            try:
                svc.confirm(v.version_id, Party.ENTERPRISE, "企业代表")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [Thread(target=confirm_enterprise) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 1)  # 恰好一个重复确认失败
        parties = {c.party for c in svc.confirmations(v.version_id)}
        self.assertEqual(parties, {Party.ENTERPRISE})


class InMemoryServiceTest(ServiceTestBase, unittest.TestCase):
    def make_service(self) -> DemandRegistryService:
        return DemandRegistryService(InMemoryRepository())


class SqliteServiceTest(ServiceTestBase, unittest.TestCase):
    def make_service(self) -> DemandRegistryService:
        # 每个用例独立临时库
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        repo = SqliteRepository(tmp.name)
        self.addCleanup(repo.close)
        return DemandRegistryService(repo)

    def test_persistence_survives_reopen(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        repo = SqliteRepository(path)
        svc = DemandRegistryService(repo)
        v = self._publish(svc)
        repo.close()

        reopened = SqliteRepository(path)
        svc2 = DemandRegistryService(reopened)
        loaded = svc2.get(v.version_id)
        self.assertIs(loaded.state, VersionState.PUBLISHED)
        self.assertEqual(loaded.regions[0].name, "杭州市")
        self.assertEqual(loaded.evidence[0].content_hash, "sha256:abc123")
        parties = {c.party for c in svc2.confirmations(v.version_id)}
        self.assertEqual(parties, {Party.ENTERPRISE, Party.COLLEGE})
        reopened.close()


if __name__ == "__main__":
    unittest.main()
