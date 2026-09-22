"""
应用配置
"""

from pathlib import Path
from typing import Literal

from pydantic import Field

from app.base.base_config import BASE_PATH, AppBaseConfig

_DEFAULT_APPLICATION_NAME = "vllm_manager"
_DEFAULT_SECRET_KEY = "lzS6PigzAZeW1wgHmuGuMt2WWtEsI0YghkIf3al0lQlbTE5PHqbzMUZZ"


class AppConfig(AppBaseConfig):
    """
    Configuration settings for application deployment
    """

    APPLICATION_NAME: str = Field(
        description="Name of the application, used for identification and logging purposes",
        default=_DEFAULT_APPLICATION_NAME,
    )

    DEBUG: bool = Field(
        description="Enable debug mode for additional logging and development features",
        default=False,
    )

    DEPLOY_ENV: Literal["PRODUCTION", "DEVELOPMENT"] = Field(
        description="Deployment environment (e.g., 'PRODUCTION', 'DEVELOPMENT'), default to DEVELOPMENT",
        default="DEVELOPMENT",
    )

    BASE_URL_PATH: str = Field(
        description="root path", default=f"/{_DEFAULT_APPLICATION_NAME}"
    )

    PUBLIC_ACCESS_DOMAIN: str | None = Field(
        default="http://127.0.0.1:8000",
        description="域名-对外访问用 (e.g. 'https://example.com')",
    )

    SECRET_KEY: str = Field(
        description="Secret key for secure session cookie signing."
        "Make sure you are changing this key for your deployment with a strong key."
        "Generate a strong key using `openssl rand -base64 42` or set via the `SECRET_KEY` environment variable.",
        default=_DEFAULT_SECRET_KEY,
    )

    SESSION_MAX_AGE: int = Field(
        description="Session cookie max age in seconds. Default is 2 days (172800 seconds).",
        default=2 * 24 * 60 * 60,  # 2 days
    )

    SESSION_HTTPS_ONLY: bool = Field(
        default=False,
        description="Force Secure on the session cookie. Production always enables it.",
    )

    TEMP_PATH: Path = Field(
        description="临时文件目录", default=BASE_PATH.joinpath("temp")
    )

    # 前端静态文件配置
    FRONTEND_DIST_DIR: Path = Field(
        description="前端构建产物目录",
        default=BASE_PATH.parent.joinpath("frontend/dist"),
    )

    DISABLED_EXTENSIONS: list[str] = Field(
        description="List of extensions to disable. Supported values: 'db', 'redis', 'saq' (logging 不可禁用)",
        default=[],
    )

    # Node 域配置（Manager 模块）
    NODE_HEARTBEAT_INTERVAL: int = Field(
        description="建议心跳间隔（秒），注册时下发给 agent", default=10
    )
    NODE_HEARTBEAT_GRACE_PERIOD: int = Field(
        description="心跳超时阈值（秒），超过则节点置 offline", default=30
    )
    NODE_PROBE_INTERVAL: int = Field(
        description="主动探测周期（秒）", default=15
    )
    NODE_PROBE_TIMEOUT: int = Field(
        description="主动探测 /healthz 超时（秒）", default=3
    )
    AGENT_DEFAULT_PORT: int = Field(
        description="agent 默认端口", default=8100
    )


app_config = AppConfig()

if app_config.DEPLOY_ENV == "PRODUCTION" and app_config.SECRET_KEY == _DEFAULT_SECRET_KEY:
    # Guard against deploying with the plaintext default committed to this repo:
    # anyone who reads config.py can forge session cookies for any user if a
    # production deployment silently inherits this default.
    raise RuntimeError(
        "SECRET_KEY must not use the default value in production. "
        "Generate a strong key using `openssl rand -base64 42` and set it via the SECRET_KEY environment variable."
    )
