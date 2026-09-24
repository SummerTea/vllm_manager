# gpustack/cmd/

## Responsibility
- CLI 子命令实现层：`gpustack` 可执行文件的全部命令均在此定义（`main.py` 只负责 argparse 装配与分派）。
- 核心职责是「启动」——`start.py` 负责构建 `Config`、初始化日志与 gateway，并按角色分派到 server 或 worker。

## Design
- 每个模块暴露 `setup_<cmd>_cmd(subparsers)` 注册 argparse 子命令，并设置 `parser.set_defaults(func=run)`；命令实现挂在模块级 `run(args)`。
- `start.py`：唯一入口式设计。`start_cmd_options()` 声明 Common/Server/Worker 三组参数（默认值来自 `GPUSTACK_*` 环境变量）；`parse_args()` 是统一配置加载管线：YAML 配置文件（`load_config_from_yaml`）→ `set_common/server/worker_options` 白名单搬运 CLI 参数（CLI 优先于 YAML）→ `_contribute_plugin_config` 插件钩子 → `Config(**config_data)` → `set_global_config`。
- **单进程多角色（无独立 `--role` 参数，以配置推导）**：`run()` 中 `if cfg.server_url: run_worker(cfg) else: run_server(cfg)`——设置 `--server-url` 即 worker 模式；否则为 server 模式；server 再经 `--enable-worker`/`--disable-worker` 决定是否内嵌 worker（`Config.ServerRole` BOTH/SERVER，见 `config/codemap.md`）。`run_server()` 通过 `multiprocessing.Process(target=run_worker)` spawn 子进程实现内嵌 worker。
- `prerun.py`：容器（s6-overlay）启动前检查——按角色计算需启用的 s6 服务（postgres/gateway/observability）、端口可用性检查、将 service/ray 端口段从内核 ephemeral 区间保留、写入 postgres/gateway/prometheus/grafana 环境配置。
- `db_migration.py`：sqlite→PostgreSQL/MySQL 一次性数据迁移（`migrate` 命令）：复制 sqlite 文件→双方 `alembic upgrade` 到 `d19176de3b74`→按 `TARGET_TABLES` 逐表搬数据→同步序列。
- `reload_config.py`：向本机 server/worker 的 `/v2/config` 端点 PUT 白名单字段（`WHITELIST_CONFIG_FIELDS`），实现不重启热更新；本机凭据解析（admin JWT 或 worker token）在 `local_auth.py` / `config/registration.py`。
- 其余工具命令：`version.py`（版本打印）、`images.py`（list/save/copy/load 容器镜像，聚合插件镜像）、`download_tools.py`（worker 依赖工具包）、`reset_admin_password.py`。

## Flow
- CLI 入口 → `main.main()` → 子命令 `run(args)` →（start 为例）`parse_args` → `setup_logging` → `initialize_gateway` → `run_worker` 或 `run_server`（内嵌 worker 走 spawn 子进程）。
- prerun 流程：`parse_args` → 计算 enabled/dependency s6 服务 → `prepare_s6_overlay` → 端口检查/保留 → 生成各内置服务 env 文件。

## Integration
- 依赖：`gpustack.config.Config`（`config/`）、`gpustack.worker.worker.Worker` 与 `gpustack.server.server.Server`（角色实现）、`gpustack.gateway.initialize_gateway`、`gpustack.extension.Plugin`（CLI 期插件钩子）。
- 被依赖：`main.py` 导入全部 `setup_*_cmd`；`prerun.py` 与 `reload_config.py` 复用 `start.py` 的 `start_cmd_options`/`parse_args`；`local_auth.py` 被 reload-config 与 reset-admin-password 共享。
