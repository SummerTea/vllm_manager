# gpustack/policies/

## Responsibility
调度策略层（Policy Layer），以 Chain of Responsibility + Strategy 模式定义调度所需的抽象基类与共享工具。上游只依赖本层的抽象（`WorkerFilter`、`ScheduleCandidatesSelector`、`ScheduleCandidatesScorer`），具体实现分散在 candidate_selectors / worker_filters / scorers / event_recorder 子包。

## Design
- **`base.py`** 定义全部抽象与数据结构：
  - `WorkerFilter`（ABC，`filter(workers) -> (workers, messages)`）+ `WorkerFilterChain`：顺序执行过滤器链，任一环节过滤光 workers 即短路，聚合所有过滤消息。
  - `ModelInstanceScorer`（对 ModelInstance 打分）与 `ScheduleCandidatesScorer`（对候选打分）两个独立评分抽象；`max_score` 属性约定用于后续归一化。
  - 数据载体：`ModelInstanceScheduleCandidate`（worker、gpu_indexes、computed_resource_claim、score、overcommit、subordinate_workers——支撑多机分布式调度）、`ModelInstanceScore`、`Allocatable`/`Allocated`（ram + {gpu_index: vram}）。
- **`utils.py`**：资源可分配性计算的单一事实源，被 selector、scorer、REST API、worker 侧 collector 共用：
  - `compute_worker_allocated()` / `get_worker_allocatable_resource()`：从当前 ModelInstance 绑定关系推导已分配资源（K8s scheduler 式），主 worker 计 ram+vram，分布式 subordinate 只计 vram；扣减 `system_reserved`，处理 UMA 统一内存。
  - 启发式 VRAM 估算：`estimate_model_vram()`（权重大小 ×1.2 激活开销 + 框架开销 + LoRA；IMAGE 类直取权重），支持 `GPUSTACK_MODEL_VRAM_CLAIM` 环境变量覆盖。
  - 分组排序工具：`group_workers_by_gpu_type`、`group_worker_gpu_by_memory`（按可分配显存 ≥0.9 倍聚类）、`sort_gpu_indexes_by_allocatable_rate`、`sort_selected_workers_by_gpu_type_and_resource`。
  - `ListMessageBuilder`：把过滤/调度消息格式化为 `- line` 列表，供 state_message 展示。
  - RAM 相关：`get_model_ram_claim` / `get_computed_ram_claim`（extended KV cache 静态 ram_size 或 ram_ratio 推导）。
- 具体策略不在本层，由子包实现后经 `find_candidate()`（scheduler/scheduler.py）按 backend 组装。

## Flow
1. `find_candidate()`（scheduler/scheduler.py）实例化 `WorkerFilterChain([ClusterFilter, GPUMatchingFilter, LabelMatchingFilter, StatusFilter, BackendFrameworkFilter, LocalPathFilter])`，把全量 workers 逐级过滤。
2. 按 backend 选 `*ResourceFitSelector`，其内部依次尝试多档候选生成策略（manual → 单 worker 单/多 GPU → 多 worker 多 GPU → 部分卸载/CPU），每个候选携带 `ComputedResourceClaim`（vram/ram/tensor_split/offload_layers）。
3. `CandidateScoreChain` 打分 → `pick_highest_score_candidate()` 取最高分；`ModelInstanceScheduleCandidate` 写回 ModelInstance（SCHEDULED）。
4. 无候选时，selector 的 `get_messages()` + 过滤链 messages 汇总写入 `state_message`（PENDING 回退）。

## Integration
- 被 **`gpustack/scheduler/scheduler.py`**（`find_candidate`）与 **`gpustack/scheduler/evaluator.py`**（兼容性预检）直接调用。
- 本层依赖 **`gpustack/schemas/`**（Model/ModelInstance/Worker/ComputedResourceClaim）、**`gpustack/client/worker_filesystem_client.py`**（worker 侧权重大小/文件查询）、**`gpustack/scheduler/calculator.py`**（本地权重体积计算）、**`gpustack/server/services.py`**（ModelFileService 取缓存大小）。
- 子包 `worker_filters/`、`candidate_selectors/`、`scorers/`、`event_recorder/` 均以 `base.py` 的抽象为共同接口。
