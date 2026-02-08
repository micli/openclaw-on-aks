# Deploy-OpenClaw-AKS.ps1
# 在 Azure AKS 上部署 LiteLLM 和 OpenClaw (PowerShell 版)

param (
    [string]$DeployName = "openclaw",
    [string]$Region = "eastus2",
    [string]$ModelName = "gpt-5.2"
)

$ErrorActionPreference = "Stop"

# ===========================================================================
# 辅助函数
# ===========================================================================
function Write-Info { param([string]$msg) Write-Host "[INFO] $msg" -ForegroundColor Blue }
function Write-Success { param([string]$msg) Write-Host "[SUCCESS] $msg" -ForegroundColor Green }
function Write-Warning { param([string]$msg) Write-Host "[WARNING] $msg" -ForegroundColor Yellow }
function Write-Error { param([string]$msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }

function Write-Step {
    param([string]$msg)
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "$msg" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
}

# ===========================================================================
# 变量定义
# ===========================================================================
$ResourceGroup = "${DeployName}-RG"
$AksClusterName = "${DeployName}-aks"
$Namespace = "openclaw-ns"
$LiteLLMDeployName = "${DeployName}-llmproxy"
$LiteLLMServiceName = "${DeployName}-llmproxy-svc"
$OpenClawServiceName = "${DeployName}-svc"
$ScriptDir = $PSScriptRoot
$AzureOpenAIConfig = Join-Path $ScriptDir "azure-openai.json"
$SecretsFile = Join-Path $ScriptDir ".secrets"

# ===========================================================================
# 检查必备工具
# ===========================================================================
Write-Step "检查必备工具"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Error "未安装 Azure CLI (az)，请先安装。"
    exit 1
}
Write-Success "Azure CLI 已安装"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    Write-Error "未安装 kubectl，请先安装。"
    exit 1
}
Write-Success "kubectl 已安装"

# 检查 Azure 登录状态
try {
    $null = az account show 2>&1
    Write-Success "Azure 已登录"
} catch {
    Write-Error "请先运行 'az login' 登录到 Azure。"
    exit 1
}

# ===========================================================================
# 读取 Azure OpenAI 配置
# ===========================================================================
Write-Step "读取 Azure OpenAI 配置"

if (-not (Test-Path $AzureOpenAIConfig)) {
    Write-Error "配置文件不存在: $AzureOpenAIConfig"
    exit 1
}

$JsonConfig = Get-Content $AzureOpenAIConfig -Raw | ConvertFrom-Json
$ApiVersion = $JsonConfig.apiVersion
$DeploymentName = $JsonConfig.deploymentName

Write-Info "模型名称: $ModelName"
Write-Info "API 版本: $ApiVersion"
Write-Info "部署名称: $DeploymentName"

# ===========================================================================
# 生成密钥 (每次强制重新生成)
# ===========================================================================
Write-Step "配置安全密钥"

if (Test-Path $SecretsFile) {
    Write-Info "发现现有 .secrets 文件，正在重新生成密钥以确保安全性..."
}

Write-Info "生成新的随机密钥..."
# Generate 32 char hex string (16 bytes)
$MasterKey = -join ((1..32) | ForEach-Object { "0123456789abcdef"[(Get-Random -Maximum 16)] })
$OpenClawToken = -join ((1..32) | ForEach-Object { "0123456789abcdef"[(Get-Random -Maximum 16)] })

# Save to .secrets
"MASTER_KEY=$MasterKey" | Out-File -FilePath $SecretsFile -Encoding utf8
"OPENCLAW_TOKEN=$OpenClawToken" | Out-File -FilePath $SecretsFile -Encoding utf8 -Append
Write-Info "新密钥已保存至: $SecretsFile"

Write-Info "LiteLLM Master Key: $MasterKey"
Write-Info "OpenClaw Token:     $OpenClawToken"

# ===========================================================================
# 生成 LiteLLM 配置文件
# ===========================================================================
Write-Step "生成 LiteLLM 配置文件"

