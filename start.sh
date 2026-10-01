#!/bin/bash
# CodeGhost - Quick Start & Cleanup Script

echo "🧹 Cleaning up old python cache..."
find . -type d -name "__pycache__" -exec rm -rf {} +
find . -type d -name ".pytest_cache" -exec rm -rf {} +

echo "🚀 Starting CodeGhost Orchestrator..."
# Ensure the virtual environment is activated
source ../.venv/bin/activate

# Load the environment variables
set -a
source .env
set +a

# Start Uvicorn
echo "✅ Environment loaded. Orchestrator running on http://127.0.0.1:8000"
uvicorn orchestrator.main:app --reload --port 8000
