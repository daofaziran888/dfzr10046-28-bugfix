"""维度快照操作台接口：候选清单、在途开单受理/确认/退回。

路由层只做参数接收与服务分派，动作资格与来源合法性全部在领域服务内判定。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.dim_snapshot import DimSnapshotService

router = APIRouter(prefix="/api/dim_snapshot", tags=["维度快照"])


class OrderPayload(BaseModel):
    snapshot_id: str
    chain_id: str


class RejectPayload(BaseModel):
    reason: str | None = None


def _result(ok: bool, message: str, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": ok, "message": message, "entry": entry}


@router.get("/candidates")
def candidates(dim_name: str) -> dict[str, Any]:
    return DimSnapshotService().chain_candidates(dim_name)


@router.get("/board")
def board() -> dict[str, Any]:
    return {"boards": DimSnapshotService().snapshot_board()}


@router.post("/orders")
def accept_order(payload: OrderPayload) -> dict[str, Any]:
    ok, message, order = DimSnapshotService().accept_order(
        payload.snapshot_id.strip(), payload.chain_id.strip())
    return _result(ok, message, order)


@router.post("/orders/{order_id}/confirm")
def confirm_order(order_id: str) -> dict[str, Any]:
    ok, message, order = DimSnapshotService().confirm_order(order_id)
    return _result(ok, message, order)


@router.post("/orders/{order_id}/reject")
def reject_order(order_id: str, payload: RejectPayload) -> dict[str, Any]:
    ok, message, order = DimSnapshotService().reject_order(
        order_id, payload.reason or "操作者退回重选")
    return _result(ok, message, order)
