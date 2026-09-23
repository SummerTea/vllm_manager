"""server→worker 统一转发工具契约测试。

模式：直接内存构造 Node（不落库、不需要 DB 夹具），
用 monkeypatch 替换模块内 httpx.AsyncClient 捕获构造参数与请求参数，
异常映射按 httpx 分层（HTTPStatusError → HttpClientException，
其余 HTTPError → ExternalServiceException）逐项验证。
"""

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from app.config import app_config
from app.exception import ExternalServiceException, HttpClientException
from app.server.node.model import Node
from app.utils.request_to_worker import build_worker_url, request_to_worker


def _node(**overrides: Any) -> Node:
    base: dict[str, Any] = {
        "hostname": "gpu-01",
        "ip": "10.0.0.1",
        "worker_port": 8100,
        "token": "test-token",
        "advertise_address": None,
    }
    base.update(overrides)
    return Node(**base)


def _install_fake_client(monkeypatch, on_request: Callable):
    """替换模块内 httpx.AsyncClient，返回 captured 记录构造与请求参数。

    on_request: () -> httpx.Response 或 raise 网络异常。
    """
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def _fake_client(**client_kwargs):
        captured["client_kwargs"] = client_kwargs

        class _FakeClient:
            async def request(self, method, url, **kwargs):
                captured["request"] = (method, url, kwargs)
                # 挂上 request 供 raise_for_status 校验响应状态码
                resp = on_request()
                resp.request = httpx.Request(method, url)
                return resp

        yield _FakeClient()

    monkeypatch.setattr(
        "app.utils.request_to_worker.httpx.AsyncClient", _fake_client
    )
    return captured


# ---------- build_worker_url 地址构建三态 ----------


def test_build_url_uses_advertise_address_as_is():
    node = _node(advertise_address="10.0.0.1:8100")
    assert (
        build_worker_url(node, "instances/i-1/start")
        == "http://10.0.0.1:8100/instances/i-1/start"
    )


def test_build_url_appends_worker_port_to_plain_host():
    node = _node(advertise_address="10.0.0.1")
    assert (
        build_worker_url(node, "instances/i-1/start")
        == "http://10.0.0.1:8100/instances/i-1/start"
    )


def test_build_url_falls_back_to_ip_and_port():
    node = _node(advertise_address=None)
    assert (
        build_worker_url(node, "instances/i-1/start")
        == "http://10.0.0.1:8100/instances/i-1/start"
    )


# ---------- request_to_worker 请求参数 ----------


async def test_request_injects_bearer_and_default_timeout(monkeypatch):
    node = _node()
    captured = _install_fake_client(monkeypatch, lambda: httpx.Response(200))

    resp = await request_to_worker(
        node,
        "post",
        "instances/i-1/start",
        json={"model": "qwen2.5"},
        params={"gpu_index": 0},
    )

    assert resp.status_code == 200
    assert captured["client_kwargs"] == {
        "timeout": app_config.INSTANCE_REQUEST_TIMEOUT
    }
    method, url, kwargs = captured["request"]
    assert method == "post"
    assert url == "http://10.0.0.1:8100/instances/i-1/start"
    assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert kwargs["json"] == {"model": "qwen2.5"}
    assert kwargs["params"] == {"gpu_index": 0}


async def test_request_explicit_timeout_overrides(monkeypatch):
    node = _node()
    captured = _install_fake_client(monkeypatch, lambda: httpx.Response(200))

    await request_to_worker(node, "post", "instances/i-1/start", timeout=5.0)

    assert captured["client_kwargs"] == {"timeout": 5.0}


# ---------- 异常映射 ----------


async def test_request_4xx_raises_http_client_exception(monkeypatch):
    node = _node()
    _install_fake_client(monkeypatch, lambda: httpx.Response(404))

    with pytest.raises(HttpClientException) as excinfo:
        await request_to_worker(node, "post", "instances/i-1/start")

    assert excinfo.value.details["url"] == "http://10.0.0.1:8100/instances/i-1/start"
    assert excinfo.value.details["status_code"] == 404
    assert "404" in excinfo.value.message


async def test_request_network_error_raises_external_exception(monkeypatch):
    node = _node()

    def _raise_connect_error():
        raise httpx.ConnectError("connection refused")

    _install_fake_client(monkeypatch, _raise_connect_error)

    with pytest.raises(ExternalServiceException) as excinfo:
        await request_to_worker(node, "post", "instances/i-1/start")

    assert excinfo.value.details["service_name"] == "worker"
    assert excinfo.value.details["url"] == "http://10.0.0.1:8100/instances/i-1/start"


async def test_request_timeout_raises_external_exception(monkeypatch):
    """httpx.TimeoutException（HTTPError 非 HTTPStatusError）→ ExternalServiceException。"""
    node = _node()

    def _raise_timeout():
        raise httpx.TimeoutException("request timed out")

    _install_fake_client(monkeypatch, _raise_timeout)

    with pytest.raises(ExternalServiceException) as excinfo:
        await request_to_worker(node, "post", "instances/i-1/start")

    assert excinfo.value.details["service_name"] == "worker"
    assert excinfo.value.details["url"] == "http://10.0.0.1:8100/instances/i-1/start"
