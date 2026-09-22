"""Worker 独立异常处理器（JSON-only，零 app.config/app.main import）。

为什么独立：worker 端点路径不带 /vllm_manager/api/v1 前缀，server 的
register_exception_handlers（基于 _is_api_request 判定）会对 worker 端点
渲染 HTML 而非 JSON；且其 import 链会拉入 server app_config（DISABLED_EXTENSIONS
解析耦合）。本模块只依赖 app.exception 与 fastapi，响应统一 JSON。

业务异常 → 400（ResourceNotExist→404、DuplicateResource→409，与 server 语义一致）；
HttpException → 其自身 status_code；校验失败 → 422；未捕获 → 500。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.exception import (
    BusinessException,
    DuplicateResourceException,
    HttpException,
    ResourceNotExistException,
)

logger = logging.getLogger(__name__)


def _error_response(status_code: int, message: str, code: str | None = None) -> JSONResponse:
    """统一错误 JSON（对齐 server BaseResponse 消息形态）。"""
    body = {"code": -1, "message": message, "data": None}
    if code is not None:
        body["data"] = {"error_code": code}
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    """注册 worker JSON 异常处理器（全部返回 JSON，不渲染 HTML）。"""

    @app.exception_handler(HttpException)
    async def http_exception_handler(request: Request, exc: HttpException):
        return _error_response(exc.status_code, exc.message, exc.code)

    @app.exception_handler(BusinessException)
    async def business_exception_handler(request: Request, exc: BusinessException):
        status_code = 400
        if isinstance(exc, ResourceNotExistException):
            status_code = 404
        elif isinstance(exc, DuplicateResourceException):
            status_code = 409
        return _error_response(status_code, exc.message, exc.code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return _error_response(422, "请求参数校验失败", "VALIDATION_ERROR")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Worker 未捕获异常")
        return _error_response(500, "服务器内部错误，请稍后重试", "INTERNAL_SERVER_ERROR")
