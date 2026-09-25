"""Operator-declared model equivalences.

OpsRamp reports a part number where the vendor recipe names a product line:
"G620" against "HPE StoreFabric SN6600B Fiber Channel Switch", "P9R53A"
against "HPE Metered and Switched PDU". Nothing in either source states they
are the same device, so the equivalence is declared in model_aliases.csv
rather than inferred.
"""
import pytest

from firmware import aliases
from firmware.matcher import LEVEL_ALIAS, RecipeIndex
from firmware.models import Asset
from firmware.normalizer import normalize_model
from recipe.normalizer import normalize_row


@pytest.fixture(autouse=True)
def clear_alias_cache():
    aliases._cache = None
    aliases._cache_path = None
    yield
    aliases._cache = None
    aliases._cache_path = None


def write_aliases(tmp_path, text):
    path = tmp_path / "model_aliases.csv"
    path.write_text(text, encoding="utf-8")
    aliases.load_aliases(path, force=True)
    return path


def entry(platform, model, category, component, version, row=1):
    normalized, problems = normalize_row(
        {
            "_row": row,
            "platform": platform,
            "model": model,
            "category": category,
            "component": component,
            "target_version": version,
        }
    )
    assert not problems, problems
    return normalized


def asset(platform_key, model, category):
    return Asset(
        tenant="SAP01",
        asset_id="a1",
        hostname="dev01",
        platform_key=platform_key,
        model=model,
        model_key=normalize_model(model),
        category=category,
    )


# --------------------------------------------------------------- file parsing


def test_comments_and_header_are_ignored(tmp_path):
    write_aliases(
        tmp_path,
        "OpsRamp Model,Recipe Model,Notes\n"
        "# a comment,ignored,ignored\n"
        "G620,SN6600B,verified\n",
    )
    assert aliases.aliases_for("G620") == ["SN6600B"]
    assert aliases.aliases_for("OpsRamp Model") == []


def test_a_missing_file_is_not_an_error(tmp_path):
    aliases.load_aliases(tmp_path / "absent.csv", force=True)
    assert aliases.aliases_for("G620") == []


def test_alias_matches_a_single_token_of_the_model(tmp_path):
    write_aliases(tmp_path, "8325,Aruba 8325,\n")
    assert aliases.aliases_for("8325-32C (JL636A)") == ["Aruba 8325"]


def test_several_aliases_for_one_model(tmp_path):
    write_aliases(tmp_path, "G620,SN6600B,\nG620,SN6700B,\n")
    assert aliases.aliases_for("G620") == ["SN6600B", "SN6700B"]


# ------------------------------------------------------------------ matching


def test_switch_matches_through_its_alias(tmp_path):
    write_aliases(tmp_path, "G620,SN6600B,Brocade G620 = HPE SN6600B\n")
    index = RecipeIndex(
        [
            entry(
                "Switch",
                "HPE StoreFabric SN6600B Fiber Channel Switch",
                "Switch",
                "Switch Firmware",
                "9.2.2c1",
            )
        ]
    )
    match = index.match(asset("switch", "G620", "switch"), "switch_firmware")
    assert match.level == LEVEL_ALIAS
    assert match.entry.target_version == "9.2.2c1"
    assert "model_aliases.csv" in match.note


def test_pdu_matches_through_its_alias(tmp_path):
    write_aliases(tmp_path, "P9R53A,Metered and Switched,HPE G2 PDU\n")
    index = RecipeIndex(
        [
            entry("PDU", "Metered and Switched", "PDU", "PDU Firmware", "2.0.0.U", row=1),
            entry("PDU", "G3 Metered", "PDU", "PDU Firmware", "3.3.4", row=2),
        ]
    )
    match = index.match(asset("pdu", "P9R53A", "pdu"), "pdu_firmware")
    assert match.level == LEVEL_ALIAS
    assert match.entry.target_version == "2.0.0.U"


