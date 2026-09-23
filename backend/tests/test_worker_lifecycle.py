"""Worker 实例生命周期契约测试（mock Popen/健康检查/显存）。

重点覆盖：start 受理即 200、task 映射、幂等、显存校验、stop 幂等、
sync 状态机、退避重启、restore 仅恢复 running、weight 求和。
"""

import json
from datetime import datetime, timedelta

import pytest

from app.exception import ExternalServiceException, NotFoundException
from app.worker.config import WorkerConfig
from app.worker.enum import WorkerInstanceStateEnum
from app.worker.lifecycle import InstanceLifecycleManager, WorkerInstanceRecord
from app.worker.schema import StartRequest, VllmSpec, WeightRequest


def _cfg(**overrides) -> WorkerConfig:
    return WorkerConfig(**overrides)


def _noop_stop_container(name: str) -> bool:
    """fixture 默认 stop_container mock：防测试触发真实 docker 命令（视为停止成功）。"""
    return True


def _start_request(**spec_overrides) -> StartRequest:
    gpu_indexes = spec_overrides.pop("gpu_indexes", [0])
    template = spec_overrides.pop("template", None)
    spec = {
        "model_name": "qwen2.5",
        "task": "auto",
        "gpu_memory_utilization": None,
        "tensor_parallel_size": 1,
        "args": None,
    }
    spec.update(spec_overrides)
    return StartRequest(
        instance_type="vllm",
        spec=VllmSpec(**spec),
        gpu_indexes=gpu_indexes,
        vram_claim=16 * 1024**3,
        template=template,
    )


class _FakePopenSimple:
    def __init__(self, cmd=None, **kwargs):
        self.pid = 1000
        self._code: int | None = None

    def poll(self):
        return self._code


async def _health_true(self, record) -> bool:
    return True


async def _health_false(self, record) -> bool:
    return False


@pytest.fixture
def manager(tmp_path, monkeypatch):
    cfg = _cfg(
        WORKER_MODEL_ROOT=tmp_path / "models",
        WORKER_LOG_DIR=tmp_path / "logs",
        WORKER_CACHE_DIR=tmp_path / "cache",
    )
    (tmp_path / "models" / "qwen2.5").mkdir(parents=True)
    # _launch_container 入口无条件 stop_container（#1）：mock 掉防真实 docker 命令
    monkeypatch.setattr("app.worker.lifecycle.stop_container", _noop_stop_container)
    m = InstanceLifecycleManager(cfg, "node-1", "tok-1")
    return m


# ---------- start ----------


async def test_start_success_creates_starting_record(manager, monkeypatch):
    """start 受理成功：record STARTING、cmd 为 docker run argv（默认模板渲染）。

    断言：--name/--network host/--shm-size 10g/--gpus device=/挂载/env/镜像、
    vllm serve model_path、--port/--served-model-name/GMU 补全。
    """
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            self.pid = 999
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)

    result = await manager.start("i-1", _start_request())

    assert result == {"status": "accepted"}
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.STARTING
    assert rec.port == 18080
    cmd = captured["cmd"]
    # docker run 骨架 + 模板关键参数
    assert cmd[0] == "docker" and cmd[1] == "run"
    assert cmd[cmd.index("--name") + 1] == "vllm-i-1"
    assert cmd[cmd.index("--network") + 1] == "host"
    assert cmd[cmd.index("--shm-size") + 1] == "10g"
    assert cmd[cmd.index("--gpus") + 1] == "device=0"
    assert "vllm/vllm-openai:latest" in cmd
    assert "vllm" in cmd and "serve" in cmd
    assert str(manager._config.WORKER_MODEL_ROOT / "qwen2.5") in cmd
    # vLLM 参数（模板 {args} 片段还原）
    assert "18080" in cmd  # --port 补全
    assert "--served-model-name" in cmd
    assert "--gpu-memory-utilization" in cmd
    # 挂载 + env（-v/-e 片段）
    assert "-v" in cmd and "-e" in cmd
    # Popen 不再传 env kwarg（docker 用 -e 传参，GPU 走 --gpus）
    assert "env" not in captured["kwargs"]


