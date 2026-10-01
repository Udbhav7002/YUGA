#!/usr/bin/env bash
set -e
echo "======================================"
echo "👻 CodeGhost E2E Test Suite"
echo "======================================"

echo "→ Warming up APIs..."
curl -s http://localhost:8000/health >/dev/null

echo "→ Triggering Crash (Division by Zero)..."
curl -s -X POST http://localhost:8000/calculate \
  -H "Content-Type: application/json" \
  -d '{"a":10,"b":0,"operation":"divide"}' | head -c 200
echo -e "\n"

echo "→ Waiting for Pipeline (watching status)..."
for i in $(seq 1 60); do
  s=$(curl -s http://localhost:9000/status/last 2>/dev/null || true)
  
  if echo "$s" | grep -q '"stage":"done"'; then
    echo -e "\n🎉 Pipeline Complete! Result:"
    echo "$s" | python3 -m json.tool
    exit 0
  elif echo "$s" | grep -q '"stage":"error"'; then
    echo -e "\n❌ Pipeline Failed! Result:"
    echo "$s" | python3 -m json.tool
    exit 1
  fi
  
  echo -n "."
  sleep 1
done

echo -e "\n❌ Timed out waiting for pipeline"
exit 1
