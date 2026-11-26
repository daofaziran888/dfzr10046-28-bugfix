"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, snapshot

# 快照固定路径必须排在通用资源路由之前，否则会被 /api/{module} 动态路径抢先匹配
ROUTERS = [snapshot, resource]
