"""HTTP 适配层（标准库 http.server，零第三方依赖）。

路由：

    POST   /demands                       采集需求 v1
    GET    /demands/{id}                  查询版本
    POST   /demands/{id}/submit           提交核验
    POST   /demands/{id}/reject           驳回
    POST   /demands/{id}/confirm          企业/院校确认
    POST   /demands/{id}/publish          双方齐备后发布
    POST   /demands/{id}/correct          更正（产生后继草稿）
    POST   /demands/{id}/withdraw         撤回（墓碑后继）
    POST   /demands/{id}/restore          失效后恢复
    GET    /families/{family_id}          替代链
    POST   /nodes                         建课程/实训/标准/培养方案节点
    POST   /links                          建立关联（循环检测）
    DELETE /links                          解除关联
    GET    /impact                        影响分析
    GET    /history                       审计事件时间线

错误映射：ValidationError→400，NotFoundError→404，
InvalidStateError/CycleDetectedError/ChainForkError/ConflictError→409。
"""
from __future__ import annotations

import dataclasses
import enum
import json
import re
import threading
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import (
    ConflictError,
    DomainError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from .models import DemandStatus, EdgeKind, Evidence, NodeKind
from .repository import DemandRepository
from .store import EventStore


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def _parse_date(value: Any, field: str, *, required: bool = True) -> date | None:
    if value is None:
        if required:
            raise ValidationError(f"{field} 不能为空")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 日期字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 日期格式错误") from exc


def _evidence(body: dict) -> Evidence:
    raw = body.get("evidence")
    if not isinstance(raw, dict):
        raise ValidationError("evidence 必须是对象")
    return Evidence(
        source=str(raw.get("source", "")),
        document_ref=str(raw.get("document_ref", "")),
        issued_on=_parse_date(raw.get("issued_on"), "evidence.issued_on",
                              required=False),
    )


class ApiHandler(BaseHTTPRequestHandler):
    repo: DemandRepository  # 由工厂注入类属性

    server_version = "DemandRepo/0.1"

    # ---- 框架 --------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("请求体不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _send(self, status: int, value: Any) -> None:
        data = json.dumps(to_jsonable(value), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, fn: Callable[[], Any]) -> None:
        try:
            result = fn()
        except ValidationError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except NotFoundError as exc:
            self._send(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (InvalidStateError, ConflictError) as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except DomainError as exc:
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音
        return

    # ---- 路由 --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        def dispatch() -> None:
            if m := re.fullmatch(r"/demands/([^/]+)", path):
                self._send(HTTPStatus.OK, self.repo.get_version(m.group(1)))
            elif m := re.fullmatch(r"/families/([^/]+)", path):
                self._send(HTTPStatus.OK,
                           self.repo.family_chain(m.group(1)))
            elif path == "/impact":
                query = dict(p.split("=", 1) for p in
                             parsed.query.split("&") if "=" in p)
                as_of = _parse_date(query.get("as_of"), "as_of",
                                    required=False)
                self._send(HTTPStatus.OK,
                           self.repo.impact_analysis(query.get("family_id"),
                                                    as_of))
            elif path == "/history":
                query = dict(p.split("=", 1) for p in
                             parsed.query.split("&") if "=" in p)
                self._send(HTTPStatus.OK,
                           self.repo.history(query.get("aggregate_id")))
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "未知路由"})

        self._handle(dispatch)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"

        def dispatch() -> None:
            body = self._read_json()
            actor = str(body.get("actor") or "system")
            if path == "/demands":
                v = self.repo.collect_demand(
                    title=str(body.get("title", "")),
                    content=str(body.get("content", "")),
                    region=str(body.get("region", "")),
                    publisher=str(body.get("publisher", "")),
                    valid_from=_parse_date(body.get("valid_from"),
                                           "valid_from"),
                    valid_to=_parse_date(body.get("valid_to"), "valid_to",
                                         required=False),
                    evidence=_evidence(body),
                    reason=str(body.get("reason", "")),
                    actor=actor,
                )
                self._send(HTTPStatus.CREATED, v)
            elif m := re.fullmatch(r"/demands/([^/]+)/(submit|reject|confirm"
                                   r"|publish|correct|withdraw|restore)", path):
                self._demand_action(m.group(1), m.group(2), body, actor)
            elif path == "/nodes":
                node = self.repo.create_node(
                    NodeKind(str(body.get("kind", ""))),
                    str(body.get("code", "")), str(body.get("title", "")),
                    actor)
                self._send(HTTPStatus.CREATED, node)
            elif path == "/links":
                for key in ("src_id", "dst_id"):
                    if not body.get(key):
                        raise ValidationError(f"{key} 不能为空")
                edge = self.repo.link(
                    str(body["src_id"]), str(body["dst_id"]),
                    EdgeKind(str(body.get("kind", "references"))), actor)
                self._send(HTTPStatus.CREATED, edge)
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "未知路由"})

        self._handle(dispatch)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"

        def dispatch() -> None:
            body = self._read_json()
            if path == "/links":
                for key in ("src_id", "dst_id"):
                    if not body.get(key):
                        raise ValidationError(f"{key} 不能为空")
                self.repo.unlink(
                    str(body["src_id"]), str(body["dst_id"]),
                    EdgeKind(str(body.get("kind", "references"))),
                    str(body.get("actor") or "system"))
                self._send(HTTPStatus.NO_CONTENT, {})
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "未知路由"})

        self._handle(dispatch)

    # ---- 动作分发 ----------------------------------------------------

    def _demand_action(self, demand_id: str, action: str, body: dict,
                       actor: str) -> None:
        if action == "submit":
            self._send(HTTPStatus.OK,
                       self.repo.submit_for_review(demand_id, actor))
        elif action == "reject":
            self._send(HTTPStatus.OK, self.repo.reject(
                demand_id, actor, str(body.get("reason", ""))))
        elif action == "confirm":
            added = self.repo.confirm(
                demand_id, str(body.get("party", "")),
                str(body.get("confirmer", "")))
            self._send(HTTPStatus.OK, {"added": added})
        elif action == "publish":
            self._send(HTTPStatus.OK, self.repo.publish(demand_id, actor))
        else:
            reason = str(body.get("reason", ""))
            overrides = self._overrides(body)
            if action == "correct":
                self._send(HTTPStatus.CREATED, self.repo.correct(
                    demand_id, actor, reason, **overrides))
            elif action == "withdraw":
                self._send(HTTPStatus.CREATED,
                           self.repo.withdraw(demand_id, actor, reason))
            else:  # restore
                self._send(HTTPStatus.CREATED, self.repo.restore(
                    demand_id, actor, reason, **overrides))

    @staticmethod
    def _overrides(body: dict) -> dict:
        overrides: dict = {}
        if "title" in body:
            overrides["title"] = str(body["title"])
        if "content" in body:
            overrides["content"] = str(body["content"])
        if "region" in body:
            overrides["region"] = str(body["region"])
        if "publisher" in body:
            overrides["publisher"] = str(body["publisher"])
        if "valid_from" in body:
            overrides["valid_from"] = _parse_date(body["valid_from"],
                                                  "valid_from")
        if "valid_to" in body:
            overrides["valid_to"] = _parse_date(body["valid_to"], "valid_to",
                                                required=False)
        if "evidence" in body:
            overrides["evidence"] = _evidence(body)
        return overrides


def create_server(db_path: str = ":memory:", host: str = "127.0.0.1",
                  port: int = 8080) -> ThreadingHTTPServer:
    store = EventStore(db_path)
    repo = DemandRepository(store)
    handler = type("BoundApiHandler", (ApiHandler,), {"repo": repo})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.demand_repo = repo          # type: ignore[attr-defined]
    server.event_store = store         # type: ignore[attr-defined]
    return server


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = create_server(db_path, host, port)
    print(f"产教需求版本库服务运行于 http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
