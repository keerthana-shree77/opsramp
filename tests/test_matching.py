import pytest

from firmware.matcher import (
    LEVEL_CODE,
    LEVEL_EXACT,
    LEVEL_FAMILY,
    LEVEL_NORMALIZED,
    RecipeIndex,
)
from firmware.models import Asset
from recipe.normalizer import normalize_row


def entry(platform, model, category, component, version, row=1, **extra):
    raw = {
        "_row": row,
        "platform": platform,
        "model": model,
        "category": category,
        "component": component,
        "target_version": version,
    }
    raw.update(extra)
    normalized, problems = normalize_row(raw)
    assert not problems, problems
    return normalized


def asset(platform_key, model, category="server"):
    from firmware.normalizer import normalize_model

    return Asset(
        tenant="SAP01",
        asset_id="a1",
        hostname="srv001",
        platform_key=platform_key,
        model=model,
        model_key=normalize_model(model),
        category=category,
    )


def test_exact_match_wins():
    index = RecipeIndex(
        [
            entry("HPE DL", "DL380 Gen11", "Server", "BIOS", "U54 v2.12", row=1),
            entry("HPE DL", "DL360 Gen11", "Server", "BIOS", "U55 v2.12", row=2),
        ]
    )
    match = index.match(asset("hpe_dl", "DL380 Gen11"), "bios")
    assert match.level == LEVEL_EXACT
    assert match.entry.target_version == "U54 v2.12"


def test_exact_match_tolerates_vendor_noise_and_spacing_in_the_model():
    """"HPE ProLiant DL380 Gen 11" and "DL380 Gen11" share a model key."""
    index = RecipeIndex([entry("HPE DL", "DL380 Gen11", "Server", "iLO", "6.10")])
    match = index.match(asset("hpe_dl", "HPE ProLiant DL380 Gen 11"), "ilo")
    assert match.level == LEVEL_EXACT
    assert match.entry.target_version == "6.10"


def test_normalized_match_when_the_recipe_model_is_less_specific():
    """A recipe row for "DL380" covers "DL380 Gen11" via token subsetting."""
    index = RecipeIndex([entry("HPE DL", "DL380", "Server", "iLO", "6.10")])
    match = index.match(asset("hpe_dl", "ProLiant DL380 Gen11"), "ilo")
    assert match.level == LEVEL_NORMALIZED
    assert match.entry.target_version == "6.10"


def test_normalized_match_prefers_the_most_specific_row():
    index = RecipeIndex(
        [
            entry("HPE DL", "DL380", "Server", "iLO", "6.00", row=1),
            entry("HPE DL", "DL380 Gen11", "Server", "iLO", "6.10", row=2),
        ]
    )
    match = index.match(asset("hpe_dl", "ProLiant DL380 Gen11"), "ilo")
    assert match.entry.target_version == "6.10"


def test_a_row_for_another_generation_loses_to_a_generic_one():
    """A "Gen11 Plus" is not a "Gen11" - it is different hardware.

    Containment would make the Gen11 row look like the more specific match,
    and that is how a Gen10 Plus machine came to be judged against Gen11
    targets. A row naming no generation covers every one of them, so it is
    the one that applies here.
    """
    index = RecipeIndex(
        [
            entry("HPE DL", "DL380", "Server", "iLO", "6.00", row=1),
            entry("HPE DL", "DL380 Gen11", "Server", "iLO", "6.10", row=2),
        ]
    )
    match = index.match(asset("hpe_dl", "ProLiant DL380 Gen11 Plus"), "ilo")
    assert match.entry.target_version == "6.00"


def test_model_specific_row_does_not_leak_to_another_model():
    index = RecipeIndex([entry("HPE DL", "DL380 Gen11", "Server", "BIOS", "U54 v2.12")])
    match = index.match(asset("hpe_dl", "DL560 Gen11"), "bios")
    assert match.entry is None
    assert "No recipe entry" in match.note


def test_family_row_matches_any_model_of_the_platform():
    index = RecipeIndex(
        [entry("Aruba", "*", "Switch", "Switch Firmware", "10.5.1")]
    )
    match = index.match(asset("switch", "CX 6300", category="switch"), "switch_firmware")
    assert match.level == LEVEL_FAMILY
    assert match.entry.target_version == "10.5.1"


def test_family_row_loses_to_a_specific_row():
    index = RecipeIndex(
        [
            entry("Aruba", "*", "Switch", "Switch Firmware", "10.4.1", row=1),
            entry("Aruba", "CX 8325", "Switch", "Switch Firmware", "10.5.1", row=2),
        ]
    )
    match = index.match(asset("switch", "CX 8325", category="switch"), "switch_firmware")
    assert match.level == LEVEL_EXACT
    assert match.entry.target_version == "10.5.1"


def test_superdome_280_matches_a_family_row_written_for_superdome_flex():
    index = RecipeIndex(
        [entry("Superdome Flex", "*", "Server", "RMC", "3.10.5")]
    )
    match = index.match(asset("superdome_flex_280", "Superdome Flex 280"), "rmc")
    assert match.entry is not None
    assert match.level == LEVEL_FAMILY


