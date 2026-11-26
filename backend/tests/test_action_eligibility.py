"""动作资格与快照开单链路测试：作废复活防护、并发提交、来源作废退回。"""
from __future__ import annotations

import threading

import pytest

from app.seed import SEED_ROWS
from app.services.dim_snapshot import DimSnapshotService
from app.services.scd_chain import ScdChainService
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    """每个用例恢复种子数据与开单登记簿，避免模块级单例相互污染。"""
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    from app.services import dim_snapshot as dim_snapshot_module
    dim_snapshot_module._ORDERS.clear()
    dim_snapshot_module._ORDER_SEQ = 0
    yield


class TestActionEligibility:
    def test_voided_record_has_no_actions(self):
        scd = ScdChainService()
        # SCD-005 种子状态为已作废
        entry, message = scd.run_action(5, "闭链记录")
        assert entry is None
        assert "不具备动作" in message
        assert store.find("scd_chain", 5)["status"] == "已作废"

    def test_close_chain_record_blocks_voided(self):
        scd = ScdChainService()
        ok, message, rec = scd.close_chain_record("SCD-005", "2026-12-01")
        assert not ok
        assert "不具备动作" in message
        assert rec is None

    def test_stale_window_cannot_revive_voided_record(self):
        """模拟旧窗口：先作废，再拿旧状态页面提交闭链，状态不得变回已闭链。"""
        scd = ScdChainService()
        entry, message = scd.run_action(1, "作废记录")
        assert entry is not None and entry["status"] == "已作废"

        stale, stale_msg = scd.run_action(1, "闭链记录", remark="2026-12-01")
        assert stale is None
        assert "已作废" in stale_msg
        assert store.find("scd_chain", 1)["status"] == "已作废"

        # 作废是终态，任何动作（含重复作废）都被拒绝
        again, _ = scd.run_action(1, "作废记录")
        assert again is None

    def test_closed_chain_cannot_close_again(self):
        scd = ScdChainService()
        # SCD-002 种子已闭链
        entry, message = scd.run_action(2, "闭链记录", remark="2026-12-01")
        assert entry is None
        assert "不具备动作" in message
        assert store.find("scd_chain", 2)["status"] == "已闭链"

    def test_legal_close_and_conflict_actions_still_work(self):
        scd = ScdChainService()
        # 加工中记录可标记冲突，冲突修正后仍可合法闭链
        conflict, _ = scd.run_action(1, "标记冲突", remark="测试冲突")
        assert conflict is not None and conflict["status"] == "区间冲突"
        fixed, _ = scd.run_action(1, "闭链记录", remark="2026-10-15")
        assert fixed is not None
        assert fixed["status"] == "已闭链"
        assert fixed["生效止日"] == "2026-10-15"
        # 已闭链后仍可作废（兼容既有合法动作的逆向处置），作废即终态
        voided, _ = scd.run_action(1, "作废记录")
        assert voided is not None and voided["status"] == "已作废"

    def test_list_payload_carries_eligible_actions(self):
        scd = ScdChainService()
        items, _ = scd.list_entries()
        by_id = {row["拉链编号"]: row for row in items}
        assert "闭链记录" in by_id["SCD-001"]["eligible_actions"]
        assert by_id["SCD-005"]["eligible_actions"] == []
        assert by_id["SCD-002"]["eligible_actions"] == ["作废记录"]

    def test_unknown_action_rejected(self):
        scd = ScdChainService()
        entry, message = scd.run_action(1, "不存在的动作")
        assert entry is None
        assert "可执行范围" in message

    def test_concurrent_void_and_close_only_one_wins(self):
        """两种动作并发提交：每个请求按提交瞬间的资格裁决，不出现非法终态反转。

        作废先到则闭链必败、终态保持作废；闭链先到则闭链生效，作废作为合法后续
        动作仍可执行，最终停在作废。无论哪种交错，已作废对象都不会显示成已闭链。
        """
        scd = ScdChainService()
        outcomes: list[tuple[str, dict | None]] = []
        barrier = threading.Barrier(2)

        def do_void():
            barrier.wait()
            entry, _ = scd.run_action(1, "作废记录")
            outcomes.append(("void", entry))

        def do_close():
            barrier.wait()
            entry, _ = scd.run_action(1, "闭链记录", remark="2026-12-01")
            outcomes.append(("close", entry))

        t1 = threading.Thread(target=do_void)
        t2 = threading.Thread(target=do_close)
        t1.start(); t2.start(); t1.join(); t2.join()

        final_status = store.find("scd_chain", 1)["status"]
        # 锁串行化后最终态只能是作废（闭链先到也会被随后的作废覆盖）
        assert final_status == "已作废"
        void_result = next(entry for name, entry in outcomes if name == "void")
        assert void_result is not None
        # 若作废先抢到锁，闭链必须被资格拦截；若闭链先到，闭链本身合法生效
        close_result = next(entry for name, entry in outcomes if name == "close")
        if close_result is not None:
            assert close_result["status"] == "已闭链"
        else:
            # 闭链被拒时记录从未被改成已闭链
            assert store.find("scd_chain", 1)["是否当前"] == "是"

    def test_other_dimension_records_unaffected(self):
        """单条记录作废不影响其他维度条目的正常动作。"""
        scd = ScdChainService()
        scd.run_action(1, "作废记录")
        # SCD-003 处于区间冲突，修正闭链仍被受理
        entry, _ = scd.run_action(3, "闭链记录", remark="2026-12-01")
        assert entry is not None and entry["status"] == "已闭链"
        assert store.find("scd_chain", 1)["status"] == "已作废"


