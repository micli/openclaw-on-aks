#!/bin/bash

##############################################################################
# deploy-openclaw-aks.sh
# 在 Azure AKS 上部署 LiteLLM 和 OpenClaw
##############################################################################

set -e

# ===========================================================================
# 参数定义 - 可通过命令行传入或直接修改这里的默认值
# ===========================================================================
DEPLOY_NAME="${1:-openclaw}"           # OpenClaw 项目的部署名
REGION="${2:-eastus2}"                 # Azure 资源所在的区域
MODELNAME="${3:-gpt-5.2}"              # Azure OpenAI 的模型名称

# ===========================================================================
# 派生变量
# ===========================================================================
RESOURCE_GROUP="${DEPLOY_NAME}-RG"
AKS_CLUSTER_NAME="${DEPLOY_NAME}-aks"
NAMESPACE="openclaw-ns"
LITELLM_DEPLOY_NAME="${DEPLOY_NAME}-llmproxy"
LITELLM_SERVICE_NAME="${DEPLOY_NAME}-llmproxy-svc"
OPENCLAW_SERVICE_NAME="${DEPLOY_NAME}-svc"

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AZURE_OPENAI_CONFIG="${SCRIPT_DIR}/azure-openai.json"

# ===========================================================================
# 彩色输出函数
# ===========================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_step() {
    echo -e "\n${CYAN}========================================${NC}"
    echo -e "${CYAN}$1${NC}"
    echo -e "${CYAN}========================================${NC}\n"
}

# ===========================================================================
# 检查必备工具
# ===========================================================================
print_step "检查必备工具"

if ! command -v az &> /dev/null; then
    print_error "未安装 Azure CLI，请先安装并配置。"
    exit 1
fi
print_success "Azure CLI 已安装"

if ! command -v kubectl &> /dev/null; then
    print_error "未安装 kubectl，请先安装。"
    exit 1
fi
print_success "kubectl 已安装"

if ! command -v jq &> /dev/null; then
    print_error "未安装 jq，请先安装（用于解析 JSON）。"
    exit 1
fi
print_success "jq 已安装"

# 检查 Azure 登录状态
if ! az account show &> /dev/null; then
    print_error "请先运行 'az login' 登录到 Azure。"
    exit 1
fi
print_success "Azure 已登录"

# ===========================================================================
# 读取 Azure OpenAI 配置
# ===========================================================================
print_step "读取 Azure OpenAI 配置"

if [[ ! -f "${AZURE_OPENAI_CONFIG}" ]]; then
    print_error "配置文件不存在: ${AZURE_OPENAI_CONFIG}"
    exit 1
fi

# 读取配置
API_VERSION=$(jq -r '.apiVersion' "${AZURE_OPENAI_CONFIG}")
DEPLOYMENT_NAME=$(jq -r '.deploymentName' "${AZURE_OPENAI_CONFIG}")

print_info "模型名称: ${MODELNAME}"
print_info "API 版本: ${API_VERSION}"
print_info "部署名称: ${DEPLOYMENT_NAME}"

# ===========================================================================
# 生成或读取密钥
# ===========================================================================
print_step "配置安全密钥"

SECRETS_FILE="${SCRIPT_DIR}/.secrets"

if [[ -f "${SECRETS_FILE}" ]]; then
    print_info "发现现有 .secrets 文件，正在重新生成密钥以确保安全性 (根据用户要求)..."
fi

print_info "生成新的随机密钥..."
MASTER_KEY=$(openssl rand -hex 16)
OPENCLAW_TOKEN=$(openssl rand -hex 16)

# 保存到 secrets 文件 (覆盖)
echo "MASTER_KEY=${MASTER_KEY}" > "${SECRETS_FILE}"
echo "OPENCLAW_TOKEN=${OPENCLAW_TOKEN}" >> "${SECRETS_FILE}"
print_info "新密钥已保存至: ${SECRETS_FILE}"

