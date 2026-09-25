"""HTTP client for the OpsRamp REST API.

Responsibilities:
  * one pooled ``requests.Session`` shared by every worker thread
  * automatic retry with exponential backoff for transient failures
  * explicit timeouts on every call
  * bearer-token injection and a single transparent re-auth on HTTP 401
  * status-code to exception mapping with operator-friendly messages
  * OpsRamp pagination

No URL is hard-coded: every path comes from ``config.Config`` so that an
instance with a different API surface can be adapted without code changes.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .auth import OpsRampAuthError, TokenManager

logger = logging.getLogger(__name__)


class OpsRampError(RuntimeError):
    """Base class for OpsRamp API failures."""

    user_message = "The OpsRamp API request failed."

    def __init__(
        self,
        message: str | None = None,
        status: int | None = None,
        detail: str = "",
    ) -> None:
        text = message or self.user_message
        if detail:
            text = f"{text} OpsRamp said: {detail}"
        super().__init__(text)
        self.status = status
        self.detail = detail
        self.user_message = text


class OpsRampNotFound(OpsRampError):
    user_message = "The requested OpsRamp resource was not found."


class OpsRampRateLimited(OpsRampError):
    user_message = (
        "OpsRamp API returned HTTP 429. The request rate has been limited. "
        "Please retry after a short interval, or lower MAX_WORKERS."
    )


class OpsRampServerError(OpsRampError):
    user_message = "OpsRamp returned a server error. Please retry shortly."


class OpsRampForbidden(OpsRampError):
    user_message = (
        "OpsRamp denied access to this resource. Verify that the integration "
        "user has permission for the selected tenant."
    )


_RETRY_STATUS = (429, 500, 502, 503, 504)


class _RateLimitGate:
    """Holds every request back after OpsRamp says the rate is too high.

    The limit is on the account, not on the connection, so one worker meeting
    a 429 means the other forty-seven are about to. Left to themselves they
    each burn their own retry budget inside the same few seconds and every one
    of them runs out - which is how a transient limit turned into whole
    categories of "no firmware version reported" in the report.

    So the pause is shared by every client in the process. The first 429 sets
    it; everyone else waits it out rather than adding to the pile. It respects
    Retry-After where OpsRamp sends one, and otherwise doubles up to a ceiling
    so a sustained limit is not met with a fixed drumbeat.
    """

    FLOOR = 2.0
    CEILING = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resume_at = 0.0
        self._last_pause = 0.0

    def wait(self) -> None:
        """Block until the pause set by the last 429 has passed."""
        while True:
            with self._lock:
                remaining = self._resume_at - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5.0))

    def hit(self, retry_after: object = None) -> float:
        """Record a 429.  Returns how long everything will now hold off."""
        delay = 0.0
        try:
            delay = float(retry_after) if retry_after else 0.0
        except (TypeError, ValueError):
            delay = 0.0
        with self._lock:
            if delay <= 0:
                delay = min(max(self._last_pause * 2, self.FLOOR), self.CEILING)
            delay = min(delay, self.CEILING)
            self._last_pause = delay
            self._resume_at = max(self._resume_at, time.monotonic() + delay)
        logger.warning(
            "OpsRamp rate-limited a request; holding every request for %.0fs", delay
        )
        return delay

    def clear(self) -> None:
        """A request got through, so the next pause starts small again."""
        with self._lock:
            if self._last_pause and time.monotonic() >= self._resume_at:
                self._last_pause = 0.0


# Shared by every client in the process: the limit belongs to the account.
RATE_LIMIT = _RateLimitGate()


class OpsRampClient:
    """Thin, well-behaved wrapper around the OpsRamp REST API."""

    def __init__(self, config) -> None:
        self.config = config
        self.base_url = config.OPSRAMP_BASE_URL.rstrip("/")
        if not self.base_url:
            raise OpsRampError("OPSRAMP_BASE_URL is not configured.")

        self.timeout = (config.HTTP_TIMEOUT_CONNECT, config.HTTP_TIMEOUT_READ)
        self.verify = config.OPSRAMP_VERIFY_TLS

        retry = Retry(
            total=config.HTTP_RETRY_TOTAL,
            connect=config.HTTP_RETRY_TOTAL,
            read=config.HTTP_RETRY_TOTAL,
            status=config.HTTP_RETRY_TOTAL,
            backoff_factor=config.HTTP_RETRY_BACKOFF,
            status_forcelist=_RETRY_STATUS,
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=config.HTTP_POOL_SIZE,
            pool_maxsize=config.HTTP_POOL_SIZE,
        )
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": "opsramp-firmware-portal/1.0"}
        )

        self.tokens = TokenManager(
            session=self.session,
            base_url=self.base_url,
            token_path=config.OPSRAMP_TOKEN_PATH,
            client_id=config.OPSRAMP_OAUTH_CLIENT_ID,
            client_secret=config.OPSRAMP_OAUTH_CLIENT_SECRET,
            timeout=self.timeout,
            verify=self.verify,
        )

    # ------------------------------------------------------------------ public
    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def verify_connectivity(self) -> None:
        """Raise if authentication is impossible.  Used by the home page."""
        try:
            self.tokens.get()
        except OpsRampAuthError as exc:
            raise OpsRampError(
                "Unable to authenticate with OpsRamp. "
                "Please verify the configured OpsRamp credentials."
            ) from exc

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        allow_missing: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        # Hold off if another worker has just been rate-limited.
        RATE_LIMIT.wait()
        response = self._send(method, url, params, json_body, retry_auth=True)

        if response.status_code == 429:
            # urllib3 has already retried inside the same crowded window, and
            # every other worker was retrying into it too. Pausing all of them
            # and trying once more from a quiet line is what actually gets the
            # value - and a PDU's firmware lives only on the detail endpoint,
            # so losing the call means the report says the version could not
            # be read when nobody ever managed to ask.
            RATE_LIMIT.hit(response.headers.get("Retry-After"))
            RATE_LIMIT.wait()
            response = self._send(method, url, params, json_body, retry_auth=True)
        elif response.status_code < 400:
            RATE_LIMIT.clear()

        status = response.status_code
        if status >= 400:
            # OpsRamp explains most 4xx failures in the body ("Invalid page
            # size", "queryString is required"). Capturing it is the difference
            # between a diagnosable failure and a guessing game.
            detail = error_detail(response)
            if detail:
                logger.error(
                    "OpsRamp returned HTTP %s for %s: %s", status, _safe(path), detail
                )
            else:
                logger.error("OpsRamp returned HTTP %s for %s", status, _safe(path))
        else:
            detail = ""

        if status == 404:
            if allow_missing:
                logger.debug("OpsRamp returned 404 for %s (tolerated)", _safe(path))
                return None
            raise OpsRampNotFound(status=404, detail=detail)
        if status == 403:
            raise OpsRampForbidden(status=403, detail=detail)
        if status == 401:
            raise OpsRampError(
                "Unable to authenticate with OpsRamp. "
                "Please verify the configured OpsRamp credentials.",
                status=401,
                detail=detail,
            )
        if status == 429:
            raise OpsRampRateLimited(status=429, detail=detail)
        if status >= 500:
            raise OpsRampServerError(
                f"OpsRamp returned HTTP {status}. Please retry shortly.",
                status=status,
                detail=detail,
            )
        if status >= 400:
            raise OpsRampError(
                f"OpsRamp returned HTTP {status}.", status=status, detail=detail
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            logger.error("OpsRamp returned a non-JSON body for %s", _safe(path))
            raise OpsRampError("OpsRamp returned an unreadable (non-JSON) response.")

    def paginate(
        self,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        method: str = "GET",
    ) -> Iterator[dict]:
        """Yield every record across OpsRamp's paged responses.

        OpsRamp v2 search responses look like::

            {"results": [...], "totalResults": n, "pageNo": 1,
             "pageSize": 100, "totalPages": 3, "nextPage": true}

        Some endpoints return a bare list; both shapes are handled.
        """
        page_no = 1
        seen = 0
        while page_no <= self.config.MAX_PAGES:
            call_params = dict(params or {})
            call_params.setdefault("pageSize", self.config.PAGE_SIZE)
            call_params["pageNo"] = page_no

            payload = self.request(
                method, path, params=call_params, json_body=json_body
            )
            if payload is None:
                return
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        yield item
                return

            results = payload.get("results")
            if results is None:
                results = payload.get("resources") or payload.get("clients") or []
            if isinstance(results, dict):
                results = [results]
            for item in results:
                if isinstance(item, dict):
                    yield item
            seen += len(results)

            total_pages = _as_int(payload.get("totalPages"))
            next_page = payload.get("nextPage")
            total_results = _as_int(payload.get("totalResults"))

            if not results:
                return
            if total_pages is not None and page_no >= total_pages:
                return
            if next_page is False:
                return
            if total_pages is None and next_page is None:
                # Endpoint gives no pagination hints at all.
                if total_results is not None and seen >= total_results:
                    return
                if len(results) < call_params["pageSize"]:
                    return
            page_no += 1

        logger.warning(
            "Stopped paginating %s after MAX_PAGES=%s pages",
            _safe(path),
            self.config.MAX_PAGES,
        )

    # ------------------------------------------------------------------ internal
    def _send(self, method, url, params, json_body, retry_auth: bool):
        token = self.tokens.get()
        headers = {"Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.session.request(
                method.upper(),
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.RequestException as exc:
            logger.error("OpsRamp request to %s failed: %s", _safe(url), exc)
            raise OpsRampError(
                "The OpsRamp API could not be reached. Check network connectivity "
                "and OPSRAMP_BASE_URL."
            ) from exc

        if response.status_code == 401 and retry_auth:
            logger.info("Access token rejected; refreshing once and retrying")
            self.tokens.invalidate()
            try:
                self.tokens.get(force_refresh=True)
            except OpsRampAuthError as exc:
                raise OpsRampError(
                    "Unable to authenticate with OpsRamp. "
                    "Please verify the configured OpsRamp credentials.",
                    status=401,
                ) from exc
            return self._send(method, url, params, json_body, retry_auth=False)
        return response


def _safe(path: str) -> str:
    """Never let query strings (which may carry identifiers) reach the log."""
    return path.split("?", 1)[0]


_DETAIL_KEYS = ("message", "description", "error_description", "error", "detail",
                "errorMessage", "reason")
_MAX_DETAIL = 300


def error_detail(response) -> str:
    """Extract a short, safe explanation from an error response body.

    OpsRamp returns either a JSON object with a message field or a list of
    them. Anything unrecognised falls back to a truncated text body. The
    result is capped so a stray HTML error page cannot flood the log or the UI.
    """
    try:
        body = response.content
    except Exception:  # pragma: no cover - defensive
        return ""
    if not body:
        return ""

    payload = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    def from_mapping(item) -> str:
        for key in _DETAIL_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = from_mapping(value)
                if nested:
                    return nested
        return ""

    text = ""
    if isinstance(payload, dict):
        text = from_mapping(payload)
    elif isinstance(payload, list):
        parts = [from_mapping(i) for i in payload if isinstance(i, dict)]
        text = "; ".join(p for p in parts if p)

    if not text:
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            return ""
        if "<html" in text[:200].lower():
            return ""

    text = " ".join(text.split())
    if len(text) > _MAX_DETAIL:
        text = text[:_MAX_DETAIL] + "..."
    return text


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
