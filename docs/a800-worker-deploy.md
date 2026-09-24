# A800 服务器集成 runbook（vllm_manager worker + 通用 GPU 集成地基）

> 定位：指导本工程在 `10.1.251.230`（主机名 master，2×A800 80GB）做各类集成工作的**可执行 runbook**。
> 权威环境参考：`.agents/skills/a800-server/SKILL.md` + `references/*`（`login.md` / `environment.md` / `container-usage.md` / `model-download.md` / `eval-data-download.md`）——**环境事实以 skill 为准**，本文件只放工程集成特有内容（部署/运维/决策）并引用之，不复制漂移。
> 集成测试结论：`docs/local-testing.md` §8（A800 GPU 测试结论，B/C/D 场景矩阵、产品 bug、记账缺口决策、收尾）。
>
> **红线（skill + 本工程）**：只动自己创建的东西；`gemma4-26b-a4b` 已授权停且**不恢复**；`kronos` / `pdf2md×3` / `evalscope` / `qwen-image`（及 skill 记载的常驻 `comfyui` 等）一律不碰；代码/venv/日志全落 `/nfsdata`（**系统盘 `/` 勿写，skill 记只剩 ~1.4G**）；私钥 `~/.ssh/a800_ed25519` 仅本机使用、**禁止上传/提交**；SSH 批量命令合并成单连接防限流（遇 `Permission denied` 为瞬时限流，`sleep 8` 重试，见 §5）。

---

## 0. 适用场景与流程总览

**适用场景**：

1. 部署 vllm_manager worker（:8100）并注册到本机 server（:8000）。
2. 起 GPU vLLM 实例，验证分配/编排闭环（B/C/D 场景，结论与关键值见 `docs/local-testing.md` §8）。
3. 其他 GPU 集成（SMG 路由 / KV 传输 / 镜像改造 / 能力验证等）——地基与运维复用本 runbook，业务步骤按 skill references。

**流程**：**地基绿（§1）→ 一次性部署（§2）→ 连通性（§3）→ 日常操作（§4）→ 收尾（§8）**；故障排查见 §5，已知约束与决策见 §6，历史证据快照见 §7。每次集成工作开始前先过一遍 §1 地基绿。

---

## 1. 地基绿检查（每次集成前，可复制命令块）

### 1.1 登录（skill login.md：SSH 密钥免密）

```bash
ssh -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no asiainfo@10.1.251.230 'uptime'
```

> 主机环境持续台账见 docs/a800-environment.md

- 认证：ed25519 私钥免密（无需密码；公钥已装服务器 authorized_keys）。
- **限流对策**：短时间内连续 SSH 会触发 `Permission denied`（瞬时限流，非凭据错）→ `sleep 8` 重试；长任务加 `-o ServerAliveInterval=30`；**多条远程命令合并成一次 SSH**（下面就是一个批量盘点块）。

### 1.2 一次性盘点（合并单连接，防限流）

```bash
ssh -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no asiainfo@10.1.251.230 \
  'nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader;
   docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}";
   df -h /nfsdata /;
   ls /nfsdata/models/;
   ss -tln | grep -E ":8100|:8000"'
```

### 1.3 各项完成标准

| 项 | 检查命令 | 完成标准 |
|---|---|---|
| GPU | `nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader` | **2 张 A800**。GPU0 通常有外部进程 `kronos` 占用（skill 基线记 ~8G；G1 时点实测 57G——**数值一律以实时为准**）；GPU1 停 gemma4 后通常全空 |
| 容器 | `docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"` | 确认各容器归属（gemma4 Exited、他人容器 Up 未动）；本次工作前应无 `vllm-*` 容器 |
| 磁盘 | `df -h /nfsdata /` | `/nfsdata` 有空间（skill 现记剩 ~165G；G1 时点 145G）；**`/` 系统盘勿写**（skill 现记剩 ~1.4G 危险，G1 时点 23G） |
| 镜像 | `docker images \| grep vllm` | harbor `vllm/vllm-openai:v0.26.0`（本项目 worker 用）在；skill 另记 `vllm-openai-smg:v0.26.0` 一镜像两用（SMG 类集成优先，见 skill container-usage） |
| 模型 | `ls /nfsdata/models/` | `Qwen/Qwen3-0.6B`（1.5G）等已就绪，勿重复下载（下载通道见 skill model-download） |
| 端口 | `ss -tln \| grep -E ":8100|:8000"` | worker 用 **8100 空闲**；8000 是 gemma4 曾用（已停，释放） |