async def test_start_task_embedding_injects(manager, monkeypatch):
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 999
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)

    await manager.start("i-1", _start_request(task="embedding"))
    cmd = captured["cmd"]
    assert cmd[cmd.index("--task") + 1] == "embed"


async def test_start_cpu_no_gpu_fragments(manager, monkeypatch):
    """CPU-only 节点（gpu_indexes=[]）start → 渲染用 CPU 片段：
    -p {port}:{port}，不含 --gpus/--network host（空串片段）。"""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 999
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)

    result = await manager.start("i-1", _start_request(gpu_indexes=[]))

    assert result == {"status": "accepted"}
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.STARTING
    assert rec.gpu_indexes == []
    cmd = captured["cmd"]
    assert cmd[0] == "docker" and cmd[1] == "run"
    # CPU 片段：-p {port}:{port}（非 --network host），无 --gpus
    assert "-p" in cmd and "18080:18080" in cmd
    assert "--network" not in cmd
    assert "--gpus" not in cmd
    # vLLM 参数（模板 {args} 片段还原）：--port 补全、挂载/env 仍在
    assert "18080" in cmd
    assert "--served-model-name" in cmd
    assert "-v" in cmd and "-e" in cmd


async def test_start_gpu_env_injects_omp_and_safetensors(manager, monkeypatch):
    """GPU 实例（gpu_indexes=[0]）env 片段含 OMP_NUM_THREADS=1 与 SAFETENSORS_FAST_GPU=1。"""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 999
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)

    await manager.start("i-1", _start_request(gpu_indexes=[0]))

    cmd = captured["cmd"]
    assert "OMP_NUM_THREADS=1" in cmd
    assert "SAFETENSORS_FAST_GPU=1" in cmd
    # VLLM_CACHE_ROOT 仍注入（GPU/CPU 均保留）
    assert any(arg.startswith("VLLM_CACHE_ROOT=") for arg in cmd)


async def test_start_cpu_env_skips_omp_and_safetensors(manager, monkeypatch):
    """CPU 实例（gpu_indexes=[]）env 片段不含 OMP_NUM_THREADS/SAFETENSORS_FAST_GPU
    （CPU 推理注入 OMP_NUM_THREADS=1 会锁单线程，性能极差）；VLLM_CACHE_ROOT 保留。"""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 999
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)

    await manager.start("i-1", _start_request(gpu_indexes=[]))

    cmd = captured["cmd"]
    assert not any(arg.startswith("OMP_NUM_THREADS=") for arg in cmd)
    assert not any(arg.startswith("SAFETENSORS_FAST_GPU=") for arg in cmd)
    # CPU 也保留 VLLM_CACHE_ROOT（缓存持久目录）
    assert any(arg.startswith("VLLM_CACHE_ROOT=") for arg in cmd)
    assert "-e" in cmd  # env 片段仍存在（VLLM_CACHE_ROOT）


async def test_start_idempotent_returns_accepted(manager, monkeypatch):
    """幂等：已存在 record（starting）→ accepted 且不重建。"""
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)
    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopenSimple)

    await manager.start("i-1", _start_request())
    calls_before = len(manager._instances)

    await manager.start("i-1", _start_request())
    assert len(manager._instances) == calls_before  # 不重建
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.STARTING


async def test_start_uses_provided_template(manager, monkeypatch):
    """start 携带 template 快照 → 渲染用传入模板（非内置默认）。"""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 999
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)
    custom_tpl = (
        "docker run --name {name} --label custom {image} "
        "{vllm_bin} serve {model_path} {args}"
    )

    await manager.start("i-1", _start_request(template=custom_tpl))

    cmd = captured["cmd"]
    assert cmd[0] == "docker" and cmd[1] == "run"
    assert cmd[cmd.index("--label") + 1] == "custom"
    # 传入模板无 --network host/--gpus → 渲染结果不含
    assert "--network" not in cmd
    assert "--gpus" not in cmd
    # 模板快照落入 record.spec（随 meta 持久化，退避重启复用）
    assert manager._instances["i-1"].spec["template"] == custom_tpl


