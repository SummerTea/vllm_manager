"""Worker 进程/路径工具：端口分配、docker 模板渲染、容器生命周期、日志/meta 路径。

Phase B 容器化：实例启动由「Popen vllm serve」改为「Popen docker run」——
- `render_docker_command` 模板渲染 → shlex.split → argv 交 Popen（**无 shell**）
- 容器停止走 `stop_container`（docker stop/kill/rm），**绝不 killpg docker CLI**
  （killpg 只杀 CLI 不杀容器 → 容器变孤儿）
- 容器存活权威判定走 `inspect_container`（docker inspect）

对齐 gpustack：
- 端口分配 → serve_manager.py:_assign_ports（socket 探测 + 集合幂等）
- 日志编号 → serve_manager.py:_get_numbered_log_path（{id}.{restart_count}.log）

> S9：terminate_process_tree/_terminate/_kill 已移除——容器化后停止路径全部走
> docker stop/kill/rm（killpg 会杀 CLI 不杀容器致孤儿），psutil 递归树无生产调用者。
"""

import json
import logging
import os
import shlex
import socket
import subprocess
from pathlib import Path
from typing import BinaryIO, TextIO

from app.exception import NotFoundException

logger = logging.getLogger(__name__)

# 默认 docker 启动模板：**必须与 server 端
# app/server/instance/service/start_template.py 的 DEFAULT_VLLM_RUN_TEMPLATE 逐字符一致**
# （防漂移注记：worker-contract §2.2；worker 渲染 {port}/{model_path}/{gpu_indexes} 等
# 由 worker 侧填写；缺省 template（含存量实例）用此内置模板兜底）
DEFAULT_VLLM_RUN_TEMPLATE = (
    "docker run --name {name} --network host --shm-size {shm_size} "
    "--gpus device={gpu_indexes} {mount_args} {env_args} {image} "
    "{vllm_bin} serve {model_path} {args}"
)


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


def build_env_args(env_items: dict[str, str]) -> str:
    """组装 docker 环境变量片段：`-e K=V ...`（每项经 shlex.join 自动 quote）。

    取代原 Popen env 用法（docker 用 -e 传参）：调用方负责构造 env_items
    （OMP_NUM_THREADS/SAFETENSORS_FAST_GPU/VLLM_CACHE_ROOT 逻辑保留在调用方）。
    返回片段已 quote，直接放入模板 {env_args} 占位即可。
    """
    parts: list[str] = []
    for key, value in env_items.items():
        parts.extend(["-e", f"{key}={value}"])
    return join_arg_fragment(parts)


def build_mount_args(mounts: list[tuple[str, str]]) -> str:
    """组装 docker 挂载片段：`-v host:container ...`（每项经 shlex.join 自动 quote）。

    同路径挂载时 host == container（模型根/缓存目录容器内同路径可见）。
    返回片段已 quote，直接放入模板 {mount_args} 占位即可。
    """
    parts: list[str] = []
    for host_path, container_path in mounts:
        parts.extend(["-v", f"{host_path}:{container_path}"])
    return join_arg_fragment(parts)


def quote_arg(value: str) -> str:
    """shlex.quote 包装单个占位值（防 shell 注入——渲染后虽无 shell，仍防御性 quote）。"""
    return shlex.quote(value)


def join_arg_fragment(parts: list[str]) -> str:
    """shlex.join 拼接参数片段（自动 quote 含空格项），供模板 {args}/{env_args} 占位。"""
    return shlex.join(parts)


def build_context(
    *,
    name: str,
    image: str,
    vllm_bin: str,
    model_path: str,
    port: str,
    gpu_indexes: str,
    shm_size: str,
    args_fragment: str,
    env_args: str,
    mount_args: str,
) -> dict[str, str]:
    """组装模板渲染上下文。

    - name/image/model_path/shm_size 等用户/配置可控值经 quote_arg 防御
    - vllm_bin 支持多词命令（如 `python -m vllm.entrypoints.openai.api_server`）：
      shlex.split 展开为多 token 后 join（每 token quote），渲染 + split 还原为独立 argv
    - args_fragment/env_args/mount_args 已是 shlex.join 后片段，**不再二次 quote**
    - args 花括号（S2）**无需转义**：str.format 不解析替换值中的 `{`/`}`（原样输出）；
      含空格 token 由 shlex.join 引号包裹 → shlex.split 正确还原（如
      `{"max_tokens": 10}` 保持单 argv）；在值中做 `{`→`{{` 转义反而会输出双花括号
    - port（数字）、gpu_indexes（"0,1"）无特殊字符直接给
    """
    return {
        "name": quote_arg(name),
        "image": quote_arg(image),
        "vllm_bin": join_arg_fragment(shlex.split(vllm_bin)),
        "model_path": quote_arg(model_path),
        "port": str(port),
        "gpu_indexes": gpu_indexes,
        "shm_size": quote_arg(shm_size),
        "args": args_fragment,
        "env_args": env_args,
        "mount_args": mount_args,
    }


