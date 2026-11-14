"""维度属性版本注册表。

枚举值与数据类型的唯一事实源在这里，而不是 dimprop 列表行上的展示字段：
- 每次「发布属性」生成一个不可变新版本，旧发布版本同步退役；
- 「下线属性」把当前发布版本标记为 retired；
- 「回滚」不是复活旧版本，而是把旧版本内容复制成一个新版本，保证版本号单调、缓存键永不复用；
- 所有动作在 dim_publications 留痕，供模板详情、聚合结果与发布记录互查。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.store import store

VERSIONS_MODULE = "dim_versions"
PUBLICATIONS_MODULE = "dim_publications"

PUBLISHED = "published"
RETIRED = "retired"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def version_number(version: str | None) -> int:
    """把 v3 解析成 3；无法解析时按 0 处理，发布排序不会因为脏数据中断。"""
    match = re.fullmatch(r"v(\d+)", str(version or "").strip())
    return int(match.group(1)) if match else 0


def parse_enum_values(raw: Any) -> list[str]:
    """枚举来源可能是列表或逗号/顿号分隔的字符串，统一成去空白、去重保序的列表。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        candidates = [str(item).strip() for item in raw]
    else:
        candidates = re.split(r"[,，、;；\n]+", str(raw))
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def list_versions(property_id: str) -> list[dict[str, Any]]:
    rows = [
        row for row in store.rows(VERSIONS_MODULE)
        if row.get("property_id") == property_id
    ]
    return sorted(rows, key=lambda row: version_number(row.get("version")))


def get_version(property_id: str, version: str) -> dict[str, Any] | None:
    for row in store.rows(VERSIONS_MODULE):
        if row.get("property_id") == property_id and row.get("version") == version:
            return row
    return None


def current_published(property_id: str) -> dict[str, Any] | None:
    """同一属性同时只允许一个 published 版本；发布新版本时旧版本会被先退役。"""
    for row in store.rows(VERSIONS_MODULE):
        if row.get("property_id") == property_id and row.get("status") == PUBLISHED:
            return row
    return None


def _record_publication(
    property_id: str,
    version: str,
    action: str,
    *,
    from_version: str | None = None,
    detail: str = "",
) -> dict[str, Any]:
    rows = store.rows(PUBLICATIONS_MODULE)
    record = {
        "id": _next_id(rows),
        "property_id": property_id,
        "version": version,
        "action": action,
        "from_version": from_version,
        "detail": detail,
        "created_at": _now(),
    }
    rows.append(record)
    return record


def _retire_latest_published(property_id: str) -> dict[str, Any] | None:
    latest = current_published(property_id)
    if latest is not None:
        latest["status"] = RETIRED
        latest["retired_at"] = _now()
    return latest


def publish_property(
    property_id: str,
    *,
    data_type: str,
    enum_values: Any,
    source_connection: str,
    action: str = "publish",
    from_version: str | None = None,
    detail: str = "",
) -> dict[str, Any]:
    """创建新版本并退役旧发布版本。调用方需要持有 work_lock 完成跨表联动。"""
    existing = list_versions(property_id)
    next_version = f"v{max((version_number(row.get('version')) for row in existing), default=0) + 1}"
    retired = _retire_latest_published(property_id)
    version_row = {
        "id": _next_id(store.rows(VERSIONS_MODULE)),
        "property_id": property_id,
        "version": next_version,
        "status": PUBLISHED,
        "data_type": str(data_type or "").strip(),
        "enum_values": parse_enum_values(enum_values),
        "source_connection": str(source_connection or "").strip(),
        "published_at": _now(),
        "retired_at": None,
    }
    store.rows(VERSIONS_MODULE).append(version_row)
    _record_publication(
        property_id,
        next_version,
        action,
        from_version=from_version or (retired.get("version") if retired else None),
        detail=detail or f"属性 {property_id} 发布 {next_version}",
    )
    return version_row


def retire_property(property_id: str, *, detail: str = "") -> dict[str, Any] | None:
    latest = _retire_latest_published(property_id)
    if latest is None:
        return None
    _record_publication(
        property_id,
        str(latest["version"]),
        "retire",
        detail=detail or f"属性 {property_id} 下线，{latest['version']} 退役",
    )
    return latest


def rollback_property(property_id: str, target_version: str, *, detail: str = "") -> dict[str, Any]:
    """回滚 = 以历史版本内容重新发布一个新版本，旧版本号与旧缓存键都不会被复活。"""
    target = get_version(property_id, target_version)
    if target is None:
        raise KeyError(f"属性 {property_id} 不存在版本 {target_version}")
    return publish_property(
        property_id,
        data_type=target.get("data_type", ""),
        enum_values=list(target.get("enum_values", [])),
        source_connection=target.get("source_connection", ""),
        action="rollback",
        from_version=target_version,
        detail=detail or f"属性 {property_id} 回滚 {target_version} 的枚举与数据类型",
    )


def publication_history(property_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows(PUBLICATIONS_MODULE)
    if property_id:
        rows = [row for row in rows if row.get("property_id") == property_id]
    return sorted(rows, key=lambda row: int(row.get("id", 0)))
