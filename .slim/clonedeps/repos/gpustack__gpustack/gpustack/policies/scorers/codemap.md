# gpustack/policies/scorers/

## Responsibility
候选/实例评分层（Scorer / Strategy），实现两种评分链：候选评分链 `CandidateScoreChain`（调度 scale up 用，对 `ModelInstanceScheduleCandidate` 打分）与实例评分链 `ModelInstanceScoreChain`（缩容 scale down 用，对 `ModelInstance` 打分）。评分决定"多个可行候选里选哪个/先缩哪个实例"。

## Design
- **`CandidateScoreChain`**（score_chain.py）：聚合多个 `ScheduleCandidatesScorer` 的分数。逐个 scorer 打分前重置 `candidate.score`，避免 scorer 间串扰；最终 `total_scores` 按对象 id 求和回填。scheduler 中实例为 `[PlacementScorer, ModelFileLocalityScorer]`。
- **`ModelInstanceScoreChain`**：只保留 `max_score > 0` 的 scorer；按各 scorer 的 max_score 加权归一化到 `total_max_score`（scale factor = total_max / sum_max），保证不同 scorer 配置下总分可比较。controllers.py 缩容路径使用。
- **`PlacementScorer`**（placement_scorer.py，同时实现两个评分接口）：核心放置策略，支持两种 `PlacementStrategyEnum`：
  - **BINPACK**（装箱）：`score_binpack()` 计算 claim/allocatable 资源占用率（ram、vram 各带权重 `ResourceWeight(vram=2.0, ram=1.0)`，vram 优先），占用越满分越高；scale_down 时按"释放后余量"计算以优先缩已占用少的；多机候选按 `InferenceServerTypeWeight(server=5.0, rpc_server=1.0)` 加权主/从 worker 分数。
  - **SPREAD**（分散）：`score_spread()` 统计每个 worker/GPU 上当前模型与其它模型的实例数（`_get_worker_model_instance_count`），`inverse_norm()` 反归一化（实例越少分越高），worker 维度权重 0.85、GPU 维度 0.15，`SpreadScoreWeights` 控制各分支基分。
  - `_max_score` 默认取 `envs.SCHEDULER_SCALE_UP_PLACEMENT_MAX_SCORE`，支持 `ScaleTypeEnum.SCALE_UP/SCALE_DOWN` 双向复用。
- **`ModelFileLocalityScorer`**：模型文件局部性。查 `ModelFileService` 中 `READY` 状态的模型文件所在 worker（主模型 + draft 模型），候选覆盖的 worker 命中比例越高分越高（draft 权重 0.5 次信号），`max_score` 由 env `SCHEDULER_SCALE_UP_LOCALITY_MAX_SCORE` 控制（为 0 则不启用）。
- **`OffloadLayerScorer`**（ModelInstanceScorer）：按 offload_layers / total_layers 比例给分（全量卸载得满分），用于缩容时优先缩卸载少的实例。
- **`StatusScorer`**（ModelInstanceScorer）：worker READY + 实例 RUNNING 得满分，否则半/零分，用于缩容排序。

## Flow
- **scale up**：`find_candidate()` → selector 产候选 → `CandidateScoreChain([PlacementScorer, ModelFileLocalityScorer]).score(candidates)` → `pick_highest_score_candidate()` 取最高分。
- **scale down**：`find_scale_down_candidates()`（server/controllers.py）→ `ModelInstanceScoreChain([StatusScorer, OffloadLayerScorer, PlacementScorer(SCALE_DOWN)])` → 按分数升序排序（分低者先缩）。

## Integration
- 被 **`gpustack/scheduler/scheduler.py`**（scale up 候选评分）与 **`gpustack/server/controllers.py`**（scale down 实例评分，`find_scale_down_candidates`）消费。
- 依赖 **`gpustack/policies/base.py`**（评分抽象与候选/实例数据结构）、**`gpustack/policies/utils.py`**（`get_worker_allocatable_resource`）、**`gpustack/server/db.py`**（async_session 查 worker）、**`gpustack/server/services.py`**（ModelFileService）。
- 各 scorer 的 max_score 均可由 env（`SCHEDULER_SCALE_UP/DOWN_*_MAX_SCORE`）配置。
