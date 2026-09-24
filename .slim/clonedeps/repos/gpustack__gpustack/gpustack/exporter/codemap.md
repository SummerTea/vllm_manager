# gpustack/exporter/

## Responsibility
Prometheus 指标导出器：以独立 uvicorn 端口暴露 GPUStack 控制面的聚合状态指标（cluster/worker/model/model_instance），并提供服务发现端点供 Prometheus 抓取各 worker 的自身指标。Server 端组件，按 `cfg.metrics_port` 启动。

## Design
- `exporter.py`：`MetricExporter(Collector)` —— 实现 `prometheus_client` 的 `Collector` 接口，`collect()` 只回放内存缓存的指标；后台任务 `generate_metrics_cache` 每 3 秒从 DB 重算缓存（`_collect_metrics`），DB 瞬时错误不逃逸、保留旧缓存。
- `_collect_metrics` 按层级构建 `InfoMetricFamily`/`GaugeMetricFamily`：
  - cluster：`cluster_info`（含 provider）、`cluster_status`（state 标签值为 1）；
  - worker：`worker_info`（动态附加 label，用 `label_name_pattern` 正则过滤非法 label 名）、`worker_status`；
  - model：`model_info`（runtime/source）、`model_desired_instances`（replicas）、`model_running_instances`（ready_replicas）；
  - model_instance：`model_instance_status`、`model_instance_restart_count`、`model_instance_latest_restart_time`。
  - 查询用 `selectinload` 预加载 cluster→workers→models→instances，指标名经 `utils.name.metric_name` 规范化。
- 路由：`GET /metrics`（`generate_latest(REGISTRY)`）；`/metrics/targets` 与 `/metrics/proxy-targets` 返回 Prometheus `static_configs` 风格的 worker 抓取目标（`{labels, targets}`）——按 worker 的 `proxy_mode == TUNNEL` 区分直连与走代理的地址（`advertise_address` vs `ip`），仅含 READY 且有 metrics_port 的 worker。
- `bus_metrics.py`：`BusMetricsCollector` 暴露进程内事件总线健康度——`bus_subscribers`、`bus_queue_depth`/`capacity`/`full`/`saturation_ratio`、`bus_subscriber_latest_keys`、`bus_events`（counter）。按 `(topic, source)` 聚合（多个 `source="streaming"` 订阅共享标签集会重复，故取 max/求和），计数叠加 `retired_event_counts` 保证单调。

## Flow
`server/server.py._start_metrics_exporter` → `MetricExporter.start()`：注册自身与 BusMetricsCollector 到全局 REGISTRY → 起 uvicorn；`generate_metrics_cache` 周期任务与 HTTP 服务并行。Prometheus 每轮抓取 `/metrics`（走缓存，DB 压力小）与 `/metrics/targets`（服务发现 worker 端点）。

## Integration
- 消费方：`server/server.py`（启动方）、Prometheus 抓取端。
- 依赖：`prometheus_client`、`uvicorn`/`fastapi`、`gpustack.server.bus.event_bus`、`gpustack.server.db.async_session`、schemas（Cluster/Worker/Model）；worker 自身的 exporter 在 `worker/exporter.py`（另一实现，消费 WorkerStatusCollector 与 RuntimeMetricsAggregator）。
