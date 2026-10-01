#!/bin/bash
echo "🚀 CodeGhost Ultimate Auto-Demo 🚀"
echo "====================================="

# Clean old processes just in case
pkill -f uvicorn

# Setup env
source ../.venv/bin/activate
set -a && source .env && set +a

# Start Orchestrator in background
echo "1. Starting Orchestrator on Port 8002..."
uvicorn orchestrator.main:app --port 8002 &
ORCH_PID=$!
sleep 3

# Start Target App in background
echo "2. Starting Target App on Port 8001..."
uvicorn target_app.main:app --port 8001 &
TARGET_PID=$!
sleep 3

echo "3. 💥 Triggering the Crash! 💥"
curl -s -X POST "http://127.0.0.1:8001/calculate" \
     -H "Content-Type: application/json" \
     -d '{"a": 10, "b": 0, "operation": "divide"}' | grep -o 'Internal server error'

echo ""
echo "✅ Crash sent! The Orchestrator is now working on the fix."
echo "👉 OPEN YOUR BROWSER NOW: http://127.0.0.1:8002/dash/"
echo ""
echo "Press Ctrl+C when you want to stop both servers."

# Wait for user to stop it
wait
