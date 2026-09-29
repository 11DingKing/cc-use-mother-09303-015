"""领域服务的不变量回归测试。"""
from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from demand_repo import (  # noqa: E402
    COLLECT,
    CORRECT,
    RESTORE,
    WITHDRAW,
    ChainForkError,
    ConflictError,
    CycleDetectedError,
    DemandRepository,
    DemandStatus,
    EdgeKind,
    EventStore,
    Evidence,
    InvalidStateError,
    NodeKind,
    NotFoundError,
    ValidationError,
)

TODAY = date(2026, 9, 29)


def make_repo(tmp: Path | None = None) -> tuple[DemandRepository, EventStore]:
    import datetime as dt

    clock = lambda: dt.datetime(2026, 9, 29, 10, 0, 0)  # noqa: E731
    store = EventStore(tmp or ":memory:")
    return DemandRepository(store, clock=clock), store


def evidence(ref: str = "企人字〔2026〕12号") -> Evidence:
    return Evidence(source="合作企业岗位说明书", document_ref=ref,
                    issued_on=date(2026, 8, 1))


def collect_published(repo: DemandRepository, **kw) -> object:
    """采集→核验→双方确认→发布，返回 v1。"""
    defaults = dict(
        title="工业机器人运维岗能力说明",
        content="能完成机器人本体保养与故障诊断",
        region="长三角", publisher="某智能制造企业",
        valid_from=date(2026, 9, 1), valid_to=None,
        evidence=evidence(), reason="专业修订引入企业新岗位",
        actor="王老师")
    defaults.update(kw)
    v = repo.collect_demand(**defaults)
    repo.submit_for_review(v.id, "王老师")
    repo.confirm(v.id, "enterprise", "企业李经理")
    repo.confirm(v.id, "college", "院校张主任")
    return repo.publish(v.id, "王老师")


def _full_publish(repo: DemandRepository, draft_id: str) -> object:
    repo.submit_for_review(draft_id, "王老师")
    repo.confirm(draft_id, "enterprise", "企业李经理")
    repo.confirm(draft_id, "college", "院校张主任")
    return repo.publish(draft_id, "王老师")


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.store = make_repo()

    def test_full_publish_flow(self) -> None:
        v = collect_published(self.repo)
        self.assertEqual(v.status, DemandStatus.PUBLISHED)
        self.assertEqual(v.version_no, 1)
        self.assertIsNone(v.supersedes_id)
        self.assertEqual(v.change_kind, COLLECT)

    def test_publish_requires_both_parties(self) -> None:
        v = self.repo.collect_demand(
            title="t", content="c", region="r", publisher="p",
            valid_from=TODAY, valid_to=None, evidence=evidence(),
            reason="why", actor="a")
        self.repo.submit_for_review(v.id, "a")
        self.repo.confirm(v.id, "enterprise", "企业李经理")
        with self.assertRaises(ConflictError):
            self.repo.publish(v.id, "a")
        # 补齐院校确认后可发布
        self.repo.confirm(v.id, "college", "院校张主任")
        published = self.repo.publish(v.id, "a")
        self.assertEqual(published.status, DemandStatus.PUBLISHED)

    def test_confirm_is_idempotent_per_party(self) -> None:
        v = self.repo.collect_demand(
            title="t", content="c", region="r", publisher="p",
            valid_from=TODAY, valid_to=None, evidence=evidence(),
            reason="why", actor="a")
        self.repo.submit_for_review(v.id, "a")
        self.assertTrue(self.repo.confirm(v.id, "enterprise", "李经理"))
        self.assertFalse(self.repo.confirm(v.id, "enterprise", "李经理"))
        self.assertEqual(self.store.count_confirmations(v.id), 1)

    def test_invalid_party_rejected(self) -> None:
        v = self.repo.collect_demand(
            title="t", content="c", region="r", publisher="p",
            valid_from=TODAY, valid_to=None, evidence=evidence(),
            reason="why", actor="a")
        with self.assertRaises(ValidationError):
            self.repo.confirm(v.id, "government", "x")

    def test_reject_then_revise_and_republish(self) -> None:
        v = self.repo.collect_demand(
            title="旧标题", content="c", region="r", publisher="p",
            valid_from=TODAY, valid_to=None, evidence=evidence(),
            reason="why", actor="a")
        self.repo.submit_for_review(v.id, "a")
        rejected = self.repo.reject(v.id, "张主任", "证据文号缺失")
        self.assertEqual(rejected.status, DemandStatus.REJECTED)
        self.repo.revise_draft(v.id, "王老师",
                               evidence=evidence("企人字〔2026〕18号"))
        self.repo.submit_for_review(v.id, "王老师")
        self.repo.confirm(v.id, "enterprise", "李经理")
        self.repo.confirm(v.id, "college", "张主任")
        published = self.repo.publish(v.id, "王老师")
        self.assertEqual(published.status, DemandStatus.PUBLISHED)
        self.assertEqual(published.evidence.document_ref, "企人字〔2026〕18号")

    def test_validity_window_validated(self) -> None:
        with self.assertRaises(ValidationError):
            self.repo.collect_demand(
                title="t", content="c", region="r", publisher="p",
                valid_from=date(2026, 10, 1),
                valid_to=date(2026, 9, 1),
                evidence=evidence(), reason="why", actor="a")

    def test_published_version_immutable(self) -> None:
        v = collect_published(self.repo)
        with self.assertRaises(InvalidStateError):
            self.repo.revise_draft(v.id, "王老师", title="新")


class SubstitutionChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.store = make_repo()

    def test_correction_produces_linear_chain(self) -> None:
        v1 = collect_published(self.repo)
        v2_draft = self.repo.correct(v1.id, "企业李经理",
                                     "岗位能力更新：新增视觉调试",
                                     content="新增视觉系统调试能力")
        self.assertEqual(v2_draft.version_no, 2)
        self.assertEqual(v2_draft.supersedes_id, v1.id)
        self.assertEqual(v2_draft.change_kind, CORRECT)
        v2 = _full_publish(self.repo, v2_draft.id)
        self.assertEqual(v2.status, DemandStatus.PUBLISHED)
        refreshed_v1 = self.repo.get_version(v1.id)
        self.assertEqual(refreshed_v1.status, DemandStatus.SUPERSEDED)

        chain = self.repo.family_chain(v1.family_id)
        self.assertEqual([x.id for x in chain], [v1.id, v2.id])

    def test_cannot_correct_superseded_version_no_fork(self) -> None:
        v1 = collect_published(self.repo)
        v2_draft = self.repo.correct(v1.id, "李经理", "更新一")
        v2 = _full_publish(self.repo, v2_draft.id)
        # 试图从旧版本 v1 再发起更正 → 链分叉被拒绝
        with self.assertRaises(ChainForkError):
            self.repo.correct(v1.id, "李经理", "更新二")
        # 从最新版本可以继续
        v3_draft = self.repo.correct(v2.id, "李经理", "更新三")
        self.assertEqual(v3_draft.version_no, 3)

    def test_no_two_pending_successors(self) -> None:
        v1 = collect_published(self.repo)
        self.repo.correct(v1.id, "李经理", "进行中的更正")
        with self.assertRaises(ConflictError):
            self.repo.correct(v1.id, "李经理", "重复更正")

    def test_abandoned_successor_releases_chain_slot(self) -> None:
        v1 = collect_published(self.repo)
        draft = self.repo.correct(v1.id, "李经理", "误发起")
        self.repo.abandon_draft(draft.id, "李经理", "误操作撤回草稿")
        # 链位释放后可重新发起
        again = self.repo.correct(v1.id, "李经理", "重新更正")
        self.assertEqual(again.supersedes_id, v1.id)

    def test_withdraw_is_tombstone_and_restore_revives_family(self) -> None:
        v1 = collect_published(self.repo)
        tomb = self.repo.withdraw(v1.id, "企业李经理",
                                   "岗位说明书被发现失效两版")
        self.assertEqual(tomb.change_kind, WITHDRAW)
        self.assertEqual(tomb.content, "")
        tomb_pub = _full_publish(self.repo, tomb.id)
        self.assertEqual(tomb_pub.status, DemandStatus.PUBLISHED)
        # 撤回版本不能直接更正
        with self.assertRaises(InvalidStateError):
            self.repo.correct(tomb_pub.id, "李经理", "改一改")
        # 恢复：复制最近实质版本 v1 的内容
        restored = self.repo.restore(
            tomb_pub.id, "李经理", "企业发布新版岗位说明，恢复需求",
            valid_to=date(2027, 12, 31))
        self.assertEqual(restored.version_no, 3)
        self.assertEqual(restored.content, v1.content)
        self.assertEqual(restored.change_kind, RESTORE)
        restored_pub = _full_publish(self.repo, restored.id)
        self.assertEqual(restored_pub.status, DemandStatus.PUBLISHED)
        chain = self.repo.family_chain(v1.family_id)
        self.assertEqual([x.version_no for x in chain], [1, 2, 3])
        # 墓碑仍是链中一环，历史完整
        self.assertEqual(chain[1].change_kind, WITHDRAW)

    def test_restore_after_expiry(self) -> None:
        v1 = collect_published(self.repo, valid_from=date(2026, 8, 1),
                               valid_to=date(2026, 8, 31))
        # v1 已过期且为族头；恢复而不是更正
        draft = self.repo.restore(v1.id, "李经理", "企业延续该岗位需求",
                                  valid_from=date(2026, 9, 1),
                                  valid_to=date(2027, 8, 31))
        v2 = _full_publish(self.repo, draft.id)
        self.assertEqual(v2.version_no, 2)
        self.assertEqual(v2.valid_to, date(2027, 8, 31))
        self.assertEqual(self.repo.get_version(v1.id).status,
                         DemandStatus.SUPERSEDED)


class LinkingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.store = make_repo()
        self.v1 = collect_published(self.repo)

    def test_reference_pins_exact_version_and_marks_linked(self) -> None:
        course = self.repo.create_node(NodeKind.COURSE, "C-001",
                                       "工业机器人技术", "王老师")
        edge = self.repo.link(course.id, self.v1.id, EdgeKind.REFERENCES,
                              "王老师")
        self.assertTrue(edge.active)
        self.assertEqual(self.repo.get_version(self.v1.id).status,
                         DemandStatus.LINKED)
        # 更正发布 v2 后，课程仍钉在 v1
        v2 = _full_publish(self.repo, self.repo.correct(
            self.v1.id, "李经理", "岗位升级", content="新能力").id)
        edges = self.store.list_edges(EdgeKind.REFERENCES, active_only=True)
        self.assertEqual([(e.src_id, e.dst_id) for e in edges],
                         [(course.id, self.v1.id)])
        self.assertEqual(self.repo.get_version(v2.id).status,
                         DemandStatus.PUBLISHED)

    def test_cannot_reference_unpublished_or_superseded(self) -> None:
        course = self.repo.create_node(NodeKind.COURSE, "C-002", "课程", "a")
        draft = self.repo.collect_demand(
            title="t", content="c", region="r", publisher="p",
            valid_from=TODAY, valid_to=None, evidence=evidence(),
            reason="why", actor="a")
        with self.assertRaises(InvalidStateError):
            self.repo.link(course.id, draft.id, EdgeKind.REFERENCES, "a")
        v2 = _full_publish(self.repo, self.repo.correct(
            self.v1.id, "李经理", "x").id)
        with self.assertRaises(InvalidStateError):
            self.repo.link(course.id, self.v1.id, EdgeKind.REFERENCES, "a")
        self.assertEqual(v2.status, DemandStatus.PUBLISHED)

    def test_cannot_reference_expired_or_withdrawn(self) -> None:
        expired = collect_published(self.repo, valid_from=date(2026, 8, 1),
                                    valid_to=date(2026, 8, 31))
        course = self.repo.create_node(NodeKind.COURSE, "C-003", "课", "a")
        with self.assertRaises(InvalidStateError):
            self.repo.link(course.id, expired.id, EdgeKind.REFERENCES, "a")

        tomb = _full_publish(self.repo, self.repo.withdraw(
            self.v1.id, "李经理", "撤回").id)
        with self.assertRaises(InvalidStateError):
            self.repo.link(course.id, tomb.id, EdgeKind.REFERENCES, "a")

    def test_edge_endpoint_rules(self) -> None:
        course = self.repo.create_node(NodeKind.COURSE, "C-004", "课", "a")
        program = self.repo.create_node(NodeKind.PROGRAM, "P-001",
                                        "培养方案", "a")
        # 需求版本不能 includes，培养方案不能 references
        with self.assertRaises(ValidationError):
            self.repo.link(program.id, self.v1.id, EdgeKind.REFERENCES, "a")
        with self.assertRaises(ValidationError):
            self.repo.link(course.id, program.id, EdgeKind.INCLUDES, "a")
        # 合法包含
        self.repo.link(program.id, course.id, EdgeKind.INCLUDES, "a")

    def test_cycle_detection_on_demand_relations(self) -> None:
        a = self.v1
        b = _full_publish(self.repo, self.repo.correct(
            a.id, "李经理", "v2").id)
        # 另一个版本族 c
        c = collect_published(self.repo, title="另一个岗位需求")
        self.repo.link(a.id, c.id, EdgeKind.RELATES_TO, "a")  # a→c
        self.repo.link(c.id, b.id, EdgeKind.RELATES_TO, "a")  # c→b
        # b→a 将形成 a→c→b→a
        with self.assertRaises(CycleDetectedError):
            self.repo.link(b.id, a.id, EdgeKind.RELATES_TO, "a")
        # 自环
        with self.assertRaises(CycleDetectedError):
            self.repo.link(a.id, a.id, EdgeKind.RELATES_TO, "a")
        # 无环边仍可建立：a→b（a 已经 c 可达 b，但不构成环）
        self.repo.link(a.id, b.id, EdgeKind.RELATES_TO, "a")

    def test_unlink_is_tombstoned_and_can_relink(self) -> None:
        course = self.repo.create_node(NodeKind.COURSE, "C-005", "课", "a")
        self.repo.link(course.id, self.v1.id, EdgeKind.REFERENCES, "a")
        self.repo.unlink(course.id, self.v1.id, EdgeKind.REFERENCES, "a")
        self.assertEqual(
            self.store.list_edges(EdgeKind.REFERENCES, active_only=True), [])
        inactive = self.store.list_edges(EdgeKind.REFERENCES,
                                         active_only=False)
        self.assertFalse(inactive[0].active)
        # 重新关联激活原边，不产生重复行
        self.repo.link(course.id, self.v1.id, EdgeKind.REFERENCES, "a")
        all_edges = self.store.list_edges(EdgeKind.REFERENCES,
                                          active_only=False)
        self.assertEqual(len(all_edges), 1)
        self.assertTrue(all_edges[0].active)

    def test_link_unknown_endpoint(self) -> None:
        with self.assertRaises(NotFoundError):
            self.repo.link("nope", self.v1.id, EdgeKind.REFERENCES, "a")


class ImpactAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.store = make_repo()

    def test_impact_lists_stale_pins_and_programs(self) -> None:
        v1 = collect_published(self.repo)
        course = self.repo.create_node(NodeKind.COURSE, "C-101",
                                       "核心课", "王老师")
        training = self.repo.create_node(NodeKind.TRAINING, "T-101",
                                         "实训项目", "王老师")
        standard = self.repo.create_node(NodeKind.STANDARD, "S-101",
                                         "技能标准", "王老师")
        program = self.repo.create_node(NodeKind.PROGRAM, "P-101",
                                        "2026 级人才培养方案", "张主任")
        unrelated = self.repo.create_node(NodeKind.PROGRAM, "P-999",
                                          "其他专业方案", "张主任")
        for node in (course, training, standard):
            self.repo.link(node.id, v1.id, EdgeKind.REFERENCES, "王老师")
        self.repo.link(program.id, course.id, EdgeKind.INCLUDES, "张主任")
        self.repo.link(program.id, training.id, EdgeKind.INCLUDES, "张主任")
        self.repo.link(unrelated.id, standard.id, EdgeKind.INCLUDES,
                       "张主任")

        # v1 被 v2 替代
        v2 = _full_publish(self.repo, self.repo.correct(
            v1.id, "李经理", "岗位能力换版", content="新内容").id)

        report = self.repo.impact_analysis()
        self.assertEqual(len(report["families"]), 1)
        fam = report["families"][0]
        self.assertEqual(fam["current_head"], v2.id)
        self.assertEqual(fam["current_version_no"], 2)
        stale = fam["stale_references"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["version_id"], v1.id)
        self.assertIn("superseded", stale[0]["reasons"])
        codes = {x["code"] for x in stale[0]["referenced_by"]}
        self.assertEqual(codes, {"C-101", "T-101", "S-101"})
        programs = {p["code"] for p in stale[0]["affected_programs"]}
        self.assertEqual(programs, {"P-101", "P-999"})
        adjust = {x["code"] for x in fam["nodes_to_adjust"]}
        self.assertEqual(adjust, {"C-101", "T-101", "S-101"})

    def test_impact_flags_expired_and_withdrawn(self) -> None:
        import datetime as dt

        # 时钟停在有效期内：先完成发布与引用
        store = EventStore(":memory:")
        repo = DemandRepository(
            store, clock=lambda: dt.datetime(2026, 8, 10, 9, 0))
        v1 = collect_published(repo, valid_from=date(2026, 8, 1),
                               valid_to=date(2026, 8, 31))
        course = repo.create_node(NodeKind.COURSE, "C-201", "课", "a")
        repo.link(course.id, v1.id, EdgeKind.REFERENCES, "a")
        # 时间推进到 9 月：版本已过期，引用仍钉住
        report = repo.impact_analysis(as_of=date(2026, 9, 29))
        self.assertIn("expired",
                      report["families"][0]["stale_references"][0]["reasons"])

        tomb = _full_publish(repo, repo.withdraw(
            v1.id, "李经理", "撤回").id)
        self.assertEqual(tomb.change_kind, WITHDRAW)
        report = repo.impact_analysis(as_of=date(2026, 9, 29))
        fam = report["families"][0]
        self.assertEqual(fam["current_head"], "withdrawn")


