# A800 主机环境台账（10.1.251.230 / master / 2×A800 80GB）

> 定位：本工程维护的 A800 主机环境**持续台账**——硬件/软件/服务/部署物「当前已知状态 + 如何取实时值 + 变更日志」。
> 环境事实以本台账为准（数值实时自取，§1-6 命令）；历史外部 skill（`.agents/skills/a800-server/`，属外部工程）**已移除**，本文件自洽、不依赖其存在；工程侧部署物与变更见 §4/§7，runbook 见 `docs/a800-worker-deploy.md`。
> 维护规则：环境变更（起/停服务、部署、磁盘/显存显著变化）后**更新本文件**并追加 §7 变更日志行（日期+变更+责任方）；数值标「<日期> 时点」，实时值用 §1-6 的命令现取。

## 0. 快速总览（2026-09-24 收尾后状态）

- A800：master / Ubuntu 内核 5.15 / 2×A800 80GB；docker 26.0.2。
- 常驻占用：GPU0 **kronos ~57G**；GPU1 全空（gemma4 已停不恢复）。
- 本工程部署：worker 已停（8100 释放）、代码/venv/miniconda 保留于 `/nfsdata/vllm_manager/`（1.3G）。
- 容器：gemma4 Exited + 他人 5 容器（pdf2md×3 / evalscope / qwen-image）Up 未动；无 `vllm-*` 残留。
- 模型：`Qwen/Qwen3-0.6B`（1.5G）等测试模型就位。
- 后续要恢复 worker → runbook `docs/a800-worker-deploy.md` §4；要查实时值 → 本文 §1-6 命令。

## 1. 硬件

- GPU：2×NVIDIA A800 80GB PCIe（index 0/1）。
  实时：`nvidia-smi --query-gpu=index,memory.total,memory.used,memory.free --format=csv,noheader`
  （末次 <2026-09-24>：GPU0 total 81920 / used 56929 / free 24225 MiB；GPU1 total 81920 / used 0 / free 81153 MiB——GPU0 被 kronos 常驻占用，GPU1 停 gemma4 后全空。）
- GPU 进程占用归属（判断谁在用卡）：
  实时：`nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader`
  （末次 2026-09-24：GPU0 上 vLLM-Omni DiffusionWorker ~54.6G + python ~2.3G，即 kronos；GPU1 无进程。）
- CPU/内存：32 核（2026-09-24 时点）；内存 62GiB（used ~6.7G，available ~55G）。
  实时：`nproc` / `free -h`。
- 磁盘：`/nfsdata` 2.0T（剩 ~144G，2026-09-24 时点，93%）；`/` 292G（剩 ~23G，2026-09-24 时点，92%）。
  实时：`df -h /nfsdata /`；**红线：系统盘 `/` 勿写大文件（只剩 ~23G）**。
- 主机：master，Ubuntu 内核 5.15.0-190-generic（历史旧记 gpuserver01，以实测 master 为准）。

## 2. 软件

- docker 26.0.2（cgroup v2 / systemd driver；Server 与 Client 同版）。
- 系统 python 3.10.12（`/usr/bin/python3`）；**工程用 3.11 venv**（见 §4，不依赖系统 python）。
- harbor.cloud.com 镜像仓库（已登录，`~/.docker/config.json`）——镜像清单（实时：`docker images | grep -iE "vllm|smg|omni"`，末次 2026-09-24）：

| 镜像 | 体积 |
|---|---|
| harbor.cloud.com/vllm/vllm-openai:v0.26.0（**本工程 worker 默认用**） | 18.9GB |
| harbor.cloud.com/vllm/vllm-openai-smg:v0.26.0 | 19.2GB |
| harbor.cloud.com/vllm/vllm-openai:v0.28.0 | 19.8GB |
| harbor.cloud.com/vllm/vllm-omni:v0.28.0 | 20.9GB |
| harbor.cloud.com/vllm/vllm-omni:minimax-h3 / minimax-h3-20260813 | 19.6/19.7GB |
| registry.cn-shanghai.aliyuncs.com/luckfu/cuda:smg_1.10.1（他人） | 814MB |

