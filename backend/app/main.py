"""指标口径与分析实验平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import ROUTERS
from app.services.queryboard import service as queryboard_service
from app.store import store


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动巡检：修复「属性已撤下、模板仍启用、旧缓存残留」的存量脏状态。
    queryboard_service.reconcile()
    yield


app = FastAPI(title="指标口径与分析实验平台", version="1.0.0", lifespan=lifespan)


@app.exception_handler(RuntimeError)
def runtime_error_handler(_: Request, exc: RuntimeError) -> JSONResponse:
    """事务块回滚后抛出的写入故障统一结构化返回，便于调用方确认模板保持不可用。"""
    return JSONResponse(
        status_code=500,
        content={"ok": False, "message": f"事务已回滚：{exc}", "entry": None, "error": None},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)

if os.environ.get("LOCAL_TEST") == "1":
    from app.routers.testing import router as testing_router

    app.include_router(testing_router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