def test_category_mismatch_blocks_the_match():
    index = RecipeIndex([entry("Aruba", "CX 8325", "Switch", "Switch Firmware", "10.5.1")])
    match = index.match(asset("switch", "CX 8325", category="storage"), "switch_firmware")
    assert match.entry is None


def test_conflicting_rows_are_reported_as_ambiguous():
    index = RecipeIndex(
        [
            entry("Aruba", "*", "Switch", "Switch Firmware", "10.4.1", row=1),
            entry("Aruba", "", "Switch", "Switch Firmware", "10.5.1", row=2,
                  applies_to_family="Yes"),
        ]
    )
    match = index.match(asset("switch", "CX 6300", category="switch"), "switch_firmware")
    assert match.ambiguous
    assert match.entry is None
    assert "1, 2" in match.note


def test_identical_duplicate_targets_are_not_ambiguous():
    index = RecipeIndex(
        [
            entry("Aruba", "*", "Switch", "Switch Firmware", "10.5.1", row=1),
            entry("Aruba", "", "Switch", "Switch Firmware", "10.5.1", row=2,
                  applies_to_family="Yes"),
        ]
    )
    match = index.match(asset("switch", "CX 6300", category="switch"), "switch_firmware")
    assert not match.ambiguous
    assert match.entry is not None


def test_unknown_component_returns_no_entry():
    index = RecipeIndex([entry("HPE DL", "DL380 Gen11", "Server", "BIOS", "U54 v2.12")])
    match = index.match(asset("hpe_dl", "DL380 Gen11"), "sps")
    assert match.entry is None
    assert "No recipe row defines this component" in match.note


# ------------------------------------------------------- memoised, not changed

# model_tokens, model_codes and norm_text are called once per recipe entry per
# component - tens of thousands of times in a sweep - on the same handful of
# model strings, so they are memoised. What must not change is the answer.

import firmware.normalizer as _norm


def _clear():
    for fn in (_norm._norm_str, _norm._collapse_gen, _norm._model_tokens_str,
               _norm._model_codes_str):
        fn.cache_clear()


MODELS = [
    "ProLiant DL380 Gen11", "ProLiant DL 380 Gen 11", "HPE Superdome Flex 280",
    "SD Flex 280", "8325-32C (JL636A)", "6300M 48G (JL762A)", "HP_3PAR",
    "HPE Alletra Storage MP", "Compute Scale-up Server 3200", "G620", "P9R53A",
    "Fibre Channel B- SN6700B", "", "   ", "Alletra 4120",
]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: repr(m)[:26])
def test_a_memoised_answer_is_the_same_answer(model):
    _clear()
    cold = (_norm.norm_text(model), _norm.model_tokens(model),
            _norm.model_codes(model), _norm.normalize_model(model))
    warm = (_norm.norm_text(model), _norm.model_tokens(model),
            _norm.model_codes(model), _norm.normalize_model(model))
    assert cold == warm
    _clear()
    again = (_norm.norm_text(model), _norm.model_tokens(model),
             _norm.model_codes(model), _norm.normalize_model(model))
    assert cold == again, "the answer depended on whether the cache was warm"


def test_none_and_non_strings_still_work():
    """The signature takes any object; the cache only ever sees a string."""
    _clear()
    assert _norm.norm_text(None) == ""
    assert _norm.model_tokens(None) == ()
    assert _norm.model_codes(None) == frozenset()
    assert _norm.norm_text(8325) == "8325"
    assert _norm.model_tokens(8325) == ("8325",)
    assert _norm.model_codes(8325) == frozenset({"8325"})


def test_a_cached_result_cannot_be_altered_by_a_caller():
    """Everything returned is immutable, so one caller cannot spoil another."""
    _clear()
    tokens = _norm.model_tokens("ProLiant DL380 Gen11")
    codes = _norm.model_codes("8325-32C (JL636A)")
    assert isinstance(tokens, tuple) and isinstance(codes, frozenset)
    assert _norm.model_tokens("ProLiant DL380 Gen11") == tokens


# ------------------------------------------- a shared code is not a shared model

# "DL380" is common to the DL380 Gen10, the Gen10 Plus and the Gen11. They are
# three different machines with three different ROM families, and a recipe that
# names only one of them used to have its targets handed to all three - which
# reads as a real finding while being entirely wrong. On one account this made
# 150 rows say NEEDS UPDATE against a target for other hardware.

GEN11_ONLY = [
    entry("HPE DL", "DL380 Gen11", "Server", "SPS", "6.1.4.89.0", row=1),
    entry("HPE DL", "DL380 Gen11", "Server", "Intelligent Provisioning", "4.35.4",
          row=2),
]


@pytest.mark.parametrize(
    "model", ["ProLiant DL380 Gen10 Plus", "ProLiant DL380 Gen10", "DL380 Gen10 Plus"]
)
def test_another_generation_does_not_take_the_target(model):
    index = RecipeIndex(GEN11_ONLY)
    match = index.match(asset("hpe_dl", model, "server"), "sps")
    assert match.entry is None, (
        f"{model} was given the DL380 Gen11 target on a shared model code"
    )