class HistoryTest(unittest.TestCase):
    def test_events_explain_why_version_was_adopted(self) -> None:
        repo, _ = make_repo()
        v1 = collect_published(repo)
        _full_publish(repo, repo.withdraw(v1.id, "李经理",
                                          "引用的岗位说明已失效两版").id)
        events = repo.history(v1.family_id)
        types = [e.event_type for e in events]
        self.assertEqual(types, [
            "DEMAND_COLLECTED", "SUBMITTED_FOR_REVIEW", "CONFIRMED",
            "CONFIRMED", "DEMAND_PUBLISHED", "SUCCESSOR_DRAFTED",
            "SUBMITTED_FOR_REVIEW", "CONFIRMED", "CONFIRMED",
            "DEMAND_PUBLISHED", "VERSION_SUPERSEDED",
        ])
        collected = events[0]
        self.assertEqual(collected.payload["reason"], "专业修订引入企业新岗位")
        self.assertEqual(collected.payload["evidence_ref"],
                         "企人字〔2026〕12号")
        self.assertEqual(events[5].payload["change_kind"], WITHDRAW)
        self.assertEqual(events[5].payload["reason"],
                         "引用的岗位说明已失效两版")


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_publish_succeeds_once(self) -> None:
        repo, store = make_repo()
        v = repo.collect_demand(
            title="t", content="c", region="r", publisher="p",
            valid_from=TODAY, valid_to=None, evidence=evidence(),
            reason="why", actor="a")
        repo.submit_for_review(v.id, "a")
        repo.confirm(v.id, "enterprise", "李经理")
        repo.confirm(v.id, "college", "张主任")

        outcomes: list[str] = []

        def publish(_: int) -> str:
            try:
                repo.publish(v.id, "a")
                return "ok"
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(publish, range(8)))
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("conflict"), 7)
        published = [e for e in repo.history(v.family_id)
                     if e.event_type == "DEMAND_PUBLISHED"]
        self.assertEqual(len(published), 1)

    def test_concurrent_correction_succeeds_once(self) -> None:
        repo, _ = make_repo()
        v1 = collect_published(repo)

        def correct(_: int) -> str:
            try:
                repo.correct(v1.id, "李经理", "并发更正")
                return "ok"
            except (ConflictError, ChainForkError):
                return "conflict"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(correct, range(8)))
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("conflict"), 7)


class PersistenceTest(unittest.TestCase):
    def test_state_survives_reopen(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "repo.db"
            repo, store = make_repo(path)
            v = collect_published(repo)
            store.close()

            store2 = EventStore(path)
            repo2 = DemandRepository(store2)
            got = repo2.get_version(v.id)
            self.assertEqual(got.status, DemandStatus.PUBLISHED)
            self.assertEqual(got.evidence.document_ref, "企人字〔2026〕12号")
            self.assertEqual(len(repo2.history(v.family_id)), 5)
            store2.close()


if __name__ == "__main__":
    unittest.main()
