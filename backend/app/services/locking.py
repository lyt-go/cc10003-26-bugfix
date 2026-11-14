"""跨模块写入的串行化锁与回滚辅助。

维度属性发布/下线要同时改版本注册表、模板状态与缓存，查询模板启用要同时校验并落缓存。
这些多步写入必须拿到同一个结论：任一步失败，前面的改动整体回滚，模板保持不可用。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.store import store

# 进程内全局锁：并发换版、重复启用、下线与启用撞车时全部串行化。
work_lock = threading.RLock()


@contextmanager
def transactional() -> Iterator[None]:
    """对 store 全表做快照，块内任一步抛错则整体还原，绝不留下半套状态。"""
    snapshot: dict[str, list[dict[str, Any]]] = {
        name: [dict(row) for row in rows] for name, rows in store._tables.items()
    }
    try:
        yield
    except Exception:
        store._tables = {
            name: [dict(row) for row in rows] for name, rows in snapshot.items()
        }
        raise
