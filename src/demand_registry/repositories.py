"""仓储实现：内存版与 SQLite 版。

SQLite 版使用 ``BEGIN IMMEDIATE`` 把所有写事务串行化，配合
``version`` 乐观锁列，保证服务层的"检查—写入"序列原子执行：
并发发布、并发确认、并发更正都不会突破历史一致性。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .errors import ConflictError, NotFoundError
from .models import (
    ApplicableRegion,
    Confirmation,
    DemandLink,
    DemandVersion,
    Evidence,
    Party,
    ReferenceTarget,
    RelationKind,
    TargetEdge,
    VersionState,
)

# ---------------------------------------------------------------------------
# 公共序列化辅助
# ---------------------------------------------------------------------------


def _parse_dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def dump_version(version: DemandVersion) -> dict:
    snap = version.snapshot()
    return {
        **snap,
        "regions_json": json.dumps(snap["regions"], ensure_ascii=False),
        "evidence_json": json.dumps(snap["evidence"], ensure_ascii=False),
    }


def hydrate_version(row: dict) -> DemandVersion:
    regions = [
        ApplicableRegion(code=item["code"], name=item["name"])
        for item in json.loads(row["regions_json"])
    ]
    evidence = [
        Evidence(
            source=item["source"],
            reference=item["reference"],
            content_hash=item["content_hash"],
            captured_at=datetime.fromisoformat(item["captured_at"]),
        )
        for item in json.loads(row["evidence_json"])
    ]
    return DemandVersion(
        version_id=row["version_id"],
        demand_code=row["demand_code"],
        revision=row["revision"],
        title=row["title"],
        competency_statement=row["competency_statement"],
        publisher=row["publisher"],
        enterprise=row["enterprise"],
        regions=regions,
        evidence=evidence,
        valid_from=_parse_dt(row["valid_from"]),
        valid_until=_parse_dt(row["valid_until"]),
        state=VersionState(row["state"]),
        supersedes_id=row["supersedes_id"],
        version=row["version"],
        created_at=_parse_dt(row["created_at"]),
        published_at=_parse_dt(row["published_at"]),
        withdrawn_at=_parse_dt(row["withdrawn_at"]),
    )


# ---------------------------------------------------------------------------
# 内存仓储
# ---------------------------------------------------------------------------


class InMemoryRepository:
    """线程安全的内存实现，用于测试与无依赖演示。"""

    def __init__(self) -> None:
        self._versions: dict[str, DemandVersion] = {}
        self._confirmations: dict[str, dict[Party, Confirmation]] = {}
        self._targets: dict[str, ReferenceTarget] = {}
        self._links: dict[str, DemandLink] = {}
        self._edges: dict[str, TargetEdge] = {}
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:  # 写事务串行化
            yield

    def next_revision(self, demand_code: str) -> int:
        used = [
            v.revision
            for v in self._versions.values()
            if v.demand_code == demand_code
        ]
        return (max(used) if used else 0) + 1

    def save_version(
        self, version: DemandVersion, expected_version: int | None = None
    ) -> None:
        current = self._versions.get(version.version_id)
        if current is None:
            self._versions[version.version_id] = version
            return
        if expected_version is not None and current.version != expected_version:
            raise ConflictError("乐观锁冲突：版本已被其他事务修改")
        self._versions[version.version_id] = version

    def get_version(self, version_id: str) -> DemandVersion:
        try:
            return self._versions[version_id]
        except KeyError:
            raise NotFoundError(f"需求版本不存在：{version_id}") from None

    def list_versions(self, demand_code: str | None = None) -> list[DemandVersion]:
        items = list(self._versions.values())
        if demand_code is not None:
            items = [v for v in items if v.demand_code == demand_code]
        return sorted(items, key=lambda v: (v.created_at, v.revision))

    def find_active_by_demand_code(self, demand_code: str) -> DemandVersion | None:
        actives = [
            v
            for v in self._versions.values()
            if v.demand_code == demand_code and v.state is VersionState.PUBLISHED
        ]
        if len(actives) > 1:  # 兜底：绝不让不变量静默被破坏
            raise ConflictError(f"需求线 {demand_code} 出现多个生效版本")
        return actives[0] if actives else None

    def find_successor(self, version_id: str) -> DemandVersion | None:
        successors = [
            v
            for v in self._versions.values()
            if v.supersedes_id == version_id
        ]
        return successors[0] if successors else None

    def add_confirmation(self, confirmation: Confirmation) -> None:
        bucket = self._confirmations.setdefault(confirmation.version_id, {})
        if confirmation.party in bucket:
            raise ConflictError(
                f"{confirmation.party.value}方确认已存在，不能重复确认"
            )
        bucket[confirmation.party] = confirmation

    def get_confirmations(self, version_id: str) -> list[Confirmation]:
        return list(self._confirmations.get(version_id, {}).values())

    def save_target(self, target: ReferenceTarget) -> None:
        self._targets[target.target_id] = target

    def get_target(self, target_id: str) -> ReferenceTarget:
        try:
            return self._targets[target_id]
        except KeyError:
            raise NotFoundError(f"培养方案对象不存在：{target_id}") from None

    def list_targets(self) -> list[ReferenceTarget]:
        return list(self._targets.values())

    def add_demand_link(self, link: DemandLink) -> None:
        for existing in self._links.values():
            if (
                existing.version_id == link.version_id
                and existing.target_id == link.target_id
            ):
                raise ConflictError("该关联已存在")
        self._links[link.link_id] = link

    def demand_links_for_version(self, version_id: str) -> list[DemandLink]:
        return [
            link
            for link in self._links.values()
            if link.version_id == version_id
        ]

    def demand_links_for_target(self, target_id: str) -> list[DemandLink]:
        return [
            link for link in self._links.values() if link.target_id == target_id
        ]

    def add_target_edge(self, edge: TargetEdge) -> None:
        for existing in self._edges.values():
            if (
                existing.from_target_id == edge.from_target_id
                and existing.to_target_id == edge.to_target_id
            ):
                raise ConflictError("该依赖边已存在")
        self._edges[edge.edge_id] = edge

    def target_edges(self) -> list[TargetEdge]:
        return list(self._edges.values())


# ---------------------------------------------------------------------------
# SQLite 仓储
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demand_versions (
    version_id            TEXT PRIMARY KEY,
    demand_code           TEXT NOT NULL,
    revision              INTEGER NOT NULL,
    title                 TEXT NOT NULL,
    competency_statement  TEXT NOT NULL,
    publisher             TEXT NOT NULL,
    enterprise            TEXT NOT NULL,
    regions_json          TEXT NOT NULL,
    evidence_json         TEXT NOT NULL,
    valid_from            TEXT NOT NULL,
    valid_until           TEXT,
    state                 TEXT NOT NULL,
    supersedes_id         TEXT,
    version               INTEGER NOT NULL,
    created_at            TEXT NOT NULL,
    published_at          TEXT,
    withdrawn_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_versions_demand_code
    ON demand_versions(demand_code);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_line_active
    ON demand_versions(demand_code)
    WHERE state = '发布';

CREATE TABLE IF NOT EXISTS confirmations (
    version_id   TEXT NOT NULL,
    party        TEXT NOT NULL,
    confirmer    TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (version_id, party)
);

CREATE TABLE IF NOT EXISTS reference_targets (
    target_id TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS demand_links (
    link_id    TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (version_id, target_id)
);

CREATE TABLE IF NOT EXISTS target_edges (
    edge_id        TEXT PRIMARY KEY,
    from_target_id TEXT NOT NULL,
    to_target_id   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (from_target_id, to_target_id)
);
"""

