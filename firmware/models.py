"""Normalized internal schemas shared by the extraction, matching, comparison
and reporting layers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------- results

STATUS_UPDATED = "UPDATED"
STATUS_NEEDS_UPDATE = "NEEDS UPDATE"
STATUS_NOT_DETECTED = "VERSION NOT DETECTED"
STATUS_NOT_IN_RECIPE = "NOT FOUND IN RECIPE"
STATUS_NOT_APPLICABLE = "NOT APPLICABLE"
STATUS_UNABLE = "UNABLE TO COMPARE"

ALL_STATUSES = (
    STATUS_UPDATED,
    STATUS_NEEDS_UPDATE,
    STATUS_NOT_DETECTED,
    STATUS_NOT_IN_RECIPE,
    STATUS_NOT_APPLICABLE,
    STATUS_UNABLE,
)

# CSS class per status, used by the dashboard and the XLSX report.
STATUS_TONE = {
    STATUS_UPDATED: "ok",
    STATUS_NEEDS_UPDATE: "bad",
    STATUS_NOT_DETECTED: "warn",
    STATUS_NOT_IN_RECIPE: "muted",
    STATUS_NOT_APPLICABLE: "muted",
    STATUS_UNABLE: "warn",
}


@dataclass
class Asset:
    """One OpsRamp resource, reduced to the fields the tool cares about."""

    tenant: str = ""
    tenant_id: str = ""
    asset_id: str = ""
    hostname: str = ""
    ip_address: str = ""
    manufacturer: str = ""
    platform: str = ""          # display platform, e.g. "HPE DL"
    platform_key: str = ""      # canonical key, e.g. "hpe_dl"
    model: str = ""
    model_key: str = ""
    category: str = ""          # server | switch | storage | esxi
    resource_name: str = ""
    resource_type: str = ""
    native_resource_type: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InstalledComponent:
    """X-axis: one component version discovered on one asset."""

    asset: Asset
    component_key: str = ""          # canonical, e.g. "bios"
    component: str = ""              # display, e.g. "BIOS"
    installed_version: str | None = None
    installed_build: str | None = None
    source_attribute: str = ""       # raw attribute path the value came from
    source_value: str = ""           # the raw, untruncated attribute value
    confidence: int = 0              # alias-match score, for troubleshooting
    detection_note: str = ""

    @property
    def detected(self) -> bool:
        return bool(self.installed_version) or bool(self.installed_build)


@dataclass
class RawAttribute:
    """A single candidate attribute preserved for audit / troubleshooting."""

    tenant: str = ""
    asset_id: str = ""
    hostname: str = ""
    ip_address: str = ""
    resource_name: str = ""
    resource_type: str = ""
    manufacturer: str = ""
    model: str = ""
    category: str = ""
    component: str = ""
    raw_attribute_name: str = ""
    raw_attribute_value: str = ""
    installed_version: str = ""
    selected: bool = False
    score: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecipeEntry:
    """Y-axis: one approved target version."""

    row_number: int = 0
    platform: str = ""
    platform_key: str = ""
    model: str = ""
    model_key: str = ""
    model_tokens: tuple[str, ...] = ()
    category: str = ""
    component: str = ""
    component_key: str = ""
    target_version: str | None = None
    target_build: str | None = None
    mandatory: bool = True
    not_applicable: bool = False
    applies_to_family: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["model_tokens"] = list(self.model_tokens)
        return data


@dataclass
class ComparisonRow:
    """One line of the compliance report."""

    tenant: str = ""
    hostname: str = ""
    ip_address: str = ""
    # What OpsRamp calls the resource. Worth carrying separately because for
    # some hardware it is the only thing that identifies the machine: an
    # HPE OneView nPartition is registered under its management IP with no
    # hostname at all, and reads as an anonymous address in the report unless
    # its resource name - "CZA0000003, Npar 0" - is shown beside it.
    resource_name: str = ""
    category: str = ""
    platform: str = ""
    model: str = ""
    component: str = ""
    installed_version: str = ""
    target_version: str = ""
    installed_build: str = ""
    target_build: str = ""
    result: str = ""
    reason: str = ""
    mandatory: bool = True
    match_level: str = ""
    source_attribute: str = ""
    asset_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


COMPARISON_COLUMNS = [
    ("tenant", "Tenant"),
    ("hostname", "Hostname"),
    ("ip_address", "IP"),
    ("resource_name", "Resource Name"),
    ("category", "Category"),
    ("platform", "Platform"),
    ("model", "Model"),
    ("component", "Component"),
    ("installed_version", "Installed Version (X-axis)"),
    ("target_version", "Target Version (Y-axis)"),
    ("installed_build", "Installed Build"),
    ("target_build", "Target Build"),
    ("result", "Result"),
    ("reason", "Reason"),
    ("mandatory", "Mandatory"),
    ("match_level", "Match Level"),
    ("source_attribute", "Source Attribute"),
    ("asset_id", "Asset ID"),
]

RAW_COLUMNS = [
    ("tenant", "Tenant"),
    ("asset_id", "Asset ID"),
    ("hostname", "Hostname"),
    ("ip_address", "IP"),
    ("resource_name", "Resource Name"),
    ("resource_type", "Resource Type"),
    ("manufacturer", "Manufacturer"),
    ("model", "Model"),
    ("category", "Category"),
    ("component", "Component"),
    ("raw_attribute_name", "Raw Attribute Name"),
    ("raw_attribute_value", "Raw Attribute Value"),
    ("installed_version", "Installed Version"),
    ("selected", "Selected"),
    ("score", "Score"),
]

RECIPE_COLUMNS = [
    ("row_number", "Recipe Row"),
    ("platform", "Platform"),
    ("platform_key", "Platform Key"),
    ("model", "Model"),
    ("model_key", "Model Key"),
    ("category", "Category"),
    ("component", "Component"),
    ("component_key", "Component Key"),
    ("target_version", "Target Version"),
    ("target_build", "Target Build"),
    ("mandatory", "Mandatory"),
    ("applies_to_family", "Applies To Family"),
    ("not_applicable", "Not Applicable"),
]
