"""聚合查询业务规则。

核心约束：维度属性与聚合模板共享枚举版本。
- 模板必须绑定一个具体的属性发布版本，枚举/数据类型/数据源只从该版本快照读取；
- 启用前做准入校验，属性已下线（DIMENSION_PROPERTY_RETIRED）等问题直接拦在验证环节；
- 聚合缓存键带版本号，属性下线或换版时联动失效，旧枚举结果不可能再命中；
- 所有跨表写操作走 work_lock + transactional，任一步失败整体回滚，模板保持不可用。
"""
from __future__ import annotations

from typing import Any

from app.services import aggregate_cache, versioning
from app.services.errors import (
    DIMENSION_PROPERTY_EMPTY_ENUM,
    DIMENSION_PROPERTY_NOT_FOUND,
    DIMENSION_PROPERTY_RETIRED,
    DIMENSION_PROPERTY_UNBOUND,
    build_error,
)
from app.services.locking import transactional, work_lock
from app.store import store

MODULE = "queryboard"
REQUIRED_FIELDS = ["模板编号", "指标名称", "维度字段"]
STATUS_ORDER = ["草稿", "模板验证中", "已启用", "已废弃"]
ACTION_RULES = {"提交模板验证": "模板验证中", "启用模板": "已启用", "废弃模板": "已废弃"}
NEGATIVE_ACTIONS: list[str] = []

ENABLED = "已启用"
VALIDATING = "模板验证中"
RETIRED = "已废弃"

# 测试故障注入点：名字出现在集合里时，对应提交环节会抛错以验证事务回滚。
FAULT_POINTS: set[str] = set()

BINDING_FIELDS = (
    "bound_property_id",
    "bound_version",
    "snapshot_data_type",
    "snapshot_enum_values",
    "snapshot_source_connection",
)


