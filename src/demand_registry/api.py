"""标准库 HTTP 接口（无第三方依赖）。

路由一览（前缀 /api/v1）：

    POST   /demands                         采集新需求（第 1 版）
    GET    /versions[?demand_code=...]      列版本
    GET    /versions/{id}                   查版本
    PATCH  /versions/{id}                   修改采集态草稿
    POST   /versions/{id}/verify            采集 → 核验
    POST   /versions/{id}/request-confirmation
    POST   /versions/{id}/confirmations     双方确认 {party, confirmer}
    POST   /versions/{id}/publish           双方齐备后发布
    POST   /versions/{id}/withdraw          撤回 {reason}
    POST   /versions/{id}/correct           更正（生成替代后继草稿）
    POST   /versions/{id}/restore           失效后恢复
    GET    /versions/{id}/chain             替代链
    GET    /versions/{id}/impact            影响分析
    POST   /targets                         登记课程/实训项目/技能标准
    GET    /targets
    POST   /versions/{id}/links             关联 {target_id, kind, note}
    POST   /targets/{id}/edges              对象间依赖 {to_target_id}

启动：``python -m demand_registry.api --db registry.db --port 8080``
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import (
    ConflictError,
    CycleError,
    ImmutableHistoryError,
    NotFoundError,
    RegistryError,
    StateError,
    ValidationError,
)
from .repositories import InMemoryRepository, SqliteRepository
from . import serializers
from .service import DemandRegistryService

_PREFIX = "/api/v1"

# 错误 → HTTP 状态
_ERROR_STATUS: dict[type[Exception], int] = {
    ValidationError: 400,
    CycleError: 422,
    NotFoundError: 404,
    ImmutableHistoryError: 409,
    StateError: 409,
    ConflictError: 409,
}


def create_service(db_path: str | None = None) -> DemandRegistryService:
    """工厂：给定路径用 SQLite 持久化，否则用内存仓储。"""
    repo = SqliteRepository(db_path) if db_path else InMemoryRepository()
    return DemandRegistryService(repo)


class ApiHandler(BaseHTTPRequestHandler):
    """请求分发；具体服务由 ``self.server.service`` 提供。"""

    server_version = "DemandRegistry/1.0"

    # BaseHTTPRequestHandler 没有泛型标注，这里放宽签名约束
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    # ---- 分发 ----

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        service: DemandRegistryService = self.server.service  # type: ignore[attr-defined]

        routes = self._routes(service, parsed)
        for pattern, verb, handler in routes:
            if verb != method:
                continue
            match = re.fullmatch(pattern, path)
            if match is None:
                continue
            try:
                payload = self._read_json() if method in ("POST", "PATCH") else {}
                result = handler(payload, **match.groupdict())
            except RegistryError as exc:
                self._write_json(
                    _ERROR_STATUS.get(type(exc), 422),
                    {"error": type(exc).__name__, "message": str(exc)},
                )
                return
            except (ValueError, KeyError, TypeError) as exc:
                self._write_json(
                    400,
                    {"error": "BadRequest", "message": str(exc)},
                )
                return
            status, body = result if isinstance(result, tuple) else (200, result)
            self._write_json(status, body)
            return

        self._write_json(404, {"error": "NotFound", "message": f"无此路由：{method} {path}"})

    def _routes(
        self, service: DemandRegistryService, parsed: Any
    ) -> list[tuple[str, str, Callable[..., tuple[int, Any] | Any]]]:
        from urllib.parse import parse_qs

        p = _prefix()
        v = lambda fn: serializers.version_to_json(fn)  # noqa: E731
        query = parse_qs(parsed.query)
        demand_code = query.get("demand_code", [None])[0]
        return [
            (rf"{p}/health", "GET", lambda payload: (200, {"status": "ok"})),
            (rf"{p}/demands", "POST",
             lambda payload: (201, v(service.register_demand(payload)))),
            (rf"{p}/versions", "GET",
             lambda payload: [
                 serializers.version_to_json(ver)
                 for ver in service.list_versions(demand_code)
             ]),
            (rf"{p}/versions/(?P<version_id>[\w-]+)", "GET",
             lambda payload, version_id: v(service.get(version_id))),
            (rf"{p}/versions/(?P<version_id>[\w-]+)", "PATCH",
             lambda payload, version_id: v(service.update_draft(version_id, payload))),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/verify", "POST",
             lambda payload, version_id: v(service.verify(version_id))),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/request-confirmation", "POST",
             lambda payload, version_id: v(service.request_confirmation(version_id))),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/confirmations", "POST",
             lambda payload, version_id: self._confirm(service, version_id, payload)),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/confirmations", "GET",
             lambda payload, version_id: [
                 serializers.confirmation_to_json(c)
                 for c in service.confirmations(version_id)
             ]),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/publish", "POST",
             lambda payload, version_id: v(service.publish(
                 version_id, expected_version=payload.get("expected_version")))),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/withdraw", "POST",
             lambda payload, version_id: v(
                 service.withdraw(version_id, payload.get("reason", "")))),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/correct", "POST",
             lambda payload, version_id: v(service.correct(version_id, payload))),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/restore", "POST",
             lambda payload, version_id: v(service.restore(version_id))),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/chain", "GET",
             lambda payload, version_id: service.chain(version_id)),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/impact", "GET",
             lambda payload, version_id: service.impact_analysis(version_id)),
            (rf"{p}/versions/(?P<version_id>[\w-]+)/links", "POST",
             lambda payload, version_id: serializers.link_to_json(
                 service.link_demand(
                     version_id,
                     payload["target_id"],
                     payload["kind"],
                     payload.get("note", ""),
                 ))),
            (rf"{p}/targets", "POST",
             lambda payload: (201, serializers.target_to_json(
                 service.register_target(payload["kind"], payload["name"])))),
            (rf"{p}/targets", "GET",
             lambda payload: [serializers.target_to_json(t)
                              for t in service.repo.list_targets()]),
            (rf"{p}/targets/(?P<target_id>[\w-]+)/edges", "POST",
             lambda payload, target_id: serializers.edge_to_json(
                 service.add_target_edge(target_id, payload["to_target_id"]))),
        ]

    @staticmethod
    def _confirm(
        service: DemandRegistryService, version_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        confirmation = service.confirm(
            version_id, payload["party"], payload["confirmer"]
        )
        return serializers.confirmation_to_json(confirmation)

    # ---- 报文工具 ----

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _write_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 简洁的访问日志；测试环境可直接丢弃
        import sys

        sys.stderr.write(
            "[api] %s - %s\n" % (self.address_string(), fmt % args)
        )


def _prefix() -> str:
    return _PREFIX


def build_server(host: str, port: int, db_path: str | None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = create_service(db_path)  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="产教需求版本库 HTTP 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=None, help="SQLite 文件路径；缺省为内存模式")
    args = parser.parse_args(argv)

    server = build_server(args.host, args.port, args.db)
    print(f"产教需求版本库服务已启动：http://{args.host}:{args.port}{_PREFIX}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
