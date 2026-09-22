"""
FastAPI 响应 Schema 模块

提供统一的响应结构：
- 标准响应格式
- 分页响应
- 列表响应
- 空响应
"""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ============ 基础响应 ============


class BaseResponse(BaseModel, Generic[T]):
    """
    统一响应格式

    属性:
        code: 业务状态码（0 表示成功）
        message: 响应消息
        data: 响应数据
    """
    code: int = Field(default=0, description="业务状态码")
    message: str = Field(default="success", description="响应消息")
    data: T | None = Field(default=None, description="响应数据")

    @classmethod
    def success(cls, data: T | None = None, message: str = "success") -> "BaseResponse[T]":
        """成功响应"""
        return cls(code=0, message=message, data=data)

    @classmethod
    def error(
        cls,
        message: str = "error",
        code: int = -1,
        data: T | None = None,
    ) -> "BaseResponse[T]":
        """错误响应"""
        return cls(code=code, message=message, data=data)


# ============ 分页响应 ============


class PageMeta(BaseModel):
    """
    分页元数据

    属性:
        page: 当前页码
        page_size: 每页数量
        total: 总记录数
        total_pages: 总页数
        has_next: 是否有下一页
        has_prev: 是否有上一页
    """
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页数量")
    total: int = Field(description="总记录数")
    total_pages: int = Field(description="总页数")
    has_next: bool = Field(description="是否有下一页")
    has_prev: bool = Field(description="是否有上一页")

    @classmethod
    def create(cls, page: int, page_size: int, total: int) -> "PageMeta":
        """根据分页参数和总数创建分页元数据"""
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        return cls(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_prev=page > 1,
        )


class PageData(BaseModel, Generic[T]):
    """
    分页数据结构

    属性:
        items: 数据列表
        meta: 分页元数据
    """
    items: list[T] = Field(default_factory=list, description="数据列表")
    meta: PageMeta = Field(description="分页元数据")

    @classmethod
    def create(
        cls,
        items: list[T],
        page: int,
        page_size: int,
        total: int,
    ) -> "PageData[T]":
        """创建分页数据"""
        return cls(
            items=items,
            meta=PageMeta.create(page, page_size, total),
        )


class PageResponse(BaseResponse[PageData[T]], Generic[T]):
    """
    分页响应

    继承 BaseResponse，data 固定为 PageData 类型
    """

    @classmethod
    def success(  # type: ignore[override]
        cls,
        items: list[T],
        page: int,
        page_size: int,
        total: int,
        message: str = "success",
    ) -> "PageResponse[T]":
        """创建成功的分页响应"""
        return cls(
            code=0,
            message=message,
            data=PageData.create(items, page, page_size, total),
        )


# ============ 列表响应 ============


class ListData(BaseModel, Generic[T]):
    """
    列表数据结构（不带分页）

    属性:
        items: 数据列表
        count: 数据条数
    """
    items: list[T] = Field(default_factory=list, description="数据列表")
    count: int = Field(default=0, description="数据条数")

    @classmethod
    def create(cls, items: list[T]) -> "ListData[T]":
        """创建列表数据"""
        return cls(items=items, count=len(items))


class ListResponse(BaseResponse[ListData[T]], Generic[T]):
    """
    列表响应（不带分页）

    适用于数据量较少、无需分页的场景
    """

    @classmethod
    def success(  # type: ignore[override]
        cls,
        items: list[T],
        message: str = "success",
    ) -> "ListResponse[T]":
        """创建成功的列表响应"""
        return cls(
            code=0,
            message=message,
            data=ListData.create(items),
        )


# ============ 特殊响应 ============


class EmptyResponse(BaseResponse[None]):
    """
    空响应

    用于删除、更新等无需返回数据的操作
    """

    @classmethod
    def success(  # type: ignore[override]
        cls, message: str = "success"
    ) -> "EmptyResponse":
        """创建成功的空响应"""
        return cls(code=0, message=message, data=None)


