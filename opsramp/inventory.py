"""Resource (asset) retrieval.

Two stages, mirroring the baseline application's behaviour but with controlled
concurrency:

  1. ``search_resources`` walks the paginated resource-search endpoint and
     returns the summary record for every asset in the tenant.
  2. ``fetch_details`` pulls the full resource document (and any configured
     extra endpoints) for the subset of assets that actually matched a
     supported category, using a bounded thread pool.

Only assets that survive category matching are deep-fetched - that is the
"targeted extraction strategy" the specification asks for, and it is what keeps
the OpsRamp call count proportional to the interesting assets rather than to
the whole tenant.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

from .client import OpsRampClient, OpsRampError

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int], None]


def _resource_id(raw: dict) -> str | None:
    for key in ("id", "uniqueId", "resourceUUID", "resourceId"):
        value = raw.get(key)
        if value:
            return str(value)
    return None


def search_resources(
    client: OpsRampClient,
    tenant_id: str,
    *,
    query: str | None = None,
    on_page: Callable[[int], None] | None = None,
) -> list[dict]:
    """Return every resource summary for a tenant."""
    path = client.config.OPSRAMP_RESOURCE_SEARCH_PATH.format(tenant_id=tenant_id)
    params: dict = {}
    if query:
        params["queryString"] = query

    resources: list[dict] = []
    seen: set[str] = set()
    for raw in client.paginate(path, params=params):
        rid = _resource_id(raw)
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        resources.append(raw)
        if on_page and len(resources) % client.config.PAGE_SIZE == 0:
            on_page(len(resources))

    logger.info("Tenant %s: retrieved %d resource summaries", tenant_id, len(resources))
    return resources


def fetch_detail(client: OpsRampClient, tenant_id: str, resource: dict) -> dict:
    """Merge the resource summary with its detail document(s).

    Returns the merged dictionary.  Detail failures are non-fatal: the summary
    is returned on its own and an ``_extraction_errors`` key records why, so the
    raw-data export can explain a missing version.
    """
    rid = _resource_id(resource)
    if not rid:
        return dict(resource)

    merged = dict(resource)
    errors: list[str] = []

    if client.config.DETAIL_FETCH_ENABLED:
        path = client.config.OPSRAMP_RESOURCE_DETAIL_PATH.format(
            tenant_id=tenant_id, resource_id=rid
        )
        try:
            detail = client.get(path, allow_missing=True)
            if isinstance(detail, dict):
                merged.update(detail)
            elif detail is None:
                errors.append("detail endpoint returned HTTP 404")
        except OpsRampError as exc:
            logger.warning("Detail fetch failed for resource %s: %s", rid, exc)
            errors.append(f"detail endpoint failed: {exc}")

    for template in client.config.OPSRAMP_RESOURCE_EXTRA_PATHS:
        try:
            extra_path = template.format(tenant_id=tenant_id, resource_id=rid)
        except (KeyError, IndexError):
            logger.error("Malformed OPSRAMP_RESOURCE_EXTRA_PATHS entry: %s", template)
            continue
        try:
            extra = client.get(extra_path, allow_missing=True)
        except OpsRampError as exc:
            logger.warning("Extra endpoint %s failed for %s: %s", template, rid, exc)
            errors.append(f"{template}: {exc}")
            continue
        if extra is None:
            continue
        key = template.rstrip("/").rsplit("/", 1)[-1] or "extra"
        merged.setdefault("_extra", {})[key] = extra

    if errors:
        merged["_extraction_errors"] = errors
    return merged


def fetch_details(
    client: OpsRampClient,
    tenant_id: str,
    resources: Iterable[dict],
    *,
    progress: ProgressFn | None = None,
) -> list[dict]:
    """Deep-fetch a list of resources with bounded concurrency."""
    items = list(resources)
    total = len(items)
    if not total:
        return []

    workers = max(1, min(client.config.MAX_WORKERS, total))
    logger.info(
        "Fetching detail for %d resource(s) in tenant %s with %d worker(s)",
        total,
        tenant_id,
        workers,
    )

    results: list[dict] = []
    done = 0
    originals: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_detail, client, tenant_id, item): item for item in items
        }
        for future in as_completed(futures):
            original = futures[future]
            originals[_resource_id(original)] = original
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - one bad asset must not abort
                rid = _resource_id(original)
                logger.error("Unexpected failure fetching resource %s: %s", rid, exc)
                fallback = dict(original)
                fallback["_extraction_errors"] = [f"unexpected failure: {exc}"]
                results.append(fallback)
            done += 1
            if progress:
                progress("extracting", done, total)

    return _retry_the_ones_that_failed(client, tenant_id, results, originals)


# A rate limit is a statement about how fast we asked, not about the resource.
# Written off, it becomes a permanent-looking "no firmware version reported" -
# and for a PDU, whose firmware is only on the detail endpoint, that is the
# whole row. So the ones that failed for a reason worth retrying are gone over
# again, one at a time: the pass that failed was the crowded one.
_WORTH_RETRYING = ("429", "rate", "too many requests", "timed out", "timeout",
                   "could not be reached", "http 50")


def _worth_retrying(document: dict) -> bool:
    errors = document.get("_extraction_errors") or []
    text = " ".join(str(e) for e in errors).lower()
    return any(mark in text for mark in _WORTH_RETRYING)


def _retry_the_ones_that_failed(
    client: OpsRampClient, tenant_id: str, results: list[dict], originals: dict
) -> list[dict]:
    failed = [d for d in results if _worth_retrying(d)]
    if not failed:
        return results

    logger.info(
        "Retrying detail for %d resource(s) that were rate-limited or timed out",
        len(failed),
    )
    recovered = 0
    by_id = {}
    for document in failed:
        rid = _resource_id(document)
        original = originals.get(rid, document)
        try:
            again = fetch_detail(client, tenant_id, original)
        except Exception as exc:  # noqa: BLE001 - it already failed once
            logger.warning("Retry of resource %s failed as well: %s", rid, exc)
            continue
        if not _worth_retrying(again):
            recovered += 1
        by_id[rid] = again

    if by_id:
        results = [by_id.get(_resource_id(d), d) for d in results]
    logger.info(
        "Recovered detail for %d of %d resource(s) on the second pass",
        recovered,
        len(failed),
    )
    return results
