"""Worker 配置默认值契约测试。"""

import pytest
from pydantic import ValidationError

from app.worker.config import WorkerConfig


def test_worker_config_defaults():
    cfg = WorkerConfig()
    assert cfg.SERVER_URL == "http://127.0.0.1:8000"
    assert cfg.WORKER_LISTEN_PORT == 8100
    assert cfg.WORKER_MACHINE_ID is None
    assert cfg.WORKER_ADVERTISE_ADDRESS is None
    assert cfg.WORKER_STATUS_INTERVAL == 15
    assert cfg.WORKER_REPORT_INTERVAL == 5
    assert cfg.INSTANCE_DEFAULT_GMU == 0.9
    assert cfg.WORKER_SYSTEM_RESERVED_RAM == 0
    assert cfg.WORKER_SYSTEM_RESERVED_VRAM == 0
    assert cfg.WORKER_STARTUP_FAIL_THRESHOLD == 2
    assert cfg.WORKER_STARTUP_TIMEOUT_SECONDS == 30
    assert cfg.WORKER_ACCELERATOR == "gpu"


def test_worker_config_accelerator_cpu_allowed():
    cfg = WorkerConfig(WORKER_ACCELERATOR="cpu")
    assert cfg.WORKER_ACCELERATOR == "cpu"


def test_worker_config_accelerator_invalid_value():
    """非法加速器类型（如 tpu）→ ValidationError（GPU 主战场 fail-closed）。"""
    with pytest.raises(ValidationError):
        WorkerConfig(WORKER_ACCELERATOR="tpu")  # type: ignore[arg-type] - 故意传非法值测校验


def test_worker_config_paths_are_absolute():
    cfg = WorkerConfig()
    assert cfg.WORKER_MODEL_ROOT.is_absolute()
    assert cfg.WORKER_LOG_DIR.is_absolute()
    assert cfg.WORKER_CACHE_DIR.is_absolute()
