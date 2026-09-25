"""Continuation rows that arrive as one cell split over two lines.

A PDF-to-Excel conversion puts "COMPLEX_METADATA: 1.76.44-" and its timestamp
in a single cell separated by a newline. The sub-component pattern ends in
"$", which cannot match mid-string, so such a row used to be attributed to the
previous component - silently losing the CSUS controller target.
"""
import pytest
from openpyxl import Workbook

from recipe.loader import load_recipe
from recipe.matrix_loader import split_version_and_build
from recipe.validator import validate_recipe

ROWS = [
    ["HPE Compute Scale-up Server 3200 Details"],
    ["Components"],
    ["HPE Compute Scale-up Server 3200 IOSP Bundle", "bp-CSUS32xx-2026-03-06-0A"],
    ["HPE Compute Scale-up Server 3200 Firmware Bundle", "1.76.44"],
    ["", "FWU: 1.72.93-20260622_060922"],
    ["", "COMPLEX_METADATA: 1.76.44-\n20260622_072146"],
    ["", "RMC_EMMC: 1.72.93-20260622_054645Z"],
    ["", "BIOS: 9.56.20.20260618_160844"],
    ["", "RMC_FWU_TOOLS: 1.72.93-\n20260622_054645Z"],
]


@pytest.fixture
def entries(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "HPE_SAP_RISE_CDC1.5"
    for row in ROWS:
        sheet.append(row)
    path = tmp_path / "matrix.xlsx"
    workbook.save(path)
    workbook.close()
    report = validate_recipe(load_recipe(path))
    assert report.ok, [i.render() for i in report.errors]
    return {(e.platform_key, e.component_key): e for e in report.entries}


def test_csus_rmc_survives_a_newline_inside_the_cell(entries):
    assert entries[("csus_3200", "rmc")].target_version == "1.76.44"


def test_sub_builds_are_still_not_used_as_the_rmc_target(entries):
    """FWU, RMC_EMMC, BIOS and *_FWU_TOOLS are parts of that release."""
    target = entries[("csus_3200", "rmc")].target_version
    assert not target.startswith("1.72.93")
    assert not target.startswith("9.56")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.76.44- 20260622_072146", "1.76.44"),
        ("1.72.93-20260622_054645Z", "1.72.93"),
        ("2.16.04", "2.16.04"),
        ("U54 v3.00 (08/20/2026)", "U54 v3.00"),
        ("3.00_08-20-2026", "3.00"),
        ("6.1.4.215.0", "6.1.4.215.0"),
    ],
)
def test_trailing_build_stamps_are_stripped(value, expected):
    assert split_version_and_build(value)[0] == expected