class TestSnapshotCandidates:
    def test_candidates_exclude_voided_and_conflict(self):
        snap = DimSnapshotService()
        board = snap.chain_candidates("客户维度")
        ids = {c["chain_id"] for c in board["candidates"]}
        # 客户维度 SCD-001(加工中)、SCD-002(已闭链) 合法
        assert {"SCD-001", "SCD-002"} <= ids

        # 区域维度的 SCD-005 已作废，必须出现在剔除清单且带作废原因
        region_board = snap.chain_candidates("区域维度")
        excluded = {e["chain_id"]: e for e in region_board["excluded"]}
        assert "SCD-005" in excluded
        assert "已作废" in excluded["SCD-005"]["reason"]
        assert region_board["candidates"] == []

        # 产品维度的 SCD-003 区间冲突，同样不得进入候选
        product_board = snap.chain_candidates("产品维度")
        excluded_ids = {e["chain_id"] for e in product_board["excluded"]}
        assert "SCD-003" in excluded_ids
        assert "SCD-003" not in {c["chain_id"] for c in product_board["candidates"]}

    def test_freeze_snapshot_counts_only_legal_chains(self):
        snap = DimSnapshotService()
        ok, _, entry = snap.freeze_snapshot("SNAP-REG", "区域维度", "2026-11-01", "B1")
        assert ok
        # 区域维度只有 SCD-005 且已作废，合法行数为 0 并出现在剔除清单
        assert entry["维度行数"] == 0
        assert entry["excluded_chain_ids"] == ["SCD-005"]
        assert entry["candidate_chain_ids"] == []

    def test_order_requires_closed_chain(self):
        snap = DimSnapshotService()
        # 加工中拉链不可开单
        ok, message, order = snap.accept_order("SNAP-CUST-20261001", "SCD-001")
        assert not ok and order is None
        assert "不是合法闭链结果" in message

    def test_order_accept_confirm_happy_path(self):
        snap = DimSnapshotService()
        ok, _, order = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        assert ok and order["status"] == "已受理"
        ok2, _, confirmed = snap.confirm_order(order["order_id"])
        assert ok2 and confirmed["status"] == "已确认"
        # 已确认不允许重复确认
        ok3, message, _ = snap.confirm_order(order["order_id"])
        assert not ok3 and "已确认" in message

    def test_order_dim_mismatch_rejected(self):
        snap = DimSnapshotService()
        # SCD-004 属于产品维度，不能挂到客户维度快照
        ok, message, _ = snap.accept_order("SNAP-CUST-20261001", "SCD-004")
        assert not ok
        assert "归属" in message

    def test_duplicate_active_order_rejected(self):
        snap = DimSnapshotService()
        snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        ok, message, _ = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        assert not ok and "在途开单" in message

    def test_confirmed_order_blocks_repeat_archiving(self):
        snap = DimSnapshotService()
        _, _, order = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        snap.confirm_order(order["order_id"])
        ok, message, _ = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        assert not ok and "不得重复建档" in message

    def test_returned_order_allows_reselect(self):
        snap = DimSnapshotService()
        _, _, order = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        snap.reject_order(order["order_id"])
        # 退回后允许对同一来源重新受理
        ok, _, reorder = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        assert ok and reorder["status"] == "已受理"
        ok2, _, confirmed = snap.confirm_order(reorder["order_id"])
        assert ok2 and confirmed["status"] == "已确认"

    def test_source_void_bounces_inflight_order(self):
        """已受理开单未完成时来源作废：开单自动退回且无法确认，需回来源重选。"""
        snap = DimSnapshotService()
        scd = ScdChainService()
        ok, _, order = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        assert ok

        bounced = snap.invalidate_chain("SCD-002")
        assert len(bounced) == 1 and bounced[0]["order_id"] == order["order_id"]
        assert bounced[0]["status"] == "已退回"

        ok2, message, updated = snap.confirm_order(order["order_id"])
        assert not ok2
        assert "不能确认" in message and "SCD-002" in message
        assert updated["status"] == "已退回"

    def test_void_action_cascades_to_snapshot_orders(self):
        """端到端：拉链页作废一条已闭链记录，在途开单随之退回。"""
        snap = DimSnapshotService()
        scd = ScdChainService()
        ok, _, order = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        assert ok

        scd.run_action(2, "作废记录")
        refreshed = next(o for o in snap._orders if o["order_id"] == order["order_id"])
        assert refreshed["status"] == "已退回"
        assert refreshed["return_reason"] == "来源拉链已作废"

    def test_board_shows_confirmability_and_return_target(self):
        snap = DimSnapshotService()
        _, _, order = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        boards = snap.snapshot_board()
        board = next(b for b in boards
                     if b["snapshot"]["快照编号"] == "SNAP-CUST-20261001")
        row = next(o for o in board["orders"] if o["order_id"] == order["order_id"])
        assert row["can_confirm"] is True
        assert row["return_to_chain"] == ""

        # 模拟来源作废：在途开单退回，同时拉链进入已作废终态并被候选清单剔除
        snap.invalidate_chain("SCD-002")
        scd = ScdChainService()
        target = next(r for r in store.rows("scd_chain") if r["拉链编号"] == "SCD-002")
        target["status"] = "已作废"
        target["abnormal"] = True
        boards = snap.snapshot_board()
        board = next(b for b in boards
                     if b["snapshot"]["快照编号"] == "SNAP-CUST-20261001")
        row = next(o for o in board["orders"] if o["order_id"] == order["order_id"])
        assert row["can_confirm"] is False
        assert row["return_to_chain"] == "SCD-002"
        # 被作废来源同时出现在剔除清单
        assert any(e["chain_id"] == "SCD-002" for e in board["excluded"])

    def test_reject_order_records_return_target(self):
        snap = DimSnapshotService()
        _, _, order = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        ok, message, returned = snap.reject_order(order["order_id"])
        assert ok and returned["status"] == "已退回"
        assert "SCD-002" in message

    def test_confirm_then_void_keeps_completed_order(self):
        """已完成确认的开单不被后续来源作废推翻，只保留来源现状供操作者辨认。"""
        snap = DimSnapshotService()
        scd = ScdChainService()
        _, _, order = snap.accept_order("SNAP-CUST-20261001", "SCD-002")
        snap.confirm_order(order["order_id"])
        bounced = snap.invalidate_chain("SCD-002")
        assert bounced == []
        refreshed = next(o for o in snap._orders if o["order_id"] == order["order_id"])
        assert refreshed["status"] == "已确认"