def render_docker_command(template: str, context: dict[str, str]) -> list[str]:
    """渲染 docker 启动命令：`template.format_map(context)` → `shlex.split` → argv。

    - 返回 list 直接交 Popen（**无 shell**，参数不经过 shell 解释）
    - 模板缺占位符 → KeyError 上抛（由调用方转 ERROR record）
    - 占位值须已 quote（build_context/join_arg_fragment 负责），split 还原原始 argv
    """
    rendered = template.format_map(context)
    return shlex.split(rendered)


def stop_container(container_name: str) -> bool:
    """停止并清理容器：`docker stop -t 3` →（stop 失败时 `docker kill`）→ `docker rm`，
    最后 `docker inspect` 确认容器已不再运行。

    **绝不 killpg docker CLI 进程**——killpg 只杀 CLI 不杀容器，容器变孤儿；
    容器必须走 docker 命令。docker 命令缺失/容器不存在等一律容错不抛。

    返回语义（True=已确认不再运行；False=仍在运行或无法确认）：
    - `True`：`docker inspect` 确认容器不存在，或状态非 running（Exited/dead/
      stopped 等）——容器已确认不再运行。
    - `False`：stop/kill/rm 尽力执行后 inspect 仍 running（容器仍在运行），或
      docker 无法确认（命令缺失/daemon 不可达/超时/探针异常）→ 调用方**不得**
      按已停止收敛（防显存账本漂移）。
    """
    def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(cmd, capture_output=True, timeout=15)
        except FileNotFoundError:
            logger.warning("docker 命令不可用，无法操作容器 %s: %s", container_name, cmd)
            return None
        except subprocess.TimeoutExpired:
            logger.warning("docker 命令超时（15s），继续下一步: %s", cmd)
            return None
        except OSError as e:  # noqa: BLE001 - daemon 不可达等容错
            logger.warning("docker 操作失败 %s: %s", cmd, e)
            return None

    stop_proc = _run(["docker", "stop", "-t", "3", container_name])
    if stop_proc is None or stop_proc.returncode != 0:
        # stop 失败或状态未知（超时/异常/容器已停）→ kill 强杀兜底
        kill_proc = _run(["docker", "kill", container_name])
        if stop_proc is None and kill_proc is None:
            return False  # docker 命令完全不可用，停止结果未知
    _run(["docker", "rm", container_name])  # 清理容器（对不存在容器容错）

    # 结果确认：复用 inspect_container 三态语义（None=容器不存在 / raise=探针异常）
    try:
        inspected = inspect_container(container_name)
    except Exception:  # noqa: BLE001 - 探针异常（docker 缺失/daemon 不可达/超时/解析失败）→ 无法确认
        return False
    if inspected is None:
        return True  # 容器不存在 → 已停止
    return inspected.get("State", {}).get("Status") != "running"


def inspect_container(container_name: str) -> dict | None:
    """`docker inspect` 容器状态（Gate 3 F1 三态语义）。

    - **容器不存在**（返回码非 0 且 stderr 含 `No such object`/`No such container`）
      → None（restore 据此删 meta）
    - **探针异常 → 上抛**：docker 命令缺失（FileNotFoundError）/ 超时
      （TimeoutExpired）/ daemon 不可达等连接错误（stderr 含 `Cannot connect`/
      `Cannot reach`）/ 返回码非 0 但 stderr 无法归类 / 输出解析失败
      （JSONDecodeError）——调用方（restore）据此**保留 meta + 告警**，
      避免 docker 短暂不可用时误删全部 meta → 实例被对账 stopped → 显存超卖。
    """
    proc = subprocess.run(  # noqa: S603 - docker 命令；异常按 F1 上抛
        ["docker", "inspect", container_name],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if "No such object" in stderr or "No such container" in stderr:
            return None  # 容器不存在
        raise RuntimeError(  # 连接错误/未知错误：保守上抛，不误判容器消失
            f"docker inspect 容器 {container_name} 失败（code={proc.returncode}）: "
            f"{stderr.strip()}"
        )
    data = json.loads(proc.stdout)  # JSONDecodeError 上抛（探针异常）
    return data[0] if data else None


def open_log_file(path: Path) -> "TextIO | BinaryIO":
    """创建日志文件父目录并以追加二进制打开（供 Popen stdout/stderr）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "ab")  # noqa: SIM115 - 由 Popen 生命周期持有
