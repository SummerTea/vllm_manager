# backend/app/base/

## Responsibility
领域无关的样板基座（基础抽象层）：为 server 业务域提供数据模型、CRUD、枚举、请求/响应、配置的通用抽象，消除各业务域的重复样板。所有业务 Model/Service/Schema 都以本层为父类。

## Design
- **泛型仓储 `BaseCrudService[T]`**（base_crud.py）：`__init_subclass__` 从泛型参数自动推断 `self.model`；内置 create/bulk_create/get_by_id/get_or_raise/get_by_field/get_list/count/paginate/update/update_with_version/delete 等；`_build_stmt` 统一支持 filters（精确）/like_filters（ILIKE）/in_filters/range_filters（gte/gt/lte/lt）/null_filters/order_by（`-field` 降序）/sa_filters；子类 `SoftDeleteCrudService` 覆写查询默认排除 `is_deleted=True`。
- **事务约定**：Service 方法默认 `commit=False`（只 flush 不提交），由 API 层 `Depends(get_session)` 统一 commit/rollback；`update_with_version` 用 `VersionMixin.lock_version` 乐观锁，冲突抛 `VersionConflictException`。
- **模型组合**（base_model.py）：`IdMixin`（UUID7 主键，`uuid7().hex`）+ `AuditMixin`（Timestamp+UserTrack）+ `StatusMixin`/`SoftDeleteMixin`/`VersionMixin` 按需组合成 `BaseModel`/`VersionedModel`；`Base.__table_cls__` 统一列顺序（id→业务列→公共尾部），建表注释/索引走 `__table_args__`。
- **TypeDecorator 自定义列类型**：`UniversalJSON`（PG JSONB）、`UniversalText`（TEXT）、`UniversalValue`（Text 存储、读取时尝试 JSON 解析）。
- **枚举基类**（base_enum.py）：`BaseEnumMixin` 提供 from_value/from_name/names/values/to_options 等；`BaseStrEnum`/`BaseIntEnum` 与带中文 label 的 `LabeledStrEnum`/`LabeledIntEnum`/`DescribedStrEnum`（`(value, "中文标签")` 定义）。
- **响应契约**（base_response_schema.py）：`BaseResponse{code, message, data}` + `success/error` 工厂；`PageResponse`（PageData{items, meta:PageMeta}）、`ListResponse`（ListData{items, count}）、`EmptyResponse`/`IdResponse`/`CountResponse`/`BatchResponse`/`ErrorResponse`。
- **请求参数**（base_request_schema.py）：`PaginationParams`（page/page_size + offset/limit 属性）、`PageSortParams`、`IdsRequest`、`ListQueryParams` 组合；类型别名 `PageParams`/`ListParams` 供 FastAPI 依赖注入。
- **配置基座**（base_config.py）：`AppBaseConfig(BaseSettings)` 只做 `.env` 读取（`env_file=BASE_PATH/.env`，`frozen=True`，`extra="ignore"`），所有扩展 Config 子类与 `AppConfig` 均继承它；`BASE_PATH` 为工程根。

## Flow
- 模型定义：`class Node(IdMixin, BaseModel)` + `__tablename__ = f"{db_config.TABLE_NAME_PREFIX}_node"` → 注册进 `Base.metadata`，由 `init_db(create_tables=True)` 建表。
- 数据流：API 层 `Depends(get_session)` 注入 AsyncSession → `XxxService(session)`（继承 BaseCrudService，默认不提交）→ 返回 ORM 实例 → API 层转 Pydantic Schema 包裹 `BaseResponse` 返回 → `get_session` 退出时统一 commit（异常 rollback）。

## Integration
- 被 `server/`（node/instance/allocator）业务域全面消费；`extensions/database.py` 引用 `Base` 做 `create_all` 与 schema_diff_check。
- 异常依赖：`base_crud.py` 抛 `app.exception` 的 `ResourceNotExistException`/`VersionConflictException`（不是本层定义）。
- 配置：`db_config.TABLE_NAME_PREFIX`（表名前缀）在 `extensions/database.py` 定义，模型建表引用。

## 导航提示
- 文件→抽象对照：`base_model.py`（IdMixin/BaseModel/UniversalJSON/UniversalText/VersionMixin）、`base_crud.py`（BaseCrudService/SoftDeleteCrudService）、`base_enum.py`（BaseStrEnum/LabeledStrEnum 等）、`base_response_schema.py`（BaseResponse/PageResponse/ListResponse/EmptyResponse 等）、`base_request_schema.py`（PaginationParams）、`base_config.py`（AppBaseConfig/BASE_PATH）。
- 通用样板找本层，业务特化抽象（如 node/instance 的 enum）放各自域 `enum.py`。
