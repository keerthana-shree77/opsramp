"""Dump what OpsRamp actually returns for a handful of assets.

Answers the two questions that guesswork cannot:
  1. Do the resource sub-endpoints exist on this instance, or do they 404?
  2. What are the real attribute names carrying model and firmware?

Usage:
    python tools/diagnose_opsramp.py [tenant_substring] [asset_count]

Writes OPSRAMP-DIAGNOSTIC.txt. No tokens or secrets are written to it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, configure_logging  # noqa: E402
from firmware.extractor import (  # noqa: E402
    build_asset,
    extract_components,
    flatten,
    is_noise_resource,
)
from opsramp.client import OpsRampClient, OpsRampError  # noqa: E402
from opsramp.inventory import search_resources  # noqa: E402
from opsramp.tenants import list_tenants  # noqa: E402

OUT = Path("OPSRAMP-DIAGNOSTIC.txt")
MAX_ATTRS = 220
MAX_VALUE = 90


def probe(client, tenant_id, resource_id, out):
    """Call the detail and sub-resource endpoints, recording each status."""
    merged = {}
    templates = [Config.OPSRAMP_RESOURCE_DETAIL_PATH] + list(
        Config.OPSRAMP_RESOURCE_EXTRA_PATHS
    )
    out.append("  endpoint results:")
    for template in templates:
        path = template.format(tenant_id=tenant_id, resource_id=resource_id)
        label = path.rsplit("/", 1)[-1]
        try:
            payload = client.get(path, allow_missing=True)
        except OpsRampError as exc:
            out.append(f"    {label:<20} FAILED  {exc}")
            continue
        if payload is None:
            out.append(f"    {label:<20} HTTP 404 (endpoint not present)")
            continue
        if isinstance(payload, list):
            out.append(f"    {label:<20} OK  list of {len(payload)} item(s)")
            merged.setdefault("_extra", {})[label] = payload
        elif isinstance(payload, dict):
            keys = len(payload)
            out.append(f"    {label:<20} OK  object with {keys} key(s)")
            if template == Config.OPSRAMP_RESOURCE_DETAIL_PATH:
                merged.update(payload)
            else:
                merged.setdefault("_extra", {})[label] = payload
        else:
            out.append(f"    {label:<20} OK  {type(payload).__name__}")
    return merged


def main() -> int:
    configure_logging("WARNING")
    wanted = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    asset_filter = sys.argv[3].lower() if len(sys.argv) > 3 else ""

    missing = Config.missing_required()
    if missing:
        print("Missing configuration: " + ", ".join(missing))
        return 1

    out: list[str] = ["OPSRAMP DIAGNOSTIC REPORT", ""]
    client = OpsRampClient(Config)

    tenants = list_tenants(client)
    out.append(f"Tenants visible: {len(tenants)}")
    for t in tenants[:20]:
        out.append(f"  - {t['name']}  (id {t['id']})")
    if not tenants:
        out.append("  (none - check TENANT_NAME_FILTER)")
        OUT.write_text("\n".join(out), encoding="utf-8")
        return 1

    chosen = next((t for t in tenants if wanted in t["name"].lower()), tenants[0])
    out.append("")
    out.append(f"Using tenant: {chosen['name']} (id {chosen['id']})")

    resources = search_resources(client, chosen["id"])
    out.append(f"Resources returned by search: {len(resources)}")
    out.append("")

    out.append("=" * 72)
    out.append("SEARCH RESULT KEYS (what the listing alone provides)")
    out.append("=" * 72)
    if resources:
        out.append("  " + ", ".join(sorted(resources[0].keys())))
    out.append("")

    # Prefer servers so the sample is representative of the problem.
    def rank(resource):
        text = json.dumps(resource).lower()
        score = 0
        if any(w in text for w in ("proliant", "dl3", "dl5", "gen1")):
            score -= 2
        if "superdome" in text or "sdflex" in text:
            score -= 1
        return score

    sample = [r for r in resources if not is_noise_resource(r)]
    if asset_filter:
        sample = [r for r in sample if asset_filter in json.dumps(r).lower()]
        out.append(f"Asset filter {asset_filter!r} matched {len(sample)} asset(s).")
    else:
        sample.sort(key=rank)
    sample = sample[:count]
    out.append(f"Sampling {len(sample)} asset(s) of {len(resources)}.")

    for resource in sample:
        rid = str(resource.get("id") or resource.get("uniqueId") or "")
        name = (
            resource.get("resourceName")
            or resource.get("hostName")
            or resource.get("name")
            or rid
        )
        out.append("")
        out.append("=" * 72)
        out.append(f"ASSET: {name}   (id {rid})")
        out.append("=" * 72)
        out.append(f"  deviceType         : {resource.get('deviceType')}")
        out.append(f"  resourceType       : {resource.get('resourceType')}")
        out.append(f"  nativeResourceType : {resource.get('nativeResourceType')}")

        merged = dict(resource)
        merged.update(probe(client, chosen["id"], rid, out))

        asset, flat = build_asset(merged, chosen["name"], chosen["id"])
        out.append("")
        out.append("  what the tool concluded:")
        out.append(f"    platform_key = {asset.platform_key or '(none)'}")
        out.append(f"    model        = {asset.model or '(none)'}")
        out.append(f"    manufacturer = {asset.manufacturer or '(none)'}")
        out.append(f"    category     = {asset.category or '(none)'}")
        components, _raws = extract_components(asset, flat)
        out.append("")
        out.append("  FIRMWARE EXTRACTED:")
        if not components:
            out.append("    (none - the platform was not identified)")
        for component in components:
            if component.detected:
                out.append(
                    f"    {component.component:<26} = {component.installed_version}"
                    + (f"  build {component.installed_build}" if component.installed_build else "")
                )
                out.append(f"       from: {component.source_attribute}")
            else:
                out.append(f"    {component.component:<26} = NOT DETECTED")

        out.append("")
        out.append(f"  ALL ATTRIBUTES FOUND ({len(flat)} total, showing {MAX_ATTRS}):")
        for path, _key, value in flat[:MAX_ATTRS]:
            shown = value if len(value) <= MAX_VALUE else value[:MAX_VALUE] + "..."
            out.append(f"    {path} = {shown}")
        if len(flat) > MAX_ATTRS:
            out.append(f"    ... {len(flat) - MAX_ATTRS} more not shown")

    client.close()
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {OUT.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
