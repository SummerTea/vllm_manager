# gpustack/config/

## Responsibility
- 全局配置子系统：定义进程级 `Config` 单例（加载/校验/派生默认值）与节点注册 token 的持久化读写。
- 是 server 与 worker 共享的配置中枢——同一份 `Config` 按角色派生不同行为。

## Design
- `config.py` 核心为 `Config` 类：继承 `WorkerConfig`（`PredefinedConfig`，含 data_dir/advertise_address/token/server_url）与 Pydantic `BaseSettings`（`env_prefix="GPUSTACK_"`，`extra="allow"` 支持插件自定义字段）。全部启动选项（端口、数据库、TLS、OIDC/SAML/CAS、CORS、gateway、内嵌 worker 等）都是其字段，带默认值。
- 构造时 `__init__` 做派生：目录绝对化（data_dir/cache_dir/bin_dir/log_dir）、token 兜底（`read_registration_token`）、生成 `server_id`、`prepare_jwt_secret_key`（自动生成并落盘 `<data_dir>/jwt_secret_key`）、`init_auth`（按 oidc/saml/cas 设置 `external_auth_type`）、`detect_gateway_mode`（auto→embedded/incluster/external/disabled 决策）。
- **角色判别（server/worker 单进程多角色）**：`Config.ServerRole` 枚举（SERVER/WORKER/BOTH）；`server_role()` = worker（有 `server_url`）→ WORKER，`_is_both_role()`（`--enable-worker`）→ BOTH，否则 SERVER；`_is_both_role()` 对未显式设置的老安装做向后兼容：data_dir 为空（新装）默认 server-only，否则看 `bootstrap_version` 文件存在性（存在=v2.0.1+ 默认 server-only，缺失=legacy v2.0.0 默认 BOTH）。
- 辅助 getter 族按角色返回不同端口：`get_api_port()`/`get_gateway_port()`（worker 用 worker_port，server 用 api_port/port）、`get_server_url()`、`get_proxy_url()`、`get_trusted_hosts()`、`get_system_reserved()`。
- 运行时热更新：`reload_worker_config()` 用 server 下发的 `PredefinedConfigNoDefaults` 覆盖白名单字段；`reload_token()` 重读注册 token。
- `registration.py`：`read/write_token`（token 与 worker_token 文件）、`registration_client()`（构造 `WorkerRegistrationClient`，legacy UUID 兜底拼 `gpustack_{uuid}_{token}`）、`determine_default_registry()`（探测 docker.io/quay.io 可达性，TTL 缓存 1h）。

## Flow
- 启动时 `cmd/start.py:parse_args` → `Config(**config_data)`（此时完成派生与校验）→ `set_global_config` 存入模块级 `_config`（`get_global_config()` 读取）。
- 角色判定链：`server_role()` → 控制 prerun 服务启用、端口计算、gateway 模式；`server.py` 与 `worker.py` 直接消费同一 `cfg`。

## Integration
- 依赖：`gpustack.schemas.*`（SystemInfo/GPUDeviceStatus 等）、`gpustack_runtime.detector`（GPU 厂商→backend 映射）、`gpustack.security`、`gpustack.ssl_context`、`config/registration.py`。
- 被依赖：几乎全系统——`cmd/start.py`、`cmd/prerun.py`、`server/`、`worker/`、`api/`、`extension.py`（插件 `__init__(app, cfg)`）都读取 `Config`。
