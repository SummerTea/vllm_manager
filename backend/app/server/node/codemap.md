# backend/app/server/node/

## Responsibility（职责）
GPU 节点域（基础设施存在）：节点登记（幂等）、状态上报（承担存活语义）、主动 /healthz 探测、
节点 CRUD 管理。产出节点的权威存活信息（state/status/gpu_devices），供 instance 域
作分配候选与指令转发目标。

Node 表是 GPU 机器的注册账本（token/address/端口），worker 跨机回连依赖其
advertise_address/ip/worker_port 字段。

## Design（设计模式与抽象）
- `model.py`：`Node`（表 `vllm_manager_node`，唯一索引 uix_node_machine_id/uix_node_token）；
  `compute_state(grace_period)` 就地派生 state——**派生规则唯一实现**，
  状态上报/service/prober 复用：

```
无心跳 → pending；心跳超时(>grace_period) → offline；
unreachable 标志 → unreachable；否则 → ready
```

- `service.py`：`NodeService(BaseCrudService[Node])`——register（幂等：machine_id 优先、
  hostname 回退；复用旧 token 不轮换）/ update_status（落
  system_reserved+status，刷新 heartbeat_time）/ refresh_state
- `prober.py`：`probe_loop` 后台循环——心跳权威信号先行（pending/offline 不探测）；
  探测前后各一次 compute_state（探测改变 unreachable 后需重派生）；httpx 超时归
  unreachable，InvalidURL 等配置错误浮出
- `dependencies.py`：`get_current_node`——Authorization Bearer + `secrets.compare_digest`
  恒时比对；token 与 node_id 不匹配抛 403
- `api.py`：`node_router`——register 公开；status worker 鉴权；
  list/get/patch/delete **过渡期开放**（注释明示待 web 鉴权收敛，部署限内网）
- `enum.py`：`NodeStateEnum`（pending/ready/offline/unreachable，LabeledStrEnum）
- `schema.py`：NodeRegisterRequest/Response、NodeStatusReportRequest、NodeUpdateRequest、
  NodeOut、GPUDeviceStatus 等；`NodeStatus.accelerator`（worker 显式声明 gpu|cpu，
  供 allocator 判定 CPU 分配，缺省/未知 fail-closed 按 GPU）

## Flow（数据与控制流）
- 注册：POST /nodes/register（幂等；并发重复触发 uix_node_machine_id → rollback 后
  按原幂等键重查）
- 存活：状态上报（POST /{id}/status 落 status 载荷、刷新 heartbeat_time，GPU 显存单位
  Bytes）→ prober 主动探测双保险（/healthz 鉴权豁免，探测仍带 token 兼容）
- 删除：DELETE /{id} 先经 `VllmInstanceService.assert_node_deletable`（存在活跃实例
  409 拒绝）再物理删除
- 探测契约要点：单进程（`uvicorn --workers > 1` 不做并发探测去重）；节点 >5 时顺序
  探测最坏 N×3s 可能超周期，二期改 gather+semaphore

## Integration（集成点）
- 被 instance 域只读引用：`instance/service/creation.py` 用 `NodeReadOnlyCrud` 只读
  node model（**不 import node service**）
- 反向只读例外：`api.py:delete_node` 导入 instance 域
  `VllmInstanceService.assert_node_deletable`（唯一跨域 service 消费）
- 后台任务：lifespan 启动 `probe_loop`（依赖 DB）；node.state 由 prober/状态上报独占派生
  写入，instance 域只读不落；失联联动经 prober 的 `on_node_lost` 回调批量交给
  instance 域 `reconcile_lost_nodes`（lifespan 组合根装配，node 域零跨域 import）
- worker 契约：`docs/worker-contract.md` §1.1–1.2（worker→server register/status
  规格）、§2.1（server→worker /healthz）；配置项 §5（NODE_HEARTBEAT_GRACE_PERIOD、
  NODE_PROBE_INTERVAL/TIMEOUT）
- 转发地址构建规则（advertise_address 含":"原样 / 纯 host 补 worker_port / 无则
  ip:port）与 `request_to_worker.build_worker_url` 一致
