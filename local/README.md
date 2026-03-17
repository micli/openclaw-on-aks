# 本地 OpenClaw + AKS LiteLLM 混合部署

本文件夹包含部署混合 OpenClaw 环境的逻辑：

*   **AKS**: 运行 LiteLLM 代理，利用托管标识（Managed Identity）安全地访问 Azure OpenAI。
*   **本地计算机**: 通过 Docker 运行 OpenClaw，配置为使用 AKS 上的 LiteLLM 代理。

## 文件说明

| 文件 | 说明 |
|------|------|
| `azure-openai.json` | Azure OpenAI 配置模板，需填入你的账户信息 |
| `deploy_hybrid.py` | 一键部署脚本：创建 AKS 集群、部署 LiteLLM、生成本地配置 |
| `start.sh` | 本地启动脚本：启动 OpenClaw Docker 容器 |
| `requirements.txt` | Python 依赖 |

## 前置条件

1.  Python 3.8 或更高版本
2.  已安装 Azure CLI 并完成登录 (`az login`)
3.  已安装 `kubectl`
4.  已安装 Docker

## 使用步骤

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 Azure OpenAI

编辑 `azure-openai.json`，填入你的 Azure OpenAI 账户信息：

```json
{
    "deployName": "openclaw",
    "region": "eastus2",
    "azureOpenAI": [
        {
            "name": "<your-azure-openai-account>",
            "endpoint": "https://<your-azure-openai-account>.openai.azure.com/",
            "resource_group": "<resource-group>"
        }
    ],
    "deploymentName": "<your-model-deployment-name>",
    "apiVersion": "2025-04-01-preview"
}
```

如果有多个 Azure OpenAI 实例，可在 `azureOpenAI` 数组中添加多项（支持跨订阅，通过 `subscription_id` 字段指定）。

### 3. 部署

```bash
python3 deploy_hybrid.py
```

脚本会自动完成：
- 创建 Azure 资源组和 AKS 集群
- 创建托管标识并分配 Azure OpenAI 访问权限
- 在 AKS 上部署 LiteLLM 代理
- 等待 LoadBalancer 分配外部 IP
- 在本地生成 `openclaw-config.json` 和 `docker-compose.yml`

### 4. 启动 OpenClaw

```bash
./start.sh
```

脚本会输出带 token 的访问地址，直接在浏览器中打开即可使用。

## 清理资源

删除 AKS 上的所有部署资源：

```bash
az group delete --name openclaw-RG --yes
```

停止本地容器：

```bash
docker compose down
```

