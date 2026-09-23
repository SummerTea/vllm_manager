"""Worker FastAPI 应用契约测试。

create_app() 直接实例化不触发 lifespan（TestClient 不用 `with` 上下文），
因此 /healthz 可无 server 依赖测试；lifespan 注册失败由 S9 测试覆盖。
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.worker.main import create_app, report_loop, status_loop, sync_loop


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


def test_business_exception_not_found_returns_404():
    """F2：ResourceNotExistException → 404 JSON（业务异常 404 分支）。"""
    from fastapi import FastAPI

    from app.exception import ResourceNotExistException
    from app.worker.exceptions import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    async def not_found():
        raise ResourceNotExistException(
            "实例不存在", resource_type="instance", resource_id="i-1"
        )

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/not-found")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["message"] == "实例不存在"
    assert body["data"] == {"error_code": "RESOURCE_NOT_FOUND"}


def test_business_exception_duplicate_returns_409():
    """F2：DuplicateResourceException → 409 JSON（业务异常 409 分支）。"""
    from fastapi import FastAPI

    from app.exception import DuplicateResourceException
    from app.worker.exceptions import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/duplicate")
    async def duplicate():
        raise DuplicateResourceException("节点已存在", field="node_id", value="n-1")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/duplicate")
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["message"] == "节点已存在"
    assert body["data"] == {"error_code": "DUPLICATE_RESOURCE"}


def test_lifespan_register_failure_starts_fails(monkeypatch):
    """S9：注册失败（重试耗尽抛异常）→ 应用启动失败（TestClient 上下文抛异常）。"""

    async def _fail_register(self):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("app.worker.server_client.ServerClient.register", _fail_register)

    app = create_app()
    with pytest.raises(httpx.ConnectError), TestClient(app):
        pass  # lifespan 进入即注册 → 抛异常，TestClient 上下文无法建立


# ---------- 三循环「异常不退出」 ----------


async def test_status_loop_exception_does_not_exit(caplog):
    """三循环-1：status_loop 首轮异常被捕获并 continue，后续轮次正常上报。"""
    calls = {"n": 0}

    class _FakeCollector:
        def collect(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("collect boom")
            return {"status": {}}

    class _FakeClient:
        def __init__(self):
            self.reported: list[dict] = []

        async def report_status(self, node_id, token, payload):
            self.reported.append(payload)

    client = _FakeClient()
    task = asyncio.create_task(status_loop(client, _FakeCollector(), "n-1", "t", 0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "状态上报循环异常" in caplog.text  # 异常被捕获并记录
    assert len(client.reported) >= 1  # 循环未退出，后续轮次正常上报


async def test_report_loop_exception_does_not_exit(caplog):
    """三循环-2：report_loop 首轮异常被捕获并 continue，后续轮次正常上报。"""
    calls = {"n": 0}

    class _FakeLifecycle:
        async def snapshot_for_report(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("snapshot boom")
            return [{"id": "i-1", "state": "running"}]

    class _FakeClient:
        def __init__(self):
            self.reported: list[list[dict]] = []

        async def report_instances(self, node_id, token, items):
            self.reported.append(items)

    client = _FakeClient()
    task = asyncio.create_task(report_loop(client, _FakeLifecycle(), "n-1", "t", 0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "实例对账循环异常" in caplog.text  # 异常被捕获并记录
    assert len(client.reported) >= 1  # 循环未退出，后续轮次正常上报


async def test_sync_loop_exception_does_not_exit(caplog):
    """三循环-3：sync_loop 首轮异常被捕获并 continue，后续轮次继续 sync。"""
    calls = {"n": 0}

    class _FakeLifecycle:
        async def sync(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("sync boom")

    task = asyncio.create_task(sync_loop(_FakeLifecycle(), 0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "实例状态机循环异常" in caplog.text  # 异常被捕获并记录
    assert calls["n"] >= 2  # 首轮异常后仍继续执行 sync
