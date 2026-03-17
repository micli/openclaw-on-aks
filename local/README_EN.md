# Local OpenClaw + AKS LiteLLM Hybrid Deployment

This directory contains logic to deploy a hybrid OpenClaw environment:

- **AKS**: Runs LiteLLM proxy with Managed Identity for secure Azure OpenAI access.
- **Local Machine**: Runs OpenClaw via Docker, configured to use the AKS LiteLLM proxy.

## Files

| File | Description |
|------|-------------|
| `azure-openai.json` | Azure OpenAI config template — fill in your account details |
| `deploy_hybrid.py` | One-click deploy: creates AKS cluster, deploys LiteLLM, generates local config |
| `start.sh` | Local startup script: launches OpenClaw Docker containers |
| `requirements.txt` | Python dependencies |

## Prerequisites

1. Python 3.8+
2. Azure CLI installed and logged in (`az login`)
3. `kubectl` installed
4. Docker installed

## Usage

### 1. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Azure OpenAI

Edit `azure-openai.json` with your Azure OpenAI account details:

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

For multiple Azure OpenAI instances, add entries to the `azureOpenAI` array (cross-subscription supported via `subscription_id` field).

### 3. Deploy

```bash
python3 deploy_hybrid.py
```

The script will automatically:
- Create Azure resource group and AKS cluster
- Create Managed Identity with Azure OpenAI access roles
- Deploy LiteLLM proxy on AKS
- Wait for LoadBalancer external IP
- Generate `openclaw-config.json` and `docker-compose.yml` locally

### 4. Start OpenClaw

```bash
./start.sh
```

The script outputs a URL with auth token — open it in your browser to start using OpenClaw.

## Cleanup

Delete all AKS deployment resources:

```bash
az group delete --name openclaw-RG --yes
```

Stop local containers:

```bash
docker compose down
```
