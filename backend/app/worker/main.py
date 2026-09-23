"""Worker FastAPI 应用入口（:8100，不连 PG，状态全内存）。

启动流程（lifespan）：
1. 构造 ServerClient 并向 server 注册（失败则启动失败退出）
2. 构造 InstanceLifecycleManager + restore running 实例
3. 启动后台任务：status_loop（采集上报，承担存活语义）/ report_loop（实例对账）/
   sync_loop（生命周期权威判定）
4. yield 后取消全部任务并关闭 http client / lifecycle

独立于 server app：异常处理器见 app.worker.exceptions（JSON-only），
不 import app.main 链（避免拉入 server app_config 配置耦合）。
"""

# ruff: noqa: E402,I001

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI

from app.worker.api import require_worker_token, worker_router
from app.worker.collector import WorkerStatusCollector
from app.worker.config import worker_config
from app.worker.exceptions import register_exception_handlers
from app.worker.lifecycle import InstanceLifecycleManager
from app.worker.server_client import ServerClient

logger = logging.getLogger(__name__)


async def status_loop(
    client: ServerClient,
    collector: WorkerStatusCollector,
    node_id: str,
    token: str,
    interval: int,
) -> None:
    """周期采集并上报节点状态（异常不退出）。"""
    while True:
        try:
            payload = collector.collect()
            await client.report_status(node_id, token, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("状态上报循环异常，进入下一轮")
        await asyncio.sleep(interval)


async def report_loop(
    client: ServerClient,
    lifecycle: InstanceLifecycleManager,
    node_id: str,
    token: str,
    interval: int,
) -> None:
    """周期上报实例快照对账（**空快照也必须上报**——server 依赖「上报消失」分支
    收敛 target=stopping 实例，快照为空若跳过则单实例 stop 后消失信号丢失，server
    永远挂起；异常不退出）。"""
    while True:
        try:
            items = await lifecycle.snapshot_for_report()
            await client.report_instances(node_id, token, items)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("实例对账循环异常，进入下一轮")
        await asyncio.sleep(interval)


async def sync_loop(lifecycle: InstanceLifecycleManager, interval: int = 5) -> None:
    """周期实例状态机收敛（健康检查/超时/退避重启；异常不退出）。"""
    while True:
        try:
            await lifecycle.sync()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("实例状态机循环异常，进入下一轮")
        await asyncio.sleep(interval)


@asynccontextmanager
async def worker_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Worker 生命周期：注册 → 实例管理 restore → 后台循环 → 清理。"""
    client = ServerClient(worker_config)
    app.state.server_client = client
    app.state.worker_collector = WorkerStatusCollector(worker_config)

    # 注册失败（重试耗尽）抛异常 → 启动失败退出
    reg = await client.register()
    app.state.node_id = reg.node_id
    app.state.node_token = reg.token
    logger.info("worker 注册成功: node_id=%s", reg.node_id)

    # 实例生命周期管理 + restore running 实例（stopped 不恢复）
    lifecycle = InstanceLifecycleManager(
        worker_config, reg.node_id, reg.token
    )
    await lifecycle.restore()
    app.state.lifecycle = lifecycle

    tasks = [
        asyncio.create_task(
            status_loop(
                client,
                app.state.worker_collector,
                reg.node_id,
                reg.token,
                worker_config.WORKER_STATUS_INTERVAL,
            ),
            name="worker-status",
        ),
        asyncio.create_task(
            report_loop(
                client,
                lifecycle,
                reg.node_id,
                reg.token,
                worker_config.WORKER_REPORT_INTERVAL,
            ),
            name="worker-report",
        ),
        asyncio.create_task(
            sync_loop(lifecycle),
            name="worker-sync",
        ),
    ]

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await lifecycle.aclose()
        await client.aclose()


def create_app() -> FastAPI:
    """构造 worker FastAPI 应用（lifespan 固定为 worker_lifespan）。"""
    app = FastAPI(
        title="vllm_manager worker",
        description="GPU 机器管控节点（:8100，不连 PG）",
        version="0.1.0",
        lifespan=worker_lifespan,
        docs_url=None,
        redoc_url=None,
    )
    register_exception_handlers(app)

    @app.get("/healthz")
    async def healthz():
        """存活探针（Bearer 豁免，liveness 探针暴露给 LB/监控）。"""
        return {"status": "ok"}

    # 实例管理路由（除 healthz 外全部 Bearer 鉴权）
    app.include_router(
        worker_router, dependencies=[Depends(require_worker_token)]
    )

    return app


app = create_app()