- pip 源：工程 venv 用阿里云 `https://mirrors.aliyun.com/pypi/simple/`（华为云源 miniconda 目录为 SPA 无法列文件，miniconda 安装包用 TUNA/官方源）。
- 他人 venv：`/nfsdata/venvs/{eval, evaldl, smg}`——**勿动**。
- **路径雷区**：`/nfsdata/miniconda3` 为他人 root:root 700 目录——**勿碰**；本工程 miniconda 在 `/nfsdata/vllm_manager/miniconda3`。

## 3. 服务/容器台账（实时：`docker ps -a`）

| 容器 | 镜像 | 状态（2026-09-24 时点） | 端口 | 归属/处置 |
|---|---|---|---|---|
| gemma4-26b-a4b | harbor vllm-openai:v0.28.0 | **Exited(0)**（2026-09-24 本工程按用户决策停止，**不恢复**） | 8000（已释放） | 勿启动 |
| pdf2md-api-1 | aliyuncs dworks/pdf2md | Up 9 days (healthy) | 28082→5000 | 他人，勿动 |
| pdf2md-svc-1 | aliyuncs dworks/pdf2md | Up 9 days (healthy) | 5000 | 他人，勿动 |
| pdf2md-redis-1 | aliyuncs dworks/pdf2md | Up 9 days | 5000 | 他人，勿动 |
| evalscope | luckfu/evalscope:latest | Up 13 days | 9000 | 他人，勿动 |
| qwen-image-2512 | harbor vllm-omni:v0.28.0 | Up 9 days | 8091 | 他人，勿动 |

- 常驻进程：GPU0 **kronos**（末次 ~57G 显存，**勿杀/勿动**）；外部 skill 曾记载另有 comfyui 等常驻，但本机 docker 未见 comfyui 容器——**勿臆断，以实时 `docker ps -a` 为准**。
- 本工程测试容器：`vllm-{instance_id}`（A800 worker 拉起，用后即停；实时 `docker ps -a | grep vllm-`，末次 2026-09-24 为 0）。
- 本机 server/worker 侧：**master 节点记录保留**（当前 offline，重启 A800 worker 即 ready）——见本机 PG（`vllm_manager_node` 表）与 runbook §4。
- 归属确认纪律：docker stop/kill 前必 `docker inspect`（runbook §5 有样例）。

## 4. 本工程部署物（/nfsdata/vllm_manager/，保留供复用）

- 代码 `backend/`（与仓库 HEAD 对齐，rsync 部署，排除 .env/.venv/.git/tests/__pycache__/data/logs；`.env*` 不传保凭据）。
- venv `/nfsdata/venvs/vllm_mgr_worker`（**Python 3.11.16**，依赖 fastapi/uvicorn/httpx/tenacity/pydantic/pydantic-settings/psutil/python-dotenv/sqlalchemy/asyncpg/redis/uuid-utils/nvidia-ml-py，阿里云 pip 源）。
- miniconda3 `/nfsdata/vllm_manager/miniconda3`（3.11.16，**自有路径**，勿与 `/nfsdata/miniconda3` 他人 root 目录混淆）。
- worker 日志 `/nfsdata/vllm_manager/worker.log`；实例 meta/日志 `backend/data/logs/instances/`（`*.json` meta 供 restore，`*.N.log` 容器输出）。
- worker 启动关键 env（完整命令见 runbook §4）：`WORKER_ACCELERATOR=gpu` / `WORKER_MODEL_ROOT=/nfsdata/models` / `WORKER_VLLM_IMAGE=harbor.cloud.com/vllm/vllm-openai:v0.26.0` / `WORKER_VLLM_SHM_SIZE_GIB=10` / `WORKER_ADVERTISE_ADDRESS=10.1.251.230` / `SERVER_URL=http://127.0.0.1:8000`（隧道） / `WORKER_STARTUP_FAIL_THRESHOLD=40` / `WORKER_STARTUP_TIMEOUT_SECONDS=240` / `WORKER_LISTEN_PORT=8100`。
- 健康自检：`/nfsdata/venvs/vllm_mgr_worker/bin/python -c "import app.worker.main"`（import 验证）；worker 起后 `curl http://10.1.251.230:8100/healthz`。
- 启动/重启/收尾命令 → 引用 `docs/a800-worker-deploy.md`（runbook §2/§4/§8）。
- 体积（2026-09-24 时点）：`/nfsdata/vllm_manager/` 1.3G（backend+miniconda3+日志），venv 115M。

