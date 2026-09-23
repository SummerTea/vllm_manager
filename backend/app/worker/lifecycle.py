"""Worker 实例生命周期管理（内存态状态机 + docker 容器启动/停止/健康检查/退避重启/restore）。

Phase B 容器化：实例启动由「Popen vllm serve」改为「Popen docker run」——
- start 指令携带完整模板快照（StartRequest.template），worker 渲染
  （{port}/{model_path}/{gpu_indexes} 由 worker 填）；缺省用内置
  DEFAULT_VLLM_RUN_TEMPLATE 兜底（含存量实例）
- 渲染安全：模板 format_map → shlex.split → argv 交 Popen（无 shell）
- 停止走 docker stop -t 3 → kill → rm（**绝不 killpg docker CLI**——会杀 CLI 不杀
  容器变孤儿）；restore 用 docker inspect 判定容器存活
- 健康检查不变（--network host，127.0.0.1:port 直连）

与 server 对账闭环的关系（docs/worker-contract.md §3）：
- worker 内存态：pending → starting → running / error（+ stopping 过渡，上报时跳过）
- 快照经 /instances/report 上报，server 按 apply_report 规则对账（M2 守卫/B1 竞态）
- stopped 为终态：restore 仅恢复 running，stopped 不恢复；重新拉起唯一入口是 server start

对齐 gpustack serve_manager.py：_start_model_instance(1291) / is_ready(1741) /
_restart_error_model_instance(1613) / _assign_ports(1414) / _get_numbered_log_path(890)。
"""

import asyncio
import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from app.worker.config import WorkerConfig
from app.worker.enum import WorkerInstanceStateEnum
from app.worker.process_utils import (
    DEFAULT_VLLM_RUN_TEMPLATE,
    _sum_weight_files,
    build_context,
    build_env_args,
    build_mount_args,
    build_vllm_command,
    get_free_port,
    inspect_container,
    instance_log_path,
    instance_meta_path,
    join_arg_fragment,
    open_log_file,
    render_docker_command,
    resolve_model_path,
    stop_container,
)
from app.worker.schema import StartRequest, WeightRequest

logger = logging.getLogger(__name__)

# 健康检查失败阈值与启动总预算（docs/worker-contract.md §3）
# 5s sync 周期 × 2 ≈ 10s 快速失败；30s 总预算兜底（模型加载超长时）
_STARTUP_FAIL_THRESHOLD = 2
_STARTUP_TIMEOUT_SECONDS = 30
# RUNNING 失活阈值（语义独立于 starting 启动失败，值相同：5s×2≈10s）
_RUNTIME_FAIL_THRESHOLD = 2


@dataclass
class WorkerInstanceRecord:
    """worker 内存态实例记录。"""

    instance_id: str
    state: WorkerInstanceStateEnum = WorkerInstanceStateEnum.PENDING
    state_message: str | None = None
    port: int | None = None
    gpu_indexes: list[int] = field(default_factory=list)
    vram_claim: int = 0
    spec: dict = field(default_factory=dict)
    model_path: str | None = None
    restart_count: int = 0
    backoff_count: int = 0
    last_restart_time: datetime | None = None
    pid: int | None = None
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    fail_count: int = 0
    retryable: bool = True
    """是否允许退避自动重启（False=永久性配置/资源错误，重试无意义）。

    拒启类 ERROR（模型缺失/显存不足/二进制缺失）置 False，sync 不自动拉起，
    防绕过显存双保险；启动后 crash/健康超时置 True，可退避重启。
    """


