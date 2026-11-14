"""聚合查询接口：维护查询模板，覆盖提交模板验证、启用模板、废弃模板等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.queryboard import service

router = APIRouter(prefix="/api/queryboard", tags=["聚合查询"])

LIST_FIELDS = ["模板编号", "指标名称", "维度字段", "度量字段", "排序规则", "分页上限", "缓存秒数", "数据源"]
STATUSES = ["草稿", "模板验证中", "已启用", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、模板验证中、已启用、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤聚合查询列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}/aggregate", response_model=dict)
def get_aggregate(entry_id: int) -> dict[str, Any]:
    """读取已启用模板的聚合结果；未通过准入的模板不产出数据并回传验证错误码。"""
    cached, error = service.get_aggregate(entry_id)
    if cached is None:
        raise HTTPException(status_code=409, detail=error or {"code": "DIMENSION_CACHE_MISS"})
    return cached


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出聚合查询清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "queryboard", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条查询模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条查询模板，缺字段时说明原因而不是静默丢弃。

    values 里可选 bound_property_id / bound_version 指定绑定的属性发布版本；
    未指定时尝试绑定该属性当前已发布版本并固化枚举与数据类型快照。
    """
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条查询模板执行提交模板验证、启用模板、废弃模板。

    启用会强制重走维度属性准入：属性已下线/版本失效/空枚举都会被拦截，
    模板停在模板验证中并返回 DIMENSION_PROPERTY_RETIRED 等结构化错误。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message, error = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=error is None, message=message, entry=entry, error=error)