async def test_start_default_template_when_missing(manager, monkeypatch):
    """start 缺省 template（存量/无快照）→ 内置默认模板兜底（--network host/--gpus）。"""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 999
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)

    await manager.start("i-1", _start_request())

    cmd = captured["cmd"]
    assert cmd[cmd.index("--network") + 1] == "host"
    assert cmd[cmd.index("--gpus") + 1] == "device=0"
    assert cmd[cmd.index("--shm-size") + 1] == "10g"


async def test_start_model_missing_error_record(manager):
    """模型不存在 → ERROR record + state_message，受理仍 200。"""
    req = _start_request(model_name="not-exist")
    result = await manager.start("i-1", req)

    assert result == {"status": "accepted"}
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.ERROR
    assert "模型目录不存在" in (rec.state_message or "")


async def test_start_vram_insufficient_error_record(manager, monkeypatch):
    """显存不足（mock _check_vram False）→ ERROR + 可读 message。"""
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._check_vram",
        lambda self, gpu_indexes, claim: False,
    )
    result = await manager.start("i-1", _start_request())

    assert result == {"status": "accepted"}
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.ERROR
    assert "显存不足" in (rec.state_message or "")


async def test_start_after_error_retryable_false_rebuilds(manager, monkeypatch):
    """Gate3 #1：拒启类 ERROR（retryable=False）显式 start 可重建为 STARTING。

    死锁场景回归：模型缺失/显存不足 ERROR → 用户修复 → server 手动 start →
    worker 必须重建而非幂等 accepted（否则实例永久 error）。
    """
    launched: list[str] = []

    # 第一次 start：显存不足 → ERROR + retryable=False + 占用端口
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._check_vram",
        lambda self, gpu_indexes, claim: False,
    )
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)
    await manager.start("i-1", _start_request())
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.ERROR
    assert rec.retryable is False
    assert 18080 not in manager._assigned_ports  # 显存不足路径在端口分配前返回，无占用

    # 第二次 start：显存充足（_check_vram True）→ 重建 STARTING + 端口重分配
    async def _fake_launch(self, record):
        launched.append(record.instance_id)

    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._check_vram",
        lambda self, gpu_indexes, claim: True,
    )
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_container",
        _fake_launch,
    )
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 19090)

    result = await manager.start("i-1", _start_request())
    assert result == {"status": "accepted"}
    new_rec = manager._instances["i-1"]
    assert new_rec.state == WorkerInstanceStateEnum.STARTING
    assert new_rec.retryable is True  # 重建后恢复可重试
    assert new_rec.port == 19090  # 新端口分配
    assert 19090 in manager._assigned_ports
    assert launched == ["i-1"]  # _launch_container 被调用


# ---------- stop ----------


async def test_stop_idempotent_missing(manager):
    result = await manager.stop("not-exist")
    assert result == {"status": "accepted"}


async def test_stop_terminates_and_cleans(manager, monkeypatch):
    """stop：走 stop_container（docker stop 容器）→ 记录清除 + 端口释放 + meta 删除。"""
    stopped: list[str] = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 2000
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)
    monkeypatch.setattr(
        "app.worker.lifecycle.stop_container",
        lambda name: stopped.append(name) or True,
    )

    await manager.start("i-1", _start_request())
    meta_path = manager._config.WORKER_LOG_DIR / "instances" / "i-1.json"
    assert meta_path.exists()  # start 写了 meta

    result = await manager.stop("i-1")
    assert result == {"status": "accepted"}
    # start 时 _launch_container 入口已清理一次（#1），stop 再清理一次 → 至少一次走 docker stop
    assert "vllm-i-1" in stopped
    assert stopped.count("vllm-i-1") >= 1
    assert "i-1" not in manager._instances
    assert 18080 not in manager._assigned_ports
    assert not meta_path.exists()


