# The portal, as one process.
#
# Read the worker count before changing anything else: jobs, their progress
# and the compliance matrix all live in this process's memory, and the sweep
# runs on a background thread started when the module is imported. A second
# worker is a second sweep against the same OpsRamp rate limit, handing back
# whichever half of the results its own memory happens to hold. Scale this
# with threads, never with processes - and never with replicas.

FROM python:3.13-slim

# Unbuffered so the log reaches the collector as it happens rather than when
# a buffer fills; no .pyc because the filesystem is read-only below.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so editing the application does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The image holds no state. The cache database and the uploaded recipes are
# the only things written, and they belong on a volume that outlives it -
# losing the cache costs a full re-sweep of every account.
ENV CACHE_DB_PATH=/data/cache.db \
    UPLOAD_DIR=/data/uploads

# Nothing here needs root, so nothing here gets it. Only /data is writable.
RUN useradd --system --uid 10001 --home-dir /app --no-create-home portal \
 && mkdir -p /data \
 && chown -R portal:portal /data
USER portal

EXPOSE 8000

# /login answers before anybody has signed in, so it says the process is
# serving without the probe holding a session of its own.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4).status == 200 else 1)"

# --timeout is generous because a comparison request reads a whole account.
# --graceful-timeout gives a sweep in flight a moment to stop cleanly.
CMD ["gunicorn", \
     "--workers", "1", \
     "--threads", "8", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
