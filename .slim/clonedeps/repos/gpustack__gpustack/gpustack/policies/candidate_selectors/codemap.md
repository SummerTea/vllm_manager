# gpustack/policies/candidate_selectors/

## Responsibility
候选节点选择层（Candidate Selector），即调度策略中的 "Resource Fit" 阶段：给定过滤后的 workers，按模型后端（backend）与资源需求，产出全部可行 `ModelInstanceScheduleCandidate`（worker + gpu_indexes + computed_resource_claim），并给出无候选时的诊断消息。vllm_manager 未实现调度，本层是"如何挑选 GPU/worker 放实例"的核心参考。

## Design
- **`ScheduleCandidatesSelector`**（base_candidate_selector.py，ABC）：所有选择器的公共基类，持有 `Config`/`Model`/`ModelInstance` 列表及缓存（worker allocatable resource、VRAM totals）。核心职责：
  - `_init_model_parameters()`：经 `get_pretrained_config_with_workers` 拉取模型 pretrained config，填充 `ModelParameters`（hidden_size、num_attention_heads、num_key_value_heads、head_dim、MoE 专家数、attention type MHA/GQA/MQA/MLA 判定等），供 TP 整除性校验使用。
  - `_set_gpu_count()`：手动 GPU 选择（`gpu_selector.gpu_ids`）与并行度参数（tp/pp/dp → world_size）的归一化与冲突校验（`GPUSTACK_SKIP_GPU_COUNT_CHECK` 可跳过）。
  - 手动选择路径 `_find_manual_gpu_selection_candidates()`：按 GPU ID 精确定位 worker/GPU，拒绝异构 GPU 类型混布，支持单 worker 单/多 GPU 与多 worker 多 GPU；`_generate_manual_selected_gpus_overcommit_message()` 判定 overcommit（显存/利用率不满足时给出"降级可运行"候选）。
  - TP 整除校验 `_is_tp_size_divisible()`：num_attention_heads / vision heads / vocab_size 必须被 tp_size 整除。
  - 抽象接口：`select_candidates()`、`get_messages()`、`_should_check_vision_tp_divisibility()`。
- **backend 分派**（由 scheduler.find_candidate 按模型选择，__init__.py 汇总导出）：
  - `VLLMResourceFitSelector`（vllm_resource_fit_selector.py）：最完整。`get_world_size_from_backend_parameters()` 解析 tp/pp/pcp/dp/dpl 并行度；`_set_gpu_memory_utilization()` 按模型类别启用/禁用 GMU（非 LLM 模型禁用，Qwen3 系例外）；候选函数按序尝试：manual → `find_single_worker_single_gpu_full_offloading_candidates` → `find_single_worker_multi_gpu_full_offloading_candidates` → `find_multi_worker_multi_gpu_candidates`；多机候选要求各 worker GPU 数一致、全部满足利用率，`_create_candidate()` 组装主/从 worker（`ModelInstanceSubordinateWorker`），MP 后端额外 `validate_multinode_topology`。
  - `GGUFResourceFitSelector`（gguf_resource_fit_selector.py，2526 行，最复杂）：为 llama.cpp 系后端特化。先 `_set_offload_resource_claim()` 计算 full/partial/disable 三种卸载档位的资源 claim（本地或 worker 远程跑 gguf-parser），候选序：单 worker 单/多 GPU 全量卸载 → 多 worker 多 GPU → 部分卸载（按层 offload、tensor split、RPC 组合）→ CPU-only；含大量组合缓存与 `_generate_combinations_for_worker_with_rpcs` 等拓扑生成。
  - `SGLangResourceFitSelector`（sglang_resource_fit_selector.py）：类似 vLLM 结构，另带 `MemFractionStaticCalculator` 静态计算 mem-fraction-static，`_get_nnodes` 处理多节点。
  - `AscendMindIEResourceFitSelector`（ascend_mindie_resource_fit_selector.py）：昇腾 NPU 特化，`_estimate_usage()` 估算 NPU 显存，取 NPU 设备网络地址做 gpu_addresses。
  - `CustomBackendResourceFitSelector`（custom_backend_resource_fit_selector.py）：兜底。只估 `estimate_model_vram`，支持 GPU 单/多卡与 CPU-only，不支持多 worker 手动选择。
- 所有选择器用 `EventCollector`（event_recorder/）记录每类失败路径的事件，`get_messages()` 最终聚合为给用户的调度说明。

## Flow
1. scheduler 过滤完 workers 后按 backend 实例化对应 selector。
2. `select_candidates(workers)`：初始化模型参数 → 估算 VRAM/RAM claim → 按优先级依次执行候选生成函数，命中即返回（overcommit 候选单独标记后返回）。
3. 候选列表交给 `CandidateScoreChain`（scorers/）打分排序。
4. 全失败时经 `get_messages()` + EventCollector 生成诊断信息，写入实例 state_message。

## Integration
- 唯一入口：**`gpustack/scheduler/scheduler.py` 的 `find_candidate()`**（后端分派）+ **`gpustack/scheduler/evaluator.py`**（兼容性预检共用同一路径）。
- 依赖 **`gpustack/policies/base.py`**（候选数据结构）、**`gpustack/policies/utils.py`**（资源可分配性/估算）、**`gpustack/policies/event_recorder/`**、**`gpustack/scheduler/calculator.py`**、**`gpustack/utils/`**（gpu 分组、命令参数解析、vllm_topology）。
- 输出 `ModelInstanceScheduleCandidate` 被 `pick_highest_score_candidate` 消费后写回 ModelInstance。