**完成标准**：以上各项明确/存在/为空且与预期一致，地基即绿。

---

## 2. 一次性部署（worker + venv + 代码）

> 已按本节在 A800 部署成功（G1，证据见 §7）；以下命令块可直接复制执行。代码更新 = 重跑 rsync + 按 §4 重启 worker。
> **执行位置约定**：本节命令中**带 `ssh ...` 前缀的**在本机执行；**无前缀的**为 A800 登录后执行（§2.2 起均在 A800 shell 内）。

### 2.1 rsync 代码

```bash
rsync -av -e "ssh -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no" \
  --exclude '.venv' --exclude '.git' --exclude 'tests' \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.env*' \
  --exclude 'data/logs' \
  backend/ asiainfo@10.1.251.230:/nfsdata/vllm_manager/backend/
```

（本机 backend 工作区 → A800 `/nfsdata/vllm_manager/backend/`，G1 时点 107 files / 506KB。）

### 2.2 Python 3.11 环境 + venv

系统 python 3.10.12 **不足**（`app/base/base_enum.py` 用 `StrEnum`，3.11+ 特性）→ 决策：**装 3.11 环境，不改代码**（诊断确认无其他 3.11 语法/依赖）。

```bash
# 1) miniconda 3.11（官方源！TUNA 镜像包 md5 损坏不可用，G1 实测）
curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-py311_26.7.1-1-Linux-x86_64.sh \
  -o /nfsdata/vllm_manager/miniconda.sh \
  && bash /nfsdata/vllm_manager/miniconda.sh -b -p /nfsdata/vllm_manager/miniconda3

# 2) venv（G1 时点版本号，以 miniconda 官方 release 为准）
/nfsdata/vllm_manager/miniconda3/bin/python -m venv /nfsdata/venvs/vllm_mgr_worker
```

> **路径冲突规避**：`/nfsdata/miniconda3` 是他人 **root:root 700** 目录（非本 worker 创建），**勿碰**，用自有路径 `/nfsdata/vllm_manager/miniconda3`。

### 2.3 pip 依赖（阿里云源，G1 实测）

```bash
/nfsdata/venvs/vllm_mgr_worker/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  fastapi "uvicorn[standard]" httpx tenacity pydantic pydantic-settings \
  psutil python-dotenv sqlalchemy asyncpg redis uuid-utils nvidia-ml-py
```

> **`nvidia-ml-py` 必须装**（pynvml）：缺失 → worker 启动时 gpu_devices 上报降级为空列表（采集优雅降级但节点变无 GPU），补装后重启即恢复（§5）。
> 注：skill environment 对**容器内** pip 包优先华为云源；宿主机 venv 用阿里云是 G1 实测路径，两者不冲突。

### 2.4 import 验证

```bash
cd /nfsdata/vllm_manager/backend \
  && /nfsdata/venvs/vllm_mgr_worker/bin/python -c "import app.worker.main; print('WORKER_IMPORT_OK')"
```

### 2.5 连通性判定（VPN 单向 → 反向隧道，见 §3）

```bash
# A800 上直连本机 Mac server —— 预期失败（VPN 单向，不可达）
ssh -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no asiainfo@10.1.251.230 \
  'curl -m5 -s -o /dev/null -w "%{http_code}" http://192.168.101.181:8000/vllm_manager/api/v1/nodes'
# 失败（000/超时）→ 必须走反向隧道；SERVER_URL=http://127.0.0.1:8000
```

> **前置步骤（必须）**：直连失败后，**先按 §3 建反向隧道并确认本机 server 运行中（Mac 侧 `curl http://127.0.0.1:8000/vllm_manager/api/v1/nodes` 为 200）**，再执行 §2.6 启动 worker——否则无隧道的 `SERVER_URL=127.0.0.1:8000` 会注册重试耗尽后启动失败退出。

### 2.6 worker 启动（完整 nohup 块）

