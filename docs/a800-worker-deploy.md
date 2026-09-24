# A800 Worker 部署与服务层测试记录（G1）

> 目标：在 10.1.251.230（A800，主机名 master）部署 vllm_manager worker（:8100）并注册到本机 server（:8000）。
> 红线：只动自己创建的东西；gemma4 已获授权停止，其余容器一律不碰；代码/venv/日志全部落 `/nfsdata`。
> 本机 server 与 A800 worker 之间采用 **SSH 反向隧道**连通（VPN 单向，A800 无法直连本机 192.168.101.181）。

## 1. 环境基线（A800）

| 项 | 值 |
|----|----|
| 主机 | master，Ubuntu（内核 5.15.0-190-generic） |
| GPU | NVIDIA A800 80GB PCIe ×2（index 0/1） |
| 系统 python | 3.10.12（不足，见 §3 决策） |
| docker | 26.0.2（cgroup v2 / systemd） |
| 部署目录 | `/nfsdata/vllm_manager/` |
| venv | `/nfsdata/venvs/vllm_mgr_worker`（Python 3.11.16） |
| miniconda | `/nfsdata/vllm_manager/miniconda3`（Python 3.11.16，仅供本 worker 使用） |
| 日志 | `/nfsdata/vllm_manager/worker.log`、`venv_install.log`、`miniconda_install.log` |

## 2. 部署步骤与结果

1. **停 gemma4**（用户授权）：`docker inspect` 确认归属（gemma4-26b-a4b / vllm-openai:v0.28.0）→ `docker stop` → Exited (0)。
   GPU 变化：GPU1 73163 MiB used → **0 MiB used（完全释放）**；GPU0 保持 kronos 占用 56929 MiB。
2. **rsync 代码**（HEAD ae21d7f，排除 .venv/.git/tests/.env*/data/logs）→ `/nfsdata/vllm_manager/backend/`（107 files / 506KB）。
3. **Python 3.11 环境 + venv + 依赖**（详见 §3 决策）：
   `pip install -i https://mirrors.aliyun.com/pypi/simple/ fastapi "uvicorn[standard]" httpx tenacity pydantic pydantic-settings psutil python-dotenv sqlalchemy asyncpg redis uuid-utils nvidia-ml-py`
4. **import 验证**：`import app.worker.main` → `WORKER_IMPORT_OK`（3.11 环境，零代码改动）。
5. **连通性判定**：A800 直连 `192.168.101.181:8000` = 000（失败）→ 采用反向隧道：
   本机 `nohup ssh -N -R 8000:127.0.0.1:8000 ...`（PID 97170）→ A800 `curl http://127.0.0.1:8000/vllm_manager/api/v1/nodes` = **200**。
   `SERVER_URL=http://127.0.0.1:8000`。
6. **启动 worker**：PID 64575（记录于 `/nfsdata/vllm_manager/worker.pid`），env 见 §4。
7. **注册验证**：节点 `master` → `state=ready`（详见 §5）。

## 3. 关键决策记录

1. **Python 3.10 → 3.11 环境升级（而非改代码）**：3.10.12 下 worker import 失败，唯一阻塞点是
   `app/base/base_enum.py` 的 `from enum import StrEnum`（Python 3.11+ 特性，经 `app/base/__init__.py`
   聚合导出影响全部 base 模块）。诊断确认无其他 3.11 语法/依赖（无 typing.Self/tomllib/ExceptionGroup）。
   决策：**保持代码不动，装 Python 3.11.16**（与本机 backend venv 3.11.11 同大版本）。
2. **miniconda 下载源**：TUNA 镜像的 `Miniconda3-py311_26.7.1-1-Linux-x86_64.sh`（98M）**md5 校验失败、
   tar 损坏**（脚本报 `md5sum mismatch` + `tarfile.ReadError`）；改用官方源
   `repo.anaconda.com` 重下（183M，脚本内置校验通过）。华为云镜像 miniconda 目录为 SPA 页面无法直接列文件。
3. **路径冲突规避**：`/nfsdata/miniconda3` 为预存的 **root:root 700 他人目录**（非本 worker 创建，miniconda
   安装脚本因目录已存在拒绝安装）。**未触碰**，改装自有路径 `/nfsdata/vllm_manager/miniconda3`。
