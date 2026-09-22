"""Worker FastAPI 应用契约测试。

create_app() 直接实例化不触发 lifespan（TestClient 不用 `with` 上下文），
因此 /healthz 可无 server 依赖测试；lifespan 注册失败由 S9 测试覆盖。
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.worker.main import create_app


def test_healthz_returns_ok():
    """GET /healthz 200 且无需鉴权（liveness 探针豁免）。"""
    app = create_app()
    client = TestClient(app)  # 直接实例化，不触发 lifespan
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_docs_disabled():
    """worker 独立节点不暴露文档（docs_url=None）。"""
    app = create_app()
    assert app.docs_url is None
    assert app.redoc_url is None


def test_error_response_is_json(monkeypatch):
    """F2：worker 异常处理器 JSON-only——未捕获异常返回 JSON 而非 HTML。"""
    from fastapi import FastAPI

    from app.worker.exceptions import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["message"] == "服务器内部错误，请稍后重试"


def test_lifespan_register_failure_starts_fails(monkeypatch):
    """S9：注册失败（重试耗尽抛异常）→ 应用启动失败（TestClient 上下文抛异常）。"""

    async def _fail_register(self):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("app.worker.server_client.ServerClient.register", _fail_register)

    app = create_app()
    with pytest.raises(httpx.ConnectError), TestClient(app):
        pass  # lifespan 进入即注册 → 抛异常，TestClient 上下文无法建立
