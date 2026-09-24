# gpustack/k8s/

## Responsibility
Kubernetes 集群接入的 manifest 渲染：把 GPUStack 集群注册信息（ClusterRegistrationToken）渲染成一组可 `kubectl apply` 的 K8s 清单（Namespace、RBAC、Service、worker DaemonSet、镜像拉取 Secret、operator 部署），用于向第三方 K8s 集群下发 gpustack-operator 与 worker。

## Design
- `manifest_template.py`：核心为 `TemplateConfig`（继承 `ClusterRegistrationTokenPublic`），`render()` 依次用 Jinja2 渲染四个模板并拼接输出：
  - `manifests.jinja`：Namespace、注册 Secret（registration-token）、ServiceAccount、ClusterRoleBinding、Service；
  - `image_pull_secrets.jinja`：`kubernetes.io/dockerconfigjson` Secret（每个 `K8sOptions.image_credentials` 一项，名称索引化 `gpustack-image-pull-secret-{i}`，与 DaemonSet 的 imagePullSecrets 引用一致）；
  - `daemonset.jinja`：worker DaemonSet——恒有 CPU worker（`gpustack-worker`，nodeSelector `feature.gpustack.ai/acceleratable: "false"`）+ 每个 GPU 厂商一个 `gpustack-worker-<runtime>`（nodeSelector 追加 `feature.node.kubernetes.io/pci-<id>.present`，NFD 标签），按 `_RUNTIME_ORDER` 规范排序保证渲染确定性；多 vendor 时加 per-pod 标签 + podAntiAffinity；
  - `operator.jinja`：operator 的 Namespace/ServiceAccount/ClusterRoleBinding/ConfigMap/Deployment（含 `GPUSTACK_*` 环境变量）。
- `_MANUFACTURER_PCI_ID` 维护厂商→PCI vendor ID 映射（NVIDIA=10de、AMD=1002 等），`_node_selector_for`/`_cpu_node_selector_for` 合成节点选择器。
- 计算属性：`operator_image`（镜像注册表前缀）、`container_namespace`（从镜像名推断）、`multi_vendor_mode`、`namespace`、`operator_env`、`operator_instance_access_static_address`。
- `WorkerRenderSpec`/`ImagePullSecretRenderSpec` 为渲染数据模型，构造时预构建 workers 与 image_pull_secrets。

## Flow
集群注册路由（K8s provider）→ 组装 `TemplateConfig`（registration + k8s_options + runtimes）→ `render()` 输出多段 YAML → 下发/应用至目标集群 → operator 在集群内拉起 worker DaemonSet，worker 用 registration-token 回连 GPUStack server。

## Integration
- 消费方：集群注册 API（生成 K8s 集群的接入 manifest）。
- 依赖：`gpustack.gpu_instances.cluster_apis_util.get_namespace_name`（principal→namespace）、`gpustack.schemas.clusters`（K8sOptions 等）、`gpustack_runtime.detector.ManufacturerEnum`、jinja2/yaml/base64。
- 模板文件（.jinja）与本模块同目录，经 `pkg_resources` 加载。
