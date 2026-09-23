"""Worker 进程/路径工具契约测试（端口/docker 模板渲染/容器生命周期）。"""

import subprocess
from pathlib import Path

import pytest

from app.exception import NotFoundException
from app.worker.process_utils import (
    DEFAULT_VLLM_RUN_TEMPLATE,
    build_context,
    build_env_args,
    build_mount_args,
    build_vllm_command,
    get_free_port,
    inspect_container,
    instance_log_path,
    instance_meta_path,
    join_arg_fragment,
    quote_arg,
    render_docker_command,
    resolve_model_path,
    stop_container,
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


# ---------- env / mount 片段 ----------


def test_build_env_args():
    """-e 片段：每项 K=V 经 shlex.join 自动 quote（含空格值）。"""
    frag = build_env_args({"OMP_NUM_THREADS": "1", "VLLM_CACHE_ROOT": "/c/v", "K": "a b"})
    assert frag == "-e OMP_NUM_THREADS=1 -e VLLM_CACHE_ROOT=/c/v -e 'K=a b'"


def test_build_mount_args():
    """-v 片段：host:container 同路径挂载 + 含空格路径 quote。"""
    frag = build_mount_args([("/a", "/a"), ("/b c", "/b c")])
    assert frag.startswith("-v /a:/a -v")
    assert "'/b c:/b c'" in frag


def test_quote_arg_and_join_fragment():
    assert quote_arg("a b") == "'a b'"
    joined = join_arg_fragment(["--port", "18080", "--served-model-name", "qwen 2.5"])
    assert joined == "--port 18080 --served-model-name 'qwen 2.5'"


def _render_cmd(tmp_path: Path) -> list[str]:
    """按 lifecycle._launch_container 同构组装默认模板渲染结果。"""
    model_dir = tmp_path / "models" / "qwen2.5"
    model_dir.mkdir(parents=True)
    spec = _spec()
    full = build_vllm_command("vllm", model_dir, spec, 18080, 0.9)
    args_frag = join_arg_fragment(full[3:])
    env_args = build_env_args(
        {"OMP_NUM_THREADS": "1", "VLLM_CACHE_ROOT": str(tmp_path / "cache" / "vllm")}
    )
    mount_args = build_mount_args([(str(tmp_path / "models"), str(tmp_path / "models"))])
    ctx = build_context(
        name="vllm-i-1",
        image="vllm/vllm-openai:latest",
        vllm_bin="vllm",
        model_path=str(model_dir),
        port="18080",
        gpu_indexes="0,1",
        shm_size="10g",
        args_fragment=args_frag,
        env_args=env_args,
        mount_args=mount_args,
    )
    return render_docker_command(DEFAULT_VLLM_RUN_TEMPLATE, ctx)


# ---------- docker 模板渲染 ----------


def test_render_docker_command_default_template(tmp_path):
    """默认模板渲染：argv 结构完整、无 shell（返回 list）。"""
    cmd = _render_cmd(tmp_path)
    assert isinstance(cmd, list)
    assert cmd[0] == "docker" and cmd[1] == "run"
    assert cmd[cmd.index("--name") + 1] == "vllm-i-1"
    assert cmd[cmd.index("--network") + 1] == "host"
    assert cmd[cmd.index("--shm-size") + 1] == "10g"
    assert cmd[cmd.index("--gpus") + 1] == "device=0,1"
    assert "vllm/vllm-openai:latest" in cmd
    assert "vllm" in cmd and "serve" in cmd
    assert str(tmp_path / "models" / "qwen2.5") in cmd
    assert "--port" in cmd and "18080" in cmd
    assert "--served-model-name" in cmd
    # 挂载/env 片段经 split 还原为独立 argv
    assert "-v" in cmd
    assert f"{tmp_path}/models:{tmp_path}/models" in cmd
    assert "-e" in cmd


def test_render_docker_command_missing_key_raises():
    """模板缺占位符 → KeyError 上抛（由调用方转 ERROR record）。"""
    with pytest.raises(KeyError):
        render_docker_command("docker run --name {name} {image}", {"name": "x"})


def test_render_docker_command_quoted_value_roundtrip(tmp_path):
    """含空格路径：shlex.quote 写入 → split 还原为原始路径（无 shell 注入）。"""
    model_dir = tmp_path / "models" / "qwen 2.5"
    model_dir.mkdir(parents=True)
    ctx = build_context(
        name="vllm-i-1",
        image="vllm/vllm-openai:latest",
        vllm_bin="vllm",
        model_path=str(model_dir),
        port="18080",
        gpu_indexes="0",
        shm_size="10g",
        args_fragment="",
        env_args="",
        mount_args="",
    )
    cmd = render_docker_command(DEFAULT_VLLM_RUN_TEMPLATE, ctx)
    assert str(model_dir) in cmd  # 还原为原始含空格路径


# ---------- 防漂移：worker 与 server 默认模板一致 ----------


def test_default_template_matches_server():
    """worker 内置 DEFAULT_VLLM_RUN_TEMPLATE 与 server 端逐字符一致（防漂移回归锁定）。

    跨域 import 是**有意的**：漂移检测需要双方实值比对；测试级引用不破坏 worker
    运行时隔离（worker 运行不 import server 域，仅此测试做一致性格栅锁）。
    """
    from app.server.instance.service.start_template import (
        DEFAULT_VLLM_RUN_TEMPLATE as server_tpl,
    )

    assert server_tpl == DEFAULT_VLLM_RUN_TEMPLATE


# ---------- stop_container ----------


def _fake_completed(returncode: int, stdout: str = "", stderr: str = ""):
    # text=True 语义：stdout/stderr 为 str（docker CLI 输出按文本处理）
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_stop_container_stop_failure_kill_then_rm(monkeypatch):
    """stop 失败（非 0）→ 顺序 stop(-t 3) → kill → rm；inspect 确认已停止 → 返回 True。"""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        code = 1 if cmd[1] == "stop" else 0
        return _fake_completed(code)

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    # inspect_container 单独 mock：聚焦 stop_container 判定语义（避免共享 subprocess.run）
    monkeypatch.setattr(
        "app.worker.process_utils.inspect_container",
        lambda name: {"State": {"Status": "exited"}},
    )
    assert stop_container("vllm-i-1") is True
    assert calls == [
        ["docker", "stop", "-t", "3", "vllm-i-1"],
        ["docker", "kill", "vllm-i-1"],
        ["docker", "rm", "vllm-i-1"],
    ]


def test_stop_container_stop_ok_skips_kill(monkeypatch):
    """stop 成功 → 跳过 kill，直接 rm；inspect 确认容器不存在 → 返回 True。"""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_completed(0)

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "app.worker.process_utils.inspect_container", lambda name: None
    )
    assert stop_container("vllm-i-1") is True
    assert calls == [
        ["docker", "stop", "-t", "3", "vllm-i-1"],
        ["docker", "rm", "vllm-i-1"],
    ]


