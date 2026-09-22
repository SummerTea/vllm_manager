"""Worker 实例生命周期契约测试（mock Popen/健康检查/显存）。

重点覆盖：start 受理即 200、task 映射、幂等、显存校验、stop 幂等、
sync 状态机、退避重启、restore 仅恢复 running、weight 求和。
"""

import json
from datetime import datetime, timedelta

import pytest

from app.exception import NotFoundException
from app.worker.config import WorkerConfig
from app.worker.enum import WorkerInstanceStateEnum
from app.worker.lifecycle import InstanceLifecycleManager, WorkerInstanceRecord
from app.worker.schema import StartRequest, VllmSpec, WeightRequest


def _cfg(**overrides) -> WorkerConfig:
    return WorkerConfig(**overrides)


def _start_request(**spec_overrides) -> StartRequest:
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
        gpu_indexes=[0],
        vram_claim=16 * 1024**3,
    )


class _FakePopenSimple:
    def __init__(self, cmd=None, **kwargs):
        self.pid = 1000
        self._code = None

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
    m = InstanceLifecycleManager(cfg, "node-1", "tok-1")
    return m


# ---------- start ----------


async def test_start_success_creates_starting_record(manager, monkeypatch):
    """start 受理成功：record STARTING、cmd 含 --port/--served-model-name/GMU。"""
    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]
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
    assert "18080" in captured["cmd"]  # --port 补全
    assert "--served-model-name" in captured["cmd"]
    assert "--gpu-memory-utilization" in captured["cmd"]
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "0"


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


async def test_start_idempotent_returns_accepted(manager, monkeypatch):
    """幂等：已存在 record（starting）→ accepted 且不重建。"""
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)
    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopenSimple)

    await manager.start("i-1", _start_request())
    calls_before = len(manager._instances)

    await manager.start("i-1", _start_request())
    assert len(manager._instances) == calls_before  # 不重建
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.STARTING


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
    async def _fake_launch(self, record, model_path):
        launched.append(record.instance_id)

    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._check_vram",
        lambda self, gpu_indexes, claim: True,
    )
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_process",
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
    assert launched == ["i-1"]  # _launch_process 被调用


# ---------- stop ----------


async def test_stop_idempotent_missing(manager):
    result = await manager.stop("not-exist")
    assert result == {"status": "accepted"}


async def test_stop_terminates_and_cleans(manager, monkeypatch):
    """stop 杀进程 → 记录清除 + 端口释放 + meta 删除（日志保留）。"""
    terminated: list[int] = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 2000
            self._code = None

        def poll(self):
            return self._code

    monkeypatch.setattr("app.worker.lifecycle.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("app.worker.lifecycle.get_free_port", lambda **kw: 18080)
    monkeypatch.setattr(
        "app.worker.lifecycle.terminate_process_tree",
        lambda pid: terminated.append(pid),
    )

    await manager.start("i-1", _start_request())
    meta_path = manager._config.WORKER_LOG_DIR / "instances" / "i-1.json"
    assert meta_path.exists()  # start 写了 meta

    result = await manager.stop("i-1")
    assert result == {"status": "accepted"}
    assert "i-1" not in manager._instances
    assert 18080 not in manager._assigned_ports
    assert not meta_path.exists()
    assert terminated == [2000]


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

    async def _fake_launch(self, record, model_path):
        launched.append(record.instance_id)

    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        backoff_count=2,  # delay = min(10·2^1, 300) = 20s
        last_restart_time=datetime.now() - timedelta(seconds=30),  # 已过 delay
    )
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_process",
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

    async def _fake_launch(self, record, model_path):
        launched.append(record.instance_id)

    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        backoff_count=3,  # delay = min(10·2^2, 300) = 40s
        last_restart_time=datetime.now(),  # 刚失败
    )
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_process",
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
    """restore：meta+psutil 存活 → RUNNING；进程死 → 删 meta。"""
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
                "restart_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (meta_dir / "i-2.json").write_text(
        json.dumps({"instance_id": "i-2", "pid": 999999}), encoding="utf-8"
    )

    monkeypatch.setattr(
        "app.worker.lifecycle.psutil.pid_exists", lambda pid: pid == 5000
    )
    await manager.restore()

    assert "i-1" in manager._instances
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.RUNNING
    assert manager._instances["i-1"].restart_count == 1
    assert 18080 in manager._assigned_ports
    assert "i-2" not in manager._instances  # 进程死，不恢复
    assert not (meta_dir / "i-2.json").exists()  # 死进程 meta 删除


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

    async def _fake_launch(self, record, model_path):
        launched.append(record.instance_id)

    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        retryable=False,  # 显存不足/模型缺失落 ERROR
        last_restart_time=datetime.now(),  # 已过 delay=0（首次立即）
    )
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_process",
        _fake_launch,
    )
    await manager.sync()
    assert launched == []
    assert manager._instances["i-1"].state == WorkerInstanceStateEnum.ERROR


async def test_restart_kills_stale_process_first(manager, monkeypatch):
    """S8 #2：重启前先杀存活旧进程（pre-kill），再 _launch_process。"""
    calls: list[str] = []
    live_proc = _FakePopenSimple()
    live_proc._code = None  # poll() -> None，进程存活

    async def _fake_launch(self, record, model_path):
        calls.append("launch")

    async def _fake_to_thread(fn, pid):
        calls.append("kill" + str(pid))
        return None

    rec = _record(
        state=WorkerInstanceStateEnum.ERROR,
        retryable=True,
        process=live_proc,
        pid=4000,
        last_restart_time=datetime.now(),  # 首次立即重试
    )
    manager._instances["i-1"] = rec
    monkeypatch.setattr(
        "app.worker.lifecycle.InstanceLifecycleManager._launch_process",
        _fake_launch,
    )
    monkeypatch.setattr(
        "app.worker.lifecycle.asyncio.to_thread", _fake_to_thread
    )

    await manager.sync()
    assert calls == ["kill4000", "launch"]  # pre-kill 先于拉起


async def test_restore_corrupt_meta_cleaned(manager, monkeypatch):
    """S8 #3：损坏 meta（JSONDecodeError）不抛且删除文件。"""
    meta_dir = manager._config.WORKER_LOG_DIR / "instances"
    meta_dir.mkdir(parents=True)
    corrupt = meta_dir / "i-bad.json"
    corrupt.write_text("{ not valid json", encoding="utf-8")

    await manager.restore()  # 不抛
    assert not corrupt.exists()  # 损坏 meta 删除
