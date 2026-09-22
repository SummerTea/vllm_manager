"""Worker 配置默认值契约测试。"""

from app.worker.config import WorkerConfig


def test_worker_config_defaults():
    cfg = WorkerConfig()
    assert cfg.SERVER_URL == "http://127.0.0.1:8000"
    assert cfg.WORKER_LISTEN_PORT == 8100
    assert cfg.WORKER_MACHINE_ID is None
    assert cfg.WORKER_ADVERTISE_ADDRESS is None
    assert cfg.WORKER_STATUS_INTERVAL == 15
    assert cfg.WORKER_REPORT_INTERVAL == 5
    assert cfg.NODE_HEARTBEAT_INTERVAL == 10
    assert cfg.INSTANCE_DEFAULT_GMU == 0.9
    assert cfg.WORKER_SYSTEM_RESERVED_RAM == 0
    assert cfg.WORKER_SYSTEM_RESERVED_VRAM == 0


def test_worker_config_paths_are_absolute():
    cfg = WorkerConfig()
    assert cfg.WORKER_MODEL_ROOT.is_absolute()
    assert cfg.WORKER_LOG_DIR.is_absolute()
    assert cfg.WORKER_CACHE_DIR.is_absolute()