def test_stop_container_docker_missing_returns_false(monkeypatch):
    """docker 命令缺失（FileNotFoundError，_run 全 None）→ 停止结果未知，返回 False；
    inspect 探针同样失败（raise）→ 无法确认。"""

    def _raise(cmd, **kwargs):
        raise FileNotFoundError()

    def _raise_inspect(name):
        raise FileNotFoundError()

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _raise)
    monkeypatch.setattr("app.worker.process_utils.inspect_container", _raise_inspect)
    assert stop_container("vllm-i-1") is False  # 不抛，但不得按已停止收敛


def test_stop_container_rm_failed_still_running_returns_false(monkeypatch):
    """P1-2：stop/kill/rm 均执行但全部非零（运行中容器不可 rm）→ inspect 确认仍
    running → 返回 False，不得按已停止收敛（防显存账本漂移）。"""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_completed(1)  # stop/kill/rm 均失败（容器仍运行）

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "app.worker.process_utils.inspect_container",
        lambda name: {"State": {"Status": "running"}},
    )
    assert stop_container("vllm-i-1") is False
    assert calls == [
        ["docker", "stop", "-t", "3", "vllm-i-1"],
        ["docker", "kill", "vllm-i-1"],
        ["docker", "rm", "vllm-i-1"],
    ]


# ---------- inspect_container ----------


def test_inspect_container_parses_json(monkeypatch):
    """成功 → 解析 JSON [0] 返回容器详情。"""

    def _fake_run(cmd, **kwargs):
        return _fake_completed(0, stdout='[{"State": {"Status": "running"}}]')

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    result = inspect_container("vllm-i-1")
    assert result == {"State": {"Status": "running"}}


def test_inspect_no_such_object_returns_none(monkeypatch):
    """F1：stderr 含 `No such object`（容器不存在）→ None。"""

    def _fake_run(cmd, **kwargs):
        return _fake_completed(1, stderr="Error: No such object: vllm-i-1")

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    assert inspect_container("vllm-i-1") is None


def test_inspect_no_such_container_returns_none(monkeypatch):
    """F1：stderr 含 `No such container`（容器不存在变体）→ None。"""

    def _fake_run(cmd, **kwargs):
        return _fake_completed(1, stderr="Error: No such container: vllm-i-1")

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    assert inspect_container("vllm-i-1") is None


def test_inspect_daemon_unreachable_raises(monkeypatch):
    """F1：daemon 不可达（stderr 含 `Cannot connect`）→ 上抛（restore 保留 meta）。"""

    def _fake_run(cmd, **kwargs):
        return _fake_completed(
            1,
            stderr=(
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?"
            ),
        )

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    with pytest.raises(RuntimeError):
        inspect_container("vllm-i-1")


def test_inspect_unknown_error_raises(monkeypatch):
    """F1：返回码非 0 且 stderr 无法归类（未知错误）→ 保守上抛。"""

    def _fake_run(cmd, **kwargs):
        return _fake_completed(1, stderr="docker: something went wrong.")

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    with pytest.raises(RuntimeError):
        inspect_container("vllm-i-1")