async def test_stop_docker_unavailable_keeps_record_and_raises(manager, monkeypatch):
    """docker 不可用（stop_container 返回 False）→ 不谎报停止：抛 ExternalServiceException、
    记录保留且状态恢复调用前、meta 未删、端口未释放、快照仍含该实例（防对账收敛 stopped
    → 显存账本漂移/超卖）。"""
    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopenSimple)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)
    await manager.start("i-1", _start_request())
    rec = manager._instances["i-1"]
    rec.state = WorkerInstanceStateEnum.RUNNING  # 调用前为可上报状态
    meta_path = manager._config.WORKER_LOG_DIR / "instances" / "i-1.json"
    assert meta_path.exists()

    monkeypatch.setattr("app.worker.lifecycle.stop_container", lambda name: False)
    with pytest.raises(ExternalServiceException):
        await manager.stop("i-1")

    # 记录仍在，状态恢复为调用前（RUNNING，STOPPING 不上报 → 必须恢复）
    assert "i-1" in manager._instances
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.RUNNING
    assert "docker" in (manager._instances["i-1"].state_message or "")
    assert meta_path.exists()  # meta 未删除
    assert 18080 in manager._assigned_ports  # 端口未释放
    items = await manager.snapshot_for_report()
    assert "i-1" in [i["id"] for i in items]  # 快照再次包含该实例（可继续对账）


async def test_launch_container_stop_false_ignored(manager, monkeypatch):
    """_launch_container 入口 stop_container 返回 False（docker 不可用）→ 返回值被忽略，
    不抛错、docker run 照常拉起（区别于 stop() 的守卫语义，入口清理仅是防同名冲突）。"""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 999
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.stop_container", lambda name: False)
    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)

    result = await manager.start("i-1", _start_request())
    assert result == {"status": "accepted"}
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.STARTING
    assert captured["cmd"][0] == "docker" and captured["cmd"][1] == "run"


# ---------- sync 状态机 ----------


def _record(**overrides) -> WorkerInstanceRecord:
    base = {
        "instance_id": "i-1",
        "state": WorkerInstanceStateEnum.STARTING,
        "port": 18080,
        "gpu_indexes": [0],
        "vram_claim": 16 * 1024**3,
        "spec": {"model_name": "qwen2.5"},
        "pid": 3000,
        "process": _FakePopenSimple(),
        "last_restart_time": datetime.now(),
    }
    base.update(overrides)
    return WorkerInstanceRecord(**base)


async def test_sync_starting_to_running(manager, monkeypatch):
    """健康检查 200 → starting → running + state_message 清空。"""
    manager._instances["i-1"] = _record()
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._health_ok",
        _health_true,
    )
    await manager.sync()
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.RUNNING
    assert rec.fail_count == 0


async def test_sync_starting_timeout_to_error(manager, monkeypatch):
    """starting 超时（总时长 > 30s）→ error「启动超时」。"""
    rec = _record(last_restart_time=datetime.now() - timedelta(seconds=60))
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._health_ok",
        _health_false,
    )
    await manager.sync()
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.ERROR
    assert "启动超时" in (manager._instances["i-1"].state_message or "")


async def test_sync_running_inactive_to_error(manager, monkeypatch):
    """running 失活（健康检查失败累计）→ error。"""
    rec = _record(state=WorkerInstanceStateEnum.RUNNING, fail_count=1)
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._health_ok",
        _health_false,
    )
    await manager.sync()
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.ERROR


async def test_sync_error_backoff_restart(manager, monkeypatch):
    """error 且退避到期 → restart_count+1、backoff 递增、置回 starting。"""
    launched: list[str] = []

    async def _fake_launch(self, record):
        launched.append(record.instance_id)

    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        backoff_count=2,  # delay = min(10·2^1, 300) = 20s
        last_restart_time=datetime.now() - timedelta(seconds=30),  # 已过 delay
    )
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_container",
        _fake_launch,
    )

    await manager.sync()
    new_rec = manager._instances["i-1"]
    assert new_rec.state == WorkerInstanceStateEnum.STARTING
    assert new_rec.restart_count == 1
    assert new_rec.backoff_count == 3


