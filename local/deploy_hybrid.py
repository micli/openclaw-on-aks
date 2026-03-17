#!/usr/bin/env python3
"""
Deploy LiteLLM on AKS to proxy Azure OpenAI, then configure local OpenClaw run.
This script:
1. Deploys/Configures AKS with Managed Identity for Azure OpenAI access.
2. Deploys LiteLLM as a proxy on AKS.
3. Generates local configuration for OpenClaw to use the AKS proxy.
4. Generates a docker-compose.yml for running OpenClaw locally.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignmentCreateParameters
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.msi import ManagedServiceIdentityClient
from azure.mgmt.resource import ResourceManagementClient
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

# Constants
OPENAI_USER_ROLE_ID_GUID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"  # Cognitive Services OpenAI User
DEFAULT_LITELLM_IMAGE = "ghcr.io/berriai/litellm:main-latest"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
def log_info(msg):
    print(f"\033[94m[INFO]\033[0m {msg}")

def log_success(msg):
    print(f"\033[92m[SUCCESS]\033[0m {msg}")

def log_warning(msg):
    print(f"\033[93m[WARNING]\033[0m {msg}")

def log_error(msg):
    print(f"\033[91m[ERROR]\033[0m {msg}", file=sys.stderr)

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def get_subscription_id():
    credential = DefaultAzureCredential()
    from azure.mgmt.subscription import SubscriptionClient
    sub_client = SubscriptionClient(credential)
    try:
        if "AZURE_SUBSCRIPTION_ID" in os.environ:
            return os.environ["AZURE_SUBSCRIPTION_ID"]
        # Respect 'az account set' default subscription
        try:
            result = subprocess.run(["az", "account", "show", "--query", "id", "-o", "tsv"], capture_output=True, text=True, check=True)
            sub_id = result.stdout.strip()
            if sub_id:
                return sub_id
        except Exception:
            pass
        subs = list(sub_client.subscriptions.list())
        if not subs:
            raise RuntimeError("No Azure subscriptions found in current context.")
        return subs[0].subscription_id
    except Exception as e:
        log_error(f"Failed to get subscription ID: {e}")
        sys.exit(1)

def extract_account_name_from_endpoint(endpoint: str) -> Optional[str]:
    match = re.search(r"https://([^.]+)\.openai\.azure\.com", endpoint)
    return match.group(1) if match else None

# -----------------------------------------------------------------------------
# Deployment Class (Adapted from aks/deploy_openclaw.py)
# -----------------------------------------------------------------------------
class AzureDeployer:
    def __init__(self, subscription_id):
        self.subscription_id = subscription_id
        self.credential = DefaultAzureCredential()
        
        self.resource_client = ResourceManagementClient(self.credential, subscription_id)
        self.msi_client = ManagedServiceIdentityClient(self.credential, subscription_id)
        self.aks_client = ContainerServiceClient(self.credential, subscription_id)
        self.compute_client = ComputeManagementClient(self.credential, subscription_id)
        self.auth_client = AuthorizationManagementClient(self.credential, subscription_id)
        self.cog_client = CognitiveServicesManagementClient(self.credential, subscription_id)

    def ensure_resource_group(self, name, location):
        log_info(f"Ensuring Resource Group exists: {name}")
        self.resource_client.resource_groups.create_or_update(name, {"location": location})
        log_success(f"Resource Group {name} ready.")

    def ensure_managed_identity(self, rg_name, mi_name, location):
        log_info(f"Ensuring User Assigned Identity exists: {mi_name}")
        identity = self.msi_client.user_assigned_identities.create_or_update(
            rg_name, mi_name, {"location": location}
        )
        log_success(f"Managed Identity {mi_name} ready. ClientID: {identity.client_id}")
        return identity

    def ensure_aks_cluster(self, rg_name, cluster_name, location, node_count=1, vm_size="Standard_B2s"):
        log_info(f"Checking AKS Cluster: {cluster_name}")
        try:
            cluster = self.aks_client.managed_clusters.get(rg_name, cluster_name)
            log_success(f"AKS Cluster {cluster_name} exists.")
            return cluster
        except Exception:
            log_info(f"Creating AKS Cluster {cluster_name}... (this may take several minutes)")
            poller = self.aks_client.managed_clusters.begin_create_or_update(
                rg_name, cluster_name,
                {
                    "location": location,
                    "dns_prefix": f"{cluster_name}-dns",
                    "agent_pool_profiles": [{
                        "name": "nodepool1",
                        "count": node_count,
                        "vm_size": vm_size,
                        "mode": "System"
                    }],
                    "identity": {"type": "SystemAssigned"},
                    "sku": {"name": "Base", "tier": "Standard"}
                }
            )
            cluster = poller.result()
            log_success(f"AKS Cluster {cluster_name} created.")
        return cluster

    def get_kubectl_credentials(self, rg_name, cluster_name):
        log_info(f"Getting credentials for AKS cluster {cluster_name}...")
        # Use az cli for ease of kubeconfig merging
        subprocess.run(["az", "aks", "get-credentials", "--resource-group", rg_name, "--name", cluster_name, "--overwrite-existing"], check=True)

    def assign_role(self, principal_id, scope, role_definition_id, target_subscription_id=None):
        current_sub_id = self.subscription_id
        target_sub_id = target_subscription_id if target_subscription_id else current_sub_id
        
        # We need a client for the target subscription if different
        if target_sub_id != current_sub_id:
            auth_client = AuthorizationManagementClient(self.credential, target_sub_id)
        else:
            auth_client = self.auth_client

        # Role assignment name must be a GUID
        # Use deterministic GUID based on input to avoid duplication errors on re-run
        assignment_name = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{principal_id}-{scope}-{role_definition_id}"))
        
        try:
            auth_client.role_assignments.create(
                scope,
                assignment_name,
                RoleAssignmentCreateParameters(
                    role_definition_id=f"/subscriptions/{target_sub_id}/providers/Microsoft.Authorization/roleDefinitions/{role_definition_id}",
                    principal_id=principal_id,
                    principal_type="ServicePrincipal"
                )
            )
            log_success(f"Role assigned on scope: {scope}")
        except Exception as e:
            if "RoleAssignmentExists" in str(e):
                log_success(f"Role already assigned on scope: {scope}")
            else:
                log_warning(f"Failed to assign role on {scope}: {e}")

# -----------------------------------------------------------------------------
# Main Deployment Logic
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Deploy LiteLLM to AKS and Configure Local OpenClaw")
    parser.add_argument("--config", default="azure-openai.json", help="Path to azure-openai.json")
    args = parser.parse_args()

    # 1. Load Config
    if not os.path.exists(args.config):
        log_error(f"Config file {args.config} not found.")
        sys.exit(1)
    
    with open(args.config, "r") as f:
        azure_conf = json.load(f)

    subscription_id = get_subscription_id()
    deployer = AzureDeployer(subscription_id)

    deploy_name = azure_conf.get("deployName", "openclaw")
    region = azure_conf.get("region", "eastus2")
    rg_name = f"{deploy_name}-RG"
    aks_name = f"{deploy_name}-aks"
    mi_name = f"{deploy_name}-identity"
    model_name = azure_conf.get("deploymentName", "gpt-5.2")
    
    log_info(f"Deploying with Name: {deploy_name}, Region: {region}, RG: {rg_name}")

    # 2. Infrastructure Setup
    deployer.ensure_resource_group(rg_name, region)
    identity = deployer.ensure_managed_identity(rg_name, mi_name, region)
    
    # 3. Role Assignments (Cognitive Services OpenAI User)
    log_info("Assigning roles to Managed Identity...")
    for item in azure_conf.get("azureOpenAI", []):
        res_group = item.get("resource_group", rg_name)
        account_name = item.get("name")
        target_sub = item.get("subscription_id", subscription_id)
        
        # Construct scope manually to avoid listing all resources
        scope = f"/subscriptions/{target_sub}/resourceGroups/{res_group}/providers/Microsoft.CognitiveServices/accounts/{account_name}"
        deployer.assign_role(identity.principal_id, scope, OPENAI_USER_ROLE_ID_GUID, target_sub)

    # 4. AKS Cluster Setup (Ensure it exists)
    cluster = deployer.ensure_aks_cluster(rg_name, aks_name, region)
    
    # 5. Connect Identity to AKS (VMSS)
    # This allows pods on the nodes to use the identity if utilizing 'aad-pod-identity' or similiar binding
    # For simplicity, we attach the identity to the VMSS of the node pool so any pod can use it w/ client_id
    node_rg = cluster.node_resource_group
    # Need to find VMSS
    vmss_list = list(deployer.compute_client.virtual_machine_scale_sets.list(node_rg))
    if vmss_list:
        vmss = vmss_list[0]
        # Attach identity logic (simplified from aks copy)
        log_info(f"Attaching Managed Identity to VMSS: {vmss.name}")
        vmss_obj = deployer.compute_client.virtual_machine_scale_sets.get(node_rg, vmss.name)
        
        user_ids = vmss_obj.identity.user_assigned_identities if (vmss_obj.identity and vmss_obj.identity.user_assigned_identities) else {}
        if identity.id not in user_ids:
            user_ids[identity.id] = {}
            id_type = "UserAssigned"
            if vmss_obj.identity and "SystemAssigned" in str(vmss_obj.identity.type):
                id_type = "SystemAssigned, UserAssigned"
            
            deployer.compute_client.virtual_machine_scale_sets.begin_update(
                node_rg, vmss.name,
                {"identity": {"type": id_type, "user_assigned_identities": user_ids}}
            ).result()
            
            deployer.compute_client.virtual_machine_scale_sets.begin_update_instances(
                node_rg, vmss.name,
                {"instance_ids": ["*"]}
            ).result()
            log_success("Identity attached to AKS VMSS.")
    
    # 6. Deploy LiteLLM to AKS
    deployer.get_kubectl_credentials(rg_name, aks_name)
    
    # Generate Secrets (Master Key)
    namespace = "openclaw-ns"
    master_key = str(uuid.uuid4()).replace("-", "")
    
    # Prepare Manifests
    manifests = []
    manifests.append(f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
""")
    
    manifests.append(f"""
apiVersion: v1
kind: Secret
metadata:
  name: litellm-secrets
  namespace: {namespace}
type: Opaque
data:
  master_key: {base64.b64encode(master_key.encode()).decode()}
""")

    # LiteLLM Config
    litellm_config = {
        "model_list": []
    }
    
    for item in azure_conf.get("azureOpenAI", []):
        litellm_config["model_list"].append({
            "model_name": model_name, # Map all to same model name for load balancing
            "litellm_params": {
                "model": f"azure/{model_name}",
                "api_base": item["endpoint"],
                "api_version": azure_conf.get("apiVersion", "2024-02-15-preview"),
                "api_key": "os.environ/AZURE_OPENAI_API_KEY" # Placeholder, we use managed identity
            }
        })

    indented_yaml = "\n".join(["    " + line for line in yaml.dump(litellm_config).splitlines()])
    
    run_proxy_script = """
import os
import sys
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

print("Starting LiteLLM Proxy Wrapper...")

# Authenticate with Managed Identity
try:
    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        print(f"Using Managed Identity Credential with Client ID: {client_id}")
        credential = ManagedIdentityCredential(client_id=client_id)
    else:
        print("Using DefaultAzureCredential")
        credential = DefaultAzureCredential()
        
    token = credential.get_token("https://cognitiveservices.azure.com/.default")
    print("Successfully acquired Azure Token")
    
    # Set environment variables for LiteLLM to pick up
    # LiteLLM supports 'azure_ad_token' in model params or env vars
    # But usually for Azure, we simply set AZURE_OPENAI_API_KEY to the token 
    # (Azure AD tokens can be used as API keys for Cognitive Services)
    os.environ["AZURE_OPENAI_API_KEY"] = token.token
    
except Exception as e:
    print(f"Failed to acquire token: {e}")
    # Don't exit, maybe it works anyway or env var provided?

# Point LiteLLM to the config file
os.environ["LITELLM_CONFIG_PATH"] = "/app/config/litellm-config.yaml"
    
# Start LiteLLM via CLI to properly load config
sys.argv = ["litellm", "--config", "/app/config/litellm-config.yaml", "--host", "0.0.0.0", "--port", "4000"]
from litellm.proxy.proxy_cli import run_server
run_server()
"""
    indented_script = "\n".join(["    " + line for line in run_proxy_script.splitlines()])

    manifests.append(f"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-config
  namespace: {namespace}
data:
  litellm-config.yaml: |
{indented_yaml}
  run_proxy.py: |
{indented_script}
""")

    # LiteLLM Deployment & Service
    manifests.append(f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy
  namespace: {namespace}
  labels:
    app: litellm-proxy
spec:
  replicas: 1
  selector:
    matchLabels:
      app: litellm-proxy
  template:
    metadata:
      labels:
        app: litellm-proxy
    spec:
      containers:
        - name: litellm
          image: {DEFAULT_LITELLM_IMAGE}
          ports:
            - containerPort: 4000
          env:
            - name: LITELLM_MASTER_KEY
              value: "{master_key}"
            - name: AZURE_CLIENT_ID
              value: "{identity.client_id}"
            - name: PYTHONUNBUFFERED
              value: "1"
          command: ["python3"]
          args: ["/app/config/run_proxy.py"]
          volumeMounts:
            - name: config-volume
              mountPath: /app/config
          resources:
            requests:
              memory: "512Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "1000m"
      volumes:
        - name: config-volume
          configMap:
            name: litellm-config
---
apiVersion: v1
kind: Service
metadata:
  name: litellm-proxy-svc
  namespace: {namespace}
spec:
  selector:
    app: litellm-proxy
  ports:
    - protocol: TCP
      port: 4000
      targetPort: 4000
  type: LoadBalancer
""")

    log_info("Applying LiteLLM Manifests to AKS...")
    full_manifest = "\n---\n".join(manifests)
    
    try:
        subprocess.run(["kubectl", "apply", "-f", "-"], input=full_manifest.encode(), check=True)
    except subprocess.CalledProcessError as e:
        log_error(f"Failed to apply K8s manifests: {e}")
        sys.exit(1)

    # 7. Wait for IP
    log_info("Waiting for LiteLLM External IP...")
    external_ip = "Pending"
    for _ in range(30):
        time.sleep(10)
        try:
            res = subprocess.run(
                ["kubectl", "get", "svc", "litellm-proxy-svc", "-n", namespace, "-o", "json"],
                capture_output=True, check=True
            )
            svc_data = json.loads(res.stdout)
            ing = svc_data["status"]["loadBalancer"].get("ingress", [])
            if ing:
                external_ip = ing[0].get("ip")
                break
        except Exception:
            pass
            
    if external_ip == "Pending":
        log_warning("Could not retrieve External IP. Check kubectl.")
    else:
        log_success(f"LiteLLM deployed on AKS: http://{external_ip}:4000")

    # 8. Generate Local Configurations
    log_info("Generating local configuration files...")
    
    # OpenClaw Config
    openclaw_token = str(uuid.uuid4()).replace("-", "")
    openclaw_config = {
        "gateway": {
            "mode": "local",
            "port": 18789,
            "auth": {
                "token": openclaw_token
            }
        },
        "models": {
            "mode": "merge",
            "providers": {
                "litellm-aks": {
                    "api": "openai-completions",
                    "baseUrl": f"http://{external_ip}:4000",
                    "apiKey": master_key,
                    "models": [
                        {
                            "id": model_name,
                            "name": f"Azure OpenAI {model_name} (AKS)",
                            "reasoning": False,
                            "input": ["text", "image"],
                            "contextWindow": 128000,
                            "maxTokens": 16384
                        }
                    ]
                }
            }
        }
    }
    
    with open("openclaw-config.json", "w") as f:
        json.dump(openclaw_config, f, indent=4)
        
    # Docker Compose
    docker_compose = f"""services:
  openclaw:
    image: alpine/openclaw:latest
    container_name: openclaw-local
    environment:
      - OPENCLAW_GATEWAY_PORT=18789
      - OPENCLAW_CONFIG_PATH=/etc/openclaw/openclaw.json
      - OPENCLAW_STATE_DIR=/home/node/.openclaw
      - NODE_OPTIONS=--max-old-space-size=4096
    volumes:
      - ./openclaw-config.json:/etc/openclaw/openclaw.json:ro
      - openclaw_data:/home/node/.openclaw
    ports:
      - "18789:18790"
    extra_hosts:
      - "host.docker.internal:host-gateway"

  socat:
    image: alpine/socat
    container_name: openclaw-socat
    network_mode: "service:openclaw"
    depends_on:
      - openclaw
    command: "TCP-LISTEN:18790,fork,bind=0.0.0.0,reuseaddr TCP:127.0.0.1:18789"
    restart: on-failure

volumes:
  openclaw_data:
"""
    with open("docker-compose.yml", "w") as f:
        f.write(docker_compose)

    log_success("Configuration generated successfully!")

    # Final Summary
    print("\n" + "="*50)
    print("DEPLOYMENT COMPLETE")
    print("="*50)
    print(f"AKS LiteLLM Proxy:  http://{external_ip}:4000")
    print(f"LiteLLM Master Key: {master_key}")
    print(f"Model:              {model_name}")
    print("-" * 50)
    print(f"Managed Identity:   {mi_name}")
    print(f"Client ID:          {identity.client_id}")
    print("-" * 50)
    print(f"OpenClaw Web UI:    http://localhost:18789/?token={openclaw_token}")
    print("-" * 50)
    print("Next: Run './start.sh' to start OpenClaw locally.")
    print("="*50)

if __name__ == "__main__":
    main()
