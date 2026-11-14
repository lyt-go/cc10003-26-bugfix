"""本地测试辅助接口：重置内存仓库、注入写入故障。

仅在 LOCAL_TEST=1 时挂载，生产部署不会暴露这些路由。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.services import dimprop as dimprop_service_module
from app.services import queryboard as queryboard_service_module
from app.services.queryboard import service as queryboard_service
from app.store import store

router = APIRouter(prefix="/api/_test", tags=["测试辅助"])


@router.post("/reset")
def reset_store() -> dict[str, object]:
    """重新从种子数据装载仓库，并执行一次启动巡检，等价于重启进程。"""
    store.reload()
    report = queryboard_service.reconcile()
    return {"ok": True, "reconcile": report}


@router.post("/faults")
def configure_faults(payload: dict) -> dict[str, object]:
    """控制故障注入点：{"points": ["enable_before_commit"]} / {"points": []}。"""
    points = set(payload.get("points") or [])
    queryboard_service_module.FAULT_POINTS.clear()
    queryboard_service_module.FAULT_POINTS.update(points)
    dimprop_service_module.FAULT_POINTS.clear()
    dimprop_service_module.FAULT_POINTS.update(points)
    return {"ok": True, "active": sorted(queryboard_service_module.FAULT_POINTS)}