$LiteLLMConfigFile = Join-Path $ScriptDir "litellm-config.yaml"

$LiteLLMConfigContent = @"
model_list:
"@ 

foreach ($endpoint in $JsonConfig.azureOpenAI) {
    $EpUrl = $endpoint.endpoint.TrimEnd('/')
    $LiteLLMConfigContent += @"

  - model_name: $ModelName
    litellm_params:
      model: azure/$DeploymentName
      api_base: $EpUrl
      api_key: $($endpoint.key)
      api_version: $ApiVersion
"@
    Write-Info "已添加端点: $($endpoint.name) ($EpUrl)"
}

$LiteLLMConfigContent += @"

litellm_settings:
  drop_params: true
  set_verbose: false

general_settings:
  master_key: $MasterKey
"@

 Set-Content -Path $LiteLLMConfigFile -Value $LiteLLMConfigContent -Encoding UTF8
 Write-Success "LiteLLM 配置文件已生成: $LiteLLMConfigFile"

# ===========================================================================
# 创建资源组
# ===========================================================================
Write-Step "创建资源组: $ResourceGroup"

if (az group show --name $ResourceGroup 2>$null) {
    Write-Warning "资源组 $ResourceGroup 已存在，将使用现有资源组。"
} else {
    Write-Info "正在创建资源组..."
    az group create --name $ResourceGroup --location $Region --output none
    Write-Success "资源组 $ResourceGroup 创建成功"
}

# ===========================================================================
# 创建 AKS 集群
# ===========================================================================
Write-Step "创建 AKS 集群: $AksClusterName"

if (az aks show --name $AksClusterName --resource-group $ResourceGroup 2>$null) {
    Write-Warning "AKS 集群 $AksClusterName 已存在，将使用现有集群。"
} else {
    Write-Info "正在创建 AKS 集群（这可能需要 5-10 分钟）..."
    az aks create `
        --resource-group $ResourceGroup `
        --name $AksClusterName `
        --node-count 1 `
        --node-vm-size "Standard_D2s_v5" `
        --enable-managed-identity `
        --generate-ssh-keys `
        --location $Region `
        --output none
    Write-Success "AKS 集群 $AksClusterName 创建命令已完成"
}

# ===========================================================================
# 等待 AKS 集群就绪
# ===========================================================================
Write-Step "等待 AKS 集群就绪"
Write-Info "等待 AKS 集群进入运行状态..."

$AksReady = $false
for ($i=1; $i -le 60; $i++) {
    $AksState = az aks show --name $AksClusterName --resource-group $ResourceGroup --query "provisioningState" -o tsv 2>$null
    if ($AksState -eq "Succeeded") {
        $AksReady = $true
        break
    } elseif ($AksState -eq "Failed") {
        Write-Error "AKS 集群创建失败"
        exit 1
    }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 10
}
Write-Host ""

if (-not $AksReady) {
    Write-Error "等待 AKS 集群就绪超时"
    exit 1
}
Write-Success "AKS 集群已就绪"

# ===========================================================================
# 获取 AKS 凭证
# ===========================================================================
Write-Step "获取 AKS 凭证"
Write-Info "正在配置 kubectl..."
az aks get-credentials --resource-group $ResourceGroup --name $AksClusterName --overwrite-existing --output none
Write-Success "kubectl 已连接到 $AksClusterName"

# ===========================================================================
# 创建命名空间
# ===========================================================================
Write-Step "创建命名空间: $Namespace"

$NsCheck = kubectl get namespace $Namespace 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Warning "命名空间 $Namespace 已存在。"
} else {
    kubectl create namespace $Namespace
    Write-Success "命名空间 $Namespace 创建成功"
}

# ===========================================================================
# 创建 LiteLLM ConfigMap
# ===========================================================================
Write-Step "创建 LiteLLM ConfigMap"

$LiteLLMConfigIndented = $LiteLLMConfigContent -split "`n" | ForEach-Object { "    $_" } | Out-String

$LiteLLMConfigMapYaml = @"
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${DeployName}-llmproxy-config
  namespace: ${Namespace}
