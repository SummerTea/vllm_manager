"""
Server 模块 vLLM 启动模板契约测试。

模式：sqlite 内存库 session 夹具 + TestClient（不触发 lifespan，seed 手动调用）；
顶部 import instance model 确保 VllmStartTemplate 注册进 Base.metadata 被 create_all
建表。
"""

import pytest
from fastapi.testclient import TestClient

import app.server.instance.model  # noqa: F401  (注册 VllmStartTemplate/VllmInstance)
from app.exception import ResourceNotExistException
from app.extensions.database import get_session
from app.main import app
from app.server.instance.service.start_template import (
    DEFAULT_VLLM_RUN_TEMPLATE,
    VllmStartTemplateService,
    seed_start_templates,
)

_BASE = "/vllm_manager/api/v1/instances/start-templates"


@pytest.fixture
def api_client(session):
    """将 get_session 依赖替换为 conftest 的 sqlite session，测试后清理 override。"""

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


# ---------- seed ----------


async def test_seed_idempotent(session):
    await seed_start_templates(session)
    count = await VllmStartTemplateService(session).count()
    assert count == 2

    await seed_start_templates(session)  # 第二次跳过
    count = await VllmStartTemplateService(session).count()
    assert count == 2

    keys = {
        tpl.template_key
        for tpl in await VllmStartTemplateService(session).get_list()
    }
    assert keys == {"vllm-default", "vllm-gpus-env"}


# ---------- resolve_template ----------


async def test_resolve_default_and_by_key(session):
    await seed_start_templates(session)
    service = VllmStartTemplateService(session)

    # 缺省取默认模板（vllm-default 内容）
    resolved = await service.resolve_template(None)
    assert resolved == DEFAULT_VLLM_RUN_TEMPLATE
    assert "docker run" in resolved
    assert "--gpus device=" in resolved

    # 指定 key 取对应内容
    resolved = await service.resolve_template("vllm-gpus-env")
    assert "NVIDIA_VISIBLE_DEVICES" in resolved

    # 未知 key → ResourceNotExistException
    with pytest.raises(ResourceNotExistException) as excinfo:
        await service.resolve_template("no-such-template")
    assert excinfo.value.details["resource_type"] == "vllm_start_template"
    assert excinfo.value.details["resource_id"] == "no-such-template"


async def test_resolve_fallback_without_seed(session):
    """表空（无默认模板）时兜底返回内置模板，create 恒能拿到快照。"""
    service = VllmStartTemplateService(session)
    resolved = await service.resolve_template(None)
    assert resolved == DEFAULT_VLLM_RUN_TEMPLATE


# ---------- 单默认约束 ----------


async def test_single_default_constraint(session):
    """任一时刻至多一个 is_default=True：create/update 设为默认时清其他默认。"""
    service = VllmStartTemplateService(session)

    await service.create(
        {
            "template_key": "tpl-a",
            "template": "docker run {args}",
            "is_default": True,
        }
    )
    b = await service.create(
        {
            "template_key": "tpl-b",
            "template": "docker run {args}",
            "is_default": True,
        }
    )

    # create 场景：B 为唯一默认，A 被清
    a_re = await service.get_by_key("tpl-a")
    assert a_re is not None and a_re.is_default is False
    assert b.is_default is True
    defaults = await service.get_list(filters={"is_default": True})
    assert len(defaults) == 1
    assert defaults[0].id == b.id

    # get_default 返回唯一默认（确定性）
    default = await service.get_default()
    assert default is not None and default.id == b.id

    # patch 场景：将 A 设为默认 → B 被清
    await service.update(a_re.id, {"is_default": True})
    a_re2 = await service.get_by_key("tpl-a")
    b_re = await service.get_by_key("tpl-b")
    assert a_re2 is not None and a_re2.is_default is True
    assert b_re is not None and b_re.is_default is False
    default = await service.get_default()
    assert default is not None and default.id == a_re.id

    # update 未设 is_default（如只改 description）不清其他默认
    await service.update(b_re.id, {"description": "说明"})
    defaults = await service.get_list(filters={"is_default": True})
    assert len(defaults) == 1
    assert defaults[0].id == a_re.id


async def test_single_default_constraint_no_wildcard_clear(session):
    """create 非默认模板不清既有默认（is_default=False 不触发清理）。"""
    service = VllmStartTemplateService(session)
    default = await service.create(
        {
            "template_key": "tpl-default",
            "template": "docker run {args}",
            "is_default": True,
        }
    )
    await service.create(
        {
            "template_key": "tpl-normal",
            "template": "docker run {args}",
            "is_default": False,
        }
    )
    defaults = await service.get_list(filters={"is_default": True})
    assert len(defaults) == 1
    assert defaults[0].id == default.id


# ---------- CRUD（API 层，含 is_default 删除守卫） ----------


def test_crud(api_client):
    # create
    resp = api_client.post(
        _BASE,
        json={
            "template_key": "my-tpl",
            "template": "docker run --name {name} {image} {args}",
            "description": "自定义模板",
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["template_key"] == "my-tpl"
    assert data["instance_type"] == "vllm"
    assert data["is_active"] is True
    assert data["is_default"] is False

    # get by key
    resp = api_client.get(f"{_BASE}/my-tpl")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == data["id"]

    # get missing → 404
    resp = api_client.get(f"{_BASE}/no-such-tpl")
    assert resp.status_code == 404

    # duplicate create → 409 DUPLICATE_RESOURCE（全局 handler 约定：重复资源 409）
    resp = api_client.post(
        _BASE, json={"template_key": "my-tpl", "template": "dup"}
    )
    assert resp.status_code == 409
    assert resp.json()["data"]["error_code"] == "DUPLICATE_RESOURCE"

    # update
    resp = api_client.patch(f"{_BASE}/my-tpl", json={"description": "更新说明"})
    assert resp.status_code == 200
    assert resp.json()["data"]["description"] == "更新说明"

    # list（含新建模板）
    resp = api_client.get(_BASE)
    assert resp.status_code == 200
    keys = {item["template_key"] for item in resp.json()["data"]["items"]}
    assert "my-tpl" in keys

    # delete is_default → OperationNotAllowedException(400)
    api_client.post(
        _BASE,
        json={"template_key": "def-tpl", "template": "x", "is_default": True},
    )
    resp = api_client.delete(f"{_BASE}/def-tpl")
    assert resp.status_code == 400
    assert resp.json()["data"]["error_code"] == "OPERATION_NOT_ALLOWED"

    # delete normal
    resp = api_client.delete(f"{_BASE}/my-tpl")
    assert resp.status_code == 200
    resp = api_client.get(f"{_BASE}/my-tpl")
    assert resp.status_code == 404
