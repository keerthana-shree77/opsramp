"""Reader for vendor "version matrix" workbooks.

HPE publishes the SAP RISE / S4HANA solution version matrices as PDFs. When
those are converted to Excel the result is not a table but a sequence of
sections::

    HPE ProLiant DL380 Gen11 Details        <- section heading
    Components            Recipe 2026.03    <- column header
    DL380 Gen11 System BIOS ROM   U54 v3.00 (08/20/2026)
    ILO6 Firmware Version         1.77
    Server Platform Services (SPS) Firmware  6.1.4.215.0
    Intelligent Provisioning      4.31.5

with continuation rows whose first cell is blank carrying sub-components::

                                  BIOS: 9.56.20.20260618_160844
                                  RMC_EMMC: 1.72.93-20260622_054645Z

This module turns that shape into the same flat rows the ordinary CSV loader
produces.  It deliberately reports what it could *not* interpret instead of
dropping it silently: an unnoticed omission in a firmware baseline is exactly
the failure mode this whole tool exists to prevent.
"""
from __future__ import annotations

import logging
import re

from firmware.normalizer import (
    CATEGORY_DISPLAY,
    PLATFORM_CATEGORY,
    component_display,
    components_for_platform,
    detect_platform,
    norm_text,
    normalize_component_for_platform,
)

logger = logging.getLogger(__name__)

# "Components" / "Component" with an optional recipe-label column beside it.
_HEADER_CELL = {"components", "component"}
_PAGE_MARKER = re.compile(r"^\s*page\s+\d+\s*[-–]\s*table\s+\d+\s*$", re.I)
_SECTION_SUFFIX = re.compile(r"\s+details\s*$", re.I)
_SUBCOMPONENT = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_ /\-]{1,40}?)\s*:\s*(\S.*)$")
_BUILD_IN_TEXT = re.compile(r"\bbuild[\s\-]*([0-9]{4,})", re.I)
_VENDOR_NOISE = re.compile(
    r"^\s*(hpe|hp|vmware)\s+", re.I
)


# A conversion of the same vendor PDFs may carry the source page number in the
# first column of every row, which shifts every other column one to the right.
_INDEX_HEADERS = {"pdf page", "page", "page no", "page number", "sr no", "s no"}

# A section header names its value columns: "Recipe 2026.02", "Target Version".
_VERSION_HEADER = re.compile(r"^\s*(recipe|target|approved|version)\b", re.I)
# ...but one issue labels its links column "Recipe 2025.01 links", which
# starts the same way and carries the same release. Read as the version
# column it makes every target a URL and the whole file is rejected. A
# column naming any of these is about the target, not the target itself.
_NOT_A_VERSION_HEADER = re.compile(
    r"\b(links?|urls?|dependenc(?:y|ies)|notes?|comments?|help|command)\b", re.I
)
# "Recipe 2025.02.1" is a later release than "Recipe 2025.02", so the patch
# part has to be read. Without it the two rank equal, and a document listing
# 2025.02 beside 2025.02.1 was measured against whichever came last in the
# row - which in one issue was the links column.
_RELEASE_LABEL = re.compile(r"(\d{4})[._\-/](\d{1,2})(?:[._\-/](\d{1,3}))?")


def _strip_index_column(table: list[list]) -> list[list]:
    """Drop a leading page-number column, if the sheet carries one.

    Newer conversions of these PDFs put the source page number in column A of
    every row. Left in place it is read as the component name and the real
    component name is read as the version, so nothing on the sheet is
    recognisable and the whole file is rejected.
    """
    if not table:
        return table

    first_label = ""
    for row in table:
        text = _text(row[0]) if row else ""
        if text:
            first_label = norm_text(text)
            break
    labelled = first_label in _INDEX_HEADERS

    numeric = populated = 0
    for row in table:
        if len(row) < 2 or not any(_text(c) for c in row[1:]):
            continue
        populated += 1
        if _text(row[0]).isdigit():
            numeric += 1
    numbered = populated >= 5 and numeric >= populated * 0.8

    if not (labelled or numbered):
        return table
    logger.info("Sheet carries a page-index column; reading from the second")
    return [list(row[1:]) for row in table]