def test_its_own_generation_still_matches():
    index = RecipeIndex(GEN11_ONLY)
    match = index.match(asset("hpe_dl", "ProLiant DL380 Gen11", "server"), "sps")
    assert match.entry is not None
    assert match.entry.target_version == "6.1.4.89.0"


def test_the_right_generation_is_chosen_when_both_are_present():
    index = RecipeIndex(
        GEN11_ONLY
        + [entry("HPE DL", "DL380 Gen10 Plus", "Server", "SPS", "4.4.4.702", row=3)]
    )
    plus = index.match(asset("hpe_dl", "ProLiant DL380 Gen10 Plus", "server"), "sps")
    gen11 = index.match(asset("hpe_dl", "ProLiant DL380 Gen11", "server"), "sps")
    assert plus.entry.target_version == "4.4.4.702"
    assert gen11.entry.target_version == "6.1.4.89.0"


def test_a_switch_still_matches_on_its_code_alone():
    """What this level exists for: neither side's prose contains the other's.

    "6300M 48-port 1GbE and 4-port SFP6" and "6300M 48G (JL762A)" share only
    the code, and neither names a generation - so the code stands on its own.
    """
    index = RecipeIndex(
        [
            entry("Switch", "6300M 48-port 1GbE and 4-port SFP6", "Switch",
                  "Switch Firmware", "10.15.1020", row=1),
        ]
    )
    match = index.match(asset("switch", "6300M 48G (JL762A)", "switch"),
                        "switch_firmware")
    assert match.entry is not None
    assert match.level == LEVEL_CODE


def test_a_plus_variant_is_not_the_same_as_the_plain_one():
    index = RecipeIndex(
        [entry("HPE DL", "DL380 Gen10", "Server", "SPS", "4.1.5.2", row=1)]
    )
    match = index.match(asset("hpe_dl", "ProLiant DL380 Gen10 Plus", "server"), "sps")
    assert match.entry is None, "Gen10 Plus is not a Gen10"

# ------------------------------------------- one row written for two machines

# The vendor heads a section "HPE ProLiant DL380 Gen10/Gen10 Plus Details" and
# lists one set of targets under it, meaning both machines. Read as a single
# model it names a generation of "gen10 *and* plus", which is neither of them:
# the Gen10 Plus matched by accident of containment, and the plain Gen10 - the
# machine the row is half written for - matched nothing at all and reported no
# approved target.

COMBINED = [
    entry("HPE DL", "DL380 Gen10/Gen10 Plus", "Server", "BIOS", "U46 v2.20", row=1),
    entry("HPE DL", "DL380 Gen10/Gen10 Plus", "Server", "iLO", "3.07", row=2),
]


@pytest.mark.parametrize(
    "model", ["ProLiant DL380 Gen10", "ProLiant DL380 Gen10 Plus", "DL380 Gen10"]
)
def test_a_row_naming_two_generations_covers_both(model):
    index = RecipeIndex(COMBINED)
    match = index.match(asset("hpe_dl", model, "server"), "bios")
    assert match.entry is not None, f"{model} is named in the row and got nothing"
    assert match.entry.target_version == "U46 v2.20"


def test_it_still_covers_neither_of_somebody_else( ):
    """Gen11 is not named in it, and must not be given its targets."""
    index = RecipeIndex(COMBINED)
    match = index.match(asset("hpe_dl", "ProLiant DL380 Gen11", "server"), "bios")
    assert match.entry is None


def test_a_row_of_its_own_still_wins():
    """The combined row must not displace the specific one where both exist."""
    index = RecipeIndex(
        COMBINED
        + [entry("HPE DL", "DL380 Gen10", "Server", "BIOS", "U30 v3.42", row=3)]
    )
    plain = index.match(asset("hpe_dl", "ProLiant DL380 Gen10", "server"), "bios")
    assert plain.entry.target_version == "U30 v3.42"
    assert plain.level == LEVEL_EXACT


def test_a_slash_between_model_names_is_left_alone():
    """"DL360/DL380 Gen11" says nothing reliable about which half is which.

    Only a slash whose every side names a generation is read as two machines;
    anything else keeps the behaviour it had, because guessing at it would
    invent an equivalence the document does not state.
    """
    index = RecipeIndex(
        [entry("HPE DL", "DL360/DL380 Gen11", "Server", "BIOS", "U54 v2.50")]
    )
    match = index.match(asset("hpe_dl", "ProLiant DL360 Gen11", "server"), "bios")
    assert match.entry is None, (
        "reading this as 'DL360 Gen11 and DL380 Gen11' would be a guess"
    )


def test_the_plain_case_is_untouched():
    index = RecipeIndex(
        [entry("HPE DL", "DL380 Gen10 Plus", "Server", "BIOS", "U46 v2.40")]
    )
    assert index.match(
        asset("hpe_dl", "ProLiant DL380 Gen10 Plus", "server"), "bios"
    ).entry is not None
    assert index.match(
        asset("hpe_dl", "ProLiant DL380 Gen10", "server"), "bios"
    ).entry is None
