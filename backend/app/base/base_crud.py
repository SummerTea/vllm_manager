"""
通用 CRUD 服务模块
提供标准的数据库 CRUD 操作基类和服务层异常

适用于样板工程架构（FastAPI + SQLAlchemy 2.0 声明式）
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Generic, TypeVar

from sqlalchemy import asc, delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.base.base_model import Base
from app.exception import ResourceNotExistException
from app.exception import VersionConflictException as ConflictException

logger = logging.getLogger(__name__)

# 泛型类型变量
T = TypeVar("T", bound=Base)

# ============ 基础服务 ============

class BaseCrudService(Generic[T]):
    """
    通用 CRUD 服务基类

    设计原则:
    - 异步优先：所有操作均为 async/await
    - 类型安全：使用泛型确保类型检查
    - 组合复用：通过继承扩展功能
    - 事务灵活：通过 commit 参数控制是否提交

    使用示例:
        class User(IdMixin, BaseModel):
            __tablename__ = "users"
            username: Mapped[str] = mapped_column(String(100))

        class UserService(BaseCrudService[User]):
            pass

        # 在路由中使用（默认不提交，由外部控制事务）
        @app.post("/users")
        async def create_user(
            data: dict,
            session: AsyncSession = Depends(get_session)
        ):
            service = UserService(session)
            user = await service.create(data, commit=True)
            return user
    """

    model: type[T]

    def __init__(self, session: AsyncSession):
        self.session = session

    def __init_subclass__(cls, **kwargs):
        """子类初始化钩子，自动从泛型参数推断模型类"""
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", []):
            if hasattr(base, "__args__") and base.__args__:
                model_type = base.__args__[0]
                if isinstance(model_type, type) and issubclass(model_type, Base):
                    cls.model = model_type
                    break

    # ============ 内部方法 ============

    async def _flush_or_commit(self, commit: bool = False) -> None:
        """根据参数决定 flush 或 commit"""
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()

    def _build_stmt(
        self,
        stmt: Any,
        filters: dict[str, Any] | None = None,
        like_filters: dict[str, Any] | None = None,
        not_like_filters: dict[str, Any] | None = None,
        in_filters: dict[str, list[Any]] | None = None,
        not_in_filters: dict[str, list[Any]] | None = None,
        range_filters: dict[str, dict[str, Any]] | None = None,
        null_filters: list[str] | None = None,
        not_null_filters: list[str] | None = None,
        order_by: list[str] | str | None = None,
        sa_filters: Sequence[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        构建查询语句（应用过滤和排序）
        
        Args:
            filters: 精确匹配 {field: value}
            like_filters: 模糊匹配 {field: value} -> field ILIKE %value%
            not_like_filters: NOT LIKE 匹配 {field: value} -> field NOT ILIKE %value%
            in_filters: IN 匹配 {field: [values]} -> field IN (values)
            not_in_filters: NOT IN 匹配 {field: [values]} -> field NOT IN (values)
            range_filters: 范围查询 {field: {"gte": min_val, "lte": max_val}}
                           支持的操作符: gte(>=), gt(>), lte(<=), lt(<)
            null_filters: NULL 匹配 [field1, field2] -> field IS NULL
            not_null_filters: NOT NULL 匹配 [field1, field2] -> field IS NOT NULL
            order_by: 排序字段 ["-created_at", "name"]
            sa_filters: 自定义 SQLAlchemy 过滤表达式列表 [or_(Model.field == val, ...)]
        """
        # 精确匹配
        if filters:
            for field, value in filters.items():
                if hasattr(self.model, field):
                    stmt = stmt.where(getattr(self.model, field) == value)

        # 模糊匹配 (like %value%)
        if like_filters:
            for field, value in like_filters.items():
                if hasattr(self.model, field):
                    column = getattr(self.model, field)
                    stmt = stmt.where(column.ilike(f"%{value}%"))

        # NOT LIKE 匹配
        if not_like_filters:
            for field, value in not_like_filters.items():
                if hasattr(self.model, field):
                    column = getattr(self.model, field)
                    stmt = stmt.where(~column.ilike(f"%{value}%"))

        # IN 范围匹配
        if in_filters:
            for field, values in in_filters.items():
                if hasattr(self.model, field) and values:
                    column = getattr(self.model, field)
                    stmt = stmt.where(column.in_(values))

        # NOT IN 匹配
        if not_in_filters:
            for field, values in not_in_filters.items():
                if hasattr(self.model, field) and values:
                    column = getattr(self.model, field)
                    stmt = stmt.where(~column.in_(values))
        
        # 范围查询 (>=, >, <=, <)
        if range_filters:
            for field, conditions in range_filters.items():
                if hasattr(self.model, field) and conditions:
                    column = getattr(self.model, field)
                    if "gte" in conditions and conditions["gte"] is not None:
                        stmt = stmt.where(column >= conditions["gte"])
                    if "gt" in conditions and conditions["gt"] is not None:
                        stmt = stmt.where(column > conditions["gt"])
                    if "lte" in conditions and conditions["lte"] is not None:
                        stmt = stmt.where(column <= conditions["lte"])
                    if "lt" in conditions and conditions["lt"] is not None:
                        stmt = stmt.where(column < conditions["lt"])

        # NULL 匹配 (IS NULL)
        if null_filters:
            for field in null_filters:
                if hasattr(self.model, field):
                    column = getattr(self.model, field)
                    stmt = stmt.where(column.is_(None))

        # NOT NULL 匹配 (IS NOT NULL)
        if not_null_filters:
            for field in not_null_filters:
                if hasattr(self.model, field):
                    column = getattr(self.model, field)
                    stmt = stmt.where(column.isnot(None))

        # 排序
        if order_by:
            if isinstance(order_by, str):
                order_by = [order_by]

            for field_name in order_by:
                is_desc = field_name.startswith("-")
                name = field_name[1:] if is_desc else field_name

                if hasattr(self.model, name):
                    column = getattr(self.model, name)
                    stmt = stmt.order_by(desc(column) if is_desc else asc(column))

        # 自定义 SQL 表达式过滤
        if sa_filters:
            stmt = stmt.where(*sa_filters)

        return stmt

    # ============ 创建操作 ============

    async def create(self, data: dict[str, Any] | T, *, commit: bool = False) -> T:
        """创建单条记录"""
        instance = self.model(**data) if isinstance(data, dict) else data
        self.session.add(instance)
        await self._flush_or_commit(commit)
        await self.session.refresh(instance)
        logger.debug(f"Created {self.model.__name__}: id={getattr(instance, 'id', None)}")
        return instance

    async def bulk_create(self, items: Sequence[dict[str, Any] | T], *, commit: bool = False, ) -> Sequence[T]:
        """批量创建记录"""
        instances = [self.model(**item) if isinstance(item, dict) else item for item in items]
        self.session.add_all(instances)
        await self._flush_or_commit(commit)
        for instance in instances:
            await self.session.refresh(instance)
        logger.debug(f"Bulk created {len(instances)} {self.model.__name__} records")
        return instances

    # ============ 查询操作 ============

    async def get_by_id(self, id_: Any, **kwargs: Any) -> T | None:
        """
        根据 ID 获取记录

        Args:
            id_: 主键 ID
            **kwargs: 扩展参数
        """
        return await self.session.get(self.model, id_)

    async def get_or_raise(self, id_: Any, **kwargs: Any) -> T:
        """
        根据 ID 获取记录，不存在则抛出异常

        Raises:
            ResourceNotExistException: 记录不存在
        """
        instance = await self.get_by_id(id_, **kwargs)
        if not instance:
            raise ResourceNotExistException(f"{self.model.__name__} {id_} 不存在")
        return instance

    async def get_by_ids(self, ids: Sequence[Any], **kwargs: Any) -> Sequence[T]:
        """根据多个 ID 获取记录"""
        if not ids:
            return []
        return await self.get_list(in_filters={"id": list(ids)}, **kwargs)

    async def get_by_field(self, field: str, value: Any, *, first_only: bool = True, **kwargs: Any) -> T | None | Sequence[T]:
        """根据字段值查询"""
        if first_only:
            items = await self.get_list(filters={field: value}, limit=1, **kwargs)
            return items[0] if items else None
        return await self.get_list(filters={field: value}, **kwargs)

    async def get_list(
        self,
        *,
        filters: dict[str, Any] | None = None,
        like_filters: dict[str, Any] | None = None,
        in_filters: dict[str, list[Any]] | None = None,
        range_filters: dict[str, dict[str, Any]] | None = None,
        order_by: list[str] | str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
        sa_filters: Sequence[Any] | None = None,
        **kwargs: Any,
    ) -> Sequence[T]:
        """
        获取记录列表（支持高级筛选、排序、分页）

        Args:
            filters: 精确匹配 {field: value}
            like_filters: 模糊匹配 {field: value} -> field ILIKE %value%
            in_filters: 范围匹配 {field: [values]} -> field IN (values)
            range_filters: 范围查询 {field: {"gte": min_val, "lte": max_val}}
            order_by: 排序字段 ["-created_at", "name"]
            page: 页码 (1-based)
            page_size: 每页数量
            limit: 限制数量 (与 page/page_size 互斥，page 优先)
            offset: 偏移量
            **kwargs: 透传给 _build_stmt 的额外参数
        """
        stmt = select(self.model)
        stmt = self._build_stmt(
            stmt,
            filters=filters,
            like_filters=like_filters,
            in_filters=in_filters,
            range_filters=range_filters,
            order_by=order_by,
            sa_filters=sa_filters,
            **kwargs
        )

        # 分页逻辑
        if page is not None and page_size is not None:
            stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        elif limit is not None:
            stmt = stmt.limit(limit)
            if offset is not None:
                stmt = stmt.offset(offset)

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count(
        self,
        *,
        filters: dict[str, Any] | None = None,
        like_filters: dict[str, Any] | None = None,
        in_filters: dict[str, list[Any]] | None = None,
        range_filters: dict[str, dict[str, Any]] | None = None,
        sa_filters: Sequence[Any] | None = None,
        **kwargs: Any
    ) -> int:
        """
        统计记录数（支持高级筛选）
        """
        stmt = select(func.count()).select_from(self.model)

        # 兼容 kwargs 传参 (合并 filters)
        final_filters = filters or {}
        # 将 kwargs 中不属于特殊参数且值不为 None 的项视为精确过滤条件
        extra_filters = {k: v for k, v in kwargs.items()
                        if k not in ['like_filters', 'not_like_filters', 'in_filters', 'not_in_filters',
                                     'range_filters', 'filters', 'null_filters', 'not_null_filters', 
                                     'include_deleted', 'sa_filters']
                        and v is not None}
        final_filters.update(extra_filters)

        stmt = self._build_stmt(
            stmt,
            filters=final_filters,
            like_filters=like_filters,
            in_filters=in_filters,
            range_filters=range_filters,
            sa_filters=sa_filters,
            **kwargs
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def paginate(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        filters: dict[str, Any] | None = None,
        like_filters: dict[str, Any] | None = None,
        in_filters: dict[str, list[Any]] | None = None,
        range_filters: dict[str, dict[str, Any]] | None = None,
        order_by: list[str] | str | None = None,
        sa_filters: Sequence[Any] | None = None,
        **kwargs: Any
    ) -> dict[str, Any]:
        """
        分页查询

        Returns:
            {"items": [...], "total": 100, "page": 1, "page_size": 20, "pages": 5}
        """
        # 兼容旧接口 kwargs 传参
        final_filters = filters or {}
        extra_filters = {k: v for k, v in kwargs.items()
                        if k not in ['like_filters', 'not_like_filters', 'in_filters', 'not_in_filters',
                                     'range_filters', 'filters', 'null_filters', 'not_null_filters',
                                     'include_deleted', 'sa_filters']
                        and v is not None}
        final_filters.update(extra_filters)

        # 1. 统计
        total = await self.count(
            filters=final_filters,
            like_filters=like_filters,
            in_filters=in_filters,
            range_filters=range_filters,
            sa_filters=sa_filters,
            **kwargs
        )

        # 2. 查询数据
        items: list[T] = []
        pages = 0
        if total > 0:
            items = await self.get_list(
                filters=final_filters,
                like_filters=like_filters,
                in_filters=in_filters,
                range_filters=range_filters,
                order_by=order_by,
                page=page,
                page_size=page_size,
                sa_filters=sa_filters,
                **kwargs
            )
            pages = (total + page_size - 1) // page_size

        return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}

    # ============ 更新操作 ============

    async def update(self, id_: Any, data: dict[str, Any], *, partial: bool = True, commit: bool = False, ) -> T | None:
        """
        更新记录

        Args:
            id_: 主键值
            data: 更新数据
            partial: 是否部分更新（忽略 None 值）
            commit: 是否提交事务
        """
        instance = await self.get_by_id(id_)
        if not instance:
            return None
        for key, value in data.items():
            if partial and value is None:
                continue
            if hasattr(instance, key):
                setattr(instance, key, value)
        self.session.add(instance)
        await self._flush_or_commit(commit)
        await self.session.refresh(instance)
        logger.debug(f"Updated {self.model.__name__}: id={id_}")
        return instance

    async def update_with_version(self, id_: Any, data: dict[str, Any], lock_version: int, *, partial: bool = True,
            version_field: str = "lock_version", commit: bool = False, ) -> T:
        """
        带乐观锁的更新记录

        使用整数版本号实现乐观锁:
        - 检查当前版本是否匹配
        - 更新时自动递增版本号

        Raises:
            ResourceNotExistException: 记录不存在
            ConflictException: 版本号不匹配（并发冲突）
        """
        instance = await self.get_by_id(id_)
        if not instance:
            raise ResourceNotExistException(f"{self.model.__name__} {id_} 不存在")

        # 检查乐观锁版本
        if hasattr(instance, version_field):
            current_version = getattr(instance, version_field)
            if current_version != lock_version:
                raise ConflictException(f"{self.model.__name__} {id_} 版本冲突: 期望 {lock_version}, 当前 {current_version}")
            setattr(instance, version_field, current_version + 1)

        # 更新字段
        for key, value in data.items():
            if partial and value is None:
                continue
            if hasattr(instance, key) and key != version_field:
                setattr(instance, key, value)

        self.session.add(instance)
        await self._flush_or_commit(commit)
        await self.session.refresh(instance)
        logger.debug(f"Updated {self.model.__name__} with version: id={id_}")
        return instance

    async def bulk_update(self, updates: Sequence[tuple[Any, dict[str, Any]]], *, commit: bool = False, ) -> Sequence[
        T]:
        """批量更新记录，updates 格式: [(id, data), ...]"""
        results = []
        for id_, data in updates:
            instance = await self.update(id_, data)
            if instance:
                results.append(instance)
        if commit:
            await self.session.commit()
        logger.debug(f"Bulk updated {len(results)} {self.model.__name__} records")
        return results

    # ============ 删除操作 ============

    async def delete(self, id_: Any, *, commit: bool = False) -> bool:
        """删除记录"""
        instance = await self.get_by_id(id_)
        if not instance:
            return False
        await self.session.delete(instance)
        await self._flush_or_commit(commit)
        logger.debug(f"Deleted {self.model.__name__}: id={id_}")
        return True

    async def bulk_delete(self, ids: Sequence[Any], *, commit: bool = False) -> int:
        """批量删除记录"""
        deleted = 0
        for id_ in ids:
            instance = await self.get_by_id(id_)
            if instance:
                await self.session.delete(instance)
                deleted += 1
        if deleted > 0:
            await self._flush_or_commit(commit)
        logger.debug(f"Bulk deleted {deleted} {self.model.__name__} records")
        return deleted

    async def delete_all(self, *, commit: bool = False) -> int:
        """删除所有记录"""
        stmt = delete(self.model)
        result = await self.session.execute(stmt)
        deleted = result.rowcount  # type: ignore[attr-defined]
        await self._flush_or_commit(commit)
        logger.debug(f"Deleted all {deleted} {self.model.__name__} records")
        return deleted

    # ============ 统计操作 ============

    async def exists(self, id_: Any) -> bool:
        """检查记录是否存在"""
        stmt = select(func.count()).select_from(self.model).where(self.model.id == id_)  # type: ignore[attr-defined]
        result = await self.session.execute(stmt)
        return (result.scalar() or 0) > 0

    async def exists_by_field(self, field: str, value: Any) -> bool:
        """根据字段值检查记录是否存在"""
        return await self.count(filters={field: value}) > 0


