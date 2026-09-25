"""Reading the right column of a vendor version matrix.

These files are reissued monthly and the conversion is not stable between
issues. One carried the source page number in column A, shifting every other
column right by one. Another listed the previous release beside the current
one, so "the first value after the component name" stopped being the answer -
it was the help text, and failing that it was *last month's* version.

Reporting an estate compliant against a superseded baseline is the most
damaging thing this reader could do, so the column is chosen by its label and
the tests below are mostly about refusing to guess.
"""
import pytest

from recipe.matrix_loader import _strip_index_column, _version_column, parse_matrix


def targets(rows):
    return {
        (row["model"] or row["platform"], row["component"]): row["target_version"]
        for row in rows
    }


# ------------------------------------------------------- choosing the column


def test_the_latest_release_column_wins():
    header = ["Components", "Help Command", "Recipe 2026.01", "Recipe 2026.02",
              "Links", "Dependency"]
    assert _version_column(header) == 3


def test_order_on_the_sheet_does_not_decide_it():
    """The newest release is normally rightmost, but the label is what counts."""
    header = ["Components", "Recipe 2026.02", "Recipe 2026.01"]
    assert _version_column(header) == 1


def test_a_single_release_column_is_found():
    assert _version_column(["Components", "Recipe 2026.03"]) == 1


def test_an_unlabelled_release_still_counts():
    assert _version_column(["Components", "Target Version"]) == 1


def test_a_header_naming_no_release_chooses_nothing():
    """Later sections repeat a partial header; it must not clear the column."""
    assert _version_column(["Components", "Help Command"]) is None


def test_the_component_column_is_never_the_value():
    assert _version_column(["Recipe", "Help Command"]) is None


# --------------------------------------------------------- the index column


def test_a_labelled_page_column_is_dropped():
    table = [
        ["PDF Page", "SAP CDC 1.5 Solution Version Matrix"],
        ["1", "VMware Components"],
        ["1", "Components", "Recipe 2026.02"],
    ]
    assert _strip_index_column(table) == [
        ["SAP CDC 1.5 Solution Version Matrix"],
        ["VMware Components"],
        ["Components", "Recipe 2026.02"],
    ]


