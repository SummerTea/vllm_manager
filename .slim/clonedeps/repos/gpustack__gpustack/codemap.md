# Repository Atlas: gpustack (v2.2.3)

## Project Responsibility

GPUStack 是 GPU 集群管理平台：调度 vLLM/SGLang 等推理引擎实例到多 GPU 节点、按需拉起/停止模型服务、采集 GPU 状态与推理指标、可选提供 SSH 可访问的 GPU 实例。单 Python 包 + 单二进制发行，靠 `Config.server_role()` 在同一代码库内以 server / worker / both 三种角色运行。

被 vllm_manager 用作参考实现（重点：worker 端 vLLM 实例启停与 GPU 监控 → 对标我们 agent；server 端状态协调 → 对标我们 manager）。

## System Entry Points

- `gpustack/cmd/start.py` — CLI 启动入口：加载配置、初始化日志与 gateway，按 `server_url` 分派到 server 或 worker（`multiprocessing.Process` 内嵌 worker 实现单进程多角色）。
- `gpustack/config/config.py` — 全局 `Config` 单例（Pydantic BaseSettings），server/worker 共享，按角色派生端口/模式等行为。
- `gpustack/schemas/` — SQLModel（ORM + Pydantic 合一）数据契约层，routes ↔ server 的模型定义。
- `gpustack/server/server.py` — server 启动编排（迁移、init_db、create_app、拉起 controllers/scheduler）。

## Reading Order (for vllm_manager reference)

1. **`gpustack/worker/`** — 实例生命周期管理（serve_manager）、状态采集上报、指标导出（对标 agent，最高价值）
2. **`gpustack/worker/backends/`** — Strategy 模式推理后端：vLLM 命令组装/参数注入/分布式拓扑
3. **`gpustack/server/`** — 声明式状态协调（controllers）、心跳落库、可达性检查与实例清理（对标 manager）
4. **`gpustack/routes/` + `gpustack/schemas/` + `gpustack/api/`** — REST 契约设计与鉴权（注册下发 token、心跳/状态分离、watch/SSE）
5. **`gpustack/utils/` + `gpustack/detectors/`** — 进程树终止（psutil 递归，非 killpg）、GPU 探测优雅降级（返回空列表而非异常）
6. 其余（scheduler/policies、gateway、k8s、cloud_providers、gpu_instances 等）— 按需参考

## Directory Map (Aggregated)

