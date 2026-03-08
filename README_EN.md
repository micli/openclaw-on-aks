# OpenClaw on AKS Deployment Guide (v2.0)

This guide helps you deploy OpenClaw on Azure Kubernetes Service (AKS) with one click, and configures it to use LiteLLM as a proxy to connect to Azure OpenAI services.

## 📋 Table of Contents

- [Deployment Architecture](#deployment-architecture)
- [Architecture Highlights](#architecture-highlights)
- [Prerequisites](#prerequisites)
- [1. Prepare Azure OpenAI Resources](#1-prepare-azure-openai-resources)
- [2. Configure Project](#2-configure-project)
- [3. Execute Deployment](#3-execute-deployment)
- [4. Access Control UI](#4-access-control-ui)
- [5. Verification & Testing](#5-verification--testing)
- [6. Cost Estimation](#6-cost-estimation)

---

## Deployment Architecture

![architecture](imgs/openclaw-deployment-architecture.jpg)

### OpenClaw on AKS Architecture

This solution builds a complete microservices architecture containing an AI Agent Gateway and Core Business Platform on Azure Kubernetes Service (AKS) via automated scripts.

#### 1. Infrastructure Layer
*   **Azure Resource Group**: All resources are encapsulated in an independent resource group (default: `<DEPLOY_NAME>-RG`).
*   **Compute Cluster (AKS)**: Deploys a single-node (SKU `Standard_D2s_v5`) AKS cluster, enabling Managed Identity for simplified permission management.
*   **Namespace Isolation**: All application components are deployed in a dedicated `openclaw-ns` Kubernetes namespace, isolated from other systems.

#### 2. Component Layer

The architecture consists of two main workloads that work together via the Kubernetes internal network:

*   **LLM Agent Gateway (LiteLLM Proxy)**
    *   **Role**: Responsible for converting Azure OpenAI's proprietary API protocol into standard OpenAI-compatible format, acting as an adapter between OpenClaw and underlying models.
    *   **Configuration**: Mounts `litellm-config.yaml` via `ConfigMap`, automatically reading and configuring Azure OpenAI Endpoint and Key.
    *   **Security**: Uses a random `MASTER_KEY` generated at startup for API call authentication.
    *   **Network**: Exposed internally on port 4000 via a Service (LoadBalancer) named `*-llmproxy-svc`.

*   **OpenClaw Core Platform**
    *   **Role**: Provides AI Agent orchestration, Control UI, and business logic processing.
    *   **Configuration**: Mounts `openclaw-config.json` via `ConfigMap`. The configuration file hardcodes the internal cluster DNS address pointing to LiteLLM Proxy (e.g., `http://<name>-llmproxy-svc.openclaw-ns.svc.cluster.local:4000/v1`), ensuring traffic does not go through the public internet.
    *   **Storage**: Uses `emptyDir` volume mounted at `/home/node/.openclaw` for storing temporary runtime data (Note: Data resets after Pod restart).
    *   **Network**: Exposed via a Service (LoadBalancer) named `*-svc`, mapping container port 18789 to service port 80.

#### 3. Access & Security

*   **User Access Path**:
    *   Since OpenClaw Control UI relies on WebCrypto API (requiring HTTPS or localhost), the architecture recommends using **Localhost Tunnel**.
    *   User -> `kubectl port-forward` (Local 18789) -> OpenClaw Service (In-Cluster).
    *   Access is protected by a randomly generated `OPENCLAW_TOKEN`.

*   **AI Call Path**:
    *   OpenClaw -> (Cluster Internal Network) -> LiteLLM Proxy -> (Public Internet HTTPS) -> Azure OpenAI Service.

---

## 🌟 Architecture Highlights

This solution uses a fully automated Python script (`deploy_openclaw.py`) for deployment, featuring the following core capabilities:

1.  **Zero-Key Management (Managed Identity)**:
    *   **Security Enhancement**: Completely eliminates storing Azure OpenAI API Keys in configuration files or environment variables.
    *   **Authentication**: Utilizes Azure **User Assigned Managed Identity** to automatically retrieve Entra ID Tokens.
    *   **RBAC Control**: The script automatically assigns the `Cognitive Services OpenAI User` role to the managed identity, implementing the principle of least privilege.

2.  **Dynamic Proxy Layer (LiteLLM Proxy)**:
    *   **Unified Interface**: LiteLLM Proxy converts Azure OpenAI specific APIs into standard OpenAI compatible interfaces, allowing OpenClaw to use them directly.
    *   **Custom Injection**: Uses a custom Python startup script (`run_proxy.py`) to dynamically inject the Azure Token Provider at runtime, ensuring automatic token refreshment.
    *   **Smart Routing**: Supports load balancing across multi-region/multi-resource Azure OpenAI instances (configured in `azure-openai.json`).

3.  **Kubernetes Native Integration**:
    *   **Secret Management**: Automatically generates and stores `Master Key` and `OpenClaw Token` into K8s Secrets. Subsequent deployments reuse them automatically without local files.
    *   **Configuration Injection**: All configurations are mounted via ConfigMap, supporting hot updates (requires Pod restart).
    *   **Service Discovery**: OpenClaw accesses the proxy layer directly via K8s internal DNS (`http://openclaw-llmproxy-svc...`), keeping traffic off the public internet.

---

## Prerequisites

Before starting, ensure your local environment has the following tools installed:

1.  **Azure CLI** (`az`)  
    *   [Installation Guide](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) - Used for managing Azure resources.
    *   Run `az login` after installation.

2.  **Kubernetes CLI** (`kubectl`)  
    *   [Installation Guide](https://kubernetes.io/docs/tasks/tools/) - Used for managing Kubernetes clusters.

3.  **Python 3.8+**
    *   Install dependencies:
        ```bash
        pip install azure-identity azure-mgmt-resource azure-mgmt-containerservice azure-mgmt-compute azure-mgmt-msi azure-mgmt-authorization azure-mgmt-cognitiveservices kubernetes
        ```

---

## 1. Prepare Azure OpenAI Resources

OpenClaw requires Large Language Model support. First, create an OpenAI resource on Azure.

1.  Visit [Azure Portal](https://portal.azure.com/).
2.  Create an **Azure OpenAI** resource.
    *   [Microsoft Docs: Create and deploy an Azure OpenAI Service resource](https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/create-resource?pivots=web-portal)
3.  Deploy a model in Azure OpenAI Studio (Recommended: **gpt-4o** or **gpt-5.2**).
    *   Remember your **Deployment Name**, needed for later configuration.

---

## 2. Configure Project

1.  Enter the `aks` directory:
    ```bash
    cd aks
    ```

2.  Edit or create `azure-openai.json` file:
    ```json
    {
      "deployName": "openclaw",
      "region": "eastus2",
      "deploymentName": "gpt-5.2",         // Deployment Name created in Azure OpenAI Studio
      "apiVersion": "2024-02-15-preview",  // Your Azure OpenAI API Version
      "azureOpenAI": [
        {
          "name": "resource-name-1",
          "endpoint": "https://resource-name-1.openai.azure.com/",
          "resource_group": "MyResourceGroup"
          // Note: "key" is not required here, script automatically uses Managed Identity
        },
        {
          "name": "resource-name-2",
          "endpoint": "https://resource-name-2.openai.azure.com/",
          "resource_group": "MyResourceGroup" 
          // Optional: "subscription_id": "xxx" if resource is in a different subscription
        }
      ]
    }
    ```
    *   `endpoint`: Your Azure OpenAI resource endpoint URL.
    *   `resource_group`: The Azure Resource Group where the resource resides.
    *   LiteLLM supports configuring multiple Azure OpenAI resources for load balancing.

---

## 3. Execute Deployment

Use the Python script for one-click deployment.

```bash
# Ensure you are in the aks directory
cd aks

# Run deployment script
python3 deploy_openclaw.py --config azure-openai.json
```

**The script automatically performs the following:**
1.  **Infrastructure**: Creates Resource Group, AKS Cluster, and User Assigned Managed Identity.
2.  **Authorization**: Assigns `Cognitive Services OpenAI User` role to the Managed Identity for Azure OpenAI.
3.  **Configuration**: Generates K8s Secrets (Master Key, OpenClaw Token) and ConfigMaps.
4.  **Deployment**: Applies K8s Manifests, starts LiteLLM Proxy and OpenClaw services.

---

## 4. Access Control UI

Since OpenClaw Control UI relies on WebCrypto API (requiring HTTPS or localhost environment), accessing directly via Cluster IP will result in unavailable functionality. We need to map the service to local using **Port Forwarding**.

1.  **Establish Local Tunnel**:
    After deployment script finishes, run the following in your terminal (keep terminal open):

    ```bash
    kubectl port-forward service/openclaw-svc 18789:80 -n openclaw-ns
    ```
    
    > **Note**: If you changed `deployName` in `azure-openai.json` (default is `openclaw`), replace `openclaw-svc` with `<YOUR_DEPLOY_NAME>-svc`.
    > **Tip**: This maps the Kubernetes service port 80 to your local computer's port 18789.

2.  **Get Token**:
    You need to retrieve the Token generated during deployment.
    ```bash
    # View Secret content
    kubectl get secret openclaw-secrets -n openclaw-ns -o jsonpath='{.data.openclaw_token}' | base64 -d
    ```
    Or view `openclaw-secrets` directly in Kubernetes Dashboard.

3.  **Open Browser**:
    Visit the following address:

    **`http://127.0.0.1:18789/?token=<YOUR_TOKEN>`**

---

## 5. Verification & Testing

1.  **Enter Overview Page**:
    Click **Overview** in the Control UI left menu. If the correct Token string was entered in Access Token, it should show "Gateway Connected".
    ![OpenClaw Chat UI](imgs/openclaw-overview.png)

2.  **Enter Chat Page**:
    Click **Chat** in the Control UI left menu.

3.  **Send Message**:
    Enter a test message in the dialog box, e.g., "Hello".

4.  **Confirm Response**:
    If OpenClaw replies successfully, the connection link (OpenClaw -> LiteLLM -> Azure OpenAI) is working normally.
    ![OpenClaw Chat UI](imgs/openclaw-chat.png)

5.  **Set Channel**:
    Click **Channel** in the Control UI left menu. Enter credential and url for corresponding social media App to ensure OpenClaw connects to social media.
    ![Channel](imgs/openclaw-channel.png)

### Common Troubleshooting

*   **Error "device identity required"**:
    *   Cause: You might be using direct IP access (http://20.x.x.x).
    *   Solution: Ensure you use `kubectl port-forward` and access via `http://127.0.0.1:18789`.

*   **Chat page unresponsive or error**:
    *   Check LiteLLM logs: `kubectl logs -l app=openclaw-llmproxy -n openclaw-ns -f`
    *   Confirm if Managed Identity has correct permissions (Role Assignments) for the OpenAI resource.

---

## 6. Cost Estimation

The following costs are based on **East US 2** region, for reference only (calculated monthly, 730 hours):

| Resource Type | SKU | Quantity | Est. Cost (Monthly) | Note |
| :--- | :--- | :--- | :--- | :--- |
| **AKS Cluster** | Standard Tier (Base) | 1 | Free | Management plane is free if SLA guarantee not enabled |
| **VM Node** | **Standard_B2s** | 1 | ~$30.37 | 2 vCPU, 4GB RAM (Suitable for testing/lightweight) |
| **Managed Disk** | P10 (128GB) | 1 | ~$5.89 | Node OS Disk |
| **Public IP** | Standard | 1 | ~$3.65 | Used for Load Balancer |
| **Load Balancer** | Standard | 1 | ~$18.00 | Data processing fees extra |
| **Total** | | | **~$58 / Month** | Excludes OpenAI usage fees |

> **Cost Saving Tips**: 
> *   After testing, use `az group delete -n <DEPLOY_NAME>-RG` to delete the entire resource group to stop billing.
> *   Production environments recommended to use at least `Standard_D2s_v5` (~$70/month) for better performance.

## 🛠️ Common Operations

*   **Restart Services**:
    ```bash
    kubectl rollout restart deployment openclaw -n openclaw-ns
    ```
*   **View Logs**:
    ```bash
    # OpenClaw Logs
    kubectl logs -l app=openclaw -n openclaw-ns -f

    # LiteLLM Proxy Logs (Check Auth Injection)
    kubectl logs -l app=openclaw-llmproxy -n openclaw-ns -f
    ```
