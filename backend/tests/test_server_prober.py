"""
Server 模块 Node 主动探测（prober）契约测试。

模式：conftest session 夹具 + httpx.MockTransport 模拟 /healthz 响应。
顶部 import app.server.node.model 确保 Node 注册进 Base.metadata 被 create_all 建表。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

import app.server.node.model  # noqa: F401  (注册 Node 到 Base.metadata)
from app.config import app_config
from app.server.node.enum import NodeStateEnum
from app.server.node.prober import _probe_node, probe_loop
from app.server.node.schema import NodeRegisterRequest
from app.server.node.service import NodeService

# 供 monkeypatch httpx.AsyncClient 的用例复用原始类（避免递归）
_ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _req(**overrides: Any) -> NodeRegisterRequest:
    base: dict[str, Any] = {
        "machine_id": "m-001",
        "hostname": "gpu-01",
        "ip": "10.0.0.1",
        "advertise_address": None,
        "worker_port": 8100,
    }
    base.update(overrides)
    return NodeRegisterRequest(**base)


def _make_client(handler) -> httpx.AsyncClient:
    """构造带 MockTransport 的 client，handler: (Request) -> Response。"""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=app_config.NODE_PROBE_TIMEOUT,
    )


async def test_probe_healthy_ready(session):
    svc = NodeService(session)
    node = await svc.register(_req())
    node.heartbeat_time = datetime.now()  # 心跳新鲜

    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"status": "ok"})

    async with _make_client(handler) as client:
        await _probe_node(svc, client, node)

    assert len(calls) == 1
    assert node.state == NodeStateEnum.READY.value
    assert node.unreachable is False


async def test_probe_healthz_500_unreachable(session):
    svc = NodeService(session)
    node = await svc.register(_req())
    node.heartbeat_time = datetime.now()  # 心跳新鲜

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _make_client(handler) as client:
        await _probe_node(svc, client, node)

    assert node.unreachable is True
    assert node.state == NodeStateEnum.UNREACHABLE.value


async def test_probe_network_error_unreachable(session):
    svc = NodeService(session)
    node = await svc.register(_req())
    node.heartbeat_time = datetime.now()  # 心跳新鲜

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _make_client(handler) as client:
        await _probe_node(svc, client, node)

    assert node.unreachable is True
    assert node.state == NodeStateEnum.UNREACHABLE.value


async def test_probe_skips_offline(session):
    svc = NodeService(session)
    node = await svc.register(_req())
    grace = app_config.NODE_HEARTBEAT_GRACE_PERIOD
    node.heartbeat_time = datetime.now() - timedelta(seconds=grace + 5)  # 心跳超时

    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    async with _make_client(handler) as client:
        await _probe_node(svc, client, node)

    assert len(calls) == 0  # 心跳超时节点不发起探测
    assert node.state == NodeStateEnum.OFFLINE.value


async def test_probe_skips_pending(session):
    svc = NodeService(session)
    node = await svc.register(_req())  # 注册后无心跳，heartbeat_time=None

    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    async with _make_client(handler) as client:
        await _probe_node(svc, client, node)

    assert len(calls) == 0  # 未收到心跳的节点不发起探测
    assert node.state == NodeStateEnum.PENDING.value


async def test_probe_loop_smoke(monkeypatch):
    """冒烟：循环一轮（DB 未初始化时 get_session_context 抛异常被吞），
    CancelledError 穿透退出，验证「异常不退出循环」语义。"""

    async def _sleep(_: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.server.node.prober.asyncio.sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await probe_loop()


async def test_probe_uses_advertise_address_url(session):
    """advertise_address 为 host:port 一体时直接使用，不再拼 worker_port。"""
    svc = NodeService(session)
    node = await svc.register(_req(advertise_address="10.0.0.1:9999"))
    node.heartbeat_time = datetime.now()  # 心跳新鲜

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    async with _make_client(handler) as client:
        await _probe_node(svc, client, node)

    assert seen == ["http://10.0.0.1:9999/healthz"]
    assert node.state == NodeStateEnum.READY.value
    assert node.unreachable is False


async def test_probe_sends_bearer_token(session):
    """探测请求携带 node token（Authorization: Bearer）作兼容双保险。"""
    svc = NodeService(session)
    node = await svc.register(_req())
    node.heartbeat_time = datetime.now()  # 心跳新鲜

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(200)

    async with _make_client(handler) as client:
        await _probe_node(svc, client, node)

    assert seen == [f"Bearer {node.token}"]
    assert node.state == NodeStateEnum.READY.value


async def test_probe_loop_success_path(session, monkeypatch):
    """成功路径编排：get_list → 探测 → refresh → 一轮后节点 state=ready，sleep 被调用。"""
    svc = NodeService(session)
    node = await svc.register(_req())
    node.heartbeat_time = datetime.now()  # 心跳新鲜

    calls = {"sleep": 0}

    @asynccontextmanager
    async def _ctx():
        yield session

    def _client_factory(*args, **kwargs) -> httpx.AsyncClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        return _ORIGINAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(handler),
            timeout=app_config.NODE_PROBE_TIMEOUT,
        )

    async def _no_sleep(_: float) -> None:
        calls["sleep"] += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.server.node.prober.get_session_context", _ctx)
    monkeypatch.setattr("app.server.node.prober.httpx.AsyncClient", _client_factory)
    monkeypatch.setattr("app.server.node.prober.asyncio.sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await probe_loop()

    assert calls["sleep"] == 1  # 跑完一轮进入 sleep
    assert node.state == NodeStateEnum.READY.value
    assert node.unreachable is False


async def test_probe_loop_callback_called_on_lost(session, monkeypatch):
    """C2：注入 on_node_lost 回调——失联（offline）节点被批量触发并携带会话。"""
    svc = NodeService(session)
    node = await svc.register(_req())
    grace = app_config.NODE_HEARTBEAT_GRACE_PERIOD
    node.heartbeat_time = datetime.now() - timedelta(seconds=grace + 5)  # 存活超时
    await session.flush()

    calls: list[tuple[Any, list[Any]]] = []

    async def on_node_lost(s, lost_nodes):
        calls.append((s, lost_nodes))

    @asynccontextmanager
    async def _ctx():
        yield session

    async def _no_sleep(_: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.server.node.prober.get_session_context", _ctx)
    monkeypatch.setattr("app.server.node.prober.asyncio.sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await probe_loop(on_node_lost=on_node_lost)

    assert len(calls) == 1
    assert calls[0][0] is session  # 回调在探测同一会话内执行
    assert [n.id for n in calls[0][1]] == [node.id]
    assert node.state == NodeStateEnum.OFFLINE.value


async def test_probe_loop_callback_not_called_when_no_lost(session, monkeypatch):
    """C2：无失联节点时 on_node_lost 不触发（健康节点正常探测到 ready）。"""
    svc = NodeService(session)
    node = await svc.register(_req())
    node.heartbeat_time = datetime.now()  # 存活新鲜
    await session.flush()

    calls: list[tuple[Any, list[Any]]] = []

    async def on_node_lost(s, lost_nodes):
        calls.append((s, lost_nodes))

    @asynccontextmanager
    async def _ctx():
        yield session

    def _client_factory(*args, **kwargs) -> httpx.AsyncClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        return _ORIGINAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(handler),
            timeout=app_config.NODE_PROBE_TIMEOUT,
        )

    async def _no_sleep(_: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.server.node.prober.get_session_context", _ctx)
    monkeypatch.setattr("app.server.node.prober.httpx.AsyncClient", _client_factory)
    monkeypatch.setattr("app.server.node.prober.asyncio.sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await probe_loop(on_node_lost=on_node_lost)

    assert calls == []
    assert node.state == NodeStateEnum.READY.value
    assert node.unreachable is False
