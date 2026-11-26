"""模块服务基类：共享查询、登记、动作和统计骨架，业务差异留在各模块文件。"""
from __future__ import annotations

import threading
from typing import Any

from app.store import store


class BaseModuleService:
    MODULE = ""
    ENTITY = ""
    LIST_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []
    # 终态：进入后不再接受任何动作，旧页面持有的动作入口到此必须失效
    TERMINAL_STATUSES: list[str] = []

    def __init__(self) -> None:
        # 同一模块的动作提交串行化，保证并发提交时只有一次有效状态流转
        self._action_lock = threading.RLock()

    def allowed_actions(self, entry: dict[str, Any]) -> list[str]:
        """返回记录当前状态下真正具备资格的动作；页面与接口共用同一判定。"""
        if entry is None:
            return []
        status = entry.get("status")
        if status in self.TERMINAL_STATUSES:
            return []
        actions: list[str] = []
        for action, target in self.ACTION_RULES.items():
            # 目标状态与当前相同的动作不重复受理（例如已闭链再次闭链）
            if target != status:
                actions.append(action)
        return actions

    def can_run_action(self, entry: dict[str, Any], action: str) -> tuple[bool, str]:
        """动作资格校验：未知动作、终态记录、目标状态与现状重复一律拒绝。"""
        target = self.ACTION_RULES.get(action)
        if target is None:
            return False, f"动作「{action}」不属于{self.MODULE}可执行范围"
        if action not in self.allowed_actions(entry):
            return False, (
                f"{self.ENTITY}当前状态为「{entry.get('status')}」，"
                f"不具备动作「{action}」资格"
            )
        return True, ""


    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        # 动作资格随清单下发：页面只渲染具备资格的入口，旧页面的失效入口由接口兜底拦截。
        # 用副本承载资格字段，避免把展示态写回仓库。
        page_rows = [dict(row) for row in rows[start:start + size]]
        for row in page_rows:
            row["eligible_actions"] = self.allowed_actions(row)
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def _apply_action(
        self, entry: dict[str, Any], action: str, remark: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        """默认状态流转；模块有领域校验时覆写本方法（如拉链闭链的日期收敛）。"""
        target = self.ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        return entry, f"{self.ENTITY}已{action}"

    def run_action(
        self, entry_id: int, action: str, remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        action = (action or "").strip()
        with self._action_lock:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            eligible, reason = self.can_run_action(entry, action)
            if not eligible:
                return None, reason
            return self._apply_action(entry, action, remark)

    def domain_snapshot(self) -> dict[str, Any]:
        """输出模块级数据质量快照，供跨模块消费和题位源码锚点使用。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
        }
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
        }
