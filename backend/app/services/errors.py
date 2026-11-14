"""维度属性准入相关错误码：模板验证链路统一从这里取码，避免字符串散落各处。"""
from __future__ import annotations

from typing import Any

# 属性绑定的版本已下线（含属性整体下线、发布换版后旧版本退役）
DIMENSION_PROPERTY_RETIRED = "DIMENSION_PROPERTY_RETIRED"
# 属性或指定版本在注册表中不存在
DIMENSION_PROPERTY_NOT_FOUND = "DIMENSION_PROPERTY_NOT_FOUND"
# 发布版本存在但枚举为空，模板无法生成维度聚合
DIMENSION_PROPERTY_EMPTY_ENUM = "DIMENSION_PROPERTY_EMPTY_ENUM"
# 模板尚未绑定任何维度属性版本
DIMENSION_PROPERTY_UNBOUND = "DIMENSION_PROPERTY_UNBOUND"


def build_error(code: str, **context: Any) -> dict[str, Any]:
    """统一错误体：code 给机器判定，context 给排查与页面展示。"""
    return {"code": code, "context": context}
