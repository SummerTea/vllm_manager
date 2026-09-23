import logging
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.base.base_response_schema import BaseResponse
from app.config import app_config
from app.exception import (
    BusinessException,
    DuplicateResourceException,
    HttpClientException,
    HttpException,
    ResourceNotExistException,
    VllmManagerException,
)

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
SAFE_INTERNAL_ERROR_MESSAGE = "服务器内部错误，请稍后重试"


def _path_startswith(path: str, prefix: str) -> bool:
    normalized_prefix = prefix.rstrip("/")
    return path == normalized_prefix or path.startswith(normalized_prefix + "/")


# 静态资源的扩展名。SPA 深路由不带这些后缀，所以按后缀区分是安全的；
# 反过来只判断「有没有点」会把 /knowledge/doc-v1.2 这类合法路由判成资源。
_STATIC_ASSET_SUFFIXES = frozenset(
    {
        ".js", ".mjs", ".cjs", ".css", ".map", ".json", ".wasm",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".avif",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".txt", ".xml", ".webmanifest",
    }
)


def _is_static_asset_request(path: str, prefix: str) -> bool:
    """这条 404 是「静态资源没找到」而不是「SPA 深路由」吗？

    为什么必须分开：给缺失的资源回 index.html，等于让浏览器在请求 JS 时
    拿到 `200 text/html`。module script 的严格 MIME 检查会直接拒绝执行，
    SPA 永远挂载不上——白屏。
    """
    if _path_startswith(path, prefix.rstrip("/") + "/assets"):
        return True
    return PurePosixPath(path).suffix.lower() in _STATIC_ASSET_SUFFIXES


def _is_api_request(request: Request) -> bool:
    return _is_api_path(request.url.path)


def _is_api_path(path: str) -> bool:
    base_path = app_config.BASE_URL_PATH.rstrip("/")
    prefixes = [
        f"{base_path}/api/v1" if base_path else "/api/v1",
        f"{base_path}/web_api" if base_path else "/web_api",
    ]
    return any(_path_startswith(path, prefix) for prefix in prefixes)


def _request_id(request: Request) -> str:
    state_request_id = getattr(request.state, "request_id", None)
    if state_request_id:
        return state_request_id
    return request.headers.get(REQUEST_ID_HEADER) or uuid4().hex


