"""Storing what was read from OpsRamp, so it is read once and reused.

Reading an account is the expensive half of this tool - hundreds of API calls
per tenant - and it is also the half that does not change when a recipe does.
Keeping the reading on disk is what lets a newly uploaded recipe be answered
immediately instead of by sweeping the whole estate again.

The encoding is deliberately plain JSON. It is written compressed because it
is repetitive text, but anyone can decompress it and read what the tool
believed was installed, which matters for a tool whose whole job is to be
checked.
"""
from __future__ import annotations

import hashlib
import json
import logging
import zlib
from dataclasses import asdict

from .models import Asset, InstalledComponent, RawAttribute

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1

# Component fields are abbreviated: an estate of forty accounts stores a few
# hundred thousand of them and the key names would otherwise dominate.
_COMPONENT_FIELDS = (
    ("a", "asset_index"),
    ("k", "component_key"),
    ("c", "component"),
    ("v", "installed_version"),
    ("b", "installed_build"),
    ("s", "source_attribute"),
    ("r", "source_value"),
    ("f", "confidence"),
    ("n", "detection_note"),
)


def encode(extraction) -> bytes:
    """Turn one tenant's reading into a compressed JSON document."""
    assets: list[dict] = []
    positions: dict[int, int] = {}

    def position_of(asset: Asset) -> int:
        # Components of one asset share its object, so it is stored once.
        marker = id(asset)
        if marker not in positions:
            positions[marker] = len(assets)
            assets.append(asdict(asset))
        return positions[marker]

    components = []
    for component in extraction.components:
        record = {"a": position_of(component.asset)}
        for short, long in _COMPONENT_FIELDS[1:]:
            record[short] = getattr(component, long)
        components.append(record)

    category = extraction.category
    document = {
        "format": FORMAT_VERSION,
        "tenant_id": extraction.tenant_id,
        "tenant_name": extraction.tenant_name,
        "category": list(category) if isinstance(category, (list, tuple)) else category,
        "assets_total": extraction.assets_total,
        "assets_matched": extraction.assets_matched,
        "assets": assets,
        "components": components,
        "raw_rows": [asdict(row) for row in extraction.raw_rows],
    }
    return zlib.compress(json.dumps(document).encode("utf-8"), 6)


def decode(blob: bytes):
    """Rebuild an :class:`service.Extraction` from a stored document.

    Raises ``ValueError`` if the blob cannot be read, so a caller can fall
    back to reading the account again rather than working from wreckage.
    """
    from service import Extraction  # local import: avoids a cycle at load

    try:
        document = json.loads(zlib.decompress(blob).decode("utf-8"))
    except (zlib.error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"the stored reading could not be decompressed: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("the stored reading is not a document")
    if document.get("format") != FORMAT_VERSION:
        raise ValueError(
            f"the stored reading is format {document.get('format')!r}, "
            f"this build reads {FORMAT_VERSION}"
        )

    assets = [Asset(**_known(Asset, record)) for record in document.get("assets", [])]
    components = []
    for record in document.get("components", []):
        index = record.get("a", -1)
        if not 0 <= index < len(assets):
            continue
        fields = {
            long: record.get(short)
            for short, long in _COMPONENT_FIELDS[1:]
        }
        components.append(InstalledComponent(asset=assets[index], **fields))

    return Extraction(
        tenant_id=document.get("tenant_id", ""),
        tenant_name=document.get("tenant_name", ""),
        category=document.get("category", "all"),
        components=components,
        raw_rows=[
            RawAttribute(**_known(RawAttribute, row))
            for row in document.get("raw_rows", [])
        ],
        assets_total=int(document.get("assets_total") or 0),
        assets_matched=int(document.get("assets_matched") or 0),
    )


def fingerprint(extraction) -> str:
    """A short hash of what was found, ignoring the order it was found in.

    The background scan uses this to tell an account that has changed from one
    that has not, so an unchanged account is not re-measured and does not churn
    the page. It covers the identity and the version of every component -
    everything a verdict depends on - and nothing else, so a re-read that
    happens to order assets differently does not read as a change.
    """
    parts = sorted(
        "|".join(
            (
                component.asset.asset_id or component.asset.hostname,
                component.asset.model_key,
                component.asset.platform_key,
                component.component_key,
                component.installed_version or "",
                component.installed_build or "",
            )
        )
        for component in extraction.components
    )
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def _known(cls, record: dict) -> dict:
    """Only the fields this build knows, so an older document still loads."""
    if not isinstance(record, dict):
        return {}
    allowed = set(cls.__dataclass_fields__)
    return {key: value for key, value in record.items() if key in allowed}