```bash
cd /nfsdata/vllm_manager/backend
nohup env WORKER_ACCELERATOR=gpu \
  WORKER_MODEL_ROOT=/nfsdata/models \
  WORKER_VLLM_IMAGE=harbor.cloud.com/vllm/vllm-openai:v0.26.0 \
  WORKER_VLLM_SHM_SIZE_GIB=10 \
  WORKER_ADVERTISE_ADDRESS=10.1.251.230 \
  SERVER_URL=http://127.0.0.1:8000 \
  WORKER_STARTUP_FAIL_THRESHOLD=40 \
  WORKER_STARTUP_TIMEOUT_SECONDS=240 \
  WORKER_LISTEN_PORT=8100 \
  /nfsdata/venvs/vllm_mgr_worker/bin/python app/work_worker.py \
  > /nfsdata/vllm_manager/worker.log 2>&1 &
echo $! > /nfsdata/vllm_manager/worker.pid
```

| env | 值 | 说明 |
|---|---|---|
| `WORKER_ACCELERATOR` | `gpu` | 显式声明 GPU 节点（allocator 走 GPU 分支；fail-closed：不声明则按 CPU 拒绝语义处理） |
| `WORKER_MODEL_ROOT` | `/nfsdata/models` | 模型根目录 |
| `WORKER_VLLM_IMAGE` | `harbor.cloud.com/vllm/vllm-openai:v0.26.0` | 纯 vLLM 官方镜像（ENTRYPOINT=vllm serve，匹配 A1 契约） |
| `WORKER_VLLM_SHM_SIZE_GIB` | `10` | vLLM 容器共享内存 |
| `WORKER_ADVERTISE_ADDRESS` | `10.1.251.230` | server→worker 回连地址（healthz 直连校验） |
| `SERVER_URL` | `http://127.0.0.1:8000` | 经反向隧道指向本机 server |
| `WORKER_STARTUP_FAIL_THRESHOLD` / `WORKER_STARTUP_TIMEOUT_SECONDS` | `40` / `240` | 首启 torch.compile 1-3min，放宽阈值（**非契约固定，实测不足可再放宽**） |
| `WORKER_LISTEN_PORT` | `8100` | worker 管控端口 |

### 2.7 注册验证

```bash
# 本机：master ready、accelerator=gpu、gpu_devices 2×A800
curl -s http://127.0.0.1:8000/vllm_manager/api/v1/nodes | python3 -m json.tool
# server→worker 回连（ADVERTISE 生效）
curl -s -m5 http://10.1.251.230:8100/healthz
```

---

## 3. 连通性（VPN 单向 → 反向隧道）

- **判定**：§2.5 A800 直连 Mac `192.168.101.181:8000` 不可达（VPN 单向）→ **隧道必用**，`SERVER_URL` 恒为 `http://127.0.0.1:8000`。
- **隧道启动**（本机 Mac，**带保活参数**；日志 `/tmp/vllm_mgr_itest/a800_tunnel.log`）：

```bash
nohup ssh -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
  -N -R 8000:127.0.0.1:8000 asiainfo@10.1.251.230 \
  > /tmp/vllm_mgr_itest/a800_tunnel.log 2>&1 &
echo $! > /tmp/vllm_mgr_itest/a800_tunnel.pid
```

- **探活**：

```bash
ssh -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no asiainfo@10.1.251.230 \
  'curl -m5 -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/vllm_manager/api/v1/nodes'
# 期望 200
```

- **断线处置**：隧道断线 → 节点 offline（**预期行为，非 bug**）、running 实例冻结 starting（无新上报）；**重建隧道（同上命令）后 ≤15s 自愈**（节点 ready、实例回 running，G1 实测）。长会话**每 30min 探活**一次。

---

## 4. 日常操作

### 4.1 worker 重启（更新代码后 / 故障恢复）

```bash
# 1) 精确取 python PID（pid 文件可能误记 bash wrapper PID，以 ps 为准）
ssh -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no asiainfo@10.1.251.230 \
  'ps aux | grep "python app/work_worker.py" | grep -v grep | awk "{print \$2}"'
# 2) kill 该 PID → 确认 8100 释放
ssh -i ~/.ssh/a800_ed25519 -o StrictHostKeyChecking=no asiainfo@10.1.251.230 \
  'kill <PID>; sleep 2; ss -tln | grep 8100 || echo "8100 released"'
# 3) 按 §2.6 命令块重启 → 幂等注册同 node_id（机器 master，token 不变）
```

### 4.2 节点管理

