"""Worker 进程/路径工具：端口分配、进程树终止、日志/meta 路径、模型路径解析。

对齐 gpustack：
- terminate_process_tree → utils/process.py:70（psutil 递归 children → SIGTERM → wait 3s → SIGKILL）
- 端口分配 → serve_manager.py:_assign_ports（socket 探测 + 集合幂等）
- 日志编号 → serve_manager.py:_get_numbered_log_path（{id}.{restart_count}.log）
"""

import contextlib
import logging
import os
import signal
import socket
import time
from pathlib import Path
from typing import BinaryIO, TextIO

import psutil

from app.exception import NotFoundException

logger = logging.getLogger(__name__)


def get_free_port(host: str = "127.0.0.1", unavailable: set[int] | None = None) -> int:
    """socket 探测空闲端口；unavailable 集合中的端口排除（幂等防复用）。

    bind((host, 0)) 让系统分配空闲端口，若落入 unavailable 则重试。
    """
    excluded = unavailable or set()
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]
        if port not in excluded:
            return port
    raise RuntimeError("无法分配空闲端口（排除集冲突次数过多）")


def _terminate(pid: int) -> None:
    """对进程组发 SIGTERM（进程不存在时静默容错）。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)


def _kill(pid: int) -> None:
    """对进程组发 SIGKILL（进程不存在时静默容错）。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def terminate_process_tree(pid: int) -> None:
    """终止进程树：killpg SIGTERM → 3s → SIGKILL + psutil 递归树兜底。

    组合策略（vLLM 多进程可能脱离进程组，psutil 递归兜底）：
    1. killpg(pid, SIGTERM)：Popen start_new_session 后进程组=pid，整组优雅退出
    2. sleep(3)：等待优雅退出
    3. killpg(pid, SIGKILL)：整组强杀
    4. psutil 递归树兜底：children(recursive=True) → terminate → wait 3s → kill
       （对齐 gpustack utils/process.py:terminate_process_tree）
    """
    _terminate(pid)
    time.sleep(3)
    _kill(pid)
    try:
        process = psutil.Process(pid)
        children = process.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    except Exception:  # noqa: BLE001 - 兜底失败不影响主流程
        logger.warning("psutil 递归树兜底失败: pid=%s", pid)
        return
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            continue
    _, alive = psutil.wait_procs(children, timeout=3)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs(alive, timeout=1)


def instance_log_path(log_dir: Path, instance_id: str, restart_count: int) -> Path:
    """实例日志路径：{log_dir}/instances/{instance_id}.{restart_count}.log。"""
    return log_dir / "instances" / f"{instance_id}.{restart_count}.log"


def instance_meta_path(log_dir: Path, instance_id: str) -> Path:
    """实例 meta 文件路径（restore 用）：{log_dir}/instances/{instance_id}.json。"""
    return log_dir / "instances" / f"{instance_id}.json"


def resolve_model_path(model_root: Path, model_name: str) -> Path:
    """解析模型目录路径。

    - model_name 为绝对路径或含路径分隔符 → 直用
    - 否则 model_root / model_name
    - 目录不存在 → 抛 NotFoundException(404)
    """
    name = Path(model_name)
    path = name if name.is_absolute() or "/" in model_name else model_root / model_name
    if not path.is_dir():
        raise NotFoundException(f"模型目录不存在: {path}")
    return path


def _sum_weight_files(root: Path) -> int:
    """递归扫描 .safetensors/.bin/.pt/.pth 权重文件求和（Bytes）。"""
    total = 0
    suffixes = (".safetensors", ".bin", ".pt", ".pth")
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if filename.endswith(suffixes):
                try:
                    total += (Path(dirpath) / filename).stat().st_size
                except OSError:
                    continue
    return total


def build_vllm_command(
    vllm_bin: str,
    model_path: Path,
    spec: dict,
    port: int,
    default_gmu: float,
) -> list[str]:
    """组装 vLLM 启动命令（用户 args 优先，缺省补全）。

    缺省补全（仅当 args 未含对应参数）：
    - --port {port}
    - --served-model-name {spec[model_name]}
    - --gpu-memory-utilization {spec[gpu_memory_utilization] or default_gmu}
    - tensor_parallel_size > 1 → --tensor-parallel-size {tp}
    - task 映射：auto/llm 不注入；embedding → --task embed；rerank → --task score
      （args 已含 --task 时不注入）
    """
    args = list(spec.get("args") or [])
    cmd = [vllm_bin, "serve", str(model_path)]

    # task 映射（用户 args 优先）
    if not _has_arg(args, "--task"):
        task = spec.get("task", "auto")
        if task == "embedding":
            args.extend(["--task", "embed"])
        elif task == "rerank":
            args.extend(["--task", "score"])

    # 缺省补全
    if not _has_arg(args, "--port"):
        args.extend(["--port", str(port)])
    if not _has_arg(args, "--served-model-name"):
        args.extend(["--served-model-name", spec.get("model_name", "")])
    if not _has_arg(args, "--gpu-memory-utilization"):
        gmu = spec.get("gpu_memory_utilization") or default_gmu
        args.extend(["--gpu-memory-utilization", str(gmu)])
    if (spec.get("tensor_parallel_size") or 1) > 1 and not _has_arg(
        args, "--tensor-parallel-size"
    ):
        args.extend(["--tensor-parallel-size", str(spec.get("tensor_parallel_size"))])

    return cmd + args


def _has_arg(args: list[str], key: str) -> bool:
    """检查 args 是否已含 key（--key 或 --key=value 形式）。"""
    return any(
        arg == key or arg.startswith(f"{key}=") for arg in args
    )


def build_env(env, cache_dir: Path, gpu_indexes: list[int]) -> dict:
    """组装 vLLM 启动 env：继承 os.environ + 优化注入 + CUDA_VISIBLE_DEVICES。"""
    result = dict(env)
    result["OMP_NUM_THREADS"] = "1"
    result["SAFETENSORS_FAST_GPU"] = "1"
    cache_vllm = cache_dir / "vllm"
    try:
        cache_vllm.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("VLLM_CACHE_ROOT 目录创建失败: %s", cache_vllm)
    else:
        result["VLLM_CACHE_ROOT"] = str(cache_vllm)
    result["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_indexes)
    return result


def open_log_file(path: Path) -> "TextIO | BinaryIO":
    """创建日志文件父目录并以追加二进制打开（供 Popen stdout/stderr）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "ab")  # noqa: SIM115 - 由 Popen 生命周期持有