def test_without_an_alias_the_gap_names_the_candidates(tmp_path):
    """A dead end has to say what the recipe does offer, or it is unactionable."""
    write_aliases(tmp_path, "# none\n")
    index = RecipeIndex(
        [
            entry("PDU", "Metered and Switched", "PDU", "PDU Firmware", "2.0.0.U", row=1),
            entry("PDU", "G3 Metered", "PDU", "PDU Firmware", "3.3.4", row=2),
        ]
    )
    match = index.match(asset("pdu", "P9R53A", "pdu"), "pdu_firmware")
    assert match.entry is None
    assert "Metered and Switched" in match.note
    assert "G3 Metered" in match.note
    assert "model_aliases.csv" in match.note


def test_an_alias_cannot_cross_a_category(tmp_path):
    write_aliases(tmp_path, "P9R53A,Metered and Switched,\n")
    index = RecipeIndex(
        [entry("PDU", "Metered and Switched", "PDU", "PDU Firmware", "2.0.0.U")]
    )
    match = index.match(asset("pdu", "P9R53A", "switch"), "pdu_firmware")
    assert match.entry is None


def test_a_direct_match_is_preferred_over_an_alias(tmp_path):
    write_aliases(tmp_path, "8325-32C,Some Other Switch,\n")
    index = RecipeIndex(
        [
            entry("Aruba", "8325-32C", "Switch", "Switch Firmware", "10.17.1031", row=1),
            entry("Aruba", "Some Other Switch", "Switch", "Switch Firmware", "1.0", row=2),
        ]
    )
    match = index.match(asset("switch", "8325-32C (JL636A)", "switch"), "switch_firmware")
    assert match.entry.target_version == "10.17.1031"
    assert match.level != LEVEL_ALIAS


def test_conflicting_aliased_targets_are_reported_as_ambiguous(tmp_path):
    write_aliases(tmp_path, "P9R53A,Metered and Switched,\nP9R53A,G3 Metered,\n")
    index = RecipeIndex(
        [
            entry("PDU", "Metered and Switched", "PDU", "PDU Firmware", "2.0.0.U", row=1),
            entry("PDU", "G3 Metered", "PDU", "PDU Firmware", "3.3.4", row=2),
        ]
    )
    match = index.match(asset("pdu", "P9R53A", "pdu"), "pdu_firmware")
    assert match.ambiguous
    assert match.entry is None


# ------------------------------------------------- component-scoped aliases

# These exercise the mechanism, not a shipped alias. The Alletra Storage
# Server borrowing the Gen11 iLO target was the case it was built for, and the
# operator has since said not to track that machine's iLO at all - so the rows
# are gone from model_aliases.csv. The scoping itself is still how a platform
# would borrow one target from comparable hardware, so it is still tested, on
# a table each test writes for itself.


def test_a_scoped_alias_applies_only_to_its_component(tmp_path):
    write_aliases(
        tmp_path,
        "OpsRamp Model,Recipe Model,Component,Notes\n"
        "Alletra Storage Server 4120,DL380 Gen11,iLO,borrow the iLO 6 target\n",
    )
    assert aliases.aliases_for("Alletra Storage Server 4120", "ilo") == ["DL380 Gen11"]
    assert aliases.aliases_for("Alletra Storage Server 4120", "bios") == []
    assert aliases.aliases_for("Alletra Storage Server 4120", None) == []


def test_an_unscoped_alias_applies_to_every_component(tmp_path):
    write_aliases(
        tmp_path,
        "OpsRamp Model,Recipe Model,Component,Notes\nG620,SN6600B,,verified\n",
    )
    for component in ("switch_firmware", "bios", None):
        assert aliases.aliases_for("G620", component) == ["SN6600B"]


def test_a_file_without_the_component_column_still_loads(tmp_path):
    """The original three-column form stays valid."""
    write_aliases(tmp_path, "OpsRamp Model,Recipe Model,Notes\nG620,SN6600B,verified\n")
    assert aliases.aliases_for("G620", "switch_firmware") == ["SN6600B"]


