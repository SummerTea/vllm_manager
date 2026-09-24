# gpustack/detectors/runtime/

## Responsibility
GPU 探测器：通过外部包 `gpustack_runtime`（Rust 原生实现，随 wheel 分发）
的 `detect_devices(fast=False)` 探测真实 GPU 硬件，并把其输出转换成
gpustack 的 `GPUDeviceStatus` 数据结构。这是默认（且唯一）的 GPU 探测实现，
覆盖 NVIDIA/AMD/Ascend/Intel/Moore 等多厂商。

## Design
- 单类 `Runtime(GPUDetector)`，是对 `gpustack_runtime.detector` 的薄适配层。
- **`is_available()` 恒返回 `True`**：探测器本身总是"可用"，真正的硬件
  存在性判断下沉到 `gather_gpu_info()`——`detect_devices()` 返回空列表时
  直接 `return ret`（空 `GPUDevicesStatus`）。
- 配合 `DetectorFactory.detect_gpus()` 对空结果继续尝试下一个探测器/
  最终返回 `[]`，构成完整优雅降级：无 GPU 环境不抛异常，只是产出空列表。
- **字段转换（单位换算关键点）**：`dev.memory` / `dev.memory_used`
  单位是 MiB，转换时 `int(dev.memory * (1 << 20))` 放大为 Bytes；
  core 的 `total` / `utilization_rate`、`temperature` 直传；
  `driver_version`、`runtime_version`、`compute_capability`、`uuid` 直接映射。
- **厂商后端映射**：`manufacturer_to_backend(dev.manufacturer)` 把厂商枚举
  转成后端名（nvidia→vllm 等），写入 `gpudev.type`；
  `vendor` 存厂商枚举的原始值。
- **附录信息修正**：`dev.appendix`（dict）里按需修正：
  - `card_id` 覆盖 `device_index`
  - `device_id` 覆盖 `device_chip_index`（用 `safe_int` 安全解析）
  - `arch_family` 记录架构族
  - Ascend 设备额外解析 `roce_ip` / `roce_mask` / `roce_gateway`
    填 `GPUNetworkInfo`（有 IP 才置 status="up"）
- `device_chip_index` 默认填 0；`core.total` 缺失时兜底 0
  （`dev.cores or 0`）。

## Flow
- `DetectorFactory` 默认 `gpu_detectors = [Runtime()]`
  → `detect_gpus()` 调 `Runtime.gather_gpu_info()`
  → `detect_devices(fast=False)` 拿原生设备列表
  → 逐设备构造 `GPUDeviceStatus`（vendor/type/index/name/uuid/驱动版本/
    算力/核/显存/温度/附录修正）
  → 返回列表
  → 工厂 `_filter_gpu_devices` 剔除 `memory.total<=0` 的设备
  → collector 写入 `status.gpu_devices` 上报。
- 无 GPU/驱动失败路径：`detect_devices` 返回空 → 返回 `[]` → 上报空 GPU
  列表，状态保持 Ready（区别于显式抛 `GPUDetectExepction` 的 NOT_READY）。

## Integration
- 依赖：外部包 `gpustack_runtime.detector`（`detect_devices`、
  `manufacturer_to_backend`、`ManufacturerEnum`）——真正执行硬件枚举的
  nvidia-smi/NVML 等价逻辑在这里，而非本仓库；`gpustack.utils.convert.safe_int`；
  `gpustack.schemas.workers` 类型（`GPUDeviceStatus`/`GPUDevicesStatus`/
  `GPUCoreInfo`/`MemoryInfo`/`GPUNetworkInfo`）。
- 消费者：`gpustack/detectors/detector_factory.py`。
- 与我们的对应：本模块 ≈ agent 的 `get_gpu_status()` 的"探测实现"角色。
  参考价值点：
  ① 硬件探测独立成外部原生包、Python 侧只做 DTO 转换，保持探测性能与
     进程隔离；
  ② 探测失败/无设备返回空列表而非异常，由工厂+collector 双层兜底；
  ③ 显存单位转换（MiB→Bytes）与附录字段安全解析（`safe_int`）的防御性写法。
