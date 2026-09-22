"""
Server 模块 Node API 契约测试。

模式：TestClient(app) + dependency_overrides[get_session] 复用 conftest 的
sqlite 内存库 session（不触发 lifespan，不需要真实 PG/Redis）。
每个测试独立 session/engine（conftest session 夹具为 function 级）。
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import app.server.node.model  # noqa: F401  (注册 Node 到 Base.metadata)
from app.extensions.database import get_session
from app.main import app
from app.server.node.service import NodeService

_BASE = "/vllm_manager/api/v1/nodes"


@pytest.fixture
def api_client(session):
    """将 get_session 依赖替换为 conftest 的 sqlite session，测试后清理 override。

    模拟真实 get_session 的请求结束 commit 语义（否则 register 数据停留在未提交
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
    payload: dict = {
        "machine_id": "m-001",
        "hostname": "gpu-01",
        "ip": "10.0.0.1",
        "advertise_address": "10.0.0.1:8100",
        "worker_port": 8100,
    }
    payload.update(overrides)
    resp = client.post(f"{_BASE}/register", json=payload)
    assert resp.status_code == 200
    return resp.json()["data"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_register_public_returns_token(api_client):
    data = _register(api_client)

    assert data["node_id"]
    assert data["token"]
    assert data["worker_port"] == 8100
    assert data["heartbeat_interval"] == 10


def test_register_idempotent_same_token(api_client):
    first = _register(api_client)
    second = _register(api_client, ip="10.0.0.2")

    assert second["node_id"] == first["node_id"]
    assert second["token"] == first["token"]


def test_heartbeat_requires_token(api_client):
    resp = api_client.post(f"{_BASE}/some-id/heartbeat")

    assert resp.status_code == 401


def test_heartbeat_wrong_token_401(api_client):
    data = _register(api_client)
    resp = api_client.post(
        f"{_BASE}/{data['node_id']}/heartbeat",
        headers=_auth("wrong-token"),
    )

    assert resp.status_code == 401


def test_heartbeat_ok_with_token(api_client):
    data = _register(api_client)
    resp = api_client.post(
        f"{_BASE}/{data['node_id']}/heartbeat",
        headers=_auth(data["token"]),
    )
    assert resp.status_code == 200

    # 心跳后 DB 中 state 应派生为 ready（经详情接口验证）
    detail = api_client.get(f"{_BASE}/{data['node_id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["state"] == "ready"


def test_heartbeat_wrong_node_403(api_client):
    a = _register(api_client, machine_id="m-a", hostname="gpu-a")
    b = _register(api_client, machine_id="m-b", hostname="gpu-b")
    resp = api_client.post(
        f"{_BASE}/{b['node_id']}/heartbeat",
        headers=_auth(a["token"]),
    )

    assert resp.status_code == 403


def test_status_report_ok(api_client):
    data = _register(api_client)
    report = {
        "system_reserved": {"ram": 8 * 1024**3, "vram": 2 * 1024**3},
        "status": {
            "memory": {
                "total": 64 * 1024**3,
                "used": 10 * 1024**3,
                "utilization_rate": 15.6,
            },
            "gpu_devices": [
                {
                    "index": 0,
                    "name": "RTX 4090",
                    "vendor": "NVIDIA",
                    "memory_total": 24 * 1024**3,
                    "memory_used": 1 * 1024**3,
                    "utilization_rate": 12.5,
                }
            ],
        },
    }
    resp = api_client.post(
        f"{_BASE}/{data['node_id']}/status",
        headers=_auth(data["token"]),
        json=report,
    )
    assert resp.status_code == 200

    # 列表接口可见 status JSON
    page = api_client.get(_BASE)
    assert page.status_code == 200
    item = page.json()["data"]["items"][0]
    assert item["status"]["gpu_devices"][0]["name"] == "RTX 4090"
    assert item["system_reserved"]["ram"] == 8 * 1024**3


def test_list_nodes_paginated(api_client):
    for i in range(3):
        _register(api_client, machine_id=f"m-{i}", hostname=f"gpu-{i}")

    resp = api_client.get(_BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["meta"]["total"] == 3
    assert len(body["data"]["items"]) == 3


def test_patch_labels(api_client):
    data = _register(api_client)
    resp = api_client.patch(
        f"{_BASE}/{data['node_id']}",
        json={"labels": {"gpu_type": "a100"}},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["labels"] == {"gpu_type": "a100"}


def test_delete_node(api_client):
    data = _register(api_client)
    resp = api_client.delete(f"{_BASE}/{data['node_id']}")

    assert resp.status_code == 200
    got = api_client.get(f"{_BASE}/{data['node_id']}")
    assert got.status_code == 404


def test_register_integrity_error_fallback(api_client):
    """并发重复注册（IntegrityError）走兜底：返回已存在节点而非 500。"""
    first = _register(api_client)

    payload = {
        "machine_id": "m-001",
        "hostname": "gpu-01",
        "ip": "10.0.0.1",
        "advertise_address": "10.0.0.1:8100",
        "worker_port": 8100,
    }
    with patch.object(
        NodeService, "register", side_effect=IntegrityError("stmt", {}, Exception("dup"))
    ):
        resp = api_client.post(f"{_BASE}/register", json=payload)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["node_id"] == first["node_id"]
        assert data["token"] == first["token"]

    # 恢复真实行为后幂等依旧成功
    again = _register(api_client)
    assert again["node_id"] == first["node_id"]
    assert again["token"] == first["token"]


def test_status_requires_token(api_client):
    data = _register(api_client)
    resp = api_client.post(f"{_BASE}/{data['node_id']}/status", json={})

    assert resp.status_code == 401


def test_status_wrong_node_403(api_client):
    a = _register(api_client, machine_id="m-a", hostname="gpu-a")
    b = _register(api_client, machine_id="m-b", hostname="gpu-b")
    resp = api_client.post(
        f"{_BASE}/{b['node_id']}/status",
        headers=_auth(a["token"]),
        json={},
    )

    assert resp.status_code == 403