4. **pip 源**：统一使用 `https://mirrors.aliyun.com/pypi/simple/`（更正：不用华为云源）。
5. **pynvml 补齐**：首次启动 worker 时 `nvidia-ml-py` 未装（GPU 上报降级为空列表），补装后重启 worker，
   gpu_devices 正常上报 2 条。

## 4. Worker 启动环境（PID 64575）

```bash
WORKER_ACCELERATOR=gpu WORKER_MODEL_ROOT=/nfsdata/models \
WORKER_VLLM_IMAGE=harbor.cloud.com/vllm/vllm-openai:v0.26.0 \
WORKER_VLLM_SHM_SIZE_GIB=10 WORKER_ADVERTISE_ADDRESS=10.1.251.230 \
SERVER_URL=http://127.0.0.1:8000 WORKER_STARTUP_FAIL_THRESHOLD=40 \
WORKER_STARTUP_TIMEOUT_SECONDS=240 WORKER_LISTEN_PORT=8100 \
/nfsdata/venvs/vllm_mgr_worker/bin/python app/work_worker.py
```

## 5. 服务层验证证据（2026-09-24）

| 验证项 | 结果 |
|--------|------|
| server 就绪 | `GET /vllm_manager/api/v1/nodes` 本机 = 200 |
| 隧道连通 | A800 `curl http://127.0.0.1:8000/.../nodes` = 200 |
| server→worker 回连 | `curl http://10.1.251.230:8100/healthz` = 200（ADVERTISE_ADDRESS 生效） |
| 节点注册 | machine_id=master, hostname=master, ip=10.1.251.230, worker_port=8100, **state=ready**, unreachable=false |
| heartbeat | 持续刷新（15s 周期） |
| status.accelerator | `gpu` |
| gpu_devices | 2 条：index=0/1，均为 NVIDIA A800 80GB PCIe，memory_total=85899345920 |
| gpu_devices.memory_used | index0=60498313216（kronos 占用中），index1=804519936（gemma4 停后基本释放） |
| system_reserved | ram=0, vram=0（未配置预留） |
| 其他上报 | cpu(32核)/memory/swap/filesystem/uptime 正常 |

## 6. 环境现状（部署后）

> 本节约为 **G1 部署时点**的快照；G4 收尾后 A800 worker/隧道已停止、本地 server 回 127.0.0.1、CPU worker 恢复（`docs/local-testing.md` §8.5 运维注记）。

- **A800 进程**：worker 64575（uvicorn 0.0.0.0:8100）；隧道监听 127.0.0.1:8000（A800 侧）。
- **容器**：gemma4-26b-a4b **Exited (0)**（未恢复，用户明确不恢复）；pdf2md×3 / evalscope / qwen-image-2512 均 Up 未动。
- **磁盘**：/nfsdata 剩 144G（93%）；`/nfsdata/vllm_manager` 1.1G、venv 115M、miniconda3 ~1G。
- **本机**：隧道进程 97170（`/tmp/vllm_mgr_itest/a800_tunnel.log`）。

## 7. 后续 phase 注意事项

- G2/G3 起实例将用 vLLM 镜像 `harbor.cloud.com/vllm/vllm-openai:v0.26.0`，模型根 `/nfsdata/models`，
  模型 `Qwen/Qwen3-0.6B`（1.5G）已确认在。
- GPU 显存（实测语义，见 `docs/local-testing.md` §8.2）：allocator 记账**不消费外部占用（memory_used）**，first-fit **恒选 GPU0**（账面 80G 全空）、GPU1 空也不换卡；GPU0 实际仅 ~23.7G 空余（kronos），大 claim 会被 worker 端 pynvml 启动校验拒启 error。外部占用缓解：`WORKER_SYSTEM_RESERVED_VRAM` 配置预留（整机级每卡都扣，单卡被占场景作用有限）。
- 隧道依赖本机 ssh 进程（97170）存活，重启本机后需重建并**带保活参数**：
  `nohup ssh -N -R 8000:127.0.0.1:8000 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no asiainfo@10.1.251.230 > /tmp/vllm_mgr_itest/a800_tunnel2.log 2>&1 &`（隧道断线 → 节点 offline 预期，重建后 ≤15s 自愈）。
- worker 重启命令见 §4；日志在 `/nfsdata/vllm_manager/worker.log`。
- **私钥 `~/.ssh/a800_ed25519` 仅本机使用，禁止上传/提交到任何仓库。**
