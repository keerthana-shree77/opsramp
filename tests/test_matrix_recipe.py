"""Vendor "version matrix" recipes (HPE SAP RISE / S4HANA PDF-to-Excel exports).

The fixture reproduces the real layout: a README sheet, the source PDF name,
"Page N - Table N" markers, section headings ending in "Details", repeated
"Components" header rows, and continuation rows carrying "KEY: value"
sub-components.
"""
import pytest
from openpyxl import Workbook

from firmware.normalizer import normalize_component, normalize_component_for_platform
from recipe.loader import load_recipe
from recipe.matrix_loader import split_version_and_build
from recipe.validator import validate_recipe

SHEET_ROWS = [
    ["HPE_SAP_RISE_CDC1.5_2026.03_Version_Matrix.pdf"],
    [],
    ["Page 1 - Table 1"],
    ["SAP CDC 1.5 Solution Version Matrix for HPE"],
    ["VMware Components"],
    ["Components", "Recipe 2026.03"],
    ["VMware vSphere ESXi", "VMware ESXi 8.0 U3k (Build 25595708)"],
    ["VMware Tools for Windows", "13.1.0.0"],
    ["HPE Compute Scale-up Server 3200 Details"],
    ["Components"],
    ["HPE Compute Scale-up Server 3200 Firmware", "1.76.44"],
    ["", "BIOS: 9.56.20.20260618_160844"],
    ["", "RMC_EMMC: 1.72.93-20260622_054645Z"],
    ["HPE ProLiant DL380 Gen11 Details"],
    ["Components"],
    ["SPP Bundle", "2026.07.00.00"],
    ["DL380 Gen11 System BIOS ROM", "U54 v3.00 (08/20/2026)"],
    ["ILO6 Firmware Version", "1.77"],
    ["Power Supply Firmware", "2.00"],
    ["Server Platform Services (SPS) Firmware", "6.1.4.215.0"],
    ["Intelligent Provisioning", "4.31.5"],
    ["SD Flex 280 Details"],
    ["Components", "Recipe 2026.03"],
    ["", "COMPLEX_METADATA: 2.16.04"],
    ["", "EMMC: 3.120.94-20260529_074248"],
    ["", "BIOS:8.160.2.20260522_083912.biosdev"],
    ["", "BMC:3.120.94-20260529_074248"],
    ["", "BMC_FWU_TOOLS:3.120.94- 20260529_074248"],
    ["HPE ILO Amplifier", "2,23"],
]


@pytest.fixture
def matrix(tmp_path):
    workbook = Workbook()
    workbook.remove(workbook.active)
    readme = workbook.create_sheet("README")
    for row in [["PDF to Excel Conversion"], [], ["Source file", "Excel sheet"]]:
        readme.append(row)
    sheet = workbook.create_sheet("HPE_SAP_RISE_CDC1.5")
    for row in SHEET_ROWS:
        sheet.append(row)
    path = tmp_path / "matrix.xlsx"
    workbook.save(path)
    workbook.close()
    return path


@pytest.fixture
def entries(matrix):
    raw = load_recipe(matrix)
    assert raw.layout == "matrix"
    report = validate_recipe(raw)
    assert report.ok, [i.render() for i in report.errors]
    return {(e.platform_key, e.component_key): e for e in report.entries}, report


def test_layout_is_detected(matrix):
    assert load_recipe(matrix).layout == "matrix"


def test_dl380_components(entries):
    found, _ = entries
    assert found[("hpe_dl", "bios")].target_version == "U54 v3.00"
    assert found[("hpe_dl", "ilo")].target_version == "1.77"
    assert found[("hpe_dl", "sps")].target_version == "6.1.4.215.0"
    assert found[("hpe_dl", "intelligent_provisioning")].target_version == "4.31.5"
    assert found[("hpe_dl", "bios")].model_key == "dl380_gen11"


def test_esxi_version_and_build(entries):
    found, _ = entries
    esxi = found[("vmware_esxi", "esxi")]
    assert esxi.target_version == "8.0 U3k"
    assert esxi.target_build == "25595708"
    # A grouping heading names no model, so the row applies to the family.
    assert esxi.model_key == ""
    assert esxi.applies_to_family is True


def test_superdome_rmc_comes_from_complex_metadata(entries):
    """COMPLEX_METADATA is the controller release everyone tracks.

    The BMC:, EMMC: and *_FWU_TOOLS: lines beside it are internal sub-builds
    of that same release, so taking one of them would report a version nobody
    recognises as the RMC version.
    """
    found, _ = entries
    assert found[("superdome_flex_280", "rmc")].target_version == "2.16.04"


def test_superdome_subbuilds_are_not_used_as_the_rmc_version(entries):
    found, report = entries
    assert found[("superdome_flex_280", "rmc")].target_version != "3.120.94-20260529_074248"
    messages = " ".join(w.message for w in report.warnings)
    assert "EMMC" in messages or "BMC" in messages


def test_csus_rmc_comes_from_the_headline_firmware_row(entries):
    """"HPE Compute Scale-up Server 3200 Firmware" is the controller release."""
    found, _ = entries
    assert found[("csus_3200", "rmc")].target_version == "1.76.44"


def test_power_supply_firmware_is_not_read_as_switch_firmware(entries):
    """The dangerous false positive: any "... Firmware" matching a switch."""
    found, report = entries
    assert not any(key[1] == "switch_firmware" for key in found)
    assert any("Power Supply Firmware" in w.message for w in report.warnings)


def test_ilo_amplifier_is_not_read_as_ilo(entries):
    found, _ = entries
    assert found[("hpe_dl", "ilo")].target_version == "1.77"


def test_unused_lines_are_reported_not_dropped(entries):
    _found, report = entries
    messages = " ".join(w.message for w in report.warnings)
    assert "VMware Tools for Windows" in messages
    assert "SPP Bundle" in messages


@pytest.mark.parametrize(
    "value,version,build",
    [
        ("VMware ESXi 8.0 U3k (Build 25595708)", "8.0 U3k", "25595708"),
        ("8.0 Update 3k (Build-25600417)", "8.0 Update 3k", "25600417"),
        ("U54 v3.00 (08/20/2026)", "U54 v3.00", ""),
        ("3.00_08-20-2026", "3.00", ""),
        ("6.1.4.215.0", "6.1.4.215.0", ""),
    ],
)
def test_split_version_and_build(value, version, build):
    assert split_version_and_build(value) == (version, build)


def test_bmc_resolves_per_platform():
    # On ProLiant the BMC is the iLO.
    assert normalize_component_for_platform("BMC", "hpe_dl") == "ilo"
    # On Superdome Flex the BMC line is a sub-build of the RMC release, not
    # the RMC version, so it must not be claimed as one.
    assert normalize_component_for_platform("BMC", "superdome_flex_280") is None


def test_platform_component_list_is_exhaustive():
    """No falling back to the global lookup past a platform's own list."""
    assert normalize_component_for_platform("RMC_EMMC", "csus_3200") is None
    assert normalize_component_for_platform("RMC_FWU_TOOLS", "csus_3200") is None
    assert normalize_component_for_platform("COMPLEX_METADATA", "csus_3200") == "rmc"
    assert normalize_component_for_platform("Power Supply Firmware", "hpe_dl") is None


def test_ilo_generation_suffix_is_folded():
    assert normalize_component("ILO6 Firmware Version") == "ilo"
    assert normalize_component("iLO 5") == "ilo"