def _release_key(cell: str) -> tuple[int, int, int]:
    """Rank a column label by the release it names.  ``(year, month, patch)``."""
    match = _RELEASE_LABEL.search(cell)
    if not match:
        return (0, 0, 0)
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)) if match.group(3) else 0,
    )


def _all_links(samples: list[list], column: int) -> bool:
    """True when every value this column actually holds is a link.

    One issue labels its links column exactly as it labels a version column -
    ``Recipe 2025.02`` for both - so the header alone cannot tell them apart.
    What it holds can: a column of URLs is not a column of versions, whatever
    it is called.
    """
    seen = 0
    for row in samples:
        if column >= len(row):
            continue
        value = _text(row[column])
        if not value:
            continue
        seen += 1
        if not is_a_link(value):
            return False
    return seen > 0


def _version_column(cells: list[str], samples: list[list] | None = None) -> int | None:
    """Which column of a section header holds the version to compare against.

    The vendor file lists the previous release beside the current one -
    ``Components | Help Command | Recipe 2026.01 | Recipe 2026.02 | Links`` -
    so "the first value after the component name" is no longer the answer: it
    is the help text, and failing that it is *last* month's version. Measuring
    an estate against a superseded baseline and reporting it as compliant is
    the most damaging thing this reader could do, so the column is chosen by
    its label, and the latest labelled release wins.

    ``samples`` are the rows beneath the header. They settle the case the
    label cannot: one issue heads its links column ``Recipe 2025.02``, the
    same words as a real version column, so it looked like the equal of one
    and - being furthest right - won. A column holding nothing but URLs is
    disqualified however it is labelled.
    """
    best: int | None = None
    best_key: tuple[int, int, int] | None = None
    for index, cell in enumerate(cells):
        if index == 0 or not _VERSION_HEADER.match(cell):
            continue
        if _NOT_A_VERSION_HEADER.search(cell):
            continue
        if samples and _all_links(samples, index):
            logger.info(
                "Column %d (%r) holds links, not versions; not reading targets "
                "from it", index, cell,
            )
            continue
        key = _release_key(cell)
        # ">=" so that among equally-labelled columns the rightmost wins,
        # which is the order these files are written in.
        if best_key is None or key >= best_key:
            best_key, best = key, index
    return best


def looks_like_matrix(table: list[list]) -> bool:
    """True when a sheet uses the sectioned "Components" layout."""
    for row in _strip_index_column(table)[:80]:
        cells = [_text(c) for c in row[:3]]
        if cells and norm_text(cells[0]) in _HEADER_CELL:
            return True
    return False


def _text(value) -> str:
    """Cell text with all whitespace collapsed to single spaces.

    Cells converted from the PDF carry embedded newlines: the value
    "COMPLEX_METADATA: 1.76.44-" and "20260622_072146" are one cell split over
    two lines. Left in, the sub-component pattern fails to match, because its
    trailing "$" cannot match in the middle of the string, and the row is then
    attributed to the previous component instead of being read as its own.
    """
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _is_section_heading(text: str) -> bool:
    """Any lone label starts a section.

    The vendor file heads sections with "HPE ProLiant DL380 Gen11 Details",
    "Infrastructure Details (Switch and Storage)" and bare words like "PDU" or
    "Windows Server", so the only reliable signal is a row with a label and no
    value beside it.
    """
    if not text:
        return False
    if _PAGE_MARKER.match(text):
        return False
    if norm_text(text) in _HEADER_CELL:
        return False
    return True


def _section_label(text: str) -> str:
    """"HPE ProLiant DL380 Gen11 Details" -> "HPE ProLiant DL380 Gen11"."""
    return _SECTION_SUFFIX.sub("", text).strip()


def _is_generic_section(label: str) -> bool:
    """"VMware Components" groups a product family, it does not name a model."""
    return norm_text(label).endswith("components")


