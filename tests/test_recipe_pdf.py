"""Reading the vendor matrix straight from a PDF.

The published matrix is a PDF; converting it to Excel first is an extra manual
step, so the loader reads either form. The fixture builds a PDF whose tables
carry the same section/Components/page-marker structure as the real document.
"""
import pytest

from recipe.loader import RecipeLoadError, load_recipe
from recipe.validator import validate_recipe

reportlab = pytest.importorskip("reportlab", reason="reportlab builds the fixture PDF")

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.platypus import SimpleDocTemplate, Table  # noqa: E402

ROWS = [
    ["HPE ProLiant DL380 Gen11 Details", ""],
    ["Components", "Recipe 2026.03"],
    ["DL380 Gen11 System BIOS ROM", "U54 v3.00 (08/20/2026)"],
    ["ILO6 Firmware Version", "1.77"],
    ["Server Platform Services (SPS) Firmware", "6.1.4.215.0"],
    ["Intelligent Provisioning", "4.31.5"],
    ["Infrastructure Details (Switch and Storage)", ""],
    ["Components", "Recipe 2026.03"],
    ["Aruba 8325-32C Switch", "10.17.1031"],
    ["HPE Alletra 9060", "9.6.20"],
    ["PDU", ""],
    ["Components", "Recipe 2026.03"],
    ["HPE G3 Metered PDU", "3.3.4"],
]


@pytest.fixture
def matrix_pdf(tmp_path):
    path = tmp_path / "matrix.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4)
    doc.build([Table(ROWS)])
    return path


@pytest.fixture
def entries(matrix_pdf):
    raw = load_recipe(matrix_pdf)
    assert raw.layout == "matrix", "the PDF should be read as a version matrix"
    report = validate_recipe(raw)
    assert report.ok, [i.render() for i in report.errors]
    return {(e.platform_key, e.component_key): e for e in report.entries}


def test_pdf_is_accepted(matrix_pdf):
    assert load_recipe(matrix_pdf).rows


def test_server_components_from_pdf(entries):
    assert entries[("hpe_dl", "bios")].target_version == "U54 v3.00"
    assert entries[("hpe_dl", "ilo")].target_version == "1.77"
    assert entries[("hpe_dl", "sps")].target_version == "6.1.4.215.0"
    assert entries[("hpe_dl", "intelligent_provisioning")].target_version == "4.31.5"


def test_switch_storage_and_pdu_from_pdf(entries):
    assert entries[("switch", "switch_firmware")].target_version == "10.17.1031"
    assert entries[("storage", "os_version")].target_version == "9.6.20"
    assert entries[("pdu", "pdu_firmware")].target_version == "3.3.4"


def test_an_unreadable_pdf_is_reported_clearly(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 not really a pdf")
    with pytest.raises(RecipeLoadError) as excinfo:
        load_recipe(path)
    assert "PDF" in str(excinfo.value)