def test_inspect_container_docker_missing_raises(monkeypatch):
    """F1：docker 命令缺失（FileNotFoundError）→ 上抛（restore 据此保留 meta）。"""

    def _raise(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _raise)
    with pytest.raises(FileNotFoundError):
        inspect_container("vllm-i-1")


def test_inspect_container_timeout_raises(monkeypatch):
    """F1/S7：docker inspect 超时（TimeoutExpired）→ 上抛，不误判容器消失。"""

    def _raise(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _raise)
    with pytest.raises(subprocess.TimeoutExpired):
        inspect_container("vllm-i-1")


def test_inspect_container_bad_json_raises(monkeypatch):
    """F1：inspect 输出损坏（JSONDecodeError）→ 上抛，不误判容器消失。"""

    def _fake_run(cmd, **kwargs):
        return _fake_completed(0, stdout="not-json")

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    with pytest.raises(ValueError):  # json.JSONDecodeError 是 ValueError 子类
        inspect_container("vllm-i-1")


def test_render_args_with_braces_ok(tmp_path):
    """S2：args 含花括号（prompt 模板/JSON 字面量）→ 渲染成功且保持单 argv 原样还原。

    走真实路径：join_arg_fragment 对含空格 token 引号包裹 → format_map 值原样输出
    （str.format 不解析替换值花括号）→ shlex.split 还原 `{"max_tokens": 10}`。
    """
    model_dir = tmp_path / "models" / "qwen2.5"
    model_dir.mkdir(parents=True)
    args_fragment = join_arg_fragment(
        ["--chat-template", '{"max_tokens": 10, "system": "You are a helpful {assistant}"}']
    )
    ctx = build_context(
        name="vllm-i-1",
        image="vllm/vllm-openai:latest",
        vllm_bin="vllm",
        model_path=str(model_dir),
        port="18080",
        gpu_indexes="0",
        shm_size="10g",
        args_fragment=args_fragment,
        env_args="",
        mount_args="",
    )
    cmd = render_docker_command(DEFAULT_VLLM_RUN_TEMPLATE, ctx)  # 不抛
    # 含花括号 token 保持单 argv 且原样还原（未被 split 拆散/未双花括号）
    assert '{"max_tokens": 10, "system": "You are a helpful {assistant}"}' in cmd


def test_stop_container_timeout_tolerated(monkeypatch):
    """S7：docker stop 超时 → 容错跳过（返回 None 触发 kill 兜底），不中断流程；
    kill/rm 可执行 + inspect 确认已停止 → 返回 True。"""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "stop":
            raise subprocess.TimeoutExpired(cmd, 15)
        return _fake_completed(0)

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "app.worker.process_utils.inspect_container",
        lambda name: {"State": {"Status": "exited"}},
    )
    assert stop_container("vllm-i-1") is True  # stop 超时但 kill/rm 成功 + 确认停止 → 结果已知
    assert calls == [
        ["docker", "stop", "-t", "3", "vllm-i-1"],
        ["docker", "kill", "vllm-i-1"],  # stop 超时（None）→ kill 兜底
        ["docker", "rm", "vllm-i-1"],
    ]


def test_stop_container_daemon_unreachable_returns_false(monkeypatch):
    """docker daemon 不可达（OSError，_run 全 None）→ 停止结果未知，返回 False；
    inspect 探针同样失败（raise）→ 无法确认。"""

    def _raise(cmd, **kwargs):
        raise OSError("Cannot connect to the Docker daemon")

    def _raise_inspect(name):
        raise RuntimeError("Cannot connect to the Docker daemon")

    monkeypatch.setattr("app.worker.process_utils.subprocess.run", _raise)
    monkeypatch.setattr("app.worker.process_utils.inspect_container", _raise_inspect)
    assert stop_container("vllm-i-1") is False  # 不抛，但不得按已停止收敛


def test_render_multi_word_vllm_bin(tmp_path):
    """S5：vllm_bin 多词命令（如 `python -m vllm...`）→ 渲染 + split 还原为独立 argv。"""
    model_dir = tmp_path / "models" / "qwen2.5"
    model_dir.mkdir(parents=True)
    ctx = build_context(
        name="vllm-i-1",
        image="vllm/vllm-openai:latest",
        vllm_bin="python -m vllm.entrypoints.openai.api_server",
        model_path=str(model_dir),
        port="18080",
        gpu_indexes="0",
        shm_size="10g",
        args_fragment="--port 18080",
        env_args="",
        mount_args="",
    )
    cmd = render_docker_command(DEFAULT_VLLM_RUN_TEMPLATE, ctx)
    assert "python" in cmd and "-m" in cmd
    assert "vllm.entrypoints.openai.api_server" in cmd
    assert cmd.index("python") < cmd.index("vllm.entrypoints.openai.api_server")
