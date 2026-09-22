"""Worker 进程/路径工具契约测试。"""

from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from app.exception import NotFoundException
from app.worker.process_utils import (
    build_env,
    build_vllm_command,
    get_free_port,
    instance_log_path,
    instance_meta_path,
    resolve_model_path,
    terminate_process_tree,
)

# ---------- get_free_port ----------


def test_get_free_port_returns_bindable():
    port = get_free_port()
    assert isinstance(port, int)
    assert 0 < port < 65536


def test_get_free_port_avoids_unavailable():
    p1 = get_free_port()
    p2 = get_free_port(unavailable={p1})
    assert p2 != p1


# ---------- terminate_process_tree ----------


def test_terminate_process_tree_sigterm_then_sigkill(monkeypatch):
    """断言 SIGTERM → sleep(3) → SIGKILL 顺序。"""
    calls: list[str] = []
    monkeypatch.setattr(
        "app.worker.process_utils._terminate",
        lambda pid: calls.append("term"),
    )
    monkeypatch.setattr(
        "app.worker.process_utils._kill",
        lambda pid: calls.append("kill"),
    )
    monkeypatch.setattr(
        "app.worker.process_utils.time.sleep",
        lambda _: calls.append("sleep"),
    )
    # psutil 兜底：进程已死，直接 return
    monkeypatch.setattr(
        "app.worker.process_utils.psutil.Process",
        lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
    )

    terminate_process_tree(1234)
    assert calls == ["term", "sleep", "kill"]


def test_terminate_process_tree_psutil_fallback(monkeypatch):
    """psutil 递归树兜底：children terminate → wait → kill。"""
    calls: list[str] = []

    class _FakeChild:
        def terminate(self):
            calls.append("child-term")

        def kill(self):
            calls.append("child-kill")

    fake_proc = SimpleNamespace(
        children=lambda recursive: [_FakeChild()],
    )
    monkeypatch.setattr(
        "app.worker.process_utils._terminate",
        lambda pid: None,
    )
    monkeypatch.setattr(
        "app.worker.process_utils._kill",
        lambda pid: None,
    )
    monkeypatch.setattr(
        "app.worker.process_utils.time.sleep",
        lambda _: None,
    )
    monkeypatch.setattr(
        "app.worker.process_utils.psutil.Process",
        lambda pid: fake_proc,
    )
    monkeypatch.setattr(
        "app.worker.process_utils.psutil.wait_procs",
        lambda procs, timeout: ([], procs),  # 首次 wait 后仍 alive → 走 kill
    )

    terminate_process_tree(1234)
    assert "child-term" in calls
    assert "child-kill" in calls


# ---------- 路径 ----------


def test_instance_log_path():
    p = instance_log_path(Path("/logs"), "i-1", 2)
    assert p == Path("/logs/instances/i-1.2.log")


def test_instance_meta_path():
    p = instance_meta_path(Path("/logs"), "i-1")
    assert p == Path("/logs/instances/i-1.json")


def test_resolve_model_path_absolute(tmp_path):
    d = tmp_path / "abs-model"
    d.mkdir()
    assert resolve_model_path(tmp_path, str(d)) == d


def test_resolve_model_path_root_join(tmp_path):
    d = tmp_path / "models" / "qwen"
    d.mkdir(parents=True)
    assert resolve_model_path(tmp_path / "models", "qwen") == d


def test_resolve_model_path_not_found(tmp_path):
    with pytest.raises(NotFoundException):
        resolve_model_path(tmp_path, "not-exist")


# ---------- 命令组装 ----------


def _spec(**overrides):
    base = {
        "model_name": "qwen2.5",
        "task": "auto",
        "gpu_memory_utilization": None,
        "tensor_parallel_size": 1,
        "args": None,
    }
    base.update(overrides)
    return base


def test_build_command_defaults():
    cmd = build_vllm_command("vllm", Path("/m/qwen"), _spec(), 8080, 0.9)
    assert cmd[0:3] == ["vllm", "serve", "/m/qwen"]
    assert "--port" in cmd and "8080" in cmd
    assert "--served-model-name" in cmd and "qwen2.5" in cmd
    assert "--gpu-memory-utilization" in cmd and "0.9" in cmd
    assert "--task" not in cmd  # auto 不注入


def test_build_command_task_mapping():
    cmd = build_vllm_command("vllm", Path("/m/e"), _spec(task="embedding"), 8080, 0.9)
    assert cmd[cmd.index("--task") + 1] == "embed"
    cmd2 = build_vllm_command("vllm", Path("/m/r"), _spec(task="rerank"), 8080, 0.9)
    assert cmd2[cmd2.index("--task") + 1] == "score"


def test_build_command_task_not_duplicated():
    spec = _spec(task="embedding", args=["--task", "embed"])
    cmd = build_vllm_command("vllm", Path("/m/e"), spec, 8080, 0.9)
    assert cmd.count("--task") == 1


def test_build_command_user_args_preserved_and_tp():
    spec = _spec(
        tensor_parallel_size=2,
        args=["--max-model-len", "8192", "--port=9999"],
    )
    cmd = build_vllm_command("vllm", Path("/m/q"), spec, 8080, 0.9)
    assert "8192" in cmd
    assert "--tensor-parallel-size" in cmd
    assert "2" in cmd
    assert "--port=9999" in cmd  # 用户端口优先，不注入 --port
    assert "--port" not in [c for c in cmd if c == "--port"]


# ---------- env ----------


def test_build_env_injects_optimizations(tmp_path):
    env = build_env({"PATH": "/bin"}, tmp_path, [0, 1])
    assert env["OMP_NUM_THREADS"] == "1"
    assert env["SAFETENSORS_FAST_GPU"] == "1"
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert env["PATH"] == "/bin"  # 继承原 env
    assert env["VLLM_CACHE_ROOT"].endswith("vllm")


# ---------- S8 #4：部分存活 children 中间态不抛 ----------


def test_terminate_tree_partial_alive_children(monkeypatch):
    """children 部分已死（terminate 抛 NoSuchProcess）不中断流程。"""

    class _DeadChild:
        def terminate(self):
            raise psutil.NoSuchProcess(1)

    class _AliveChild:
        def terminate(self):
            pass

        def kill(self):
            pass

    fake_proc = SimpleNamespace(children=lambda recursive: [_DeadChild(), _AliveChild()])
    monkeypatch.setattr('app.worker.process_utils._terminate', lambda pid: None)
    monkeypatch.setattr('app.worker.process_utils._kill', lambda pid: None)
    monkeypatch.setattr('app.worker.process_utils.time.sleep', lambda _: None)
    monkeypatch.setattr('app.worker.process_utils.psutil.Process', lambda pid: fake_proc)
    # wait_procs 返回部分存活
    monkeypatch.setattr(
        'app.worker.process_utils.psutil.wait_procs',
        lambda procs, timeout: ([], [_AliveChild()]),
    )

    terminate_process_tree(1234)  # 不抛
