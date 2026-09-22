"""worker GPU/系统状态采集（对齐 server NodeStatusReportRequest 载荷）。

GPU 采集基于 pynvml（nvidia-ml-py）；无 GPU / pynvml 不可用 / 初始化失败时
优雅降级为 gpu_devices=[]，仅首次告警一次，绝不抛异常（对齐 AGENTS.md）。
"""

import contextlib
import logging
import platform
import time
from typing import Any

import psutil

from app.worker.config import WorkerConfig

logger = logging.getLogger(__name__)

try:
    import pynvml  # type: ignore[import-not-found]

    _PYNVML_AVAILABLE = True
except ImportError:
    pynvml = None  # type: ignore[assignment]
    _PYNVML_AVAILABLE = False

# 降级告警标志：pynvml 不可用 / 初始化失败 / 无 GPU 时各告警一次
_warned_no_pynvml = False
_warned_init_failed = False
_warned_no_gpu = False


class WorkerStatusCollector:
    """节点状态采集器（无状态；collect() 每次现采）。"""

    def __init__(self, config: WorkerConfig):
        self._config = config

    def collect(self) -> dict:
        """采集完整状态载荷，返回 NodeStatusReportRequest 结构。

        Returns:
            {"system_reserved": {"ram": int, "vram": int}, "status": {...}}
        """
        return {
            "system_reserved": {
                "ram": self._config.WORKER_SYSTEM_RESERVED_RAM,
                "vram": self._config.WORKER_SYSTEM_RESERVED_VRAM,
            },
            "status": {
                "cpu": self._collect_cpu(),
                "memory": self._collect_memory(),
                "swap": self._collect_swap(),
                "gpu_devices": self._collect_gpu_devices(),
                "filesystem": self._collect_filesystem(),
                "os": self._collect_os(),
                "kernel": self._collect_kernel(),
                "uptime": self._collect_uptime(),
            },
        }

    # ---------- 系统采集 ----------

    def _collect_cpu(self) -> dict:
        return {
            "total": psutil.cpu_count(),
            "utilization_rate": psutil.cpu_percent(interval=None),
        }

    def _collect_memory(self) -> dict:
        mem = psutil.virtual_memory()
        return {
            "total": mem.total,
            "used": mem.used,
            "utilization_rate": mem.percent,
        }

    def _collect_swap(self) -> dict:
        swap = psutil.swap_memory()
        return {"total": swap.total, "used": swap.used}

    def _collect_filesystem(self) -> list[dict]:
        result: list[dict] = []
        try:
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    result.append(
                        {
                            "name": part.device,
                            "mount_point": part.mountpoint,
                            "total": usage.total,
                            "used": usage.used,
                        }
                    )
                except OSError:
                    continue
        except Exception:  # noqa: BLE001 - 文件系统采集失败降级为空列表
            logger.warning("文件系统采集失败")
        return result

    def _collect_os(self) -> dict:
        return {"name": platform.system(), "version": platform.release()}

    def _collect_kernel(self) -> dict:
        return {"release": platform.release(), "version": platform.version()}

    def _collect_uptime(self) -> dict:
        try:
            boot_time = psutil.boot_time()
            return {"seconds": int(time.time() - boot_time)}
        except Exception:  # noqa: BLE001 - 采集失败给空 dict
            return {}

    # ---------- GPU 采集（pynvml，优雅降级） ----------

    def _collect_gpu_devices(self) -> list[dict[str, Any]]:
        """采集 GPU 设备列表；pynvml 不可用 / 无 GPU / 失败 → [] 且不抛异常。"""
        global _warned_no_pynvml, _warned_init_failed, _warned_no_gpu

        if not _PYNVML_AVAILABLE or pynvml is None:
            if not _warned_no_pynvml:
                logger.warning("pynvml 不可用（nvidia-ml-py 未安装），GPU 状态降级为空列表")
                _warned_no_pynvml = True
            return []

        try:
            pynvml.nvmlInit()
        except Exception as e:  # noqa: BLE001 - nvml 初始化失败（无驱动等）降级
            if not _warned_init_failed:
                logger.warning("nvmlInit 失败，GPU 状态降级为空列表: %s", e)
                _warned_init_failed = True
            return []

        try:
            try:
                count = pynvml.nvmlDeviceGetCount()
            except pynvml.NVMLError as e:
                if not _warned_init_failed:
                    logger.warning("nvmlDeviceGetCount 失败，GPU 状态降级为空列表: %s", e)
                    _warned_init_failed = True
                return []

            if count == 0:
                if not _warned_no_gpu:
                    logger.warning("本机无 NVIDIA GPU，GPU 状态为空列表")
                    _warned_no_gpu = True
                return []

            devices: list[dict[str, Any]] = []
            for index in range(count):
                try:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    try:
                        power_mw = pynvml.nvmlDeviceGetPowerUsage(handle)
                        power = power_mw / 1000.0  # 毫瓦 → 瓦
                    except pynvml.NVMLError:
                        power = None
                    devices.append(
                        {
                            "index": index,
                            "name": pynvml.nvmlDeviceGetName(handle),
                            "vendor": "NVIDIA",
                            "uuid": pynvml.nvmlDeviceGetUUID(handle),
                            "memory_total": mem.total,
                            "memory_used": mem.used,
                            "utilization_rate": float(utilization.gpu),
                            "temperature": float(
                                pynvml.nvmlDeviceGetTemperature(
                                    handle, pynvml.NVML_TEMPERATURE_GPU
                                )
                            ),
                            "power": power,
                        }
                    )
                except Exception as e:  # noqa: BLE001 - 单卡采集异常跳过该卡
                    logger.warning("GPU 卡 %s 采集失败，跳过: %s", index, e)
            return devices
        finally:
            with contextlib.suppress(Exception):  # shutdown 失败不影响结果
                pynvml.nvmlShutdown()
