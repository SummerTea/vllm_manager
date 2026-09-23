"""worker→server 通信客户端（注册/状态上报/实例对账）。

对齐 docs/worker-contract.md §1：路径带 /vllm_manager/api/v1 前缀，
响应统一 BaseResponse{code, message, data}，业务数据在 data 字段。
注册失败重试耗尽抛异常（启动失败退出）；状态/对账失败仅 warning 不抛（长驻容错）。
"""

import logging
import socket

import httpx
import psutil
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.worker.config import WorkerConfig

logger = logging.getLogger(__name__)

_BASE_API_PATH = "/vllm_manager/api/v1"


class NodeRegisterResponse(BaseModel):
    """节点注册响应（与 server node/schema.py NodeRegisterResponse 对齐）。"""

    node_id: str = Field(description="节点 ID")
    token: str = Field(description="Bearer 鉴权令牌")
    worker_port: int = Field(description="worker 端口")


def _get_first_non_loopback_ip() -> str:
    """取第一非回环 IPv4 地址；失败回退 socket.gethostbyname(hostname)。"""
    try:
        for _, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    return addr.address
    except Exception:  # noqa: BLE001 - 采集失败走兜底
        pass
    return socket.gethostbyname(socket.gethostname())


class ServerClient:
    """worker→server 通信客户端（httpx.AsyncClient 连接复用）。"""

    def __init__(self, config: WorkerConfig):
        self._config = config
        self._http = httpx.AsyncClient(timeout=15.0)

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_fixed(3),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    async def register(self) -> NodeRegisterResponse:
        """向 server 注册（仅网络/超时重试 10 次 × 3s；4xx/5xx 业务错误立即抛）。

        注意：raise_for_status() 抛出的 HTTPStatusError 不属于 TransportError，
        自然不重试——4xx（参数/鉴权错误）与 5xx（server 内部错误）直接浮出，
        不浪费 30s 重试窗口。
        """
        hostname = socket.gethostname()
        payload = {
            "machine_id": self._config.WORKER_MACHINE_ID or hostname,
            "hostname": hostname,
            "ip": _get_first_non_loopback_ip(),
            "advertise_address": self._config.WORKER_ADVERTISE_ADDRESS,
            "worker_port": self._config.WORKER_LISTEN_PORT,
        }
        resp = await self._http.post(
            f"{self._config.SERVER_URL}{_BASE_API_PATH}/nodes/register", json=payload
        )
        resp.raise_for_status()
        return NodeRegisterResponse.model_validate(resp.json()["data"])

    async def report_status(self, node_id: str, token: str, payload: dict) -> None:
        """上报节点状态（失败仅 warning，不抛）。"""
        try:
            await self._http.post(
                f"{self._config.SERVER_URL}{_BASE_API_PATH}/nodes/{node_id}/status",
                headers=self._headers(token),
                json=payload,
            )
        except Exception as e:  # noqa: BLE001 - 上报失败不应中断 worker
            logger.warning("节点状态上报失败: %s", e)

    async def report_instances(self, node_id: str, token: str, items: list[dict]) -> None:
        """上报实例状态对账（Phase 2 使用；失败仅 warning，不抛）。"""
        try:
            await self._http.post(
                f"{self._config.SERVER_URL}{_BASE_API_PATH}/instances/report",
                headers=self._headers(token),
                json={"items": items},
            )
        except Exception as e:  # noqa: BLE001 - 上报失败不应中断 worker
            logger.warning("实例对账上报失败: %s", e)

    async def aclose(self) -> None:
        """关闭底层 http client。"""
        await self._http.aclose()
