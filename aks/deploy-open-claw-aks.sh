#!/bin/bash

# ===========================================================================
# deploy-open-claw-aks.sh
# Wrapper script to deploy OpenClaw & LiteLLM on AKS using Python Logic
# ===========================================================================

set -e

# Script Directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Arguments
CONFIG_FILE="${1:-azure-openai.json}"

echo "Starting deployment wrapper..."
echo "Using Configuration File: $CONFIG_FILE"

# Python Environment Setup
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is not installed."
    exit 1
fi

# Check if we are already in a virtual environment or Conda environment
if [[ -n "$VIRTUAL_ENV" ]] || [[ -n "$CONDA_PREFIX" ]]; then
    echo "Using active environment (Venv: $VIRTUAL_ENV, Conda: $CONDA_PREFIX)"
else
    if [[ ! -d "venv" ]]; then
        echo "Creating Python virtual environment..."
        python3 -m venv venv
    fi

    echo "Activating virtual environment..."
    source venv/bin/activate
fi

echo "Installing requirements..."
pip install -r requirements.txt

# Run the Python deployment script
echo "Running deployment logic..."

# Pass arguments only if they override defaults
python3 deploy_openclaw.py --config "$CONFIG_FILE"

echo "Deployment attempt finished."
