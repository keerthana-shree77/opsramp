import pytest

from firmware.comparator import compare_versions, looks_like_version, parse_version
from firmware.models import (
    STATUS_NEEDS_UPDATE,
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_DETECTED,
    STATUS_UNABLE,
    STATUS_UPDATED,
)


@pytest.mark.parametrize(
    "text,release,prefix,build",
    [
        ("1.2.3", (1, 2, 3), None, None),
        ("v1.2.3", (1, 2, 3), None, None),
        ("V1.2.3", (1, 2, 3), None, None),
        ("1.2.3-build123", (1, 2, 3), None, "123"),
        ("U54 v2.10", (2, 10), "u54", None),
        ("2.10", (2, 10), None, None),
        ("8.0.3", (8, 0, 3), None, None),
        ("8.0 Update 3", (8, 0, 3), None, None),
        ("8.0.3 build-24022510", (8, 0, 3), None, "24022510"),
        ("  6.10  ", (6, 10), None, None),
    ],
)
def test_parse_version(text, release, prefix, build):
    parsed = parse_version(text)
    assert parsed.release == release
    assert parsed.prefix == prefix
    assert parsed.build == build


def test_parse_version_rejects_nonsense():
    assert not parse_version("unknown").parsed
    assert not parse_version("").parsed
    assert not parse_version(None).parsed


@pytest.mark.parametrize(
    "installed,target,expected",
    [
        ("1.2.3", "1.2.3", STATUS_UPDATED),
        ("v1.2.3", "1.2.3", STATUS_UPDATED),
        ("1.2.3", "1.2.4", STATUS_NEEDS_UPDATE),
        ("8.0.3", "8.0 Update 3", STATUS_UPDATED),
        ("8.0 Update 2", "8.0.3", STATUS_NEEDS_UPDATE),
        ("U54 v2.10", "U54 v2.12", STATUS_NEEDS_UPDATE),
        ("U54 v2.12", "U54 v2.12", STATUS_UPDATED),
        ("6.10", "6.10", STATUS_UPDATED),
        ("1.2", "1.2.0", STATUS_UPDATED),
        ("2.0", "1.9.9", STATUS_UPDATED),  # newer than target, "at_least" policy
    ],
)
def test_compare_versions(installed, target, expected):
    assert compare_versions(installed, target)["status"] == expected


def test_exact_policy_rejects_newer():
    verdict = compare_versions("2.0", "1.9", policy="exact")
    assert verdict["status"] == STATUS_NEEDS_UPDATE
    assert "exact match" in verdict["reason"]


def test_builds_take_precedence():
    older = compare_versions("8.0.3", "8.0.3", "24022500", "24022510")
    assert older["status"] == STATUS_NEEDS_UPDATE
    same = compare_versions("8.0.3", "8.0.3", "24022510", "24022510")
    assert same["status"] == STATUS_UPDATED


def test_inline_build_is_extracted():
    verdict = compare_versions("8.0.3 build-24022510", "8.0.3", None, "24022510")
    assert verdict["status"] == STATUS_UPDATED
    assert verdict["installed_build"] == "24022510"


def test_a_missing_installed_build_falls_back_to_the_version():
    """The recipe pins a build that the inventory does not report.

    ESXi does this on every host: the build is published in the version
    matrix and is rarely discoverable from OpsRamp. Refusing to answer left
    every ESXi row unscored, when the versions themselves compare perfectly
    well. The verdict is given on the versions and the gap is stated.
    """
    verdict = compare_versions("8.0.3", "8.0.3", None, "24022510")
    assert verdict["status"] == STATUS_UPDATED
    assert "could not be verified" in verdict["reason"]

    older = compare_versions("8.0.2", "8.0.3", None, "24022510")
    assert older["status"] == STATUS_NEEDS_UPDATE
    newer = compare_versions("8.0.4", "8.0.3", None, "24022510")
    assert newer["status"] == STATUS_UPDATED


def test_a_missing_build_with_no_comparable_version_is_still_refused():
    verdict = compare_versions("unknown", "8.0.3", None, "24022510")
    assert verdict["status"] == STATUS_UNABLE
    assert "no installed build" in verdict["reason"]


def test_a_revision_suffix_is_ordered_not_refused():
    """"9.2.2c" against "9.2.2c1" is a release and its patch, not a puzzle."""
    assert compare_versions("9.2.2c", "9.2.2c1")["status"] == STATUS_NEEDS_UPDATE
    assert compare_versions("9.2.2c1", "9.2.2c")["status"] == STATUS_UPDATED
    assert compare_versions("9.2.2c1", "9.2.2c1")["status"] == STATUS_UPDATED
    # Digit runs inside a suffix compare as numbers, so c10 follows c9.
    assert compare_versions("9.2.2c9", "9.2.2c10")["status"] == STATUS_NEEDS_UPDATE
    assert compare_versions("9.2.2c10", "9.2.2c9")["status"] == STATUS_UPDATED


def test_an_esxi_update_letter_is_ordered():
    assert compare_versions("8.0 U3h", "8.0 U3i")["status"] == STATUS_NEEDS_UPDATE
    assert compare_versions("8.0 U3k", "8.0 U3i")["status"] == STATUS_UPDATED


def test_a_suffix_the_recipe_does_not_specify_is_not_held_against_the_asset():
    verdict = compare_versions("2.0.0.U", "2.0.0")
    assert verdict["status"] == STATUS_UPDATED


def test_prefix_mismatch_is_not_compared():
    verdict = compare_versions("U32 v2.12", "U54 v2.12")
    assert verdict["status"] == STATUS_UNABLE
    assert "family" in verdict["reason"]


def test_missing_installed_version():
    verdict = compare_versions("", "1.2.3")
    assert verdict["status"] == STATUS_NOT_DETECTED


def test_not_applicable_target():
    verdict = compare_versions("1.2.3", "N/A")
    assert verdict["status"] == STATUS_NOT_APPLICABLE


def test_uninterpretable_target():
    assert compare_versions("1.2.3", "latest")["status"] == STATUS_UNABLE


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.2.3", True),
        ("U54 v2.12", True),
        ("2.10", True),
        ("8.0.3 build-24022510", True),
        ("10.10.5.21", False),          # an IP address
        ("00:1b:44:11:3a:b7", False),   # a MAC address
        ("2024-03-09", False),          # a date
        ("unknown", False),
        ("", False),
    ],
)
def test_looks_like_version(value, expected):
    assert looks_like_version(value) is expected
