# gpustack/scheduler/

## Responsibility
模型实例（ModelInstance）调度核心层。负责把 PENDING/ANALYZING/SCHEDULED 状态的模型实例驱动到 SCHEDULED（选定 worker 与 GPU 并写入 `worker_id`/`gpu_indexes`/`computed_resource_claim`/`distributed_servers`）。包含调度器本体、调度队列、模型资源需求评估（calculator）、模型分类识别（model_registry/meta_registry）与模型兼容性预检（evaluator）。

## Design
- **`Scheduler`**（scheduler.py）：单例常驻协程。构造时持有 `Config`、`AsyncUniqueQueue` 与 `check_interval`（默认 180s）。`start()` 注册两条入队路径：
  - 定时任务：`AsyncIOScheduler` + `IntervalTrigger`，周期调用 `_enqueue_pending_instances()` 全量扫描；启动时立即 bootstrap 一次。
  - 事件驱动：订阅 `ModelInstance.subscribe` 的 `CREATED` 事件，逐个 `_enqueue_event_instance()`（不重放历史）。
- **`AsyncUniqueQueue`**（queue.py）：asyncio.Queue + set 去重 + Lock，保证同一实例不会重复排队。
- **`_should_schedule()`**：状态机门控——新创建 PENDING（<1s）、长时间未更新的 PENDING（>90s）、ANALYZING/SCHEDULED 卡住超 3 分钟（可能 server 重启/worker 掉线）才重新调度。
- **`_schedule_cycle()` / `_schedule_one()`**：消费队列，逐个 `find_candidate()`；有候选则置 SCHEDULED 并回填 worker/GPU 信息，无候选则回退 PENDING 并附过滤消息。
- **`find_candidate()`**（模块级函数）：策略链编排入口（见 policies/）。按 backend 分发到不同 `*ResourceFitSelector`；vLLM 非 omni → `VLLMResourceFitSelector`，GGUF → `GGUFResourceFitSelector`，SGLang → `SGLangResourceFitSelector`，Ascend → `AscendMindIEResourceFitSelector`，其余 → `CustomBackendResourceFitSelector`。随后 `CandidateScoreChain([PlacementScorer, ModelFileLocalityScorer])` 打分，`pick_highest_score_candidate()` 取最高分。
- **`evaluate_gguf_model` / `evaluate_pretrained_config` / `evaluate_diffusion_model`**：模型评估，更新 categories（LLM/EMBEDDING/IMAGE/RERANKER）、distributable、`gpus_per_replica`。
- **`calculator.py`**：GGUF 模型的资源需求计算。调 `gguf-parser` 二进制（`_gguf_parser_command`，本地或经 `WorkerFilesystemClient` 在 worker 上并发解析 `_try_parse_on_workers`），产出 `Estimate`（含 `LayerMemoryEstimate`、offloadLayers、fullOffloaded 等），包装为 `ModelResourceClaim`。对非 GGUF 提供 `get_pretrained_config_with_workers`（含 worker 回退）。
- **`model_registry.py`**：与 vLLM 同步的架构名白名单，`detect_model_type()` 把架构映射到 `CategoryEnum`（LLM/EMBEDDING/RERANKER/SPEECH_TO_TEXT/TEXT_TO_SPEECH/UNKNOWN）。
- **`meta_registry.py`**：`get_model_meta()` 依据架构补充语言/音色等模型元数据（Voxtral/Granite-Speech/Whisper/Qwen3-TTS）。
- **`evaluator.py`**：`evaluate_models()` 批量兼容性预检（输入校验 → 元数据 → 环境约束），带 `TTLCache` 缓存与 `AsyncLimiter` 限流（50 次/10s，规避 HF API 限速）；每个 cluster 调用 `scheduler.find_candidate` 判断可调度性与 overcommit。

## Flow
1. ModelInstance CREATED 事件或 180s 定时扫描 → `_evaluate()`：置 ANALYZING，GGUF 先 `evaluate_gguf_model` 或 `evaluate_pretrained_config` 评估模型，再 `_queue.put()`。
2. `_schedule_cycle()` 取实例 → `_schedule_one()`：加载全量 workers/models/instances → `find_candidate()`（WorkerFilterChain 过滤 → 按 backend 选 selector → `select_candidates()` → 评分链 → 选最高分）。
3. 有候选：写 `worker_id`/`gpu_indexes`/`computed_resource_claim`/`distributed_servers`，状态 → SCHEDULED；无候选：回退 PENDING，`state_message` 汇总过滤消息。
4. 评估路径：`evaluator.evaluate_model()` 也走 `find_candidate()`，只取结果不回写，产出 `ModelEvaluationResult`（compatible/resource_claim）。

## Integration
- 被 **`gpustack/server/server.py`** 启动：`Scheduler(self._config).start()` 作为后台任务。
- 依赖 **`policies/`** 全部子包（base 的策略链、candidate_selectors、scorers、worker_filters、event_recorder、utils 的资源分配计算）。
- 依赖 **`gpustack/schemas/`**（Model/ModelInstance/Worker）、**`gpustack/server/services.py`**（ModelInstanceService/ModelService/ModelFileService）、**`gpustack/server/bus.py`**（EventType）、**`gpustack/client/worker_filesystem_client.py`**（worker 远程解析/读配置）。
- **`gpustack/server/controllers.py`** 消费 `evaluator.evaluate_models` 做模型创建前兼容性检查；缩容（scale down）另用 `ModelInstanceScoreChain`（StatusScorer + OffloadLayerScorer + PlacementScorer SCALE_DOWN），与调度器的 scale up 评分互为镜像。
