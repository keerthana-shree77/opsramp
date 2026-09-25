# Request: somewhere to run the Firmware Compliance Portal

*Copy the section below into a ticket or an email. Fill in the three blanks.*

---

**What it is.** An internal web page that shows which firmware our managed
accounts are running, compared against the approved recipe, per account and
per component. It reads from OpsRamp; it changes nothing there.

**What I need.** A small Linux VM that stays on, so the team can use the page
without it running on my laptop. It is packaged as a container, so there is
nothing to install by hand.

| | |
|---|---|
| Size | 2 vCPU, 4 GB RAM, 20 GB disk |
| Software | Docker (or Podman) and Docker Compose |
| Runs as | one container, one replica - see the note below |
| Listens on | 127.0.0.1:8000, for a reverse proxy to publish |
| Outbound | HTTPS to our OpsRamp API endpoint |
| Inbound | HTTPS (443) from the HPE internal network only - not the internet |
| Storage | one volume, for its cache. Nothing on it is irreplaceable. |

**What I need from you specifically:**

1. **An internal DNS name** for it, something like
   `firmware.____________.hpe.com`.
2. **A TLS certificate** for that name, and a reverse proxy (nginx, or
   whatever is standard here) in front of the container to terminate it. The
   page signs people in, so it must not be reachable over plain HTTP.
3. **Restricted to the internal network**, and to the group
   `____________` if that is how access is normally limited.

**Deployment.** The source and the container definition live at
`https://github.hpe.com/____________`. Deploying is:

```bash
git clone <that repository>
cd opsramp_firmware_tool
cp .env.example .env        # I will supply the filled-in .env separately
docker compose up -d --build
```

The `.env` holds the OpsRamp credentials. **It is deliberately not in the
repository** and must not be put there - I will hand it over through whatever
secret store we use.

**One important constraint.** It must run as **a single instance**. It keeps
its working state in memory and polls OpsRamp on a timer, so a second copy
would poll twice against the same rate limit and the two would disagree about
what they had found. Please do not scale it horizontally or put it behind a
load balancer with more than one backend. If it needs to be faster, give it a
bigger machine.

**Secure connection.** Everything above assumes HTTPS end to end. The app sets
`TRUSTED_PROXY_HOPS=1` so it reads the real client address and scheme from the
proxy's `X-Forwarded-*` headers - if there is more than one proxy hop, tell me
the number and I will change it.
