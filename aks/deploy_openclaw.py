#!/usr/bin/env python3
"""
Deploy OpenClaw & LiteLLM on AKS with Managed Identity Support.
Unifies logic from deploy-openclaw-aks.sh and deploy_mi_aks_litellm.py.
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
        # This iterates over available subscriptions. 
        # For simplicity, pick the first one or use AZURE_SUBSCRIPTION_ID env var if set.
        if "AZURE_SUBSCRIPTION_ID" in os.environ:
            return os.environ["AZURE_SUBSCRIPTION_ID"]
        subs = list(sub_client.subscriptions.list())
        if not subs:
            raise RuntimeError("No Azure subscriptions found in current context.")
        return subs[0].subscription_id
    except Exception as e:
        log_error(f"Failed to get subscription ID: {e}")
        sys.exit(1)

def extract_account_name_from_endpoint(endpoint: str) -> Optional[str]:
    # Endpoint format: https://<name>.openai.azure.com/ or similar
    match = re.search(r"https://([^.]+)\.openai\.azure\.com", endpoint)
    return match.group(1) if match else None

# -----------------------------------------------------------------------------
# Deployment Class
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
        log_info(f"Ensuring AKS Cluster exists: {cluster_name}")
        try:
            cluster = self.aks_client.managed_clusters.get(rg_name, cluster_name)
            log_success(f"AKS Cluster {cluster_name} already exists.")
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

    def get_aks_vmss(self, node_rg):
        # Find the VMSS in the node resource group
        vmss_list = list(self.compute_client.virtual_machine_scale_sets.list(node_rg))
        if not vmss_list:
            log_warning(f"No VMSS found in node resource group {node_rg}. AKS might be using Availability Sets?")
            return None
        return vmss_list[0]

    def attach_identity_to_vmss(self, node_rg, vmss_name, identity_id):
        log_info(f"Attaching Managed Identity to VMSS: {vmss_name}")
        vmss = self.compute_client.virtual_machine_scale_sets.get(node_rg, vmss_name)
        
        # Prepare identity update
        user_identities = vmss.identity.user_assigned_identities if (vmss.identity and vmss.identity.user_assigned_identities) else {}
        if identity_id in user_identities:
            log_success("Identity already attached to VMSS.")
            return

        user_identities[identity_id] = {}
        identity_type = "UserAssigned"
        # If SystemAssigned is already enabled, keep it
        if vmss.identity and "SystemAssigned" in str(vmss.identity.type):
             identity_type = "SystemAssigned, UserAssigned"

        poller = self.compute_client.virtual_machine_scale_sets.begin_update(
            node_rg, vmss_name,
            {
                "identity": {
                    "type": identity_type,
                    "user_assigned_identities": user_identities
                }
            }
        )
        poller.result()
        log_success("Identity attached to VMSS.")

        # Update instances to ensure latest model is applied
        log_info("Updating VMSS instances...")
        poller = self.compute_client.virtual_machine_scale_sets.begin_update_instances(
            node_rg, vmss_name,
            {
                "instance_ids": ["*"] # Update all instances
            }
        )
        poller.result()
        log_success("VMSS instances updated.")


    def find_cognitive_account(self, name):
        # Inefficient but effective: list all and match name. 
        # Ideally we would limit by logic but user config doesn't provide RG.
        accounts = self.cog_client.accounts.list()
        for account in accounts:
            if account.name.lower() == name.lower():
                return account
        return None

    def assign_role(self, principal_id, scope, role_definition_id, target_subscription_id=None):
        """
        Assign a role to a principal on a specific scope.
        Args:
            principal_id: The Object ID of the Managed Identity.
            scope: The full Azure Resource ID to assign the role on.
            role_definition_id: The GUID of the built-in role.
            target_subscription_id: The subscription ID where the target resource resides. 
                                    Defaults to the deployer's current subscription.
        """
        # Normalize subscription IDs
        current_sub_id = self.subscription_id.lower()
        scope_sub_id = target_subscription_id.lower() if target_subscription_id else current_sub_id
        
        # Determine if we need to switch context (Cross-Subscription)
        is_cross_subscription = (scope_sub_id != current_sub_id)
        
        if is_cross_subscription:
            log_info(f"Context Switch: Target Subscription {scope_sub_id} (Current: {current_sub_id})")
            # Create a client for the target subscription
            auth_client = AuthorizationManagementClient(self.credential, scope_sub_id)
        else:
            auth_client = self.auth_client

        # Check existing assignments
        try:
            # Note: For cross-subscription, listing might require higher privileges at subscription level.
            # If listing fails, we proceed to create (which might work if we have resource-level access).
            existing = list(auth_client.role_assignments.list_for_scope(
                scope, filter=f"principalId eq '{principal_id}'"
            ))
            
            for assign in existing:
                if assign.role_definition_id.lower().endswith(role_definition_id.lower()):
                    log_success(f"Role already assigned on {scope}")
                    return
        except Exception as e:
            log_warning(f"Could not check existing assignments (Permission issue?): {e}. Attempting to create anyway...")

        log_info(f"Assigning role to {principal_id} on {scope}...")
        
        # Get full role definition ID for the TARGET subscription
        full_role_id = f"/subscriptions/{scope_sub_id}/providers/Microsoft.Authorization/roleDefinitions/{role_definition_id}"
        
        assignment_name = str(uuid.uuid4())
        try:
            auth_client.role_assignments.create(
                scope,
                assignment_name,
                RoleAssignmentCreateParameters(
                    role_definition_id=full_role_id,
                    principal_id=principal_id,
                    principal_type="ServicePrincipal"
                )
            )
            log_success(f"Role assigned successfully on {scope}")
        except Exception as e:
            log_error(f"Failed to assign role: {e}")
            log_error("Ensure you have 'User Access Administrator' or 'Owner' permissions on the target resource or subscription.")


    def get_kubectl_credentials(self, rg_name, cluster_name):
        log_info("Getting AKS Credentials...")
        # Using AZ CLI for simplicity as Python SDK requires merging kubeconfig manually
        subprocess.check_call(
            ["az", "aks", "get-credentials", "--resource-group", rg_name, "--name", cluster_name, "--overwrite-existing"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        log_success("Kubeconfig updated.")

def retrieve_or_generate_secrets(rg_name, aks_name, namespace="openclaw-ns", secret_name="openclaw-secrets"):
    """
    Tries to retrieve secrets from Kubernetes. If not found, generates new ones.
    Returns: (master_key, openclaw_token)
    """
    import base64
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException

    try:
        # Load config
        log_info("Loading kubeconfig from ~/.kube/config ...")
        config.load_kube_config()
        
        v1 = client.CoreV1Api()
        
        # Check if namespace exists, if not, obviously secrets don't exist
        try:
             v1.read_namespace(namespace)
        except ApiException as e:
            if e.status == 404:
                raise Exception(f"Namespace {namespace} not found")
            else:
                raise e

        # Try read secret
        log_info(f"Checking for existing secrets in Secret '{secret_name}' (ns: {namespace})...")
        secret = v1.read_namespaced_secret(secret_name, namespace)
        
        if not secret.data:
            raise Exception("Secret data is empty")

        b64_master_key = secret.data.get("master_key", "")
        b64_openclaw_token = secret.data.get("openclaw_token", "")
        
        master_key = base64.b64decode(b64_master_key).decode("utf-8")
        openclaw_token = base64.b64decode(b64_openclaw_token).decode("utf-8")
        
        log_success("Found existing secrets. Reusing them.")
        return master_key, openclaw_token
        
    except Exception as e:
        # If 404 Not Found or other issues, generate new ones
        log_warning(f"Could not retrieve existing secrets ({e}). Generating new ones.")
        
        new_master_key = str(uuid.uuid4()).replace("-", "")
        new_openclaw_token = str(uuid.uuid4()).replace("-", "")
        return new_master_key, new_openclaw_token


def load_config(path: str):
    with open(path, "r", encoding='utf-8') as f:
        return json.load(f)

def render_litellm_config(azure_config, model_name_override=None):
    """
    Generate LiteLLM config dictionary.
    """
    model_list = []
    
    # Global settings
    if "apiVersion" not in azure_config:
        log_warning("No 'apiVersion' in config, defaulting to '2023-05-15'")
    api_version = azure_config.get("apiVersion", "2023-05-15")

    if "deploymentName" not in azure_config:
        log_warning("No 'deploymentName' in config, defaulting to 'gpt-3.5-turbo'")
    deployment_name = azure_config.get("deploymentName", "gpt-3.5-turbo")
    
    # Override model name if provided
    model_alias = model_name_override if model_name_override else deployment_name

    for item in azure_config.get("azureOpenAI", []):
        endpoint = item.get("endpoint")
        # key = item.get("key") # We intentionally IGNORE key for Managed Identity
        
        if not endpoint:
            continue

        endpoint = endpoint.rstrip("/")
        
        model_list.append({
            "model_name": model_alias,
            "litellm_params": {
                "model": f"azure/{deployment_name}",
                "api_base": endpoint,
                "api_version": api_version
                # "api_key": key # Omitted for MI.
                # LiteLLM automatically uses DefaultAzureCredential if AZURE_CLIENT_ID env var is present.
            }
        })
    
    return {
        "model_list": model_list,
        "litellm_settings": {
            "drop_params": True,
            "set_verbose": False
        }
    }

def main():
    parser = argparse.ArgumentParser(description="Deploy OpenClaw to AKS")
    parser.add_argument("--config", default="azure-openai.json", help="Path to azure-openai.json")
    # Optional overrides, but defaults come from config
    parser.add_argument("--deploy-name", help="Override deployment name prefix")
    parser.add_argument("--region", help="Override Azure Region")
    parser.add_argument("--model-name", help="Override Model Name Alias")
    
    args = parser.parse_args()
    config_path = args.config
    
    # Validations
    if not os.path.exists(config_path):
        log_error(f"Config file not found: {config_path}")
        sys.exit(1)
        
    azure_conf = load_config(config_path)

    # Load from config, fallback to args, then default
    deploy_name = args.deploy_name or azure_conf.get("deployName", "openclaw")
    region = args.region or azure_conf.get("region", "eastus2")
    # modelName is optional in json, fallback to deploymentName if not present
    model_name_default = azure_conf.get("deploymentName", "gpt-3.5-turbo")
    model_name = args.model_name or azure_conf.get("modelName", model_name_default)

    log_info(f"Configuration Loaded:")
    log_info(f"  Deploy Name: {deploy_name}")
    log_info(f"  Region:      {region}")
    log_info(f"  Model Name:  {model_name}")

    # Resource Names
    rg_name = f"{deploy_name}-RG"
    aks_name = f"{deploy_name}-aks"
    mi_name = f"{deploy_name}-identity"
    namespace = "openclaw-ns"
    
    # 1. Init Deployer
    sub_id = get_subscription_id()
    log_info("\n=== Step 1/6: Initialize Azure Context ===")
    log_info(f"Using Subscription ID: {sub_id}")
    deployer = AzureDeployer(sub_id)
    
    # 2. Infrastructure
    log_info("\n=== Step 2/6: Ensure Infrastructure (RG, Identity, AKS) ===")
    deployer.ensure_resource_group(rg_name, region)
    
    mi = deployer.ensure_managed_identity(rg_name, mi_name, region)
    mi_principal_id = mi.principal_id
    mi_client_id = mi.client_id
    mi_id = mi.id
    
    aks = deployer.ensure_aks_cluster(rg_name, aks_name, region)
    node_rg = aks.node_resource_group
    
    vmss = deployer.get_aks_vmss(node_rg)
    if vmss:
        deployer.attach_identity_to_vmss(node_rg, vmss.name, mi_id)
    else:
        log_warning("Skipping VMSS attachment (no VMSS found).")

    # 3. Role Assignments
    log_info("\n=== Step 3/6: Configure Role Assignments ===")
    log_info("Checking Azure OpenAI Role Assignments...")
    for item in azure_conf.get("azureOpenAI", []):
        endpoint = item.get("endpoint")
        if not endpoint: continue
        
        # Resource Parameters
        # JSON Schema for reference:
        # { "name": "...", "endpoint": "...", "resource_group": "...", "subscription_id": "..." }
        
        name_alias = item.get("name") # This might be the alias, or resource name
        target_rg = item.get("resource_group")
        target_sub_id = item.get("subscription_id")
        
        # Try to infer resource name from endpoint if not explicit
        # Assuming endpoint is https://<resource-name>.openai.azure.com/
        resource_name = extract_account_name_from_endpoint(endpoint)
        if not resource_name:
             # Fallback to name field if it looks like a resource name (vs alias)
             resource_name = name_alias

        if not resource_name:
            log_warning(f"Skipping entry: Could not determine resource name for {endpoint}")
            continue

        scope_sub = target_sub_id if target_sub_id else sub_id

        if target_rg:
            # Construct Resource ID directly
            resource_id = f"/subscriptions/{scope_sub}/resourceGroups/{target_rg}/providers/Microsoft.CognitiveServices/accounts/{resource_name}"
            log_info(f"Targeting Resource: {resource_name} (in RG: {target_rg}, Sub: {scope_sub})")
            deployer.assign_role(mi_principal_id, resource_id, OPENAI_USER_ROLE_ID_GUID, target_subscription_id=scope_sub)
        else:
            # Legacy/Fallback: Search in current subscription
            # This logic only works for current subscription resources
            if scope_sub != sub_id:
                log_warning(f"Cannot search for resource '{resource_name}' in another subscription ({scope_sub}) without 'resource_group'. Please update config.")
                continue

            log_info(f"Searching for Cognitive Account '{resource_name}' in current subscription...")
            account = deployer.find_cognitive_account(resource_name)
            if account:
                deployer.assign_role(mi_principal_id, account.id, OPENAI_USER_ROLE_ID_GUID)
            else:
                log_warning(f"Resource '{resource_name}' not found in current subscription. Provide 'resource_group' in config.")


    # 4. K8s Config Generation
    log_info("\n=== Step 4/6: Generate Secrets & Configs ===")
    
    # Secrets
    log_info("Retrieving kubeconfig to check for existing secrets...")
    # NOTE: We fetch credentials here even if they might be fetched again later
    # This allows us to check for existing secrets reliably
    deployer.get_kubectl_credentials(rg_name, aks_name)

    master_key, openclaw_token = retrieve_or_generate_secrets(rg_name, aks_name, namespace)
    log_info(f"Master Key: {master_key[:5]}... (hidden)")

    # Configs
    litellm_conf_data = render_litellm_config(azure_conf, model_name)
    # Add master key to config object directly to ensure clean YAML dump
    litellm_conf_data["general_settings"] = {"master_key": master_key}
    
    litellm_yaml_str = yaml.dump(litellm_conf_data)
    # Indent for ConfigMap
    indented_yaml = "\n".join(["    " + line for line in litellm_yaml_str.splitlines()])

    # Generate Python wrapper script for LiteLLM to handle Managed Identity
    run_proxy_script = """
