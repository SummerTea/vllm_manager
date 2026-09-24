# 本地环境测试指导（无 GPU / colima / CPU 推理）

> 目标：在本机（macOS + colima，无 NVIDIA 显卡）用 **CPU** 跑通 vLLM 推理，为
> vllm_manager 的本地集成测试提供可复现的环境基础。本文档沉淀自 2026-09 实测验证
> （qwen3-0.6B 在 colima arm64 CPU 上完整跑通 健康检查 + 真实推理）。
>
> 关联：`docs/worker-contract.md`（server↔worker 契约）、`docs/architecture.md`
> （架构蓝图）、`docs/gpustack-borrowings.md`（借鉴调研）。

---

## 1. 结论速览

| 项 | 结论 |
|---|---|
| 本机 CPU 跑 vLLM | ✅ **可行**（`vllm/vllm-openai-cpu:latest-arm64` 镜像） |
| 本机已有模型 | ✅ `Qwen3-0.6B`（HF 缓存 1.4G），**无需下载**；建议解引用复制到 `~/models/` |
| 健康检查链路 | ✅ `GET /v1/models` → 200 |
| 真实推理链路 | ✅ `chat/completions` → 返回内容（float32） |
| **通过本项目全链路** | ✅ **可行**——显式 `WORKER_ACCELERATOR=cpu` 声明 + allocator CPU 分支 + 模板 CPU 变体（已实施，见 §5/§6） |

---

## 2. 环境准备

### 2.1 容器运行时（colima）

本机 docker context 可能指向错误的 socket（`/var/run/docker.sock` 不存在），需确认 colima：

```bash
# 确认 colima 已安装并启动（本机实测：4 CPU / 8GiB / aarch64）
colima start                 # 首次启动
docker context use colima    # 切换到 colima context（关键！）
docker version --format 'Server={{.Server.Os}}/{{.Server.Arch}}'   # 期望 linux/arm64
```

> 注意：本机 docker context 可能有多个（default/orbstack/colima），default 指向
> `/var/run/docker.sock` 是坏的，**必须 `docker context use colima`** 后再操作。

### 2.2 模型准备（Qwen3-0.6B）

本机 HF 缓存已有完整模型（含 `model.safetensors` 1.4G），**无需下载**：

```bash
ls ~/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B/snapshots/*/   # 应含 config.json + model.safetensors
```

**必须解引用复制**（HF 缓存的 snapshot 是相对 symlink，colima virtiofs 挂载下容器内
解析失败——实测报 `Invalid repository ID or local directory specified`）：

```bash
mkdir -p ~/models
cp -R -L ~/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B/snapshots/<hash> ~/models/Qwen3-0.6B
# 验证：~/models/Qwen3-0.6B/config.json 是真实文件（非 symlink）
```

> 该目录形态恰好符合 worker 的模型语义：`WORKER_MODEL_ROOT/{model_name}`。

如需从 ModelScope 下载（本机无 modelscope CLI，`modelscope` 命令不存在）：

```bash
pip install modelscope
modelscope download --model Qwen/Qwen3-0.6B --local_dir ~/models/Qwen3-0.6B
```

### 2.3 镜像

```bash
docker pull vllm/vllm-openai-cpu:latest-arm64   # 约 835MB，arm64 专属
```

---

## 3. 手工验证（可复现命令）

### 3.1 启动容器

```bash
MODEL=~/models/Qwen3-0.6B
docker run -d --name vllm-cpu-test \
  -p 8000:8000 --shm-size 4g \
  -v "$MODEL:$MODEL" \
  -e VLLM_CPU_KVCACHE_SPACE=1 \
  -e VLLM_CPU_OMP_THREADS_BIND=0-3 \
  vllm/vllm-openai-cpu:latest-arm64 \
  "$MODEL" \
  --dtype float32 --enforce-eager --max-model-len 4096 \
  --port 8000 --served-model-name qwen3-0.6b
```

### 3.2 健康检查（约 30-60s 后就绪）

```bash
curl -s http://127.0.0.1:8000/v1/models | python3 -m json.tool
# 期望: "data": [{"id": "qwen3-0.6b", ...}]
```

### 3.3 真实推理

```bash
curl -s --max-time 90 http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-0.6b","messages":[{"role":"user","content":"1+1=? 只回答数字"}],"max_tokens":20}' \
  | python3 -m json.tool
# 期望: choices[0].message.content 返回内容（qwen3 会先输出 <think> 思维链）
```

