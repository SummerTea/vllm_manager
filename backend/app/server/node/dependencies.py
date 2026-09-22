"""Server 模块 Node 鉴权依赖。"""

import secrets
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.exception import UnauthorizedException
from app.extensions.database import get_session
from app.server.node.model import Node
from app.server.node.service import NodeService


async def get_current_node(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Node:
    """从 Authorization: Bearer {token} 解析并校验节点身份。

    - 无 Bearer 令牌 → 401（未提供节点令牌）
    - 令牌查无此节点或不匹配 → 401（无效的节点令牌）
    """
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedException("未提供节点令牌")

    found = await NodeService(session).get_by_field("token", token)
    node = found if isinstance(found, Node) else None
    if node is None or not secrets.compare_digest(node.token, token):
        raise UnauthorizedException("无效的节点令牌")
    return node
