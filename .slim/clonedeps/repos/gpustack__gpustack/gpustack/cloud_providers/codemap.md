# gpustack/cloud_providers/

## Responsibility
云厂商 GPU 虚拟机生命周期管理：为"云上 worker 池"（ClusterProvider 为 DigitalOcean 等公有云）提供实例创建/销毁、SSH 密钥、块存储卷挂载与启动脚本（cloud-init user_data）生成能力。

## Design
- `abstract.py`：`ProviderClientBase` ABC 定义标准生命周期契约，注释明确调用顺序：`create_ssh_key` → `create_instance` → `wait_for_started` → `wait_for_public_ip` →（可选）`create_volumes_and_attach` → `delete_instance` →（可选）`delete_ssh_key`；另有 `construct_user_data` 基类实现与 `get_api_endpoint`/`process_header` 钩子。数据类 `CloudInstance`/`CloudInstanceCreate` 与 `InstanceState` 枚举统一各厂商状态。
- `digital_ocean.py`：`DigitalOceanClient`（基于 `pydo.aio`）——droplet 创建（labels 转 tags）、`destroy_with_associated_resources_dangerous` 销毁、网络 v4 提取公网 IP、SSH key 管理、卷创建+attach（校验 size_gb/format，label 限 16 字符）；`construct_user_data` 根据镜像类型（GPU 镜像 vs ubuntu/debian）决定驱动安装策略（`ManufacturerEnum`），并通过 DigitalOcean metadata 服务（169.254.169.254）写入 external_id 与 advertise_address。
- `common.py`：厂商工厂 `factory: Dict[ClusterProvider, (client_class, constructor)]`，`get_client_from_provider` 按 credential 构建 client；`construct_cloud_instance` 把 worker/worker_pool/cluster 映射为 `CloudInstanceCreate`；`generate_ssh_key_pair`/`key_bytes_to_openssh_pem`（RSA/ED25519）生成并转换密钥。
- `user_data.py`：`UserDataTemplate` 用 Jinja2 渲染 `#cloud-config`（写入 `/var/lib/gpustack/config.yaml`、生成 worker 容器启动脚本），按需注入 NVIDIA 驱动安装（dkms + cuda + nvidia-container-toolkit）、`nvidia-ctk` 配置与安装后重启（`post_boot_service` oneshot）。

## Flow
server 侧 worker 池控制器（cluster provider 管理）→ `get_client_from_provider` 取 client → 按上述生命周期依次创建 SSH key / 实例 / 等待就绪 / 挂卷 → worker 容器经 user_data 自举注册回 server；销毁反向执行并回收卷与密钥。

## Integration
- 消费方：server 的集群/worker 池管理逻辑（基于 `ClusterProvider`/`CloudCredential`/`Worker` schema）。
- 依赖：`pydo`（DigitalOcean API）、`cryptography`（密钥生成）、`jinja2`/`yaml`（user_data 渲染）、`gpustack_runtime.detector.ManufacturerEnum`（GPU 厂商识别）、`gpustack.schemas.clusters`。
- 目前仅实现 DigitalOcean 一个厂商，`factory` 为扩展点。