- worker 重启 = 幂等注册（node_id/token 不变，不新增节点）。
- **node 删除 = 机器退役**：存活守卫（READY/UNREACHABLE 心跳宽限内 → 409）+ 级联删本节点 stopped 实例；删除后 worker 重启注册**新 node_id**（契约见 `docs/worker-contract.md` §1.4）。
- 本机 CPU worker / A800 worker 切换时，测试期停其一（8100 释放），避免双节点干扰分配候选。

### 4.3 起 / 停 vLLM 实例（走平台 API，不手工 docker run）

起实例一律走 `POST /vllm_manager/api/v1/instances`（模板由 worker 渲染 docker run：`--gpus '"device=0,1"'` 内引号多卡、`--network host`、挂载模型）：

```json
{
  "model_name": "Qwen/Qwen3-0.6B",
  "args": [
    "--no-enable-log-requests", "--gpu-memory-utilization", "0.2",
    "--max-model-len", "4096", "--host", "0.0.0.0"
  ]
}
```

- **模型名用子目录式** `Qwen/Qwen3-0.6B`（`resolve_model_path` 已修复支持，见 local-testing §8.3）。
- **GMU 建议 0.2**（16GiB 账面）：allocator 恒选 GPU0（不消费外部占用，见 §6），GPU0 真实空余 G1 时点 ~23.7G——**GMU 0.3（24GiB claim）会账面通过但 worker 启动时刻 pynvml 拒启 error**（缺口实证）。
- 首启 torch.compile **25–70s**（阈值 40/240 足够）；`--host 0.0.0.0` 便于 Mac 侧直连推理冒烟。
- 停/删：`POST /vllm_manager/api/v1/instances/{id}/stop`、`DELETE /vllm_manager/api/v1/instances/{id}`（端点契约见 `docs/worker-contract.md`）。

---

## 5. 故障排查表

| 现象 | 根因 | 处置 |
|---|---|---|
| SSH `Permission denied` | 短时间连太多被**瞬时限流**（非凭据错） | `sleep 8` 重试；远程命令合并单连接（§1.2）；长任务加 `-o ServerAliveInterval=30` |
| 8100 被占用，worker 起不来 | 前次 worker 未停干净 | 按 §4.1 精确 PID kill，确认释放后重启 |
| gpu_devices 上报为空 | `nvidia-ml-py`（pynvml）未装 | 补装（§2.3）后重启 worker |
| 子目录模型名 weight 404 → create 500 | worker 跑的是旧代码（`resolve_model_path` 已修复） | rsync 修复版（§2.1）→ 重启（§4.1） |
| tp>1 只挂首卡 / `World size(2)>GPUs(1)` | worker 旧代码（`gpus_args` 未改内引号版） | rsync 修复版 → 重启 → 容器内 `nvidia-smi -L` 应见 2 卡 |
| vLLM 启动 error「显存不足」 | allocator 恒选 GPU0 且不消费外部占用（账面通过），worker 启动时刻 pynvml 校验拒启 | 调低 GMU 至真实空余以下；或配置 `WORKER_SYSTEM_RESERVED_VRAM` 预留（见 §6） |
| vLLM 启动即退（argparse 错误） | v0.26.0 的 `--disable-log-requests` **已移除** | 用 `--no-enable-log-requests`（skill container-usage 同证） |
| 首启超 240s（启动超时 error） | torch.compile 首启 1-3min 属正常 | 40/240 阈值**非契约固定**，按实测放宽重测 |
| 隧道断线 → 节点 offline / 实例冻结 starting | 隧道单连接断开（G1 实测过远端关闭） | 重建隧道（§3）≤15s 自愈；offline/冻结是预期，非 bug |
| pid 文件 PID 无效 | nohup 块 `$!` 可能记 bash wrapper PID | 以 `ps aux \| grep "python app/work_worker.py"` 为准（§4.1） |

---

## 6. 已知约束与决策（为什么）

