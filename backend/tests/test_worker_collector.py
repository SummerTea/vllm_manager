"""Worker 状态采集器契约测试（pynvml mock + 优雅降级）。"""

from types import SimpleNamespace

import app.worker.collector as collector_mod
from app.worker.collector import WorkerStatusCollector
from app.worker.config import WorkerConfig


def _make_collector(**cfg_overrides) -> WorkerStatusCollector:
    cfg = WorkerConfig(**cfg_overrides)
    return WorkerStatusCollector(cfg)


def _fake_nvml():
    """构造模块级假 pynvml：一卡标准数据。"""
    return SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda i: SimpleNamespace(_i=i),
        nvmlDeviceGetName=lambda h: "NVIDIA A100",
        nvmlDeviceGetUUID=lambda h: "GPU-1234",
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(total=80 * 1024**3, used=8 * 1024**3),
        nvmlDeviceGetUtilizationRates=lambda h: SimpleNamespace(gpu=42),
        nvmlDeviceGetTemperature=lambda h, t: 55,
        nvmlDeviceGetPowerUsage=lambda h: 250_000,  # 毫瓦 → 250W
        NVML_TEMPERATURE_GPU=0,
        NVMLError=Exception,
    )


def test_collect_gpu_devices_fields(monkeypatch):
    """pynvml 可用时：gpu_devices 字段正确（index/name/memory_total/单位换算）。"""
    monkeypatch.setattr(collector_mod, "pynvml", _fake_nvml())
    monkeypatch.setattr(collector_mod, "_PYNVML_AVAILABLE", True)
    collector = _make_collector()

    payload = collector.collect()
    devices = payload["status"]["gpu_devices"]

    assert len(devices) == 1
    gpu = devices[0]
    assert gpu["index"] == 0
    assert gpu["name"] == "NVIDIA A100"
    assert gpu["vendor"] == "NVIDIA"
    assert gpu["uuid"] == "GPU-1234"
    assert gpu["memory_total"] == 80 * 1024**3
    assert gpu["memory_used"] == 8 * 1024**3
    assert gpu["utilization_rate"] == 42
    assert gpu["temperature"] == 55
    assert gpu["power"] == 250.0  # 毫瓦 → 瓦


def test_collect_gpu_zero_count_degrades(monkeypatch, caplog):
    """count==0（无 GPU）：gpu_devices==[]，不抛异常。"""
    nvml = SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetCount=lambda: 0,
        NVMLError=Exception,
    )
    monkeypatch.setattr(collector_mod, "pynvml", nvml)
    monkeypatch.setattr(collector_mod, "_PYNVML_AVAILABLE", True)
    collector = _make_collector()

    payload = collector.collect()
    assert payload["status"]["gpu_devices"] == []
    assert "无 NVIDIA GPU" in caplog.text


def test_collect_gpu_pynvml_unavailable(monkeypatch, caplog):
    """pynvml 不可用（import 失败）：gpu_devices==[]，不抛异常。"""
    monkeypatch.setattr(collector_mod, "pynvml", None)
    monkeypatch.setattr(collector_mod, "_PYNVML_AVAILABLE", False)
    collector = _make_collector()

    payload = collector.collect()
    assert payload["status"]["gpu_devices"] == []
    assert "pynvml 不可用" in caplog.text


def test_collect_gpu_nvml_init_failed(monkeypatch, caplog):
    """nvmlInit 失败（无驱动）：gpu_devices==[]，不抛异常。"""
    nvml = SimpleNamespace(
        nvmlInit=lambda: (_ for _ in ()).throw(RuntimeError("driver not found")),
        NVMLError=Exception,
    )
    monkeypatch.setattr(collector_mod, "pynvml", nvml)
    monkeypatch.setattr(collector_mod, "_PYNVML_AVAILABLE", True)
    collector = _make_collector()

    payload = collector.collect()
    assert payload["status"]["gpu_devices"] == []
    assert "nvmlInit 失败" in caplog.text


def test_collect_gpu_single_card_failure_skips(monkeypatch):
    """单卡采集异常跳过该卡，不拖垮整次采集。"""
    nvml = SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetCount=lambda: 2,
        nvmlDeviceGetHandleByIndex=lambda i: SimpleNamespace(_i=i),
        nvmlDeviceGetName=lambda h: "NVIDIA T4",
        nvmlDeviceGetUUID=lambda h: "GPU-x",
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(total=16 * 1024**3, used=1 * 1024**3),
        nvmlDeviceGetUtilizationRates=lambda h: SimpleNamespace(gpu=10),
        nvmlDeviceGetTemperature=lambda h, t: 40,
        nvmlDeviceGetPowerUsage=lambda h: 70_000,
        NVML_TEMPERATURE_GPU=0,
        NVMLError=Exception,
    )

    real_get_uuid = nvml.nvmlDeviceGetUUID
    call_count = {"n": 0}

    def _flaky_get_uuid(h):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("卡 0 采集炸了")
        return real_get_uuid(h)

    nvml.nvmlDeviceGetUUID = _flaky_get_uuid
    monkeypatch.setattr(collector_mod, "pynvml", nvml)
    monkeypatch.setattr(collector_mod, "_PYNVML_AVAILABLE", True)
    collector = _make_collector()

    payload = collector.collect()
    devices = payload["status"]["gpu_devices"]
    assert len(devices) == 1  # 卡 0 跳过，仅剩卡 1
    assert devices[0]["index"] == 1


def test_collect_system_fields_present(monkeypatch):
    """系统采集：cpu/memory/swap/filesystem/os/kernel/uptime 结构存在。"""
    monkeypatch.setattr(collector_mod, "pynvml", None)
    monkeypatch.setattr(collector_mod, "_PYNVML_AVAILABLE", False)
    collector = _make_collector()

    status = collector.collect()["status"]
    assert "total" in status["cpu"] and "utilization_rate" in status["cpu"]
    assert "total" in status["memory"] and "used" in status["memory"]
    assert "total" in status["swap"] and "used" in status["swap"]
    assert isinstance(status["filesystem"], list)
    assert isinstance(status["os"], dict)
    assert isinstance(status["kernel"], dict)
    assert isinstance(status["uptime"], dict)


def test_collect_system_reserved_from_config(monkeypatch):
    """system_reserved 取自配置的 RAM/VRAM 预留值。"""
    monkeypatch.setattr(collector_mod, "pynvml", None)
    monkeypatch.setattr(collector_mod, "_PYNVML_AVAILABLE", False)
    collector = _make_collector(
        WORKER_SYSTEM_RESERVED_RAM=8 * 1024**3,
        WORKER_SYSTEM_RESERVED_VRAM=2 * 1024**3,
    )

    reserved = collector.collect()["system_reserved"]
    assert reserved == {"ram": 8 * 1024**3, "vram": 2 * 1024**3}
