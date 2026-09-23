# backend/app/server/instance/

## Responsibility（职责）
vLLM 实例域（多类型实例域，现阶段 vllm 表）：实例生命周期（创建编排/启停/删除）、worker 上报对账、节点失联联动。worker 业务指令只由此域经 `utils/request_to_worker` 发出。

## Design（设计模式与抽象）
- `base.py`：`InstanceLifecycleMixin`（node_id/state/target_state/port/gpu_indexes/vram_claim/allocated_vram/labels/restart_count/is_active 公共列，多类型实例表共用）+ 状态机纯函数（无 DB/网络）：
  - `estimate_vram_claim`：embedding/rerank → weight×1.2+512MiB；其余 → weight×1.2+2GiB（gpustack 实证口径）
  - `apply_report`：worker 上报合并纯规则（见 Flow）
  - `reconcile_unreachable`：running → unreachable（失联联动）
  - `is_target_achieved`：starting→running/error 达成；stopping→stopped 达成；none 恒达成
- `enum.py`：`InstanceStateEnum`（pending/starting/running/stopped/error/unreachable，**无 stopping**——停止意图由 target_state 承载）、`InstanceTargetStateEnum`（none/starting/stopping，达成后清回）、`InstanceTaskEnum`（auto/llm/embedding/rerank，产品层分类，worker 端映射 vLLM --task）
- `model.py`：`VllmInstance`（表 `vllm_manager_vllm_instance`：model_name/task/model_weight_bytes/gpu_memory_utilization/tensor_parallel_size/args）
- `schema.py`：`VllmInstanceCreateRequest`/`VllmInstanceOut`/`InstanceReportRequest`（items）/`InstanceReportItem`（state 校验即 InstanceStateEnum）
- `api.py`：`instance_router`——`/report`（worker 鉴权对账）**先于 `/{instance_id}` 注册防路径捕获**；create/list/get/start/stop/delete 过渡期开放（注释明示待 web 鉴权收敛）
- `service/`：服务层目录化（聚合导出 + creation 编排 + 生命周期），详见 `service/codemap.md`；
  失联联动能力在 `service/instance.py`——`reconcile_node_loss(node)`（节点失联 → running→
  unreachable）/ `reconcile_lost_nodes(nodes)`（批量），由 prober 的 `on_node_lost`
  回调驱动（lifespan 组合根装配，无独立后台循环）

## Flow（数据与控制流）
- 状态机（server 侧，state 与 target_state 分离）：pending/starting/running/stopped/error/unreachable；用户操作写 target，对账达成后清回 none：

```
create→pending(target=starting)→[worker 上报 running/error]→running/error(target 清回)
start(stopped/error)→target=starting；stop(running/starting/unreachable)→target=stopping
node 失联 → running→unreachable（人工介入）；stop 收敛 → stopped（终态，M2 防复活）
```

- create 编排（请求内同步 8 步，详见 service/codemap.md）：候选节点 → 指定校验 → 权重（请求覆盖 > worker 广播并发取首个成功）→ 需求取值 → 聚合 allocator 输入 → first-fit → 建记录（pending+target=starting）→ 转发 start（失败整事务回滚无残留）
- apply_report 对账规则（base.py 纯函数 + service 编排）：
  - state 取上报值，受 **M2 stopped 防复活守卫**拦截：target=none 且当前 stopped 时不接受非 stopped 上报（防 stop 收敛后在途旧上报复活实例与占账）；有 target 意图不拦截（start 从 stopped 发起依赖此路径）
  - target 仅 `is_target_achieved` 成立时清回 none（**B1 竞态防护**：防 stop/start 在途被仍带旧 state 的上报清掉 target 导致实例不再收敛）
  - state_message/port/restart_count 仅上报非 None 时更新，否则保留
  - 上报消失：target=stopping → 收敛 stopped+清 target；**其余消失不动**（容忍 worker 重启窗口，不能误判已停止）

## Integration（集成点）
- 依赖：只读 node 的 model/schema（`NodeReadOnlyCrud`，不 import node service）
- 被依赖：node 域删除守卫反向消费 `VllmInstanceService.assert_node_deletable`（唯一跨域反向例外）
- 转发：`app/utils/request_to_worker`（统一构建地址+注入 Bearer+超时；非 2xx→HttpClientException、传输错误→ExternalServiceException）
- 会话：路由层 `get_session`（统一 commit/rollback）；后台任务 `get_session_context`
- 挂载：路由 `app/main/routers.py`；失联联动无独立后台任务——probe_loop 回调
  `reconcile_lost_nodes`（lifespan 装配，依赖 DB）
- worker 契约：`docs/worker-contract.md` §1.3（/instances/report 字段与对账规则）、§2.2–2.4（start/stop/models/weight）、§3（worker 侧生命周期与 stopped 终态硬约束）；配置项 §5（INSTANCE_REQUEST_TIMEOUT/WEIGHT_TIMEOUT/DEFAULT_GMU）
