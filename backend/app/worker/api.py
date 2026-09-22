"""Worker 实例管理 API（裸路径，无 /vllm_manager 前缀——契约 §2 server→worker）。

端点：
- POST /instances/{instance_id}/start：受理启动（2xx=已受理，始终 200 accepted）
- POST /instances/{instance_id}/stop：幂等停止
- POST /models/weight：权重广播查询

鉴权：除 /healthz（挂在 app 上，豁免）外，全部 Bearer（node token）——
由 main.create_app 挂载路由时统一注入 dependencies=[Depends(require_worker_token)]。
"""

import secrets

from fastapi import APIRouter, Request

from app.exception import UnauthorizedException
from app.worker.lifecycle import InstanceLifecycleManager
from app.worker.schema import StartRequest, StopRequest, WeightRequest

worker_router = APIRouter(tags=["worker 实例管理"])


async def require_worker_token(request: Request) -> None:
    """Bearer 鉴权依赖：token 与注册响应下发的 node token 恒时比对。

    缺失/不匹配 → 401 Unauthorized。
    """
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    node_token: str | None = getattr(request.app.state, "node_token", None)
    if scheme.lower() != "bearer" or not token or node_token is None:
        raise UnauthorizedException("未提供节点令牌")
    if not secrets.compare_digest(token, node_token):
        raise UnauthorizedException("无效的节点令牌")


def _get_lifecycle(request: Request) -> InstanceLifecycleManager:
    return request.app.state.lifecycle


@worker_router.post("/instances/{instance_id}/start")
async def start_instance(
    instance_id: str,
    body: StartRequest,
    request: Request,
) -> dict:
    """受理启动指令（始终 200 accepted；实际状态经 report 对账）。"""
    lifecycle = _get_lifecycle(request)
    return await lifecycle.start(instance_id, body)


@worker_router.post("/instances/{instance_id}/stop")
async def stop_instance(
    instance_id: str,
    body: StopRequest,
    request: Request,
) -> dict:
    """停止实例（幂等：不存在也返回 accepted）。"""
    lifecycle = _get_lifecycle(request)
    return await lifecycle.stop(instance_id)


@worker_router.post("/models/weight")
async def model_weight(
    body: WeightRequest,
    request: Request,
) -> dict:
    """权重查询（返回 {weight_bytes} 供 server 消费；目录不存在 → 404）。"""
    lifecycle = _get_lifecycle(request)
    return await lifecycle.weight(body)