class QueryboardService:
    # ------------------------------------------------------------------ 查询
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
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ----------------------------------------------------------------- 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for field in BINDING_FIELDS:
            entry[field] = None
        entry["validation_error"] = None
        self._bind_from_values(entry, values)
        rows.append(entry)
        return entry, []

    def _bind_from_values(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        """登记时可显式指定绑定版本；未指定则尝试当前已发布版本，并固化版本快照。"""
        property_id = str(values.get("bound_property_id") or entry.get("维度字段") or "").strip()
        if not property_id:
            return
        version = str(values.get("bound_version") or "").strip()
        version_row = versioning.get_version(property_id, version) if version else versioning.current_published(property_id)
        if version_row is None:
            # 允许先登记后发布：草稿保留绑定意向，验证环节再给出 NOT_FOUND 结论。
            entry["bound_property_id"] = property_id
            entry["bound_version"] = version or None
            return
        self._apply_snapshot(entry, version_row)

    @staticmethod
    def _apply_snapshot(entry: dict[str, Any], version_row: dict[str, Any]) -> None:
        entry["bound_property_id"] = version_row["property_id"]
        entry["bound_version"] = version_row["version"]
        # 快照只来自绑定的发布版本，后续属性改名/换版都不会污染草稿里的字段。
        entry["snapshot_data_type"] = version_row.get("data_type")
        entry["snapshot_enum_values"] = list(version_row.get("enum_values", []))
        entry["snapshot_source_connection"] = version_row.get("source_connection")

    # ------------------------------------------------------------- 准入校验
    def validate_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """返回 None 表示通过；否则返回 {code, context} 错误体。"""
        property_id = entry.get("bound_property_id")
        if not property_id:
            return build_error(
                DIMENSION_PROPERTY_UNBOUND,
                template_id=entry.get("模板编号"),
            )
        bound_version = entry.get("bound_version")
        version_row = versioning.get_version(property_id, str(bound_version or ""))
        if version_row is None:
            return build_error(
                DIMENSION_PROPERTY_NOT_FOUND,
                property_id=property_id,
                requested_version=bound_version,
            )
        if version_row.get("status") == versioning.RETIRED:
            return build_error(
                DIMENSION_PROPERTY_RETIRED,
                property_id=property_id,
                retired_version=bound_version,
            )
        if not version_row.get("enum_values"):
            return build_error(
                DIMENSION_PROPERTY_EMPTY_ENUM,
                property_id=property_id,
                version=bound_version,
            )
        return None

    def _mark_invalid(self, entry: dict[str, Any], error: dict[str, Any]) -> None:
        entry["status"] = VALIDATING
        entry["pending"] = True
        entry["abnormal"] = True
        entry["validation_error"] = error

    # ----------------------------------------------------------------- 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询模板 {entry_id} 不存在或已归档", None
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于聚合查询可执行范围", None

        with work_lock:
            if action == "废弃模板":
                return self._discard(entry)
            if action == "提交模板验证":
                return self._submit_validation(entry)
            return self._enable(entry)

    def _submit_validation(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        entry["status"] = VALIDATING
        entry["pending"] = True
        error = self.validate_entry(entry)
        if error is not None:
            entry["abnormal"] = True
            entry["validation_error"] = error
            return entry, f"模板验证未通过：{error['code']}", error
        entry["abnormal"] = False
        entry["validation_error"] = None
        return entry, "模板验证通过，可执行启用模板", None

    def _enable(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        # 重复启用也要重新走一遍准入：属性在两次启用之间可能已经下线或换版。
        with transactional():
            error = self.validate_entry(entry)
            if error is not None:
                self._mark_invalid(entry, error)
                aggregate_cache.invalidate_template(int(entry["id"]))
                return entry, f"模板启用被拦截：{error['code']}", error

            version_row = versioning.get_version(
                str(entry["bound_property_id"]), str(entry["bound_version"])
            )
            assert version_row is not None  # validate_entry 已保证
            self._apply_snapshot(entry, version_row)

            if "enable_before_commit" in FAULT_POINTS:
                raise RuntimeError("模拟启用提交阶段写入失败")

            entry["status"] = ENABLED
            entry["pending"] = False
            entry["abnormal"] = False
            entry["validation_error"] = None
            aggregate_cache.invalidate_template(int(entry["id"]))
            aggregate_cache.put_cached(
                int(entry["id"]),
                str(entry["bound_property_id"]),
                str(entry["bound_version"]),
                metric_name=str(entry.get("指标名称", "")),
                rows=self._build_aggregate_rows(entry, version_row),
            )
        return entry, "查询模板已启用，聚合结果已按绑定版本生成", None

    def _discard(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str, None]:
        with transactional():
            entry["status"] = RETIRED
            entry["pending"] = False
            entry["abnormal"] = False
            aggregate_cache.invalidate_template(int(entry["id"]))
        return entry, "查询模板已废弃，聚合缓存已清理", None

    @staticmethod
    def _build_aggregate_rows(entry: dict[str, Any], version_row: dict[str, Any]) -> list[dict[str, Any]]:
        """演示环境没有真实数仓，按绑定版本的枚举确定性地生成聚合行。"""
        metric = str(entry.get("指标名称") or "metric")
        dimension = str(entry.get("bound_property_id"))
        rows: list[dict[str, Any]] = []
        for ordinal, enum_value in enumerate(version_row["enum_values"], start=1):
            # 稳定哈希：同一版本重复启用得到同样的数值，换枚举后数值随版本变化。
            digest = sum(ord(char) for char in f"{entry['id']}:{version_row['version']}:{enum_value}")
            rows.append({dimension: enum_value, metric: 1000 * ordinal + digest % 1000})
        return rows

    # -------------------------------------------------------- 属性事件联动
    def handle_version_retired(self, property_id: str, retired_version: str) -> list[int]:
        """属性下线或发布换版导致某个版本退役时，把引用它的模板打回验证中并清缓存。

        调用方已持有 work_lock；这里只做状态联动，返回受影响的模板 id 供发布记录互查。
        """
        affected: list[int] = []
        error = build_error(
            DIMENSION_PROPERTY_RETIRED,
            property_id=property_id,
            retired_version=retired_version,
        )
        for entry in store.rows(MODULE):
            if entry.get("bound_property_id") != property_id:
                continue
            if entry.get("bound_version") != retired_version:
                continue
            if entry.get("status") == RETIRED:
                continue
            aggregate_cache.invalidate_template(int(entry["id"]))
            self._mark_invalid(entry, error)
            affected.append(int(entry["id"]))
        aggregate_cache.invalidate_property_version(property_id, retired_version)
        return affected

    # ------------------------------------------------------------- 启动巡检
    def reconcile(self) -> dict[str, int]:
        """服务启动时复核存量绑定：修复「属性已撤下、模板仍启用、旧缓存残留」的脏状态。"""
        reverted = 0
        cache_purged = 0
        with work_lock:
            for entry in store.rows(MODULE):
                if not entry.get("bound_property_id"):
                    continue
                error = self.validate_entry(entry)
                if error is None:
                    continue
                if entry.get("status") == ENABLED or entry.get("status") == VALIDATING:
                    cache_purged += aggregate_cache.invalidate_template(int(entry["id"]))
                    self._mark_invalid(entry, error)
                    reverted += 1
            # 兜底：清掉任何指向已退役版本的残留缓存。
            for cached in list(store.rows(aggregate_cache.CACHE_MODULE)):
                version_row = versioning.get_version(
                    str(cached.get("property_id")), str(cached.get("version"))
                )
                if version_row is None or version_row.get("status") == versioning.RETIRED:
                    aggregate_cache.invalidate_property_version(
                        str(cached.get("property_id")), str(cached.get("version"))
                    )
                    cache_purged += 1
        return {"templates_reverted": reverted, "cache_purged": cache_purged}

    # ------------------------------------------------------------- 聚合读取
    def get_aggregate(self, entry_id: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """只服务已启用模板；未通过准入的模板不产生聚合结果。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, build_error(DIMENSION_PROPERTY_NOT_FOUND, template_id=entry_id)
        if entry.get("status") != ENABLED:
            return None, entry.get("validation_error") or build_error(
                DIMENSION_PROPERTY_UNBOUND, template_id=entry.get("模板编号")
            )
        cached = aggregate_cache.get_cached(
            int(entry["id"]),
            str(entry["bound_property_id"]),
            str(entry["bound_version"]),
        )
        if cached is None:
            return None, build_error(
                "DIMENSION_CACHE_MISS",
                template_id=entry.get("模板编号"),
                property_id=entry.get("bound_property_id"),
                version=entry.get("bound_version"),
            )
        return cached, None


service = QueryboardService()
