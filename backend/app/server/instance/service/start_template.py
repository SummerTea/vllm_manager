"""Instance 子域 vLLM 启动模板服务（docker run 模板存 PG，create 时快照到实例）。

预制模板由 lifespan 启动时 seed（表空才插，幂等）；resolve_template 供 create
编排解析实例应固化的模板内容：指定 key 取表内模板（无则 404 语义异常），缺省取
默认模板（is_default + is_active），无默认模板时回退内置 DEFAULT_VLLM_RUN_TEMPLATE
（与 worker 内置一致，保证 create 恒能拿到模板快照）。
"""

import logging
from typing import Any

from app.base.base_crud import BaseCrudService
from app.exception import ResourceNotExistException
from app.extensions.database import get_session_context
from app.server.instance.model import VllmStartTemplate

logger = logging.getLogger(__name__)

# server 兜底模板：与 worker 内置一致，表内无默认模板时保证实例仍能拿到快照。
DEFAULT_VLLM_RUN_TEMPLATE = (
    "docker run --name {name} --network host --shm-size {shm_size} "
    "--gpus device={gpu_indexes} {mount_args} {env_args} {image} "
    "{vllm_bin} serve {model_path} {args}"
)

# 变体：经 NVIDIA_VISIBLE_DEVICES 环境变量分配 GPU（--gpus 不可用场景）。
VLLM_GPUS_ENV_TEMPLATE = (
    "docker run --name {name} --network host --shm-size {shm_size} "
    "-e NVIDIA_VISIBLE_DEVICES={gpu_indexes} -e NVIDIA_DRIVER_CAPABILITIES=compute,utility "
    "{mount_args} {env_args} {image} {vllm_bin} serve {model_path} {args}"
)

SEED_START_TEMPLATES: list[dict] = [
    {
        "template_key": "vllm-default",
        "instance_type": "vllm",
        "template": DEFAULT_VLLM_RUN_TEMPLATE,
        "description": "默认 vLLM docker 启动模板（--gpus device= 按索引分配 GPU）",
        "is_active": True,
        "is_default": True,
    },
    {
        "template_key": "vllm-gpus-env",
        "instance_type": "vllm",
        "template": VLLM_GPUS_ENV_TEMPLATE,
        "description": "vLLM docker 启动模板（NVIDIA_VISIBLE_DEVICES 环境变量分配 GPU）",
        "is_active": True,
        "is_default": False,
    },
]


class VllmStartTemplateService(BaseCrudService[VllmStartTemplate]):
    """vLLM 启动模板服务（默认不提交事务，由 API 路由层统一提交）。"""

    async def get_by_key(self, template_key: str) -> VllmStartTemplate | None:
        """按唯一键查询模板。"""
        found = await self.get_by_field("template_key", template_key)
        return found if isinstance(found, VllmStartTemplate) else None

    async def get_default(self) -> VllmStartTemplate | None:
        """查询当前默认模板（is_default 且 is_active），template_key 确定性排序首条。"""
        items = await self.get_list(
            filters={"is_default": True, "is_active": True},
            order_by=["template_key"],
            limit=1,
        )
        return items[0] if items else None

    async def _clear_other_defaults(self, exclude_id: str) -> None:
        """单默认约束：同事务内把其余 is_default=True 的行清为 False。

        仅在设为目标默认（exclude_id）时调用；模板数量少，get_list + 循环 flush 可接受。
        """
        defaults = await self.get_list(filters={"is_default": True})
        changed = False
        for tpl in defaults:
            if tpl.id != exclude_id:
                tpl.is_default = False
                changed = True
        if changed:
            await self.session.flush()

    async def create(
        self, data: dict[str, Any] | VllmStartTemplate, *, commit: bool = False
    ) -> VllmStartTemplate:
        """创建模板；is_default=True 时同事务清除其余默认（单默认约束）。"""
        instance = await super().create(data, commit=commit)
        is_default = (
            data.get("is_default", False)
            if isinstance(data, dict)
            else getattr(data, "is_default", False)
        )
        if is_default:
            await self._clear_other_defaults(instance.id)
        return instance

    async def update(
        self,
        id_: Any,
        data: dict[str, Any],
        *,
        partial: bool = True,
        commit: bool = False,
    ) -> VllmStartTemplate | None:
        """更新模板；data.is_default=True 时同事务清除其余默认（单默认约束）。"""
        instance = await super().update(id_, data, partial=partial, commit=commit)
        if instance is not None and data.get("is_default") is True:
            await self._clear_other_defaults(id_)
        return instance

    async def resolve_template(self, template_key: str | None) -> str:
        """解析实例应固化的模板内容。

        - 指定 template_key：取表内模板，不存在抛 ResourceNotExistException
        - 缺省（None）：取默认模板；无默认模板回退 DEFAULT_VLLM_RUN_TEMPLATE
          （停用/缺失默认模板时告警一次，提示正回退内置常量）
        """
        if template_key is not None:
            tpl = await self.get_by_key(template_key)
            if tpl is None:
                raise ResourceNotExistException(
                    "启动模板不存在",
                    resource_type="vllm_start_template",
                    resource_id=template_key,
                )
            return tpl.template
        default = await self.get_default()
        if default is not None:
            return default.template
        logger.warning(
            "未找到启用的默认启动模板，回退内置 DEFAULT_VLLM_RUN_TEMPLATE"
            "（默认模板缺失或已被停用）"
        )
        return DEFAULT_VLLM_RUN_TEMPLATE


async def _seed_in_session(session) -> None:
    """在给定会话内执行 seed（表空才插，count>0 跳过）。"""
    service = VllmStartTemplateService(session)
    count = await service.count()
    if count > 0:
        logger.info("启动模板表已有 %d 条记录，跳过 seed", count)
        return
    await service.bulk_create(SEED_START_TEMPLATES, commit=True)
    logger.info("已 seed %d 条预制启动模板", len(SEED_START_TEMPLATES))


async def seed_start_templates(session=None) -> None:
    """预制启动模板 seed（幂等）：表空才插入定稿模板，二次调用跳过。

    - 缺省走全局引擎会话（lifespan init_db 后调用，生产路径）
    - 显式传入 session（测试注入 sqlite 会话），跳过全局引擎依赖
    """
    if session is not None:
        await _seed_in_session(session)
        return
    async with get_session_context() as session:
        await _seed_in_session(session)
