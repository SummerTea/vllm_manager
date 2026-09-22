"""
样板工程 BaseCrudService 测试

使用 sqlite 内存库验证通用 CRUD 逻辑（不依赖真实 PostgreSQL）。
模型定义与 session fixture 见 conftest.py。
"""

import pytest
from conftest import UserService

from app.exception import ResourceNotExistException


async def test_create_and_get(session):
    svc = UserService(session)
    user = await svc.create({"username": "alice"}, commit=True)
    assert user.id
    assert user.username == "alice"

    got = await svc.get_by_id(user.id)
    assert got is not None
    assert got.username == "alice"


async def test_get_or_raise_missing(session):
    svc = UserService(session)
    with pytest.raises(ResourceNotExistException):
        await svc.get_or_raise("no-such-id")


async def test_list_filters(session):
    svc = UserService(session)
    for name in ["alice", "alex", "bob"]:
        await svc.create({"username": name})
    await session.commit()

    exact = await svc.get_list(filters={"username": "alice"})
    assert len(exact) == 1

    like = await svc.get_list(like_filters={"username": "al"})
    assert len(like) == 2

    ordered = await svc.get_list(order_by=["-username"])
    # 字节序倒序: bob > alice > alex（alice 与 alex 第三字符 i > e）
    assert [u.username for u in ordered] == ["bob", "alice", "alex"]


async def test_paginate_and_count(session):
    svc = UserService(session)
    for i in range(25):
        await svc.create({"username": f"user{i}"})
    await session.commit()

    page = await svc.paginate(page=2, page_size=10, order_by=["username"])
    assert page["total"] == 25
    assert page["pages"] == 3
    assert len(page["items"]) == 10

    assert await svc.count() == 25


async def test_update(session):
    svc = UserService(session)
    user = await svc.create({"username": "alice"}, commit=True)

    updated = await svc.update(user.id, {"username": "alice2"}, commit=True)
    assert updated is not None
    assert updated.username == "alice2"

    # partial 更新时 None 值被忽略
    await svc.update(user.id, {"username": None})
    got = await svc.get_by_id(user.id)
    assert got is not None
    assert got.username == "alice2"


async def test_delete_and_exists(session):
    svc = UserService(session)
    user = await svc.create({"username": "alice"}, commit=True)

    assert await svc.exists(user.id) is True
    assert await svc.delete(user.id, commit=True) is True
    assert await svc.exists(user.id) is False
    assert await svc.delete(user.id) is False
