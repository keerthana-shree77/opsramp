"""End-to-end comparison pipeline.

    identify asset -> determine platform/model -> determine components ->
    query OpsRamp -> normalize version -> match recipe -> compare

Everything in here is plain data in, plain data out: no Flask, no globals.  The
web layer calls :func:`run_comparison` from a worker thread and reads progress
through the callback.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from firmware import extractor
from firmware.comparator import compare_versions
from firmware.matcher import RecipeIndex
from firmware.models import (
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_DETECTED,
    STATUS_NOT_IN_RECIPE,
    STATUS_UNABLE,
    ComparisonRow,
    RawAttribute,
    RecipeEntry,
)
from firmware.normalizer import CATEGORY_DISPLAY, platform_display
from opsramp import inventory
from opsramp.client import OpsRampClient

logger = logging.getLogger(__name__)

ProgressFn = Callable[[dict], None]


@dataclass
class Tenant:
    id: str
    name: str


@dataclass
class ComparisonResult:
    tenant_name: str = ""
    tenant_id: str = ""
    category: str = ""
    recipe_name: str = ""
    tenant_names: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    rows: list[ComparisonRow] = field(default_factory=list)
    raw_rows: list[RawAttribute] = field(default_factory=list)
    recipe_entries: list[RecipeEntry] = field(default_factory=list)
    assets_total: int = 0
    assets_matched: int = 0
    summary: dict = field(default_factory=dict)


def _noop(_: dict) -> None:
    return None


def run_many(
    client: OpsRampClient,
    *,
    tenants: list[Tenant],
    categories: list[str],
    entries: list[RecipeEntry],
    recipe_name: str,
    progress: ProgressFn | None = None,
) -> ComparisonResult:
    """Run the comparison across several tenants and merge the results."""
    emit = progress or _noop
    merged = ComparisonResult(
        tenant_name=", ".join(t.name for t in tenants),
        tenant_id=", ".join(t.id for t in tenants),
        category=", ".join(categories),
        recipe_name=recipe_name,
        recipe_entries=entries,
        tenant_names=[t.name for t in tenants],
        categories=list(categories),
    )

    totals = {"assets_discovered": 0, "assets_processed": 0, "assets_selected": 0}
    for index, tenant in enumerate(tenants, start=1):
        prefix = f"Tenant {index}/{len(tenants)}: {tenant.name}"

        def relay(update: dict, _prefix=prefix, _base=dict(totals)) -> None:
            merged_update = dict(update)
            for key in ("assets_discovered", "assets_processed", "assets_selected"):
                if key in merged_update:
                    merged_update[key] = _base[key] + (merged_update[key] or 0)
            if merged_update.get("message"):
                merged_update["message"] = f"{_prefix} - {merged_update['message']}"
            emit(merged_update)

        one = run_comparison(
            client,
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            category=categories,
            entries=entries,
            recipe_name=recipe_name,
            progress=relay,
        )
        merged.rows.extend(one.rows)
        merged.raw_rows.extend(one.raw_rows)
        merged.assets_total += one.assets_total
        merged.assets_matched += one.assets_matched
        totals["assets_discovered"] += one.assets_total
        totals["assets_processed"] += one.assets_matched
        totals["assets_selected"] += one.assets_matched

    merged.summary = build_summary(merged)
    emit({"stage": "done", "message": "Comparison complete"})
    return merged


@dataclass
class Extraction:
    """What one tenant's assets yielded, before any recipe is applied.

    Reading a tenant costs hundreds of OpsRamp calls; comparing what was read
    against a recipe costs nothing but CPU.  Keeping the two apart is what lets
    the matrix measure one estate against several recipes without querying that
    estate several times over.
    """

    tenant_id: str = ""
    tenant_name: str = ""
    category: object = "all"
    components: list = field(default_factory=list)
    raw_rows: list[RawAttribute] = field(default_factory=list)
    assets_total: int = 0
    assets_matched: int = 0


def extract_tenant(
    client: OpsRampClient,
    *,
    tenant_id: str,
    tenant_name: str,
    category,
    progress: ProgressFn | None = None,
) -> Extraction:
    """Read one tenant's installed firmware.  No recipe is involved."""
    emit = progress or _noop

    emit({"stage": "authenticating", "message": "Authenticating with OpsRamp"})
    client.verify_connectivity()

    emit({"stage": "discovering", "message": f"Discovering assets in {tenant_name}"})
    resources = inventory.search_resources(
        client,
        tenant_id,
        on_page=lambda count: emit(
            {
                "stage": "discovering",
                "assets_discovered": count,
                "message": f"Discovered {count} assets so far",
            }
        ),
    )
    emit(
        {
            "stage": "discovering",
            "assets_discovered": len(resources),
            "message": f"Discovered {len(resources)} assets",
        }
    )

    # ------------------------------------------------ first pass: category gate
    candidates = []
    unresolved = []
    noise = 0
    skipped_types: dict[str, int] = {}
    for resource in resources:
        if extractor.is_noise_resource(resource):
            noise += 1
            # Recorded by type so that a resource wrongly treated as a part is
            # visible in the log rather than silently missing from the matrix.
            kind = str(
                resource.get("resourceType") or resource.get("deviceType") or "?"
            )
            skipped_types[kind] = skipped_types.get(kind, 0) + 1
            continue
        asset, _flat = extractor.build_asset(resource, tenant_name, tenant_id)
        if asset.platform_key:
            if extractor.matches_category(asset, category):
                candidates.append(resource)
        else:
            unresolved.append(resource)

    deep_unresolved = getattr(client.config, "DEEP_FETCH_UNMATCHED", True)
    limit = getattr(client.config, "MAX_UNRESOLVED_DEEP_FETCH", 2000)
    if deep_unresolved and unresolved:
        take = unresolved[:limit]
        if len(unresolved) > limit:
            logger.warning(
                "Only the first %d of %d unclassified assets will be deep-fetched "
                "(MAX_UNRESOLVED_DEEP_FETCH)",
                limit,
                len(unresolved),
            )
        candidates.extend(take)

    logger.info(
        "Tenant %s: %d asset(s) selected for extraction out of %d discovered "
        "(%d skipped as parts of a machine: %s)",
        tenant_id,
        len(candidates),
        len(resources),
        noise,
        ", ".join(
            f"{kind} x{count}"
            for kind, count in sorted(skipped_types.items(), key=lambda kv: -kv[1])
        ) or "none",
    )
    emit(
        {
            "stage": "extracting",
            "assets_discovered": len(resources),
            "assets_selected": len(candidates),
            "assets_processed": 0,
            "message": f"Extracting firmware from {len(candidates)} assets",
        }
    )

    detailed = inventory.fetch_details(
        client,
        tenant_id,
        candidates,
        progress=lambda stage, done, total: emit(
            {
                "stage": "extracting",
                "assets_processed": done,
                "assets_selected": total,
                "message": f"Extracting firmware {done}/{total}",
            }
        ),
    )

    # ------------------------------------------------ second pass: extraction
    assets_matched = 0
    installed_components = []
    raw_rows: list[RawAttribute] = []
    for document in detailed:
        asset, components, raws = extractor.process_resource(
            document, tenant_name, tenant_id, category
        )
        if asset is None:
            continue
        assets_matched += 1
        installed_components.extend(components)
        raw_rows.extend(raws)

    emit(
        {
            "stage": "comparing",
            "assets_processed": len(detailed),
            "assets_selected": len(candidates),
            "components": len(installed_components),
            "message": f"Extracted {len(installed_components)} components",
        }
    )
    return Extraction(
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        category=category,
        components=installed_components,
        raw_rows=raw_rows,
        assets_total=len(resources),
        assets_matched=assets_matched,
    )


