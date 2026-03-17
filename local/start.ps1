# PowerShell script to start OpenClaw locally
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "错误: 未找到 docker，请先安装 Docker。" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path "openclaw-config.json")) {
    Write-Host "错误: 未找到 openclaw-config.json 配置文件。" -ForegroundColor Red
    exit 1
}

# 停止已有容器
Write-Host "正在停止已有容器..."
docker compose down 2>$null

# 确保 volume 目录权限正确
$volumeName = "$(Split-Path -Leaf $PSScriptRoot)_openclaw_data"
Write-Host "正在初始化数据目录权限 ($volumeName)..."
docker run --rm --user root `
  -v "${volumeName}:/home/node/.openclaw" `
  alpine/openclaw:latest `
  sh -c "mkdir -p /home/node/.openclaw && chown -R node:node /home/node/.openclaw"

Write-Host "正在启动 OpenClaw..."
docker compose up -d

# 读取配置信息
try {
    $config = Get-Content "openclaw-config.json" -Raw | ConvertFrom-Json
    $token = $config.gateway.auth.token
    $provider = ($config.models.providers.PSObject.Properties | Select-Object -First 1).Value
    $baseUrl = $provider.baseUrl
    $model = $provider.models[0].id
} catch {
    $token = ""
    $baseUrl = "unknown"
    $model = "unknown"
}

Write-Host ""
Write-Host "=========================================="
Write-Host "  OpenClaw 启动完成"
Write-Host "=========================================="
Write-Host "  LiteLLM 代理: $baseUrl"
Write-Host "  模型:         $model"
Write-Host "------------------------------------------"
if ($token) {
    Write-Host "  Web UI: http://localhost:18789/?token=$token"
} else {
    Write-Host "  Web UI: http://localhost:18789"
}
Write-Host "=========================================="
