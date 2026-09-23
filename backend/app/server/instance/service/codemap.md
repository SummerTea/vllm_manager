# backend/app/server/instance/service/

## Responsibility（职责）
instance 子域服务层（service 目录化）：`__init__.py` 聚合导出 `VllmInstanceService`
（import 面不变），`creation.py` 承载 create 请求内编排，`instance.py` 承载生命周期
服务（启停/删除/对账/守卫/失联联动）。这是 worker 指令的唯一发起点。

## Design（设计模式与抽象）
- `__init__.py`：聚合导出 `from app.server.instance.service import VllmInstanceService`
- `creation.py`：
  - `NodeReadOnlyCrud(BaseCrudService[Node])`：node 只读查询（instance 域禁止 import
    node service，泛型子类自动绑定 model）
  - `VllmInstanceReadOnlyCrud(BaseCrudService[VllmInstance])`：与 VllmInstanceService
    相互解耦，防循环依赖
  - `_build_start_payload(inst)`：start 指令请求体（创建与手动启动复用，含
    spec/gpu_indexes/vram_claim）
  - `create_vllm_instance(session, data)`：创建编排入口（见 Flow）
- `instance.py`：`VllmInstanceService(BaseCrudService[VllmInstance])`——create（转调
  编排）/ start / stop / delete / apply_report / assert_node_deletable /
  reconcile_node_loss
- 事务语义：所有方法**不提交事务**，路由层 `get_session` 统一 commit/rollback；
  转发失败抛异常 → 整事务回滚不落库（用户可重试）

## Flow（数据与控制流）
- **create 编排（creation.py，8 步请求内同步）**：
  1. 候选节点 = is_active 且 compute_state 重派生 ready（随即 expunge 脱离会话，
     防派生状态落库）
  2. 指定 node_id 须在候选中，否则 OperationNotAllowedException
  3. 权重：请求覆盖 > worker 广播（asyncio.gather 并发向全部候选查 `models/weight`
     取首个成功；全败抛 ExternalServiceException）
  4. 需求取值：vram_claim 未指定则 `estimate_vram_claim(weight, task)`；gmu 未指定
     用 INSTANCE_DEFAULT_GMU
  5. 聚合 WorkerResource：未停止实例的 allocated_vram 合并记账 +
     system_reserved_vram + gpu_devices（status 载荷）
  6. `AllocatorService.first_fit` 决策；AllocationRejected → OperationNotAllowedException
  7. 建 VllmInstance（pending + target=starting）并 flush 生成 id
  8. 转发 `instances/{id}/start`；失败包装 ExternalServiceException（**不写 error
     状态**——整事务回滚无残留）
- **start**：仅 stopped/error 可启动 → target=starting → 转发 start
- **stop**：仅 running/starting/unreachable 可停止 → target=stopping → 转发 stop
  （失败时 target 写入随事务回滚，可重试）
- **delete**：非 stopped 先尽力转发 stop（失败仅记 warning 不阻断）→ 物理删除
- **apply_report**：对账编排——上报条目逐一过 base.apply_report 纯规则（M2 守卫/
  B1 target 保留/非 None 字段更新），随后处理消失实例（target=stopping → stopped+
  清 target，其余不动）；返回处理条数
- **assert_node_deletable**：节点存在非 stopped 实例 → ConflictException(409)
- **reconcile_node_loss**：节点失联 → 该节点 running 实例置 unreachable +
  state_message="节点失联"

## Integration（集成点）
- 对外暴露：`VllmInstanceService` 被 `instance/api.py`（全部端点）与 `node/api.py`
  （delete 守卫）消费
- 依赖方向：只读 node model（NodeReadOnlyCrud）；allocator 纯函数（输入自行聚合后
  调 `AllocatorService.first_fit`）
- 转发：`request_to_worker`（`instances/{id}/start|stop`、`models/weight`，端点规格
  见 docs/worker-contract.md §2.2–2.4）
- 异常映射：转发非 2xx → HttpClientException（透出 url/status_code）；传输错误 →
  ExternalServiceException（service_name="worker"）
