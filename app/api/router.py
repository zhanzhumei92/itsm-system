"""
API 路由总装配
===============================================================================
统一前缀：/api/v1

模块对应关系（与需求文档的 5 大核心模块一一对应）：
  模块1 设备接入与协议采集  → devices / metrics / logs(采集日志)
  模块2 IT 资产管理         → assets
  模块3 监控指标与自定义告警 → metrics / alerts
  模块4 可视化大屏          → dashboard
  模块5 后台管理            → auth / users / configs / system

新增业务模块时的标准动作：
  1. 在 app/api/v1/ 下新建模块，定义 router = APIRouter(prefix="/xxx", tags=[...])
  2. 在本文件 import 并 include_router
  3. 如需权限控制，在路由上用 dependencies=[Depends(require_role(ROLE_ADMIN))]
===============================================================================
"""
from fastapi import APIRouter

from app.api.v1 import (
    alerts,
    assets,
    auth,
    configs,
    dashboard,
    devices,
    logs,
    metrics,
    system,
    users,
)

api_router = APIRouter(prefix="/api/v1")

# 认证与用户
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(system.router)
api_router.include_router(configs.router)

# 业务模块
api_router.include_router(devices.router)
api_router.include_router(assets.router)
api_router.include_router(metrics.router)
api_router.include_router(alerts.router)
api_router.include_router(logs.router)
# 可视化大屏（模块4）：只读聚合接口，供 web/dashboard.html 使用
api_router.include_router(dashboard.router)

__all__ = ["api_router"]
