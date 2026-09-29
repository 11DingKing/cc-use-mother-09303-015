"""HTTP 接口端到端测试：真实起服务，用 urllib 走完整报文路径。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from demand_registry.api import build_server

BASE_DATA = {
    "demand_code": "JOB-OPS-002",
    "title": "运维岗位能力需求",
    "competency_statement": "能完成系统监控与故障应急处置",
    "publisher": "市域产教联合体",
    "enterprise": "北辰数据服务有限公司",
    "regions": [{"code": "320500", "name": "苏州市"}],
    "evidence": [
        {
            "source": "企业用人标准.docx",
            "reference": "二级条款 2.3",
            "content_hash": "sha256:deadbeef",
        }
    ],
}


class ApiEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = build_server("127.0.0.1", 0, None)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method: str, path: str, body=None):
        url = f"http://127.0.0.1:{self.port}/api/v1{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _publish_full(self) -> str:
        _, v = self._request("POST", "/demands", BASE_DATA)
        vid = v["version_id"]
        self._request("POST", f"/versions/{vid}/verify")
        self._request("POST", f"/versions/{vid}/request-confirmation")
        self._request("POST", f"/versions/{vid}/confirmations",
                      {"party": "企业", "confirmer": "企业HR"})
        self._request("POST", f"/versions/{vid}/confirmations",
                      {"party": "院校", "confirmer": "专业主任"})
        status, published = self._request("POST", f"/versions/{vid}/publish")
        self.assertEqual(status, 200)
        return vid

    def test_health_and_validation_error(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

        bad = dict(BASE_DATA)
        del bad["publisher"]
        status, body = self._request("POST", "/demands", bad)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "ValidationError")

    def test_full_lifecycle_chain_impact_over_http(self) -> None:
        vid = self._publish_full()

        # 登记对象并关联
        _, course = self._request("POST", "/targets",
                                  {"kind": "课程", "name": "运维自动化"})
        _, project = self._request(
            "POST", "/targets", {"kind": "实训项目", "name": "故障演练实训"}
        )
        status, link = self._request(
            "POST", f"/versions/{vid}/links",
            {"target_id": course["target_id"], "kind": "课程"},
        )
        self.assertEqual(status, 200)

        # 类型不匹配 → 400
        status, body = self._request(
            "POST", f"/versions/{vid}/links",
            {"target_id": course["target_id"], "kind": "技能标准"},
        )
        self.assertEqual(status, 400)

        # 依赖与成环
        self._request("POST", f"/targets/{project['target_id']}/edges",
                      {"to_target_id": course["target_id"]})
        _, standard = self._request(
            "POST", "/targets", {"kind": "技能标准", "name": "运维证书标准"}
        )
        self._request("POST", f"/targets/{standard['target_id']}/edges",
                      {"to_target_id": project["target_id"]})
        status, body = self._request(
            "POST", f"/targets/{course['target_id']}/edges",
            {"to_target_id": standard["target_id"]},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "CycleError")

        # 影响分析
        status, impact = self._request("GET", f"/versions/{vid}/impact")
        self.assertEqual(status, 200)
        scope = {item["target_id"] for item in impact["impact_scope"]}
        self.assertEqual(scope, {course["target_id"], project["target_id"],
                                 standard["target_id"]})

        # 更正替代链：旧版本保留，后继发布后旧版变"已更正"
        _, v2 = self._request("POST", f"/versions/{vid}/correct",
                              {"title": "运维岗位能力需求（第二版）"})
        self._request("POST", f"/versions/{v2['version_id']}/verify")
        self._request("POST", f"/versions/{v2['version_id']}/request-confirmation")
        self._request("POST", f"/versions/{v2['version_id']}/confirmations",
                      {"party": "企业", "confirmer": "企业HR"})
        self._request("POST", f"/versions/{v2['version_id']}/confirmations",
                      {"party": "院校", "confirmer": "专业主任"})
        status, v2p = self._request("POST", f"/versions/{v2['version_id']}/publish")
        self.assertEqual(status, 200)

        _, old = self._request("GET", f"/versions/{vid}")
        self.assertEqual(old["state"], "已更正")
        # 恢复已更正版本 → 409
        status, body = self._request("POST", f"/versions/{vid}/restore")
        self.assertEqual(status, 409)

        _, chain = self._request("GET", f"/versions/{v2p['version_id']}/chain")
        self.assertEqual(chain["root_id"], vid)
        self.assertEqual(chain["latest_id"], v2p["version_id"])

        # 按需求线列出全部版本，历史可追溯
        _, listing = self._request(
            "GET", "/versions?demand_code=JOB-OPS-002"
        )
        self.assertEqual([item["revision"] for item in listing], [1, 2])

    def test_withdraw_and_restore_over_http(self) -> None:
        vid = self._publish_full()
        status, body = self._request("POST", f"/versions/{vid}/withdraw", {})
        self.assertEqual(status, 400)  # 撤回原因必填

        status, _ = self._request(
            "POST", f"/versions/{vid}/withdraw", {"reason": "需求暂停"}
        )
        self.assertEqual(status, 200)
        status, restored = self._request("POST", f"/versions/{vid}/restore")
        self.assertEqual(status, 200)
        self.assertEqual(restored["state"], "发布")

    def test_not_found_and_unknown_route(self) -> None:
        status, body = self._request("GET", "/versions/ver_notexist")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NotFoundError")

        status, body = self._request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
