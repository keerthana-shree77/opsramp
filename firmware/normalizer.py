"""Canonicalisation of categories, platforms, models and component names.

Both axes go through this module, so an asset discovered in OpsRamp and a row
typed by hand into a recipe spreadsheet end up expressed in exactly the same
vocabulary.  Everything here is pure text handling - no I/O, no API calls -
which is what makes it cheap to unit-test.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

# --------------------------------------------------------------------- basics

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


# The four functions below are pure functions of a string and are called on
# the same handful of model strings tens of thousands of times in one sweep -
# once per recipe entry per component. Memoising them is most of the
# comparison cost. Every return value is immutable, so a cached one cannot be
# altered by a caller.
@lru_cache(maxsize=16384)
def _norm_str(text: str) -> str:
    text = text.strip().lower()
    text = _NON_ALNUM.sub(' ', text)
    return _WS.sub(' ', text).strip()


def norm_text(value: object) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    if value is None:
        return ''
    return _norm_str(value if isinstance(value, str) else str(value))


def slug(value: object) -> str:
    """``"SD Flex 280"`` -> ``"sd_flex_280"``."""
    return norm_text(value).replace(" ", "_")


# ------------------------------------------------------------------ categories

CATEGORY_ALL = "all"
CATEGORY_SERVER = "server"
CATEGORY_SWITCH = "switch"
CATEGORY_STORAGE = "storage"
CATEGORY_ESXI = "esxi"
CATEGORY_PDU = "pdu"

SELECTABLE_CATEGORIES = [
    (CATEGORY_ALL, "All"),
    (CATEGORY_SERVER, "Server"),
    (CATEGORY_SWITCH, "Switch"),
    (CATEGORY_STORAGE, "Storage"),
    (CATEGORY_ESXI, "ESXi / VMware"),
    (CATEGORY_PDU, "PDU / Power"),
]

CATEGORY_DISPLAY = {
    CATEGORY_ALL: "All",
    CATEGORY_SERVER: "Server",
    CATEGORY_SWITCH: "Switch",
    CATEGORY_STORAGE: "Storage",
    CATEGORY_ESXI: "ESXi",
    CATEGORY_PDU: "PDU",
}

_CATEGORY_ALIASES = {
    "all": CATEGORY_ALL,
    "any": CATEGORY_ALL,
    "server": CATEGORY_SERVER,
    "servers": CATEGORY_SERVER,
    "compute": CATEGORY_SERVER,
    "host": CATEGORY_SERVER,
    "switch": CATEGORY_SWITCH,
    "switches": CATEGORY_SWITCH,
    "network": CATEGORY_SWITCH,
    "networking": CATEGORY_SWITCH,
    "storage": CATEGORY_STORAGE,
    "san": CATEGORY_STORAGE,
    "array": CATEGORY_STORAGE,
    "esxi": CATEGORY_ESXI,
    "esx": CATEGORY_ESXI,
    "vmware": CATEGORY_ESXI,
    "vmware esxi": CATEGORY_ESXI,
    "esxi vmware": CATEGORY_ESXI,
    "hypervisor": CATEGORY_ESXI,
    "vsphere": CATEGORY_ESXI,
    "pdu": CATEGORY_PDU,
    "pdus": CATEGORY_PDU,
    "power": CATEGORY_PDU,
    "power distribution": CATEGORY_PDU,
    "rack pdu": CATEGORY_PDU,
    "ups": CATEGORY_PDU,
}


def normalize_category(value: object) -> str | None:
    text = norm_text(value)
    if not text:
        return None
    if text in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[text]
    for token in text.split():
        if token in _CATEGORY_ALIASES:
            return _CATEGORY_ALIASES[token]
    return None


# ------------------------------------------------------------------- platforms
# Ordered most-specific first: the first pattern that fires wins.

PLATFORM_PATTERNS: list[tuple[str, str, list[str]]] = [
    (
        "superdome_flex_280",
        "Superdome Flex 280",
        [r"\bsuperdome\s*flex\s*280\b", r"\bsd\s*flex\s*280\b", r"\bsdflex280\b"],
    ),
    (
        "csus_3200",
        "Compute Scale-up Server 3200",
        [
            r"\bcompute\s*scale\s*up\s*server\s*3200\b",
            r"\bcsus\s*3200\b",
            r"\bcsus3200\b",
            r"\bscale\s*up\s*server\s*3200\b",
            # One issue heads the section "SUS3200 Details". Not a guess:
            # the rows beneath it name the machine in full and carry its
            # bundle, "bp-CSUS3200-2024-04-03-0A". Without it the whole
            # section is dropped and the RMC target goes with it.
            r"\bsus\s*3200\b",
        ],
    ),
    (
        "superdome_flex",
        "Superdome Flex",
        [r"\bsuperdome\s*flex\b", r"\bsd\s*flex\b", r"\bsdflex\b", r"\bsuperdome\b"],
    ),
    (
        "csus",
        "Compute Scale-up Server",
        [r"\bcompute\s*scale\s*up\s*server\b", r"\bcsus\b"],
    ),
    (
        "vmware_esxi",
        "VMware",
        # Deliberately strict: a bare "esx" inside a hostname must not turn a
        # bare-metal server into a hypervisor.
        [
            r"\besxi\b",
            r"\bvmware\b",
            # OpsRamp writes these as single tokens: VMWAREHOST, ESXIHOST.
            r"\bvmwarehost\b",
            r"\besxi?host\b",
            r"\bvsphere\b",
            r"\bhostsystem\b",
            r"\bvcenter\s*managed\s*host\b",
        ],
    ),
    (
        "hpe_dl",
        "HPE DL",
        [
            r"\bproliant\b",
            r"\bdl\s?\d{2,3}\s*gen\s?\d+\b",
            r"\bdl\s?\d{3}\b",
            r"\bml\s?\d{3}\b",
            r"\bbl\s?\d{3}\b",
        ],
    ),
    (
        # Checked before switch/storage: a PDU's deviceType is "Power" and its
        # model is a part number such as P9R53A.
        "pdu",
        "PDU",
        [
            r"\bpdu\b",
            r"\bpower\s*distribution\b",
            r"\bp9r\d{2}[a-z]\b",
            r"\brack\s*pdu\b",
            r"\bups\b",
            r"^power$",
            r"\bpower\s*outlet\b",
        ],
    ),
    (
        # Alletra 4000-series are ProLiant-based storage *servers*: the vendor
        # recipe lists BIOS, SPP and SPS for them, so they are servers here.
        "alletra_server",
        "HPE Alletra Server",
        [r"\balletra\s*4\d{3}\b", r"\balletra\s*storage\s*server\b"],
    ),
    (
        # Before storage: a label such as "HPE Storage Fibre Channel Switch
        # B-series SN6700B" names a switch, and "switch" is the more specific
        # of the two words.
        "switch",
        "Switch",
        [
            r"\bswitch\b",
            r"\baruba\b",
            r"\bnexus\b",
            r"\bcatalyst\b",
            r"\bbrocade\b",
            r"\bmellanox\b",
            r"\bcumulus\b",
            r"\bnx\s*os\b",
            r"\bios\s*xe\b",
            r"\bjuniper\b",
            r"\bjunos\b",
            r"\bprocurve\b",
            r"\bcomware\b",
            r"\bstorefabric\b",
            r"\bsn\d{4}[a-z]?\b",
            r"\bfabric\s*interconnect\b",
            r"\bfabric\s*composer\b",
        ],
    ),
    (
        "storage",
        "Storage",
        [
            r"\bstorage\b",
            r"\b3par\b",
            r"\bprimera\b",
            r"\balletra\b",
            r"\bnimble\b",
            r"\bstoreserv\b",
            r"\bstoreonce\b",
            r"\bnetapp\b",
            r"\bontap\b",
            r"\bhitachi\b",
            r"\bvsp\b",
            r"\bpowerstore\b",
            r"\bpowermax\b",
            r"\bunity\b",
            r"\bisilon\b",
            r"\bmsa\b",
            r"\bxp7\b",
            r"\bpure\s*storage\b",
            r"\bflasharray\b",
        ],
    ),
    (
        "server_generic",
        "Server",
        [r"\bserver\b", r"\bilo\b", r"\bsynergy\b", r"\bblade\b", r"\brack\s*mount\b"],
    ),
]

PLATFORM_DISPLAY = {key: display for key, display, _ in PLATFORM_PATTERNS}

PLATFORM_CATEGORY = {
    "alletra_server": CATEGORY_SERVER,
    "superdome_flex_280": CATEGORY_SERVER,
    "superdome_flex": CATEGORY_SERVER,
    "csus_3200": CATEGORY_SERVER,
    "csus": CATEGORY_SERVER,
    "hpe_dl": CATEGORY_SERVER,
    "server_generic": CATEGORY_SERVER,
    "vmware_esxi": CATEGORY_ESXI,
    "switch": CATEGORY_SWITCH,
    "storage": CATEGORY_STORAGE,
    "pdu": CATEGORY_PDU,
}

# Catch-alls: only used once no specific platform matched any fragment.
GENERIC_PLATFORMS = {"server_generic"}
_GENERIC_PLATFORMS = GENERIC_PLATFORMS  # internal alias

_COMPILED_PLATFORMS = [
    (key, display, [re.compile(p) for p in patterns])
    for key, display, patterns in PLATFORM_PATTERNS
]


def detect_platform(*fragments: object) -> tuple[str | None, str, str]:
    """Identify the platform from free-text fragments, in priority order.

    Fragments are examined **one at a time**, not as one blob: the first
    fragment that identifies a platform wins.  Callers therefore pass the most
    authoritative field first.  Joining everything instead lets an incidental
    mention decide - a ProLiant DL360's tags mention VMware, which would
    otherwise classify the server as a hypervisor and make the tool look for
    an ESXi build rather than its BIOS and iLO.

    Returns ``(platform_key, display_name, matched_text)``.
    """
    haystacks = [norm_text(f) for f in fragments if f not in (None, "")]
    haystacks = [h for h in haystacks if h]

    # Two passes. A catch-all like "SERVER" in a deviceType field must not
    # settle the platform while a model such as "ProLiant DL360 Gen11" is
    # still to be examined, or every server collapses to the generic profile
    # and only BIOS and iLO get looked for.
    for allow_generic in (False, True):
        for haystack in haystacks:
            for key, display, patterns in _COMPILED_PLATFORMS:
                if not allow_generic and key in _GENERIC_PLATFORMS:
                    continue
                for pattern in patterns:
                    match = pattern.search(haystack)
                    if match:
                        return key, display, match.group(0).strip()
    return None, "", ""


def normalize_platform(value: object) -> str | None:
    """Canonicalise a platform name written by a human into a recipe."""
    key, _display, _hit = detect_platform(value)
    return key


# ---------------------------------------------------------------------- models

_MODEL_NOISE = {
    "hpe",
    "hp",
    "hewlett",
    "packard",
    "enterprise",
    "proliant",
    "server",
    "system",
    "rack",
    "the",
    "inc",
    "corporation",
    "corp",
    "ltd",
    "compute",
}

_WILDCARD_MODELS = {"", "*", "any", "all", "n a", "na", "model", "generic", "family"}


# Precompiled: this ran seven uncompiled re.sub calls per invocation, each
# of which the regex module had to look up again.
_COLLAPSE_RULES = [
    (re.compile(r"\bgen\s+(\d+)\b"), r"gen\1"),
    (re.compile(r"\bg\s+(\d+)\b"), r"gen\1"),
    (re.compile(r"\bdl\s+(\d+)\b"), r"dl\1"),
    (re.compile(r"\bml\s+(\d+)\b"), r"ml\1"),
    (re.compile(r"\bbl\s+(\d+)\b"), r"bl\1"),
    (re.compile(r"\bsd\s+flex\b"), "sdflex"),
    (re.compile(r"\bsuperdome\s+flex\b"), "sdflex"),
]


@lru_cache(maxsize=16384)
def _collapse_gen(text: str) -> str:
    for pattern, replacement in _COLLAPSE_RULES:
        text = pattern.sub(replacement, text)
    return text


@lru_cache(maxsize=16384)
def _model_tokens_str(text: str) -> tuple:
    collapsed = _collapse_gen(_norm_str(text))
    return tuple(t for t in collapsed.split() if t and t not in _MODEL_NOISE)


def model_tokens(value: object) -> tuple[str, ...]:
    """Significant tokens of a model string, vendor noise removed."""
    if value is None:
        return ()
    return _model_tokens_str(value if isinstance(value, str) else str(value))


_MODEL_CODE_RE = re.compile(r"^[a-z]{0,3}\d{3,5}[a-z]?$")


@lru_cache(maxsize=16384)
def _model_codes_str(text: str) -> frozenset:
    return frozenset(
        token for token in _model_tokens_str(text) if _MODEL_CODE_RE.match(token)
    )


def model_codes(value: object) -> frozenset[str]:
    """The distinctive model codes in a model string.

    A vendor recipe writes "Aruba 6300M 48-port 1GbE and 4-port SFP6 Switch"
    while OpsRamp reports "6300M 48G (JL762A)". Neither token set contains the
    other, but both contain the code 6300M, which is what actually identifies
    the model.
    """
    if value is None:
        return frozenset()
    return _model_codes_str(value if isinstance(value, str) else str(value))


def normalize_model(value: object) -> str:
    """Canonical model key.  Wildcards collapse to an empty string."""
    text = norm_text(value)
    if text in _WILDCARD_MODELS:
        return ""
    return "_".join(model_tokens(value))


def is_wildcard_model(value: object) -> bool:
    return norm_text(value) in _WILDCARD_MODELS


# ------------------------------------------------------------------ components

COMPONENTS: dict[str, dict] = {
    "bios": {
        "display": "BIOS",
        "aliases": [
            "bios",
            "bios version",
            "bios firmware",
            "bios firmware version",
            "system bios",
            "system bios rom",
            "bios rom",
            "system rom",
            "system rom version",
            "rom version",
            "rom firmware",
            "firmware revision",
            "uefi",
            "uefi version",
        ],
        # Substrings that disqualify an otherwise-matching attribute.
        "exclude": [
            "date",
            # "Redundant System ROM_SystemRomBackup" is the standby copy, not
            # the running BIOS.
            "backup",
            "systemrombackup",
            "redundant",
            "vendor",
            "release date",
            "serial",
            "mode",
            "setting",
            "boot order",
            "language",
        ],
    },
    "ilo": {
        "display": "iLO",
        "aliases": [
            "ilo",
            "ilo version",
            "ilo firmware",
            "ilo firmware version",
            "ilo fw",
            "integrated lights out",
            "integrated lights out version",
            "integrated lights out firmware",
            "management processor firmware",
            "ilom",
            "bmc",
            "bmc firmware",
            "bmc version",
        ],
        "exclude": [
            "ip",
            "ipaddress",
            "ip address",
            "hostname",
            "mac",
            "license",
            "url",
            "port",
            "user",
            "password",
            "serial",
            "dns",
            "fqdn",
            "subnet",
            "gateway",
            # "HPE iLO Amplifier" is a separate management product, not iLO
            # firmware, and must never be read as one.
            "amplifier",
            "amplifier pack",
            # ESXi ships an iLO driver and the ilorest CLI; their versions
            # (800.10.9.1.4-1OEM...) are not the iLO firmware version.
            "driver",
            "ilorest",
            "rest",
            "tool",
            "tools",
            "agent",
        ],
    },
    "sps": {
        "display": "SPS",
        "aliases": [
            "sps",
            "sps version",
            "sps firmware",
            "sps firmware version",
            "server platform services",
            "server platform services firmware",
            "server platform services version",
            "me firmware",
            "management engine firmware",
        ],
        # A host can publish both "SPS Descriptor" (1.2) and the real
        # "SPSFirmwareVersionData" (4.1.5.201); the descriptor is not a
        # firmware version.
        "exclude": ["date", "serial", "mode", "descriptor"],
    },
    "intelligent_provisioning": {
        "display": "Intelligent Provisioning",
        "aliases": [
            "intelligent provisioning",
            "intelligent provisioning version",
            "intelligent provisioning firmware",
            "intelligent provisioning fw",
            "ip firmware",
            "ip version",
        ],
        "exclude": ["address", "ip address", "ipaddress", "date", "serial"],
    },
    "rmc": {
        "display": "RMC",
        "aliases": [
            "rmc",
            "rmc version",
            "rmc firmware",
            "rmc firmware version",
            "rmc fw",
            "resource management controller",
            "resource management controller firmware",
            "resource management controller version",
            "management controller firmware",
            "management controller version",
            # The vendor matrix reports the Superdome Flex / CSUS controller
            # release as COMPLEX_METADATA. The BMC:, EMMC: and *_FWU_TOOLS:
            # lines beside it are internal sub-builds of that release, not the
            # version anyone tracks, so they are excluded below.
            "complex metadata",
            "complex metadata version",
        ],
        "exclude": [
            "ip",
            "ip address",
            "ipaddress",
            "hostname",
            "mac",
            "url",
            "serial",
            "user",
            "port",
            "emmc",
            "fwu",
            "fwu tools",
            "tools",
            "iosp",
            "bundle",
            "bios",
        ],
    },
    "switch_firmware": {
        "display": "Switch Firmware",
        "aliases": [
            "switch firmware",
            "switch firmware version",
            "switch os",
            "switch os version",
            "network os version",
            "network os",
            "nos version",
            "firmware version",
            "firmware revision",
            "image version",
            "software version",
            "software image version",
            "ios version",
            "nxos version",
            "junos version",
            "os version",
            "firmware",
        ],
        "exclude": [
            "date",
            "boot",
            "backup",
            "rommon",
            "license",
            "serial",
            "config",
            "uptime",
        ],
    },
    "os_version": {
        "display": "OS Version",
        "aliases": [
            "os version",
            "operating system version",
            "operating system",
            "storage os version",
            "storage os",
            "array os version",
            "array os",
            "array software version",
            "controller software version",
            "software version",
            "firmware os revision",
            "os revision",
            "os release",
            "ontap version",
            "microcode version",
        ],
        # "installedApp.version" / "properties.appVersion" are the OpsRamp
        # agent's own version, not the array's.
        "exclude": [
            "date", "kernel build date", "license", "serial", "uptime",
            "app", "agent", "snmp", "metric", "installedapp",
        ],
    },
    "pdu_firmware": {
        "display": "PDU Firmware",
        "aliases": [
            "pdu firmware",
            "pdu firmware version",
            "firmware version",
            "firmware revision",
            "firmware",
            "pdu os",
            "management module firmware",
        ],
        "exclude": [
            "date", "serial", "license", "uptime", "snmp", "app", "agent",
            "metric", "outlet", "breaker",
        ],
    },
    "esxi": {
        "display": "ESXi",
        "aliases": [
            "esxi version",
            "esx version",
            "esxi",
            "vmware esxi",
            "vmware esxi version",
            "hypervisor version",
            "vsphere version",
            "product version",
            "host version",
            "os version",
            "full name",
            # OpsRamp reports the hypervisor build here:
            # osName = "VMware ESXi 8.0.3 build-24784735"
            "os name",
            "osname",
            "operating system name",
        ],
        "exclude": ["date", "license", "serial", "uptime", "vcenter", "tools"],
    },
}

# Attributes that specifically carry a build number.
BUILD_ALIASES = [
    "build",
    "build number",
    "esxi build",
    "esx build",
    "hypervisor build",
    "product build",
    "software build",
    "firmware build",
]

COMPONENT_DISPLAY = {key: meta["display"] for key, meta in COMPONENTS.items()}

# Extra spellings accepted from a recipe's Component column only (they are too
# ambiguous to be used when scanning arbitrary OpsRamp attribute names).
_RECIPE_ONLY_COMPONENT_ALIASES = {
    "ip": "intelligent_provisioning",
    "intelligent provision": "intelligent_provisioning",
    "sps fw": "sps",
    "ilo fw": "ilo",
    "switch fw": "switch_firmware",
    "firmware": "switch_firmware",
    "os": "os_version",
    "esx": "esxi",
    "esxi build": "esxi",
}

_COMPONENT_EXACT: dict[str, str] = {}
for _key, _meta in COMPONENTS.items():
    _COMPONENT_EXACT[norm_text(_key)] = _key
    for _alias in _meta["aliases"]:
        _COMPONENT_EXACT.setdefault(norm_text(_alias), _key)


def _fold_component_text(text: str) -> str:
    """Fold generation-suffixed spellings onto their base name.

    Vendor matrices write "ILO6 Firmware Version" and "iLO 5"; both mean iLO.
    """
    text = re.sub(r"\bilo\s*[0-9]\b", "ilo", text)
    text = re.sub(r"\bilom\s*[0-9]\b", "ilo", text)
    return text


def normalize_component(value: object, *, recipe_context: bool = False) -> str | None:
    """Canonicalise a component name.

    ``recipe_context`` widens the accepted spellings - a recipe's Component
    column is authored deliberately, so ``IP`` there really does mean
    Intelligent Provisioning, whereas ``ip`` found on an arbitrary OpsRamp
    attribute almost certainly means an IP address.
    """
    text = _fold_component_text(norm_text(value))
    if not text:
        return None
    if recipe_context and text in _RECIPE_ONLY_COMPONENT_ALIASES:
        return _RECIPE_ONLY_COMPONENT_ALIASES[text]
    if text in _COMPONENT_EXACT:
        return _COMPONENT_EXACT[text]

    # Longest alias contained in the text wins, so "ilo firmware version"
    # beats a bare "version".
    best: tuple[int, str] | None = None
    for alias, key in _COMPONENT_EXACT.items():
        if len(alias) < 3:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", text):
            if best is None or len(alias) > best[0]:
                best = (len(alias), key)
    if best:
        return best[1]
    return None


def normalize_component_for_platform(
    value: object, platform_key: str | None, *, recipe_context: bool = False
) -> str | None:
    """Resolve a component name in the context of a known platform.

    The same word means different things on different hardware - "BMC" is the
    RMC on a Superdome Flex and the iLO on a ProLiant - so the platform's own
    component list is consulted first, longest alias winning.  Falls back to
    the platform-agnostic lookup when nothing matches.
    """
    text = _fold_component_text(norm_text(value))
    if not text:
        return None

    best_key: str | None = None
    best_len = 0
    for key in components_for_platform(platform_key):
        meta = COMPONENTS.get(key)
        if not meta:
            continue
        if any(
            re.search(rf"(?<![a-z0-9]){re.escape(norm_text(bad))}(?![a-z0-9])", text)
            for bad in meta.get("exclude", [])
        ):
            continue
        for alias in list(meta["aliases"]) + [key.replace("_", " ")]:
            normalized = norm_text(alias)
            if not normalized:
                continue
            matched = text == normalized or re.search(
                rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text
            )
            if matched and len(normalized) > best_len:
                best_len = len(normalized)
                best_key = key
    if best_key:
        return best_key
    # When the platform declares a component list, that list is exhaustive:
    # falling back to the global lookup here would both bypass the platform's
    # exclusions ("RMC_EMMC" is not the RMC) and resolve names the platform
    # does not even have.
    if components_for_platform(platform_key):
        return None
    return normalize_component(value, recipe_context=recipe_context)


def component_display(key: str | None) -> str:
    if not key:
        return ""
    return COMPONENT_DISPLAY.get(key, key.replace("_", " ").title())


# --------------------------------------------- which components matter per platform

PLATFORM_COMPONENTS: dict[str, list[str]] = {
    # BIOS and SPS only. The vendor recipe states neither an Intelligent
    # Provisioning nor an iLO target for this platform, and the operator does
    # not track either on it - so its iLO is not looked for and not compared.
    "alletra_server": ["bios", "sps"],
    "superdome_flex_280": ["rmc"],
    "superdome_flex": ["rmc"],
    "csus_3200": ["rmc"],
    "csus": ["rmc"],
    "hpe_dl": ["bios", "ilo", "sps", "intelligent_provisioning"],
    "server_generic": ["bios", "ilo"],
    "switch": ["switch_firmware"],
    "storage": ["os_version"],
    "vmware_esxi": ["esxi"],
    "pdu": ["pdu_firmware"],
}


# Attribute names that mean a given component only on a given platform.
#
# Superdome Flex and CSUS publish their controller release through a tag named
# "Bios Version". It is not the BIOS: the vendor matrix lists the SD Flex BIOS
# as 8.160.2.2026xxxx, whereas this value (2.10.04) is in exactly the format
# the matrix uses for COMPLEX_METADATA / the RMC release (2.16.04). On these
# platforms it is the only firmware value OpsRamp exposes at all.
PLATFORM_COMPONENT_ALIASES: dict[tuple[str, str], list[str]] = {
    ("superdome_flex_280", "rmc"): ["bios version", "complex metadata", "bmc firmware"],
    ("superdome_flex", "rmc"): ["bios version", "complex metadata", "bmc firmware"],
    ("csus_3200", "rmc"): ["bios version", "complex metadata"],
    ("csus", "rmc"): ["bios version", "complex metadata"],
    # Prefer the System ROM tag over "bios.biosVersion". Both are correct, but
    # only the tag carries the ROM family ("U54 v2.80"), which is the form the
    # vendor recipe states its target in. "active" keeps this from matching
    # "Redundant System ROM_SystemRomBackup", the standby copy.
    ("hpe_dl", "bios"): ["system rom active"],
    ("alletra_server", "bios"): ["system rom active"],
    ("server_generic", "bios"): ["system rom active"],
    # Storage arrays publish their release as "System Base Version" (present
    # on both 3PAR and Alletra) and, on Alletra, also as osName ("9.6.5.19").
    # System Base Version is preferred: it is the granularity the recipe
    # states its target in, and it is the one attribute both families share.
    # A drive enclosure's plain "FirmwareVersion" is the last resort.
    ("storage", "os_version"): [
        "system base version",
        "systembaseversion",
        "os name",
        "osname",
        "firmware version",
        "firmwareversion",
    ],
    ("pdu", "pdu_firmware"): ["firmware version", "firmwareversion"],
    # Aruba switches report two different things:
    #   generalInfo.firmwareVersion = GL.01.16.0004   (boot image number)
    #   generalInfo.softwareVersion = GL.10.15.1020   (the AOS-CX release)
    # The recipe targets the AOS-CX release, so softwareVersion must win.
    # "firmware version" stays in the generic alias list as a fallback for
    # switches that publish only that (the Brocade FC switches report the same
    # value in both).
    ("switch", "switch_firmware"): ["software version", "softwareversion"],
}


def platform_component_aliases(platform_key: str | None, component_key: str) -> list[str]:
    if not platform_key:
        return []
    return PLATFORM_COMPONENT_ALIASES.get((platform_key, component_key), [])


def components_for_platform(platform_key: str | None) -> list[str]:
    if not platform_key:
        return []
    return list(PLATFORM_COMPONENTS.get(platform_key, []))


def platform_display(key: str | None, fallback: str = "") -> str:
    if not key:
        return fallback
    return PLATFORM_DISPLAY.get(key, key.replace("_", " ").title())


def iter_component_keys() -> Iterable[str]:
    return COMPONENTS.keys()
