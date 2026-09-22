"""server→worker 统一转发工具。

供 server 端业务域（如 instance 子域）向 GPU 机器上的 worker 管控节点
转发启停等指令，统一承担三件事：
- 按 node 字段构建 worker 地址（规则与 prober._probe_node 一致：
  advertise_address 含 ":" 原样使用、纯 host 补 :worker_port、未配置时 ip:worker_port）
- 注入 node token（Authorization: Bearer）
- 应用统一超时（INSTANCE_REQUEST_TIMEOUT，可显式覆盖）

异常语义（httpx → 分层业务异常）：
- 非 2xx：raise_for_status 抛出的 httpx.HTTPStatusError → HttpClientException
  （携带 url 与 status_code，供 API 层透出）
- 网络/超时等传输错误（httpx.HTTPError 且非 HTTPStatusError，如 ConnectError/
  TimeoutException/InvalidURL）→ ExternalServiceException
  （service_name="worker"，details 携带 url）
成功时返回已通过 raise_for_status 的 httpx.Response。
"""

import httpx

from app.config import app_config
from app.exception import ExternalServiceException, HttpClientException


def build_worker_url(node, path: str) -> str:
    """构建 worker 指令 URL（http://host:port/path）。

    Args:
        node: Node 模型实例或鸭子类型（需含 advertise_address/ip/worker_port 字段）。
        path: 指令路径，如 "instances/xxx/start"（前导斜杠自动去除）。

    Returns:
        形如 "http://10.0.0.1:8100/instances/xxx/start" 的 URL。
    """
    if node.advertise_address:
        hostport = (
            node.advertise_address
            if ":" in node.advertise_address
            else f"{node.advertise_address}:{node.worker_port}"
        )
    else:
        hostport = f"{node.ip}:{node.worker_port}"
    return f"http://{hostport}/{path.lstrip('/')}"


async def request_to_worker(
    node,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """向 worker 转发指令请求。

    Args:
        node: Node 模型实例或鸭子类型（需含 advertise_address/ip/worker_port/token 字段）。
        method: HTTP 方法（小写，如 "post"），透传给 httpx.AsyncClient.request。
        path: 指令路径，如 "instances/xxx/start"。
        json: JSON 请求体。
        params: 查询参数。
        timeout: 超时（秒），None 时使用 app_config.INSTANCE_REQUEST_TIMEOUT。

    Returns:
        2xx 响应（已通过 raise_for_status）。

    Raises:
        HttpClientException: worker 返回非 2xx 状态码。
        ExternalServiceException: 网络/超时等传输错误（service_name="worker"）。
    """
    url = build_worker_url(node, path)
    headers = {"Authorization": f"Bearer {node.token}"}
    if timeout is None:
        timeout = app_config.INSTANCE_REQUEST_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, url, headers=headers, json=json, params=params
            )
            resp.raise_for_status()
            return resp
    except httpx.HTTPStatusError as e:
        raise HttpClientException(
            f"转发 worker 请求失败: {method} {url} status={e.response.status_code}",
            url=url,
            status_code=e.response.status_code,
        ) from e
    except httpx.HTTPError as e:
        raise ExternalServiceException(
            f"转发 worker 请求失败: {url}",
            service_name="worker",
            details={"url": url},
        ) from e
