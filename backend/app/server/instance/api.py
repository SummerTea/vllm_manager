"""Server 模块 Instance 公共 API（/vllm_manager/api/v1/instances）。

安全姿态（过渡期）：管理端点（create/list/get/start/stop/delete）当前未接入
会话鉴权，仅 /report 走 worker 节点 token 鉴权。管理端点开放是过渡姿态，待
web 鉴权里程碑接入后收敛。部署约束：仅限内网/本机可达，禁止直接暴露公网。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.base.base_request_schema import PaginationParams
from app.base.base_response_schema import BaseResponse, EmptyResponse, PageResponse
from app.exception import ResourceNotExistException
from app.extensions.database import get_session
from app.server.instance.enum import InstanceStateEnum
from app.server.instance.schema import (
    InstanceReportRequest,
    VllmInstanceCreateRequest,
    VllmInstanceOut,
)
from app.server.instance.service import VllmInstanceService
from app.server.node.dependencies import get_current_node
from app.server.node.model import Node

# 路由注册顺序：/report 必须在 /{instance_id} 之前，避免被路径参数捕获
instance_router = APIRouter(prefix="/instances", tags=["实例管理"])


@instance_router.post("/report", response_model=BaseResponse[None])
async def instance_report(
    report: InstanceReportRequest,
    current_node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[None]:
    """实例状态对账（worker 鉴权）：合并上报并处理消失实例。"""
    count = await VllmInstanceService(session).apply_report(
        current_node.id, report
    )
    return BaseResponse.success(message=f"对账 {count} 条")


@instance_router.post("", response_model=BaseResponse[VllmInstanceOut])
async def create_instance(
    data: VllmInstanceCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[VllmInstanceOut]:
    """创建 vLLM 实例（候选节点→权重→分配→建记录→转发启动）。"""
    inst = await VllmInstanceService(session).create(data)
    return BaseResponse.success(
        VllmInstanceOut.model_validate(inst), message="创建成功"
    )


@instance_router.get("", response_model=PageResponse[VllmInstanceOut])
async def list_instances(
    params: Annotated[PaginationParams, Depends()],
    session: Annotated[AsyncSession, Depends(get_session)],
    state: Annotated[InstanceStateEnum | None, Query(description="按状态过滤")] = None,
    node_id: Annotated[str | None, Query(description="按节点过滤")] = None,
) -> PageResponse[VllmInstanceOut]:
    """实例分页列表（支持 state/node_id 过滤，非法 state 值由 FastAPI 直接 422）。"""
    filters: dict[str, Any] = {}
    if state is not None:
        filters["state"] = state.value
    if node_id is not None:
        filters["node_id"] = node_id

    result = await VllmInstanceService(session).paginate(
        page=params.page,
        page_size=params.page_size,
        filters=filters,
        order_by=["-created_at"],
    )
    items = [VllmInstanceOut.model_validate(inst) for inst in result["items"]]
    return PageResponse.success(
        items=items,
        page=params.page,
        page_size=params.page_size,
        total=result["total"],
    )


@instance_router.get("/{instance_id}", response_model=BaseResponse[VllmInstanceOut])
async def get_instance(
    instance_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[VllmInstanceOut]:
    """实例详情。"""
    inst = await VllmInstanceService(session).get_by_id(instance_id)
    if inst is None:
        raise ResourceNotExistException(
            "实例不存在", resource_type="vllm_instance", resource_id=instance_id
        )
    return BaseResponse.success(VllmInstanceOut.model_validate(inst))


@instance_router.post(
    "/{instance_id}/start", response_model=BaseResponse[VllmInstanceOut]
)
async def start_instance(
    instance_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[VllmInstanceOut]:
    """启动实例（仅 stopped/error 可启动）。"""
    inst = await VllmInstanceService(session).start(instance_id)
    return BaseResponse.success(VllmInstanceOut.model_validate(inst))


@instance_router.post(
    "/{instance_id}/stop", response_model=BaseResponse[VllmInstanceOut]
)
async def stop_instance(
    instance_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[VllmInstanceOut]:
    """停止实例（running/starting/unreachable 可停止）。"""
    inst = await VllmInstanceService(session).stop(instance_id)
    return BaseResponse.success(VllmInstanceOut.model_validate(inst))


@instance_router.delete("/{instance_id}", response_model=EmptyResponse)
async def delete_instance(
    instance_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EmptyResponse:
    """删除实例（非 stopped 先尽力转发 stop，随后物理删除）。"""
    await VllmInstanceService(session).delete(instance_id)
    return EmptyResponse.success(message="删除成功")
