"""Worker 实例管理 API 契约测试（healthz 豁免 / start/stop/weight 鉴权与受理）。"""

import pytest
from fastapi.testclient import TestClient

from app.worker.main import create_app


class _FakeLifecycle:
    """注入的 lifecycle 假实现（记录调用）。"""

    def __init__(self):
        self.started = []
        self.stopped = []
        self.weights = []

    async def start(self, instance_id, request):
        self.started.append((instance_id, request))
        return {"status": "accepted"}

    async def stop(self, instance_id):
        self.stopped.append(instance_id)
        return {"status": "accepted"}

    async def weight(self, request):
        self.weights.append(request.model_name)
        return {"weight_bytes": 1234}


@pytest.fixture
def client():
    """直接实例化 app（不触发 lifespan），注入 fake lifecycle + node_token。"""
    app = create_app()
    app.state.node_token = "tok-1"
    app.state.lifecycle = _FakeLifecycle()
    return TestClient(app, raise_server_exceptions=False)


def test_healthz_no_auth(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_start_requires_token(client):
    resp = client.post(
        "/instances/i-1/start",
        json={
            "instance_type": "vllm",
            "spec": {
                "model_name": "qwen2.5",
                "task": "auto",
                "gpu_memory_utilization": None,
                "tensor_parallel_size": 1,
                "args": [],
            },
            "gpu_indexes": [0],
            "vram_claim": 16 * 1024**3,
        },
    )
    assert resp.status_code == 401


def test_start_wrong_token(client):
    resp = client.post(
        "/instances/i-1/start",
        headers={"Authorization": "Bearer wrong-token"},
        json={
            "instance_type": "vllm",
            "spec": {"model_name": "qwen2.5"},
            "gpu_indexes": [0],
            "vram_claim": 16 * 1024**3,
        },
    )
    assert resp.status_code == 401


def test_start_accepted(client):
    resp = client.post(
        "/instances/i-1/start",
        headers={"Authorization": "Bearer tok-1"},
        json={
            "instance_type": "vllm",
            "spec": {"model_name": "qwen2.5"},
            "gpu_indexes": [0],
            "vram_claim": 16 * 1024**3,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert client.app.state.lifecycle.started[0][0] == "i-1"
    assert client.app.state.lifecycle.started[0][1].template is None  # 缺省


def test_start_accepted_with_template(client):
    """StartRequest 接受 template 快照字段（server _build_start_payload 顶层下发）。"""
    tpl = "docker run --name {name} --network host --shm-size {shm_size} {image}"
    resp = client.post(
        "/instances/i-1/start",
        headers={"Authorization": "Bearer tok-1"},
        json={
            "instance_type": "vllm",
            "spec": {"model_name": "qwen2.5"},
            "gpu_indexes": [0],
            "vram_claim": 16 * 1024**3,
            "template": tpl,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert client.app.state.lifecycle.started[0][1].template == tpl


def test_stop_idempotent_with_token(client):
    resp = client.post(
        "/instances/i-1/stop",
        headers={"Authorization": "Bearer tok-1"},
        json={"instance_type": "vllm"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert client.app.state.lifecycle.stopped == ["i-1"]


def test_stop_requires_token(client):
    resp = client.post("/instances/i-1/stop", json={"instance_type": "vllm"})
    assert resp.status_code == 401


def test_weight_with_token(client):
    resp = client.post(
        "/models/weight",
        headers={"Authorization": "Bearer tok-1"},
        json={"model_name": "qwen2.5"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"weight_bytes": 1234}


def test_weight_requires_token(client):
    resp = client.post("/models/weight", json={"model_name": "qwen2.5"})
    assert resp.status_code == 401
