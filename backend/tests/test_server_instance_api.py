"""
Server 模块 Instance API 契约测试。

模式：TestClient(app) + dependency_overrides[get_session] 复用 conftest 的
sqlite 内存库 session（不触发 lifespan，不需要真实 PG/Redis）；
request_to_worker 由 monkeypatch 替换，创建/启停不真发网络。
实例状态机由 /report 端点驱动（避免测试线程直接操作会话造成事件循环混用）。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.server.instance.model  # noqa: F401  (注册 VllmInstance 到 Base.metadata)
import app.server.node.model  # noqa: F401  (注册 Node 到 Base.metadata)
from app.extensions.database import get_session
from app.main import app
from app.server.instance.enum import InstanceStateEnum

_BASE = "/vllm_manager/api/v1/instances"
_NODES = "/vllm_manager/api/v1/nodes"
_GIB = 1024**3


class FakeResponse:
    """鸭子类型响应（request_to_worker 被 patch 后返回对象需含 .json()）。"""

    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def json(self) -> dict:
        return self._data


@pytest.fixture
def api_client(session):
    """将 get_session 依赖替换为 conftest 的 sqlite session，测试后清理 override。

    模拟真实 get_session 的请求结束 commit 语义（否则创建数据停留在未提交
    事务中，api 兜底的 session.rollback() 会将其回滚导致重查不到）。
    """

    async def _override_get_session():
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app.dependency_overrides[get_session] = _override_get_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _register(client: TestClient, **overrides) -> dict:
    payload: dict[str, Any] = {
        "machine_id": "m-001",
        "hostname": "gpu-01",
        "ip": "10.0.0.1",
        "advertise_address": "10.0.0.1:8100",
        "worker_port": 8100,
    }
    payload.update(overrides)
    resp = client.post(f"{_NODES}/register", json=payload)
    assert resp.status_code == 200
    return resp.json()["data"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ready_node(client: TestClient, **overrides) -> dict:
    """注册 + 心跳 + 状态上报（1 卡 24GiB、system_reserved vram=2GiB）→ ready。"""
    data = _register(client, **overrides)
    client.post(f"{_NODES}/{data['node_id']}/heartbeat", headers=_auth(data["token"]))
    client.post(
        f"{_NODES}/{data['node_id']}/status",
        headers=_auth(data["token"]),
        json={
            "system_reserved": {"ram": 8 * _GIB, "vram": 2 * _GIB},
            "status": {
                "memory": {"total": 64 * _GIB, "used": 10 * _GIB, "utilization_rate": 15.6},
                "gpu_devices": [
                    {
                        "index": 0,
                        "name": "RTX 4090",
                        "vendor": "NVIDIA",
                        "memory_total": 24 * _GIB,
                        "memory_used": 1 * _GIB,
                        "utilization_rate": 12.5,
                    }
                ],
            },
        },
    )
    return data


def _create(client: TestClient, monkeypatch, **payload: Any) -> dict:
    """创建实例（patch creation.request_to_worker：权重广播返回 2GiB，启动转发成功）。"""
    async def fake_request_to_worker(node, method, path, **kwargs):
        return FakeResponse({"weight_bytes": 2 * _GIB})

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )
    body: dict[str, Any] = {"model_name": "qwen2.5"}
    body.update(payload)
    resp = client.post(_BASE, json=body)
    assert resp.status_code == 200
    return resp.json()["data"]


def _report_state(client: TestClient, node: dict, instance_id: str, state: str) -> None:
    """经 /report 端点驱动实例状态（apply_report 落库 + 清 target）。"""
    resp = client.post(
        f"{_BASE}/report",
        headers=_auth(node["token"]),
        json={"items": [{"id": instance_id, "state": state}]},
    )
    assert resp.status_code == 200


def test_create_returns_pending(api_client, monkeypatch):
    _ready_node(api_client)

    data = _create(api_client, monkeypatch)

    assert data["id"]
    assert data["state"] == InstanceStateEnum.PENDING.value
    assert data["target_state"] == "starting"
    assert data["node_id"]
    assert data["model_name"] == "qwen2.5"
    assert data["gpu_indexes"] == [0]


def test_list_paginated(api_client, monkeypatch):
    node1 = _ready_node(api_client, machine_id="m-001", hostname="gpu-001")
    node2 = _ready_node(api_client, machine_id="m-002", hostname="gpu-002")
    _create(api_client, monkeypatch, node_id=node1["node_id"])
    _create(api_client, monkeypatch, node_id=node2["node_id"])

    resp = api_client.get(_BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["meta"]["total"] == 2
    assert len(body["data"]["items"]) == 2

    # node_id / state 过滤
    by_node = api_client.get(_BASE, params={"node_id": node1["node_id"]})
    assert by_node.json()["data"]["meta"]["total"] == 1
    by_state = api_client.get(_BASE, params={"state": "pending"})
    assert by_state.json()["data"]["meta"]["total"] == 2


def test_get_detail_and_404(api_client, monkeypatch):
    _ready_node(api_client)
    inst = _create(api_client, monkeypatch)

    resp = api_client.get(f"{_BASE}/{inst['id']}")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == inst["id"]

    missing = api_client.get(f"{_BASE}/no-such-instance")
    assert missing.status_code == 404


def test_start_stop_ok(api_client, monkeypatch):
    node = _ready_node(api_client)
    inst = _create(api_client, monkeypatch)

    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append(path)
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    # start：stopped 可启动
    _report_state(api_client, node, inst["id"], "stopped")
    resp = api_client.post(f"{_BASE}/{inst['id']}/start")
    assert resp.status_code == 200
    assert resp.json()["data"]["target_state"] == "starting"
    assert calls == [f"instances/{inst['id']}/start"]

    # stop：running 可停止
    _report_state(api_client, node, inst["id"], "running")
    resp = api_client.post(f"{_BASE}/{inst['id']}/stop")
    assert resp.status_code == 200
    assert resp.json()["data"]["target_state"] == "stopping"
    assert calls == [f"instances/{inst['id']}/start", f"instances/{inst['id']}/stop"]


def test_start_invalid_state_400(api_client, monkeypatch):
    node = _ready_node(api_client)
    inst = _create(api_client, monkeypatch)
    _report_state(api_client, node, inst["id"], "running")

    resp = api_client.post(f"{_BASE}/{inst['id']}/start")

    # InvalidStateException(BusinessException) 由全局 handler 映射为 400
    assert resp.status_code == 400
    assert resp.json()["data"]["error_code"] == "INVALID_STATE"


def test_stop_invalid_state_400(api_client, monkeypatch):
    node = _ready_node(api_client)
    inst = _create(api_client, monkeypatch)
    _report_state(api_client, node, inst["id"], "stopped")

    resp = api_client.post(f"{_BASE}/{inst['id']}/stop")

    assert resp.status_code == 400


def test_delete_then_404(api_client, monkeypatch):
    node = _ready_node(api_client)
    inst = _create(api_client, monkeypatch)
    _report_state(api_client, node, inst["id"], "stopped")  # 置 stopped 跳过 stop 转发

    resp = api_client.delete(f"{_BASE}/{inst['id']}")
    assert resp.status_code == 200

    got = api_client.get(f"{_BASE}/{inst['id']}")
    assert got.status_code == 404


def test_report_requires_token(api_client):
    resp = api_client.post(f"{_BASE}/report", json={"items": []})
    assert resp.status_code == 401


def test_report_wrong_token_401(api_client):
    _ready_node(api_client)
    resp = api_client.post(f"{_BASE}/report", headers=_auth("wrong-token"), json={"items": []})
    assert resp.status_code == 401


def test_report_route_not_captured(api_client):
    """路由顺序：POST /report 返回 200 而非被 /{instance_id} 捕获成 404/422。"""
    node = _ready_node(api_client)
    resp = api_client.post(
        f"{_BASE}/report", headers=_auth(node["token"]), json={"items": []}
    )
    assert resp.status_code == 200
    assert "对账 0 条" in resp.json()["message"]


def test_report_ok_persists(api_client, monkeypatch):
    node = _ready_node(api_client)
    inst = _create(api_client, monkeypatch)

    resp = api_client.post(
        f"{_BASE}/report",
        headers=_auth(node["token"]),
        json={
            "items": [
                {
                    "id": inst["id"],
                    "state": "running",
                    "state_message": "ok",
                    "port": 8001,
                    "restart_count": 1,
                }
            ]
        },
    )
    assert resp.status_code == 200
    assert "对账 1 条" in resp.json()["message"]

    # 上报已落库（详情可见 running/port，target 清回 none）
    detail = api_client.get(f"{_BASE}/{inst['id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["state"] == "running"
    assert detail.json()["data"]["port"] == 8001
    assert detail.json()["data"]["target_state"] == "none"


def test_report_ignores_foreign_instance(api_client, monkeypatch):
    node1 = _ready_node(api_client, machine_id="m-001", hostname="gpu-001")
    inst = _create(api_client, monkeypatch, node_id=node1["node_id"])
    node2 = _ready_node(api_client, machine_id="m-002", hostname="gpu-002")

    resp = api_client.post(
        f"{_BASE}/report",
        headers=_auth(node2["token"]),
        json={"items": [{"id": inst["id"], "state": "running"}]},
    )
    assert resp.status_code == 200
    assert "对账 0 条" in resp.json()["message"]

    # 非本节点实例未被改动
    detail = api_client.get(f"{_BASE}/{inst['id']}")
    assert detail.json()["data"]["state"] == InstanceStateEnum.PENDING.value