print_info "LiteLLM Master Key: ${MASTER_KEY}"
print_info "OpenClaw Token:     ${OPENCLAW_TOKEN}"

# ===========================================================================
# 生成 LiteLLM 配置文件 (litellm-config.yaml)
# ===========================================================================
print_step "生成 LiteLLM 配置文件"

LITELLM_CONFIG_FILE="${SCRIPT_DIR}/litellm-config.yaml"

# 构建 model_list
cat > "${LITELLM_CONFIG_FILE}" << EOF
model_list:
EOF

# 从 azure-openai.json 读取每个 Azure OpenAI 端点
ENDPOINT_COUNT=$(jq '.azureOpenAI | length' "${AZURE_OPENAI_CONFIG}")
for ((i=0; i<ENDPOINT_COUNT; i++)); do
    NAME=$(jq -r ".azureOpenAI[$i].name" "${AZURE_OPENAI_CONFIG}")
    ENDPOINT=$(jq -r ".azureOpenAI[$i].endpoint" "${AZURE_OPENAI_CONFIG}")
    KEY=$(jq -r ".azureOpenAI[$i].key" "${AZURE_OPENAI_CONFIG}")
    
    # 移除末尾的斜杠
    ENDPOINT="${ENDPOINT%/}"
    
    # LiteLLM Azure 配置: model 应该是 azure/<deployment-name>
    # model_name 是客户端调用时使用的名称，可以与模型名相同
    cat >> "${LITELLM_CONFIG_FILE}" << EOF
  - model_name: ${MODELNAME}
    litellm_params:
      model: azure/${DEPLOYMENT_NAME}
      api_base: ${ENDPOINT}
      api_key: ${KEY}
      api_version: ${API_VERSION}

EOF
    print_info "已添加端点: ${NAME} (${ENDPOINT}), 部署名: ${DEPLOYMENT_NAME}"
done

# 添加其他配置
cat >> "${LITELLM_CONFIG_FILE}" << EOF
litellm_settings:
  drop_params: true
  set_verbose: false

general_settings:
  master_key: ${MASTER_KEY}
EOF

print_success "LiteLLM 配置文件已生成: ${LITELLM_CONFIG_FILE}"

# ===========================================================================
# 创建资源组
# ===========================================================================
print_step "创建资源组: ${RESOURCE_GROUP}"

if az group show --name "${RESOURCE_GROUP}" &> /dev/null; then
    print_warning "资源组 ${RESOURCE_GROUP} 已存在，将使用现有资源组。"
else
    print_info "正在创建资源组..."
    az group create --name "${RESOURCE_GROUP}" --location "${REGION}" --output none
    print_success "资源组 ${RESOURCE_GROUP} 创建成功"
fi

# ===========================================================================
# 创建 AKS 集群
# ===========================================================================
print_step "创建 AKS 集群: ${AKS_CLUSTER_NAME}"

if az aks show --name "${AKS_CLUSTER_NAME}" --resource-group "${RESOURCE_GROUP}" &> /dev/null; then
    print_warning "AKS 集群 ${AKS_CLUSTER_NAME} 已存在，将使用现有集群。"
else
    print_info "正在创建 AKS 集群（这可能需要 5-10 分钟）..."
    # 使用 System Assigned Identity
    az aks create \
        --resource-group "${RESOURCE_GROUP}" \
        --name "${AKS_CLUSTER_NAME}" \
        --node-count 1 \
        --node-vm-size "Standard_D2s_v5" \
        --enable-managed-identity \
        --generate-ssh-keys \
        --location "${REGION}" \
        --output none
    print_success "AKS 集群 ${AKS_CLUSTER_NAME} 创建命令已完成"
fi


# ===========================================================================
# 等待 AKS 集群就绪
# ===========================================================================
print_step "等待 AKS 集群就绪"

