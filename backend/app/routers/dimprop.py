"""维度属性接口：维护维度属性，覆盖提交属性评审、发布属性、下线属性等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dimprop import service

router = APIRouter(prefix="/api/dimprop", tags=["维度属性"])

LIST_FIELDS = ["属性编号", "维度名称", "属性名称", "数据类型", "枚举值列表", "关联数据源", "更新频率", "最近更新时间"]
STATUSES = ["草稿", "属性评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按属性编号检索"),
    status: str | None = Query(default=None, description="草稿、属性评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按属性编号与状态过滤维度属性列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/property/{property_id}/versions", response_model=dict)
def list_versions(property_id: str) -> dict[str, Any]:
    """返回属性的全部发布版本（含枚举、数据类型、退役状态），供模板详情互查。"""
    if service.find_by_property_id(property_id) is None:
        raise HTTPException(status_code=404, detail=f"维度属性 {property_id} 不存在或已归档")
    return {"property_id": property_id, "items": service.versions(property_id)}


@router.get("/property/{property_id}/publications", response_model=dict)
def list_publications(property_id: str) -> dict[str, Any]:
    """发布记录：发布、换版、下线、回滚每个动作都留痕。"""
    if service.find_by_property_id(property_id) is None:
        raise HTTPException(status_code=404, detail=f"维度属性 {property_id} 不存在或已归档")
    return {"property_id": property_id, "items": service.publications(property_id)}


@router.post("/{entry_id}/rollback", response_model=ActionResult)
def rollback_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """回滚到历史版本：历史版本内容会被复制成一个新发布版本，旧版本号不复活。"""
    target_version = str(payload.values.get("target_version") or "").strip()
    if not target_version:
        return ActionResult(ok=False, message="缺少 target_version，无法执行回滚")
    entry, message = service.rollback(entry_id, target_version)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出维度属性清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dimprop", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条维度属性明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"维度属性 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条维度属性，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="维度属性已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条维度属性执行提交属性评审、发布属性、下线属性。

    发布时可在 values 里携带 数据类型/枚举值列表/关联数据源 覆盖值（枚举改名、空枚举场景）。
    准入失败会返回结构化 error，含错误码与上下文。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message, error = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=error is None, message=message, entry=entry, error=error)