def _model_from_section(label: str) -> str:
    """Strip the vendor word so "HPE ProLiant DL380 Gen11" -> "DL380 Gen11".

    A grouping heading such as "VMware Components" names no model at all, so
    it yields an empty model and the row becomes a platform-family rule.
    """
    if _is_generic_section(label):
        return ""
    cleaned = _VENDOR_NOISE.sub("", label).strip()
    cleaned = re.sub(r"^(proliant|compute)\s+", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s+server\s*$", "", cleaned, flags=re.I).strip()
    return cleaned or label


# Whatever column it arrives in, a link is not a version.
_LOOKS_LIKE_A_LINK = re.compile(r"^\s*(?:https?://|www\.|ftp://)", re.I)


def is_a_link(value: str) -> bool:
    return bool(_LOOKS_LIKE_A_LINK.match(_text(value)))


def split_version_and_build(value: str) -> tuple[str, str]:
    """"VMware ESXi 8.0 U3k (Build 25595708)" -> ("8.0 U3k", "25595708")."""
    text = _text(value)
    build = ""
    match = _BUILD_IN_TEXT.search(text)
    if match:
        build = match.group(1)
        text = (text[: match.start()] + " " + text[match.end() :])
    # Drop the bracket the build lived in, and any product-name prefix.
    text = re.sub(r"[()\[\]]", " ", text)
    text = re.sub(r"(?i)\b(vmware\s+esxi|vmware|esxi|version)\b", " ", text)
    # HPE appends the release date: "U54 v3.00 08/20/2026", "3.00_08-20-2026".
    text = re.sub(r"[\s_]+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s*$", "", text)
    text = re.sub(r"[\s_]+\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*$", "", text)
    # "3.20 Jun 02 2026"
    text = re.sub(
        r"(?i)[\s_]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
        r"\s+\d{1,2},?\s+\d{4}\s*$",
        "",
        text,
    )
    # "1.76.44- 20260622_072146" / "1.72.93-20260622_054645Z": a trailing
    # yyyymmdd stamp, optionally with a time, is a build date rather than part
    # of the version.
    text = re.sub(r"[-\s_]+\d{8}([_-]\d{6})?[a-zA-Z]?\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -–,;_")
    return text, build


def _headline_component(component: str, platform_key: str, section: str) -> str | None:
    """Resolve a section's headline firmware row.

    Superdome Flex and CSUS sections lead with a row named after the machine
    itself - "HPE Compute Scale-up Server 3200 Firmware" - whose value is the
    controller release. Where a platform tracks exactly one component, that
    headline row is that component.
    """
    allowed = components_for_platform(platform_key)
    if len(allowed) != 1:
        return None
    text = norm_text(component)
    if not text.endswith("firmware"):
        return None
    # "... IOSP Bundle Firmware" is a media bundle, not the controller.
    if any(word in text for word in ("bundle", "iosp", "tools", "emmc", "fwu")):
        return None
    # The row must be named after the machine, not after some other component
    # that merely ends in "Firmware". Without this, a "Server Platform
    # Services (SPS) Firmware" row inside a CSUS section would be recorded as
    # that machine's RMC target.
    section_tokens = {
        t for t in norm_text(_section_label(section)).split() if len(t) > 2
    } - {"hpe", "the", "server", "compute", "details"}
    row_tokens = {t for t in text.split() if len(t) > 2} - {"firmware"}
    if not section_tokens or not section_tokens & row_tokens:
        return None
    return allowed[0]


class MatrixRow:
    __slots__ = ("row_number", "sheet", "section", "component", "value", "reason")

    def __init__(self, row_number, sheet, section, component, value):
        self.row_number = row_number
        self.sheet = sheet
        self.section = section
        self.component = component
        self.value = value
        self.reason = ""


def _scan_sheet(
    sheet: str,
    table: list[list],
    version_col: int | None = None,
    section: str = "",
) -> tuple[list[MatrixRow], int | None, str]:
    """Walk one sheet, tracking the current section heading.

    Returns the rows found and the value column in force at the end, so the
    caller can carry it into the next sheet. A PDF is split into one sheet per
    page, and a table that runs over a page break does not repeat its header:
    without carrying the column across, everything after the break is read
    from whichever column happens to be filled - in practice the help text.
    """
    found: list[MatrixRow] = []
    last_component = ""

    for index, raw_row in enumerate(table, start=1):
        cells = [_text(c) for c in raw_row]
        first = cells[0] if cells else ""

        if norm_text(first) in _HEADER_CELL:
            # "Components | Recipe 2026.03" - the column header of a section.
            # Later sections repeat the header without naming the value
            # columns again, so a header that names none leaves the column
            # established by the first one in place.
            #
            # The rows beneath it come too: where two columns carry the same
            # label, what they hold is the only way to tell a version column
            # from a column of links.
            column = _version_column(cells, table[index : index + 12])
            if column is not None:
                version_col = column
            continue

        rest = [c for c in cells[1:] if c]
        if version_col is not None and version_col < len(cells):
            value = cells[version_col]
        elif version_col is None or len(rest) == 1:
            # Either no value column has been named yet, or the row is too
            # short to have one and offers a single candidate - so there is
            # nothing to choose wrongly between.
            value = rest[0] if rest else ""
        else:
            # Several candidates and no way to tell which release each is.
            # Guessing risks reading last month's version and reporting the
            # estate compliant against a superseded baseline, so the row is
            # left without a target and reported as such.
            value = ""

        if not first and not rest:
            continue
        if _PAGE_MARKER.match(first):
            continue

        # A heading stands alone. Testing the whole row rather than just the
        # chosen column matters now that a row can carry help text and links
        # beside a blank version - that is a component with no target this
        # release, not the start of a new section.
        if first and not rest:
            if _is_section_heading(first):
                section = _section_label(first)
                last_component = ""
            continue

        if first:
            found.append(MatrixRow(index, sheet, section, first, value))
            last_component = first
            continue

        # Continuation row: blank first cell, value in the second.
        sub = _SUBCOMPONENT.match(value)
        if sub:
            found.append(
                MatrixRow(index, sheet, section, sub.group(1).strip(), sub.group(2).strip())
            )
        elif last_component:
            row = MatrixRow(index, sheet, section, last_component, value)
            row.reason = (
                f"Additional value for {last_component!r}; the first value listed "
                "is the one used."
            )
            found.append(row)
    return found, version_col, section


_VENDOR_WORDS = (
    "hpe", "hp", "aruba", "brocade", "cisco", "vmware", "storefabric",
    "store", "fabric", "series", "b series",
)
_MODEL_CODE_RE = re.compile(r"^[a-z]{0,3}\d{3,5}[a-z]?$")

# A row in the infrastructure sections only names a *device* whose firmware
# this tool tracks. Management software, host add-ons, installable media and
# sub-assemblies mention the same vendor words but are not the device, and
# treating them as one produces confidently wrong targets - "VMware Tools"
# became an ESXi target, "HPE Add-ons for SuperdomeFlex" became an RMC target
# and an IOSP bundle filename became a version number.
_NOT_A_DEVICE = (
    # management and host software
    "tools", "add on", "add ons", "addons", "isut", "oneview", "vcenter",
    "nsx", "vsan", "console", "ssmc", "amplifier", "composer", "management",
    "edition", "vm ", " vm", "driver", "ilorest",
    # installable media
    "bundle", "iosp", "spp", "iso",
    # sub-assemblies and adapters
    # "adapt" also catches the source's "Adapater" typo.
    "hba", "adapt", "drive", "nvme", "battery", "energy pack", "cage",
    "cages", "sas", "sff", "ssd", "hdd", "microcode", "power supply",
    "tpm", "innovation engine",
    # the hypervisor itself is handled by its section, not as a model
    "vsphere", "esxi",
)


def _looks_like_a_device(label: str) -> bool:
    text = norm_text(label)
    if not text:
        return False
    return not any(word in text for word in _NOT_A_DEVICE)


def _device_model(label: str) -> str:
    """Model of a device row: "Aruba 8325-32C Switch" -> "8325-32C"."""
    text = _text(label)
    cleaned = re.sub(
        r"(?i)\b(" + "|".join(_VENDOR_WORDS) + r")\b", " ", text
    )
    cleaned = re.sub(r"(?i)\b(switch|pdu|storage|adapter|drive|server)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–,;")
    return cleaned or text


def _build_row(
    entry,
    sheet: str,
    platform_key: str,
    platform_name: str,
    component_key: str,
    model: str,
    seen: set,
    skipped: list[str],
    where: str,
) -> dict | None:
    if is_a_link(entry.value):
        skipped.append(
            f"{where}: {entry.component!r} has a link where a version should "
            "be - the value column of this sheet could not be identified."
        )
        return None
    version, build = split_version_and_build(entry.value)
    if not version and not build:
        skipped.append(f"{where}: {entry.component!r} has no usable version value.")
        return None
    if entry.reason:
        skipped.append(f"{where}: {entry.reason}")
        return None

    key = (platform_key, norm_text(model), component_key)
    if key in seen:
        skipped.append(
            f"{where}: duplicate target for {model or platform_name} / "
            f"{component_display(component_key)}; the first one is used."
        )
        return None
    seen.add(key)

    category = PLATFORM_CATEGORY.get(platform_key, "")
    return {
        "_row": entry.row_number,
        "_sheet": sheet,
        "_empty": False,
        "_raw": {
            "Sheet": sheet,
            "Section": entry.section,
            "Component": entry.component,
            "Value": entry.value,
        },
        "platform": platform_name,
        "model": model,
        "category": CATEGORY_DISPLAY.get(category, category),
        "component": component_display(component_key),
        "target_version": version,
        "target_build": build,
        "mandatory": "Yes",
        "applies_to_family": "Yes" if not model else "No",
        "notes": f"{sheet}: {entry.section} / {entry.component}",
    }


def parse_matrix(
    sheets: list[tuple[str, list[list]]], continuous: bool = False
) -> tuple[list[dict], list[str]]:
    """Convert matrix sheets into flat recipe rows.

    Returns ``(rows, skipped)`` where ``rows`` use the same canonical keys the
    CSV loader produces and ``skipped`` explains every line that was not
    turned into a target, so nothing disappears without a trace.

    ``continuous`` says the sheets are pages of one document rather than
    separate sheets of a workbook. A table that runs over a page break keeps
    neither its header nor its section heading, so both are carried forward.
    Between the sheets of a workbook they are not: a heading from one sheet
    has no authority over another, and attributing a row to the wrong machine
    is worse than reporting that it could not be attributed at all.
    """
    rows: list[dict] = []
    skipped: list[str] = []
    seen: set[tuple] = set()

    version_col: int | None = None
    section = ""
    for sheet, table in sheets:
        if not looks_like_matrix(table):
            continue
        entries, version_col, section = _scan_sheet(
            sheet,
            _strip_index_column(table),
            version_col,
            section if continuous else "",
        )
        for entry in entries:
            where = f"{sheet} row {entry.row_number}"

            if not entry.section:
                skipped.append(
                    f"{where}: {entry.component!r} appears before any platform "
                    "heading, so it cannot be attributed to a platform."
                )
                continue

            # Three row shapes exist. In the server sections a row names a
            # component of the section's machine. In "Infrastructure Details
            # (Switch and Storage)" and "PDU" each row names a *device* and
            # the value is that device's only tracked firmware.
            device_key, device_name, _d = detect_platform(entry.component)
            device_components = components_for_platform(device_key)
            if (
                device_key
                and len(device_components) == 1
                and _looks_like_a_device(entry.component)
            ):
                row = _build_row(
                    entry,
                    sheet,
                    device_key,
                    device_name,
                    device_components[0],
                    _device_model(entry.component),
                    seen,
                    skipped,
                    where,
                )
                if row:
                    rows.append(row)
                continue

            platform_key, platform_name, _hit = detect_platform(entry.section)
            if not platform_key:
                skipped.append(
                    f"{where}: platform {entry.section!r} is not a supported "
                    f"platform (component {entry.component!r})."
                )
                continue

            component_key = normalize_component_for_platform(
                entry.component, platform_key, recipe_context=True
            )
            if component_key is None:
                component_key = _headline_component(
                    entry.component, platform_key, entry.section
                )
            if not component_key:
                skipped.append(
                    f"{where}: {entry.component!r} is not a firmware component "
                    "this tool compares."
                )
                continue

            # A component only counts if the platform actually exposes it.
            # Without this, "Power Supply Firmware" under a DL server matches
            # the generic switch-firmware aliases and becomes a bogus target.
            allowed = components_for_platform(platform_key)
            if allowed and component_key not in allowed:
                skipped.append(
                    f"{where}: {entry.component!r} is not one of the components "
                    f"tracked for {platform_name} "
                    f"({', '.join(component_display(c) for c in allowed)})."
                )
                continue

            row = _build_row(
                entry,
                sheet,
                platform_key,
                platform_name,
                component_key,
                _model_from_section(entry.section),
                seen,
                skipped,
                where,
            )
            if row:
                rows.append(row)

    logger.info(
        "Matrix parse produced %d target(s); %d line(s) not used",
        len(rows),
        len(skipped),
    )
    return rows, skipped