async def test_sync_error_backoff_not_due(manager, monkeypatch):
    """error 但退避未到期 → 不重启。"""
    launched: list[str] = []

    async def _fake_launch(self, record):
        launched.append(record.instance_id)

    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        backoff_count=3,  # delay = min(10·2^2, 300) = 40s
        last_restart_time=datetime.now(),  # 刚失败
    )
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_container",
        _fake_launch,
    )
    await manager.sync()
    assert launched == []
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.ERROR


async def test_backoff_delay_formula(manager):
    rec = _record(backoff_count=0)
    assert manager._backoff_delay(rec) == 0
    rec2 = _record(backoff_count=1)
    assert manager._backoff_delay(rec2) == 10
    rec3 = _record(backoff_count=6)  # min(10·2^5, 300)=300
    assert manager._backoff_delay(rec3) == 300


# ---------- snapshot / restore ----------


async def test_snapshot_skips_stopping(manager):
    manager._instances["i-1"] = _record(state=WorkerInstanceStateEnum.RUNNING)
    manager._instances["i-2"] = _record(
        instance_id="i-2", state=WorkerInstanceStateEnum.STOPPING
    )
    items = await manager.snapshot_for_report()
    ids = [i["id"] for i in items]
    assert "i-1" in ids
    assert "i-2" not in ids  # stopping 不上报（server 枚举无此值）


async def test_restore_running_only(manager, monkeypatch):
    """restore：docker inspect 容器 running → RUNNING；容器不存在/非 running → 删 meta。"""
    meta_dir = manager._config.WORKER_LOG_DIR / "instances"
    meta_dir.mkdir(parents=True)
    (meta_dir / "i-1.json").write_text(
        json.dumps(
            {
                "instance_id": "i-1",
                "pid": 5000,
                "port": 18080,
                "gpu_indexes": [0],
                "spec": {"model_name": "qwen2.5"},
                "model_path": str(manager._config.WORKER_MODEL_ROOT / "qwen2.5"),
                "restart_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (meta_dir / "i-2.json").write_text(
        json.dumps({"instance_id": "i-2", "pid": 999999}), encoding="utf-8"
    )

    monkeypatch.setattr(
        "app.worker.lifecycle.inspect_container",
        lambda name: (
            {"State": {"Status": "running"}} if name == "vllm-i-1" else None
        ),
    )
    await manager.restore()

    assert "i-1" in manager._instances
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.RUNNING
    assert manager._instances["i-1"].restart_count == 1
    assert manager._instances["i-1"].model_path is not None
    assert 18080 in manager._assigned_ports
    assert "i-2" not in manager._instances  # 容器不存在，不恢复
    assert not (meta_dir / "i-2.json").exists()  # 非 running meta 删除


# ---------- weight ----------


async def test_weight_sums_files(manager, tmp_path):
    model_dir = tmp_path / "models" / "qwen2.5"
    (model_dir / "model.safetensors").write_bytes(b"a" * 100)
    (model_dir / "model.bin").write_bytes(b"b" * 50)
    (model_dir / "note.txt").write_text("x")  # 非权重忽略

    result = await manager.weight(WeightRequest(model_name="qwen2.5"))
    assert result == {"weight_bytes": 150}


async def test_weight_dir_missing_raises(manager):
    with pytest.raises(NotFoundException):
        await manager.weight(WeightRequest(model_name="not-exist"))


async def test_weight_empty_dir_zero(manager, tmp_path):
    (tmp_path / "models" / "empty").mkdir()
    result = await manager.weight(WeightRequest(model_name="empty"))
    assert result == {"weight_bytes": 0}


# ---------- S8 回归测试 ----------


async def test_error_not_retryable_not_restarted(manager, monkeypatch):
    """S8 #1：拒启类 ERROR（retryable=False）不被 _maybe_restart 自动拉起。"""
    launched: list[str] = []

    async def _fake_launch(self, record):
        launched.append(record.instance_id)

    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        retryable=False,  # 显存不足/模型缺失落 ERROR
        last_restart_time=datetime.now(),  # 已过 delay=0（首次立即）
    )
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_container",
        _fake_launch,
    )
    await manager.sync()
    assert launched == []
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.ERROR


