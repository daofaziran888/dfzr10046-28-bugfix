"""拉链加工领域服务：闭链校验与重叠冲突检测，属性失效联动转换，产出拉链记录数据集。"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ScdChainService(BaseModuleService):
    MODULE = "scd_chain"
    ENTITY = "拉链记录"
    LIST_FIELDS = ["拉链编号", "所属维度", "代理键", "变更字段", "生效起日", "生效止日", "是否当前", "链上前序"]
    REQUIRED_FIELDS = ["拉链编号", "所属维度", "代理键"]
    STATUS_ORDER = ["加工中", "已闭链", "区间冲突", "已作废"]
    ACTION_RULES = {"闭链记录": "已闭链", "标记冲突": "区间冲突", "作废记录": "已作废"}
    NEGATIVE_ACTIONS = ["作废记录"]
    # 作废为终态：作废生效后旧页面持有的任何动作入口都不得复活该记录
    TERMINAL_STATUSES = ["已作废"]

    def allowed_actions(self, entry: dict[str, Any]) -> list[str]:
        """拉链动作资格：加工中可闭链/标冲突/作废；冲突可闭链/作废；已闭链仅可作废。"""
        if entry is None or entry.get("status") in self.TERMINAL_STATUSES:
            return []
        status = entry.get("status")
        if status == "加工中":
            return ["闭链记录", "标记冲突", "作废记录"]
        if status == "区间冲突":
            return ["闭链记录", "作废记录"]
        if status == "已闭链":
            return ["作废记录"]
        return []

    def _find_by_chain_id(self, chain_id: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("拉链编号") == chain_id:
                return row
        return None

    def _apply_action(
        self, entry: dict[str, Any], action: str, remark: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        if action == "闭链记录":
            # 通用动作入口承载闭链参数：remark 作为生效止日，日期非法时保持原状态
            end_date = (remark or "").strip() or str(date.today().isoformat())
            ok, message, updated = self.close_chain_record(
                entry.get("拉链编号", ""), end_date, entry.get("链上前序") or None,
            )
            return (updated, message) if ok else (None, message)
        if action == "作废记录":
            entry["status"] = "已作废"
            entry["pending"] = False
            entry["abnormal"] = True
            entry["是否当前"] = "否"
            # 作废生效后，同步通知维度快照：引用该链的在途开单必须退回，候选清单同步剔除
            from app.services.dim_snapshot import DimSnapshotService
            DimSnapshotService().invalidate_chain(entry.get("拉链编号", ""))
            return entry, f"拉链记录已{action}"
        # 标记冲突
        entry["status"] = "区间冲突"
        entry["pending"] = False
        entry["abnormal"] = True
        if remark:
            entry["冲突原因"] = remark
        return entry, f"拉链记录已{action}"

    def close_chain_record(
        self, chain_id: str, end_date: str, predecessor_id: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """闭链操作：先过动作资格，再校验起止日期与链上前序收敛条件。"""
        with self._action_lock:
            target = self._find_by_chain_id(chain_id)
            if target is None:
                return False, f"拉链 {chain_id} 不存在", None
            # 与页面、通用动作接口同一资格判定：已作废/已闭链均不得再闭链
            eligible, reason = self.can_run_action(target, "闭链记录")
            if not eligible:
                return False, reason, None

            try:
                end_dt = date.fromisoformat(end_date)
                start_dt = date.fromisoformat(target.get("生效起日", end_date))
            except ValueError:
                return False, "日期格式无效", None

            if end_dt < start_dt:
                return False, f"生效止日 {end_date} 早于生效起日 {target.get('生效起日')}", None

            if predecessor_id:
                pred = self._find_by_chain_id(predecessor_id)
                if pred is None:
                    return False, f"链上前序 {predecessor_id} 不存在", None
                pred_end = pred.get("生效止日", "")
                if pred_end != target.get("生效起日", ""):
                    return False, (
                        f"前序止日 {pred_end} 与当前起日 {target.get('生效起日')} 不首尾相接"
                    ), None
                target["链上前序"] = predecessor_id

            target["生效止日"] = end_date
            target["是否当前"] = "否"
            target["status"] = "已闭链"
            target["pending"] = False
            target["abnormal"] = False
            return True, f"拉链 {chain_id} 已闭链至 {end_date}", dict(target)

    def detect_overlap_conflicts(self, dim_name: str) -> list[dict[str, Any]]:
        """检测指定维度下的拉链区间重叠冲突。"""
        rows = store.rows(self.MODULE)
        dim_rows = sorted(
            [r for r in rows if r.get("所属维度") == dim_name],
            key=lambda r: r.get("生效起日", ""),
        )
        if len(dim_rows) < 2:
            return []

        conflicts: list[dict[str, Any]] = []
        for i, a in enumerate(dim_rows):
            a_start = a.get("生效起日", "")
            a_end = a.get("生效止日", "")
            for b in dim_rows[i + 1:]:
                b_start = b.get("生效起日", "")
                b_end = b.get("生效止日", "")
                if a_end > b_start and a_start < b_end:
                    conflicts.append({
                        "chain_a": a.get("拉链编号"),
                        "chain_b": b.get("拉链编号"),
                        "a_interval": f"[{a_start}, {a_end}]",
                        "b_interval": f"[{b_start}, {b_end}]",
                        "overlap_start": max(a_start, b_start),
                        "overlap_end": min(a_end, b_end),
                    })
        return conflicts

    def mark_conflict(self, chain_id: str, reason: str) -> bool:
        """标记拉链记录为区间冲突状态；已作废记录不接受标记。"""
        with self._action_lock:
            target = self._find_by_chain_id(chain_id)
            if target is None:
                return False
            eligible, _ = self.can_run_action(target, "标记冲突")
            if not eligible:
                return False
            target["status"] = "区间冲突"
            target["abnormal"] = True
            target["冲突原因"] = reason
            return True

    def apply_attr_invalidation(
        self, dim_name: str, attr_name: str, default_value: str,
    ) -> int:
        """属性失效联动：将指定维度属性失效后回填默认值并标记受影响的拉链。"""
        rows = store.rows(self.MODULE)
        count = 0
        for r in rows:
            if r.get("所属维度") == dim_name:
                r["变更字段"] = f"{attr_name}→{default_value}（属性已失效）"
                count += 1
        return count
