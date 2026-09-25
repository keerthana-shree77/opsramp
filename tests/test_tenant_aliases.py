"""An alias that holds in some accounts and not others.

The same OpsRamp model can stand for different hardware in different accounts.
"HP_3PAR" is one estate's label for two distinct arrays - the label is simply
wrong at the source - so the equivalence cannot be stated estate-wide. The
file therefore says "this means X everywhere, except in these accounts where
it means Y", and an alias naming accounts beats one that names none.
"""
import pytest

from firmware.aliases import DEFAULT_PATH, aliases_for, load_aliases

HEADER = "OpsRamp Model,Recipe Model,Component,Tenant,Notes\n"


@pytest.fixture
def table(tmp_path):
    def build(body):
        path = tmp_path / "aliases.csv"
        path.write_text(HEADER + body, encoding="utf-8")
        load_aliases(path, force=True)
        return path
    yield build
    # Once another file is loaded the module stays on it by design, so the
    # shipped one has to be named to get back to it.
    load_aliases(DEFAULT_PATH, force=True)


# --------------------------------------------------------------- the scoping


def test_an_alias_naming_accounts_applies_only_to_them(table):
    table("HP_3PAR,Alletra MP,,grr01;grr02,mislabelled\n")
    assert aliases_for("HP_3PAR", None, "SAP-grr01-PrivateCloud-abc") == ["Alletra MP"]
    assert aliases_for("HP_3PAR", None, "SAP-grr02-PrivateCloud-def") == ["Alletra MP"]
    assert aliases_for("HP_3PAR", None, "SAP-acme01-PrivateCloud-ghi") == []


def test_an_unscoped_alias_applies_everywhere(table):
    table("HP_3PAR,Alletra 9060,,,mislabelled\n")
    for tenant in ("SAP-grr01-x", "SAP-acme01-y", "SAP-anything-z"):
        assert aliases_for("HP_3PAR", None, tenant) == ["Alletra 9060"]


def test_a_named_account_beats_the_estate_wide_rule(table):
    """The case this exists for: X everywhere, Y in four accounts."""
    table(
        "HP_3PAR,Alletra MP,,grr01;grr02;idp01;idp02,mislabelled\n"
        "HP_3PAR,Alletra 9060,,,mislabelled\n"
    )
    for named in ("grr01", "grr02", "idp01", "idp02"):
        assert aliases_for("HP_3PAR", None, f"SAP-{named}-PrivateCloud-x") == [
            "Alletra MP"
        ], f"{named} should take the scoped rule alone"
    for other in ("acme01", "other01", "globex01", "saplab"):
        assert aliases_for("HP_3PAR", None, f"SAP-{other}-PrivateCloud-x") == [
            "Alletra 9060"
        ]


def test_the_two_rules_are_never_offered_together(table):
    """Both at once would make the match ambiguous and report neither."""
    table(
        "HP_3PAR,Alletra MP,,idp01,mislabelled\n"
        "HP_3PAR,Alletra 9060,,,mislabelled\n"
    )
    assert len(aliases_for("HP_3PAR", None, "SAP-idp01-PrivateCloud-x")) == 1


# ------------------------------------------------------- naming an account


def test_an_account_is_matched_by_any_part_of_its_name(table):
    table("HP_3PAR,Alletra MP,,grr01,mislabelled\n")
    assert aliases_for("HP_3PAR", None,
                       "SAP-grr01-PrivateCloud-d6n6m25pmomc73c5io00") == ["Alletra MP"]


def test_a_longer_account_name_also_matches(table):
    table("HP_3PAR,Alletra MP,,SAP-grr01-PrivateCloud,mislabelled\n")
    assert aliases_for("HP_3PAR", None,
                       "SAP-grr01-PrivateCloud-d6n6m25") == ["Alletra MP"]


def test_a_near_miss_is_not_a_match(table):
    """grr01 and grr02 are different accounts and must not be confused."""
    table("HP_3PAR,Alletra MP,,grr01,mislabelled\n")
    assert aliases_for("HP_3PAR", None, "SAP-grr02-PrivateCloud-x") == []
    assert aliases_for("HP_3PAR", None, "SAP-grr011-PrivateCloud-x") == []


def test_a_scoped_alias_without_an_account_to_check_does_not_apply(table):
    table("HP_3PAR,Alletra MP,,grr01,mislabelled\n")
    assert aliases_for("HP_3PAR", None, "") == []


# --------------------------------------------- alongside the other scoping


def test_an_account_scope_and_a_component_scope_combine(table):
    table("HP_3PAR,Alletra MP,OS Version,grr01,mislabelled\n")
    assert aliases_for("HP_3PAR", "os_version", "SAP-grr01-x") == ["Alletra MP"]
    assert aliases_for("HP_3PAR", "bios", "SAP-grr01-x") == []
    assert aliases_for("HP_3PAR", "os_version", "SAP-other-x") == []


def test_a_file_without_the_column_still_loads(table):
    """The column is new; a file written before it must keep working."""
    path = table("")
    path.write_text(
        "OpsRamp Model,Recipe Model,Component,Notes\n"
        "G620,SN6600B,,Brocade G620 = HPE SN6600B\n",
        encoding="utf-8",
    )
    load_aliases(path, force=True)
    assert aliases_for("G620", None, "SAP-anything-x") == ["SN6600B"]


# ------------------------------------------------- the shipped declarations


def test_the_shipped_file_maps_3par_as_the_operator_stated():
    """The estate has no 3PAR; the label is wrong at the source."""
    load_aliases(DEFAULT_PATH, force=True)
    for named in ("grr01", "grr02", "idp01", "idp02"):
        assert aliases_for("HP_3PAR", "os_version",
                           f"SAP-{named}-PrivateCloud-x") == ["Alletra MP"]
    for other in ("acme01", "acme02", "other01", "other02", "other03", "other04"):
        assert aliases_for("HP_3PAR", "os_version",
                           f"SAP-{other}-PrivateCloud-x") == ["Alletra 9060"]
