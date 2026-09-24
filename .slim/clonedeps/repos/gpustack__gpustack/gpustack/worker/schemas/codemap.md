# gpustack/worker/schemas/

## Responsibility

**Benchmark 结果数据模型层**：纯 pydantic 模式定义，用于序列化/反序列化压测 runner 产生的报告数据，是 runner 输出与服务器持久化之间的解耦契约。

## Design

- 单一文件 `benchmark_runner.py`，数据模型改编自 `vllm-project/guidellm` 的 benchmark schemas（文件头注释注明出处），无业务逻辑，全部为 `BaseModel`（pydantic）。
- 分层结构：基础统计模型 → 生成式指标聚合模型 → 完整报告模型：
  - `StatusBreakdown[SuccessfulT, ErroredT, IncompleteT, TotalT]`：泛型状态分组模型（successful / errored / incomplete / total 四个字段）。
  - `SchedulerMetrics`：调度器时间线，六个 Unix 时间戳（start / request_start / measure_start / measure_end / request_end / end）。
  - `Percentiles`：p50 / p90 / p95 / p99 分位数。
  - `DistributionSummary`：含 mean / variance / std_dev / min / max 与 PDF，支持从原始值或时序事件构造；`StatusDistributionSummary` 包装成功/失败分布。
  - `GenerativeMetrics`：生成式任务核心指标（吞吐、延迟分布、token 统计）；支撑模型 `RequestTimings`（TTFT/TPOT 等）、`RequestInfo`、`UsageMetrics`。
  - `GenerativeRequestStats`：按状态分组 + 分位数统计请求（`type_="generative_request_stats"`，含 request_id / request_type / request_args / info / input_metrics / output_metrics 等字段）。
  - `GenerativeBenchmark`：单次压测完整结果（scheduler_metrics / metrics / start_time / end_time / duration / requests_truncated 状态分组）。
  - `GenerativeBenchmarksReport`：顶层报告容器（`benchmarks` 列表 + `to_metrics()` / `load_file()` 方法）。
- 泛型与类型别名：`BaseModelT` / `RegisterClassT` / `SuccessfulT` 等 `TypeVar` 支持不同结果类型的复用；`GenerativeRequestType` 枚举 text/chat completions、audio transcriptions/translations。

## Flow

数据流单向：`BenchmarkManager` 用 `GenerativeBenchmarksReport.load_file()` 读取 runner 落盘的 `{benchmark_dir}/{id}.dual_json`（`load_file` 支持 JSON/YAML，按扩展名自动识别）→ 解析为结构化报告 → 调用 `report.to_metrics()` 转换为服务器侧 `BenchmarkMetrics` 并回传（含 `raw_metrics` 全量 JSON 与 requests_per_second / latency / token 速率等均值与并发峰值指标）。模型本身不产生数据，只做结构化承载、校验与转换。

## Integration

- 被 `benchmark_manager.py` 消费：第 475-476 行 `report = GenerativeBenchmarksReport.load_file(metrics_file_path)` → `metrics = report.to_metrics()`；另有 `_parse_report` 类方法（第 577 行起）从报告中提取指标。
- `__init__.py` 重新导出 `GenerativeBenchmark`、`GenerativeMetrics`、`SchedulerMetrics` 三个常用模型。
- 依赖 `gpustack.schemas.benchmark.BenchmarkMetrics`（`to_metrics` 的目标类型，`raw_metrics` 字段承载完整报告 JSON）。
- 与 `worker/benchmark/runner.py` 通过 `*.dual_json` 文件衔接，构成 runner 侧与 manager 侧的稳定数据契约。
