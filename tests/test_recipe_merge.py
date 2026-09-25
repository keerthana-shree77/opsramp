"""One approved baseline that arrives as several files.

The vendor publishes the monthly baseline as two documents - the CDC matrix
and the S4HANA matrix - which describe one set of approved versions between
them. Held as two separate recipes each would score badly on the equipment the
other covers, so they are read together.
"""
import pytest

from recipe import merge
from recipe.merge import merge_recipe_files

CDC = (
    "Platform,Model,Category,Component,Target Version\n"
    "HPE DL,DL380 Gen11,Server,BIOS,U54 v3.00\n"
    "HPE DL,DL380 Gen11,Server,iLO,1.74\n"
)
S4HANA = (
    "Platform,Model,Category,Component,Target Version\n"
    "Switch,8325-32C,Switch,Switch Firmware,10.17.1010\n"
    "Storage,Alletra 9060,Storage,OS Version,9.6.20\n"
)


def write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_two_files_become_one_set_of_targets(tmp_path):
    entries, problem = merge_recipe_files(
        [write(tmp_path, "cdc.csv", CDC), write(tmp_path, "s4.csv", S4HANA)],
        "February baseline",
    )
    assert problem == ""
    assert len(entries) == 4
    assert {e.component_key for e in entries} == {
        "bios", "ilo", "switch_firmware", "os_version",
    }


def test_a_single_file_still_works(tmp_path):
    entries, problem = merge_recipe_files([write(tmp_path, "cdc.csv", CDC)])
    assert problem == "" and len(entries) == 2


def test_the_first_file_wins_a_collision(tmp_path):
    other = CDC.replace("U54 v3.00", "U54 v2.80")
    entries, problem = merge_recipe_files(
        [write(tmp_path, "a.csv", CDC), write(tmp_path, "b.csv", other)]
    )
    assert problem == ""
    assert len(entries) == 2
    bios = next(e for e in entries if e.component_key == "bios")
    assert bios.target_version == "U54 v3.00"


# ------------------------------------------------------------------- refusals


def test_one_unreadable_file_rejects_the_whole_recipe(tmp_path):
    entries, problem = merge_recipe_files(
        [write(tmp_path, "cdc.csv", CDC), write(tmp_path, "junk.csv", "nonsense\n")],
        "February baseline",
    )
    assert entries == [], (
        "accepting the readable half would quietly narrow the baseline, and a "
        "target that has silently gone missing is the failure to avoid"
    )
    assert "junk.csv" in problem


def test_one_invalid_file_rejects_the_whole_recipe(tmp_path):
    broken = "Platform,Model,Category,Component,Target Version\nHPE DL,DL380 Gen11,Server,BIOS,\n"
    entries, problem = merge_recipe_files(
        [write(tmp_path, "cdc.csv", CDC), write(tmp_path, "broken.csv", broken)]
    )
    assert entries == []
    assert "broken.csv" in problem and "Target Version" in problem


def test_no_files_is_reported_not_crashed(tmp_path):
    entries, problem = merge_recipe_files([])
    assert entries == [] and "no file" in problem


def test_a_missing_file_is_reported(tmp_path):
    entries, problem = merge_recipe_files([tmp_path / "gone.csv"])
    assert entries == [] and "gone.csv" in problem


# ------------------------------------------------------- not re-reading a file

# Reading a recipe is the expensive part - a PDF goes through pdfplumber page
# by page - and the same files are read again on every sweep and every
# re-measure. They are cached by their own size and modification time, so a
# file that has changed is never served from the cache: comparing against a
# superseded recipe is the one thing worse than being slow.


@pytest.fixture(autouse=True)
def clean_cache():
    merge.forget_cached_recipes()
    yield
    merge.forget_cached_recipes()


def test_an_unchanged_file_is_not_read_twice(tmp_path, monkeypatch):
    path = write(tmp_path, "cdc.csv", CDC)
    reads = []
    real = merge.load_recipe
    monkeypatch.setattr(
        merge, "load_recipe",
        lambda p, **kw: (reads.append(str(p)), real(p, **kw))[1],
    )
    first, _ = merge_recipe_files([path])
    second, _ = merge_recipe_files([path])
    assert len(reads) == 1, "the file was parsed again for nothing"
    assert [e.component_key for e in first] == [e.component_key for e in second]


def test_a_changed_file_is_read_again(tmp_path):
    path = write(tmp_path, "cdc.csv", CDC)
    before, _ = merge_recipe_files([path])
    assert [e.target_version for e in before] == ["U54 v3.00", "1.74"]

    # The monthly revision, same name and same length of content.
    path.write_text(CDC.replace("U54 v3.00", "U54 v9.99"), encoding="utf-8")
    after, _ = merge_recipe_files([path])
    assert [e.target_version for e in after] == ["U54 v9.99", "1.74"], (
        "a revised recipe served from the cache would measure the estate "
        "against the superseded one"
    )


def test_a_replaced_file_of_the_same_size_is_read_again(tmp_path):
    """The one a size-and-timestamp key would get wrong.

    Written within one tick of the filesystem clock, at exactly the same
    length: only the content itself distinguishes the two.
    """
    path = write(tmp_path, "cdc.csv", CDC)
    merge_recipe_files([path])
    swapped = CDC.replace("1.74", "1.75")
    assert len(swapped) == len(CDC)
    path.write_text(swapped, encoding="utf-8")
    after, _ = merge_recipe_files([path])
    assert "1.75" in [e.target_version for e in after]


def test_each_set_of_files_is_cached_separately(tmp_path):
    one = write(tmp_path, "cdc.csv", CDC)
    two = write(tmp_path, "s4.csv", S4HANA)
    both, _ = merge_recipe_files([one, two])
    just_one, _ = merge_recipe_files([one])
    assert len(both) == 4 and len(just_one) == 2


def test_the_caller_cannot_corrupt_what_the_next_one_gets(tmp_path):
    path = write(tmp_path, "cdc.csv", CDC)
    first, _ = merge_recipe_files([path])
    first.clear()
    second, _ = merge_recipe_files([path])
    assert len(second) == 2, "the cache handed out its own list"


def test_a_missing_file_is_not_cached_as_a_failure(tmp_path):
    """It may appear a moment later; a cached refusal would outlive it."""
    absent = tmp_path / "later.csv"
    entries, problem = merge_recipe_files([absent])
    assert entries == [] and problem
    absent.write_text(CDC, encoding="utf-8")
    entries, problem = merge_recipe_files([absent])
    assert problem == "" and len(entries) == 2