async def test_restart_cleans_stale_container_first(manager, monkeypatch):
    """#1 崩溃恢复回归：容器 crash 后 docker CLI 退出（poll 非 None）→ 重启时
    _launch_container 入口先 stop_container 清理同名残留容器（Exited），再 docker run
    ——防「Exited 残留容器 → docker run 同名冲突 → CLI 立退 → 无限循环」。"""
    calls: list[str] = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append("run")
            self.pid = 6000
            self._code = None

        def poll(self):
            return self._code

    def _fake_stop(name):
        calls.append("stop:" + name)
        return True

    monkeypatch.setattr("app.worker.lifecycle.stop_container", _fake_stop)
    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        retryable=True,
        process=_FakePopenSimple(),  # 模拟 CLI 已退出（poll 非 None 场景下 pre-kill 曾失效）
        pid=4000,
        model_path=str(manager._config.WORKER_MODEL_ROOT / "qwen2.5"),
        last_restart_time=datetime.now(),  # 首次立即重试
    )
    rec.process._code = 1  # type: ignore[union-attr] - 模拟 docker CLI 已退出（容器 crash 后必然退出）
    manager._instances["i-1"] = rec

    await manager.sync()
    # 无条件清理（stop_container）先于 docker run；重启置回 starting
    assert calls == ["stop:vllm-i-1", "run"]
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.STARTING
    assert manager._instances["i-1"].restart_count == 1


async def test_restore_corrupt_meta_cleaned(manager, monkeypatch):
    """S8 #3：损坏 meta（JSONDecodeError）不抛且删除文件。"""
    meta_dir = manager._config.WORKER_LOG_DIR / "instances"
    meta_dir.mkdir(parents=True)
    corrupt = meta_dir / "i-bad.json"
    corrupt.write_text("{ not valid json", encoding="utf-8")

    await manager.restore()  # 不抛
    assert not corrupt.exists()  # 损坏 meta 删除


# ---------- Gate 2 修复回归 ----------


async def test_sync_starting_cli_exited_to_error(manager, monkeypatch):
    """#2 快速失败：starting 期间 docker CLI 已退出（容器必然结束，如镜像缺失立即退）
    → 直接 error + 退出码诊断，不再空耗启动超时。"""
    proc = _FakePopenSimple()
    proc._code = 125  # docker run 立即失败退出
    manager._instances["i-1"] = _record(process=proc, pid=999)
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._health_ok",
        _health_true,  # 不应触达（先判 CLI 退出）
    )
    await manager.sync()
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.ERROR
    assert "code=125" in (rec.state_message or "")


async def test_sync_starting_cli_exit_code_125_retryable_false(manager, monkeypatch):
    """S5：CLI 快速失败 code=125（docker daemon 错误，如镜像缺失）→ retryable=False。"""
    proc = _FakePopenSimple()
    proc._code = 125
    rec = _record(process=proc, pid=999, retryable=True)
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._health_ok",
        _health_true,  # 不应触达（先判 CLI 退出）
    )
    await manager.sync()
    assert manager._instances["i-1"].retryable is False  # 不再无限退避重启


async def test_sync_starting_cli_exit_business_code_keeps_retryable(manager, monkeypatch):
    """S5：CLI 退出 code=1（容器内业务错误，如模型加载失败）→ 保持 retryable=True 可恢复。"""
    proc = _FakePopenSimple()
    proc._code = 1
    rec = _record(process=proc, pid=999, retryable=True)
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._health_ok",
        _health_true,  # 不应触达（先判 CLI 退出）
    )
    await manager.sync()
    assert manager._instances["i-1"].retryable is True  # 可退避恢复


async def test_start_docker_missing_error_retryable_false(manager, monkeypatch):
    """#2：Popen 抛 FileNotFoundError（docker CLI 缺失）→ ERROR + retryable=False + 受理 200。"""
    def _raise(*args, **kwargs):
        raise FileNotFoundError("docker 未安装")

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _raise)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)
    result = await manager.start("i-1", _start_request())
    assert result == {"status": "accepted"}
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.ERROR
    assert rec.retryable is False
    assert "docker" in (rec.state_message or "")  # 可读原因


