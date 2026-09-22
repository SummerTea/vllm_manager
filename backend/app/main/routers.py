from fastapi import APIRouter

from app.config import app_config
from app.server.instance.api import instance_router
from app.server.node.api import node_router


def get_api_router() -> APIRouter:
    """聚合所有 API 路由"""
    api_router = APIRouter(prefix=f"{app_config.BASE_URL_PATH}/api/v1")

    # 业务路由在此 include
    api_router.include_router(node_router)
    api_router.include_router(instance_router)

    return api_router
