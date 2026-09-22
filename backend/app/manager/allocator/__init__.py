"""Allocator 子域：显存分配器（二期实现，依赖 node + instance）。

边界约束：只**只读**引用 node/instance 的 model/schema，产出纯数据决策
（gpu_indexes / gmu / port 等）；不 import 两者的 service，
避免「allocator 决策 → instance 执行 → 回写 → allocator 再读」形成 service 互调环。
"""