async def test_restart_docker_missing_sets_retryable_false(manager, monkeypatch):
    """#2：退避重启路径 Popen 抛 FileNotFoundError → retryable=False（不再无限重试）。"""
    def _raise(*args, **kwargs):
        raise FileNotFoundError("docker 未安装")

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _raise)
    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        retryable=True,
        model_path=str(manager._config.WORKER_MODEL_ROOT / "qwen2.5"),
        last_restart_time=datetime.now(),  # 首次立即重试
    )
    manager._instances["i-1"] = rec
    await manager.sync()
    new_rec = manager._instances["i-1"]
    assert new_rec.state == WorkerInstanceStateEnum.ERROR
    assert new_rec.retryable is False  # docker 缺失 → 永久性


async def test_start_model_outside_root_error(manager, monkeypatch):
    """S6：绝对路径模型在 WORKER_MODEL_ROOT 外（容器挂载不可见）→ ERROR + 可读 message。"""
    outside = manager._config.WORKER_MODEL_ROOT.parent / "outside-model"
    outside.mkdir(parents=True, exist_ok=True)
    result = await manager.start("i-1", _start_request(model_name=str(outside)))
    assert result == {"status": "accepted"}
    rec = manager._instances["i-1"]
    assert rec.state == WorkerInstanceStateEnum.ERROR
    assert rec.retryable is False
    assert "不在挂载目录" in (rec.state_message or "")


async def test_error_outside_root_not_auto_restarted(manager, monkeypatch):
    """S6：根外模型 ERROR 不自动重启（等用户 start 修复移入挂载目录）。"""
    launched: list[str] = []

    async def _fake_launch(self, record):
        launched.append(record.instance_id)

    outside = manager._config.WORKER_MODEL_ROOT.parent / "outside-model"
    outside.mkdir(parents=True, exist_ok=True)
    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        retryable=True,
        model_path=str(outside),
        spec={"model_name": str(outside)},
        last_restart_time=datetime.now(),
    )
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_container",
        _fake_launch,
    )
    await manager.sync()
    assert launched == []
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.ERROR


async def test_start_concurrent_stop_no_resurrect(manager, monkeypatch):
    """S8：_launch_container 入口 stop_container（await 释放锁）窗口内 stop 删除记录
    → 二次校验拦截 Popen、start 不复活记录（防孤儿拉起/端口复用）。"""
    launched: list[str] = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append("run")  # 不应发生（S8 二次校验拦截）
            self.pid = 7000
            self._code = None

        def poll(self):
            return self._code

    def _fake_stop(name):
        # 模拟锁释放窗口内 stop() 已删除记录并释放端口
        manager._instances.pop("i-1", None)
        manager._assigned_ports.discard(18080)
        return True

    monkeypatch.setattr("app.worker.lifecycle.stop_container", _fake_stop)
    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)

    await manager.start("i-1", _start_request())
    assert launched == []  # Popen 未执行
    assert "i-1" not in manager._instances  # 记录不复活
    assert 18080 not in manager._assigned_ports  # 端口已释放不回收


async def test_restore_inspect_exception_keeps_meta(manager, monkeypatch):
    """S4：restore 时 inspect 探针异常（docker 命令缺失/daemon 不可达）→ 保留 meta，
    不误判容器消失（防误删 meta → 实例被对账 stopped → 显存超卖）。"""
    meta_dir = manager._config.WORKER_LOG_DIR / "instances"
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "i-1.json"
    meta_path.write_text(
        json.dumps({"instance_id": "i-1", "pid": 5000}), encoding="utf-8"
    )

    def _raise(name):
        raise FileNotFoundError("docker 命令缺失")

    monkeypatch.setattr("app.worker.lifecycle.inspect_container", _raise)
    await manager.restore()  # 不抛
    assert meta_path.exists()  # meta 保留
    assert "i-1" not in manager._instances
