"""
VllmManager 应用入口
FastAPI 应用
"""

# ruff: noqa: E402,I001,SIM117

# 在最早时机加载 .env 到 os.environ，确保 pydantic-settings 未覆盖的
# 环境变量也能通过 os.environ 传递给子进程。
from dotenv import load_dotenv

load_dotenv()

import logging

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger(__name__)

from app.config import app_config
from app.main.exceptions import register_exception_handlers
from app.main.lifespan import lifespan as app_lifespan
from app.main.routers import get_api_router
from app.main.static_files import SafeStaticFiles
from app.main.web_routers import get_web_api_router

# ============ 应用初始化 ============

app = FastAPI(
    title=app_config.APPLICATION_NAME,
    description="Async-First FastAPI Architecture",
    version="0.1.0",
    lifespan=app_lifespan,
    docs_url=f"{app_config.BASE_URL_PATH}/docs",
    redoc_url=f"{app_config.BASE_URL_PATH}/redoc",
)

# Cookie Session 配置（认证由业务方自行接入，如登录接口写入 user_id）
app.add_middleware(
    SessionMiddleware,
    secret_key=app_config.SECRET_KEY,
    session_cookie="session",
    max_age=app_config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=app_config.SESSION_HTTPS_ONLY or app_config.DEPLOY_ENV == "PRODUCTION",
)

# ============ 注册路由 ============

# 公共 API 路由 (/api/v1)
app.include_router(get_api_router())

# 内部 Web API 路由 (/web_api)
app.include_router(get_web_api_router())

# ============ 静态文件 (前端 SPA) ============

# 挂载前端构建产物，目录不存在时静默（SafeStaticFiles check_config 为空操作）
app.mount(
    app_config.BASE_URL_PATH + "/frontend",
    SafeStaticFiles(
        directory=str(app_config.FRONTEND_DIST_DIR), html=True, check_dir=False
    ),
    name="frontend",
)

# ============ 首页重定向 ============


@app.get("/")
async def root_redirect():
    """根路径重定向到前端页面"""
    return RedirectResponse(url=app_config.BASE_URL_PATH + "/frontend/")

# ============ 心跳端点 ============


@app.get("/heartbeat")
async def heartbeat():
    """
    心跳/健康检查端点

    用于负载均衡或监控系统检测服务可用性。
    """
    return {"status": "ok"}


# ============ 异常处理 ============
register_exception_handlers(app)
