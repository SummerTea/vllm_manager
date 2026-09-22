"""
前后端契约测试

验证 app.main 对外暴露的 URL 契约（路径、状态码、内容类型），不触发 lifespan
（直接实例化 TestClient(app) 而非 `with TestClient(app)`，因此 init_db 不会被
调用、不需要真实 DB/Redis）。
"""

from fastapi.testclient import TestClient

from app.config import app_config
from app.main import app

client = TestClient(app)


def test_api_404_json():
    """API 404 必须返回 JSON（不是 SPA 的 HTML）"""
    response = client.get("/vllm_manager/api/v1/nonexistent")
    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]
    assert "message" in response.json()


def test_missing_static_asset_404_json():
    """缺失静态资源（.js 后缀）404 走 JSON 分支，避免浏览器拿到 text/html 白屏"""
    response = client.get("/vllm_manager/frontend/assets/missing.js")
    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]


def test_account_stub_501():
    """account 契约占位：统一 501 未实现"""
    profile = client.get("/vllm_manager/web_api/account/profile")
    assert profile.status_code == 501
    assert "application/json" in profile.headers["content-type"]

    login = client.post("/vllm_manager/web_api/account/login", json={})
    assert login.status_code == 501
    assert "application/json" in login.headers["content-type"]


def test_spa_deep_route_fallback(tmp_path):
    """SPA 深路由：dist 存在 index.html 时回源 index.html（200 text/html）"""
    original_dist = app_config.FRONTEND_DIST_DIR
    (tmp_path / "index.html").write_text("<html><body>spa</body></html>")
    object.__setattr__(app_config, "FRONTEND_DIST_DIR", tmp_path)
    try:
        response = client.get("/vllm_manager/frontend/some/deep/route")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "spa" in response.text
    finally:
        object.__setattr__(app_config, "FRONTEND_DIST_DIR", original_dist)


def test_spa_deep_route_no_dist_404_html(tmp_path):
    """dist 无 index.html 时深路由 404 回 HTML 错误页（不能给浏览器 JSON）

    注：默认 FRONTEND_DIST_DIR 指向 vllm_manager-scaffold/frontend/dist，本地构建过
    前端后该目录真实存在，因此这里显式指向不存在的路径，保证分支确定性。
    """
    original_dist = app_config.FRONTEND_DIST_DIR
    missing_dist = tmp_path / "no_dist"
    object.__setattr__(app_config, "FRONTEND_DIST_DIR", missing_dist)
    try:
        response = client.get("/vllm_manager/frontend/another/route")
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
    finally:
        object.__setattr__(app_config, "FRONTEND_DIST_DIR", original_dist)