import os
import sys
import yaml
import uvicorn
import litellm
# Try importing Router early to ensure availability
from litellm.router import Router
from litellm.proxy.proxy_server import app, proxy_config
# Import standard Azure identity components
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

def main():
    print("[Wrapper] Starting LiteLLM Proxy Wrapper with Managed Identity Support")

    # 1. Setup Azure Auth Provider
    print("[Wrapper] Initializing Azure Auth Provider...")
    try:
        # DefaultAzureCredential handles Managed Identity automatically when available
        credential = DefaultAzureCredential()
        # Scope is standard for Azure OpenAI
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        print("[Wrapper] Azure Auth Provider initialized successfully.")
    except Exception as e:
        print(f"[Wrapper] CRITICAL ERROR creating toke provider: {e}")
        # We continue, but Azure calls will likely fail if key is missing
        token_provider = None

    # 2. Load Config from standard path
    config_path = "/app/config/litellm-config.yaml"
    if not os.path.exists(config_path):
        print(f"[Wrapper] Config file not found at {config_path}")
        sys.exit(1)

    with open(config_path, 'r') as f:
        config_data = yaml.safe_load(f)
    
    print(f"[Wrapper] Loaded configuration from {config_path}")

    # 3. Inject Token Provider into Model List
    print("[Wrapper] Injecting Azure Token Provider into model configuration...")
    final_model_list = []
    managed_identity_count = 0
    
    raw_model_list = config_data.get("model_list", [])
    for model in raw_model_list:
        litellm_params = model.get("litellm_params", {})
        model_name = litellm_params.get("model", "unknown")
        
        # Check if this is an Azure model
        if "azure" in model_name:
            if token_provider:
                litellm_params["azure_ad_token_provider"] = token_provider
                managed_identity_count += 1
            
            # Remove api_key if present to force usage of token provider or MI
            if "api_key" in litellm_params:
                del litellm_params["api_key"]
                
        final_model_list.append(model)
    
    print(f"[Wrapper] Processed {len(final_model_list)} models. {managed_identity_count} using Managed Identity.")

    # 4. Update Global State
    # Set the global model_list for litellm
    litellm.model_list = final_model_list
    
    # Check Master Key
    if "general_settings" in config_data and "master_key" in config_data["general_settings"]:
        os.environ["LITELLM_MASTER_KEY"] = config_data["general_settings"]["master_key"]
        print("[Wrapper] LITELLM_MASTER_KEY set from config.")
    
    # 5. Re-initialize Router (CRITICAL FIX)
    # The proxy server app relies on the global 'llm_router' object.
    # By default, it loads from config file paths. We must overwrite it with our 
    # Python-object-injected router (containing the token_provider callable).
    print("[Wrapper] Re-constructing Global Router...")
    try:
        new_router = Router(model_list=final_model_list)
        
        # We need to update the router instance in the imported proxy_server module
        import litellm.proxy.proxy_server as proxy_server_module
        
        if hasattr(proxy_server_module, 'llm_router'):
            print("[Wrapper] Overwriting proxy_server.llm_router with authenticated router instance.")
            proxy_server_module.llm_router = new_router
        else:
            print("[Wrapper] WARNING: proxy_server.llm_router not found. This version of LiteLLM might differ.")

        # Update proxy_config if it exists (used for some settings)
        if hasattr(proxy_server_module, 'proxy_config') and proxy_server_module.proxy_config:
            print("[Wrapper] Updating proxy_config model list.")
            proxy_server_module.proxy_config.model_list = final_model_list
            
            # Apply general settings
            settings = config_data.get("litellm_settings", {})
            if settings:
                proxy_server_module.proxy_config.general_settings = settings
    except Exception as e:
        print(f"[Wrapper] Error during Router reconstruction: {e}")
        # Fallback: hope global litellm.model_list is enough
        
    # Check Master Key
    # Already handled above before router init for env var, but ensuring config consistency
    
    # 6. Start Server
    print("[Wrapper] Starting Uvicorn Server on port 4000...")
    # remove LITELLM_CONFIG_PATH env var to prevent double-loading interference
    if "LITELLM_CONFIG_PATH" in os.environ:
        del os.environ["LITELLM_CONFIG_PATH"]
        
    uvicorn.run(app, host="0.0.0.0", port=4000)

