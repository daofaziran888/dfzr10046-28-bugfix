"""维度快照领域服务：冻结校验维度全量状态，产出快照版本数据集。

快照候选清单只接收拉链加工的合法结果（加工中/已闭链）；已作废、区间冲突的拉链
不得进入候选，也不能据其开单。已受理但未完成的在途开单若遇来源作废，自动退回，
快照操作者据此知道哪笔开单还能确认、哪笔必须回到来源记录重选。
"""
from __future__ import annotations

import threading
from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 可作为维度快照来源的合法拉链状态；终态作废与区间冲突均不合法
LEGAL_CHAIN_STATUSES = ("加工中", "已闭链")
# 只有合法闭链结果才允许开单
ORDERABLE_CHAIN_STATUSES = ("已闭链",)
ORDER_ACTIVE = "已受理"
ORDER_CONFIRMED = "已确认"
ORDER_RETURNED = "已退回"

# 开单登记簿为模块级共享状态：路由、快照服务、拉链作废联动拿到的是同一份在途开单
_ORDERS: list[dict[str, Any]] = []
_ORDER_SEQ = 0
_ORDER_LOCK = threading.RLock()


class DimSnapshotService(BaseModuleService):
    MODULE = "dim_snapshot"
    ENTITY = "快照版本"
    LIST_FIELDS = ["快照编号", "所属维度", "快照日期", "维度行数", "当前链占比", "快照摘要值", "引用批次", "生成时间"]
    REQUIRED_FIELDS = ["快照编号", "所属维度", "快照日期"]
    STATUS_ORDER = ["生成中", "可用", "已过期", "已重建"]
    ACTION_RULES = {"标记可用": "可用", "标记过期": "已过期", "重建快照": "已重建"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        super().__init__()
        # 兼容测试复位入口，真实状态保存在模块级寄存器
        self._orders = _ORDERS
        self._order_lock = _ORDER_LOCK

    def _next_order_seq(self) -> int:
        global _ORDER_SEQ
        _ORDER_SEQ += 1
        return _ORDER_SEQ

    # ------------------------------------------------------------------
    # 候选清单：只同步拉链的合法结果
    # ------------------------------------------------------------------
    def chain_candidates(self, dim_name: str) -> dict[str, Any]:
        """返回某维度当前可用于快照的合法候选与被剔除记录及原因。"""
        scd_rows = store.rows("scd_chain")
        candidates: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for row in scd_rows:
            if row.get("所属维度") != dim_name:
                continue
            status = row.get("status")
            if status not in LEGAL_CHAIN_STATUSES:
                reason = "来源拉链已作废" if status == "已作废" else f"来源拉链状态为「{status}」"
                excluded.append({
                    "chain_id": row.get("拉链编号"),
                    "status": status,
                    "interval": f"{row.get('生效起日', '')}~{row.get('生效止日', '')}",
                    "reason": reason,
                })
                continue
            candidates.append({
                "chain_id": row.get("拉链编号"),
                "surrogate_key": row.get("代理键"),
                "status": status,
                "interval": f"{row.get('生效起日', '')}~{row.get('生效止日', '')}",
                "is_current": row.get("是否当前"),
                "orderable": status in ORDERABLE_CHAIN_STATUSES,
                "active_order": self._active_order_id(row.get("拉链编号")),
            })
        return {"dim_name": dim_name, "candidates": candidates, "excluded": excluded}

    def _active_order_id(self, chain_id: str) -> str | None:
        for order in self._orders:
            if order.get("chain_id") == chain_id and order.get("status") == ORDER_ACTIVE:
                return order.get("order_id")
        return None

    def _existing_order(
        self, snapshot_id: str, chain_id: str,
    ) -> dict[str, Any] | None:
        """同快照同来源的未退回开单（已受理/已确认）；已退回的允许重选再开。"""
        for order in self._orders:
            if (
                order.get("snapshot_id") == snapshot_id
                and order.get("chain_id") == chain_id
                and order.get("status") != ORDER_RETURNED
            ):
                return order
        return None

    def freeze_snapshot(
        self, snapshot_id: str, dim_name: str, snapshot_date: str,
        ref_batch: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """冻结维度快照：只统计合法拉链，记录候选与剔除清单。"""
        board = self.chain_candidates(dim_name)
        dim_rows = [
            r for r in store.rows("scd_chain")
            if r.get("所属维度") == dim_name and r.get("status") in LEGAL_CHAIN_STATUSES
        ]
        total = len(dim_rows)
        current_count = sum(1 for r in dim_rows if r.get("是否当前") == "是")
        ratio = current_count / total if total > 0 else 0.0

        import hashlib
        checksum = hashlib.md5(
            str(sorted(r.get("代理键", "") for r in dim_rows)).encode()
        ).hexdigest()[:8]

        rows = store.rows(self.MODULE)
        entry = {
            "快照编号": snapshot_id, "所属维度": dim_name,
            "快照日期": snapshot_date, "维度行数": total,
            "当前链占比": f"{ratio:.1%}", "快照摘要值": checksum,
            "引用批次": ref_batch, "生成时间": snapshot_date,
            "status": "可用", "pending": False, "abnormal": False,
            "candidate_chain_ids": [c["chain_id"] for c in board["candidates"]],
            "excluded_chain_ids": [c["chain_id"] for c in board["excluded"]],
        }
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        return True, f"维度 {dim_name} 快照已生成：{total} 行合法拉链", entry

    def verify_snapshot_consistency(
        self, dim_name: str, snapshot_date: str,
    ) -> tuple[bool, str, int]:
        """验证同一快照日期的维度行数一致性。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("所属维度") == dim_name and r.get("快照日期") == snapshot_date:
                count = int(r.get("维度行数", 0))
                return True, f"快照一致：{count} 行", count
        return False, f"维度 {dim_name} 无 {snapshot_date} 快照", 0

    # ------------------------------------------------------------------
    # 在途开单：只能据合法闭链结果开单，来源作废即退回
    # ------------------------------------------------------------------
    def _find_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("快照编号") == snapshot_id:
                return row
        return None

    def _find_chain(self, chain_id: str) -> dict[str, Any] | None:
        for row in store.rows("scd_chain"):
            if row.get("拉链编号") == chain_id:
                return row
        return None

    def accept_order(
        self, snapshot_id: str, chain_id: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """快照页受理开单：校验快照可用、拉链为同维度合法闭链结果且无在途开单。"""
        with self._order_lock:
            snapshot = self._find_snapshot(snapshot_id)
            if snapshot is None:
                return False, f"快照 {snapshot_id} 不存在", None
            if snapshot.get("status") != "可用":
                return False, f"快照 {snapshot_id} 状态为「{snapshot.get('status')}」，不可受理开单", None
            chain = self._find_chain(chain_id)
            if chain is None:
                return False, f"拉链 {chain_id} 不存在", None
            if chain.get("所属维度") != snapshot.get("所属维度"):
                return False, (
                    f"拉链 {chain_id} 归属「{chain.get('所属维度')}」与快照归属"
                    f"「{snapshot.get('所属维度')}」不一致"
                ), None
            if chain.get("status") not in ORDERABLE_CHAIN_STATUSES:
                return False, (
                    f"拉链 {chain_id} 当前状态为「{chain.get('status')}」，"
                    "不是合法闭链结果，不能据此开单"
                ), None
            existing = self._existing_order(snapshot_id, chain_id)
            if existing is not None:
                if existing.get("status") == ORDER_CONFIRMED:
                    return False, (
                        f"拉链 {chain_id} 在快照 {snapshot_id} 下已有确认开单 "
                        f"{existing.get('order_id')}，不得重复建档"
                    ), None
                return False, (
                    f"拉链 {chain_id} 已有在途开单 {existing.get('order_id')}"
                ), None

            seq = self._next_order_seq()
            order = {
                "order_id": f"ORD-{seq:04d}",
                "snapshot_id": snapshot_id,
                "chain_id": chain_id,
                "dim_name": snapshot.get("所属维度"),
                "interval": f"{chain.get('生效起日', '')}~{chain.get('生效止日', '')}",
                "status": ORDER_ACTIVE,
                "accepted_at": date.today().isoformat(),
                "return_reason": "",
            }
            self._orders.append(order)
            return True, f"开单 {order['order_id']} 已受理（来源 {chain_id}）", dict(order)

    def confirm_order(self, order_id: str) -> tuple[bool, str, dict[str, Any] | None]:
        """确认开单：来源仍合法才确认；已退回的开单必须回来源记录重选。"""
        with self._order_lock:
            order = self._find_order(order_id)
            if order is None:
                return False, f"开单 {order_id} 不存在", None
            if order["status"] == ORDER_CONFIRMED:
                return False, f"开单 {order_id} 已确认，无需重复确认", dict(order)
            chain = self._find_chain(order["chain_id"])
            source_valid = chain is not None and chain.get("status") in ORDERABLE_CHAIN_STATUSES
            if order["status"] == ORDER_RETURNED or not source_valid:
                reason = order.get("return_reason") or (
                    f"来源拉链 {order['chain_id']} 已不是合法闭链结果"
                )
                if order["status"] != ORDER_RETURNED:
                    order["status"] = ORDER_RETURNED
                    order["return_reason"] = reason
                return False, (
                    f"开单 {order_id} 不能确认：{reason}，请回到拉链 {order['chain_id']} 处理后重选"
                ), dict(order)
            order["status"] = ORDER_CONFIRMED
            return True, f"开单 {order_id} 已确认", dict(order)

    def reject_order(
        self, order_id: str, reason: str = "操作者退回重选",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """退回开单：记录退回原因与应回到的来源拉链。"""
        with self._order_lock:
            order = self._find_order(order_id)
            if order is None:
                return False, f"开单 {order_id} 不存在", None
            if order["status"] == ORDER_CONFIRMED:
                return False, f"开单 {order_id} 已确认，不能退回", dict(order)
            order["status"] = ORDER_RETURNED
            order["return_reason"] = reason
            return True, (
                f"开单 {order_id} 已退回，请回到拉链 {order['chain_id']} 重选"
            ), dict(order)

    def invalidate_chain(self, chain_id: str) -> list[dict[str, Any]]:
        """来源拉链作废时联动：在途开单退回，已确认开单保持完成态并留痕。"""
        bounced: list[dict[str, Any]] = []
        with self._order_lock:
            for order in self._orders:
                if order.get("chain_id") == chain_id and order.get("status") == ORDER_ACTIVE:
                    order["status"] = ORDER_RETURNED
                    order["return_reason"] = "来源拉链已作废"
                    bounced.append(dict(order))
        return bounced

    def _find_order(self, order_id: str) -> dict[str, Any] | None:
        for order in self._orders:
            if order.get("order_id") == order_id:
                return order
        return None

    def snapshot_board(self) -> list[dict[str, Any]]:
        """快照操作台：每个快照附候选清单、剔除清单和开单可确认性。"""
        boards: list[dict[str, Any]] = []
        for snapshot in store.rows(self.MODULE):
            dim_name = snapshot.get("所属维度", "")
            board = self.chain_candidates(dim_name)
            orders = [dict(o) for o in self._orders if o.get("snapshot_id") == snapshot.get("快照编号")]
            for order in orders:
                chain = self._find_chain(order["chain_id"])
                order["source_status"] = chain.get("status") if chain else "记录缺失"
                order["can_confirm"] = (
                    order["status"] == ORDER_ACTIVE
                    and chain is not None
                    and chain.get("status") in ORDERABLE_CHAIN_STATUSES
                )
                order["return_to_chain"] = (
                    order["chain_id"] if order["status"] == ORDER_RETURNED else ""
                )
            boards.append({
                "snapshot": dict(snapshot),
                "candidates": board["candidates"],
                "excluded": board["excluded"],
                "orders": orders,
            })
        return boards