def test_a_file_with_no_header_at_all_still_loads(tmp_path):
    write_aliases(tmp_path, "G620,SN6600B,verified\n")
    assert aliases.aliases_for("G620", "switch_firmware") == ["SN6600B"]


def test_an_unknown_component_scope_is_rejected(tmp_path):
    """Better to drop the alias than to apply it to everything by accident."""
    write_aliases(
        tmp_path,
        "OpsRamp Model,Recipe Model,Component,Notes\n"
        "G620,SN6600B,Flux Capacitor,typo\n",
    )
    assert aliases.aliases_for("G620", "switch_firmware") == []
    assert aliases.aliases_for("G620", None) == []


def test_a_scoped_alias_fills_one_gap_and_leaves_the_rest(tmp_path):
    write_aliases(
        tmp_path,
        "OpsRamp Model,Recipe Model,Component,Notes\n"
        "Alletra Storage Server 4120,DL380 Gen11,iLO,borrow the iLO 6 target\n",
    )
    index = RecipeIndex(
        [
            entry("Alletra", "Alletra 4120", "Server", "BIOS", "3.00", row=1),
            entry("HPE DL", "DL380 Gen11", "Server", "BIOS", "U54 v3.00", row=2),
            entry("HPE DL", "DL380 Gen11", "Server", "iLO", "1.77", row=3),
        ]
    )
    subject = asset("alletra_server", "Alletra Storage Server 4120", "server")

    ilo = index.match(subject, "ilo")
    assert ilo.entry.target_version == "1.77"
    assert ilo.level == LEVEL_ALIAS

    bios = index.match(subject, "bios")
    assert bios.entry.target_version == "3.00", "its own BIOS target must win"
    assert bios.level != LEVEL_ALIAS


def test_a_shared_model_code_cannot_borrow_across_generations(tmp_path):
    """"DL380" is common to Gen11 and Gen10; the generation must still agree."""
    write_aliases(
        tmp_path,
        "OpsRamp Model,Recipe Model,Component,Notes\n"
        "Alletra Storage Server 4120,DL380 Gen11,iLO,\n",
    )
    index = RecipeIndex(
        [
            entry("HPE DL", "DL380 Gen11", "Server", "iLO", "1.77", row=1),
            entry("HPE DL", "DL380 Gen10", "Server", "iLO", "3.20", row=2),
            entry("HPE DL", "DL380 Gen10 Plus", "Server", "iLO", "3.13", row=3),
        ]
    )
    match = index.match(asset("alletra_server", "Alletra Storage Server 4120", "server"), "ilo")
    assert match.entry is not None, "the Gen11 row should still be found"
    assert match.entry.target_version == "1.77"
    assert not match.ambiguous


def test_an_alias_may_point_at_another_platform(tmp_path):
    """The whole purpose of a scoped alias: the row sits under HPE DL."""
    write_aliases(
        tmp_path,
        "OpsRamp Model,Recipe Model,Component,Notes\n"
        "Alletra Storage Server 4120,DL380 Gen11,iLO,\n",
    )
    index = RecipeIndex([entry("HPE DL", "DL380 Gen11", "Server", "iLO", "1.77")])
    match = index.match(asset("alletra_server", "Alletra Storage Server 4120", "server"), "ilo")
    assert match.entry.target_version == "1.77"


def test_an_alias_still_cannot_cross_a_category(tmp_path):
    write_aliases(
        tmp_path,
        "OpsRamp Model,Recipe Model,Component,Notes\n"
        "Alletra Storage Server 4120,DL380 Gen11,iLO,\n",
    )
    index = RecipeIndex([entry("HPE DL", "DL380 Gen11", "Server", "iLO", "1.77")])
    match = index.match(asset("alletra_server", "Alletra Storage Server 4120", "storage"), "ilo")
    assert match.entry is None