print_info "等待 AKS 集群进入运行状态（最多等待 10 分钟）..."
AKS_READY=false
for i in {1..60}; do
    AKS_STATE=$(az aks show --name "${AKS_CLUSTER_NAME}" --resource-group "${RESOURCE_GROUP}" --query "provisioningState" -o tsv 2>/dev/null || echo "NotFound")
    if [[ "${AKS_STATE}" == "Succeeded" ]]; then
        AKS_READY=true
        break
    elif [[ "${AKS_STATE}" == "Failed" ]]; then
        print_error "AKS 集群创建失败"
        exit 1
    fi
    echo -n "."
    sleep 10
done
echo ""

if [[ "${AKS_READY}" != "true" ]]; then
    print_error "等待 AKS 集群就绪超时，当前状态: ${AKS_STATE}"
    exit 1
fi

print_success "AKS 集群已就绪"

# ===========================================================================
# 获取 AKS 凭证
# ===========================================================================
print_step "获取 AKS 凭证"

print_info "正在配置 kubectl 连接到 AKS 集群..."
az aks get-credentials \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${AKS_CLUSTER_NAME}" \
    --overwrite-existing \
    --output none

print_success "kubectl 已配置连接到 ${AKS_CLUSTER_NAME}"

# ===========================================================================
# 创建命名空间
# ===========================================================================
print_step "创建命名空间: ${NAMESPACE}"

if kubectl get namespace "${NAMESPACE}" &> /dev/null; then
    print_warning "命名空间 ${NAMESPACE} 已存在。"
else
    kubectl create namespace "${NAMESPACE}"
    print_success "命名空间 ${NAMESPACE} 创建成功"
fi

# ===========================================================================
# 创建 LiteLLM ConfigMap
# ===========================================================================
print_step "创建 LiteLLM ConfigMap"

# 生成临时 ConfigMap YAML 文件（替换变量）
LITELLM_CM_TEMP="${SCRIPT_DIR}/litellm-configmap-temp.yaml"
cat > "${LITELLM_CM_TEMP}" << EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${DEPLOY_NAME}-llmproxy-config
  namespace: ${NAMESPACE}
data:
  litellm-config.yaml: |
EOF

# 将 litellm-config.yaml 的内容添加到 ConfigMap（带缩进）
sed 's/^/    /' "${LITELLM_CONFIG_FILE}" >> "${LITELLM_CM_TEMP}"

# 删除旧的 ConfigMap（如果存在）并创建新的
kubectl delete configmap "${DEPLOY_NAME}-llmproxy-config" -n "${NAMESPACE}" --ignore-not-found
kubectl apply -f "${LITELLM_CM_TEMP}"
rm -f "${LITELLM_CM_TEMP}"

print_success "LiteLLM ConfigMap 创建成功"

# ===========================================================================
# 部署 LiteLLM
# ===========================================================================
print_step "部署 LiteLLM"

# 生成临时 Deployment YAML 文件（替换变量）
LITELLM_DEPLOY_TEMP="${SCRIPT_DIR}/litellm-deploy-temp.yaml"
# 注意：这里增加替换 IDENTITY_CLIENT_ID 和 TENANT_ID
sed -e "s/\${DEPLOY_NAME}/${DEPLOY_NAME}/g" \
    -e "s/\${MASTER_KEY}/${MASTER_KEY}/g" \
    "${SCRIPT_DIR}/litellm-deployment.yaml" > "${LITELLM_DEPLOY_TEMP}"

kubectl apply -f "${LITELLM_DEPLOY_TEMP}"
rm -f "${LITELLM_DEPLOY_TEMP}"


print_info "等待 LiteLLM Deployment 就绪..."
kubectl rollout status deployment/${LITELLM_DEPLOY_NAME} -n ${NAMESPACE} --timeout=300s

print_success "LiteLLM 部署成功"

# ===========================================================================
# 获取 LiteLLM Service URL
# ===========================================================================
print_step "获取 LiteLLM Service 外部 IP"

print_info "等待 LoadBalancer 分配外部 IP（最多等待 5 分钟）..."

