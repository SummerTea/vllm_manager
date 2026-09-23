"""
main/lifespan 组合根装配契约测试。

模式：不触发真实资源初始化（monkeypatch init_*/close_* 为 no-op），直接驱动
lifespan 上下文管理器验证组合根装配：
- db 未禁用：probe 后台任务启动，on_node_lost 回调指向 instance 域
  reconcile_lost_nodes（回调真实执行失联联动）
- DISABLED_EXTENSIONS 含 db：不初始化 DB、不启动 probe 后台任务

注：app_config 为 frozen 单例，测试内通过 object.__setattr__ 临时改写
DISABLED_EXTENSIONS，try/finally 保证恢复。
"""

import asyncio
from typing import Any

from fastapi import FastAPI

import app.server.instance.model  # noqa: F401  (注册 VllmInstance 到 Base.metadata)
import app.server.node.model  # noqa: F401  (注册 Node 到 Base.metadata)
from app.config import app_config
from app.main.lifespan import lifespan
from app.server.instance.enum import InstanceStateEnum
from app.server.instance.model import VllmInstance
from app.server.node.schema import NodeRegisterRequest
from app.server.node.service import NodeService


async def _noop(*args, **kwargs) -> None:
    """占位扩展初始化/清理（不碰真实 PG/Redis）。"""
    return None


async def test_lifespan_starts_prober_with_reconcile_callback(session, monkeypatch):
    """db 未禁用：probe 后台任务启动，on_node_lost 回调真实执行 reconcile_lost_nodes。"""
    # lifespan 体内 `from app.server.node.prober import probe_loop` 在调用时取模块
    # 属性 → monkeypatch 模块属性即可截获回调
    captured: dict[str, Any] = {}

    async def fake_probe_loop(on_node_lost=None):
        captured["on_node_lost"] = on_node_lost
        await asyncio.Event().wait()  # 任务常驻，直到 __aexit__ cancel

    monkeypatch.setattr("app.server.node.prober.probe_loop", fake_probe_loop)
    for name in (
        "init_logging",
        "shutdown_logging",
        "init_db",
        "init_redis",
        "init_saq",
        "close_redis",
        "dispose_db",
    ):
        monkeypatch.setattr(f"app.main.lifespan.{name}", _noop)

    cm = lifespan(FastAPI())
    await cm.__aenter__()
    await asyncio.sleep(0)  # 让 create_task 调度的 probe 任务启动（进入 fake_probe_loop）
    try:
        assert "on_node_lost" in captured  # probe 任务已启动并装配回调

        # 回调指向真实 instance 域联动：造 OFFLINE 节点下 running 实例，
        # 直接驱动回调 → running 置 unreachable
        svc = NodeService(session)
        node = await svc.register(
            NodeRegisterRequest(
                machine_id="m-001",
                hostname="gpu-01",
                ip="10.0.0.1",
                advertise_address="10.0.0.1:8100",
                worker_port=8100,
            )
        )
        inst = VllmInstance(
            node_id=node.id,
            state=InstanceStateEnum.RUNNING.value,
            target_state="none",
            model_name="qwen2.5-7b",
            gpu_indexes=[0],
            args=[],
            labels={},
            allocated_vram={},
            restart_count=0,
        )
        session.add(inst)
        await session.flush()

        await captured["on_node_lost"](session, [node])

        assert inst.state == InstanceStateEnum.UNREACHABLE.value
        assert inst.state_message == "节点失联"
    finally:
        await cm.__aexit__(None, None, None)


async def test_lifespan_skips_prober_when_db_disabled(monkeypatch):
    """DISABLED_EXTENSIONS 含 db：不初始化 DB、不启动 probe 后台任务。"""
    called = {"probe": False, "init_db": False, "dispose_db": False}

    async def fake_probe_loop(on_node_lost=None):
        called["probe"] = True
        await asyncio.Event().wait()

    async def noop_init_db(create_tables: bool = False) -> None:
        called["init_db"] = True

    async def noop_dispose_db() -> None:
        called["dispose_db"] = True

    monkeypatch.setattr("app.server.node.prober.probe_loop", fake_probe_loop)
    monkeypatch.setattr("app.main.lifespan.init_db", noop_init_db)
    monkeypatch.setattr("app.main.lifespan.dispose_db", noop_dispose_db)
    for name in (
        "init_logging",
        "shutdown_logging",
        "init_redis",
        "init_saq",
        "close_redis",
    ):
        monkeypatch.setattr(f"app.main.lifespan.{name}", _noop)

    original = app_config.DISABLED_EXTENSIONS
    object.__setattr__(app_config, "DISABLED_EXTENSIONS", ["db"])
    try:
        cm = lifespan(FastAPI())
        await cm.__aenter__()
        try:
            assert called["probe"] is False  # 后台任务不启动
            assert called["init_db"] is False  # DB 扩展不初始化
        finally:
            await cm.__aexit__(None, None, None)
        assert called["dispose_db"] is False  # 退出同样不清理 DB
    finally:
        object.__setattr__(app_config, "DISABLED_EXTENSIONS", original)
