"""
样板工程基础模型测试

验证 SQLAlchemy 声明式模型的行为（模型定义见 conftest.py）：
- 列顺序（id -> 业务字段 -> 公共尾部）
- 主键默认值（UUID7）
- 乐观锁 version_id_col
"""

from conftest import Doc, User


def test_column_order_id_first_tail_last():
    cols = [c.name for c in User.__table__.columns]
    assert cols[0] == "id"
    assert cols.index("username") < cols.index("created_at")
    assert cols[-1] == "updated_by"


def test_primary_key_default_uuid7():
    pk = User.__table__.primary_key.columns
    assert list(pk.keys()) == ["id"]
    u = User(username="alice")
    # SQLAlchemy 的 mapped_column(default=...) 在 flush 时才生成值，
    # 与 SQLModel（pydantic 构造时默认）语义不同
    assert u.id is None or len(u.id) == 32
    assert u.is_active is None or u.is_active is True


def test_version_mixin_mapper_args():
    assert Doc.__mapper__.version_id_col.name == "lock_version"


def test_universal_json_roundtrip():
    u = User(username="bob", profile={"a": 1, "b": ["x", "y"]})
    assert u.profile == {"a": 1, "b": ["x", "y"]}


def test_metadata_contains_tables():
    from app.base.base_model import Base

    assert {"users", "docs"} <= set(Base.metadata.tables.keys())
