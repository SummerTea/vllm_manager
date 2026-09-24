# gpustack/http_proxy/

## Responsibility
推理网关侧的模型实例负载均衡：在 OpenAI 兼容路由（`routes/openai.py`）转发请求时，从候选 `ModelInstance` 列表中挑选一个目标实例。极小的策略化组件。

## Design
- `LoadBalancer`（`load_balancer.py`）：持有 `LoadBalancingStrategy`（默认 `RoundRobinStrategy`），`set_strategy` 可热切换策略，`get_instance(instances)` 委托策略选实例。
- `strategies.py`：`LoadBalancingStrategy` 抽象基类（`select_instance(instances) -> ModelInstance`）；`RoundRobinStrategy` 按 `model_id` 缓存 `itertools.cycle` 迭代器，实例列表变化时重建，实现每个模型独立的轮询。

## Flow
`routes/openai.py` 维护模块级 `load_balancer = LoadBalancer()`，请求到达时收集某模型可用实例 → `get_instance` → 返回的实例地址/端口用于本模型路由代理。实例列表为空时策略抛异常由上层处理。

## Integration
- 消费方：`gpustack/routes/openai.py`（唯一的导入方，模型推理 HTTP 代理路径）。
- 依赖：`gpustack.schemas.models.ModelInstance`（实例 schema）；无反向依赖，纯被调用库。
