# backend/app/server/

## Responsibility（职责）
server 管理端业务域（FastAPI 服务侧业务逻辑所在），承载 GPU 节点登记/存活管理、vLLM 实例生命周期与显存分配决策。PG 是唯一权威账本（worker 状态全内存），worker 业务指令只从 instance 域经 `utils/request_to_worker` 发出。遵循 AGENTS.md 两级结构：业务域 → 子域包（node/instance/allocator）→ 职责文件。

子域依赖单向：`node ← instance ← allocator`（instance 只读引用 node 的 model/schema，禁止跨域 service 互调；allocator 纯函数零 DB IO）。

## Design（设计模式与抽象）
- 子域划分：`node`（基础设施存在）/ `instance`（工作负载生命周期）/ `allocator`（分配决策）
- 各子域统一模式：`model.py`（ORM）/ `schema.py`（Pydantic）/ `service.py`（继承 `BaseCrudService[T]`，默认不提交事务）/ `api.py`（路由）
- 跨域约束：禁止跨域 service 互调；唯一反向只读例外是 node 删除守卫消费 `VllmInstanceService.assert_node_deletable`
- 后台任务（lifespan 启动，异常不退出下一轮重试）：`probe_loop`（node 主动探测；失联联动由组合根注入 `on_node_lost` 回调 → instance 域 `reconcile_lost_nodes`，node 域零跨域 import）
- 安全姿态（过渡期）：worker 端点（status/report）走节点 Bearer token；管理端点暂开放，注释明示待 web 鉴权里程碑收敛，部署限内网

## Flow（数据与控制流）
- 对账闭环：用户操作（创建/启停）→ API → Service → 写 `target_state` + `request_to_worker` 转发 → worker 执行 → `/instances/report` 上报 → apply_report 收敛 state → 达成清回 `target_state=none`
- 节点存活双通道：worker 状态上报（被动，刷新 heartbeat_time）+ server 主动 `/healthz` 探测（prober），`compute_state` 统一派生 state：

```
worker ──register/status───────────▶ server(node: state 派生)
worker ──/instances/report─────────▶ server(instance: apply_report 对账)
server ──request_to_worker─────────▶ worker(healthz/start/stop/models/weight)
```

- 节点状态写独占：node.state 归 prober/状态上报派生，instance 域只读（expunge 防落库）

## Integration（集成点）
- 路由：`app/main/routers.py` 聚合 `node_router`/`instance_router`，挂
  `/vllm_manager/api/v1`（BASE_URL_PATH）
- 后台任务：`app/main/lifespan.py` 启动 probe_loop（`DISABLED_EXTENSIONS` 含 db 时不启动），`on_node_lost` 回调注入 instance 域失联联动
- worker 契约：`docs/worker-contract.md`（server↔worker 端点/字段/鉴权/对账规则冻结规格）
- 基础设施：`app/extensions/database`（get_session 依赖 / get_session_context 后台会话）、`app/base/`（BaseCrudService/响应契约/枚举基类）、`app/utils/request_to_worker`（统一转发）、`app/config`（app_config frozen）
- 边界：server 域不 import `app/worker` 包（worker 是独立进程，只经 REST 契约交互）；异常语义经 `app.exception` 分层异常渲染
- 测试：`backend/tests/` 契约测试仿照样板写法

## 子 map 索引
- `node/codemap.md`：GPU 节点域（登记幂等/心跳/状态上报/主动探测/worker 鉴权）
- `instance/codemap.md`：vLLM 实例域（生命周期字段/状态机纯函数/对账/失联联动）
- `instance/service/codemap.md`：实例服务层（create 编排/启停/对账/守卫）
- `allocator/codemap.md`：显存分配决策（first-fit 纯函数，零 DB/网络；GPU 路径 + 仅显式 `accelerator=cpu` 的 CPU 路径，其余 fail-closed 走 GPU 拒绝）
