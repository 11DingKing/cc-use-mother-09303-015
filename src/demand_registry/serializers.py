"""只读序列化：领域对象 → 可返回的 JSON 结构。"""
from __future__ import annotations

from typing import Any

from .models import Confirmation, DemandLink, DemandVersion, ReferenceTarget, TargetEdge


def version_to_json(version: DemandVersion) -> dict[str, Any]:
    data = version.snapshot()
    data["is_effective"] = version.is_effective
    return data


def confirmation_to_json(confirmation: Confirmation) -> dict[str, Any]:
    return {
        "version_id": confirmation.version_id,
        "party": confirmation.party.value,
        "confirmer": confirmation.confirmer,
        "confirmed_at": confirmation.confirmed_at.isoformat(),
    }


def target_to_json(target: ReferenceTarget) -> dict[str, Any]:
    return {
        "target_id": target.target_id,
        "kind": target.kind.value,
        "name": target.name,
    }


def link_to_json(link: DemandLink) -> dict[str, Any]:
    return {
        "link_id": link.link_id,
        "version_id": link.version_id,
        "target_id": link.target_id,
        "kind": link.kind.value,
        "note": link.note,
        "created_at": link.created_at.isoformat(),
    }


def edge_to_json(edge: TargetEdge) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "from_target_id": edge.from_target_id,
        "to_target_id": edge.to_target_id,
        "created_at": edge.created_at.isoformat(),
    }