_VERSION_COLUMNS = (
    "version_id", "demand_code", "revision", "title", "competency_statement",
    "publisher", "enterprise", "regions_json", "evidence_json",
    "valid_from", "valid_until", "state", "supersedes_id", "version",
    "created_at", "published_at", "withdrawn_at",
)


class SqliteRepository:
    """基于标准库 sqlite3 的持久化实现（无第三方依赖）。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._depth = threading.local()
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,  # 显式事务控制
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """立即获取写锁，把并发写事务整体串行化；支持同线程嵌套（SAVEPOINT）。"""
        depth = getattr(self._depth, "value", 0)
        with self._lock:
            if depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            else:
                self._conn.execute(f"SAVEPOINT sp_{depth}")
            self._depth.value = depth + 1
            try:
                yield
            except Exception:
                if depth == 0:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO sp_{depth}")
                    self._conn.execute(f"RELEASE sp_{depth}")
                raise
            else:
                if depth == 0:
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute(f"RELEASE sp_{depth}")
            finally:
                self._depth.value = depth

    def next_revision(self, demand_code: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision "
            "FROM demand_versions WHERE demand_code = ?",
            (demand_code,),
        ).fetchone()
        return int(row["next_revision"])

    def save_version(
        self, version: DemandVersion, expected_version: int | None = None
    ) -> None:
        data = dump_version(version)
        existing = self._conn.execute(
            "SELECT version FROM demand_versions WHERE version_id = ?",
            (version.version_id,),
        ).fetchone()
        if existing is None:
            placeholders = ", ".join("?" for _ in _VERSION_COLUMNS)
            self._conn.execute(
                f"INSERT INTO demand_versions "
                f"({', '.join(_VERSION_COLUMNS)}) VALUES ({placeholders})",
                [data[name] for name in _VERSION_COLUMNS],
            )
            return
        if expected_version is not None and existing["version"] != expected_version:
            raise ConflictError("乐观锁冲突：版本已被其他事务修改")
        try:
            self._conn.execute(
                """
                UPDATE demand_versions SET
                    demand_code = ?, revision = ?, title = ?,
                    competency_statement = ?, publisher = ?, enterprise = ?,
                    regions_json = ?, evidence_json = ?,
                    valid_from = ?, valid_until = ?, state = ?,
                    supersedes_id = ?, version = ?, created_at = ?,
                    published_at = ?, withdrawn_at = ?
                WHERE version_id = ?
                """,
                (
                    data["demand_code"], data["revision"], data["title"],
                    data["competency_statement"], data["publisher"],
                    data["enterprise"], data["regions_json"],
                    data["evidence_json"], data["valid_from"],
                    data["valid_until"], data["state"], data["supersedes_id"],
                    data["version"], data["created_at"], data["published_at"],
                    data["withdrawn_at"], data["version_id"],
                ),
            )
        except sqlite3.IntegrityError as exc:  # 唯一生效索引
            raise ConflictError(
                f"需求线 {version.demand_code} 已存在生效版本"
            ) from exc

    def get_version(self, version_id: str) -> DemandVersion:
        row = self._conn.execute(
            f"SELECT {', '.join(_VERSION_COLUMNS)} FROM demand_versions "
            "WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"需求版本不存在：{version_id}")
        return hydrate_version(dict(row))

    def list_versions(self, demand_code: str | None = None) -> list[DemandVersion]:
        sql = (
            f"SELECT {', '.join(_VERSION_COLUMNS)} FROM demand_versions"
        )
        params: list = []
        if demand_code is not None:
            sql += " WHERE demand_code = ?"
            params.append(demand_code)
        sql += " ORDER BY created_at, revision"
        return [hydrate_version(dict(r)) for r in self._conn.execute(sql, params)]

    def find_active_by_demand_code(self, demand_code: str) -> DemandVersion | None:
        row = self._conn.execute(
            f"SELECT {', '.join(_VERSION_COLUMNS)} FROM demand_versions "
            "WHERE demand_code = ? AND state = '发布' LIMIT 1",
            (demand_code,),
        ).fetchone()
        return hydrate_version(dict(row)) if row else None

    def find_successor(self, version_id: str) -> DemandVersion | None:
        row = self._conn.execute(
            f"SELECT {', '.join(_VERSION_COLUMNS)} FROM demand_versions "
            "WHERE supersedes_id = ? LIMIT 1",
            (version_id,),
        ).fetchone()
        return hydrate_version(dict(row)) if row else None

    def add_confirmation(self, confirmation: Confirmation) -> None:
        try:
            self._conn.execute(
                "INSERT INTO confirmations "
                "(version_id, party, confirmer, confirmed_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    confirmation.version_id,
                    confirmation.party.value,
                    confirmation.confirmer,
                    confirmation.confirmed_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该方确认已存在，不能重复确认") from exc

    def get_confirmations(self, version_id: str) -> list[Confirmation]:
        rows = self._conn.execute(
            "SELECT version_id, party, confirmer, confirmed_at "
            "FROM confirmations WHERE version_id = ? ORDER BY confirmed_at",
            (version_id,),
        ).fetchall()
        return [
            Confirmation(
                version_id=r["version_id"],
                party=Party(r["party"]),
                confirmer=r["confirmer"],
                confirmed_at=datetime.fromisoformat(r["confirmed_at"]),
            )
            for r in rows
        ]

    def save_target(self, target: ReferenceTarget) -> None:
        self._conn.execute(
            "INSERT INTO reference_targets (target_id, kind, name) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(target_id) DO UPDATE SET kind = excluded.kind, "
            "name = excluded.name",
            (target.target_id, target.kind.value, target.name),
        )

    def get_target(self, target_id: str) -> ReferenceTarget:
        row = self._conn.execute(
            "SELECT target_id, kind, name FROM reference_targets "
            "WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"培养方案对象不存在：{target_id}")
        return ReferenceTarget(
            target_id=row["target_id"],
            kind=RelationKind(row["kind"]),
            name=row["name"],
        )

    def list_targets(self) -> list[ReferenceTarget]:
        rows = self._conn.execute(
            "SELECT target_id, kind, name FROM reference_targets"
        ).fetchall()
        return [
            ReferenceTarget(
                target_id=r["target_id"],
                kind=RelationKind(r["kind"]),
                name=r["name"],
            )
            for r in rows
        ]

    def add_demand_link(self, link: DemandLink) -> None:
        try:
            self._conn.execute(
                "INSERT INTO demand_links "
                "(link_id, version_id, target_id, kind, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    link.link_id, link.version_id, link.target_id,
                    link.kind.value, link.note, link.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该关联已存在") from exc

    def demand_links_for_version(self, version_id: str) -> list[DemandLink]:
        return self._load_links("WHERE version_id = ?", (version_id,))

    def demand_links_for_target(self, target_id: str) -> list[DemandLink]:
        return self._load_links("WHERE target_id = ?", (target_id,))

    def _load_links(self, where: str, params: tuple) -> list[DemandLink]:
        rows = self._conn.execute(
            "SELECT link_id, version_id, target_id, kind, note, created_at "
            f"FROM demand_links {where}",
            params,
        ).fetchall()
        return [
            DemandLink(
                link_id=r["link_id"],
                version_id=r["version_id"],
                target_id=r["target_id"],
                kind=RelationKind(r["kind"]),
                note=r["note"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def add_target_edge(self, edge: TargetEdge) -> None:
        try:
            self._conn.execute(
                "INSERT INTO target_edges "
                "(edge_id, from_target_id, to_target_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    edge.edge_id, edge.from_target_id, edge.to_target_id,
                    edge.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该依赖边已存在") from exc

    def target_edges(self) -> list[TargetEdge]:
        rows = self._conn.execute(
            "SELECT edge_id, from_target_id, to_target_id, created_at "
            "FROM target_edges"
        ).fetchall()
        return [
            TargetEdge(
                edge_id=r["edge_id"],
                from_target_id=r["from_target_id"],
                to_target_id=r["to_target_id"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