| 约束 | 决策 / 缓解 | 依据 |
|---|---|---|
| 系统 python 3.10 用不了 `StrEnum`（`base_enum.py`） | **装 Python 3.11 环境，不改代码**（miniconda 官方源；TUNA 镜像 md5 损坏弃用；`/nfsdata/miniconda3` 他人 root:700 勿碰，用自有路径） | G1 部署记录 |
| **allocator 记账缺口**：`available = total − Σallocated − system_reserved` **不消费 `memory_used`**（外部占用如 kronos 被当全空），first-fit 恒选 GPU0 | 用户决策**方案 A：维持现状**（与 gpustack 同构，不照搬「消费 memory_used」——陈旧性/双重计数/外部不可控三重风险）；kronos 类单卡占用用 **`WORKER_SYSTEM_RESERVED_VRAM` 配置预留**缓解（**整机级每卡都扣，单卡被占场景作用有限**）；worker 启动时刻 pynvml `_check_vram` 是兜底边界（**不覆盖运行期渐进占用**） | `docs/local-testing.md` §8.4 |
| 缺口实证（G2） | 24GiB claim → allocator 账面通过 → worker 拒启 error（GPU1 全空也不换卡）；并发账面 Σ32GiB 不超、真实逼近极限时 vLLM Engine init 失败（自身 OOM 兜底，无炸机） | `docs/local-testing.md` §8.2/§8.4 |
| vLLM v0.26.0 参数语义 | `--no-enable-log-requests`（旧名已移除）；GMU：0.6B 平台测 0.2 落 GPU0（16GiB），skill 手工起容器记 0.3~0.35；torch.compile 首启 1-3min | skill container-usage + G3 实测 |
| VPN 单向（A800 无法直连本机） | 反向隧道是**必然架构**（§3），`SERVER_URL` 恒 `127.0.0.1:8000`；server→worker 直连 `10.1.251.230:8100`（ADVERTISE） | G1 连通性实测 |
| 多卡 `--gpus device=1,0` 只挂首卡（docker 26.0.2） | 平台模板已改**内引号版** `--gpus '"device=1,0"'`（NVIDIA 官方多卡 workaround）→ A800 实测 tp=2 running | `docs/local-testing.md` §8.3 |
| 磁盘红线 | 代码/venv/日志全落 `/nfsdata`；`/` 系统盘勿写（skill 记剩 ~1.4G 危险） | skill environment + SKILL 红线 |

---

## 7. 验证证据（G1 时点值，仅对照）

> 以下为 **G1 部署时点快照**，只用于对照——**数值以实时检查为准**（§1 地基绿）。

| 验证项 | G1 时点值 |
|---|---|
| 节点注册 | machine_id=master、hostname=master（skill environment 记 hostname=gpuserver01，以实时为准）、ip=10.1.251.230、worker_port=8100、**state=ready**、unreachable=false |
| heartbeat | 15s 周期持续刷新 |
| status.accelerator | `gpu` |
| gpu_devices | 2 条：index 0/1，均为 NVIDIA A800 80GB PCIe，memory_total=85899345920（80GiB/卡） |
| gpu_devices.memory_used | index0=60498313216（kronos 占用中）、index1=804519936（gemma4 停后基本释放） |
| system_reserved | ram=0、vram=0（未配置预留） |
| 其他上报 | cpu 32 核 / memory / swap / filesystem / uptime 正常 |

> **结论级证据**（B/C/D 场景矩阵、产品 bug 与修复、记账缺口两种形式、收尾状态）见 `docs/local-testing.md` §8（§8.2 验证结论 / §8.3 产品 bug / §8.4 记账缺口决策 / §8.5 运维注记）。

---

## 8. 收尾 / 清理

1. **停 worker**：按 §4.1 精确 PID kill；确认 8100 释放（`ss -tln | grep 8100` 无输出）。
2. **停隧道**（本机）：`kill $(cat /tmp/vllm_mgr_itest/a800_tunnel.pid)`；确认本机 8000 回 127.0.0.1 常态。
3. **本地还原**：server 回 `127.0.0.1:8000` 重启；CPU worker 恢复（`WORKER_ACCELERATOR=cpu`，本机 colima，见 `docs/local-testing.md`）；**master 节点记录保留**（offline，历史记录，非 delete）。
4. **A800 侧还原确认**：`docker ps -a` 无本次 `vllm-*` 容器、gemma4 保持 Exited、他人容器未动、GPU0 kronos 未动。
5. **保留产物**（供复用，勿删）：`/nfsdata/vllm_manager/` 代码（~1.1G）+ venv（115M）+ miniconda（~1G）；worker 重启命令见 §2.6。
6. **红线复核**：gemma4 不恢复（用户决策）；私钥 `~/.ssh/a800_ed25519` 未上传/未入库；本次改动只落在 `/nfsdata` 与 docs/。