def test_an_unlabelled_but_numbered_column_is_dropped():
    table = [["", "Components", "Recipe 2026.02"]]
    table += [[str(1 + n // 3), f"Component {n}", "1.0"] for n in range(9)]
    assert all(row[0] != "1" for row in _strip_index_column(table))


def test_a_sheet_without_one_is_left_alone():
    table = [
        ["HPE_SAP_RISE_CDC1.5_2026.03_Version_Matrix"],
        ["Components", "Recipe 2026.03"],
        ["DL380 Gen11 System BIOS ROM", "U54 v3.00"],
    ]
    assert _strip_index_column(table) == table


# ------------------------------------------------------------- end to end


PAGE_COLUMN_SHEET = [
    ["PDF Page", "SAP CDC 1.5 Solution Version Matrix for 2026.02 Release"],
    ["1", "HPE ProLiant DL380 Gen11 Details"],
    ["1", "Components", "Help Command", "Recipe 2026.01", "Recipe 2026.02",
     "Links", "Dependency"],
    ["1", "DL380 Gen11 System BIOS ROM", "dmidecode -t bios", "U54 v2.70",
     "U54 v2.80 (01/29/2026)", "https://support.hpe.com", ""],
    ["1", "ILO6 Firmware Version", "iLO Firmware & OS Software", "1.73", "1.74",
     "", ""],
    ["1", "Intelligent Provisioning", "iLO -> Firmware & OS Software", "4.35.4",
     "4.31.5", "", ""],
]


def test_a_shifted_sheet_with_two_releases_reads_the_current_one():
    rows, _skipped = parse_matrix([("HPE CDC 1.5", PAGE_COLUMN_SHEET)])
    found = targets(rows)
    assert found[("DL380 Gen11", "BIOS")] == "U54 v2.80"
    assert found[("DL380 Gen11", "iLO")] == "1.74"


def test_the_previous_release_is_never_read():
    """Intelligent Provisioning went *down* between these two releases.

    A version that decreases is the one case where reading the wrong column
    cannot hide behind "newer is compliant anyway", so it is worth asserting
    on its own.
    """
    rows, _skipped = parse_matrix([("HPE CDC 1.5", PAGE_COLUMN_SHEET)])
    assert targets(rows)[("DL380 Gen11", "Intelligent Provisioning")] == "4.31.5"
    assert "4.35.4" not in {r["target_version"] for r in rows}


def test_the_help_command_is_never_read_as_a_version():
    rows, _skipped = parse_matrix([("HPE CDC 1.5", PAGE_COLUMN_SHEET)])
    assert not any("dmidecode" in r["target_version"] for r in rows)


def test_a_component_with_no_current_target_is_reported_not_guessed():
    sheet = [
        ["HPE ProLiant DL380 Gen11 Details"],
        ["Components", "Help Command", "Recipe 2026.01", "Recipe 2026.02"],
        ["ILO6 Firmware Version", "iLO -> Firmware", "1.73", ""],
    ]
    rows, skipped = parse_matrix([("sheet", sheet)])
    assert rows == [], "falling back to 1.73 would approve a superseded version"
    assert any("no usable version" in note for note in skipped)


def test_the_old_two_column_layout_still_works():
    sheet = [
        ["HPE_SAP_RISE_CDC1.5_2026.03_Version_Matrix"],
        ["Page 1 - Table 1"],
        ["HPE ProLiant DL380 Gen11 Details"],
        ["Components", "Recipe 2026.03"],
        ["DL380 Gen11 System BIOS ROM", "U54 v3.00 (08/20/2026)"],
        ["ILO6 Firmware Version", "1.77"],
    ]
    found = targets(parse_matrix([("HPE_SAP_RISE_CDC1.5", sheet)])[0])
    assert found[("DL380 Gen11", "BIOS")] == "U54 v3.00"
    assert found[("DL380 Gen11", "iLO")] == "1.77"


# ------------------------------------------------- carrying across a break

# A PDF is read one page at a time. A table that runs over a page break
# repeats neither its header nor its section heading.

PAGE_ONE = [
    ["HPE ProLiant DL380 Gen11 Details"],
    ["Components", "Help Command", "Recipe 2026.01", "Recipe 2026.02"],
    ["DL380 Gen11 System BIOS ROM", "dmidecode", "U54 v2.70", "U54 v2.80"],
]
PAGE_TWO = [
    ["ILO6 Firmware Version", "iLO -> Firmware", "1.73", "1.74"],
    ["Components", "Help Command"],
    ["Intelligent Provisioning", "iLO -> Firmware", "4.35.4", "4.31.5"],
]


def test_a_section_survives_a_page_break():
    rows, _skipped = parse_matrix(
        [("Page 1", PAGE_ONE), ("Page 2", PAGE_TWO)], continuous=True
    )
    found = targets(rows)
    assert found[("DL380 Gen11", "iLO")] == "1.74"
    assert found[("DL380 Gen11", "Intelligent Provisioning")] == "4.31.5"


def test_a_heading_does_not_reach_across_workbook_sheets():
    """Between sheets, attributing a row to the wrong machine is the risk."""
    rows, skipped = parse_matrix([("Sheet A", PAGE_ONE), ("Sheet B", PAGE_TWO)])
    assert ("DL380 Gen11", "iLO") not in targets(rows)
    assert any("before any platform heading" in note for note in skipped)


def test_a_partial_header_does_not_clear_the_column():
    rows, _skipped = parse_matrix(
        [("Page 1", PAGE_ONE), ("Page 2", PAGE_TWO)], continuous=True
    )
    assert "4.35.4" not in {r["target_version"] for r in rows}


# --------------------------------------------------- a column about the target

# One issue names its links column "Recipe 2025.01 links". It begins the same
# way as the version column and carries the same release, so on the tie the
# rightmost won and every target became a URL - which the validator rejected,
# taking the whole file with it.

LINKS_HEADER = ["Components", "Help Command", "Recipe 2024.02.3", "Recipe 2025.01",
                "Recipe 2025.01 links", "", "Dependency"]


def test_a_links_column_is_not_a_version_column():
    assert _version_column(LINKS_HEADER) == 3


@pytest.mark.parametrize(
    "header",
    ["Recipe 2025.01 links", "Recipe 2025.01 link", "Target Version URL",
     "Recipe 2025.01 notes", "Recipe 2025.01 dependency", "Version comments",
     "Recipe help command"],
)
def test_a_column_about_the_target_is_never_the_target(header):
    assert _version_column(["Components", "Recipe 2025.01", header]) == 1


def test_the_release_still_decides_between_real_version_columns():
    assert _version_column(
        ["Components", "Recipe 2025.01", "Recipe 2024.02.3"]
    ) == 1


LINKS_SHEET = [
    ["SAP S4HANA Solution Version Matrix"],
    ["HPE ProLiant DL360 Gen11 Details"],
    LINKS_HEADER,
    ["DL360 Gen11 System BIOS ROM", "dmidecode", "U54 v2.20", "U54 v2.34",
     "https://support.hpe.com/connect/s/softwaredetails", "", ""],
    ["ILO6 Firmware Version", "iLO", "1.60", "1.65", "https://spp.hpe.com", "", ""],
]


def test_the_release_is_read_and_not_the_link():
    rows, _skipped = parse_matrix([("Page 1", LINKS_SHEET)])
    found = targets(rows)
    assert found[("DL360 Gen11", "BIOS")] == "U54 v2.34"
    assert found[("DL360 Gen11", "iLO")] == "1.65"
    assert not any("http" in r["target_version"] for r in rows)


def test_a_link_is_never_accepted_as_a_version():
    """A safety net for a layout whose value column cannot be identified."""
    sheet = [
        ["HPE ProLiant DL360 Gen11 Details"],
        # A header naming no release at all, so the reader has nothing to go on
        # and falls back to the first value it finds.
        ["Components", "Help Command"],
        ["ILO6 Firmware Version", "https://spp.hpe.com"],
    ]
    rows, skipped = parse_matrix([("Page 1", sheet)])
    assert rows == []
    assert any("link where a version should be" in note for note in skipped), skipped

# ------------------------------------- a links column labelled like a version

# The 2025.02.1 issue heads three columns the same way:
#
#   Components | Help Command | Recipe 2025.02 | Recipe 2025.02.1 | Recipe 2025.02
#                               last release     this release       links
#
# Two things then went wrong at once. "Recipe 2025.02.1" ranked equal to
# "Recipe 2025.02", because only the year and month were read - so the patch
# release did not outrank the release it patched. And the links column, headed
# with those same words and carrying no word like "links" to give it away, won
# the tie for being furthest right.
#
# Every target on those pages then came out as a URL and was dropped as
# unreadable, which cost the recipe 27 of its 44 targets - including every
# Gen10 and Gen10 Plus row. The estate showed NOT FOUND IN RECIPE with an
# empty target for hardware the document covers perfectly well, and the
# recipe was still accepted, because the pages that did parse looked fine.

SAME_LABEL_HEADER = [
    "Components", "Help Command", "Recipe 2025.02", "Recipe 2025.02.1",
    "Recipe 2025.02", "", "Dependency",
]

SAME_LABEL_ROWS = [
    ["DL380 Gen10 plus System BIOS ROM", "iLO", "U46 v2.40 (04/18/2025)",
     "U46 v2.40 (04/18/2025)", "https://support.hpe.com/connect/s/softwaredetails",
     "", "This version of the System ROM"],
    ["Server Platform Services (SPS)", "iLO", "4.4.4.702", "4.4.4.702",
     "https://support.hpe.com/connect/s/softwaredetails", "", ""],
    ["ILO5 Firmware Version", "iLO", "3.13 Apr 24 2025", "3.13 Apr 24 2025",
     "https://support.hpe.com/connect/s/softwaredetails", "", ""],
]


def test_a_patch_release_outranks_the_release_it_patches():
    """2025.02.1 came after 2025.02, so it is the one to measure against."""
    assert _version_column(["Components", "Recipe 2025.02", "Recipe 2025.02.1"]) == 2
    assert _version_column(["Components", "Recipe 2025.02.1", "Recipe 2025.02"]) == 1


def test_a_links_column_is_refused_on_what_it_holds():
    """Its label is word for word a version column's, so only content tells."""
    assert _version_column(SAME_LABEL_HEADER, SAME_LABEL_ROWS) == 3


def test_without_the_rows_the_label_still_decides():
    """A header read on its own must still not fall back to the last column."""
    assert _version_column(SAME_LABEL_HEADER) == 3


def test_a_version_column_is_not_refused_for_being_empty_here():
    """No values to judge is not evidence of links - the label stands."""
    blank = [["DL380 Gen10 plus System BIOS ROM", "iLO", "", "", "", "", ""]]
    assert _version_column(SAME_LABEL_HEADER, blank) == 3


SAME_LABEL_SHEET = [
    ["SAP S4HANA Solution Version Matrix"],
    ["HPE ProLiant DL380 Gen10 Plus Details"],
    SAME_LABEL_HEADER,
] + SAME_LABEL_ROWS


def test_the_section_is_read_rather_than_lost_to_the_links_column():
    rows, skipped = parse_matrix([("Page 2", SAME_LABEL_SHEET)])
    found = targets(rows)
    assert found[("DL380 Gen10 Plus", "BIOS")] == "U46 v2.40"
    assert found[("DL380 Gen10 Plus", "SPS")] == "4.4.4.702"
    assert found[("DL380 Gen10 Plus", "iLO")] == "3.13"
    assert not any("http" in row["target_version"] for row in rows)
    assert not any("link where a version" in note for note in skipped), skipped


def test_the_previous_release_column_is_still_not_the_one_read():
    """The fix must not overshoot into reading last month's versions."""
    sheet = [
        ["HPE ProLiant DL380 Gen10 Plus Details"],
        SAME_LABEL_HEADER,
        ["DL380 Gen10 plus System BIOS ROM", "iLO", "U46 v2.30", "U46 v2.40",
         "https://support.hpe.com/connect", "", ""],
    ]
    rows, _skipped = parse_matrix([("Page 2", sheet)])
    assert targets(rows)[("DL380 Gen10 Plus", "BIOS")] == "U46 v2.40"
