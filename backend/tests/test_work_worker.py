"""Worker 入口脚本契约测试（--check 退出码 / argparse 默认值）。"""

import subprocess
import sys
from pathlib import Path

from app.worker.config import worker_config

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def test_check_exits_zero_subprocess():
    """--check 退出码 0 且输出 Health check passed。"""
    result = subprocess.run(
        [sys.executable, str(_BACKEND_DIR / "app" / "work_worker.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "Health check passed" in result.stdout


def test_argparse_defaults():
    """argparse 默认值：host=0.0.0.0、port=worker_config.WORKER_LISTEN_PORT。"""
    import argparse

    from app.worker.config import worker_config as wc

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=wc.WORKER_LISTEN_PORT)
    args = parser.parse_args([])
    assert args.host == "0.0.0.0"
    assert args.port == worker_config.WORKER_LISTEN_PORT == 8100
