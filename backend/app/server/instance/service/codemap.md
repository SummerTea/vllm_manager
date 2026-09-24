# backend/app/server/instance/service/

## Responsibility（职责）
instance 子域服务层（service 目录化）：`__init__.py` 聚合导出 `VllmInstanceService`
（import 面不变），`creation.py` 承载 create 请求内编排（候选→权重→advisory lock
串行→allocator 决策→建记录→转发），`instance.py` 承载生命周期服务（启停/删除/
对账/守卫/失联联动/级联删除）。这是 worker 指令的唯一发起点。

## Design（设计模式与抽象）
- `__init__.py`：聚合导出 `from app.server.instance.service import VllmInstanceService`
- `creation.py`：
  - `NodeReadOnlyCrud(BaseCrudService[Node])`：node 只读查询（instance 域禁止 import
    node service，泛型子类自动绑定 model）
  - `VllmInstanceReadOnlyCrud(BaseCrudService[VllmInstance])`：与 VllmInstanceService
    相互解耦，防循环依赖
  - `DEFAULT_VLLM_RUN_TEMPLATE`：server 内置 docker run 启动模板常量，**必须与
    worker 侧 `app/worker/process_utils.py` 逐字符一致**（防漂移注记
    worker-contract §2.2）；create 时固化快照到 `template` 列下发，worker 渲染
    {port}/{model_path}/{gpu_indexes} 等；镜像 ENTRYPOINT 承担 `vllm serve`，
    {net_args}/{gpus_args} 为结构性网络/GPU 差异片段由 worker 按 gpu_indexes 派生
  - `_get_create_lock`/`_create_locks`：按事件循环懒建的 asyncio.Lock（模块级锁
    会绑定创建时 loop，pytest 跨 loop 报错），串行化「聚合→first_fit→建记录」段
  - `_build_start_payload(inst)`：start 指令请求体（创建与手动启动复用，含
    spec/gpu_indexes/vram_claim/template）
  - `create_vllm_instance(session, data)`：创建编排入口（见 Flow）
- `instance.py`：`VllmInstanceService(BaseCrudService[VllmInstance])`——create（转调
  编排）/ start / stop / delete / apply_report / assert_node_deletable /
  reconcile_node_loss / reconcile_lost_nodes / delete_stopped_by_node
  - stop 悬挂收敛常量：`_MAX_STOP_REDISPATCH=3`（超限置人工介入告警，绝不自动收敛
    stopped）、`_REDISPATCH_TIMEOUT=5`（后台重下发短超时，不长时间占用事件循环）
  - `_pending_redispatch_tasks`：fire-and-forget 后台 stop 重下发任务集合（模块级
    持引用防 GC 回收，done 回调 discard；任务只依赖调度时固化的字段快照，不触碰
    ORM/session——调度后 session 可能已随端点返回关闭）
- 事务语义：所有方法**不提交事务**，路由层 `get_session` 统一 commit/rollback；
  转发失败抛异常 → 整事务回滚不落库（用户可重试）

## Flow（数据与控制流）
- **create 编排（creation.py，8 步请求内同步）**：
  1. 候选节点 = is_active 且 compute_state 重派生 ready（随即 expunge 脱离会话，
     防派生状态落库；node.state 归 prober 独占写）
  2. 指定 node_id 须在候选中，否则 OperationNotAllowedException
  3. 权重：请求覆盖 > worker 广播（asyncio.gather 并发向全部候选查 `models/weight`
     取首个成功；全败抛 ExternalServiceException）；仅当请求既未给 weight 也未给
     vram_claim 时才走广播
  4. 需求取值：vram_claim 未指定则 `estimate_vram_claim(weight, task)`；gmu 未指定
     用 INSTANCE_DEFAULT_GMU
  5-7. 聚合→决策→建记录整体进 `_get_create_lock`：PG 额外执行事务级
     `pg_advisory_xact_lock(hashtext("vllm_create_alloc"))`（随事务提交/回滚自动
     释放，覆盖「锁内写、锁外提交」的 READ COMMITTED 可见性窗口；sqlite 测试走
     asyncio.Lock 兜底）；候选/权重/转发等只读或网络步骤留在锁外
  5. 聚合 WorkerResource：未停止实例的 allocated_vram 合并记账 +
     system_reserved_vram + gpu_devices（status 载荷）
  6. `AllocatorService.first_fit` 决策；**指定 node_id 时先按 node_id 过滤 workers
     防 fallthrough**（防指定 GPU 节点不满足时静默落到 CPU 等其他节点）；
     AllocationRejected → OperationNotAllowedException
  7. 建 VllmInstance（pending + target=starting，template=DEFAULT_VLLM_RUN_TEMPLATE
     固化快照）并 flush 生成 id
  8. 转发 `instances/{id}/start`（HttpClientException 透传；其余包装
     ExternalServiceException，**不写 error 状态**——整事务回滚无残留）
- **start**：仅 stopped/error 可启动 → target=starting + target_retry_count 重置 0
  → 转发 start（节点不存在 / 转发失败 → ExternalServiceException）
- **stop**：仅 running/starting/unreachable/**error** 可停止（C1：error 可回收释放
  显存占账；delete 不改仍保持阻断语义）→ target=stopping + target_retry_count 重置 0
  → 转发 stop（失败时 target 写入随事务回滚，可重试）
- **delete**：非 stopped 先尽力转发 stop，**失败抛 ExternalServiceException 阻断
  删除**（节点不存在则跳过转发记 warning）→ 物理删除
- **apply_report**：对账编排——上报条目逐一过 base.apply_report 纯规则（M2 守卫 /
  B1 target 保留 / 非 None 字段更新 / state_message 转换清空）；target=stopping 但
  上报仍非 stopped → 归入重下发记账队列；随后处理消失实例（target=stopping →
  stopped + 清 target + 清 state_message/target_retry_count，其余不动）；最后对
  重下发队列记账（target_retry_count 限次递增 / 写 state_message，超
  `_MAX_STOP_REDISPATCH` 置「停止指令多次未生效，需人工介入」）并
  `_schedule_stop_redispatch` 派发 fire-and-forget 后台重下发（/report 不阻塞、DB
  事务内不产生网络 IO，转发失败仅 warning 留待下轮对账）；返回处理条数
- **assert_node_deletable**：节点存在非 stopped 实例 → ConflictException(409)
- **reconcile_node_loss**：节点失联 → 该节点 running 实例置 unreachable +
  state_message="节点失联"；**reconcile_lost_nodes(nodes)** 批量逐节点调用（内部已
  flush），由 prober `on_node_lost` 回调驱动（lifespan 组合根装配）
- **delete_stopped_by_node**：级联删除节点下已 stopped 实例（stopped 终态不转发
  stop，直接物理删），供 node 域删除编排清理残留防孤儿占账

## Integration（集成点）
- 对外暴露：`VllmInstanceService` 被 `instance/api.py`（全部端点）与 `node/api.py`
  （delete 编排：assert_node_deletable 守卫 + delete_stopped_by_node 级联）消费
- 依赖方向：只读 node model（NodeReadOnlyCrud）；allocator 纯函数（输入自行聚合后
  调 `AllocatorService.first_fit`）
- 转发：`request_to_worker`（`instances/{id}/start|stop`、`models/weight`，端点规格
  见 docs/worker-contract.md §2.2–2.4）
- 异常映射：转发非 2xx → HttpClientException（透出 url/status_code）；传输错误 →
  ExternalServiceException（service_name="worker"）
