"""HTTP API 端到端测试（标准库 urllib，零依赖）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from demand_repo.http_api import create_server  # noqa: E402


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(":memory:", "127.0.0.1", _free_port())
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else {}

    def test_end_to_end_version_confirm_publish_impact(self) -> None:
        # 采集
        status, v1 = self.request("POST", "/demands", {
            "title": "工业机器人运维岗能力说明",
            "content": "本体保养、故障诊断",
            "region": "长三角", "publisher": "某智能制造企业",
            "valid_from": "2026-09-01", "valid_to": None,
            "evidence": {"source": "企业岗位说明书",
                         "document_ref": "企人字〔2026〕12号",
                         "issued_on": "2026-08-01"},
            "reason": "专业修订引入企业新岗位", "actor": "王老师"})
        self.assertEqual(status, 201)

        # 缺字段 → 400
        status, err = self.request("POST", "/demands", {"title": "x"})
        self.assertEqual(status, 400)
        self.assertIn("error", err)

        vid = v1["id"]
        self.assertEqual(self.request("POST", f"/demands/{vid}/submit",
                                      {"actor": "王老师"})[0], 200)
        # 单方确认发布 → 409
        self.assertEqual(self.request("POST", f"/demands/{vid}/confirm",
                                      {"party": "enterprise",
                                       "confirmer": "李经理"})[0], 200)
        status, _ = self.request("POST", f"/demands/{vid}/publish",
                                 {"actor": "王老师"})
        self.assertEqual(status, 409)
        self.assertEqual(self.request("POST", f"/demands/{vid}/confirm",
                                      {"party": "college",
                                       "confirmer": "张主任"})[0], 200)
        status, published = self.request("POST", f"/demands/{vid}/publish",
                                         {"actor": "王老师"})
        self.assertEqual(status, 200)
        self.assertEqual(published["status"], "发布")

        # 课程节点 + 引用
        _, course = self.request("POST", "/nodes", {
            "kind": "course", "code": "C-001", "title": "核心课",
            "actor": "王老师"})
        _, program = self.request("POST", "/nodes", {
            "kind": "program", "code": "P-001", "title": "培养方案",
            "actor": "张主任"})
        status, _ = self.request("POST", "/links", {
            "src_id": course["id"], "dst_id": vid, "kind": "references",
            "actor": "王老师"})
        self.assertEqual(status, 201)
        self.assertEqual(self.request("POST", "/links", {
            "src_id": program["id"], "dst_id": course["id"],
            "kind": "includes", "actor": "张主任"})[0], 201)

        # 更正并发版 v2
        _, v2d = self.request("POST", f"/demands/{vid}/correct", {
            "content": "新增视觉调试", "reason": "岗位升级",
            "actor": "李经理"})
        self.assertEqual(v2d["version_no"], 2)
        for party, who in (("enterprise", "李经理"), ("college", "张主任")):
            self.request("POST", f"/demands/{v2d['id']}/submit",
                         {"actor": "王老师"})
            self.request("POST", f"/demands/{v2d['id']}/confirm",
                         {"party": party, "confirmer": who})
        status, v2 = self.request("POST", f"/demands/{v2d['id']}/publish",
                                  {"actor": "王老师"})
        self.assertEqual(status, 200)

        # 影响分析：课程钉住旧版 v1
        status, report = self.request("GET", "/impact")
        self.assertEqual(status, 200)
        fam = report["families"][0]
        self.assertEqual(fam["current_head"], v2["id"])
        self.assertEqual(fam["stale_references"][0]["version_id"], vid)
        self.assertEqual(
            [p["code"] for p in
             fam["stale_references"][0]["affected_programs"]], ["P-001"])

        # 历史可解释为什么采用
        status, events = self.request(
            "GET", f"/history?aggregate_id={v1['family_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(events[0]["payload"]["evidence_ref"],
                         "企人字〔2026〕12号")

    def test_cycle_rejected_over_http(self) -> None:
        def publish_demand(**over) -> dict:
            body = {
                "title": "需求", "content": "c", "region": "r",
                "publisher": "p", "valid_from": "2026-09-01",
                "evidence": {"source": "s", "document_ref": "d"},
                "reason": "why", "actor": "a"}
            body.update(over)
            _, v = self.request("POST", "/demands", body)
            self.request("POST", f"/demands/{v['id']}/submit", {"actor": "a"})
            for party, who in (("enterprise", "e"), ("college", "c")):
                self.request("POST", f"/demands/{v['id']}/confirm",
                             {"party": party, "confirmer": who})
            _, pv = self.request("POST", f"/demands/{v['id']}/publish",
                                 {"actor": "a"})
            return pv

        a = publish_demand()
        c = publish_demand(title="另一需求")
        self.assertEqual(self.request("POST", "/links", {
            "src_id": a["id"], "dst_id": c["id"], "kind": "relates_to",
            "actor": "a"})[0], 201)
        status, err = self.request("POST", "/links", {
            "src_id": c["id"], "dst_id": a["id"], "kind": "relates_to",
            "actor": "a"})
        self.assertEqual(status, 409)
        self.assertIn("循环", err["error"])

    def test_unknown_route_404(self) -> None:
        self.assertEqual(self.request("GET", "/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