if __name__ == "__main__":
    main()
"""
    # Indent the script for YAML inclusion
    indented_script = "\n".join(["    " + line for line in run_proxy_script.splitlines()])
    
    # 5. Apply to K8s
    deployer.get_kubectl_credentials(rg_name, aks_name)
    
    # Using 'kubernetes' client or 'kubectl'
    # For manifest application, kubectl apply -f - is often robust.
    # We will construct manifests in memory string and apply.
    
    # Namespace
    manifests = []
    manifests.append(f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
""")
    
    # Store Secrets Persistently
    b64_master_key = base64.b64encode(master_key.encode()).decode()
    b64_openclaw_token = base64.b64encode(openclaw_token.encode()).decode()
    
    manifests.append(f"""
apiVersion: v1
kind: Secret
metadata:
  name: openclaw-secrets
  namespace: {namespace}
type: Opaque
data:
  master_key: {b64_master_key}
  openclaw_token: {b64_openclaw_token}
""")

    # LiteLLM ConfigMap
    manifests.append(f"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: {deploy_name}-llmproxy-config
  namespace: {namespace}
data:
  litellm-config.yaml: |
{indented_yaml}
  run_proxy.py: |
{indented_script}
""")

    # LiteLLM Deployment
    # Injects AZURE_CLIENT_ID for Managed Identity usage
    # Changes entrypoint to python script
    manifests.append(f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {deploy_name}-llmproxy
  namespace: {namespace}
  labels:
    app: {deploy_name}-llmproxy
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {deploy_name}-llmproxy
  template:
    metadata:
      labels:
        app: {deploy_name}-llmproxy
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
              value: "{mi_client_id}"
            - name: LITELLM_LOG
              value: "DEBUG"
            # Ensure python output is unbuffered
            - name: PYTHONUNBUFFERED
              value: "1"
          # Override entrypoint to run our wrapper script
          command: ["python3"]
          args: ["/app/config/run_proxy.py"]
          volumeMounts:
            - name: config-volume
              mountPath: /app/config
              readOnly: true
          resources:
            requests:
              memory: "512Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "1000m"
          readinessProbe:
            httpGet:
              path: /health/readiness
              port: 4000
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
          livenessProbe:
            httpGet:
              path: /health/liveliness
              port: 4000
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
      volumes:
        - name: config-volume
          configMap:
            name: {deploy_name}-llmproxy-config
""")

    # LiteLLM Service
    manifests.append(f"""
apiVersion: v1
kind: Service
metadata:
  name: {deploy_name}-llmproxy-svc
  namespace: {namespace}
spec:
  selector:
    app: {deploy_name}-llmproxy
  ports:
    - protocol: TCP
      port: 4000
      targetPort: 4000
  type: LoadBalancer
""")

    # OpenClaw ConfigMap
    # Logic from openclaw-configmap.yaml but rendered
    openclaw_config = {
      "gateway": {
        "mode": "remote",
        "port": 18789,
        "auth": {
            "token": openclaw_token
        }
      },
      "models": {
        "mode": "merge",
        "providers": {
          "litellm": {
            "api": "openai-completions",
            "baseUrl": f"http://{deploy_name}-llmproxy-svc.{namespace}.svc.cluster.local:4000",
            "apiKey": master_key,
            "models": [
              {
                "id": model_name,
                "name": f"Azure OpenAI {model_name}",
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

    openclaw_json_str = json.dumps(openclaw_config, indent=4)
    indented_openclaw_json = "\n".join(["    " + line for line in openclaw_json_str.splitlines()])

    manifests.append(f"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: {deploy_name}-openclaw-config
  namespace: {namespace}
data:
  openclaw-config.json: |
{indented_openclaw_json}
""")
    
    # Prepare models.json content for init container
    agent_models_json = {
      "providers": {
        "litellm": {
          "api": "openai-completions",
          "baseUrl": f"http://{deploy_name}-llmproxy-svc.{namespace}.svc.cluster.local:4000",
          "apiKey": master_key, # Using master key as API Key for LiteLLM
          "models": [
            {
              "id": model_name,
              "name": f"Azure OpenAI {model_name}",
              "reasoning": False,
              "input": ["text", "image"],
              "contextWindow": 128000,
              "maxTokens": 16384
            }
          ]
        }
      },
      "primary": f"litellm/{model_name}"
    }
    # Escape quotes for shell command (single-quoted echo)
    # AND escape for YAML double-quoted string
    json_str = json.dumps(agent_models_json)
    shell_safe = json_str.replace("'", "'\\''")
    # For YAML double-quoted string: escape backslash and double-quote
    agent_models_content = shell_safe.replace('\\', '\\\\').replace('"', '\\"')

    # OpenClaw Deployment
    manifests.append(f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {deploy_name}
  namespace: {namespace}
  labels:
    app: {deploy_name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {deploy_name}
  template:
    metadata:
      labels:
        app: {deploy_name}
    spec:
      initContainers:
        - name: init-agent-config
          image: busybox
          command: ["/bin/sh", "-c"]
          args:
            - "mkdir -p /home/node/.openclaw/agents/main/agent && echo '{agent_models_content}' > /home/node/.openclaw/agents/main/agent/models.json && chmod -R 777 /home/node/.openclaw"
          volumeMounts:
            - name: openclaw-agents
              mountPath: /home/node/.openclaw/agents
      containers:
        - name: sidecar
          image: alpine/socat
          args: ["TCP-LISTEN:18790,fork,bind=0.0.0.0", "TCP:127.0.0.1:18789"]
          ports:
            - containerPort: 18790
        - name: openclaw
          image: alpine/openclaw:latest
          ports:
            - containerPort: 18789
          env:
            - name: OPENCLAW_GATEWAY_PORT
              value: "18789"
            - name: OPENCLAW_CONFIG_PATH
              value: "/etc/openclaw/openclaw.json"
            - name: OPENCLAW_STATE_DIR
              value: "/home/node/.openclaw"
            - name: NODE_OPTIONS
              value: "--max-old-space-size=4096"
          volumeMounts:
            - name: config-volume
              mountPath: /etc/openclaw/openclaw.json
              subPath: openclaw-config.json
              readOnly: true
            - name: openclaw-data
              mountPath: /home/node/.openclaw
            - name: openclaw-agents
              mountPath: /home/node/.openclaw/agents
          resources:
            requests:
              memory: "256Mi"
              cpu: "100m"
            limits:
              memory: "2Gi"
      volumes:
        - name: config-volume
          configMap:
            name: {deploy_name}-openclaw-config
        - name: openclaw-data
          emptyDir: {{}}
        - name: openclaw-agents
          emptyDir: {{}}
""")

    # OpenClaw Service
    manifests.append(f"""
apiVersion: v1
kind: Service
metadata:
  name: {deploy_name}-svc
  namespace: {namespace}
spec:
  selector:
    app: {deploy_name}
  ports:
    - protocol: TCP
      port: 80
      targetPort: 18790
  type: LoadBalancer
""")

    log_info("\n=== Step 5/6: Apply Kubernetes Manifests ===")
    log_info("Applying Kubernetes Manifests...")
    full_manifest = "\n---\n".join(manifests)
    
    # Debug: Write manifest to file
    with open("generated_manifest.yaml", "w") as f:
        f.write(full_manifest)
    log_info("Written generated_manifest.yaml for debugging.")
    
    # subprocess.run with input
    try:
        subprocess.run(["kubectl", "apply", "-f", "-"], input=full_manifest.encode(), check=True)
        log_success("Kubernetes resources applied.")
    except subprocess.CalledProcessError as e:
        log_error(f"Failed to apply K8s manifests: {e}")

    # Wait for IPs
    log_info("\n=== Step 6/6: Wait for LB IP Allocation ===")
    log_info("Waiting for IP allocation (max 120s)...")
    info = {}
    for _ in range(12):  # Poll every 10s for 120s
        time.sleep(10)
        try:
            result = subprocess.run(
                ["kubectl", "get", "svc", "-n", namespace, "-o", "json"],
                capture_output=True, check=True
            )
            data = json.loads(result.stdout)
            
            pending = False
            for item in data.get("items", []):
                name = item["metadata"]["name"]
                lb = item["status"].get("loadBalancer", {}).get("ingress", [])
                ip = lb[0].get("ip") if lb else "Pending"
                info[name] = ip
                if ip == "Pending":
                    pending = True
            
            if not pending:
                log_success("All IPs assigned.")
                break
        except Exception:
            pass
    else:
        log_warning("Timeout waiting for LoadBalancer IPs. Check 'kubectl get svc -n openclaw-ns'.")

    # Wait for Pods to be Running before suggesting port-forward
    log_info("Waiting for Pods to start (This will take 3 minutes)...")
    wait_time = 180  # 3 minutes
    for remaining in range(wait_time, 0, -10):
        print(f"Waiting for pods to stabilize: {remaining}s remaining...", end="\r")
        time.sleep(10)
    print("\nWaiting complete. Checking pod status...")

    try:
        subprocess.run(
            ["kubectl", "wait", "--for=condition=ready", "pod", "-l", f"app={deploy_name}", "-n", namespace, "--timeout=30s"],
            check=False  # Don't crash script if timeout
        )
    except Exception:
        log_warning("Pods rely readiness check failed or timed out.")

    # Retrieve IPs (final check)
    try:
        print("\n" + "="*50)
        print("DEPLOYMENT COMPLETE")
        print("="*50)
        print(f"LiteLLM Dashboard (via Proxy): http://{info.get(f'{deploy_name}-llmproxy-svc', 'Pending')}:4000")
        print(f"OpenClaw Management UI:       http://{info.get(f'{deploy_name}-svc', 'Pending')}")
        print("-" * 50)
        print("IMPORTANT: Accessing OpenClaw Control UI requires port-forwarding due to Origin security policies.")
        print(f"Run this command in a separate terminal: kubectl port-forward svc/{deploy_name}-svc 18789:80 -n {namespace}")
        print(f"Then access: http://localhost:18789/?token={openclaw_token}")
        print("-" * 50)
        print(f"Master Key:     {master_key}")
        print(f"OpenClaw Token: {openclaw_token}")
        print("-" * 50)
        print("NOTE: Using Managed Identity for Azure OpenAI Access.")
        print("Please verify the Role Assignments if connection fails.")
        print(f"Managed Identity: {mi_name}")
        print(f"Principal ID:     {mi_principal_id}")
        
    except Exception as e:
        log_warning(f"Could not retrieve service status: {e}")

if __name__ == "__main__":
    main()
