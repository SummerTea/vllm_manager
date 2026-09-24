# gpustack/routes/worker/

## Responsibility
Worker 侧（管控节点）的 HTTP API 端点集，由 GPUStack server 通过 HTTP 调用
（server 是唯一合法调用方）。

职责涵盖：serve 日志流式读取、到本地推理服务的反向代理、
模型文件系统探查（config 解析 / GGUF 解析 / 权重体积估算）、以及 K8s API 代理。

对应本项目 agent 端的 REST 面。

## Design
4 个模块，各含一个 `APIRouter`，均在 `gpustack/worker/worker.py` 的 `_serve_apis()` 中挂载：

- `logs.py`：
  - `GET /serveLogs/{id}`（`StreamingResponse`，`application/octet-stream`）、
    `GET /serveLogOptions/{id}`、`GET /benchmark_logs/{id}`。
  - 日志文件命名规约 `{id}.{restart_count}.log` / `{id}.container.{name}.{restart_count}.log`，
    支持 `tail`/`follow`/`previous` 参数。
  - 用 `LogSourceChain` 把下载日志、主日志、容器日志三源合并流式输出。
- `proxy.py`：
  - `/proxy/{path:path}` 通用反向代理到本机推理端口
    （`http://{worker_ip}:{target_port}/{path}`），是推理流量的数据面。
  - `set_port_from_model_name` 中间件根据请求头 `x-gpustack-model-instance`
    （`router_header_key`）解析目标端口与实例 id，写入
    `request.state.x_target_port` / `x_target_instance_id`。
  - 成功响应调用 `record_successful_inference` 供健康检查使用。
- `filesystem.py`：
  - `GET /files/model-config`、`GET /files/file-exists`、
    `GET /files/model-weight-size`、`POST /files/parse-gguf`。
  - 全部经 `validate_path_security`（`os.path.realpath` + base 目录校验，防目录穿越）；
    model-config 仅允许白名单配置文件（`config.json` 等 8 个）。
- `cluster_proxy.py`：
  - `/cluster-proxy/{path:path}`，用 pod 内 ServiceAccount 凭证转发到 K8s API server，
    供 server 端管理集群资源。

**认证**：`logs` 挂载时注入 `worker_request_auth`；`proxy`/`filesystem`/`cluster_proxy`
自带 `worker_auth` 依赖（Bearer token 等于 worker token / server 注册 token，
或经 server `/token-auth` 校验 `X-Higress-Llm-Model`）。

代理响应头过滤：剔除 hop-by-hop 头（`connection`/`transfer-encoding`/`content-encoding` 等），
避免 ASGI 重复注入导致客户端拒绝。

## Flow
server（如 `routes/model_instances.py` 的日志端点、`openai.py` 的推理端点）
→ `request_to_worker`/`stream_to_worker` 发起 HTTP
→ worker 路由校验 `worker_auth` → 执行本地操作
（读日志文件 / 转发到推理端口 / 子进程跑 gguf-parser / 转发 K8s API）
→ 流式或普通响应返回 server，server 原样透传给上层客户端。

## Integration
- 依赖方：`gpustack/worker/logs.py`（`LogOptions`、`log_generator`）、
  `gpustack/worker/log_sources.py`（`LogSourceChain` 等）、
  `gpustack/api/auth.py`（`worker_auth`）、`gpustack/api/exceptions.py`、
  `gpustack/schemas/models.py`（`ServeLogOptionsResponse`、`Model`）、
  `gpustack/schemas/filesystem.py`、`gpustack/scheduler/calculator.py`（GGUF 解析命令）。
- 被依赖方：`gpustack/worker/worker.py`（`_serve_apis()` 挂载全部 router）；
  server 端消费者为 `routes/model_instances.py`、`routes/benchmarks.py`、
  `scheduler/evaluator.py`（`WorkerFilesystemClient`）。
- 关键约定：所有端点只被 server 信任调用（Bearer token 认证），
  不直接面向浏览器/外部客户端。
