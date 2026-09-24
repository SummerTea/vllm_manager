# gpustack/

## Responsibility
- Python 包根（`gpustack/`）：GPUStack 单二进制发行版的程序入口与进程级基础设施。
- 承载 CLI 主入口（`main.py`）、版本常量（`__init__.py`）、日志初始化（`logging.py`）、插件扩展接口（`extension.py`）、认证与密钥工具（`security.py`）、worker/client 侧 SSL 工厂（`ssl_context.py`）。
- 同时是各功能子包的容器：`server/`（管理端 API 与控制面）、`worker/`（节点端）、`api/`（路由）、`schemas/`（ORM 模型 + Pydantic schema）、`cmd/`（CLI 子命令）、`config/`、`migrations/` 等。

## Design
- `__init__.py` 仅导出构建期常量：`__version__`、`__git_commit__`、`__benchmark_runner_version__`、`__operator_version__`（版本统一由 `extension.resolve_version_info()` 提供插件覆盖钩子）。
- `main.py` 用 argparse 组装子命令（`start`/`reload-config`/`download-tools`/`migrate`/`images`/`prerun`/`reset-admin-password`/`version`），每个命令经 `setup_*_cmd(subparsers)` 注册，`args.func(args)` 分派；`freeze_support()` 支持 spawn 多进程。
- `extension.py` 定义插件契约 `Plugin` 基类：`__init__(app, cfg)` 在构造时完成装配、`async_tasks()` 提供后台协程、`setup_start_cmd()`/`contribute_config()`/`extra_image_list()`/`get_version_info()` 为 CLI 期 classmethod 钩子，通过 `gpustack.plugins` entry-point 发现。
- `security.py`：argon2 密码哈希 + `JWTManager`（HS256）、API key 三段格式 `gpustack_{access_key}_{secret_key}` 解析（含 legacy UUID 兼容回退）。
- `ssl_context.py`：`make_ssl_context()` 返回进程级单例 SSLContext，将 CA 信任固定到 bundle 文件（`X509_LOOKUP_file`），避免每次握手重复目录扫描。

## Flow
- 进程启动：`python -m gpustack` → `main.main()` → argparse 解析 → 子命令 `run(args)`。
- `start` 命令（见 `cmd/start.py`）：`parse_args` 构建 `Config` → `initialize_gateway` → 按 `cfg.server_url` 分支为 worker 或 server（server 可 spawn 内嵌 worker 子进程）。
- 认证流：`security.py` 生成/校验 JWT 与 API key，被 `api/auth` 与 CLI 本地命令（`cmd/local_auth.py` mint_admin_jwt）复用。

## Integration
- 依赖方：`cmd/` 各子命令、`config/config.py`（`set_global_config`）、`server/`、`worker/`、`schemas/` 全部从本包导入基础设施。
- 被依赖方：`logging.py` 被 `cmd` 各入口调用；`extension.py` 被 `server/server.py`（create_app 时实例化插件）、`cmd/start.py`、`cmd/version.py` 消费。
- 与 server/worker 连接点：包根是唯一进程入口，server 与 worker 两个角色共享同一二进制，靠 `Config.server_role()` 判别（详见 `config/codemap.md`）。
