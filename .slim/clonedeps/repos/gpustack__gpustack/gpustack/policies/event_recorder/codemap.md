# gpustack/policies/event_recorder/

## Responsibility
调度过程事件记录器（Event Recorder）：为 candidate selectors 提供轻量级的结构化事件采集与日志输出，是调度"失败原因可解释性"的基础设施。选择器内各类失败/降级路径通过它记录事件，最终由 `get_messages()` 聚合为用户可见的调度诊断消息。

## Design
- **`EventLevelEnum`**（str Enum）：`INFO` / `WARNING` / `ERROR` 三级。
- **`Event`**：单条事件载体。字段含 `level`、`action`（调度动作/路径标识，如 `default_scheduling_msg`、`manual_multi_gpu_scheduling_msg`、`auto_single_gpu_scheduling_msg` 等，常量定义在 base_candidate_selector.py 与 gguf selector 中）、`message`、`reason`（可选，如 `INSUFFICIENT_RESOURCES`）、`timestamp`（UTC ISO），并支持 kwargs 附加字段。
- **`EventCollector`**：每 selector 实例持有一个。`add(level, action, message, **kwargs)` 内部以 `{level}-{action}-{message}` 去重（`_event_keys`），避免同一调度轮次重复记录；同时把事件按级别镜像到传入的 `logger`。`events` 列表供 `get_messages()` 遍历聚合。

## Flow
1. 选择器在候选生成过程中调用 `event_collector.add(...)` 记录默认需求说明、各档候选路径的失败原因、overcommit 提示等。
2. `EventCollector` 去重后追加到 `self.events`，并同步写日志（ERROR/WARNING/INFO 分别走对应日志级别）。
3. 选择器的 `_set_messages()`/`get_messages()` 按优先级（manual > 多 worker > 单 worker 多 GPU > 单 GPU > 默认）取第一个非空 action 消息组装诊断文本。

## Integration
- 仅被 **`gpustack/policies/candidate_selectors/`** 使用（base_candidate_selector.py 及各 backend selector 均实例化 `EventCollector(self._model, logger)`）。
- `action`/`reason` 常量与各 selector 的 `_set_messages()` 聚合逻辑耦合，新增候选路径时需同步定义常量。
- 最终消息经 scheduler 写入 ModelInstance 的 `state_message` 呈现给用户。