| Directory | Responsibility Summary | Detailed Map |
|-----------|------------------------|--------------|
| `gpustack/` | 包根：CLI 主入口、版本、日志、插件接口、SSL 工厂 | [View](gpustack/codemap.md) |
| `gpustack/api/` | API 横切层：统一认证/多租户上下文/异常体系/中间件 | [View](gpustack/api/codemap.md) |
| `gpustack/api/types/` | OpenAI 兼容响应类型扩展（embedding 双格式、nullable finish_reason） | [View](gpustack/api/types/codemap.md) |
| `gpustack/cloud_providers/` | 云厂商 GPU 虚拟机生命周期（DigitalOcean 等，cloud-init 渲染） | [View](gpustack/cloud_providers/codemap.md) |
| `gpustack/cmd/` | CLI 子命令实现：start 启动编排与 server/worker 分派 | [View](gpustack/cmd/codemap.md) |
| `gpustack/config/` | 全局 Config 单例 + ServerRole 角色推导 + 注册 token 持久化 | [View](gpustack/config/codemap.md) |
| `gpustack/detectors/` | 硬件检测抽象层：GPU/系统信息探测接口 + 工厂（无 GPU 返回空列表降级） | [View](gpustack/detectors/codemap.md) |
| `gpustack/detectors/custom/` | 静态数据探测器（配置注入硬件信息，跳过真实探测） | [View](gpustack/detectors/custom/codemap.md) |
| `gpustack/detectors/fastfetch/` | fastfetch 二进制采集系统信息（外部命令缺失时 is_available 降级） | [View](gpustack/detectors/fastfetch/codemap.md) |
| `gpustack/detectors/runtime/` | 默认 GPU 探测器：gpustack_runtime Rust 包 detect_devices 薄适配 | [View](gpustack/detectors/runtime/codemap.md) |
| `gpustack/envs/` | GPUSTACK_* 环境变量常量仓库（第二配置通道） | [View](gpustack/envs/codemap.md) |
| `gpustack/exporter/` | Prometheus 指标导出器（server 端，3s 缓存 Collector + targets 发现） | [View](gpustack/exporter/codemap.md) |
| `gpustack/gateway/` | Higress Ingress 集成：AI 推理路由映射 + Wasm 插件链装配 | [View](gpustack/gateway/codemap.md) |
| `gpustack/gateway/client/` | Higress/Istio CRD 轻量异步客户端（McpBridge/WasmPlugin/EnvoyFilter） | [View](gpustack/gateway/client/codemap.md) |
| `gpustack/gpu_instances/` | GPU 实例生命周期控制（K8s CRD 投射 + 对账） | [View](gpustack/gpu_instances/codemap.md) |
| `gpustack/http_proxy/` | 推理网关负载均衡（LoadBalancer + RoundRobin 策略） | [View](gpustack/http_proxy/codemap.md) |
| `gpustack/k8s/` | K8s 集群接入 manifest 渲染（DaemonSet/RBAC/operator） | [View](gpustack/k8s/codemap.md) |
| `gpustack/migrations/` | Alembic 迁移环境（SQLModel 元数据 → SQL 迁移执行） | [View](gpustack/migrations/codemap.md) |
| `gpustack/migrations/versions/` | 32 个迁移版本：初始建表 → v0.6/0.7 → v2.0/2.1/2.2 | [View](gpustack/migrations/versions/codemap.md) |
| `gpustack/mixins/` | 通用 ORM mixin：ActiveRecord 全套 CRUD + 事件发布 + 时间戳 | [View](gpustack/mixins/codemap.md) |
| `gpustack/policies/` | 调度策略抽象层（Chain of Responsibility + Strategy 基类） | [View](gpustack/policies/codemap.md) |
| `gpustack/policies/candidate_selectors/` | 候选节点选择（Resource Fit：worker+GPU+资源声明） | [View](gpustack/policies/candidate_selectors/codemap.md) |
| `gpustack/policies/event_recorder/` | 调度事件记录器（失败原因可解释性基础设施） | [View](gpustack/policies/event_recorder/codemap.md) |
| `gpustack/policies/scorers/` | 候选/实例评分层（scale up/down 双评分链 + 放置策略） | [View](gpustack/policies/scorers/codemap.md) |
| `gpustack/policies/worker_filters/` | Worker 预过滤链（Cluster/GPU/Label/Status/Backend/LocalPath 六过滤器） | [View](gpustack/policies/worker_filters/codemap.md) |
| `gpustack/routes/` | REST Controller 层：资源路由 + 认证分层装配 + watch SSE 推送 | [View](gpustack/routes/codemap.md) |
| `gpustack/routes/worker/` | worker 侧端点集（日志流/推理反代/文件系统探查，Bearer 保护） | [View](gpustack/routes/worker/codemap.md) |
| `gpustack/scheduler/` | ModelInstance 调度核心（队列、资源计算、模型识别、兼容预检） | [View](gpustack/scheduler/codemap.md) |
| `gpustack/schemas/` | SQLModel 数据契约层（每资源 Base/table/Create/Public 四件套） | [View](gpustack/schemas/codemap.md) |
| `gpustack/server/` | 服务端核心：启动编排 + controllers 声明式状态协调 + worker 管理 | [View](gpustack/server/codemap.md) |
| `gpustack/server/coordinator/` | 多实例协调抽象（leader 选举 + 跨实例 pub/sub） | [View](gpustack/server/coordinator/codemap.md) |
| `gpustack/utils/` | 通用工具库（psutil 进程树终止、GPU 标识解析、CLI 参数解析） | [View](gpustack/utils/codemap.md) |
| `gpustack/websocket_proxy/` | WebSocket CONNECT 隧道代理（server↔agent 长连接二进制协议） | [View](gpustack/websocket_proxy/codemap.md) |
| `gpustack/worker/` | 管控节点核心：实例生命周期管理 + 状态采集上报 + 指标导出 | [View](gpustack/worker/codemap.md) |
| `gpustack/worker/backends/` | 推理后端 Strategy 实现：VLLM/SGLang/MindIE/VoxBox/Custom | [View](gpustack/worker/backends/codemap.md) |
| `gpustack/worker/benchmark/` | 压测 Runner（子进程容器化基准测试 + 进度回写） | [View](gpustack/worker/benchmark/codemap.md) |
| `gpustack/worker/schemas/` | Benchmark 报告数据模型（guidellm 改编 pydantic 分层） | [View](gpustack/worker/schemas/codemap.md) |