# ============ 扩展服务 ============

class SoftDeleteCrudService(BaseCrudService[T]):
    """
    支持软删除的 CRUD 服务

    要求模型继承 SoftDeleteMixin，提供:
    - is_deleted: 是否已删除
    - deleted_at: 删除时间
    - deleted_by: 删除者

    特性:
    - 默认查询排除已删除记录
    - 提供软删除和恢复方法
    """

    async def soft_delete(self, id_: Any, deleted_by: str | None = None, *, commit: bool = False, ) -> T | None:
        """软删除记录"""
        return await self.update(id_, {"is_deleted": True, "deleted_at": datetime.now(), "deleted_by": deleted_by, },
            commit=commit, )

    async def restore(self, id_: Any, *, commit: bool = False) -> T | None:
        """恢复软删除的记录"""
        return await self.update(id_, {"is_deleted": False, "deleted_at": None, "deleted_by": None, }, commit=commit, )

    async def get_by_id(self, id_: Any, *, include_deleted: bool = False, **kwargs: Any) -> T | None:
        """
        根据 ID 获取记录（支持软删除过滤）

        Args:
            id_: 主键 ID
            include_deleted: 是否包含已删除记录
        """
        # 如果需要包含已删除记录，或者模型不支持软删除（没有 is_deleted 字段），直接使用 session.get
        if include_deleted or not hasattr(self.model, "is_deleted"):
            return await super().get_by_id(id_)

        # 否则，查询时过滤 is_deleted=False
        stmt = select(self.model).where(self.model.id == id_, self.model.is_deleted == False)  # noqa: E712, type: ignore[attr-defined]
        result = await self.session.execute(stmt)
        return result.scalars().first()
    def _build_stmt(
        self,
        stmt: Any,
        filters: dict[str, Any] | None = None,
        like_filters: dict[str, Any] | None = None,
        not_like_filters: dict[str, Any] | None = None,
        in_filters: dict[str, list[Any]] | None = None,
        not_in_filters: dict[str, list[Any]] | None = None,
        range_filters: dict[str, dict[str, Any]] | None = None,
        null_filters: list[str] | None = None,
        not_null_filters: list[str] | None = None,
        order_by: list[str] | str | None = None,
        include_deleted: bool = False,
        sa_filters: Sequence[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        # 先应用软删除过滤
        if not include_deleted:
            stmt = stmt.where(self.model.is_deleted == False)  # noqa: E712, type: ignore[attr-defined]

        # 再调用父类构建
        return super()._build_stmt(
            stmt,
            filters=filters,
            like_filters=like_filters,
            not_like_filters=not_like_filters,
            in_filters=in_filters,
            not_in_filters=not_in_filters,
            range_filters=range_filters,
            null_filters=null_filters,
            not_null_filters=not_null_filters,
            order_by=order_by,
            sa_filters=sa_filters,
            **kwargs
        )
