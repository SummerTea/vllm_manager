"""
Worker 管控节点入口（:8100，不连 PG，状态全内存）
Usage:
    # 启动 worker（默认绑定 0.0.0.0:8100——server 跨机回连硬约束）
    python app/work_worker.py
    # 指定监听地址（端口由 WORKER_LISTEN_PORT 配置决定）
    python app/work_worker.py --host 10.0.0.1
    # 健康检查（退出码 0）
    python app/work_worker.py --check
"""
import argparse
import sys
from pathlib import Path

# 将项目根目录添加到 python path，确保能导入 app 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器（独立函数便于测试直接断言默认值）。"""
    parser = argparse.ArgumentParser(description="VllmManager Worker 管控节点")
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="监听地址（默认 0.0.0.0——server 跨机回连硬约束）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="执行健康检查并退出（用于健康检查）",
    )
    return parser


def main():
    """worker 入口：uvicorn.run(create_app, factory=True)。"""
    from app.worker.config import worker_config

    args = _build_parser().parse_args()

    if args.check:
        # 轻量 import 校验：应用入口模块不可加载时报错退出非 0
        try:
            import app.worker.main  # noqa: F401
        except ImportError as e:
            print(f"Health check failed: {e}")
            sys.exit(1)
        print("Health check passed")
        sys.exit(0)

    import uvicorn

    uvicorn.run(
        "app.worker.main:create_app",
        factory=True,
        host=args.host,
        port=worker_config.WORKER_LISTEN_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
