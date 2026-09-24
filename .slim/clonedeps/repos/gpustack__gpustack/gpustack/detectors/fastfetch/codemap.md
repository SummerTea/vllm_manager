# gpustack/detectors/fastfetch/

## Responsibility
基于打包的 fastfetch CLI 二进制采集系统信息（OS/内核/CPU/内存/交换分区/
磁盘挂载点），实现 `SystemInfoDetector`。是默认的系统信息探测器，
替代逐项调用系统命令/读取 proc 的繁琐做法——一次 fastfetch JSON 输出
即拿到全套 SystemInfo。

## Design
- 单类 `Fastfetch(SystemInfoDetector)`，方法按功能分组：
  - **可用性探测** `is_available()`：执行 `_command_version()`
    （`fastfetch --version`），异常被捕获并 warning 后返回 `False`——
    这是"外部命令缺失时优雅降级"的入口，调用方（collector）对系统信息
    失败仅 log，不中断上报。
  - **采集** `gather_system_info()`：执行 `_command_gather_system()` 并以
    JSON 解析，按每条结果的 `type` 字段分发填充 `SystemInfo`：
    - OS → `OperatingSystemInfo`（name/version）
    - Kernel → `KernelInfo`（name/release/version/architecture）
    - Uptime → `UptimeInfo`（毫秒转秒，`/1000`）
    - CPU → `CPUInfo.total`；CPUUsage → 各核平均 `utilization_rate`
    - Memory/Swap → 总/用/利用率（`used/total*100`）
    - Disk → `MountPoint` 列表
  - **命令构造**：`_command_executable_path()` 用
    `pkg_resources.path("gpustack.third_party.bin.fastfetch", ...)` 定位
    打包的二进制（Windows 加 `.exe` 后缀）；GPU 与系统各用独立 jsonc
    配置（`--config` + `--format json`）。GPU 配置额外带
    `--gpu-driver-specific`、`--gpu-temp`、`--gpu-detection-method pci`。
- **`_run_command` 统一异常语义**：`subprocess.run(check=True)` 包装，
  非零返回码/空输出/JSON 解析失败一律抛带 stdout/stderr 的 `Exception`，
  由 `is_available` 或 collector 兜底。
- **`_get_value(input, *keys)`** 链式安全取字典值，任意层级缺失返回
  `None`（JSON 字段可选时永不 KeyError）。
- jsonc 配置：`config_gpu.jsonc`（modules: ["gpu"]）、
  `config_system_info.jsonc`（title/os/kernel/uptime/cpu/cpuusage/memory/
  swap/disk/localip），通过 `pkg_resources.path` 随包分发。

## Flow
- `DetectorFactory.__init__` 默认 `system_info_detector = Fastfetch()`
  → collector `detect_system_info()`
  → `gather_system_info()`
  → `_command_gather_system()` 组装命令
  → `subprocess.run`
  → `json.loads`
  → 按 `type` 逐条映射到 `SystemInfo` 各字段
  → collector `model_validate` 进 WorkerStatus。
- 异常路径：命令缺失/执行失败/输出为空/JSON 非法 → 抛异常 → collector
  catch 后仅 `logger.error`，status 保留默认值继续上报。

## Integration
- 消费者：`gpustack/detectors/detector_factory.py`（默认 system_info_detector）。
- 依赖：打包资源 `gpustack.third_party.bin.fastfetch`（真实二进制随 wheel
  分发）与同目录 jsonc 配置、`gpustack.utils.compat_importlib.pkg_resources`、
  `gpustack.schemas.workers` 类型。
- 与我们的对应：其"外部工具缺失 → `is_available` False/采集抛异常 →
  调用方 catch 降级"的模式，与我们 agent 在无 GPU 机器上 pynvml 不可用时
  返回空列表并告警一次的约定同构；
  可选参考点是用一次外部命令批量取系统信息，减少逐项探测的进程开销。