LITELLM_EXTERNAL_IP=""
for i in {1..60}; do
    LITELLM_EXTERNAL_IP=$(kubectl get svc ${LITELLM_SERVICE_NAME} -n ${NAMESPACE} -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [[ -n "${LITELLM_EXTERNAL_IP}" ]]; then
        break
    fi
    echo -n "."
    sleep 5
done
echo ""

if [[ -z "${LITELLM_EXTERNAL_IP}" ]]; then
    print_error "无法获取 LiteLLM Service 的外部 IP"
    exit 1
fi

LITELLM_URL="http://${LITELLM_EXTERNAL_IP}:4000"
print_success "LiteLLM Service URL: ${LITELLM_URL}"

# ===========================================================================
# 测试 LiteLLM 服务
# ===========================================================================
print_step "测试 LiteLLM 服务"

print_info "发送测试请求到 LiteLLM..."
sleep 10  # 等待服务完全就绪

TEST_RESPONSE=$(curl -s -X POST "${LITELLM_URL}/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    -d "{\"model\": \"${MODELNAME}\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]}" \
    --max-time 60 || true)

if [[ -n "${TEST_RESPONSE}" ]] && echo "${TEST_RESPONSE}" | jq -e '.choices' &> /dev/null; then
    print_success "LiteLLM 测试成功！"
    echo -e "${GREEN}响应内容:${NC}"
    echo "${TEST_RESPONSE}" | jq '.choices[0].message.content' 2>/dev/null || echo "${TEST_RESPONSE}"
else
    print_warning "LiteLLM 测试返回异常，但继续部署..."
    echo "响应: ${TEST_RESPONSE}"
fi

# ===========================================================================
# 创建 OpenClaw 配置文件
# ===========================================================================
print_step "生成 OpenClaw 配置文件"

OPENCLAW_CONFIG_FILE="${SCRIPT_DIR}/openclaw-config.json"

# 根据 OpenClaw 文档配置 models.providers 使用 LiteLLM 作为 OpenAI 兼容提供者
cat > "${OPENCLAW_CONFIG_FILE}" << EOF
{
  "gateway": {
    "mode": "local",
    "bind": "lan",
    "port": 18789,
    "auth": {
      "mode": "token",
      "token": "${OPENCLAW_TOKEN}"
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
        "primary": "litellm/${MODELNAME}"
      },
      "models": {
        "litellm/${MODELNAME}": {
          "alias": "Azure OpenAI ${MODELNAME}"
        }
      }
    }
  },
  "models": {
    "mode": "merge",
    "providers": {
      "litellm": {
        "baseUrl": "http://${DEPLOY_NAME}-llmproxy-svc.${NAMESPACE}.svc.cluster.local:4000/v1",
        "apiKey": "${MASTER_KEY}",
        "api": "openai-completions",
        "models": [
          {
            "id": "${MODELNAME}",
            "name": "Azure OpenAI ${MODELNAME}",
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
EOF

print_success "OpenClaw 配置文件已生成: ${OPENCLAW_CONFIG_FILE}"

# ===========================================================================
# 创建 OpenClaw ConfigMap
# ===========================================================================
print_step "创建 OpenClaw ConfigMap"

# 删除旧的 ConfigMap（如果存在）并创建新的
kubectl delete configmap "${DEPLOY_NAME}-openclaw-config" -n "${NAMESPACE}" --ignore-not-found
kubectl create configmap "${DEPLOY_NAME}-openclaw-config" \
    --from-file=openclaw-config.json="${OPENCLAW_CONFIG_FILE}" \
    -n "${NAMESPACE}"

print_success "OpenClaw ConfigMap 创建成功"

# ===========================================================================
# 部署 OpenClaw
# ===========================================================================
print_step "部署 OpenClaw"

# 生成临时 Deployment YAML 文件（替换变量）
OPENCLAW_DEPLOY_TEMP="${SCRIPT_DIR}/openclaw-deploy-temp.yaml"
sed -e "s/\${DEPLOY_NAME}/${DEPLOY_NAME}/g" \
    "${SCRIPT_DIR}/openclaw-deployment.yaml" > "${OPENCLAW_DEPLOY_TEMP}"

kubectl apply -f "${OPENCLAW_DEPLOY_TEMP}"
rm -f "${OPENCLAW_DEPLOY_TEMP}"

print_info "等待 OpenClaw Deployment 就绪..."
kubectl rollout status deployment/${DEPLOY_NAME} -n ${NAMESPACE} --timeout=600s


print_success "OpenClaw 部署成功"

# ===========================================================================
# 获取 OpenClaw Service URL
# ===========================================================================
print_step "获取 OpenClaw Service 外部 IP"

print_info "等待 LoadBalancer 分配外部 IP（最多等待 5 分钟）..."

OPENCLAW_EXTERNAL_IP=""
for i in {1..60}; do
    OPENCLAW_EXTERNAL_IP=$(kubectl get svc ${OPENCLAW_SERVICE_NAME} -n ${NAMESPACE} -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [[ -n "${OPENCLAW_EXTERNAL_IP}" ]]; then
        break
    fi
    echo -n "."
    sleep 5
done
echo ""

if [[ -z "${OPENCLAW_EXTERNAL_IP}" ]]; then
    print_error "无法获取 OpenClaw Service 的外部 IP"
    exit 1
fi

OPENCLAW_URL="http://${OPENCLAW_EXTERNAL_IP}"

# ===========================================================================
# 输出部署结果
# ===========================================================================
print_step "部署完成！"

echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}          部署信息摘要${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "${CYAN}Azure 资源:${NC}"
echo -e "  资源组:      ${RESOURCE_GROUP}"
echo -e "  AKS 集群:    ${AKS_CLUSTER_NAME}"
echo -e "  区域:        ${REGION}"
echo ""
echo -e "${CYAN}Kubernetes 资源:${NC}"
echo -e "  命名空间:    ${NAMESPACE}"
echo ""
echo -e "${CYAN}LiteLLM 服务:${NC}"
echo -e "  Deployment:  ${LITELLM_DEPLOY_NAME}"
echo -e "  Service:     ${LITELLM_SERVICE_NAME}"
echo -e "  ${GREEN}URL:         ${LITELLM_URL}${NC}"
echo -e "  Master Key:  ${MASTER_KEY}"
echo -e "  模型名:      ${MODELNAME}"
echo -e "  部署名:      ${DEPLOYMENT_NAME}"

# ===========================================================================
# 访问指引
# ===========================================================================
print_step "访问指引"

echo -e "${YELLOW}由于浏览器安全限制，HTTP 站点无法使用 WebCrypto API，导致 Control UI 报错。${NC}"
echo -e "${YELLOW}请使用 Port Forwarding (端口转发) 来作为 localhost 访问以绕过此限制。${NC}"
echo ""
echo -e "请在新的终端窗口中运行以下命令保持连接："
echo -e "${GREEN}kubectl port-forward service/${OPENCLAW_SERVICE_NAME} 18789:80 -n ${NAMESPACE}${NC}"
echo ""
echo -e "然后访问 Control UI:"
echo -e "${GREEN}http://127.0.0.1:18789/?token=${OPENCLAW_TOKEN}${NC}"
echo ""
echo ""
echo -e "${CYAN}OpenClaw 服务:${NC}"
echo -e "  Deployment:  ${DEPLOY_NAME}"
echo -e "  Service:     ${OPENCLAW_SERVICE_NAME}"
echo -e "  ${GREEN}URL:         ${OPENCLAW_URL}${NC}"
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}请在浏览器中访问 OpenClaw Control UI:${NC}"
echo -e "${GREEN}${OPENCLAW_URL}${NC}"
echo -e "${GREEN}============================================${NC}"
