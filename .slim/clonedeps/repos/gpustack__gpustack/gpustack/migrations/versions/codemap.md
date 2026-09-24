# gpustack/migrations/versions/

## Responsibility
- Alembic 迁移版本库：按时间线排列的全部数据库 schema 变更脚本（32 个），串成一条从首次建表到 v2.2.2 的 revision 链。
- 每个文件为标准 `upgrade()`/`downgrade()` 结构（`script.py.mako` 模板生成），命名 `YYYY_MM_DD_HHMM-<revision>_<描述>.py`。

## Design
- 链首 `1dd9fa5b38ff initialize tables` 创建全部初始表（system_loads/workers/users/api_keys/model_files/models/model_instances/model_usages 等），此后逐版本增量演进。
- 演进阶段：
  - **早期（2024）**：`8277680cfcb7` embedding-only 模型、`6dcb3a50da19` 分布式调度（model/instance/system_load 改造）、`f29521415cbd` model scope source、`0f7dde8b3c4b` reranker、`004c73a5c09e` 本地路径、`e6bf9e067296` 模型分类。
  - **v0.6-0.7（2025 前段）**：`c45e397531d1` env 支持、`cbbc03c88985` worker uuid、`d19176de3b74` users source（SQLite→PG 数据迁移的截止 revision）。
  - **v2.0（2025-09）**：`924c9a0b4c13` v2.0 大迁移（v2 平台重构）、worker metrics port、users is_active、`eeacfbc6a2bf` 模型访问控制、`nrxtab43e8j8` image_name/run_command + inference_backends 表。
  - **v2.0 尾段**：worker ifname、AuthProviderEnum、server proxy 改进、speculative config、gateway followup、cpu_offloading 可空、backend 字段映射、worker maintenance、`e30134bd18dc` model instance port→ports。
  - **v2.1（2025 底-2026 初）**：`2aed534bd7b2` v2.0.2、`53667f33f000` v2.1.0 变更、`8ad0f94c92e8` legacy backend_source→CUSTOM、`8bf38a6bb3b5` v2.2.0（worker version 等）。
  - **v2.2（2026）**：`7c5e3f9a2d18` 多租户地基（users→principals 表，USER/ORG/GROUP/SYSTEM 单表判别 + 资源归属/ACL/成员关系）、`61929acb0676` GPU 实例、`b2c3d4e5f6a7` 计量计费（metered_usage/usage events）、`c4d7e8f9a0b1` v2.2.2 收尾（principals.source 枚举改 VARCHAR 等）。
- 部分迁移含跨库兼容逻辑（复用 `migrations/utils.py` 的 `is_opengauss` 检测），并有数据回填/迁移代码，非纯 DDL。

## Flow
- 由 `migrations/env.py` 按 `down_revision` 链依序执行；`gpustack migrate`（cmd/db_migration.py）可将某 revision 应用于独立目标库。

## Integration
- 上游：`migrations/` 环境 + Alembic；下游：数据库 schema 必须与 `schemas/` 当前模型一致，否则 server 启动报错——新增字段流程即「改 schemas → autogenerate 新版本 → 追加到本目录」。
