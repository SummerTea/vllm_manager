"""Server 模块业务域包。

子域划分：
- node：节点域（登记/心跳/GPU 监控/主动探测），现有实现。
- instance：vLLM 运行实例域，二期实现。
- allocator：显存分配器域，二期实现（依赖 node + instance）。

保持惰性：不在包级 import 子模块，避免潜在循环依赖。
子域间依赖单向：仅允许只读引用他域的 model/schema；禁止跨域 service 互调
（allocator 只读 node/instance 的 model/schema，产出纯数据决策）。
"""
