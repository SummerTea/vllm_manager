# gpustack/api/types/

## Responsibility
OpenAI 兼容 API 的响应类型扩展（DTO 层）。

在 `openai` Python SDK 官方类型基础上做最小扩展，满足 GPUStack 对
embedding 格式、nullable finish_reason 等兼容性需求。

纯 Pydantic 模型定义，无业务逻辑。

## Design
单文件 `openai_ext.py`，4 个模型，全部继承官方 `openai.types`：

- `EmbeddingExt(Embedding)`：
  把 `embedding` 字段扩为 `Union[str, List[float]]`，
  同时支持 base64 编码与 float list 两种向量格式
  （对应 llama-box 等后端的差异输出）。
- `CreateEmbeddingResponseExt(CreateEmbeddingResponse)`：
  `data` 元素替换为 `EmbeddingExt`。
- `CompletionChoiceExt(CompletionChoice)`：
  `finish_reason` 改为可空
  （`Optional[Literal["stop","length","content_filter"]]`），
  适配部分后端不返回该字段的情况。
- `CompletionExt(Completion)`：
  `choices` 元素替换为 `CompletionChoiceExt`。

命名约定：官方类型名 + `Ext` 后缀，明确表达"扩展官方类型"而非重定义。

## Flow
推理响应（普通 JSON 或 SSE chunk）
→ `api/middlewares.py` 的 `ModelUsageMiddleware.process_request`
用对应 `*Ext` 类解析响应体/流式 chunk（`response_class(**response_dict)`）
→ 提取 `usage`（total/prompt/completion tokens、cached tokens）
→ `record_model_usage` 聚合计费与速率指标。

解析失败只记日志，不中断响应透传。

## Integration
- 依赖方：`openai` SDK 类型
  （`Embedding`、`CreateEmbeddingResponse`、`Completion`、`CompletionChoice`）。
- 被依赖方：`gpustack/api/middlewares.py`
  （`CreateEmbeddingResponseExt`、`CompletionExt`）——仅此一处，
  属于推理路径的内部类型，不直接暴露给路由层。
- 参考价值：对"推理服务返回格式兼容（如 vLLM 与 OpenAI 标准差异）"的封装思路，
  本项目 agent 采集指标时若需归一化多后端输出可借鉴。
