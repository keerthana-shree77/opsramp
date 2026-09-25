"""Firmware/software version extraction from OpsRamp resource documents.

The guiding rule from the specification: *never assume every OpsRamp resource
exposes firmware information under the same attribute name*.  So the engine

  1. flattens the whole resource document (including ``attributes``,
     ``customAttributes``, native attributes, hardware blocks and anything the
     detail endpoints added) into ``(path, key, value)`` triples;
  2. identifies the platform and category from many fields, not the hostname;
  3. looks up which components that platform is expected to expose;
  4. scores every flattened attribute against that component's alias list,
     rejecting attributes whose name contains a disqualifying word and values
     that do not look like a version at all;
  5. keeps the best-scoring candidate and preserves every scored candidate as
     raw audit data.

Nothing is invented: when no candidate survives, the component is emitted with
``installed_version=None`` so the comparison reports VERSION NOT DETECTED.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from .comparator import looks_like_version
from .models import Asset, InstalledComponent, RawAttribute
from .normalizer import (
    BUILD_ALIASES,
    CATEGORY_ALL,
    COMPONENTS,
    GENERIC_PLATFORMS,
    component_display,
    components_for_platform,
    detect_platform,
    model_tokens,
    norm_text,
    normalize_model,
    platform_component_aliases,
    platform_display,
    PLATFORM_CATEGORY,
)

logger = logging.getLogger(__name__)

MAX_DEPTH = 8
MAX_NODES = 8000
MAX_LIST_ITEMS = 250
MAX_RAW_PER_COMPONENT = 6
MAX_VALUE_LEN = 512

# Bookkeeping this module adds itself; everything else is real payload.
_INTERNAL_KEYS = {"_extraction_errors"}

# Key names used by OpsRamp for name/value attribute pairs.
_NAME_KEYS = (
    "name",
    "attributeName",
    "customAttributeName",
    "metricName",
    "key",
    "label",
    "displayName",
    "attribute",
)
_VALUE_KEYS = (
    "value",
    "attributeValue",
    "customAttributeValue",
    "metricValue",
    "attrValue",
    "val",
    "displayValue",
    "data",
    "content",
)


# ------------------------------------------------------------------ flattening


_NESTED_NAME_HOLDERS = ("customAttribute", "attribute", "metric", "property", "tag")


def _pair_from(item: dict) -> tuple[str, Any] | None:
    # OpsRamp nests custom attributes as
    #   {"customAttribute": {"name": "..."}, "value": "..."}
    # so the name and the value sit at different levels.
    for holder in _NESTED_NAME_HOLDERS:
        nested = item.get(holder)
        if not isinstance(nested, dict):
            continue
        nested_name = None
        for key in _NAME_KEYS:
            candidate = nested.get(key)
            if isinstance(candidate, (str, int)) and str(candidate).strip():
                nested_name = str(candidate).strip()
                break
        if nested_name is None:
            continue
        for key in _VALUE_KEYS:
            if key in item and not isinstance(item[key], (dict, list)):
                return nested_name, item[key]
        for key in _VALUE_KEYS:
            if key in nested and not isinstance(nested[key], (dict, list)):
                return nested_name, nested[key]

    name = None
    for key in _NAME_KEYS:
        candidate = item.get(key)
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            name = str(candidate).strip()
            break
    if name is None:
        return None
    for key in _VALUE_KEYS:
        if key in item:
            return name, item[key]
    return None


def flatten(document: Any) -> list[tuple[str, str, str]]:
    """Return ``(path, leaf_key, value)`` for every scalar in the document."""
    out: list[tuple[str, str, str]] = []
    budget = [MAX_NODES]

    def walk(node: Any, path: str, key: str, depth: int) -> None:
        if budget[0] <= 0 or depth > MAX_DEPTH:
            return
        if isinstance(node, dict):
            pair = _pair_from(node)
            if pair is not None and not isinstance(pair[1], (dict, list)):
                name, value = pair
                budget[0] -= 1
                out.append((f"{path}.{name}" if path else name, name, _stringify(value)))
                # Still walk siblings that are not the name/value pair itself.
                for sub_key, sub in node.items():
                    if sub_key in _NAME_KEYS or sub_key in _VALUE_KEYS:
                        continue
                    walk(sub, f"{path}.{name}.{sub_key}" if path else f"{name}.{sub_key}",
                         sub_key, depth + 1)
                return
            for sub_key, sub in node.items():
                # "_extra" carries the merged sub-resource payloads, which is
                # where most firmware actually lives; only bookkeeping keys
                # are skipped.
                if sub_key in _INTERNAL_KEYS:
                    continue
                walk(sub, f"{path}.{sub_key}" if path else str(sub_key), str(sub_key),
                     depth + 1)
            return
        if isinstance(node, list):
            for index, item in enumerate(node[:MAX_LIST_ITEMS]):
                walk(item, f"{path}[{index}]", key, depth + 1)
            return
        if node is None or isinstance(node, bool):
            return
        budget[0] -= 1
        out.append((path, key, _stringify(node)))

    walk(document, "", "", 0)
    return out


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:MAX_VALUE_LEN]


# ------------------------------------------------------------- asset identity

_HOSTNAME_KEYS = ("hostname", "hostName", "name", "resourceName", "displayName")
_IP_KEYS = ("ipAddress", "ip", "managementIpAddress", "managementIp", "primaryIp",
            "systemIp", "ipAddresses")
_MANUFACTURER_KEYS = ("manufacturer", "make", "vendor", "systemManufacturer",
                      "hardwareVendor")
_MODEL_KEYS = ("model", "systemModel", "productName", "product", "deviceModel",
               "hardwareModel", "modelName", "partNumber")
_TYPE_KEYS = ("resourceType", "deviceType", "nativeResourceType", "type",
              "resourceKind", "classCode")

_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def _first(document: dict, keys: Iterable[str]) -> str:
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list) and value:
            head = value[0]
            if isinstance(head, str) and head.strip():
                return head.strip()
            if isinstance(head, dict):
                for candidate in ("address", "ipAddress", "value", "name"):
                    sub = head.get(candidate)
                    if isinstance(sub, str) and sub.strip():
                        return sub.strip()
        if isinstance(value, dict):
            for candidate in ("name", "value", "address", "ipAddress"):
                sub = value.get(candidate)
                if isinstance(sub, str) and sub.strip():
                    return sub.strip()
    return ""


def _deep_first(flat: list[tuple[str, str, str]], key_names: Iterable[str]) -> str:
    wanted = {norm_text(k) for k in key_names}
    for _path, key, value in flat:
        if norm_text(key) in wanted and value:
            return value
    return ""


# Model and manufacturer are rarely on the resource document under the name we
# expect, so fall back to a term scan over every flattened key - the approach
# the reference extraction script uses.
_MODEL_TERMS = ("model", "product", "sku", "partnumber", "part number", "machine type")
_MODEL_ANTI_TERMS = (
    "version", "firmware", "software", "os", "serial", "number of", "count",
    "family", "license", "url", "id",
)
_MAKE_TERMS = ("manufacturer", "make", "vendor", "brand")
_MAKE_ANTI_TERMS = ("version", "firmware", "software", "id", "url")


def _deep_by_terms(
    flat: list[tuple[str, str, str]],
    terms: tuple[str, ...],
    anti_terms: tuple[str, ...],
) -> str:
    best = ""
    for _path, key, value in flat:
        if not value or len(value) > 120:
            continue
        normalized = norm_text(key)
        if not normalized:
            continue
        compact = normalized.replace(" ", "")
        if not any(term.replace(" ", "") in compact for term in terms):
            continue
        if any(anti.replace(" ", "") in compact for anti in anti_terms):
            continue
        if norm_text(value) in {"unknown", "null", "none", "n a", "other", "not detected"}:
            continue
        # An exact hit ("model") beats an incidental one ("chassis model name").
        if normalized in {norm_text(t) for t in terms}:
            return value
        if not best:
            best = value
    return best


def _find_ip(document: dict, flat: list[tuple[str, str, str]]) -> str:
    direct = _first(document, _IP_KEYS)
    if direct and _IPV4.search(direct):
        return _IPV4.search(direct).group(0)
    deep = _deep_first(flat, _IP_KEYS)
    if deep and _IPV4.search(deep):
        return _IPV4.search(deep).group(0)
    return direct or deep


def _resource_id(document: dict) -> str:
    for key in ("id", "uniqueId", "resourceUUID", "resourceId"):
        value = document.get(key)
        if value:
            return str(value)
    return ""


def build_asset(document: dict, tenant_name: str, tenant_id: str) -> tuple[Asset, list]:
    """Create the normalized asset record and the flattened attribute list."""
    flat = flatten(document)

    hostname = _first(document, _HOSTNAME_KEYS) or _deep_first(flat, _HOSTNAME_KEYS)
    manufacturer = (
        _first(document, _MANUFACTURER_KEYS)
        or _deep_first(flat, _MANUFACTURER_KEYS)
        or _deep_by_terms(flat, _MAKE_TERMS, _MAKE_ANTI_TERMS)
    )
    model = (
        _first(document, _MODEL_KEYS)
        or _deep_first(flat, _MODEL_KEYS)
        or _deep_by_terms(flat, _MODEL_TERMS, _MODEL_ANTI_TERMS)
    )
    resource_type = _first(document, ("resourceType", "type"))
    device_type = _first(document, ("deviceType",))
    native_type = _first(document, ("nativeResourceType",))
    resource_name = _first(document, ("resourceName", "name", "displayName"))

    # Tags / custom attributes often hold the platform label, so feed them in.
    tag_text = " ".join(
        value
        for path, key, value in flat
        if value
        and any(
            marker in norm_text(path)
            for marker in ("tag", "customattribute", "attributes", "category", "class")
        )
    )[:2000]

    # Two rounds on purpose: the hostname is the weakest signal, so it is only
    # consulted when nothing more authoritative identified the platform.
    # Most authoritative first. The resource's own type says what OpsRamp
    # thinks it is; the model says what the hardware is; tags come last
    # because they mention every product that touches the machine.
    strong = (
        device_type,
        resource_type,
        native_type,
        model,
        resource_name,
        manufacturer,
        document.get("description"),
        tag_text,
    )
    platform_key, platform_name, hit = detect_platform(*strong)
    if not platform_key:
        platform_key, platform_name, hit = detect_platform(*strong, hostname)
    category = PLATFORM_CATEGORY.get(platform_key or "", "")

    asset = Asset(
        tenant=tenant_name,
        tenant_id=tenant_id,
        asset_id=_resource_id(document),
        hostname=hostname,
        ip_address=_find_ip(document, flat),
        manufacturer=manufacturer,
        platform=platform_name or (manufacturer or ""),
        platform_key=platform_key or "",
        model=model,
        model_key=normalize_model(model),
        category=category,
        resource_name=resource_name,
        resource_type=resource_type or device_type,
        native_resource_type=native_type,
        errors=list(document.get("_extraction_errors") or []),
    )
    if hit:
        asset.errors = asset.errors  # platform hit recorded below in raw data
    return asset, flat


# Sub-components that OpsRamp returns as resources in their own right. They
# carry no firmware of their own and only add noise (and API calls).
_IGNORE_EXACT_TYPES = {
    "port", "volume", "interface", "disk", "cpu", "memory", "fan", "sensor",
    "service", "datastore", "group", "synthetic", "vi_cluster", "vi_datastore",
    "data_center", "cluster", "logical_volume", "power_supply",
    # Account and container objects, not hardware. A CLOUD_PROVIDER resource
    # has no model at all and was landing in the storage category as a row
    # with nothing in it.
    "cloud_provider", "cloud_account", "resource_group", "subscription",
    "vi_resource_pool", "vi_folder", "vi_vapp", "network", "namespace",
    # "Other" is OpsRamp's catch-all, and what lands in it is parts: DIMMs,
    # processors, network adapters, HBAs, drive backplanes, and entries whose
    # "model" is a firmware version. Across a real estate of fifteen accounts,
    # 564 components came from resources typed this way and not one of them
    # could be scored - no recipe names a DIMM. Left in they are counted as
    # assets, drag every percentage down and fill the pending count.
    "other",
    # Container and management objects, not hardware.
    "docker_container", "hpeoneview_resource",
    # A LUN is a slice of an array's capacity, not a thing with firmware. An
    # array presents hundreds of them, and each one was being counted as a
    # storage asset whose OS version could not be read - 360 rows across this
    # estate, which is most of everything the report could not score. The
    # array itself is a separate resource and is read as one.
    "storage_array_lun", "lun", "storage_array_pool",
}
_IGNORE_NAME_PARTS = ("logical", "empty bay")

# Of the types above, the ones no name can argue with. A container, a cloud
# account, a datastore or a LUN is not a piece of equipment whatever it is
# called; a "port" or a "disk", on the other hand, is sometimes how OpsRamp
# types a switch or an array, so those stay open to the name rescue.
_NEVER_HARDWARE_TYPES = {
    "docker_container", "cloud_provider", "cloud_account", "resource_group",
    "subscription", "namespace", "synthetic", "vi_folder", "vi_vapp",
    "vi_resource_pool", "data_center", "cluster", "vi_cluster", "datastore",
    "vi_datastore", "volume", "logical_volume",
    "storage_array_lun", "lun", "storage_array_pool",
}
# Drive cages/enclosures. They publish a FirmwareVersion of their own, but it
# is the enclosure's, not the array's OS version, and the operator does not
# track it - so they are skipped rather than reported against the wrong target.
_CAGE_NAME_RE = re.compile(r"(?<![a-z0-9])cage\s*\d*(?![a-z0-9])")
# Guest VMs are not hypervisors and expose no firmware of their own. Left in,
# they land in the ESXi category (their tags mention VMware) and report
# VERSION NOT DETECTED for an ESXi build they will never have.
_GUEST_MODELS = ("virtualmachine", "virtual machine", "vmware virtual platform")
# VMware also reports a guest's virtual hardware version as its model -
# "VMware7,1", "VMware20,1" - which normalises to "vmware7 1". One such guest
# was landing in the ESXi category with its antivirus product version read as
# the ESXi build it will never have.
_GUEST_MODEL_RE = re.compile(r"^vmware\d+ \d+$")
# ...unless the name says it is one of these, which are real assets.
_KEEP_NAME_PARTS = ("pdu", "switch", "ilo")

# A guest operating system, named by OpsRamp in the resource's type or its
# osName. On its own this proves nothing - an ESXi host runs an OS too - so it
# only counts when the resource has no hardware model to speak of. An NSX edge
# node is an Ubuntu VM with a model of "Other" and a tag reading
# "vmware_pool=VCF-edge...", and that tag was enough to file it as an ESXi
# host: 31 of them, each contributing a row whose ESXi build could not be read
# because it does not have one.
_GUEST_OS_RE = re.compile(
    r"(?<![a-z])(ubuntu|linux|windows|freebsd|centos|rhel|red hat|suse|debian"
    r"|photon|solaris)(?![a-z])"
)
# Nothing that identifies hardware. "Other" is OpsRamp's placeholder and
# arrives in the model field of everything it cannot identify.
_VAGUE_MODELS = {"", "other", "unknown", "none", "n a", "na", "not specified"}

# Parts that live inside a machine the tool already tracks. OpsRamp types them
# "Power" and "Storage", so left in they land in the PDU and Storage categories
# where they are neither a rack PDU nor an array: a server power supply beside
# real PDUs, a drive backplane and individual SSDs beside real arrays. No
# recipe names them, so they cannot be scored, and a whole category then reads
# as unscored because of parts nobody tracks the firmware of.
#
# The names are matched after normalisation, which turns punctuation into
# spaces: "rack1/chassis_u1/psu0" arrives as "rack1 chassis u1 psu0".
_PART_NAME_RE = re.compile(
    r"(?<![a-z0-9])(psu|powersupply|power supply|backplane|riser|dimm|"
    r"drive bay|fan)\s*\d*(?![a-z0-9])"
)
# "960GB SATA SSD", "1.92TB NVMe" - one drive, named after its capacity.
_DRIVE_NAME_RE = re.compile(
    r"^\d+(?:\s\d+)?\s*[gt]b(?![a-z0-9]).*"
    r"(?<![a-z0-9])(ssd|hdd|nvme|sas|sata|hard drive)(?![a-z0-9])"
)
# "1:11", "41:2" - an enclosure and slot, which is how an array names the
# drives inside it. Matched against the raw name, before normalisation turns
# the colon into a space: "192.0.2.201" would otherwise look the same, and
# that is a real PDU.
_SLOT_NAME_RE = re.compile(r"^\s*\d{1,3}\s*:\s*\d{1,3}\s*$")
_PART_MODEL_RE = re.compile(
    r"(?<![a-z0-9])(crps|common redundant power supply|power supply|backplane)"
    r"(?![a-z0-9])"
)


def is_noise_resource(document: dict) -> bool:
    """True for sub-component resources that should never be scanned."""
    device_type = norm_text(
        document.get("deviceType") or document.get("resourceType") or ""
    ).replace(" ", "_")
    name = norm_text(
        document.get("resourceName") or document.get("hostName") or
        document.get("name") or ""
    )

    # Two tiers, because the name rescue below cuts both ways. OpsRamp
    # sometimes types a real switch "port", and its name is what saves it -
    # but a container called "pdu_metrics" is a container however it is named,
    # and that rescue was filing it as a power strip whose firmware could not
    # be read. So the types that are never hardware are settled first, and
    # only the types that could be a mis-typing are left to the name.
    if device_type in _NEVER_HARDWARE_TYPES:
        return True

    if any(keep in name for keep in _KEEP_NAME_PARTS):
        return False

    model = norm_text(_first(document, _MODEL_KEYS))

    general = document.get("generalInfo") or {}
    if norm_text(model) in _VAGUE_MODELS:
        os_text = norm_text(
            document.get("osName") or general.get("osName") or ""
        )
        type_text = norm_text(general.get("resourceType") or "")
        if _GUEST_OS_RE.search(os_text) or _GUEST_OS_RE.search(type_text):
            return True
    if model and (
        any(guest in model for guest in _GUEST_MODELS)
        or _GUEST_MODEL_RE.match(model)
    ):
        return True

    if _CAGE_NAME_RE.search(name):
        return True

    # Every name the resource carries, not just the first one present: a part
    # often has an id for its resourceName and its position for its hostName.
    for candidate in (name, norm_text(document.get("hostName")),
                      norm_text(document.get("name"))):
        if candidate and (
            _PART_NAME_RE.search(candidate) or _DRIVE_NAME_RE.search(candidate)
        ):
            return True
    if model and _PART_MODEL_RE.search(model):
        return True
    for raw in (document.get("resourceName"), document.get("hostName"),
                document.get("name")):
        if raw and _SLOT_NAME_RE.match(str(raw)):
            return True

    if device_type in _IGNORE_EXACT_TYPES:
        return True
    return any(part in name for part in _IGNORE_NAME_PARTS)


def matches_category(asset: Asset, selected) -> bool:
    """Category gate. Accepts one category or several; ``all`` accepts any."""
    if not asset.platform_key:
        return False
    if not selected:
        return True
    wanted = {selected} if isinstance(selected, str) else set(selected)
    if not wanted or CATEGORY_ALL in wanted:
        return True
    return asset.category in wanted


# ------------------------------------------------------------------- scoring

_EXCLUDE_CACHE = {
    key: [norm_text(word) for word in meta.get("exclude", [])]
    for key, meta in COMPONENTS.items()
}
_ALIAS_CACHE = {
    key: sorted({norm_text(a) for a in meta["aliases"]}, key=len, reverse=True)
    for key, meta in COMPONENTS.items()
}
_BUILD_ALIAS_CACHE = sorted({norm_text(a) for a in BUILD_ALIASES}, key=len, reverse=True)


# Two lowercase characters before the capital, so "iLO" and "iSUT" stay whole
# while "firmwareVersion" splits.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9]{2})(?=[A-Z])")


def split_camel(text: str) -> str:
    """"firmwareVersion" -> "firmware Version", but "iLO 6" stays "iLO 6".

    OpsRamp mixes camelCase keys ("firmwareVersion", "biosVersion", "osName")
    with spaced tag names ("System ROM_SystemRomActive"). Without this split
    the two-word aliases never match the camelCase form.
    """
    return _CAMEL_RE.sub(" ", text or "")


def _word_in(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def score_attribute(
    component_key: str, key: str, path: str, platform_key: str | None = None
) -> int:
    """How strongly does this attribute name indicate ``component_key``?

    ``platform_key`` admits platform-specific spellings - on Superdome Flex the
    controller release arrives in a tag named "Bios Version".
    """
    nk = norm_text(split_camel(key))
    npath = norm_text(
        split_camel(path.replace(".", " ").replace("[", " ").replace("]", " "))
    )
    if not nk and not npath:
        return 0

    # A platform-specific alias is a deliberate, evidence-based statement about
    # that hardware, so it outranks the generic exclusions. On Superdome Flex
    # "Bios Version" carries the RMC release even though RMC normally excludes
    # anything mentioning BIOS.
    for alias in platform_component_aliases(platform_key, component_key):
        normalized = norm_text(alias)
        if normalized and (nk == normalized or _word_in(normalized, nk)):
            return 120 + min(len(normalized), 24)

    for bad in _EXCLUDE_CACHE.get(component_key, []):
        if _word_in(bad, nk):
            return 0

    best = 0
    for alias in _ALIAS_CACHE[component_key]:
        bonus = min(len(alias), 24)
        if nk == alias:
            best = max(best, 100 + bonus)
            continue
        if _word_in(alias, nk):
            # Penalise long names that only partly concern this component.
            slack = max(0, len(nk) - len(alias))
            best = max(best, 78 + bonus - min(slack, 20))
            continue
        if _word_in(alias, npath):
            best = max(best, 50 + bonus - 10)
    return best


def score_build_attribute(key: str, path: str) -> int:
    nk = norm_text(split_camel(key))
    npath = norm_text(split_camel(path.replace(".", " ")))
    best = 0
    for alias in _BUILD_ALIAS_CACHE:
        if nk == alias:
            best = max(best, 100)
        elif _word_in(alias, nk):
            best = max(best, 80)
        elif _word_in(alias, npath):
            best = max(best, 45)
    return best


# ------------------------------------------------------- value -> version text

_PREFIXED_RE = re.compile(r"\b([A-Za-z]{1,4}\d{1,4}[A-Za-z]?)\s+v?(\d+(?:\.\d+)+)")
# Keep vendor suffixes attached to the number: Brocade reports "9.2.2c1" and
# an HPE PDU reports "2.0.0.U". Dropping them loses real precision.
_SUFFIXED_RE = re.compile(r"\bv?(\d+(?:\.\d+)+(?:[a-z]\d{0,2}|\.[A-Z]))(?![\w.])")
_DOTTED_RE = re.compile(r"\bv?(\d+(?:\.\d+)+)")
_BARE_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)?)\s*$")
_ESXI_RE = re.compile(r"\besxi?\b[^0-9]{0,12}(\d+(?:\.\d+)+)", re.I)
_BUILD_IN_VALUE_RE = re.compile(r"\b(?:build|bld)[\s\-_:#]*(\d{3,})", re.I)
_TRAILING_REV_RE = re.compile(r"^(\d+(?:\.\d+)+)\s+([A-Za-z]\d{1,3})\b")


def extract_version_text(component_key: str, value: str) -> str | None:
    """Pull the version substring out of a free-form attribute value."""
    text = (value or "").strip()
    if not text:
        return None

    if component_key == "esxi":
        match = _ESXI_RE.search(text)
        if match:
            return match.group(1)

    match = _PREFIXED_RE.search(text)
    if match:
        # Keep the vendor's own spelling ("U54 v2.10") so the report shows what
        # OpsRamp actually reported; the comparator normalizes it later.
        return re.sub(r"\s+", " ", match.group(0)).strip()

    match = _SUFFIXED_RE.search(text)
    if match:
        return match.group(1)

    match = _DOTTED_RE.search(text)
    if match:
        candidate = match.group(1)
        tail = _TRAILING_REV_RE.match(text.strip())
        if tail and tail.group(1) == candidate:
            return f"{candidate} {tail.group(2)}"
        return candidate

    match = _BARE_RE.match(text)
    if match:
        return match.group(1)
    return None


def extract_build_text(value: str) -> str | None:
    match = _BUILD_IN_VALUE_RE.search(value or "")
    if match:
        return match.group(1)
    stripped = (value or "").strip()
    if re.fullmatch(r"\d{4,}", stripped):
        return stripped
    return None


# ------------------------------------------------------------------ extraction


def extract_components(
    asset: Asset, flat: list[tuple[str, str, str]]
) -> tuple[list[InstalledComponent], list[RawAttribute]]:
    """Extract every expected component for ``asset``."""
    wanted = components_for_platform(asset.platform_key)
    components: list[InstalledComponent] = []
    raw_records: list[RawAttribute] = []

    build_value = _best_build(flat)

    for component_key in wanted:
        candidates: list[tuple[int, str, str, str]] = []  # score, path, key, version
        for path, key, value in flat:
            if not value:
                continue
            score = score_attribute(component_key, key, path, asset.platform_key)
            if score <= 0:
                continue
            # A score of 78+ means the attribute's own name identifies this
            # component, so an IP-shaped value is trusted as a version.
            if not looks_like_version(value, trust_name=score >= 78):
                continue
            version = extract_version_text(component_key, value)
            if not version:
                continue
            candidates.append((score, path, value, version))

        candidates.sort(key=lambda item: (-item[0], len(item[1]), item[1]))

        component = InstalledComponent(
            asset=asset,
            component_key=component_key,
            component=component_display(component_key),
        )

        if candidates:
            score, path, value, version = candidates[0]
            component.installed_version = version
            component.source_attribute = path
            component.source_value = value
            component.confidence = score
            inline_build = extract_build_text(value)
            if component_key == "esxi":
                component.installed_build = inline_build or build_value or None
            elif inline_build and inline_build != version:
                component.installed_build = inline_build
        else:
            component.detection_note = (
                "No attribute matching this component's alias list contained a "
                "recognisable version value."
            )
            if asset.errors:
                component.detection_note += " Detail retrieval reported: " + "; ".join(
                    asset.errors
                )

        components.append(component)

        for index, (score, path, value, version) in enumerate(
            candidates[:MAX_RAW_PER_COMPONENT]
        ):
            raw_records.append(
                RawAttribute(
                    tenant=asset.tenant,
                    asset_id=asset.asset_id,
                    hostname=asset.hostname,
                    ip_address=asset.ip_address,
                    resource_name=asset.resource_name,
                    resource_type=asset.resource_type,
                    manufacturer=asset.manufacturer,
                    model=asset.model,
                    category=asset.category,
                    component=component.component,
                    raw_attribute_name=path,
                    raw_attribute_value=value,
                    installed_version=version,
                    selected=index == 0,
                    score=score,
                )
            )

        if not candidates:
            raw_records.append(
                RawAttribute(
                    tenant=asset.tenant,
                    asset_id=asset.asset_id,
                    hostname=asset.hostname,
                    ip_address=asset.ip_address,
                    resource_name=asset.resource_name,
                    resource_type=asset.resource_type,
                    manufacturer=asset.manufacturer,
                    model=asset.model,
                    category=asset.category,
                    component=component.component,
                    raw_attribute_name="(none)",
                    raw_attribute_value=component.detection_note,
                    installed_version="",
                    selected=False,
                    score=0,
                )
            )
            logger.warning(
                "Firmware version not detected for asset %s component %s",
                asset.asset_id or asset.hostname,
                component.component,
            )

    return components, raw_records


def _best_build(flat: list[tuple[str, str, str]]) -> str | None:
    best_score = 0
    best_value: str | None = None
    for path, key, value in flat:
        if not value:
            continue
        score = score_build_attribute(key, path)
        if score <= best_score:
            continue
        build = extract_build_text(value)
        if build:
            best_score = score
            best_value = build
    return best_value


def process_resource(
    document: dict, tenant_name: str, tenant_id: str, selected_category: str
) -> tuple[Asset | None, list[InstalledComponent], list[RawAttribute]]:
    """Full pipeline for one OpsRamp resource document.

    The noise gate is applied here as well as in the caller, so that a
    sub-component resource can never produce comparison rows by some other
    route: one place decides what is a real asset.
    """
    if is_noise_resource(document):
        return None, [], []
    asset, flat = build_asset(document, tenant_name, tenant_id)
    if not matches_category(asset, selected_category):
        return None, [], []
    components, raws = extract_components(asset, flat)

    # An asset that only matched the catch-all "server" pattern, has no model,
    # and exposed no firmware at all tells nobody anything: it produces a BIOS
    # and an iLO row with every column empty. Those rows were the bulk of the
    # noise in the server category. A generic asset that did yield a version,
    # or that at least has a model to match a recipe row by, is still kept.
    if (
        asset.platform_key in GENERIC_PLATFORMS
        and not asset.model
        and not any(c.detected for c in components)
    ):
        logger.info(
            "Skipping unidentified asset %s: no model and no firmware detected",
            asset.asset_id or asset.hostname or "(unnamed)",
        )
        return None, [], []

    return asset, components, raws


def summarize_platform(asset: Asset) -> str:
    return platform_display(asset.platform_key, asset.platform)


def asset_model_tokens(asset: Asset) -> tuple[str, ...]:
    return model_tokens(asset.model or asset.resource_name)
