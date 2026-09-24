# gpustack/policies/worker_filters/

## Responsibility
Worker 预过滤层（Filter Chain），在候选生成之前把全量 workers 缩减为与模型匹配的集合。每个过滤器实现 `WorkerFilter.filter(workers) -> (workers, messages)`，由 `WorkerFilterChain`（policies/base.py）顺序执行、短路聚合。对应 vllm_manager 的"节点可用性/匹配"概念。

## Design
六个过滤器按顺序在 `find_candidate()`（scheduler/scheduler.py）中组装：
1. **`ClusterFilter`**（cluster_filter.py）：按 `model.cluster_id` 过滤，只留同集群 worker。
2. **`GPUMatchingFilter`**（gpu_matching_filter.py）：手动 GPU 选择（`model.gpu_selector.gpu_ids`）时，用 `group_gpu_ids_by_worker` 反解出被选 GPU 所属的 worker 名，只保留这些 worker；未手动选 GPU 则直接放行。
3. **`LabelMatchingFilter`**（label_matching_filter.py）：`model.worker_selector`（标签字典）全量匹配 worker.labels（`label_matching()`），vLLM/Ascend 后端附加"仅支持 Linux"提示。
4. **`StatusFilter`**（status_filter.py）：只保留 `WorkerStateEnum.READY` 的 worker。
5. **`BackendFrameworkFilter`**（backend_framework_filter.py，逻辑最重）：按模型 backend 与 worker GPU 的框架/版本兼容性过滤。从 worker GPU 提取 `(gpu_type, runtime_version, backend_version, variant)` 查询条件（Ascend 附 CANN variant）；`_visible_version_configs()` 合并平台级与模型属主级的 `InferenceBackend.version_configs`（Hybrid 可见性）；`_has_supported_runners()` 检查内置框架/custom_framework 支持或 `list_service_runners()` 是否存在可用 runner。模型指定 `backend_version` 时走版本精确匹配，否则自动匹配任一可用版本。CUSTOM backend 跳过。
6. **`LocalPathFilter`**（local_path_filter.py）：仅 LOCAL_PATH 模型且手动选了 GPU 时生效，经 `WorkerFilesystemClient.path_exists` 逐 worker 校验模型路径存在性（异常视作不存在）。

## Flow
`find_candidate()` 构建 `WorkerFilterChain(filters)` → 依序调用各 `filter()`：前一个的输出作为下一个的输入；某一步过滤后 workers 为空则中断；所有过滤消息聚合后经 `ListMessageBuilder` 追加到调度消息，作为无候选时的 `state_message` 明细。

## Integration
- 唯一编排点：**`gpustack/scheduler/scheduler.py::find_candidate`**（scheduler 与 evaluator 共用）。
- 被 **`gpustack/scheduler/calculator.py`** 复用：`_calculate_from_workers`/`read_local_path_file_from_workers` 在向 worker 广播解析请求前同样套用 GPUMatchingFilter/LabelMatchingFilter/LocalPathFilter 缩小广播范围。
- 依赖 **`gpustack/policies/base.py`**（WorkerFilter 抽象）、**`gpustack/schemas/`**（Model/Worker/InferenceBackend）、**`gpustack_runtime.detector`**（Ascend CANN variant）、**`gpustack_runner`**（list_service_runners 查询可用 runner）、**`gpustack/client/worker_filesystem_client.py`**（路径校验）。
