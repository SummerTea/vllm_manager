"""
样板工程共享测试夹具与模型定义。

模型定义在 conftest.py 中，避免多个测试文件重复注册同名表到
Base.metadata（SQLAlchemy 表名在同一个 metadata 内必须唯一）。
"""

import pytest
from sqlalchemy import String, Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.base.base_crud import BaseCrudService
from app.base.base_model import (
    Base,
    BaseModel,
    IdMixin,
    StatusMixin,
    UniversalJSON,
    VersionMixin,
)


class User(IdMixin, StatusMixin, BaseModel):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(100))
    profile: Mapped[dict] = mapped_column(UniversalJSON)


class Doc(IdMixin, VersionMixin, BaseModel):
    __tablename__ = "docs"
    title: Mapped[str] = mapped_column(Text)


class UserService(BaseCrudService[User]):
    pass


@pytest.fixture
async def session():
    """sqlite 内存库会话（验证 ORM/CRUD 逻辑，不依赖真实 PostgreSQL）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
