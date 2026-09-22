"""
FastAPI 请求 Schema 模块

提供通用的请求参数结构：
- 分页参数
- 排序参数
- 批量操作参数
- 通用查询参数
"""
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, Field, field_validator

from .base_enum import BaseStrEnum

T = TypeVar("T")


# ============ 枚举定义 ============


class SortOrder(BaseStrEnum):
    """排序方向"""
    ASC = "asc"
    DESC = "desc"


# ============ 分页与排序 ============


class PaginationParams(BaseModel):
    """
    分页参数

    属性:
        page: 页码（从 1 开始）
        page_size: 每页数量（默认 20，最大 100）
    """
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量")

    @property
    def offset(self) -> int:
        """计算偏移量"""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """返回限制数量"""
        return self.page_size


class SortParams(BaseModel):
    """
    排序参数

    属性:
        sort_by: 排序字段
        sort_order: 排序方向（asc/desc）
    """
    sort_by: str | None = Field(default=None, description="排序字段")
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="排序方向")


class PageSortParams(PaginationParams, SortParams):
    """分页 + 排序组合参数"""


# ============ 批量操作 ============


class IdsRequest(BaseModel):
    """
    批量 ID 请求

    用于批量删除、批量更新等场景
    """
    ids: list[str] = Field(..., min_length=1, max_length=100, description="ID 列表")


class BatchRequest(BaseModel, Generic[T]):
    """
    批量数据请求

    用于批量创建、批量更新等场景
    """
    items: list[T] = Field(..., min_length=1, max_length=100, description="数据列表")


# ============ 通用查询 ============


class KeywordSearchParams(BaseModel):
    """
    关键词搜索参数

    属性:
        keyword: 搜索关键词
        search_fields: 搜索字段列表（可选）
    """
    keyword: str | None = Field(default=None, max_length=100, description="搜索关键词")
    search_fields: list[str] | None = Field(default=None, description="搜索字段")

    @field_validator("keyword")
    @classmethod
    def strip_keyword(cls, v: str | None) -> str | None:
        """去除关键词首尾空格"""
        return v.strip() if v else None


class DateRangeParams(BaseModel):
    """
    日期范围参数

    属性:
        start_date: 开始日期（ISO 格式）
        end_date: 结束日期（ISO 格式）
    """
    start_date: str | None = Field(default=None, description="开始日期")
    end_date: str | None = Field(default=None, description="结束日期")


class StatusFilterParams(BaseModel):
    """
    状态过滤参数

    属性:
        is_active: 是否启用
        is_deleted: 是否已删除
    """
    is_active: bool | None = Field(default=None, description="是否启用")
    is_deleted: bool | None = Field(default=False, description="是否已删除")


# ============ 组合查询参数 ============


class ListQueryParams(PageSortParams, KeywordSearchParams, StatusFilterParams):
    """
    列表查询参数

    组合分页、排序、关键词搜索、状态过滤
    """


# ============ 依赖注入辅助 ============


# FastAPI 依赖注入类型别名
PageParams = Annotated[PaginationParams, "分页参数"]
SortablePageParams = Annotated[PageSortParams, "分页排序参数"]
ListParams = Annotated[ListQueryParams, "列表查询参数"]
