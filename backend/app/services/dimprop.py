"""维度属性业务规则：状态流转、版本发布与对聚合模板的联动。

与 queryboard 的契约：
- 发布动作创建不可变新版本（versioning 注册表），旧发布版本退役并通知模板打回验证；
- 下线动作退役当前发布版本，模板保留草稿字段但不可再启用、聚合缓存清理；
- 回滚不复活旧版本号，而是复制旧版本内容发布新版本；
- 全程 work_lock 串行化 + transactional 回滚，任一侧写入失败则整体不动。
"""
from __future__ import annotations

from typing import Any

from app.services import versioning
from app.services.locking import transactional, work_lock
from app.services.queryboard import QueryboardService
from app.store import store

MODULE = "dimprop"
REQUIRED_FIELDS = ["属性编号", "维度名称", "属性名称"]
STATUS_ORDER = ["草稿", "属性评审中", "已发布", "已下线"]
ACTION_RULES = {"提交属性评审": "属性评审中", "发布属性": "已发布", "下线属性": "已下线"}
NEGATIVE_ACTIONS = ["下线属性"]

# 发布/回滚时允许通过动作载荷覆盖的可编辑字段（对应需求里的枚举改名、空枚举场景）。
EDITABLE_FIELDS = {
    "数据类型": "data_type",
    "枚举值列表": "enum_values",
    "关联数据源": "source_connection",
}

FAULT_POINTS: set[str] = set()


class DimpropService:
    def __init__(self) -> None:
        self.queryboard = QueryboardService()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("属性编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_property_id(self, property_id: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("属性编号") == property_id:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 可选字段原样保留，避免详情页再丢 data_type / source_connection。
        for field in EDITABLE_FIELDS:
            entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ----------------------------------------------------------------- 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"维度属性 {entry_id} 不存在或已归档", None
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于维度属性可执行范围", None

        payload = payload or {}
        with work_lock:
            if action == "提交属性评审":
                entry["status"] = ACTION_RULES[action]
                entry["pending"] = True
                entry["abnormal"] = False
                return entry, "维度属性已提交属性评审", None
            if action == "发布属性":
                return self._publish(entry, payload)
            return self._retire(entry)

    def _apply_edits(self, entry: dict[str, Any], payload: dict[str, Any]) -> None:
        for field in EDITABLE_FIELDS:
            if field in payload and payload[field] is not None:
                entry[field] = payload[field]

    def _publish(
        self, entry: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        with transactional():
            self._apply_edits(entry, payload)
            previous = versioning.current_published(str(entry["属性编号"]))
            version_row = versioning.publish_property(
                str(entry["属性编号"]),
                data_type=str(entry.get("数据类型") or ""),
                enum_values=entry.get("枚举值列表"),
                source_connection=str(entry.get("关联数据源") or ""),
                detail=f"属性 {entry['属性编号']} 发布新版本",
            )
            if "publish_after_version" in FAULT_POINTS:
                raise RuntimeError("模拟属性发布后模板联动写入失败")
            affected: list[int] = []
            if previous is not None:
                # 重新发布即换版：引用旧版本的模板全部打回验证中，旧缓存键随之失效。
                affected = self.queryboard.handle_version_retired(
                    str(entry["属性编号"]), str(previous["version"])
                )
            entry["status"] = ACTION_RULES["发布属性"]
            entry["pending"] = False
            entry["abnormal"] = False
        return (
            entry,
            f"维度属性已发布 {version_row['version']}"
            + (f"，{len(affected)} 个引用旧版本的模板回到模板验证中" if affected else ""),
            None,
        )

    def _retire(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        property_id = str(entry["属性编号"])
        with transactional():
            retired = versioning.retire_property(property_id)
            if retired is None:
                return entry, f"属性 {property_id} 没有已发布版本可下线", None
            if "retire_after_version" in FAULT_POINTS:
                raise RuntimeError("模拟属性下线后模板联动写入失败")
            affected = self.queryboard.handle_version_retired(
                property_id, str(retired["version"])
            )
            entry["status"] = ACTION_RULES["下线属性"]
            entry["pending"] = False
            entry["abnormal"] = True
        return (
            entry,
            f"维度属性已下线，{retired['version']} 退役，{len(affected)} 个模板回到模板验证中",
            None,
        )

    def rollback(
        self, entry_id: int, target_version: str
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"维度属性 {entry_id} 不存在或已归档"
        property_id = str(entry["属性编号"])
        with work_lock, transactional():
            previous = versioning.current_published(property_id)
            version_row = versioning.rollback_property(property_id, target_version)
            affected: list[int] = []
            if previous is not None:
                affected = self.queryboard.handle_version_retired(
                    property_id, str(previous["version"])
                )
            # 回滚后的属性回到已发布状态；列表行展示字段同步为新版本内容。
            entry["status"] = ACTION_RULES["发布属性"]
            entry["pending"] = False
            entry["abnormal"] = False
            entry["数据类型"] = version_row["data_type"]
            entry["枚举值列表"] = ",".join(version_row["enum_values"])
            entry["关联数据源"] = version_row["source_connection"]
        return entry, (
            f"属性已回滚 {target_version} 的内容并重新发布为 {version_row['version']}，"
            f"{len(affected)} 个模板回到模板验证中"
        )

    # ------------------------------------------------------------- 版本查询
    def versions(self, property_id: str) -> list[dict[str, Any]]:
        return versioning.list_versions(property_id)

    def publications(self, property_id: str | None = None) -> list[dict[str, Any]]:
        return versioning.publication_history(property_id)


service = DimpropService()