### 3.4 清理

```bash
docker rm -f vllm-cpu-test
```

---

## 4. 已知问题与对策（实测踩坑，按顺序）

| # | 现象 | 根因 | 对策 |
|---|---|---|---|
| 1 | `Invalid repository ID or local directory specified` | HF 缓存 snapshot 是相对 symlink（`config.json -> ../../blobs/...`），colima virtiofs 挂载下容器内解析失败 | 模型解引用复制（`cp -R -L`）到独立目录，见 §2.2 |
| 2 | `ValueError: Non-integer device ID 'cpu' is not supported by cpu` | CPU 镜像**默认就是 CPU 设备**，不接受 `--device cpu` 参数 | 去掉 `--device cpu` |
| 3 | 启动后 EngineCore 卡死（日志 `No available shared memory broadcast block found in 60 seconds... compilation`） | **torch.compile / inductor 编译在 arm64 CPU 上卡死**（Triton 缺失，回退 V1 runner） | 加 **`--enforce-eager`** 跳过编译 |
| 4 | `ValueError: ... 4.38 GiB KV cache is needed, larger than available (0.99 GiB)` | 默认 `max_model_len=40960`（qwen3 0.6B 的 max len）需要的 KV cache 超本机内存 | 加 **`--max-model-len 4096`**（本地测试足够；可用 `--max-model-len` 推导公式 `4096 ≈ 可用内存`） |
| 5 | 推理 500 `EngineDeadError`；日志 `Failed to create oneDNN linear, fallback to torch linear` + `extern_kernels.mm` 卡死 | **bf16 在 arm64 CPU 上 oneDNN 无法建 matmul primitive**，回退 torch linear 后矩阵乘卡死 | 改 **`--dtype float32`**（BLAS 路径稳定，推理成功） |

**关键参数组合（实测通过）**：`--enforce-eager --max-model-len 4096 --dtype float32`
——这三个都是**用户透传 args**，模板无需内置（见 §5 对接现状）。

---

## 5. 与本项目（vllm_manager）的对接现状

> 现状结论：**CPU 集成测试改造已实施**——通过 server→worker 全链路创建实例在
> colima CPU 环境下可跑通（见 §6 实测闭环）。设计要点见下方「已实施的 CPU 改造」。

| # | 原阻塞点 | 解决方案（已实施） | 代码位置 |
|---|---|---|---|
| 1 | server allocator 拒绝无 GPU 节点（`gpu_devices` 为空 → `AllocationRejected("节点 X 无可用 GPU")`） | 显式 `WORKER_ACCELERATOR=cpu` 声明 → allocator 走 CPU 分支：`gpu_indexes=[]`、`allocated_vram={}`、tp>1 拒绝；**fail-closed：GPU/未声明/`gpu_devices` 空仍拒绝**（不按空列表推断 CPU，防 GPU 采集降级 fail-open 误分配） | `allocator/service.py:_try_worker`、`allocator/schema.py:WorkerResource`、`service/creation.py` |
| 2 | docker 模板硬编码 `--gpus device=`（colima 无 NVIDIA 驱动报错） | 模板新增结构性片段 `{net_args}`/`{gpus_args}`：CPU → `-p {port}:{port}` + 空串（无 `--gpus`）；GPU 行为不变；server/worker 双份常量同步 + 防漂移测试锁定 | `process_utils.py:DEFAULT_VLLM_RUN_TEMPLATE`/`build_context`、`creation.py:DEFAULT_VLLM_RUN_TEMPLATE` |
| 3 | `--network host` + `127.0.0.1` 健康检查在 colima 下不通 | CPU 改用 `-p {port}:{port}` 端口映射（colima 下宿主机 `127.0.0.1:{port}` 可达），健康检查不变（`GET /v1/models` 1s 200） | `lifecycle.py:_launch_container`/`_health_ok` |
| 4 | 默认模板含 `{vllm_bin} serve` 与镜像 ENTRYPOINT 叠加 → docker 实执行 `vllm serve vllm serve <path>` → exit 2（GPU/CPU 主战场同 bug） | 默认模板去 `{vllm_bin} serve`，**依赖镜像 `ENTRYPOINT=["vllm","serve"]`**（A1 整改）：`docker run ... {image} {model_path} {args}`；`{vllm_bin}` 键仅存量模板兼容保留 | `process_utils.py:DEFAULT_VLLM_RUN_TEMPLATE`、`creation.py:DEFAULT_VLLM_RUN_TEMPLATE` |