data:
  litellm-config.yaml: |
$LiteLLMConfigIndented
"@

$LiteLLMCMTemp = Join-Path $ScriptDir "litellm-configmap-temp.yaml"
Set-Content -Path $LiteLLMCMTemp -Value $LiteLLMConfigMapYaml -Encoding UTF8

kubectl delete configmap "${DeployName}-llmproxy-config" -n $Namespace --ignore-not-found
kubectl apply -f $LiteLLMCMTemp
Remove-Item $LiteLLMCMTemp -Force

Write-Success "LiteLLM ConfigMap 创建成功"

# ===========================================================================
# 部署 LiteLLM
# ===========================================================================
Write-Step "部署 LiteLLM"

$LiteLLMDeployContent = Get-Content (Join-Path $ScriptDir "litellm-deployment.yaml") -Raw
$LiteLLMDeployContent = $LiteLLMDeployContent -replace '\$\{DEPLOY_NAME\}', $DeployName
$LiteLLMDeployContent = $LiteLLMDeployContent -replace '\$\{MASTER_KEY\}', $MasterKey

$LiteLLMDeployTemp = Join-Path $ScriptDir "litellm-deploy-temp.yaml"
Set-Content -Path $LiteLLMDeployTemp -Value $LiteLLMDeployContent -Encoding UTF8

kubectl apply -f $LiteLLMDeployTemp
Remove-Item $LiteLLMDeployTemp -Force

Write-Info "等待 LiteLLM Deployment 就绪..."
kubectl rollout status deployment/${LiteLLMDeployName} -n $Namespace --timeout=300s
Write-Success "LiteLLM 部署成功"

# ===========================================================================
# 获取 LiteLLM Service URL
# ===========================================================================
Write-Step "获取 LiteLLM Service 外部 IP"
Write-Info "等待 LoadBalancer 分配外部 IP..."

$LiteLLMExternalIp = ""
for ($i=1; $i -le 60; $i++) {
    $LiteLLMExternalIp = kubectl get svc $LiteLLMServiceName -n $Namespace -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if ($LiteLLMExternalIp) { break }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 5
}
Write-Host ""

if (-not $LiteLLMExternalIp) {
    Write-Error "无法获取 LiteLLM Service 外部 IP"
    exit 1
}

$LiteLLMUrl = "http://${LiteLLMExternalIp}:4000"
Write-Success "LiteLLM Service URL: $LiteLLMUrl"

# ===========================================================================
# 测试 LiteLLM 服务
# ===========================================================================
Write-Step "测试 LiteLLM 服务"
Start-Sleep -Seconds 10

$TestPayload = @{
    model = $ModelName
    messages = @(
        @{ role = "user"; content = "Hello" }
    )
} | ConvertTo-Json -Depth 5 -Compress

# Escape quotes for curl/invoke-restmethod if needed, but PowerShell Invoke-RestMethod is easier
try {
    $Response = Invoke-RestMethod -Uri "${LiteLLMUrl}/chat/completions" `
        -Method Post `
        -Headers @{ "Authorization" = "Bearer $MasterKey"; "Content-Type" = "application/json" } `
        -Body $TestPayload `
        -TimeoutSec 60

    if ($Response.choices) {
        Write-Success "LiteLLM 测试成功！"
        Write-Host "响应内容: $($Response.choices[0].message.content)" -ForegroundColor Green
    } else {
        Write-Warning "LiteLLM returned unexpected response."
    }
} catch {
    Write-Warning "LiteLLM 测试请求失败 (可能服务尚未完全准备好): $_"
}

# ===========================================================================
# 创建 OpenClaw 配置文件
# ===========================================================================
Write-Step "生成 OpenClaw 配置文件"

$OpenClawConfigFile = Join-Path $ScriptDir "openclaw-config.json"