def compare_extraction(
    extraction: Extraction,
    *,
    entries: list[RecipeEntry],
    recipe_name: str,
    policy: str = "at_least",
) -> ComparisonResult:
    """Measure an already-read tenant against one recipe.  Pure, and cheap.

    Called once per recipe over the same :class:`Extraction`, which is how a
    single read of an estate yields a verdict for every approved recipe.
    """
    index = RecipeIndex(entries)
    rows = [
        _compare_one(component, index, policy) for component in extraction.components
    ]

    category = extraction.category
    result = ComparisonResult(
        tenant_name=extraction.tenant_name,
        tenant_id=extraction.tenant_id,
        category=category if isinstance(category, str) else ", ".join(category),
        recipe_name=recipe_name,
        rows=rows,
        raw_rows=extraction.raw_rows,
        recipe_entries=entries,
        assets_total=extraction.assets_total,
        assets_matched=extraction.assets_matched,
    )
    result.summary = build_summary(result)
    return result


def run_comparison(
    client: OpsRampClient,
    *,
    tenant_id: str,
    tenant_name: str,
    category,
    entries: list[RecipeEntry],
    recipe_name: str,
    progress: ProgressFn | None = None,
) -> ComparisonResult:
    emit = progress or _noop
    extraction = extract_tenant(
        client,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        category=category,
        progress=progress,
    )
    emit(
        {
            "stage": "comparing",
            "message": (
                f"Comparing {len(extraction.components)} components against the recipe"
            ),
        }
    )
    result = compare_extraction(
        extraction,
        entries=entries,
        recipe_name=recipe_name,
        policy=client.config.VERSION_COMPARE_POLICY,
    )
    emit({"stage": "done", "message": "Comparison complete"})
    return result