## 5. 网络

- VPN 单向：A800 **无法直连本机**（`192.168.101.181:8000` 不通）→ 必须 SSH 反向隧道（命令/保活见 runbook §3；断线 → 节点 offline 预期、重建 ≤15s 自愈）。
- IP：A800 `10.1.251.230`（固定）；本机 `192.168.101.181`（可能变化——实时：`ipconfig getifaddr en0`）。
- 端口：8100（A800 worker）/ 8000（本机 server）——实时：`ss -tln | grep -E ":8100|:8000"`（末次 2026-09-24：两端口均 free）。
- SSH 限流对策：短时连续 SSH 触发 `Permission denied`（瞬时限流）→ `sleep 8` 重试；长命令加 `-o ServerAliveInterval=30`；多条命令合并单连接。
- 私钥 `~/.ssh/a800_ed25519`：仅本机使用，**禁止上传/提交到任何仓库**。

## 6. 模型库（/nfsdata/models/，实时：`ls /nfsdata/models/`）

- **本工程测试用**：`Qwen/Qwen3-0.6B`（1.5G，含 model.safetensors；worker `resolve_model_path` 支持子目录式模型名——G3 已验证）；顶层另有独立 `Qwen3-0.6B`（2.9G）目录。
- 其他（摘要，勿整表复制）：`Qwen3-4B-Instruct-2507` / `Qwen3-4B-Thinking-2507` / `Qwen3.5-35B-A3B` / `Qwen3.8-27B` 系列 / `GLM-5.3-Flash-AWQ-W4A16` / `GLM-5.3-Flash-W4A8` / `MiniMax-H3` / `Qwen-Image-2512` / `FLUX.2-dev` / `CogVideoX-2b` / `DeepSeek-OCR` / `SenseVoiceSmall` / `Qwen2___5-0___5B-Instruct` 等——完整清单以实时 `ls /nfsdata/models/` 为准。
- 体积参考：`du -sh /nfsdata/models/{Qwen/Qwen3-0.6B,Qwen3-0.6B}`（末次 1.5G / 2.9G）。
- 模型下载/管理操作 → 按外部 skill 同款流程（hf-mirror / ModelScope 双通道，大权重 302 到 AWS CDN 被拒时回退 ModelScope）自行准备。

## 7. 变更日志（时间倒序）

| 日期 | 变更 | 责任方 |
|---|---|---|
| 2026-09-24 | 创建本台账；只读探测快照（GPU0 56929/24225、GPU1 0/81153 MiB；/nfsdata 剩 144G；容器 gemma4 Exited + 他人 5 容器 Up；8100/8000 free；部署物 1.3G+115M） | 本工程台账 |
| 2026-09-24 | A800 worker/隧道停止收尾；master 节点记录保留（offline）；本地 server 回 127.0.0.1、CPU worker 恢复（liurui.local ready） | 本工程 G4/收尾 |
| 2026-09-24 | G3 测试：D1-D9 生命周期全链路 + tp=2 观察（记录疑似 bug：`--gpus device=1,0` 多卡只挂首卡）；隧道意外断线一次（remote host closed，重建自愈）；结束态容器/实例清零 | 本工程 G3 |
| 2026-09-24 | G2 测试：节点域（注册幂等/鉴权/掉线恢复）+ 分配（C0-C4）；发现 worker `resolve_model_path` 子目录式模型名解析 bug（后续已修复） | 本工程 G2 |
| 2026-09-24 | gemma4-26b-a4b 停止（用户决策，**不恢复**）；GPU1 释放 ~73G；部署 vllm_manager 产物（代码/venv/miniconda，~2.2G） | 本工程 G1 |