class IdResponse(BaseResponse[str]):
    """
    ID 响应

    用于创建操作，返回新创建资源的 ID
    """

    @classmethod
    def success(  # type: ignore[override]
        cls, id: str, message: str = "success"
    ) -> "IdResponse":
        """创建成功的 ID 响应"""
        return cls(code=0, message=message, data=id)


class CountResponse(BaseResponse[int]):
    """
    计数响应

    用于统计操作，返回数量
    """

    @classmethod
    def success(  # type: ignore[override]
        cls, count: int, message: str = "success"
    ) -> "CountResponse":
        """创建成功的计数响应"""
        return cls(code=0, message=message, data=count)


class BatchResultData(BaseModel):
    """
    批量操作结果数据

    属性:
        success_count: 成功数量
        failed_count: 失败数量
        failed_ids: 失败的 ID 列表
        errors: 错误详情
    """
    success_count: int = Field(default=0, description="成功数量")
    failed_count: int = Field(default=0, description="失败数量")
    failed_ids: list[str] = Field(default_factory=list, description="失败的 ID 列表")
    errors: dict[str, str] = Field(default_factory=dict, description="错误详情")


class BatchResponse(BaseResponse[BatchResultData]):
    """
    批量操作响应

    用于批量创建、批量更新、批量删除等操作
    """

    @classmethod
    def success(  # type: ignore[override]
        cls,
        success_count: int,
        failed_count: int = 0,
        failed_ids: list[str] | None = None,
        errors: dict[str, str] | None = None,
        message: str = "success",
    ) -> "BatchResponse":
        """创建批量操作响应"""
        return cls(
            code=0,
            message=message,
            data=BatchResultData(
                success_count=success_count,
                failed_count=failed_count,
                failed_ids=failed_ids or [],
                errors=errors or {},
            ),
        )


# ============ 错误响应 ============


class ErrorDetail(BaseModel):
    """
    错误详情

    属性:
        field: 错误字段（可选）
        message: 错误消息
    """
    field: str | None = Field(default=None, description="错误字段")
    message: str = Field(description="错误消息")


class ErrorData(BaseModel):
    """
    错误数据结构

    属性:
        code: 错误代码
        details: 错误详情列表
    """
    code: str = Field(description="错误代码")
    details: list[ErrorDetail] = Field(default_factory=list, description="错误详情")


class ErrorResponse(BaseResponse[ErrorData]):
    """
    错误响应

    用于返回详细的错误信息
    """

    @classmethod
    def error(  # type: ignore[override]
        cls,
        message: str,
        error_code: str = "ERROR",
        details: list[ErrorDetail] | None = None,
        code: int = -1,
    ) -> "ErrorResponse":
        """创建错误响应"""
        return cls(
            code=code,
            message=message,
            data=ErrorData(code=error_code, details=details or []),
        )

    @classmethod
    def validation_error(
        cls,
        errors: list[dict[str, Any]],
        message: str = "数据验证失败",
    ) -> "ErrorResponse":
        """创建验证错误响应"""
        details = [
            ErrorDetail(
                field=".".join(str(loc) for loc in err.get("loc", [])),
                message=err.get("msg", "验证失败"),
            )
            for err in errors
        ]
        return cls.error(message, "VALIDATION_ERROR", details, code=422)


class KeyValuePair(BaseModel):
    """
    键值对
    """
    key: str = Field(description="键")
    value: Any = Field(description="值")
    label: str | None = Field(default=None, description="标签")
    extra: dict | None = Field(default=None, description="额外信息")

class OptionValue(BaseModel):
    """
    前端下拉框选项值通用载体
    """
    label: str = Field(description="展示标签")
    value: str = Field(description="绑定值")


class OptionResponse(BaseModel):
    """
    前端下拉框列表通用响应体
    """
    options: list[OptionValue] = Field(default_factory=list, description="前端下拉框模型选项")

