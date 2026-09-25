"""The library of approved recipes.

An estate rarely runs one baseline. The library holds several under names the
operator chooses, and every account is measured against all of them, so which
one an account follows becomes a reading rather than an assumption.
"""
import pytest

from recipes import RecipeError, RecipeLibrary, clean_name
from store import Database


class FakeConfig:
    def __init__(self, tmp_path, default=""):
        self.UPLOAD_DIR = tmp_path / "var" / "uploads"
        self.DEFAULT_RECIPE_PATH = default


@pytest.fixture
def library(tmp_path):
    config = FakeConfig(tmp_path)
    return RecipeLibrary(config, Database(tmp_path / "cache.db"))


def make_file(tmp_path, name="recipe.csv", body="Platform,Component\nHPE DL,BIOS\n"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------------------------- adoption


def test_the_library_starts_empty(library):
    assert library.all() == []
    assert library.describe()["configured"] is False


def test_an_added_recipe_is_measured_against(library, tmp_path):
    ref, replaced = library.add(make_file(tmp_path), "approved.csv", "Q3 baseline", 44,
                                "operator")
    assert replaced is False
    assert ref.name == "Q3 baseline"

    held = library.all()
    assert len(held) == 1
    assert held[0].name == "Q3 baseline"
    assert held[0].filename == "approved.csv"
    assert held[0].targets == 44
    assert held[0].uploaded_by == "operator"
    assert held[0].source == "upload"


def test_several_recipes_are_held_in_the_order_they_arrived(library, tmp_path):
    library.add(make_file(tmp_path, "a.csv"), "a.csv", "Current", 1)
    library.add(make_file(tmp_path, "b.csv"), "b.csv", "Previous", 2)
    library.add(make_file(tmp_path, "c.csv"), "c.csv", "Pinned", 3)
    assert [r.name for r in library.all()] == ["Current", "Previous", "Pinned"]


def test_the_original_file_is_copied_not_referenced(library, tmp_path):
    source = make_file(tmp_path)
    library.add(source, "approved.csv", "Current", 1)
    source.unlink()
    assert library.all()[0].path.exists(), (
        "the recipe must survive the upload being cleaned up"
    )


def test_it_survives_a_restart(tmp_path):
    config = FakeConfig(tmp_path)
    db = Database(tmp_path / "cache.db")
    RecipeLibrary(config, db).add(make_file(tmp_path), "approved.csv", "Current", 7)

    # A second instance over the same files stands for a restart.
    reopened = RecipeLibrary(config, Database(tmp_path / "cache.db")).all()
    assert [(r.name, r.targets) for r in reopened] == [("Current", 7)]


# ------------------------------------------------------------------ replacing


def test_the_same_name_replaces_rather_than_duplicates(library, tmp_path):
    library.add(make_file(tmp_path, "march.csv"), "march.csv", "Current", 10)
    ref, replaced = library.add(
        make_file(tmp_path, "april.csv"), "april.csv", "Current", 12
    )
    assert replaced is True, "re-uploading this month's revision is the normal case"
    held = library.all()
    assert len(held) == 1
    assert held[0].filename == "april.csv" and held[0].targets == 12


def test_a_replacement_in_another_format_leaves_no_stale_file(library, tmp_path):
    library.add(make_file(tmp_path, "one.csv"), "one.csv", "Current", 1)
    library.add(make_file(tmp_path, "two.xlsx", "x"), "two.xlsx", "Current", 2)
    held = library.all()
    assert [p.suffix for p in held[0].paths] == [".xlsx"], (
        "reading the replaced file alongside the replacement would compare "
        "against a mixture of two releases"
    )


def test_a_revision_of_fewer_files_leaves_none_of_the_previous_one(library, tmp_path):
    library.add(
        [(make_file(tmp_path, "cdc.csv"), "cdc.csv"),
         (make_file(tmp_path, "s4.csv"), "s4.csv")],
        name="Current", targets=2,
    )
    assert len(library.all()[0].paths) == 2
    library.add([(make_file(tmp_path, "both.csv"), "both.csv")], name="Current",
                targets=1)
    assert len(library.all()[0].paths) == 1


def test_several_files_make_one_recipe(library, tmp_path):
    ref, _ = library.add(
        [(make_file(tmp_path, "cdc.pdf"), "cdc.pdf"),
         (make_file(tmp_path, "s4hana.pdf"), "s4hana.pdf")],
        name="February baseline", targets=47,
    )
    assert ref.filename == "cdc.pdf, s4hana.pdf"
    held = library.all()
    assert len(held) == 1, "two documents of one release are one baseline"
    assert len(held[0].paths) == 2
    assert held[0].public()["files"] == 2


def test_the_files_of_a_recipe_keep_their_order(library, tmp_path):
    library.add(
        [(make_file(tmp_path, "a.csv"), "first.csv"),
         (make_file(tmp_path, "b.pdf"), "second.pdf"),
         (make_file(tmp_path, "c.csv"), "third.csv")],
        name="Current", targets=3,
    )
    assert [p.suffix for p in library.all()[0].paths] == [".csv", ".pdf", ".csv"]


def test_adding_with_no_file_is_refused(library):
    with pytest.raises(RecipeError, match="No recipe file"):
        library.add([], name="Current", targets=0)


def test_a_name_that_differs_only_in_case_is_the_same_recipe(library, tmp_path):
    library.add(make_file(tmp_path, "a.csv"), "a.csv", "Current", 1)
    _ref, replaced = library.add(make_file(tmp_path, "b.csv"), "b.csv", "CURRENT", 2)
    assert replaced is True
    assert len(library.all()) == 1


# -------------------------------------------------------------------- naming


def test_a_blank_name_falls_back_to_the_file(library, tmp_path):
    ref, _ = library.add(make_file(tmp_path, "q3_baseline.csv"), "q3_baseline.csv", "", 1)
    assert ref.name == "q3 baseline"


def test_a_name_is_trimmed_and_collapsed():
    assert clean_name("  Q3   baseline \n") == "Q3 baseline"
    assert len(clean_name("x" * 200)) == 60


def test_renaming_keeps_the_figures_attached_to_the_same_recipe(library, tmp_path):
    ref, _ = library.add(make_file(tmp_path), "a.csv", "Currnt", 1)
    renamed = library.rename(ref.id, "Current")
    assert renamed.id == ref.id, "the id is what cached measurements are keyed on"
    assert [r.name for r in library.all()] == ["Current"]


def test_renaming_onto_another_recipe_is_refused(library, tmp_path):
    library.add(make_file(tmp_path, "a.csv"), "a.csv", "Current", 1)
    second, _ = library.add(make_file(tmp_path, "b.csv"), "b.csv", "Previous", 1)
    with pytest.raises(RecipeError, match="already called"):
        library.rename(second.id, "Current")


def test_renaming_something_that_is_gone_is_refused(library):
    with pytest.raises(RecipeError, match="no longer"):
        library.rename("nope", "Current")


# -------------------------------------------------------------------- removal


def test_removing_a_recipe_takes_its_file_with_it(library, tmp_path):
    ref, _ = library.add(make_file(tmp_path), "a.csv", "Current", 1)
    assert library.remove(ref.id) == "Current"
    assert library.all() == []
    assert not ref.path.exists()


def test_removing_something_that_is_gone_is_not_an_error(library):
    assert library.remove("nope") == ""


# ------------------------------------------------------- the configured seed


def test_the_configured_path_stands_in_when_nothing_is_uploaded(tmp_path):
    configured = make_file(tmp_path, "seed.csv")
    library = RecipeLibrary(
        FakeConfig(tmp_path, str(configured)), Database(tmp_path / "cache.db")
    )
    held = library.all()
    assert len(held) == 1
    assert held[0].source == "configured"
    assert library.describe()["seeded"] is True


def test_a_configured_path_that_does_not_exist_is_not_used(tmp_path):
    library = RecipeLibrary(
        FakeConfig(tmp_path, str(tmp_path / "gone.csv")), Database(tmp_path / "c.db")
    )
    assert library.all() == []


def test_an_upload_takes_over_from_the_configured_path(tmp_path):
    configured = make_file(tmp_path, "seed.csv")
    library = RecipeLibrary(
        FakeConfig(tmp_path, str(configured)), Database(tmp_path / "cache.db")
    )
    library.add(make_file(tmp_path, "uploaded.csv"), "uploaded.csv", "Current", 5)
    assert [(r.name, r.source) for r in library.all()] == [("Current", "upload")]


# ------------------------------------------------------------------ recovery


def test_a_recipe_whose_file_vanished_is_ignored(library, tmp_path):
    ref, _ = library.add(make_file(tmp_path), "a.csv", "Current", 1)
    ref.path.unlink()
    assert library.all() == [], "a dangling pointer must not be reported as a recipe"


def test_the_previous_single_recipe_is_carried_over(tmp_path):
    """Upgrading from the release that held exactly one approved recipe."""
    import json

    legacy = tmp_path / "var" / "recipe"
    legacy.mkdir(parents=True)
    (legacy / "active.csv").write_text("Platform,Component\nHPE DL,BIOS\n", encoding="utf-8")
    (legacy / "active.json").write_text(
        json.dumps({"filename": "approved_2026_03.csv", "stored": "active.csv",
                    "targets": 44, "uploaded_by": "operator"}),
        encoding="utf-8",
    )

    library = RecipeLibrary(FakeConfig(tmp_path), Database(tmp_path / "cache.db"))
    held = library.all()
    assert len(held) == 1, "an upgrade must not silently empty the matrix"
    assert held[0].filename == "approved_2026_03.csv"
    assert held[0].targets == 44
    assert not (legacy / "active.json").exists(), "and it is not carried over twice"
