"""Client / SAP tenant discovery."""
from __future__ import annotations

import logging
import re

from .client import OpsRampClient, OpsRampError

logger = logging.getLogger(__name__)


def _display_name(raw: dict) -> str:
    for key in ("name", "clientName", "uniqueId", "displayName", "companyName"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(raw.get("id") or raw.get("uniqueId") or "unknown")


def _tenant_id(raw: dict) -> str | None:
    for key in ("uniqueId", "id", "clientUniqueId", "tenantId"):
        value = raw.get(key)
        if value:
            return str(value)
    return None


def _matches_filter(name: str, pattern: str) -> bool:
    if not pattern:
        return True
    try:
        return re.search(pattern, name, re.IGNORECASE) is not None
    except re.error:
        return pattern.lower() in name.lower()


# A path answering with one of these does not accept the request in the shape
# we send it - wrong URL (404), wrong method (405) or wrong parameters (400).
# Anything else (401, 403, 429, 5xx) is a real failure and must not be masked
# by silently retrying somewhere else.
_TRY_NEXT_STATUSES = (400, 404, 405)


def _fetch_clients(client: OpsRampClient, partner_id: str) -> list[dict]:
    """Fetch raw client records, tolerating instance-specific endpoint shapes.

    OpsRamp exposes the client listing at ``/clients/search`` on current
    versions; a bare ``/clients`` is the create endpoint and answers GET with
    HTTP 405.  Rather than making the operator guess, the configured path is
    tried first and the configured fallbacks after it.
    """
    templates = [client.config.OPSRAMP_CLIENTS_PATH] + list(
        getattr(client.config, "OPSRAMP_CLIENTS_FALLBACK_PATHS", [])
    )
    attempted: list[str] = []
    failures: list[str] = []
    last_error: OpsRampError | None = None

    for template in templates:
        try:
            path = template.format(partner_id=partner_id)
        except (KeyError, IndexError):
            logger.error("Malformed client path template: %s", template)
            continue
        if path in attempted:
            continue
        attempted.append(path)
        try:
            records = list(client.paginate(path))
        except OpsRampError as exc:
            if exc.status in _TRY_NEXT_STATUSES:
                note = f"{path} -> HTTP {exc.status}"
                detail = getattr(exc, "detail", "")
                if detail:
                    note += f" ({detail})"
                logger.info("Client listing %s; trying the next candidate", note)
                failures.append(note)
                last_error = exc
                continue
            raise
        logger.info("Client listing retrieved from %s", path)
        return records

    if last_error is not None:
        raise OpsRampError(
            "Could not list OpsRamp tenants. No configured client endpoint was "
            "accepted. Attempts: "
            + "; ".join(failures)
            + ". Set OPSRAMP_CLIENTS_PATH in the settings file to the correct "
            "path for this OpsRamp instance.",
            status=last_error.status,
        ) from last_error
    raise OpsRampError("No usable OpsRamp client listing endpoint is configured.")


def list_tenants(client: OpsRampClient) -> list[dict]:
    """Return the selectable tenants, filtered by ``TENANT_NAME_FILTER``.

    Each entry: ``{"id": ..., "name": ..., "status": ...}``
    """
    partner_id = client.config.OPSRAMP_PARTNER_TENANT_ID
    if not partner_id:
        raise OpsRampError("OPSRAMP_PARTNER_TENANT_ID is not configured.")

    pattern = client.config.TENANT_NAME_FILTER.strip()

    tenants: list[dict] = []
    seen: set[str] = set()
    for raw in _fetch_clients(client, partner_id):
        tenant_id = _tenant_id(raw)
        if not tenant_id or tenant_id in seen:
            continue
        name = _display_name(raw)
        if not _matches_filter(name, pattern):
            continue
        seen.add(tenant_id)
        tenants.append(
            {
                "id": tenant_id,
                "name": name,
                "status": str(raw.get("activeStatus", raw.get("status", ""))),
            }
        )

    tenants.sort(key=lambda t: t["name"].lower())
    logger.info(
        "Discovered %d selectable tenant(s)%s",
        len(tenants),
        f" matching {pattern!r}" if pattern else "",
    )
    return tenants
