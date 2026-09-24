# gpustack/websocket_proxy/

## Responsibility
基于 WebSocket 的 CONNECT 式隧道代理：server 与 agent（client）之间建立长连接二进制协议隧道，把 HTTP/HTTPS 代理请求转发到 agent 私有网络内的目标（TCP/unix socket），并支持多 server 联邦（federation）共享 client 注册信息。用于 GPUStack worker 的 tunnel 代理模式，vllm_manager 不涉及。

## Design
- 二进制消息协议（`message.py`）：`BaseMessage.pack/parse` + `MessageRegistry` 装饰器注册；消息类型含 `ConnectRequestMessage`/`ConnectResponseMessage`/`DataMessage`（支持 gzip 压缩）/`DisconnectMessage`/`HeartbeatMessage`/`ClientUpdateMessage`（server 间同步 client）；session_id 为 UUID，大端 `struct` 编码，协议版本 `PROTOCOL_VERSION=0x01`。`ServerInfo`/`RegisteredClientInfo` 通过 WS 握手 header（`x-server-id`/`x-client-id`/`x-cidrs` 等）完成注册。
- 连接管理三态抽象：
  - `ConnectionManager`（server 侧）：对目标发起 CONNECT_REQUEST 并等 `TunnelConnection.connect_result`（30s 超时）；
  - `ClientConnectionManager`（client 侧）：收到 CONNECT_REQUEST 后直连目标、回 CONNECT_RESPONSE，再用 `tunnel()` 双向转发；
  - `RemoteConnectionManager`：跨联邦 peer 时通过对方 HTTP proxy 发 `CONNECT target HTTP/1.1` 中转。
- `MessageServerHandler`（`message_server.py`）：维护 `client_registry`、`connection_managers`、`_cidr_registry`、peers/serving_peers 两套联邦连接；`get_connection_manager_by_ip_in_cidr` 用 `CIDRRegistry`（Patricia Trie，基于 py-radix）做最长前缀匹配选路；`callback_on_connect/disconnect` 钩子 + generation 去重防陈旧回调。`main.py` 提供 server/client CLI 与联邦管理 REST API。
- `proxy_server.py`：`HTTPSProxyServer`（asyncio 手写 HTTP/1.1 代理），支持 GET 等直连代理与 CONNECT 隧道；按 RFC 7230 过滤 hop-by-hop 头，`wait_for_complete_response` 按 Content-Length 转发响应；支持可选 `HeaderAuthenticator`/`HeaderRouter` 钩子与 `/metrics` 免鉴权。
- `authenticator.py`：`Authenticator` 抽象 + `HMACAuthenticator`（HMAC-SHA256 签名 header）与 `NoOpAuthenticator`，`create_authenticator(key)` 工厂。

## Flow
HTTP 请求 → HTTPSProxyServer → 按目标 IP 查 CIDRRegistry 定位 ConnectionManager → connect(target_url)（本地走 WS CONNECT_REQUEST，远端走 RemoteConnectionManager）→ 隧道建立 → `tunnel()` 双向 relay；client 侧 `MessageClient.run()` 带指数退避重连，CIDR 变更时主动断开触发重连。server 之间通过 `ClientUpdateMessage` 广播 client 增删，联邦内任意 server 均可路由到全部 client。

## Integration
- 消费方：`server/app.py`（`worker_websocket_connect_callback` 接入）、`routes/worker/proxy.py`（tunnel 模式路由）、worker 端 tunnel 客户端。
- 依赖：`websockets`、`starlette`/`fastapi`、`py-radix`；独立子系统，可脱离 GPUStack 单独运行（`python -m gpustack.websocket_proxy.main`）。
