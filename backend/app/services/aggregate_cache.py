"""聚合结果缓存：缓存键带属性发布版本号，版本失效后旧键物理上不可能再命中。"""
from __future__ import annotations

from typing import Any

from app.store import store

CACHE_MODULE = "aggregate_cache"


def cache_key(template_id: int, property_id: str, version: str) -> str:
    """键结构同时含模板、属性与版本；换版后查询落到新键，旧枚举结果自然失联。"""
    return f"tpl:{template_id}:prop:{property_id}:ver:{version}"


def _rows() -> list[dict[str, Any]]:
    return store.rows(CACHE_MODULE)


def get_cached(template_id: int, property_id: str, version: str) -> dict[str, Any] | None:
    """只返回与当前绑定版本完全一致的缓存；版本不匹配一律视为未命中。"""
    key = cache_key(template_id, property_id, version)
    for row in _rows():
        if row.get("cache_key") == key:
            return row
    return None


def put_cached(
    template_id: int,
    property_id: str,
    version: str,
    *,
    metric_name: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    existing = get_cached(template_id, property_id, version)
    if existing is not None:
        existing["metric_name"] = metric_name
        existing["rows"] = rows
        return existing
    entry = {
        "id": max((int(row.get("id", 0)) for row in _rows()), default=0) + 1,
        "cache_key": cache_key(template_id, property_id, version),
        "template_id": template_id,
        "property_id": property_id,
        "version": version,
        "metric_name": metric_name,
        "rows": rows,
    }
    _rows().append(entry)
    return entry


def invalidate_template(template_id: int) -> int:
    """属性下线/换版联动：删除该模板的全部缓存，返回清理条数便于发布记录互查。"""
    before = len(_rows())
    store._tables[CACHE_MODULE] = [
        row for row in _rows() if row.get("template_id") != template_id
    ]
    return before - len(_rows())


def invalidate_property_version(property_id: str, version: str) -> int:
    before = len(_rows())
    store._tables[CACHE_MODULE] = [
        row
        for row in _rows()
        if not (row.get("property_id") == property_id and row.get("version") == version)
    ]
    return before - len(_rows())