**已实施的 CPU 集成测试改造**：

1. **显式加速器声明（取代空 `gpu_devices` 推断）**：worker `WORKER_ACCELERATOR=cpu`
   经 status 载荷 `accelerator` 字段上报；server `NodeStatus.accelerator`/
   `WorkerResource.accelerator` 显式字段，allocator **仅 `accelerator == "cpu"`** 走
   CPU 分配（`gpu_indexes=[]`、`allocated_vram={}`、tp>1 拒绝）；GPU / 未声明 /
   `gpu_devices` 为空 → fail-closed 拒绝（「无可用 GPU」）。
2. **allocator CPU 分支**：分配 `gpu_indexes=[]`、`allocated_vram={}`，显存校验跳过
   （`vram_claim` 仅记录不判定）。
3. **模板 CPU 变体**：`DEFAULT_VLLM_RUN_TEMPLATE` 按 `gpu_indexes` 空否派生
   `{net_args}`/`{gpus_args}`（GPU → `--network host` + `--gpus device=`；CPU →
   `-p {port}:{port}` + 空串）；`{gpu_indexes}`/`{port}` 键保留（存量模板快照兼容）。
   server 常量 + worker 常量 + 防漂移测试同步。
4. **CPU 三参数走 args**：`--enforce-eager --dtype float32 --max-model-len 4096`
   （§4 关键参数）**经用户 args 透传**，模板不内置。
5. **CPU 不注入 OMP/SAFETENSORS**：`gpu_indexes` 为空时不注入
   `OMP_NUM_THREADS=1`/`SAFETENSORS_FAST_GPU=1`（注入会把推理锁单线程）；
   `VLLM_CACHE_ROOT` GPU/CPU 都保留。
6. **启动阈值配置化**：`WORKER_STARTUP_FAIL_THRESHOLD`（默认 2）/
   `WORKER_STARTUP_TIMEOUT_SECONDS`（默认 30）——本地 CPU 模型加载 30-60s，需调大
   （本机实测 12 / 90）。

worker 侧已有两处正确降级（无需改）：`_check_vram`（pynvml 不可用/无 GPU → 不拦截，
`lifecycle.py:_check_vram`）与 GPU 采集（无 GPU → 空列表 + 告警一次，`collector.py`）。

---

## 6. 全链路集成测试前置条件（本机实测闭环）

本机可跑通的闭环（改造已实施，§5）：

```
注册（worker→server, /nodes/register）
  → 状态上报（status.accelerator=cpu 显式声明）
  → 创建实例（allocator CPU 分支分配 gpu_indexes=[]）
  → worker 渲染 CPU 模板（无 --gpus、-p 端口映射）
  → docker run 拉起 qwen3-0.6B（float32 + enforce-eager + max-model-len 2048 走 args）
  → 健康检查（worker 探宿主机 127.0.0.1:{port}/v1/models → 200 → running）
  → /instances/report 对账 → server 收敛 running
  → stop → docker stop/kill/rm → report 消失 → 收敛 stopped
```

**运行前检查清单**：

1. colima 已启动（`docker context use colima`）、模型已解引用复制到
   `~/models/Qwen3-0.6B`（非 symlink，见 §2.2）、镜像已拉取（§2.3）。
2. **worker 侧 `.env` 调参**（worker 独立读 `WORKER_*`）：

   | 配置 | 本机取值 | 说明 |
   |---|---|---|
   | `WORKER_ACCELERATOR` | `cpu` | 显式声明 CPU 节点（fail-closed：不声明则按 GPU 拒绝） |
   | `WORKER_MODEL_ROOT` | `~/models`（模型目录，如 `$HOME/models`） | worker 模型根目录 |
   | `WORKER_VLLM_IMAGE` | `vllm/vllm-openai-cpu:latest-arm64` | CPU 版镜像 |
   | `WORKER_VLLM_SHM_SIZE_GIB` | `4` | 8GiB 内存机默认 10g 过大，调 4g |
   | `WORKER_STARTUP_FAIL_THRESHOLD` | `12` | CPU 模型加载 30-60s，放宽快速失败阈值 |
   | `WORKER_STARTUP_TIMEOUT_SECONDS` | `90` | 总预算放宽 |
   | `SERVER_URL` | `http://127.0.0.1:8000` | 指向本机 server |
   | `WORKER_LISTEN_PORT` | `8100`（默认） | 按本机实际端口 |