def _compare_one(component, index: RecipeIndex, policy: str) -> ComparisonRow:
    asset = component.asset
    row = ComparisonRow(
        tenant=asset.tenant,
        hostname=asset.hostname or asset.resource_name,
        ip_address=asset.ip_address,
        resource_name=asset.resource_name,
        category=CATEGORY_DISPLAY.get(asset.category, asset.category),
        platform=platform_display(asset.platform_key, asset.platform),
        model=asset.model,
        component=component.component,
        installed_version=component.installed_version or "",
        installed_build=component.installed_build or "",
        source_attribute=component.source_attribute,
        asset_id=asset.asset_id,
    )

    match = index.match(asset, component.component_key)

    if match.ambiguous:
        row.result = STATUS_UNABLE
        row.reason = match.note
        row.match_level = match.level
        return row

    if match.entry is None:
        if not component.detected:
            row.result = STATUS_NOT_DETECTED
            row.reason = (
                "The OpsRamp asset was found, but no recognised attribute "
                "containing the required component version was detected. "
                "Additionally, no recipe entry exists for this "
                "platform/model/component combination."
            )
            return row
        row.result = STATUS_NOT_IN_RECIPE
        row.reason = match.note or (
            "No recipe entry exists for this platform/model/component combination."
        )
        return row

    entry = match.entry
    row.match_level = match.level
    # Carry a matcher note (an alias that was applied, for instance) into the
    # reason, so every match can be traced to why it was made.
    match_note = match.note
    row.target_version = entry.target_version or ""
    row.target_build = entry.target_build or ""
    row.mandatory = entry.mandatory

    if entry.not_applicable:
        row.result = STATUS_NOT_APPLICABLE
        row.reason = (
            f"Recipe row {entry.row_number} marks {component.component} as not "
            "applicable for this platform."
        )
        return row

    if not component.detected:
        row.result = STATUS_NOT_DETECTED
        row.reason = component.detection_note or (
            "The OpsRamp asset was found, but no recognised attribute containing "
            "the required component version was detected."
        )
        return row

    verdict = compare_versions(
        component.installed_version,
        entry.target_version,
        component.installed_build,
        entry.target_build,
        policy=policy,
    )
    row.result = verdict["status"]
    row.reason = verdict["reason"]
    if match_note:
        row.reason = f"{row.reason} {match_note}".strip()
    row.installed_build = verdict["installed_build"]
    row.target_build = verdict["target_build"]
    return row


def build_summary(result: ComparisonResult) -> dict:
    counts: dict[str, int] = {}
    for row in result.rows:
        counts[row.result] = counts.get(row.result, 0) + 1

    hosts = {(row.asset_id or row.hostname) for row in result.rows}
    categories = result.categories or [result.category]
    return {
        "tenant": result.tenant_name,
        "category": ", ".join(
            CATEGORY_DISPLAY.get(c, c) for c in categories if c
        ) or CATEGORY_DISPLAY.get(result.category, result.category),
        "recipe": result.recipe_name,
        "assets_total": result.assets_total,
        "assets_in_scope": result.assets_matched or len(hosts),
        "components_checked": len(result.rows),
        "recipe_entries": len(result.recipe_entries),
        "counts": counts,
    }
