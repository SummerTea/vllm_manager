"""Worker ServerClient 契约测试（注册/心跳/状态/对账）。

模式：monkeypatch 模块内 httpx.AsyncClient 捕获请求参数（对齐
test_request_to_worker.py 的 fake client 模式）；tenacity 重试用
monkeypatch 降低 attempt 参数验证语义。
"""

from typing import Any

import httpx
import pytest

import app.worker.server_client as server_client_mod
from app.worker.config import WorkerConfig
from app.worker.server_client import ServerClient

_BASE = "http://127.0.0.1:8000/vllm_manager/api/v1"


def _make_client(**cfg_overrides) -> ServerClient:
    cfg = WorkerConfig(**cfg_overrides)
    return ServerClient(cfg)


class _FakeAsyncClient:
    """记录调用并返回可编程响应/异常的假 client（需替代 aclose）。"""

    def __init__(self, on_request):
        self.calls: list[dict[str, Any]] = []
        self._on_request = on_request
        self.closed = False

    async def post(self, url, **kwargs):
        self.calls.append({"method": "post", "url": url, "kwargs": kwargs})
        resp = self._on_request()
        resp.request = httpx.Request("POST", url)
        return resp

    async def aclose(self):
        self.closed = True


def _install_fake_client(monkeypatch, on_request):
    fake = _FakeAsyncClient(on_request)
    monkeypatch.setattr(
        server_client_mod.httpx, "AsyncClient", lambda **kw: fake
    )
    return fake


def _reg_response(**overrides):
    data = {
        "node_id": "node-1",
        "token": "tok-1",
        "worker_port": 8100,
        "heartbeat_interval": 10,
    }
    data.update(overrides)
    return httpx.Response(200, json={"code": 0, "message": "success", "data": data})


# ---------- register ----------


async def test_register_success_parses_data(monkeypatch):
    fake = _install_fake_client(monkeypatch, lambda: _reg_response())

    client = _make_client()
    reg = await client.register()

    assert reg.node_id == "node-1"
    assert reg.token == "tok-1"
    assert reg.worker_port == 8100
    assert reg.heartbeat_interval == 10
    assert fake.calls[0]["url"] == f"{_BASE}/nodes/register"
    body = fake.calls[0]["kwargs"]["json"]
    assert body["machine_id"] == body["hostname"]  # 未配 machine_id 用主机名
    assert body["worker_port"] == 8100


async def test_register_machine_id_override(monkeypatch):
    fake = _install_fake_client(monkeypatch, lambda: _reg_response())

    client = _make_client(WORKER_MACHINE_ID="m-abc")
    await client.register()

    body = fake.calls[0]["kwargs"]["json"]
    assert body["machine_id"] == "m-abc"


async def test_register_retries_then_raises(monkeypatch):
    """网络错误重试耗尽抛异常（attempt=3，连续 ConnectError）。"""

    def _fail():
        raise httpx.ConnectError("connection refused")

    fake = _install_fake_client(monkeypatch, _fail)

    # 降低 attempt：对原方法重装饰（保留 retry 条件=网络/超时），
    # 验证「网络错误重试耗尽抛异常」语义
    original = server_client_mod.ServerClient.register.__wrapped__

    async def _short_register(self):
        return await original(self)

    _short_register = server_client_mod.retry(
        stop=server_client_mod.stop_after_attempt(3),
        wait=server_client_mod.wait_fixed(0),
        retry=server_client_mod.retry_if_exception_type(
            (httpx.TransportError, httpx.TimeoutException)
        ),
        reraise=True,
    )(_short_register)
    monkeypatch.setattr(
        server_client_mod.ServerClient, "register", _short_register
    )

    client = _make_client()
    with pytest.raises(httpx.ConnectError):
        await client.register()
    assert len(fake.calls) == 3  # 重试 3 次后耗尽


async def test_register_4xx_immediate_raise(monkeypatch):
    """4xx 业务错误立即抛（HTTPStatusError 不属于 TransportError，不重试）。"""

    def _fail():
        return httpx.Response(400, json={"code": -1, "message": "bad request"})

    fake = _install_fake_client(monkeypatch, _fail)

    client = _make_client()
    with pytest.raises(httpx.HTTPStatusError):
        await client.register()
    assert len(fake.calls) == 1  # 仅调用一次，不进入重试


# ---------- heartbeat / status / report ----------


async def test_heartbeat_sends_bearer_and_url(monkeypatch):
    fake = _install_fake_client(monkeypatch, lambda: httpx.Response(200))

    client = _make_client()
    await client.heartbeat("node-1", "tok-1")

    call = fake.calls[0]
    assert call["url"] == f"{_BASE}/nodes/node-1/heartbeat"
    assert call["kwargs"]["headers"] == {"Authorization": "Bearer tok-1"}


async def test_heartbeat_failure_only_warns(monkeypatch, caplog):
    """心跳失败仅 warning 不抛（长驻容错）。"""

    def _fail():
        raise httpx.ConnectError("connection refused")

    _install_fake_client(monkeypatch, _fail)

    client = _make_client()
    await client.heartbeat("node-1", "tok-1")  # 不应抛异常
    assert "心跳上报失败" in caplog.text


async def test_report_status_sends_payload(monkeypatch):
    fake = _install_fake_client(monkeypatch, lambda: httpx.Response(200))

    payload = {"system_reserved": {"ram": 0, "vram": 0}, "status": {"cpu": {}}}
    client = _make_client()
    await client.report_status("node-1", "tok-1", payload)

    call = fake.calls[0]
    assert call["url"] == f"{_BASE}/nodes/node-1/status"
    assert call["kwargs"]["headers"] == {"Authorization": "Bearer tok-1"}
    assert call["kwargs"]["json"] == payload


async def test_report_status_failure_only_warns(monkeypatch, caplog):
    def _fail():
        raise httpx.ConnectError("connection refused")

    _install_fake_client(monkeypatch, _fail)

    client = _make_client()
    await client.report_status("node-1", "tok-1", {"status": {}})
    assert "节点状态上报失败" in caplog.text


async def test_report_instances_sends_items(monkeypatch):
    fake = _install_fake_client(monkeypatch, lambda: httpx.Response(200))

    items = [{"id": "i-1", "state": "running"}]
    client = _make_client()
    await client.report_instances("node-1", "tok-1", items)

    call = fake.calls[0]
    assert call["url"] == f"{_BASE}/instances/report"
    assert call["kwargs"]["headers"] == {"Authorization": "Bearer tok-1"}
    assert call["kwargs"]["json"] == {"items": items}


async def test_report_instances_failure_only_warns(monkeypatch, caplog):
    def _fail():
        raise httpx.ConnectError("connection refused")

    _install_fake_client(monkeypatch, _fail)

    client = _make_client()
    await client.report_instances("node-1", "tok-1", [])
    assert "实例对账上报失败" in caplog.text


async def test_aclose_closes_http(monkeypatch):
    fake = _install_fake_client(monkeypatch, lambda: httpx.Response(200))
    client = _make_client()
    await client.aclose()
    assert fake.closed is True