3. **create 请求示例**（`POST /vllm_manager/api/v1/instances`，**实测跑通参数**）：

    ```json
    {
      "model_name": "Qwen3-0.6B",
      "args": [
        "--enforce-eager", "--dtype", "float32", "--max-model-len", "2048",
        "--gpu-memory-utilization", "0.65"
      ]
    }
    ```

   （§4 关键参数经 `args` 透传；`tensor_parallel_size` 保持 1——CPU 分支 tp>1 拒绝。）

> **集成测试定位（vLLM 最小参数启动即可）**：本机 CPU 集成测试的目标是**验证
> vllm_manager 管理链路闭环**（注册 → 创建 → 启动 → 健康检查 → 对账 → 停止），
> **不是 vLLM 推理性能/能力测试**。因此 vLLM **只须以最小参数启动、能响应
> `/v1/models` 与一次推理即可**，无需调优推理质量参数（`--temperature`/
> `--repetition-penalty`/`--max-num-seqs` 等一律不传，走 vLLM 默认值）。
>
> 上表 create 示例中的 4 个参数是**为让 8GiB CPU 跑通而必须的最小集**，逐个说明：
>
> | 参数 | 为什么必须 |
> |---|---|
> | `--enforce-eager` | 跳过 torch.compile（arm64 CPU 上编译会卡死，§4 坑 3） |
> | `--dtype float32` | bf16 在 arm64 oneDNN 无法建 matmul primitive，推理 500（§4 坑 5） |
> | `--max-model-len 2048` | 压缩 KV cache 需求到 ~0.44GiB（默认 40960 需 4.38GiB 超内存） |
> | `--gpu-memory-utilization 0.65` | CPU 后端视其为 CPU 内存保留比例（KV 公式见下），升到可行窗口内才能启动 |
>
> 除这 4 个外**不要额外加参数**——集成测试验证的是平台链路与状态机，vLLM 侧
> 保持最小启动即可；推理质量参数属于模型服务调优，不属于本测试范围。

> **CPU 内存预算（P1-2，第四轮实测定稿）**：CPU 后端把 `--gpu-memory-utilization`
> （GMU）解释为 **CPU 内存保留比例**（非显存），且 KV cache 校验公式为
> **`KV空间 = gmu×total − 进程RSS`**（vLLM v0.30 `cpu_worker.determine_available_memory`
> 源码实证；RSS 为权重+引擎常驻，Qwen3-0.6B/float32 ≈ 4.1~4.3GiB）。
> 由此：**正确方向是升 GMU 而非降**——GMU 0.3/0.5 时 `KV空间` 被固定 RSS 反超为负而
> 拒绝启动；**可行窗口 GMU ∈ (0.53, 0.80)**（8GiB colima 实测 0.65 通过，0.6 差
> ~70MiB）。`--max-model-len 2048` 把 KV 需求压到 ~0.44GiB（4096 需更大）。
> GPU 场景不受影响（GMU 仍按显存语义，worker 侧只透传不解释）。

> **运行内存实测（第四轮，8GiB 零扩容跑通）**：colima **8GiB 足以跑通
> Qwen3-0.6B/float32 全链路**（GMU 0.65 + max-len 2048）：create → running（~45s）→
> /v1/models 200 → 推理成功 → stop → stopped 自动收敛（~10s）。无需扩容 16GiB。
> 若换更大模型需上调 GMU 或扩容（`colima stop && colima start --memory N`，
> N 依模型；colima stop 后内存归还本机）。

> **A1 注记（模板依赖镜像 ENTRYPOINT）**：默认模板不再含 `{vllm_bin} serve`——
> `vllm serve` 由官方镜像 `ENTRYPOINT=["vllm","serve"]` 承担（旧模板叠加会实执行
> `vllm serve vllm serve <path>` → exit 2）。**存量实例若 template 快照是旧格式
> （含 `{vllm_bin} serve`），需 delete + create 重建**才会下发新模板；worker 侧
> `{vllm_bin}` 键仍兼容旧格式模板渲染（不 KeyError）。
