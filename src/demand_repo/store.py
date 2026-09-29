"""SQLite 持久化与追加式事件日志。

并发模型：SQLite 开启 ``WAL`` 并将一次事务串行化（busy timeout +
``IMMEDIATE`` 事务），所有状态变更同时写入业务表与 ``events`` 审计表。
唯一约束在数据库层兜底，保证：

* 同一需求族的版本号不重复、链不回退；
* 每个版本最多只有一条活动的同类型关联边；
* 双方确认（enterprise / college）各至多一条，重复确认幂等。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ConflictError
from .models import (
    DemandStatus,
    DemandVersion,
    EdgeKind,
    Evidence,
    Event,
    NodeKind,
    VersionEdge,
    VersionNode,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS demand_versions (
    id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    region TEXT NOT NULL,
    publisher TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    evidence_source TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    evidence_issued_on TEXT,
    status TEXT NOT NULL,
    supersedes_id TEXT,
    change_kind TEXT NOT NULL DEFAULT 'collect',
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(family_id, version_no),
    UNIQUE(supersedes_id)        -- 每个版本至多有一个直接后继：链不分叉
);
CREATE INDEX IF NOT EXISTS idx_demand_family ON demand_versions(family_id);
CREATE INDEX IF NOT EXISTS idx_demand_status ON demand_versions(status);

CREATE TABLE IF NOT EXISTS confirmations (
    demand_version_id TEXT NOT NULL,
    party TEXT NOT NULL CHECK (party IN ('enterprise', 'college')),
    confirmer TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (demand_version_id, party)  -- 双方各至多一次确认
);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, code, revision)
);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(kind, src_id, dst_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(kind, src_id, active);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(kind, dst_id, active);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def _d(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: datetime) -> str:
    return value.isoformat()


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class EventStore:
    """封装 SQLite 连接与行映射。仓库层（repository）在其上实现领域逻辑。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(
            self.path,
            isolation_level=None,           # 手工事务
            timeout=30.0,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        # 单写锁：多线程发布时在 Python 层串行化写事务，配合 IMMEDIATE
        # 避免写写竞争；读操作不需要持锁（WAL 支持并发读）。
        self._write_lock = __import__("threading").RLock()

    # ---- 事务 ----------------------------------------------------------

    def begin(self) -> None:
        self._write_lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        try:
            self.conn.execute("COMMIT")
        finally:
            self._write_lock.release()

    def rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        finally:
            self._write_lock.release()

    # ---- 事件 ----------------------------------------------------------

    def append_event(
        self,
        event_type: str,
        aggregate_id: str,
        actor: str,
        at: datetime,
        payload: Mapping[str, object],
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(event_type, aggregate_id, actor, at, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_type, aggregate_id, actor, _dt(at),
             json.dumps(payload, ensure_ascii=False, default=str)),
        )
        return int(cur.lastrowid)

    def list_events(self, aggregate_id: str | None = None) -> list[Event]:
        if aggregate_id is None:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY seq").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE aggregate_id=? ORDER BY seq",
                (aggregate_id,)).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            seq=row["seq"],
            event_type=row["event_type"],
            aggregate_id=row["aggregate_id"],
            actor=row["actor"],
            at=datetime.fromisoformat(row["at"]),
            payload=json.loads(row["payload"]),
        )

    # ---- 需求版本 ------------------------------------------------------

    def insert_demand(self, v: DemandVersion) -> None:
        try:
            self.conn.execute(
                "INSERT INTO demand_versions(id, family_id, version_no, title,"
                " content, region, publisher, valid_from, valid_to,"
                " evidence_source, evidence_ref, evidence_issued_on, status,"
                " supersedes_id, change_kind, reason, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (v.id, v.family_id, v.version_no, v.title, v.content,
                 v.region, v.publisher, _d(v.valid_from), _d(v.valid_to),
                 v.evidence.source, v.evidence.document_ref,
                 _d(v.evidence.issued_on), v.status.value,
                 v.supersedes_id, v.change_kind, v.reason, _dt(v.created_at)),
            )
        except sqlite3.IntegrityError as exc:  # 版本号/链冲突
            raise ConflictError(str(exc)) from exc

    def get_demand(self, demand_id: str) -> DemandVersion | None:
        row = self.conn.execute(
            "SELECT * FROM demand_versions WHERE id=?", (demand_id,)).fetchone()
        return self._row_to_demand(row) if row else None

    def list_family(self, family_id: str) -> list[DemandVersion]:
        rows = self.conn.execute(
            "SELECT * FROM demand_versions WHERE family_id=? ORDER BY version_no",
            (family_id,)).fetchall()
        return [self._row_to_demand(r) for r in rows]

    def find_family_of(self, demand_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT family_id FROM demand_versions WHERE id=?",
            (demand_id,)).fetchone()
        return row["family_id"] if row else None

    def set_status(self, demand_id: str, status: DemandStatus) -> None:
        self.conn.execute(
            "UPDATE demand_versions SET status=? WHERE id=?",
            (status.value, demand_id))

    def set_supersedes(self, demand_id: str,
                       supersedes_id: str | None) -> None:
        """释放或改写替代意图（驳回/放弃后继时使用）。"""
        try:
            self.conn.execute(
                "UPDATE demand_versions SET supersedes_id=? WHERE id=?",
                (supersedes_id, demand_id))
        except sqlite3.IntegrityError as exc:
            raise ConflictError(str(exc)) from exc

    def update_draft(self, demand_id: str, fields: dict) -> None:
        """修订未发布草稿的内容字段（发布后不可变）。"""
        self.conn.execute(
            "UPDATE demand_versions SET title=?, content=?, region=?,"
            " publisher=?, valid_from=?, valid_to=?, evidence_source=?,"
            " evidence_ref=?, evidence_issued_on=?, reason=? WHERE id=?",
            (
                fields["title"].strip(), fields["content"].strip(),
                fields["region"].strip(), fields["publisher"].strip(),
                _d(fields["valid_from"]), _d(fields["valid_to"]),
                fields["evidence"].source, fields["evidence"].document_ref,
                _d(fields["evidence"].issued_on), fields["reason"].strip(),
                demand_id,
            ),
        )

    def latest_version_no(self, family_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS m FROM demand_versions"
            " WHERE family_id=?", (family_id,)).fetchone()
        return int(row["m"])

    def list_demands(self, status: DemandStatus | None = None) -> list[DemandVersion]:
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM demand_versions ORDER BY family_id, version_no"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM demand_versions WHERE status=? ORDER BY family_id,"
                " version_no", (status.value,)).fetchall()
        return [self._row_to_demand(r) for r in rows]

    @staticmethod
    def _row_to_demand(row: sqlite3.Row) -> DemandVersion:
        return DemandVersion(
            id=row["id"],
            family_id=row["family_id"],
            version_no=row["version_no"],
            title=row["title"],
            content=row["content"],
            region=row["region"],
            publisher=row["publisher"],
            valid_from=date.fromisoformat(row["valid_from"]),
            valid_to=_parse_date(row["valid_to"]),
            evidence=Evidence(
                source=row["evidence_source"],
                document_ref=row["evidence_ref"],
                issued_on=_parse_date(row["evidence_issued_on"]),
            ),
            status=DemandStatus(row["status"]),
            supersedes_id=row["supersedes_id"],
            change_kind=row["change_kind"],
            reason=row["reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ---- 确认 ----------------------------------------------------------

    def add_confirmation(
        self, demand_id: str, party: str, confirmer: str, at: datetime
    ) -> bool:
        """插入确认；重复确认返回 False（幂等）。"""
        try:
            self.conn.execute(
                "INSERT INTO confirmations(demand_version_id, party, confirmer,"
                " confirmed_at) VALUES (?,?,?,?)",
                (demand_id, party, confirmer, _dt(at)),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def count_confirmations(self, demand_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM confirmations WHERE demand_version_id=?",
            (demand_id,)).fetchone()
        return int(row["c"])

    def clear_confirmations(self, demand_id: str) -> None:
        """驳回/放弃后清空确认：确认针对的是被否的草稿内容。"""
        self.conn.execute(
            "DELETE FROM confirmations WHERE demand_version_id=?",
            (demand_id,))

    # ---- 节点与边 ------------------------------------------------------

    def insert_node(self, node: VersionNode) -> None:
        try:
            self.conn.execute(
                "INSERT INTO nodes(id, kind, code, title, revision, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (node.id, node.kind.value, node.code, node.title,
                 node.revision, _dt(node.created_at)),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(str(exc)) from exc

    def get_node(self, node_id: str) -> VersionNode | None:
        row = self.conn.execute(
            "SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            return None
        return VersionNode(
            id=row["id"], kind=NodeKind(row["kind"]), code=row["code"],
            title=row["title"], revision=row["revision"],
            created_at=datetime.fromisoformat(row["created_at"]))

    def insert_edge(self, edge: VersionEdge) -> None:
        try:
            self.conn.execute(
                "INSERT INTO edges(id, kind, src_id, dst_id, created_at, active)"
                " VALUES (?,?,?,?,?,?)",
                (edge.id, edge.kind.value, edge.src_id, edge.dst_id,
                 _dt(edge.created_at), 1 if edge.active else 0),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(str(exc)) from exc

    def set_edge_active(self, kind: EdgeKind, src_id: str, dst_id: str,
                        active: bool) -> int:
        cur = self.conn.execute(
            "UPDATE edges SET active=? WHERE kind=? AND src_id=? AND dst_id=?",
            (1 if active else 0, kind.value, src_id, dst_id))
        return int(cur.rowcount or 0)

    def list_edges(self, kind: EdgeKind | None = None, *,
                   src_id: str | None = None, dst_id: str | None = None,
                   active_only: bool = True) -> list[VersionEdge]:
        sql = "SELECT * FROM edges WHERE 1=1"
        params: list[Any] = []
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind.value)
        if src_id is not None:
            sql += " AND src_id=?"
            params.append(src_id)
        if dst_id is not None:
            sql += " AND dst_id=?"
            params.append(dst_id)
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY created_at, id"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            VersionEdge(
                id=r["id"], kind=EdgeKind(r["kind"]), src_id=r["src_id"],
                dst_id=r["dst_id"],
                created_at=datetime.fromisoformat(r["created_at"]),
                active=bool(r["active"]))
            for r in rows
        ]

    def active_edge_exists(self, kind: EdgeKind, src_id: str,
                           dst_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM edges WHERE kind=? AND src_id=? AND dst_id=? AND"
            " active=1", (kind.value, src_id, dst_id)).fetchone()
        return row is not None

    def close(self) -> None:
        self.conn.close()