def _merge_headers(
    *headers_list: dict[str, str] | None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header_items in headers_list:
        if header_items:
            headers.update(header_items)
    return headers


class APIRequestIDMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if not _is_api_path(path):
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from_scope(scope)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def _request_id_from_scope(scope: Scope) -> str:
    headers = MutableHeaders(scope=scope)
    return headers.get(REQUEST_ID_HEADER) or uuid4().hex


def _api_error_response(
    *,
    request: Request,
    status_code: int,
    message: str,
    code: int,
    error_code: str | None = None,
    details: Any | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    data: dict[str, Any] = {"request_id": request_id}
    if error_code is not None:
        data["error_code"] = error_code
    if details is not None:
        data["details"] = details

    return JSONResponse(
        status_code=status_code,
        content=BaseResponse.error(message=message, code=code, data=data).model_dump(),
        headers={REQUEST_ID_HEADER: request_id},
    )


def _http_exception_message(exc: HTTPException) -> tuple[str, str, Any | None]:
    if exc.status_code >= 500:
        return SAFE_INTERNAL_ERROR_MESSAGE, "INTERNAL_SERVER_ERROR", None
    if isinstance(exc.detail, str):
        return exc.detail, "HTTP_EXCEPTION", None
    return "请求失败", "HTTP_EXCEPTION", exc.detail


def _html_error_response(
    status_code: int, message: str, headers: dict[str, str] | None = None
) -> HTMLResponse:
    return HTMLResponse(
        f"<h1>{status_code} {message}</h1>",
        status_code=status_code,
        headers=headers,
    )


def register_exception_handlers(app: FastAPI):
    """
    注册全局异常处理器
    """

    app.add_middleware(APIRequestIDMiddleware)

    @app.exception_handler(HttpException)
    async def http_exception_handler(request: Request, exc: HttpException):
        """处理 HTTP 异常"""
        if not _is_api_request(request):
            return _html_error_response(exc.status_code, exc.message)
        return _api_error_response(
            request=request,
            status_code=exc.status_code,
            message=exc.message,
            code=exc.status_code,
            error_code=exc.code,
            details=exc.details,
        )

    @app.exception_handler(BusinessException)
    async def business_exception_handler(request: Request, exc: BusinessException):
        """
        处理业务异常
        默认映射为 400 错误，特定异常可映射为其他状态码
        """
        status_code = 400
        if isinstance(exc, ResourceNotExistException):
            status_code = 404
        elif isinstance(exc, DuplicateResourceException):
            status_code = 409

        if not _is_api_request(request):
            return _html_error_response(status_code, exc.message)
        return _api_error_response(
            request=request,
            status_code=status_code,
            message=exc.message,
            code=-1,
            error_code=exc.code,
            details=exc.details,
        )

    @app.exception_handler(VllmManagerException)
    async def vllm_manager_exception_handler(request: Request, exc: VllmManagerException):
        """处理其他项目异常"""
        if not _is_api_request(request):
            return _html_error_response(500, SAFE_INTERNAL_ERROR_MESSAGE)
        return _api_error_response(
            request=request,
            status_code=500,
            message=exc.message,
            code=500,
            error_code=exc.code,
            details=exc.details,
        )

    @app.exception_handler(HttpClientException)
    async def http_client_exception_handler(request: Request, exc: HttpClientException):
        """处理 worker 转发错误：透传 worker 状态码，避免恒 500 丢失 4xx 语义。

        worker 401/403 → 502：worker 鉴权失败不代表 server 侧前端登录失效，
        映射为 502 防前端误判自身登录态被踢。
        """
        raw = (exc.details or {}).get("status_code") or 502
        status_code = 502 if raw in (401, 403) else int(raw)
        if not _is_api_request(request):
            return _html_error_response(status_code, SAFE_INTERNAL_ERROR_MESSAGE)
        return _api_error_response(
            request=request,
            status_code=status_code,
            message=exc.message,
            code=status_code,
            error_code=exc.code,
            details=exc.details,
        )

    @app.exception_handler(HTTPException)
    async def fastapi_http_exception_handler(request: Request, exc: HTTPException):
        """处理 FastAPI 原生 HTTP 异常"""
        if not _is_api_request(request):
            message = exc.detail if isinstance(exc.detail, str) else "Error"
            return _html_error_response(
                exc.status_code,
                message,
                dict(exc.headers) if exc.headers is not None else None,
            )
        message, error_code, details = _http_exception_message(exc)
        return _api_error_response(
            request=request,
            status_code=exc.status_code,
            message=message,
            code=exc.status_code,
            error_code=error_code,
            details=details,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        """处理请求参数校验异常"""
        if not _is_api_request(request):
            return _html_error_response(422, "Validation Error")
        return _api_error_response(
            request=request,
            status_code=422,
            message="请求参数校验失败",
            code=422,
            error_code="VALIDATION_ERROR",
            details=exc.errors(),
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc):
        """404 处理：API 返回 JSON，SPA 深路由回 index.html，其他返回 HTML"""
        if _is_api_request(request):
            if isinstance(exc, HTTPException) and exc.detail != "Not Found":
                message, error_code, details = _http_exception_message(exc)
                return _api_error_response(
                    request=request,
                    status_code=exc.status_code,
                    message=message,
                    code=exc.status_code,
                    error_code=error_code,
                    details=details,
                )
            return _api_error_response(
                request=request,
                status_code=404,
                message="Not Found",
                code=404,
            )

        # SPA 深路由：前端 base 路径下的非静态资源 404 → 回 index.html
        spa_prefix = app_config.BASE_URL_PATH + "/frontend"
        if request.url.path.startswith(spa_prefix):
            if _is_static_asset_request(request.url.path, spa_prefix):
                return JSONResponse(
                    status_code=404,
                    content=BaseResponse.error(message="Not Found", code=404).model_dump(),
                )
            index_path = app_config.FRONTEND_DIST_DIR / "index.html"
            if index_path.exists():
                # index.html 一旦被缓存，就会长期指向上一版构建的 hash chunk，
                # 而那些 chunk 发版后已经不存在了。必须每次回源校验。
                return FileResponse(
                    str(index_path),
                    headers={"Cache-Control": "no-cache"},
                )

        return _html_error_response(404, "Not Found")

    @app.exception_handler(500)
    async def server_error_handler(request: Request, exc):
        """500 页面"""
        if _is_api_request(request):
            logger.error("Unhandled API exception", exc_info=exc)
            return _api_error_response(
                request=request,
                status_code=500,
                message=SAFE_INTERNAL_ERROR_MESSAGE,
                code=500,
                error_code="INTERNAL_SERVER_ERROR",
            )
        return HTMLResponse("<h1>500 Internal Server Error</h1>", status_code=500)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        """处理未捕获异常"""
        if request.scope["type"] == "websocket":
            logger.exception("Unhandled WebSocket exception")
            return
        if _is_api_request(request):
            logger.exception("Unhandled API exception")
            return _api_error_response(
                request=request,
                status_code=500,
                message=SAFE_INTERNAL_ERROR_MESSAGE,
                code=500,
                error_code="INTERNAL_SERVER_ERROR",
            )
        logger.exception("Unhandled non-API exception")
        return HTMLResponse("<h1>500 Internal Server Error</h1>", status_code=500)
