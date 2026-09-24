# gpustack/detectors/custom/

## Responsibility
自定义静态探测器：把配置里直接给定的 GPU 列表与系统信息原样注入检测层，
跳过真实硬件探测。用于"人工指定 worker 硬件参数"或测试/演示场景
（例如配置里手动填写 GPU 型号与系统信息，而不是跑真实探测）。

## Design
- 单文件 `custom.py`，一个类 `Custom`，同时实现 `GPUDetector` 与
  `SystemInfoDetector` 两个 ABC（多重继承）。
- 构造时注入数据：`Custom(gpu_devices=..., system_info=...)`，
  不注入的字段保持 `None`。
- 接口全部退化为"数据访问器"：
  - `is_available()` 恒返回 `True`（静态数据永远"可用"）
  - `gather_gpu_info()` 直接返回构造时持有的 `_gpu_devices`
  - `gather_system_info()` 直接返回构造时持有的 `_system_info`
  （两者都可能为 `None`，由上层自行兜底）
- 无任何探测逻辑、无 IO、无异常路径——是探测器家族里最简单的一档，
  用于验证"多实现可插拔"的架构边界。

## Flow
- `worker/collector.py` 判定配置存在 GPU/系统信息时，构造：
  ```
  DetectorFactory(
      device="custom",
      gpu_detectors={"custom": [Custom(gpu_devices=...)]},
      system_info_detector=Custom(system_info=...),
  )
  ```
- 之后与真实探测器走同一条 `detect_gpus()` / `detect_system_info()`
  调用路径，结果直接进入 WorkerStatus 上报。
- 三种组合按配置自动选择：
  ① GPU + 系统信息都有 → 两个都注入
  ② 仅 GPU → 只注入 gpu_detectors
  ③ 仅系统信息 → 只注入 system_info_detector

## Integration
- 被 `worker/collector.py` 按配置选择性实例化（上述三种组合）。
- 依赖 `gpustack.detectors.base` 的接口与 `gpustack.schemas.workers`
  的数据类型（`GPUDevicesStatus`、`SystemInfo`）。
- 与我们的对应：相当于 agent 配置中手动指定 GPU 状态/系统信息的覆盖通道；
  其"静态数据也走标准探测器接口"的设计让 collector 无需区分探测来源，
  是配置注入与真实探测共存的范例。
