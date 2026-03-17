#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker &>/dev/null; then
  echo "错误: 未找到 docker，请先安装 Docker。"
  exit 1
fi

if [ ! -f openclaw-config.json ]; then
  echo "错误: 未找到 openclaw-config.json 配置文件。"
  exit 1
fi

# 停止已有容器
echo "正在停止已有容器..."
docker compose down 2>/dev/null || true

# 确保 volume 目录权限正确（使用 compose 项目名对应的 volume）
VOLUME_NAME="$(basename "$(pwd)")_openclaw_data"
echo "正在初始化数据目录权限 (${VOLUME_NAME})..."
docker run --rm --user root \
  -v "${VOLUME_NAME}:/home/node/.openclaw" \
  alpine/openclaw:latest \
  sh -c "mkdir -p /home/node/.openclaw && chown -R node:node /home/node/.openclaw"

echo "正在启动 OpenClaw..."
docker compose up -d

# 读取配置信息
TOKEN=$(python3 -c "import json; print(json.load(open('openclaw-config.json'))['gateway']['auth']['token'])" 2>/dev/null || echo "")
BASE_URL=$(python3 -c "import json; c=json.load(open('openclaw-config.json')); p=list(c['models']['providers'].values())[0]; print(p['baseUrl'])" 2>/dev/null || echo "unknown")
MODEL=$(python3 -c "import json; c=json.load(open('openclaw-config.json')); p=list(c['models']['providers'].values())[0]; print(p['models'][0]['id'])" 2>/dev/null || echo "unknown")

echo ""
echo "=========================================="
echo "  OpenClaw 启动完成"
echo "=========================================="
echo "  LiteLLM 代理: ${BASE_URL}"
echo "  模型:         ${MODEL}"
echo "------------------------------------------"
if [ -n "$TOKEN" ]; then
  echo "  Web UI: http://localhost:18789/?token=${TOKEN}"
else
  echo "  Web UI: http://localhost:18789"
fi
echo "=========================================="
