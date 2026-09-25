#!/usr/bin/env bash
# First run on the VM. Safe to run again - it changes nothing it does not own.
set -euo pipefail

cd "$(dirname "$0")/.."
echo "Working in: $(pwd)"
echo

# --------------------------------------------------------------- what we need
missing=0
if ! command -v docker >/dev/null 2>&1; then
  echo "MISSING: docker is not installed."
  echo "  On RHEL/CentOS:  sudo dnf install -y docker docker-compose-plugin"
  echo "  On Ubuntu:       sudo apt install -y docker.io docker-compose-v2"
  missing=1
fi

if command -v docker >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
  echo "MISSING: the docker compose plugin."
  echo "  Install 'docker-compose-plugin' (or 'docker-compose-v2' on Ubuntu)."
  missing=1
fi

if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
  echo "PROBLEM: docker is installed but this account cannot use it."
  echo "  Either the service is stopped:  sudo systemctl enable --now docker"
  echo "  Or this account is not allowed:  sudo usermod -aG docker $USER"
  echo "  (log out and back in after that second one)"
  missing=1
fi
[ "$missing" -eq 0 ] || { echo; echo "Fix the above, then run this again."; exit 1; }

# ------------------------------------------------------------------ the .env
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from the example - and it is not filled in yet."
  echo
  echo "Edit it now:   nano .env"
  echo
  echo "These seven have to have values, or the portal starts but cannot"
  echo "sign anyone in or reach OpsRamp:"
  echo "    SECRET_KEY                    (any long random string)"
  echo "    APP_USERNAME                  (what you and colleagues sign in with)"
  echo "    APP_PASSWORD"
  echo "    OPSRAMP_BASE_URL"
  echo "    OPSRAMP_PARTNER_TENANT_ID"
  echo "    OPSRAMP_OAUTH_CLIENT_ID"
  echo "    OPSRAMP_OAUTH_CLIENT_SECRET"
  echo
  echo "A SECRET_KEY you can paste:"
  echo "    $(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo
  echo "Then run this script again."
  exit 0
fi
chmod 600 .env

# The seven the application refuses to work without.
blank=""
for name in SECRET_KEY APP_USERNAME APP_PASSWORD OPSRAMP_BASE_URL \
            OPSRAMP_PARTNER_TENANT_ID OPSRAMP_OAUTH_CLIENT_ID \
            OPSRAMP_OAUTH_CLIENT_SECRET; do
  # Read the value without printing it.
  value="$(grep -E "^${name}=" .env | head -1 | cut -d= -f2- || true)"
  [ -n "$value" ] || blank="$blank $name"
done
if [ -n "$blank" ]; then
  echo "These are still empty in .env:$blank"
  echo "Fill them in with 'nano .env', then run this again."
  exit 1
fi

# ------------------------------------------------------------------- start it
echo "Building and starting. The first build takes a few minutes."
echo
docker compose up -d --build

echo
echo "Waiting for it to answer..."
for attempt in $(seq 1 30); do
  if curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:8000/login 2>/dev/null; then
    echo
    echo "It is running."
    echo
    echo "  On this VM:      http://127.0.0.1:8000"
    echo "  Watch the log:   docker compose logs -f portal"
    echo "  Stop it:         docker compose down"
    echo "  Update it:       ./deploy/update.sh"
    echo
    echo "Colleagues cannot reach it yet - only this machine can. Putting a"
    echo "name and HTTPS in front of it is the next step; see deploy/README.md."
    exit 0
  fi
  sleep 2
done

echo
echo "It did not answer within a minute. What it says about itself:"
docker compose logs --tail 40 portal
exit 1