class InstanceLifecycleManager:
    """实例生命周期管理器（worker 内存态，单进程内权威）。"""

    def __init__(self, config: WorkerConfig, node_id: str, token: str):
        self._config = config
        self._node_id = node_id
        self._token = token
        self._instances: dict[str, WorkerInstanceRecord] = {}
        self._assigned_ports: set[int] = set()
        self._lock = asyncio.Lock()
        self._health_http = httpx.AsyncClient(timeout=1.0)

    # ---------- 启动 ----------

    async def start(self, instance_id: str, request: StartRequest) -> dict:
        """受理 start 指令（同步完成，始终返回 accepted——契约 §2.2 2xx=指令已受理）。

        幂等：已存在 STARTING/RUNNING/STOPPING record → 直接返回 accepted，不重建。
        ERROR record（含 retryable=False 拒启类）→ **允许重建**：释放旧端口后走完整
        start 流程（覆盖旧 record，保留 spec/restart_count）——用户显式 start 可修复
        模型缺失/显存不足等拒启实例（区别于 sync 自动重启，retryable 守卫不拦用户操作）。
        失败（模型不存在/显存不足）→ 建 ERROR record + state_message，经 report 上报。
        """
        async with self._lock:
            existing = self._instances.get(instance_id)
            if existing is not None:
                if existing.state in (
                    WorkerInstanceStateEnum.STARTING,
                    WorkerInstanceStateEnum.RUNNING,
                    WorkerInstanceStateEnum.STOPPING,
                ):
                    return {"status": "accepted"}
                # ERROR 重建：释放旧端口，避免端口占用/漂移
                if existing.port is not None:
                    self._assigned_ports.discard(existing.port)

            record = WorkerInstanceRecord(
                instance_id=instance_id,
                gpu_indexes=list(request.gpu_indexes),
                vram_claim=request.vram_claim,
                spec=request.spec.model_dump(),
            )
            # 模板快照（StartRequest 顶层）落入 spec：随 meta 持久化、退避重启复用；
            # _launch_container 取 spec["template"] 缺省用内置默认模板兜底（存量实例）
            if request.template:
                record.spec["template"] = request.template
            # 提前登记（PENDING）：使 _launch_container 的 S8 二次校验（await 窗口
            # stop 可能删除记录）对 start 路径同样生效；stop 并发删记录后不复活
            self._instances[instance_id] = record

            # 1. 模型路径解析
            try:
                model_path = resolve_model_path(
                    self._config.WORKER_MODEL_ROOT, request.spec.model_name
                )
            except Exception as e:  # noqa: BLE001 - 模型不存在落 ERROR 而非 500
                record.state = WorkerInstanceStateEnum.ERROR
                record.retryable = False  # 永久性配置错误，重试无意义
                record.state_message = str(e)
                return {"status": "accepted"}

            # S6：根外绝对路径模型容器挂载不可见 → 拒启（与 _launch_container 校验一致，
            #     早失败省 docker 调用）
            if not model_path.resolve().is_relative_to(
                self._config.WORKER_MODEL_ROOT.resolve()
            ):
                record.state = WorkerInstanceStateEnum.ERROR
                record.retryable = False
                record.state_message = (
                    "模型路径不在挂载目录 WORKER_MODEL_ROOT 内，容器内不可见: "
                    f"{model_path}"
                )
                return {"status": "accepted"}
            record.model_path = str(model_path)

            # 2. 显存校验（无 GPU 时跳过）
            if not self._check_vram(request.gpu_indexes, request.vram_claim):
                record.state = WorkerInstanceStateEnum.ERROR
                record.retryable = False  # 拒启：防退避重启绕过显存双保险
                record.state_message = (
                    "显存不足：目标卡空闲显存 < 需求 "
                    f"{request.vram_claim} Bytes（卡 {request.gpu_indexes}）"
                )
                return {"status": "accepted"}

            # 3. 端口分配
            port = get_free_port(unavailable=self._assigned_ports)
            record.port = port
            self._assigned_ports.add(port)

            # 4. 启动容器
            try:
                await self._launch_container(record)
            except Exception as e:  # noqa: BLE001 - 启动失败落 ERROR
                logger.exception("实例 %s 启动失败", instance_id)
                record.state = WorkerInstanceStateEnum.ERROR
                record.retryable = False  # Popen 即失败（如 docker 未装/镜像缺失）→ 永久性
                record.state_message = f"启动失败: {e}"
                return {"status": "accepted"}

            # S8 配套：_launch_container 内 await（stop_container）释放锁期间 stop 可能已
            # 删除该记录 → 不复活（容器未拉起或已被清理，端口由 stop 释放）
            if self._instances.get(instance_id) is not record:
                return {"status": "accepted"}
            record.state = WorkerInstanceStateEnum.STARTING
            return {"status": "accepted"}

    async def _launch_container(self, record: WorkerInstanceRecord) -> None:
        """渲染 docker run 命令、Popen 启动、写 meta（start 与退避重启复用）。

        - template = spec["template"]（start 快照）或内置 DEFAULT_VLLM_RUN_TEMPLATE 兜底
        - build_vllm_command 产出 [vllm_bin, serve, model_path, ...args]，模板已含
          {vllm_bin} serve {model_path}，仅取 args 部分（第 3 元素后）经 shlex.join
          作为 {args} 片段
        - env 经 -e 传入（不再 Popen env）；GPU 经 --gpus 透传（不再 CUDA_VISIBLE_DEVICES）
        - 渲染后 argv 交 Popen（**无 shell**）；记录 docker CLI pid 供探活/日志

        容器可靠性（Gate 2）：
        - **#1 入口无条件 `stop_container(name)`**：docker CLI 存活 ≠ 容器存活——容器
          crash 后 CLI 退出（poll 非 None），残留 Exited 容器导致 docker run 同名冲突
          无限循环；统一在拉起前 docker stop → rm 清理同名残留（同时覆盖原 pre-kill，
          运行中容器一并停止，start 幂等路径不会走到此）
        - **S8 await 窗口二次校验**：入口 stop_container 经 asyncio.to_thread 释放锁，
          stop() 可能已删除该记录/释放端口 → Popen 前防御重校验，不拉起孤儿
        - **S6 模型路径必须在挂载目录内**：绝对路径在 WORKER_MODEL_ROOT 外 → 容器内
          不可见，拒启（抛 ValueError，start 转 ERROR / restart 保持 error）
        """
        spec = record.spec
        assert record.port is not None
        assert record.model_path is not None
        name = f"vllm-{record.instance_id}"
        template = spec.get("template") or DEFAULT_VLLM_RUN_TEMPLATE
        model_path = Path(record.model_path)

        # S6：仅挂载 WORKER_MODEL_ROOT/WORKER_CACHE_DIR，根外绝对路径容器内不可见 → 拒启
        if not model_path.resolve().is_relative_to(
            self._config.WORKER_MODEL_ROOT.resolve()
        ):
            raise ValueError(
                "模型路径不在挂载目录 WORKER_MODEL_ROOT 内，容器内不可见: "
                f"{record.model_path}"
            )

        # #1：无条件清理同名残留容器（Exited/运行中 → docker stop → rm），防同名冲突死循环
        await asyncio.to_thread(stop_container, name)
        # S8：await 窗口 stop 可能已删除该记录（锁释放）→ 不拉起孤儿容器
        if self._instances.get(record.instance_id) is not record:
            return

        # args 片段：仅取 build_vllm_command 的 args 部分（模板已含 vllm_bin/serve/model_path）
        full_cmd = build_vllm_command(
            self._config.WORKER_VLLM_BIN,
            model_path,
            spec,
            record.port,
            self._config.INSTANCE_DEFAULT_GMU,
        )
        args_fragment = join_arg_fragment(full_cmd[3:])

        # env -e 片段（OMP/SAFETENSORS/VLLM_CACHE_ROOT；GPU 由 --gpus 透传）
        env_items = {"OMP_NUM_THREADS": "1", "SAFETENSORS_FAST_GPU": "1"}
        cache_vllm = self._config.WORKER_CACHE_DIR / "vllm"
        try:
            cache_vllm.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("VLLM_CACHE_ROOT 目录创建失败: %s", cache_vllm)
        else:
            env_items["VLLM_CACHE_ROOT"] = str(cache_vllm)
        env_args = build_env_args(env_items)

        # mount -v 片段（模型根/缓存同路径挂载，容器内路径一致）
        mount_args = build_mount_args(
            [
                (
                    str(self._config.WORKER_MODEL_ROOT),
                    str(self._config.WORKER_MODEL_ROOT),
                ),
                (
                    str(self._config.WORKER_CACHE_DIR),
                    str(self._config.WORKER_CACHE_DIR),
                ),
            ]
        )

        context = build_context(
            name=name,
            image=self._config.WORKER_VLLM_IMAGE,
            vllm_bin=self._config.WORKER_VLLM_BIN,
            model_path=record.model_path,
            port=str(record.port),
            gpu_indexes=",".join(str(i) for i in record.gpu_indexes),
            shm_size=f"{self._config.WORKER_VLLM_SHM_SIZE_GIB:g}g",
            args_fragment=args_fragment,
            env_args=env_args,
            mount_args=mount_args,
        )

        cmd = render_docker_command(template, context)

        log_path = instance_log_path(
            self._config.WORKER_LOG_DIR, record.instance_id, record.restart_count
        )
        log_file = open_log_file(log_path)
        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=False,
            )
        except Exception:
            log_file.close()  # Popen 失败显式关闭句柄，不依赖 refcount 回收
            raise
        record.process = process
        record.pid = process.pid
        record.log_path = log_path
        record.last_restart_time = datetime.now()
        self._write_meta(record)

    def _write_meta(self, record: WorkerInstanceRecord) -> None:
        """写实例 meta json（restore 用）。"""
        meta = {
            "instance_id": record.instance_id,
            "pid": record.pid,
            "port": record.port,
            "gpu_indexes": record.gpu_indexes,
            "spec": record.spec,
            "model_path": record.model_path,
            "restart_count": record.restart_count,
        }
        path = instance_meta_path(
            self._config.WORKER_LOG_DIR, record.instance_id
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("实例 meta 写入失败: %s", path)

    # ---------- 停止 ----------

    async def stop(self, instance_id: str) -> dict:
        """停止实例（幂等：不存在 → accepted）。

        容器化：走 `docker stop -t 3 → kill → rm`（stop_container），**绝不 killpg
        docker CLI**（会杀 CLI 不杀容器 → 容器变孤儿）；docker CLI 进程在容器停止后
        自然退出。清内存记录/端口/meta → report 消失 → server 收敛 stopped。

        已知边缘（S9 注释，worker 侧仅记录）：worker 收到 stop 后未停容器即崩溃
        → meta+容器存活 → 重启后 restore 报 running，server target=stopping 悬挂
        ——属 server 侧收敛逻辑待补，worker 侧不处理。
        """
        async with self._lock:
            record = self._instances.get(instance_id)
            if record is None:
                return {"status": "accepted"}

            record.state = WorkerInstanceStateEnum.STOPPING
            await asyncio.to_thread(stop_container, f"vllm-{instance_id}")

            self._instances.pop(instance_id, None)
            if record.port is not None:
                self._assigned_ports.discard(record.port)
            meta_path = instance_meta_path(
                self._config.WORKER_LOG_DIR, instance_id
            )
            try:
                meta_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("实例 meta 删除失败: %s", meta_path)
            logger.info("实例 %s 已停止", instance_id)
            return {"status": "accepted"}

    # ---------- 权重查询 ----------

    async def weight(self, request: WeightRequest) -> dict:
        """扫描模型目录权重文件求和（.safetensors/.bin/.pt/.pth）。

        404 广播语义（S9 注释）：server 并发向所有候选节点广播，本节点无此模型
        （404）即当失败跳过；全部节点失败 server 抛 ExternalServiceException。
        """
        model_path = resolve_model_path(
            self._config.WORKER_MODEL_ROOT, request.model_name
        )
        total = _sum_weight_files(model_path)
        return {"weight_bytes": total}

    # ---------- 对账快照 ----------

    async def snapshot_for_report(self) -> list[dict]:
        """活跃实例快照（跳过 STOPPING——server 枚举无此值会 422）。

        锁内取值（S5）：消除依赖隐式不变量的隐患；report_loop 为 async 调用方。
        """
        async with self._lock:
            records = list(self._instances.values())
        items = []
        for record in records:
            if record.state == WorkerInstanceStateEnum.STOPPING:
                continue
            items.append(
                {
                    "id": record.instance_id,
                    "state": record.state.value,
                    "state_message": record.state_message,
                    "port": record.port,
                    "restart_count": record.restart_count,
                }
            )
        return items

    # ---------- sync 周期对账 ----------

    async def sync(self) -> None:
        """周期权威判定（遍历快照避免迭代中修改）。

        读-写间无 await 不变量（S4）：从读取 record.state 到 Popen/状态写入
        之间不得插入 await——否则 stop 可在中途删除记录/释放端口，导致孤儿
        拉起+端口复用竞态。_maybe_restart 变更前锁内防御重校验兜底。
        """
        async with self._lock:
            snapshot = list(self._instances.values())
        now = datetime.now()
        for record in snapshot:
            state = record.state
            if state == WorkerInstanceStateEnum.STARTING:
                # #2 快速失败：docker CLI 已退出（poll 非 None）→ 容器必然结束
                # （docker run 前台 attach，CLI 退出 = 容器退出；镜像缺失/拉取失败等
                # 会立即退出而非空耗启动超时）→ 直接落 error + 退出码诊断
                code = record.process.poll() if record.process is not None else None
                if code is not None:
                    record.state = WorkerInstanceStateEnum.ERROR
                    record.state_message = f"docker run 进程已退出（code={code}）"
                    # S5：docker 侧永久错误（125 daemon 错误/126 不可执行/127 命令不存在）
                    # → retryable=False 不再自动重启（镜像缺失/容器内命令不存在重试无意义）；
                    # 其余退出码（容器内业务错误，如模型加载失败）保持 retryable=True 可退避恢复
                    if code in (125, 126, 127):
                        record.retryable = False
                elif await self._health_ok(record):
                    record.state = WorkerInstanceStateEnum.RUNNING
                    record.state_message = None
                    record.fail_count = 0
                else:
                    record.fail_count += 1
                    if (
                        record.fail_count >= _STARTUP_FAIL_THRESHOLD
                        or (
                            record.last_restart_time is not None
                            and (now - record.last_restart_time).total_seconds()
                            > _STARTUP_TIMEOUT_SECONDS
                        )
                    ):
                        record.state = WorkerInstanceStateEnum.ERROR
                        record.state_message = "启动超时"
            elif state == WorkerInstanceStateEnum.RUNNING:
                if await self._health_ok(record):
                    record.fail_count = 0
                else:
                    record.fail_count += 1
                    if record.fail_count >= _RUNTIME_FAIL_THRESHOLD:
                        record.state = WorkerInstanceStateEnum.ERROR
                        record.state_message = "进程失活/健康检查失败"
            elif state == WorkerInstanceStateEnum.ERROR:
                await self._maybe_restart(record, now)

    async def _maybe_restart(self, record: WorkerInstanceRecord, now: datetime) -> None:
        """error 退避重启（无上限；首次立即重试，之后 min(10·2^(n-1), 300s)）。

        #1 retryable 守卫：拒启类 ERROR（模型缺失/显存不足/二进制缺失）不自动重启。
        #2 同名容器清理已并入 _launch_container 入口（无条件 stop_container），
           此处不再 pre-kill——崩溃残留/运行中旧容器统一由拉起前清理兜底。
        """
        if not record.retryable:
            return  # 永久性错误不重启（防绕过显存双保险）
        delay = self._backoff_delay(record)
        if record.last_restart_time is not None:
            elapsed = (now - record.last_restart_time).total_seconds()
            if elapsed < delay:
                return
        try:
            resolved = resolve_model_path(
                self._config.WORKER_MODEL_ROOT, record.spec.get("model_name", "")
            )
        except Exception:  # noqa: BLE001 - 模型仍缺失，保持 error
            return
        # S6：根外绝对路径模型容器内不可见（_launch_container 会拒启），不自动重启，
        #     等用户显式 start 修复（模型移入挂载目录）
        if not resolved.resolve().is_relative_to(
            self._config.WORKER_MODEL_ROOT.resolve()
        ):
            return
        # S4 防御重校验：读取与写入之间 stop 可能已删除该记录/释放端口
        if self._instances.get(record.instance_id) is not record:
            return
        record.restart_count += 1
        record.backoff_count += 1
        record.last_restart_time = now
        record.fail_count = 0
        try:
            await self._launch_container(record)
            record.state = WorkerInstanceStateEnum.STARTING
            record.state_message = None
            logger.info(
                "实例 %s 第 %s 次重启", record.instance_id, record.restart_count
            )
        except FileNotFoundError as e:  # docker CLI 缺失 → 永久性，不再自动重启
            logger.exception("实例 %s 重启失败（docker 不可用）", record.instance_id)
            record.state = WorkerInstanceStateEnum.ERROR
            record.retryable = False
            record.state_message = f"重启失败: {e}"
        except Exception as e:  # noqa: BLE001 - 其他重启失败保持 error
            logger.exception("实例 %s 重启失败", record.instance_id)
            record.state = WorkerInstanceStateEnum.ERROR
            record.state_message = f"重启失败: {e}"

    def _backoff_delay(self, record: WorkerInstanceRecord) -> float:
        """退避延迟：backoff_count==0 → 0（首次立即）；否则 min(10·2^(n-1), 300)。"""
        if record.backoff_count == 0:
            return 0
        return min(10 * (2 ** (record.backoff_count - 1)), 300)

    # ---------- 健康检查 ----------

    async def _health_ok(self, record: WorkerInstanceRecord) -> bool:
        """进程存活 && GET /v1/models 200（1s 超时）。"""
        if record.process is not None and record.process.poll() is not None:
            return False
        if record.port is None:
            return False
        try:
            resp = await self._health_http.get(
                f"http://127.0.0.1:{record.port}/v1/models"
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ---------- 显存校验 ----------

    def _check_vram(self, gpu_indexes: list[int], vram_claim: int) -> bool:
        """pynvml 目标卡空闲显存 ≥ vram_claim；pynvml 不可用/无 GPU → True（不拦截）。"""
        try:
            import pynvml  # type: ignore[import-not-found]
        except ImportError:
            return True
        try:
            pynvml.nvmlInit()
        except Exception:  # noqa: BLE001 - 无驱动降级
            return True
        try:
            for index in gpu_indexes:
                try:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                except Exception:  # noqa: BLE001 - 卡不存在降级不拦截
                    continue
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                free = int(mem.total) - int(mem.used)
                if free < vram_claim:
                    return False
            return True
        finally:
            with contextlib.suppress(Exception):  # shutdown 失败不影响结果
                pynvml.nvmlShutdown()

    # ---------- restore ----------

    async def restore(self) -> None:
        """worker 重启后恢复 running 实例（meta + docker inspect 容器存活）；stopped 不恢复。

        容器状态权威是 docker inspect（State.Status == running），非宿主机 pid 探活；
        容器不存在/Exited/dead → 删 meta（不恢复）。
        S4：inspect 探针异常（docker 命令缺失/daemon 不可达/超时/解析失败）≠ 容器消失
        ——保留 meta 并告警，避免 docker 短暂不可用时误删 meta → 实例被对账 stopped
        → 显存超卖；下轮/下次重启再试。
        """
        meta_dir = self._config.WORKER_LOG_DIR / "instances"
        if not meta_dir.is_dir():
            return
        for meta_path in meta_dir.glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                instance_id = meta["instance_id"]
                try:
                    inspected = await asyncio.to_thread(
                        inspect_container, f"vllm-{instance_id}"
                    )
                except Exception as e:  # noqa: BLE001 - 探针异常保留 meta（S4）
                    logger.warning(
                        "restore 检查容器 vllm-%s 失败，保留 meta 待下次重试: %s",
                        instance_id,
                        e,
                    )
                    continue
                if (
                    inspected is None
                    or inspected.get("State", {}).get("Status") != "running"
                ):
                    meta_path.unlink(missing_ok=True)
                    continue
                record = WorkerInstanceRecord(
                    instance_id=instance_id,
                    state=WorkerInstanceStateEnum.RUNNING,
                    port=meta.get("port"),
                    gpu_indexes=meta.get("gpu_indexes") or [],
                    vram_claim=0,
                    spec=meta.get("spec") or {},
                    model_path=meta.get("model_path"),
                    restart_count=meta.get("restart_count") or 0,
                    pid=meta.get("pid"),
                )
                self._instances[record.instance_id] = record
                if record.port is not None:
                    self._assigned_ports.add(record.port)
                logger.info(
                    "restore 实例 %s（容器 vllm-%s, port=%s）",
                    record.instance_id,
                    instance_id,
                    record.port,
                )
            except (json.JSONDecodeError, OSError, KeyError):
                # S8：损坏 meta 不抛，且删除文件（避免每次重启重复解析失败）
                logger.warning("实例 meta 解析失败，删除: %s", meta_path)
                with contextlib.suppress(OSError):
                    meta_path.unlink(missing_ok=True)

    # ---------- 清理 ----------

    async def aclose(self) -> None:
        """关闭健康检查 http client。"""
        await self._health_http.aclose()
