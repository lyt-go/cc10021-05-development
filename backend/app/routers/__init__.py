"""业务路由汇总：通用资源路由之外，聚合类模块挂自己的业务路由。"""
from __future__ import annotations

from app.routers import incident_agg, resource

# incident_agg 的固定路径必须排在通用 /api/{module} 之前注册。
ROUTERS = [incident_agg, resource]
