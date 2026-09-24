# gpustack/detectors/

## Responsibility
worker 端硬件检测抽象层：定义"探测 GPU 与系统信息"的稳定接口，并负责按
配置选择具体探测实现。对应我们 agent 端的 GPU 监控与系统状态采集基础设施。
产出 `GPUDevicesStatus`（GPU 列表）与 `SystemInfo`（OS/内核/CPU/内存/磁盘）
两类结构化结果，供 `worker/collector.py` 组装 WorkerStatus 上报。

## Design
- **接口抽象（`base.py`）**：两个 ABC——
  `GPUDetector`：`is_available()` + `gather_gpu_info() -> GPUDevicesStatus`
  `SystemInfoDetector`：`gather_system_info() -> SystemInfo`
- 另定义 `GPUDetectExepction`（注意拼写，源码如此）：
  约定抛此异常会把错误信息写进 worker 的 `state_message`，
  并把状态置为 NOT_READY（见 collector 注释说明）。
- **工厂模式（`detector_factory.py`）**：`DetectorFactory` 持有
  `system_info_detector` 与 `gpu_detectors` 探测器列表。
  默认（未显式配置 device）构造 `[Runtime()]` 作为 GPU 探测器、
  `Fastfetch()` 作为系统信息探测器；
  显式 device 时从注入的 `gpu_detectors` dict 中按设备名取值。
- **优雅降级链路**：`detect_gpus()` 顺序遍历探测器，第一个
  `is_available()` 为真且 `gather_gpu_info()` 有返回的即为结果；
  全部不可用/无返回则 `return []`（空列表，不抛异常）。
- `_filter_gpu_devices` 过滤掉 `memory.total <= 0` 的无效 GPU
  （仅 debug 日志）。
- `Runtime.is_available()` 恒为 True，但底层 `detect_devices` 探测不到
  设备时返回空 list——这就是"无 GPU 环境优雅降级"的机制：
  上层拿到 `[]` 而非崩溃。
- **多实现并存**：
  - `custom/`：配置注入的静态数据（`Custom` 类）
  - `runtime/`：gpustack-runtime 原生硬件检测（`Runtime` 类）
  - `fastfetch/`：打包的 fastfetch 二进制采集系统信息（`Fastfetch` 类）

## Flow
- `worker/collector.py.WorkerStatusCollector.__init__` 按配置组合探测器：
  配置了 GPU 与系统信息 → `device="custom"` 注入 `Custom` 探测器；
  否则走默认 `DetectorFactory()`。
- `collect()` 流程：
  ① `detect_system_info()`（失败仅 log error，状态照常上报）
  ② 非 initial 轮询时 `detect_gpus()` → 捕获 `GPUDetectExepction` 时写
     `state_message`（状态 NOT_READY），其他异常仅 log error
  ③ 注入 unified_memory 与 Windows 合并磁盘用量（`_inject_unified_memory`、
     `_inject_computed_filesystem_usage`）
  ④ 组装 `WorkerStatusPublic` 上报管理端
- fastfetch 探测：`_run_command(subprocess.run, check=True)` → 按模块配置
  （`config_gpu.jsonc` 仅 gpu 模块、`config_system_info.jsonc` 含
  os/kernel/uptime/cpu/memory/swap/disk 等）执行并 `json.loads` 解析 →
  `gather_system_info` 按 `type` 字段分发填进 `SystemInfo` 各子结构。

## Integration
- 消费者：`worker/collector.py`（唯一调用方），其结果进入
  `WorkerStatus.gpu_devices` / `system_info` 并通过 API 上报 server。
- 依赖方：`gpustack.schemas.workers` 的数据类型（`GPUDevicesStatus`、
  `SystemInfo` 等）；`runtime/` 依赖外部包 `gpustack_runtime.detector`；
  `fastfetch/` 依赖 `gpustack.third_party.bin.fastfetch` 打包二进制与
  `compat_importlib.pkg_resources` 定位资源路径。
- 与我们 agent 的对应关系：本层 ≈ agent 的 `get_gpu_status()` 抽象——
  gpustack 的做法是把"探测实现"与"上报组装"解耦，并约定
  "探测失败返回空/写 state_message、绝不抛异常中断上报"，
  正是我们优雅降级约定的参考模板。
