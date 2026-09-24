# backend/app/server/allocator/

## Responsibility（职责）
显存分配决策模块（纯函数）：给定候选节点资源快照与显存需求，输出 first-fit 分配
结果或拒绝原因。**零 DB IO、零网络**——输入由调用方（instance 域 create 编排）
聚合好传入，产出纯数据决策（node_id/gpu_indexes/gmu/allocated_vram），不感知
执行与回写。

## Design（设计模式与抽象）
- `service.py`：`AllocatorService.first_fit`（staticmethod 纯函数）——按候选顺序逐
  节点尝试，首个命中即返回；参数校验（GMU∈(0,1]、vram_claim>0、tp≥1）不满足直接
  拒绝；`_try_worker` 按 tp 分流单卡/多卡策略；**CPU 分配门控 = 显式
  `worker.accelerator == "cpu"`**——分配 `gpu_indexes=[]`、`allocated_vram={}`、
  显存不校验、tp>1 拒绝；**fail-closed：GPU / 未声明 / `gpu_devices` 空一律走
  GPU 路径拒绝**（不按空 `gpu_devices` 推断 CPU——GPU 节点采集降级也会上报空列表，
  推断会 fail-open 误分配）
- 单卡（tp=1）`_try_worker_single`：逐卡判定 `available/total ≥ GMU` 且
  `claim ≤ total×GMU`，双条件同真才命中
- 多卡（tp>1）`_try_worker_multi`：按 gpu_type 同型号分组（`gpu_type` None 归
  `<unknown>`），组内仅可用率 `> GMU`（严格大于）的卡入围，按可用显存降序取
  tp 张，`Σ(total×GMU) ≥ claim` 命中（tp>1 必须同型号——vLLM 张量并行硬约束）
- `schema.py`：`GPUResource`（index/memory_total/gpu_type）/ `WorkerResource`（节点
  快照，含 `accelerator`——worker 显式声明 gpu|cpu，None/未知按 GPU fail-closed）/
  `AllocationResult`（node_id/gpu_indexes/gmu/vram_claim/allocated_vram 记账）/
  `AllocationRejected`（reason）
- 返回类型区分：命中返回 `AllocationResult`，失败返回 `AllocationRejected`（非异常，
  由调用方决定如何拒绝并透出）

## Flow（数据与控制流）
- 记账口径（`_available`）：
  `单卡可用 = max(total − Σ已分配(allocated_vram) − 系统预留(system_reserved_vram，整机级每卡都扣), 0)`
  以 allocated_vram 账本为准，**不消费 node.status 的 memory_used 实时值**
- 命中记账：`allocated_vram = {gpu_index: int(total×GMU)}`（per-gpu 占账口径；
  vram_claim 仅作需求校验阈值，非记账值）
- 失败语义：记录首个失败原因（可用率不足 / 超单卡上限 / 同型号数量不足 / tp 总和
  不足），供上层 `OperationNotAllowedException` 透出可读信息
- 决策流程伪代码：

```
for worker in candidates:
    r = _try_worker(worker, ...)
    if r 是 AllocationResult: return r
    记录首个 reason
return AllocationRejected(首个 reason)
```

## Integration（集成点）
- 调用方：`instance/service/creation.py`（候选节点 → 聚合 WorkerResource → first_fit
  → 建记录 → 转发 start）
- 依赖方向：allocator 只读 node/instance 的 model/schema（`gpu_type` 映射
  `node.status.gpu_devices[].name`），**不 import 两者 service**，杜绝「决策→执行→
  回写→再读」service 互调环
- 决策结果消费：`node_id/gpu_indexes/gmu/vram_claim/allocated_vram` 落 `VllmInstance`
  记录并进入 start 转发载荷
- 契约来源：`docs/gpustack-borrowings.md`（显存分配落地规格）、`docs/worker-contract.md`
  §1.3（gpu_devices[].index 为分配依赖键、显存单位 Bytes）
