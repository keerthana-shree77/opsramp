"""Central configuration.

Every tunable value is read from the environment.  Nothing secret is ever
hard-coded here, and nothing secret is ever written to the log (see
``RedactingFilter``).
"""
from __future__ import annotations

import logging
import logging.config
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class Config:
    # ------------------------------------------------------------------ Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", True)
    PERMANENT_SESSION_LIFETIME = _int("SESSION_LIFETIME_SECONDS", 8 * 3600)
    MAX_CONTENT_LENGTH = _int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)  # 10 MiB
    JSON_SORT_KEYS = False

    # ------------------------------------------------------- Application login
    APP_USERNAME = os.getenv("APP_USERNAME", "")
    APP_PASSWORD = os.getenv("APP_PASSWORD", "")

    # ------------------------------------------------------------- OpsRamp API
    OPSRAMP_BASE_URL = os.getenv("OPSRAMP_BASE_URL", "").rstrip("/")
    OPSRAMP_PARTNER_TENANT_ID = os.getenv("OPSRAMP_PARTNER_TENANT_ID", "")
    OPSRAMP_OAUTH_CLIENT_ID = os.getenv("OPSRAMP_OAUTH_CLIENT_ID", "")
    OPSRAMP_OAUTH_CLIENT_SECRET = os.getenv("OPSRAMP_OAUTH_CLIENT_SECRET", "")
    OPSRAMP_VERIFY_TLS = _bool("OPSRAMP_VERIFY_TLS", True)

    # Endpoint templates.  Override these if your OpsRamp instance exposes a
    # different path; nothing else in the code base hard-codes a URL.
    OPSRAMP_TOKEN_PATH = os.getenv("OPSRAMP_TOKEN_PATH", "/auth/oauth/token")
    # Listing clients lives on /clients/search; a bare /clients is the create
    # endpoint and answers GET with HTTP 405.  If the first path is rejected
    # with 404/405 the fallbacks are tried in order and the winner is logged.
    OPSRAMP_CLIENTS_PATH = os.getenv(
        "OPSRAMP_CLIENTS_PATH", "/api/v2/tenants/{partner_id}/clients/minimal"
    )
    OPSRAMP_CLIENTS_FALLBACK_PATHS = [
        p.strip()
        for p in os.getenv(
            "OPSRAMP_CLIENTS_FALLBACK_PATHS",
            "/api/v2/tenants/{partner_id}/clients/search,"
            "/api/v2/tenants/{partner_id}/clients",
        ).split(",")
        if p.strip()
    ]
    OPSRAMP_RESOURCE_SEARCH_PATH = os.getenv(
        "OPSRAMP_RESOURCE_SEARCH_PATH", "/api/v2/tenants/{tenant_id}/resources/search"
    )
    OPSRAMP_RESOURCE_DETAIL_PATH = os.getenv(
        "OPSRAMP_RESOURCE_DETAIL_PATH",
        "/api/v2/tenants/{tenant_id}/resources/{resource_id}",
    )
    # Firmware does not live on the resource document itself - it is spread
    # across these sub-resources.  Without them almost every component reports
    # VERSION NOT DETECTED.  SPS firmware in particular arrives as a tag.
    # Each costs one API call per asset, so trim the list if a tenant is huge.
    _DEFAULT_EXTRA_PATHS = ",".join(
        "/api/v2/tenants/{tenant_id}/resources/{resource_id}/" + name
        for name in (
            "appAttributes",
            "customAttributes",
            "nativeAttributes",
            "components",
            "tags",
            "assets",
        )
    )
    OPSRAMP_RESOURCE_EXTRA_PATHS = [
        p.strip()
        for p in os.getenv("OPSRAMP_RESOURCE_EXTRA_PATHS", _DEFAULT_EXTRA_PATHS).split(",")
        if p.strip()
    ]

    # Only tenants whose name matches this pattern are offered in the dropdown.
    # Set to an empty string to list every client the partner can see.
    TENANT_NAME_FILTER = os.getenv("TENANT_NAME_FILTER", "")

    # ------------------------------------------------------------ HTTP tuning
    HTTP_TIMEOUT_CONNECT = _float("HTTP_TIMEOUT_CONNECT", 10.0)
    HTTP_TIMEOUT_READ = _float("HTTP_TIMEOUT_READ", 60.0)
    HTTP_RETRY_TOTAL = _int("HTTP_RETRY_TOTAL", 4)
    HTTP_RETRY_BACKOFF = _float("HTTP_RETRY_BACKOFF", 1.5)
    HTTP_POOL_SIZE = _int("HTTP_POOL_SIZE", 32)
    # OpsRamp rejects oversized pages with HTTP 400 on some endpoints; 100 is
    # the documented default and is accepted everywhere.
    PAGE_SIZE = _int("OPSRAMP_PAGE_SIZE", 100)
    MAX_PAGES = _int("OPSRAMP_MAX_PAGES", 200)

    # ----------------------------------------------------------- Concurrency
    MAX_WORKERS = max(1, min(_int("MAX_WORKERS", 12), 64))
    DETAIL_FETCH_ENABLED = _bool("DETAIL_FETCH_ENABLED", True)
    # Resource summaries often omit the model, so an asset whose platform cannot
    # be identified from the summary alone is still deep-fetched by default.
    # Turning this off makes a run much cheaper but can silently miss assets.
    DEEP_FETCH_UNMATCHED = _bool("DEEP_FETCH_UNMATCHED", True)
    MAX_UNRESOLVED_DEEP_FETCH = _int("MAX_UNRESOLVED_DEEP_FETCH", 2000)

    # ------------------------------------------------------------- Comparison
    # "at_least": installed >= target counts as UPDATED (newer is fine).
    # "exact":    only an exact normalized match counts as UPDATED.
    VERSION_COMPARE_POLICY = os.getenv("VERSION_COMPARE_POLICY", "at_least").lower()

    # ------------------------------------------------------------- Overview
    # The compliance matrix on the home page is built by sweeping every tenant
    # against a recipe held on the server, so it is populated without anyone
    # choosing a tenant or uploading a file. Point this at the approved recipe.
    DEFAULT_RECIPE_PATH = os.getenv("DEFAULT_RECIPE_PATH", "")
    # Categories the sweep covers. "all" means every category.
    OVERVIEW_CATEGORIES = [
        c.strip().lower()
        for c in os.getenv("OVERVIEW_CATEGORIES", "all").split(",")
        if c.strip()
    ]
    # Minutes between automatic refreshes; 0 disables them and leaves the
    # matrix to the Refresh button.
    #
    # A sweep reads every asset of every account, so on a large estate it can
    # take longer than this interval. That is handled rather than prevented: a
    # tick that arrives while a sweep is still running is dropped, so the
    # sweeps run back to back instead of piling up. Raise this if the load on
    # OpsRamp matters more than the freshness of the grid.
    OVERVIEW_REFRESH_MINUTES = _int("OVERVIEW_REFRESH_MINUTES", 10)
    # Refresh once shortly after start-up, so the page is populated on arrival.
    # The cache means the page is not empty in the meantime.
    OVERVIEW_REFRESH_ON_START = _bool("OVERVIEW_REFRESH_ON_START", True)
    # Cap the sweep. 0 means no limit.
    OVERVIEW_MAX_TENANTS = _int("OVERVIEW_MAX_TENANTS", 0)
    # How many accounts the sweep reads at once. Reading is mostly waiting on
    # OpsRamp, so this is where the time goes on a large estate - but each
    # account already fans out over its own assets, so the requests in flight
    # are roughly this times MAX_WORKERS. Raise it if OpsRamp keeps up; lower
    # it to 1 for the old one-at-a-time behaviour.
    OVERVIEW_TENANT_WORKERS = max(1, min(_int("OVERVIEW_TENANT_WORKERS", 4), 16))

    # ------------------------------------------------------------------- Cache
    # The matrix is kept here between refreshes, so signing in renders the last
    # known state immediately instead of waiting on a sweep. It holds no
    # credentials - only account names and firmware versions - and deleting it
    # costs nothing but the next refresh.
    CACHE_DB_PATH = Path(
        os.getenv("CACHE_DB_PATH", str(BASE_DIR / "var" / "cache.db"))
    )

    # ----------------------------------------------------------------- Uploads
    UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "var" / "uploads")))
    ALLOWED_RECIPE_EXTENSIONS = {".csv", ".xlsx", ".xls", ".json", ".pdf"}

    # -------------------------------------------------------------- Job store
    JOB_RETENTION_SECONDS = _int("JOB_RETENTION_SECONDS", 6 * 3600)
    # A sweep creates one job per account *per recipe*, and every cell of the
    # matrix links to one of them, so this has to comfortably exceed
    # accounts x recipes or the oldest links start to 404.
    MAX_JOBS = _int("MAX_JOBS", 1000)

    # ------------------------------------------------------------------ Proxy
    # How many reverse proxies stand in front. Zero - the default - means the
    # app believes what the connection tells it, which is right when nothing
    # is in front of it.
    #
    # Behind a proxy that terminates TLS, the connection says "http" and the
    # real scheme is in X-Forwarded-Proto. Setting this to the number of
    # proxies makes the app read that header instead. It is a count rather
    # than a switch because the header is a list that anything upstream may
    # append to: trusting one hop when two are in front reads the client's
    # own forged value, and trusting a hop that is not there lets any caller
    # claim to be on HTTPS. Count them, do not guess.
    TRUSTED_PROXY_HOPS = max(0, _int("TRUSTED_PROXY_HOPS", 0))

    # ------------------------------------------------------------------ Misc
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

    @classmethod
    def missing_required(cls) -> list[str]:
        """Return the names of required settings that are absent."""
        required = {
            "SECRET_KEY": cls.SECRET_KEY,
            "APP_USERNAME": cls.APP_USERNAME,
            "APP_PASSWORD": cls.APP_PASSWORD,
            "OPSRAMP_BASE_URL": cls.OPSRAMP_BASE_URL,
            "OPSRAMP_PARTNER_TENANT_ID": cls.OPSRAMP_PARTNER_TENANT_ID,
            "OPSRAMP_OAUTH_CLIENT_ID": cls.OPSRAMP_OAUTH_CLIENT_ID,
            "OPSRAMP_OAUTH_CLIENT_SECRET": cls.OPSRAMP_OAUTH_CLIENT_SECRET,
        }
        return sorted(name for name, value in required.items() if not value)


# --------------------------------------------------------------------- logging

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(client_secret[\"'=:\s]+)[^\s\"'&,}]+"),
    re.compile(r"(?i)(access_token[\"'=:\s]+)[^\s\"'&,}]+"),
    re.compile(r"(?i)(password[\"'=:\s]+)[^\s\"'&,}]+"),
    re.compile(r"(?i)(authorization[\"'=:\s]+)[^\s\"',}]+"),
]


class RedactingFilter(logging.Filter):
    """Strip anything that looks like a credential out of every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        redacted = message
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(r"\1***REDACTED***", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str | None = None) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"redact": {"()": RedactingFilter}},
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "filters": ["redact"],
                }
            },
            "root": {"level": level or Config.LOG_LEVEL, "handlers": ["console"]},
            "loggers": {
                "urllib3": {"level": "WARNING"},
                "werkzeug": {"level": "WARNING"},
            },
        }
    )
