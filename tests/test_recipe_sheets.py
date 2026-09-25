"""Multi-worksheet XLSX handling.

A real recipe workbook usually is not a bare table on Sheet1: it has a cover
sheet, a notes sheet, and the data somewhere else.
"""
import pytest
from openpyxl import Workbook

from recipe.loader import RecipeLoadError, load_recipe
from recipe.validator import validate_recipe

HEADER = ["Platform", "Model", "Category", "Component", "Target Version"]
ROW = ["HPE DL", "DL380 Gen11", "Server", "BIOS", "U54 v2.12"]


def build(tmp_path, sheets):
    """sheets: list of (name, rows)."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets:
        worksheet = workbook.create_sheet(name)
        for row in rows:
            worksheet.append(row)
    path = tmp_path / "recipe.xlsx"
    workbook.save(path)
    workbook.close()
    return path


def test_data_on_the_first_sheet(tmp_path):
    path = build(tmp_path, [("Sheet1", [HEADER, ROW])])
    raw = load_recipe(path)
    assert raw.sheet == "Sheet1"
    assert len(raw.rows) == 1


def test_data_on_a_later_sheet_is_found(tmp_path):
    path = build(
        tmp_path,
        [
            ("Cover", [["Firmware recipe 2026"], ["Prepared by the platform team"]]),
            ("Notes", [["Contact"], ["platform-team@example.com"]]),
            ("Baseline", [HEADER, ROW]),
        ],
    )
    raw = load_recipe(path)
    assert raw.sheet == "Baseline"
    assert len(raw.rows) == 1
    assert validate_recipe(raw).ok


def test_title_rows_above_the_header_are_skipped(tmp_path):
    path = build(
        tmp_path,
        [("Recipe", [["HPE Firmware Recipe 2026"], [], ["Revision 4"], HEADER, ROW])],
    )
    raw = load_recipe(path)
    assert len(raw.rows) == 1
    assert validate_recipe(raw).ok


def test_best_matching_sheet_wins(tmp_path):
    """A sheet naming all four required columns beats one naming only two."""
    partial = [["Platform", "Component"], ["HPE DL", "BIOS"]]
    path = build(tmp_path, [("Partial", partial), ("Full", [HEADER, ROW])])
    raw = load_recipe(path)
    assert raw.sheet == "Full"


def test_no_header_anywhere_lists_each_sheet(tmp_path):
    path = build(
        tmp_path,
        [
            ("Cover", [["Firmware recipe 2026"]]),
            ("Inventory", [["Hostname", "Serial"], ["srv001", "CZ123"]]),
        ],
    )
    with pytest.raises(RecipeLoadError) as excinfo:
        load_recipe(path)
    message = str(excinfo.value)
    assert "Cover" in message
    assert "Inventory" in message
    assert "Hostname" in message


def test_empty_workbook_sheet(tmp_path):
    path = build(tmp_path, [("Empty", [])])
    with pytest.raises(RecipeLoadError):
        load_recipe(path)