$OpenClawConfigContent = @"
{
  "gateway": {
    "mode": "local",
    "bind": "lan",
    "port": 18789,
    "auth": {
      "mode": "token",
      "token": "${OpenClawToken}"
    },
    "controlUi": {
      "enabled": true,
      "allowInsecureAuth": true
    },
    "trustedProxies": ["0.0.0.0/0"]
  },
  "agents": {
    "defaults": {
      "workspace": "/home/node/.openclaw/workspace",
      "model": {
        "primary": "litellm/${ModelName}"
      },
      "models": {
        "litellm/${ModelName}": {
          "alias": "Azure OpenAI ${ModelName}"
        }
      }
    }
  },
  "models": {
    "mode": "merge",
    "providers": {
      "litellm": {
        "baseUrl": "http://${DeployName}-llmproxy-svc.${Namespace}.svc.cluster.local:4000/v1",
        "apiKey": "${MasterKey}",
        "api": "openai-completions",
        "models": [
          {
            "id": "${ModelName}",
            "name": "Azure OpenAI ${ModelName}",
            "reasoning": false,
            "input": ["text", "image"],
            "contextWindow": 128000,
            "maxTokens": 16384
          }
        ]
      }
    }
  }
}
"@

Set-Content -Path $OpenClawConfigFile -Value $OpenClawConfigContent -Encoding UTF8
Write-Success "OpenClaw 配置文件已生成: $OpenClawConfigFile"

# ===========================================================================
# 创建 OpenClaw ConfigMap
# ===========================================================================
Write-Step "创建 OpenClaw ConfigMap"

kubectl delete configmap "${DeployName}-openclaw-config" -n $Namespace --ignore-not-found
kubectl create configmap "${DeployName}-openclaw-config" `
    --from-file=openclaw-config.json="${OpenClawConfigFile}" `
    -n $Namespace

Write-Success "OpenClaw ConfigMap 创建成功"

# ===========================================================================
# 部署 OpenClaw
# ===========================================================================
Write-Step "部署 OpenClaw"

$OpenClawDeployContent = Get-Content (Join-Path $ScriptDir "openclaw-deployment.yaml") -Raw
$OpenClawDeployContent = $OpenClawDeployContent -replace '\$\{DEPLOY_NAME\}', $DeployName

$OpenClawDeployTemp = Join-Path $ScriptDir "openclaw-deploy-temp.yaml"
Set-Content -Path $OpenClawDeployTemp -Value $OpenClawDeployContent -Encoding UTF8

kubectl apply -f $OpenClawDeployTemp
Remove-Item $OpenClawDeployTemp -Force

Write-Info "等待 OpenClaw Deployment 就绪..."
kubectl rollout status deployment/${DeployName} -n $Namespace --timeout=600s

Write-Success "OpenClaw 部署成功"

# ===========================================================================
# 获取 OpenClaw Service URL (Optional if using port-forward)
# ===========================================================================
Write-Step "完成部署"

Write-Host "============================================" -ForegroundColor Green
Write-Host "          部署信息摘要" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Azure 资源:" -ForegroundColor Cyan
Write-Host "  资源组:      $ResourceGroup"
Write-Host "  AKS 集群:    $AksClusterName"
Write-Host "  区域:        $Region"
Write-Host ""
Write-Host "Kubernetes 资源:" -ForegroundColor Cyan
Write-Host "  命名空间:    $Namespace"
Write-Host ""
Write-Host "访问指引:" -ForegroundColor Cyan
Write-Host "由于浏览器安全限制，HTTP 站点无法使用 WebCrypto API，导致 Control UI 报错。" -ForegroundColor Yellow
Write-Host "请使用 Port Forwarding (端口转发) 来作为 localhost 访问以绕过此限制。" -ForegroundColor Yellow
Write-Host ""
Write-Host "请在 PowerShell 中运行以下命令保持连接："
Write-Host "kubectl port-forward service/${OpenClawServiceName} 18789:80 -n ${Namespace}" -ForegroundColor Green
Write-Host ""
Write-Host "然后访问 Control UI:"
Write-Host "http://127.0.0.1:18789/?token=${OpenClawToken}" -ForegroundColor Green
Write-Host ""
