"""
通用异常模块

提供分层的异常体系，覆盖应用各层的错误处理需求：
- 基础异常：所有自定义异常的根类
- HTTP 异常：API 层的标准 HTTP 错误
- 业务异常：服务层的业务逻辑错误
- 外部服务异常：第三方服务调用错误
"""

from typing import Any

# ============ 基础异常 ============


class VllmManagerException(Exception):
    """
    应用基础异常

    所有自定义异常的根类，提供统一的错误结构

    属性:
        message: 错误描述信息
        code: 错误代码（用于前端识别和国际化）
        details: 附加错误详情
    """

    def __init__(
        self,
        message: str = "系统错误",
        code: str = "VLLM_MANAGER_ERROR",
        details: dict[str, Any] | None = None,
    ):
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式，便于 JSON 序列化"""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


# ============ HTTP 异常 ============


class HttpException(VllmManagerException):
    """
    HTTP 异常基类

    用于 API 层返回标准 HTTP 错误响应

    属性:
        status_code: HTTP 状态码
    """

    status_code: int = 500

    def __init__(
        self,
        message: str = "服务器内部错误",
        code: str = "HTTP_ERROR",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, code, details)


class UnauthorizedException(HttpException):
    """401 未认证"""

    status_code = 401

    def __init__(
        self,
        message: str = "未认证，请先登录",
        code: str = "UNAUTHORIZED",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, code, details)


class ForbiddenException(HttpException):
    """403 禁止访问"""

    status_code = 403

    def __init__(
        self,
        message: str = "无权限访问该资源",
        code: str = "FORBIDDEN",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, code, details)


class NotFoundException(HttpException):
    """404 资源不存在"""

    status_code = 404

    def __init__(
        self,
        message: str = "资源不存在",
        code: str = "NOT_FOUND",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, code, details)


class ConflictException(HttpException):
    """409 资源冲突"""

    status_code = 409

    def __init__(
        self,
        message: str = "资源冲突",
        code: str = "CONFLICT",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, code, details)


# ============ 业务异常 ============


class BusinessException(VllmManagerException):
    """
    业务异常基类

    用于服务层的业务逻辑错误
    """

    def __init__(
        self,
        message: str = "业务处理失败",
        code: str = "BUSINESS_ERROR",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, code, details)


class ResourceNotExistException(BusinessException):
    """资源不存在异常"""

    def __init__(
        self,
        message: str = "资源不存在",
        resource_type: str | None = None,
        resource_id: Any | None = None,
    ):
        details = {}
        if resource_type:
            details["resource_type"] = resource_type
        if resource_id:
            details["resource_id"] = str(resource_id)
        super().__init__(message, "RESOURCE_NOT_FOUND", details)


class DuplicateResourceException(BusinessException):
    """资源重复异常"""

    def __init__(
        self,
        message: str = "资源已存在",
        field: str | None = None,
        value: Any | None = None,
    ):
        details = {}
        if field:
            details["field"] = field
        if value:
            details["value"] = str(value)
        super().__init__(message, "DUPLICATE_RESOURCE", details)


class InvalidStateException(BusinessException):
    """无效状态异常"""

    def __init__(
        self,
        message: str = "当前状态不允许此操作",
        current_state: str | None = None,
        expected_state: str | None = None,
    ):
        details = {}
        if current_state:
            details["current_state"] = current_state
        if expected_state:
            details["expected_state"] = expected_state
        super().__init__(message, "INVALID_STATE", details)


class VersionConflictException(BusinessException):
    """版本冲突异常（乐观锁）"""

    def __init__(
        self,
        message: str = "资源版本冲突，请刷新后重试",
        expected_version: int | None = None,
        current_version: int | None = None,
    ):
        details = {}
        if expected_version is not None:
            details["expected_version"] = expected_version
        if current_version is not None:
            details["current_version"] = current_version
        super().__init__(message, "VERSION_CONFLICT", details)


class OperationNotAllowedException(BusinessException):
    """操作不允许异常"""

    def __init__(
        self,
        message: str = "当前操作不被允许",
        operation: str | None = None,
        reason: str | None = None,
    ):
        details = {}
        if operation:
            details["operation"] = operation
        if reason:
            details["reason"] = reason
        super().__init__(message, "OPERATION_NOT_ALLOWED", details)


# ============ 外部服务异常 ============


class ExternalServiceException(VllmManagerException):
    """
    外部服务异常基类

    用于第三方服务调用错误
    """

    def __init__(
        self,
        message: str = "外部服务调用失败",
        code: str = "EXTERNAL_SERVICE_ERROR",
        service_name: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        details = details or {}
        if service_name:
            details["service_name"] = service_name
        super().__init__(message, code, details)


class DatabaseException(ExternalServiceException):
    """数据库异常"""

    def __init__(
        self,
        message: str = "数据库操作失败",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, "DATABASE_ERROR", "database", details)


class RedisException(ExternalServiceException):
    """Redis 异常"""

    def __init__(
        self,
        message: str = "Redis 操作失败",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, "REDIS_ERROR", "redis", details)


class HttpClientException(ExternalServiceException):
    """HTTP 客户端异常"""

    def __init__(
        self,
        message: str = "HTTP 请求失败",
        url: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        details = details or {}
        if url:
            details["url"] = url
        if status_code:
            details["status_code"] = status_code
        super().__init__(message, "HTTP_CLIENT_ERROR", "http_client", details)
