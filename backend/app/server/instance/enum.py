"""Instance 子域业务枚举。"""

from app.base.base_enum import LabeledStrEnum


class InstanceTypeEnum(LabeledStrEnum):
    """实例类型枚举。"""

    VLLM = "vllm", "vLLM 推理实例"


class InstanceStateEnum(LabeledStrEnum):
    """实例状态枚举。"""

    PENDING = "pending", "待启动"
    STARTING = "starting", "启动中"
    RUNNING = "running", "运行中"
    STOPPED = "stopped", "已停止"
    ERROR = "error", "错误"
    UNREACHABLE = "unreachable", "不可达"


class InstanceTargetStateEnum(LabeledStrEnum):
    """实例目标状态枚举（用户操作写入，达成后清回 none）。"""

    NONE = "none", "无"
    STARTING = "starting", "启动"
    STOPPING = "stopping", "停止"


class InstanceTaskEnum(LabeledStrEnum):
    """产品层任务分类（页面创建/展示/过滤），非 vLLM --task 枚举；worker 端负责
    映射 vLLM 实际枚举（见 architecture.md 契约表：auto/llm 不注入、embedding→embed、
    rerank→score）。"""

    AUTO = "auto", "自动推断"
    LLM = "llm", "LLM 文本生成"  # 产品层显式 LLM 标记 vs AUTO 自动推断（行为等价，仅服务前端展示/过滤）
    EMBEDDING = "embedding", "Embedding 向量化"
    RERANK = "rerank", "Rerank 重排"  # cross-encoder 重排，worker 映射 --task score
