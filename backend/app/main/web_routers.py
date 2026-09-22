"""
前端内部 Web API 路由聚合。

注意：文件中的 /account/* 路由是**契约占位**（前端 UserContext 已按此路径调用），
统一返回 501 未实现。业务方实现登录/登出/用户信息接口后，直接替换对应
handler 即可，路径与响应结构保持不变。
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.base.base_response_schema import BaseResponse
from app.config import app_config


def _not_implemented() -> JSONResponse:
    """契约占位统一响应：501 未实现"""
    return JSONResponse(
        status_code=501,
        content=BaseResponse.error(
            message="Not Implemented: account 接口由业务方实现", code=501
        ).model_dump(),
    )


def _build_account_router() -> APIRouter:
    """账户认证契约路由（占位，业务方实现后替换）"""
    account_router = APIRouter(prefix="/account")

    @account_router.post("/login")
    async def account_login() -> JSONResponse:
        """登录（契约占位）：body 任意，由业务方实现"""
        return _not_implemented()

    @account_router.post("/logout")
    async def account_logout() -> JSONResponse:
        """登出（契约占位）"""
        return _not_implemented()

    @account_router.get("/profile")
    async def account_profile() -> JSONResponse:
        """当前用户信息（契约占位）"""
        return _not_implemented()

    return account_router


def get_web_api_router() -> APIRouter:
    """聚合所有供前端专用的内部 Web API 路由"""
    web_api_router = APIRouter(prefix=f"{app_config.BASE_URL_PATH}/web_api")

    # 认证契约占位：前端 UserContext 按此路径调用
    web_api_router.include_router(_build_account_router())

    # 业务路由在此 include，例如：
    # web_api_router.include_router(xxx_web_api_router)

    return web_api_router
