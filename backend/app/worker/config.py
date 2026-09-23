"""Worker 域配置（继承 AppBaseConfig，读同一 .env；frozen 不可写）。

worker 不连 PG、不初始化任何扩展（database/redis/saq 均不加载），因此
不读取/不依赖 server 的 DISABLED_EXTENSIONS（pydantic-settings 对其 list[str]
字段要求 JSON 数组格式，裸逗号分隔值会 SettingsError）。本配置独立读 WORKER_*，
模块级 `worker_config = WorkerConfig()` 单例，import 即读环境，运行期不可改。
"""

from pathlib import Path
from typing import Literal

from pydantic import Field

from app.base.base_config import BASE_PATH, AppBaseConfig


class WorkerConfig(AppBaseConfig):
    """Worker 配置（server 地址、注册身份、采集周期、预留资源等）。"""

    SERVER_URL: str = Field(
        default="http://127.0.0.1:8000",
        description="server 管理端地址（注册目标）",
    )
    WORKER_LISTEN_PORT: int = Field(
        default=8100, description="worker 监听端口"
    )
    WORKER_MACHINE_ID: str | None = Field(
        default=None, description="机器唯一标识（幂等注册键，None 时用主机名）"
    )
    WORKER_ADVERTISE_ADDRESS: str | None = Field(
        default=None, description="对外可达地址（host:port 或纯 host，供 server 回连）"
    )
    WORKER_MODEL_ROOT: Path = Field(
        default=BASE_PATH / "data" / "models", description="模型根目录"
    )
    WORKER_VLLM_BIN: str = Field(
        default="vllm", description="vLLM 可执行命令（PATH 或绝对路径，容器内命令）"
    )
    WORKER_VLLM_IMAGE: str = Field(
        default="vllm/vllm-openai:latest", description="vLLM 容器镜像"
    )
    WORKER_VLLM_SHM_SIZE_GIB: float = Field(
        default=10.0, description="共享内存 GiB，vLLM 大模型加载需要（--shm-size）"
    )
    WORKER_LOG_DIR: Path = Field(
        default=BASE_PATH / "data" / "logs", description="实例日志目录"
    )
    WORKER_CACHE_DIR: Path = Field(
        default=BASE_PATH / "data" / "cache", description="VLLM_CACHE_ROOT 持久目录"
    )
    WORKER_STATUS_INTERVAL: int = Field(
        default=15, description="节点状态上报周期（秒）"
    )
    WORKER_REPORT_INTERVAL: int = Field(
        default=5, description="实例对账上报周期（秒，Phase 2 使用）"
    )
    INSTANCE_DEFAULT_GMU: float = Field(
        default=0.9, description="实例默认显存利用率 GMU（0-1，缺省补参数）"
    )
    WORKER_SYSTEM_RESERVED_RAM: int = Field(
        default=0, description="系统预留内存（Bytes，上报 system_reserved.ram）"
    )
    WORKER_SYSTEM_RESERVED_VRAM: int = Field(
        default=0, description="系统预留显存（Bytes，上报 system_reserved.vram）"
    )
    WORKER_ACCELERATOR: Literal["gpu", "cpu"] = Field(
        default="gpu",
        description="加速器类型（gpu|cpu）；cpu 为本机 CPU 集成测试显式声明，默认 gpu 保持 GPU 主战场 fail-closed",
    )
    WORKER_STARTUP_FAIL_THRESHOLD: int = Field(
        default=2, description="starting 健康检查连续失败阈值（5s sync 周期 × N ≈ 快速失败）"
    )
    WORKER_STARTUP_TIMEOUT_SECONDS: int = Field(
        default=30, description="starting 总预算秒数（模型加载超长兜底）"
    )


worker_config = WorkerConfig()