## 8. 工程测试速查（G1-G3 固化，供后续 phase 复用）

### 8.1 实例 create 载荷模板（GPU 节点，Qwen3-0.6B）

```json
{
  "model_name": "Qwen/Qwen3-0.6B",
  "gpu_memory_utilization": 0.2,
  "args": ["--no-enable-log-requests", "--gpu-memory-utilization", "0.2",
           "--max-model-len", "4096", "--host", "0.0.0.0"]
}
```

- 可显式 `vram_claim`（Bytes）覆盖权重估算；`tensor_parallel_size`（GPU 节点 tp=2 分配双卡，但启动失败——见 §8.3 bug-1）。
- 端点为 `/vllm_manager/api/v1/instances`（create/start/stop/delete 管理端点免鉴权——**过渡姿态，仅内网可达**；worker 侧 :8100 端点 Bearer 鉴权）。

### 8.2 实例状态机与轮询

- 状态：pending → starting → running（target=starting → none）；stop → stopping → stopped；启动失败/运行失活 → error；节点失联时 running → unreachable（不删除）。
- 轮询：`curl -s http://127.0.0.1:8000/vllm_manager/api/v1/instances/{id}`；首启含 torch.compile **~25s**、容器就绪总计 **~70s**（G3 实测）；error 实例不自动重启（retryable=False）；docker kill 失活 → delay-0 自愈（restart+1）。

### 8.3 分配记账事实（账面 vs 真实）

- allocator 单卡 `available = total − Σallocated − reserved`（**不消费 memory_used**）→ first-fit 恒选 GPU0（账面 80G 全空）；真实显存由 worker `_check_vram` 启动时刻校验（空闲 ≥ vram_claim 否则拒启 error）。
- 账面上限 per-gpu = `memory_total × GMU`；GMU 0.2 → 配额 16GiB（0.6B 实际只吃 ~2-3G）。
- 缺口实证（G2）：vram_claim=24GiB 账面通过 → worker 拒启「显存不足」（GPU0 实际仅空 ~23.7G）。
- 并发叠加实证（G3）：3 个 0.6B + kronos 使 GPU0 used 达 77G/free 4G → engine 初始化失败 error。

### 8.4 已知 bug / 观察项（已修复项标注修复方式，修复证据见 `docs/local-testing.md` §8.3）

| 编号 | 现象 | 最小复现 | 状态/影响面 |
|---|---|---|---|
| bug-1 | `--gpus device=1,0` 多卡只挂首卡 | create tp=2 → 容器 DeviceIDs=["1"] → vLLM "World size(2)>GPUs(1)" exit 1 | **已修复**（gpus_args 内引号版 `--gpus '"device=X,Y"'`，G3 实测 tp=2 双卡 running） |
| bug-2 | 子目录式模型名广播 404 | POST :8100/models/weight {"model_name":"Qwen/Qwen3-0.6B"} | **已修复**（resolve_model_path 相对路径拼 model_root） |
| obs-1 | worker 断链时实例状态冻结（starting 不转） | 隧道断 → worker 上报失败刷屏 | 预期行为；隧道恢复即自愈，勿误判 |

### 8.5 隧道与探活

- 隧道：Mac `nohup ssh -N -R 8000:127.0.0.1:8000 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -i ~/.ssh/a800_ed25519 ...`；断线（remote host closed 曾发生一次）→ 节点 offline 预期、重建后 ≤15s 自愈。
- 探活：`curl -s -m5 http://127.0.0.1:8000/vllm_manager/api/v1/nodes`（200）；worker 侧 `curl http://10.1.251.230:8100/healthz`。
