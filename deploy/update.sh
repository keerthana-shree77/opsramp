#!/usr/bin/env bash
# Pull the latest code and restart. This is the whole of "deploying a change".
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Currently running: $(git rev-parse --short HEAD) - $(git log -1 --format=%s)"
echo
git pull --ff-only
echo
echo "Now at:            $(git rev-parse --short HEAD) - $(git log -1 --format=%s)"
echo

docker compose up -d --build

echo
echo "Waiting for it to answer..."
for attempt in $(seq 1 30); do
  if curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:8000/login 2>/dev/null; then
    echo "Updated and running."
    exit 0
  fi
  sleep 2
done

echo "It did not come back. What it says about itself:"
docker compose logs --tail 40 portal
echo
echo "To go back to the version that worked:"
echo "    git reset --hard HEAD~1 && docker compose up -d --build"
exit 1
