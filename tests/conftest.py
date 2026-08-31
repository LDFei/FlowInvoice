# tests/conftest.py —— 测试夹具（临时库容器 + 路由级 FastAPI 客户端）
# 业务：测试用临时 SQLite，避免污染开发库 data/flowinvoice.db
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.advance import router as advance_router
from app.api.reimburse import router as reimburse_router
from app.container import build_container
from app.main import seed_demo_data


@pytest.fixture()
def container(tmp_path):
    """依赖容器（临时库 + 演示数据）"""
    c = build_container(db_path=tmp_path / "test.db")
    seed_demo_data(c)
    return c


@pytest.fixture()
def client(tmp_path):
    """路由级 FastAPI 测试客户端（临时库）"""
    container = build_container(db_path=tmp_path / "api.db")
    seed_demo_data(container)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(reimburse_router)
    app.include_router(advance_router)
    with TestClient(app) as c:
        yield c


def make_ticket(invoice_no="INV-TEST-001", invoice_type="火车票", amount=528.50,
                on_date=None, title="北京-上海 二等座") -> str:
    """构造一份 Mock 文本票面（供 OCR 解析）"""
    from datetime import date
    on_date = on_date or date.today().isoformat()
    return (
        f"发票号码: {invoice_no}\n"
        f"发票类型: {invoice_type}\n"
        f"开票日期: {on_date}\n"
        f"金额: {amount}\n"
        f"项目: {title}\n"
    )
