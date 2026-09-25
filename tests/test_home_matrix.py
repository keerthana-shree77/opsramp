"""The compliance matrix on the overview page.

Percentages count only what could be compared. A component with no approved
target, or whose version was not read, is counted separately rather than being
folded into a rate it would distort in one direction or the other.
"""
import pytest

from firmware.models import ComparisonRow
from reports.summary import (
    compliance_tone,
    tenant_category_matrix,
    tenant_compliance,
)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows


def row(tenant, category, result, asset="a1", component="BIOS"):
    return ComparisonRow(
        tenant=tenant, category=category, result=result,
        asset_id=asset, hostname=asset, component=component,
    )


@pytest.fixture
def result():
    return FakeResult([
        row("SAP-one", "Server", "UPDATED", "srv1"),
        row("SAP-one", "Server", "UPDATED", "srv2"),
        row("SAP-one", "Server", "NEEDS UPDATE", "srv3"),
        row("SAP-one", "Server", "VERSION NOT DETECTED", "srv4"),
        row("SAP-one", "Switch", "NEEDS UPDATE", "sw1"),
        row("SAP-two", "Storage", "UPDATED", "st1"),
        row("SAP-two", "PDU", "NOT FOUND IN RECIPE", "pdu1"),
    ])


def test_rate_counts_only_comparable_components(result):
    grid = tenant_category_matrix(result)
    server = grid["SAP-one"]["Server"]
    # 2 updated, 1 needs update, 1 not detected -> 2 of 3 comparable.
    assert server["comparable"] == 3
    assert server["rate"] == pytest.approx(66.7)
    assert server["not_detected"] == 1


def test_a_category_with_nothing_comparable_has_no_rate(result):
    pdu = tenant_category_matrix(result)["SAP-two"]["PDU"]
    assert pdu["rate"] is None, "a percentage here would be invented"
    assert pdu["not_in_recipe"] == 1


def test_zero_and_full_compliance(result):
    grid = tenant_category_matrix(result)
    assert grid["SAP-one"]["Switch"]["rate"] == 0.0
    assert grid["SAP-two"]["Storage"]["rate"] == 100.0


def test_pending_is_what_a_reader_would_act_on(result):
    server = tenant_category_matrix(result)["SAP-one"]["Server"]
    assert server["pending"] == server["needs_update"] + server["not_detected"] == 2


def test_assets_are_counted_distinctly():
    duplicated = FakeResult([
        row("SAP-one", "Server", "UPDATED", "srv1", "BIOS"),
        row("SAP-one", "Server", "UPDATED", "srv1", "iLO"),
    ])
    cell = tenant_category_matrix(duplicated)["SAP-one"]["Server"]
    assert cell["assets"] == 1
    assert cell["components"] == 2


def test_a_tenant_only_has_the_categories_it_owns(result):
    grid = tenant_category_matrix(result)
    assert set(grid["SAP-one"]) == {"Server", "Switch"}
    assert set(grid["SAP-two"]) == {"Storage", "PDU"}


def test_per_tenant_totals_agree_with_the_matrix(result):
    grid = tenant_category_matrix(result)
    for stats in tenant_compliance(result):
        cells = grid[stats["tenant"]].values()
        assert stats["updated"] == sum(c["updated"] for c in cells)
        assert stats["needs_update"] == sum(c["needs_update"] for c in cells)
        assert stats["components"] == sum(c["components"] for c in cells)


@pytest.mark.parametrize(
    "rate,tone",
    [
        # Above 90 is green.
        (100, "ok"), (95, "ok"), (90.1, "ok"),
        # 80 to 90 inclusive is amber - 90 itself is not "above 90".
        (90, "warn"), (85, "warn"), (80, "warn"),
        # Below 80 is red.
        (79.9, "bad"), (0, "bad"),
        # No percentage to judge, so no colour to judge it with. Colouring
        # this red would report a gap in the recipe as a compliance failure.
        (None, "muted"),
    ],
)
def test_traffic_light_bands(rate, tone):
    assert compliance_tone(rate) == tone


# --------------------------------------------------------------------- coverage

# Coverage answers a different question from compliance: not "is this up to
# date" but "does this recipe describe this equipment at all". It is what
# decides which recipe an account is judged against, so it is worth pinning.


def covered_result():
    return FakeResult([
        # Two components the recipe has a target for, one of them not read.
        row_with_target("SAP-one", "Server", "UPDATED", "srv1", "BIOS", "U54 v3.00"),
        row_with_target("SAP-one", "Server", "VERSION NOT DETECTED", "srv2", "iLO",
                        "1.62"),
        # One the recipe says nothing about.
        row_with_target("SAP-one", "Server", "NOT FOUND IN RECIPE", "srv3", "SPS", ""),
    ])


def row_with_target(tenant, category, result, asset, component, target):
    return ComparisonRow(
        tenant=tenant, category=category, result=result,
        asset_id=asset, hostname=asset, component=component,
        target_version=target,
    )


def test_a_component_with_a_target_counts_as_covered():
    [stats] = tenant_compliance(covered_result())
    assert stats["components"] == 3
    assert stats["covered"] == 2, (
        "a version the recipe has a target for is covered even when OpsRamp "
        "never reported what is installed"
    )
    assert stats["coverage"] == pytest.approx(66.7)


def test_coverage_is_separate_from_compliance():
    [stats] = tenant_compliance(covered_result())
    # One comparable component, and it is up to date.
    assert stats["comparable"] == 1 and stats["rate"] == 100.0
    # But the recipe only describes two thirds of what is installed.
    assert stats["coverage"] == pytest.approx(66.7)


def test_cells_carry_coverage_too():
    grid = tenant_category_matrix(covered_result())
    cell = grid["SAP-one"]["Server"]
    assert cell["covered"] == 2
    assert cell["coverage"] == pytest.approx(66.7)


def test_a_not_applicable_row_still_counts_as_described():
    result = FakeResult([
        row_with_target("SAP-one", "Server", "NOT APPLICABLE", "srv1", "SPS", ""),
    ])
    [stats] = tenant_compliance(result)
    assert stats["covered"] == 1, (
        "a recipe that explicitly marks a component N/A has described it"
    )
    assert stats["rate"] is None
