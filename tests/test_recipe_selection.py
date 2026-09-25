"""Choosing which recipe an account is judged by.

An estate runs several approved baselines at once, and the portal measures
every account against all of them - so which one an account is *judged* by is
a choice, not a computation. Left alone it is made automatically, by coverage.
Made by hand it has to hold: it belongs to one account, it survives a restart,
and it cannot quietly start applying to a recipe that has left the library.

What it must never do is change what was compared. The figures for every
recipe are already worked out and stored, which is the whole reason a choice
can be honoured the instant it is made - so a test that finds the comparison
being run again for a choice has found a regression, not an optimisation.
"""
import time

import pytest

from overview import OverviewStore, _in_view, _Ready
from test_overview import (  # the sweep's own scaffolding, stubs and all
    FakeJobs,
    MANY,
    RECIPE_CSV,
    TENANTS,
    build,
    find,
    pipeline,  # noqa: F401 - used as a fixture
    row,
    sweep,
    wait,
)

TWO = ("Current", "Legacy")


def recipe_id(store, name: str) -> str:
    return next(ref.id for ref in store.test_library.all() if ref.name == name)


@pytest.fixture
def two(tmp_path, pipeline):  # noqa: F811
    """Two accounts, two recipes, both measured - the ordinary situation."""
    for tenant in TENANTS:
        pipeline.rows[(tenant["name"], "Current")] = [
            row(tenant["name"], "Server", "UPDATED", "s1"),
            row(tenant["name"], "Server", "NEEDS UPDATE", "s2"),
        ]
        pipeline.rows[(tenant["name"], "Legacy")] = [
            row(tenant["name"], "Server", "UPDATED", "s1"),
            row(tenant["name"], "Server", "UPDATED", "s2"),
        ]
    store = build(tmp_path, recipes=TWO)
    sweep(store)
    return store


# --------------------------------------------------- what the row then shows


def test_the_row_shows_only_the_chosen_recipe(two):
    """The point of choosing: the row answers for that baseline and no other."""
    legacy = recipe_id(two, "Legacy")
    accepted, _state = two.select("t1", [legacy])
    assert accepted
    wait(two)

    one = find(two.snapshot(), "SAP-one")
    assert [entry["name"] for entry in one["shown"]] == ["Legacy"]
    assert one["selected"] == [legacy]
    assert one["automatic"] is False
    assert [entry["name"] for entry in one["cells"]["Server"]["shown"]] == ["Legacy"]


def test_a_choice_belongs_to_one_account_only(two):
    """One account's choice must never reach another account's row.

    Every row is rendered from the same snapshot, so a choice held anywhere
    other than against its own account would leak into the rest of the grid.
    """
    two.select("t1", [recipe_id(two, "Legacy")])
    wait(two)

    snapshot = two.snapshot()
    assert [e["name"] for e in find(snapshot, "SAP-one")["shown"]] == ["Legacy"]
    assert [e["name"] for e in find(snapshot, "SAP-two")["shown"]] == [
        "Current", "Legacy",
    ], "the other account was left on automatic and must still show both"


def test_the_figures_underneath_are_untouched(two):
    """Choosing is a view, so every recipe's line is still there to go back to."""
    two.select("t1", [recipe_id(two, "Legacy")])
    wait(two)
    one = find(two.snapshot(), "SAP-one")
    assert [entry["name"] for entry in one["recipes"]] == ["Current", "Legacy"]


def test_the_pending_count_and_links_follow_the_choice(two):
    """A reader must land on the figures the row is showing them."""
    legacy = recipe_id(two, "Legacy")
    two.select("t1", [legacy])
    wait(two)
    one = find(two.snapshot(), "SAP-one")
    assert one["focus"]["id"] == legacy
    assert one["focus"]["pending"] == 0, "Legacy has everything up to date here"


def test_automatic_still_shows_every_recipe(two):
    one = find(two.snapshot(), "SAP-one")
    assert one["automatic"] is True
    assert [entry["name"] for entry in one["shown"]] == ["Current", "Legacy"]
    assert one["focus"]["id"] == one["best"]


def test_going_back_to_automatic_restores_every_line(two):
    two.select("t1", [recipe_id(two, "Legacy")])
    wait(two)
    accepted, _state = two.select("t1", [])
    assert accepted
    wait(two)

    one = find(two.snapshot(), "SAP-one")
    assert one["automatic"] is True
    assert one["selected"] == []
    assert [entry["name"] for entry in one["shown"]] == ["Current", "Legacy"]


# ------------------------------------------------------------ the choice holds


def test_a_choice_survives_a_restart(two):
    """It is written to the cache database, not held in the session."""
    legacy = recipe_id(two, "Legacy")
    two.select("t1", [legacy])
    wait(two)

    restarted = OverviewStore(
        two.test_config,
        FakeJobs(),
        lambda: object(),
        lambda client: TENANTS,
        library=two.test_library,
        db=two.test_db,
    )
    one = find(restarted.snapshot(), "SAP-one")
    assert one["selected"] == [legacy]
    assert [entry["name"] for entry in one["shown"]] == ["Legacy"]


def test_a_recipe_that_is_not_in_the_library_is_refused(two):
    accepted, state = two.select("t1", ["never-existed"])
    assert (accepted, state) == (False, "unknown")
    assert two.selection("t1") == []


def test_a_choice_naming_a_departed_recipe_falls_back_to_automatic(two):
    """The recipe can go while nobody is looking - the page must still read.

    Pinned to a name that is no longer in the library, the row would be judged
    by nothing and would render as though it had never been measured.
    """
    legacy = recipe_id(two, "Legacy")
    two.select("t1", [legacy])
    wait(two)
    two.test_library.remove(legacy)

    one = find(two.snapshot(), "SAP-one")
    assert one["automatic"] is True
    assert [entry["name"] for entry in one["shown"]] == ["Current"]


def test_removing_a_recipe_releases_the_accounts_pinned_to_it(two):
    legacy = recipe_id(two, "Legacy")
    two.select("t1", [legacy])
    wait(two)

    assert two.forget_recipe(legacy) == 1
    assert two.selection("t1") == []
    assert two.test_db.load_selections() == {}


def test_the_choice_of_a_departed_account_is_forgotten(tmp_path, pipeline):  # noqa: F811
    """An id OpsRamp has stopped reporting must not linger in the database."""
    store = build(tmp_path, recipes=TWO)
    for tenant in TENANTS:
        for name in TWO:
            pipeline.rows[(tenant["name"], name)] = [
                row(tenant["name"], "Server", "UPDATED", "s1")
            ]
    sweep(store)
    store.select("t2", [recipe_id(store, "Legacy")])
    wait(store)
    assert store.test_db.load_selections()

    store.tenant_lister = lambda client: TENANTS[:1]  # SAP-two has gone
    sweep(store)
    assert store.test_db.load_selections() == {}
    assert store.selection("t2") == []


# ------------------------------------------------ nothing is compared again

# The figures for every recipe are already stored, so a choice is answered
# from them. What the choice does trigger is a look at the account itself -
# and that look rewrites the figures only if the firmware has actually moved.


def test_choosing_does_not_compare_anything_again(two, pipeline):  # noqa: F811
    pipeline.compared.clear()
    pipeline.read.clear()
    two.select("t1", [recipe_id(two, "Legacy")])
    wait(two)

    assert pipeline.read == ["SAP-one"], (
        "only the chosen account is looked at, and only that one"
    )
    assert pipeline.compared == [], (
        "the firmware had not moved, so re-comparing it would spend the time "
        "to arrive at the figures already on disk"
    )


def test_an_account_whose_firmware_moved_is_measured_again(two, pipeline):  # noqa: F811
    pipeline.compared.clear()
    pipeline.installed["SAP-one"] = (("bios", "2.0"), ("ilo", "3.1"))
    two.select("t1", [recipe_id(two, "Legacy")])
    wait(two)

    assert sorted(pipeline.compared) == [("SAP-one", "Current"), ("SAP-one", "Legacy")], (
        "the reading changed, so the figures had to be worked out again - "
        "against every recipe, not only the chosen one"
    )


def test_a_check_leaves_the_rest_of_the_grid_alone(two, pipeline):  # noqa: F811
    before = find(two.snapshot(), "SAP-two")
    two.select("t1", [recipe_id(two, "Legacy")])
    wait(two)

    snapshot = two.snapshot()
    assert [t["name"] for t in snapshot["tenants"]] == ["SAP-one", "SAP-two"], (
        "a one-account check must not re-order the matrix"
    )
    after = find(snapshot, "SAP-two")
    assert after["recipes"] == before["recipes"]
    assert after["status"] == "done"


def test_a_one_account_check_does_not_claim_the_estate_was_quiet(two):
    """"23 accounts unchanged" after looking at one would be a lie."""
    two.unchanged = 2
    two.select("t1", [recipe_id(two, "Legacy")])
    wait(two)
    assert two.snapshot()["unchanged"] == 2


def test_choosing_while_a_sweep_runs_does_not_start_a_second_one(two):
    two.status = "running"          # stand in for a sweep in flight
    accepted, state = two.select("t1", [recipe_id(two, "Legacy")])
    assert (accepted, state) == (True, "sweeping"), (
        "a sweep reads every account, so there is nothing to add"
    )
    two.status = "done"
    assert two.selection("t1"), "the choice is kept whatever the sweep is doing"


# ------------------------------------------------------------------- priority

# A sweep over two dozen accounts takes minutes. Somebody who has just chosen
# a recipe for one of them wants that account's figures, not to wait behind
# twenty-three others nobody is looking at.


def test_a_chosen_account_goes_to_the_front_of_a_running_sweep(tmp_path, pipeline):  # noqa: F811
    for tenant in MANY[:6]:
        pipeline.rows[(tenant["name"], "Current")] = [
            row(tenant["name"], "Server", "UPDATED", "s1")
        ]
    pipeline.hold = 0.2  # slow enough that the queue is still long
    store = build(tmp_path, tenants=MANY[:6], OVERVIEW_TENANT_WORKERS=1)

    assert store.refresh() is True
    for _ in range(400):
        if pipeline.read:
            break
        time.sleep(0.01)

    # SAP-06 is last in line; the choice puts it next.
    assert store.focus("t6") == "promoted"
    wait(store, timeout=20)
    assert pipeline.read.index("SAP-06") < 4, (
        "the chosen account waited its turn: " + ", ".join(pipeline.read)
    )
    assert sorted(pipeline.read) == sorted(t["name"] for t in MANY[:6]), (
        "promoting one account must not lose or repeat any of the others"
    )


def test_an_account_the_sweep_has_passed_is_left_to_the_sweep(tmp_path, pipeline):  # noqa: F811
    """It reads every account, so asking for one again would read it twice."""
    for tenant in MANY[:3]:
        pipeline.rows[(tenant["name"], "Current")] = []
    pipeline.hold = 0.15
    store = build(tmp_path, tenants=MANY[:3], OVERVIEW_TENANT_WORKERS=1)
    assert store.refresh() is True
    for _ in range(400):
        if pipeline.read:
            break
        time.sleep(0.01)
    assert store.focus(MANY[0]["id"]) == "sweeping"
    wait(store, timeout=20)
    assert pipeline.read.count("SAP-01") == 1


def test_nothing_running_means_the_account_is_checked_at_once(two, pipeline):  # noqa: F811
    pipeline.read.clear()
    assert two.focus("t1") == "checking"
    wait(two)
    assert pipeline.read == ["SAP-one"]


def test_an_account_opsramp_no_longer_lists_is_not_an_error(two):
    """The grid can outlive the account; a check for one must simply end."""
    assert two.focus("nobody-at-all") == "checking"
    snapshot = wait(two)
    assert snapshot["status"] == "done"
    assert [t["name"] for t in snapshot["tenants"]] == ["SAP-one", "SAP-two"]


# -------------------------------------------------------------- the queue itself


def test_the_queue_hands_out_each_account_once():
    ready = _Ready([{"id": "a"}, {"id": "b"}, {"id": "c"}])
    assert ready.total == 3
    taken = [ready.take(), ready.take(), ready.take()]
    assert [t[1]["id"] for t in taken] == ["a", "b", "c"]
    assert ready.take() is None


def test_the_position_travels_with_the_account():
    """Measuring order is no longer matrix order, so the row index must ride along."""
    ready = _Ready([{"id": "a"}, {"id": "b"}, {"id": "c"}])
    ready.promote("c")
    position, tenant = ready.take()
    assert (position, tenant["id"]) == (2, "c")


def test_promoting_moves_an_account_to_the_front():
    ready = _Ready([{"id": "a"}, {"id": "b"}, {"id": "c"}])
    assert ready.promote("c") is True
    assert [ready.take()[1]["id"] for _ in range(3)] == ["c", "a", "b"]


def test_promoting_one_that_has_been_taken_is_refused():
    ready = _Ready([{"id": "a"}, {"id": "b"}])
    ready.take()
    assert ready.promote("a") is False, (
        "it is already being measured; saying yes would mean nobody follows up"
    )
    assert ready.waiting() == 1


def test_promoting_the_one_already_at_the_front_changes_nothing():
    ready = _Ready([{"id": "a"}, {"id": "b"}])
    assert ready.promote("a") is True
    assert [ready.take()[1]["id"] for _ in range(2)] == ["a", "b"]


# ------------------------------------------------- narrowing a row to a recipe

# _in_view is what decides which lines a row shows. It is given a row exactly
# as the cache holds it, so these are the shapes the page actually renders.

KNOWN = {"cur": "Current", "leg": "Legacy"}


def entry(recipe_id, name, rate, comparable=2, covered=2, components=2,
          not_in_recipe=0, not_detected=0):
    return {
        "id": recipe_id, "name": name, "job_id": "job-" + recipe_id, "rate": rate,
        "tone": "ok", "updated": 1, "needs_update": 1, "not_detected": not_detected,
        "not_in_recipe": not_in_recipe, "comparable": comparable, "covered": covered,
        "coverage": None, "components": components, "pending": 1,
        "no_target_models": [],
    }


def sample_row(**overrides):
    payload = {
        "tenant_id": "t1", "name": "SAP-one", "status": "done", "best": "cur",
        "recipes": [entry("cur", "Current", 50.0), entry("leg", "Legacy", 100.0)],
        "cells": {
            "Server": {
                "assets": 2, "components": 2, "best": "cur",
                "comparable_any": True, "explain": "", "unscored": "",
                "recipes": [entry("cur", "Current", 50.0),
                            entry("leg", "Legacy", 100.0)],
            }
        },
    }
    payload.update(overrides)
    return payload


def test_a_cell_is_rescored_for_the_chosen_recipe_alone():
    """A cell another recipe can score must not look scored under this one."""
    view = _in_view(
        sample_row(
            cells={
                "Storage": {
                    "assets": 3, "components": 3, "best": "leg",
                    "comparable_any": True, "explain": "", "unscored": "",
                    "recipes": [
                        entry("cur", "Current", None, comparable=0, covered=0,
                              components=3, not_in_recipe=3),
                        entry("leg", "Legacy", 100.0, components=3),
                    ],
                }
            }
        ),
        ["cur"],
        KNOWN,
    )
    cell = view["cells"]["Storage"]
    assert cell["comparable_any"] is False, (
        "Legacy could score this cell; Current cannot, and Current is what "
        "was asked for"
    )
    assert cell["unscored"] == "no target"
    assert "no approved target" in cell["explain"]


def test_a_cell_the_chosen_recipe_says_nothing_about_says_no_target():
    """It produced no rows here at all - a gap in the recipe, so name it."""
    view = _in_view(
        sample_row(
            cells={
                "PDU": {
                    "assets": 1, "components": 4, "best": "leg",
                    "comparable_any": True, "explain": "", "unscored": "",
                    "recipes": [entry("leg", "Legacy", 100.0, components=4)],
                }
            }
        ),
        ["cur"],
        KNOWN,
    )
    cell = view["cells"]["PDU"]
    assert cell["shown"] == []
    assert cell["unscored"] == "no target"
    assert "Current has no target" in cell["explain"]


def test_a_choice_with_no_figures_yet_says_so():
    """Chosen moments after being uploaded: the row has no line for it yet.

    An empty row reads as a broken page, so it has to say which recipe it is
    waiting on - the check that puts the figures there is already running.
    """
    view = _in_view(
        sample_row(recipes=[entry("cur", "Current", 50.0)]), ["leg"], KNOWN
    )
    assert view["awaiting"] == ["Legacy"]
    assert view["selected_names"] == ["Legacy"]
    assert view["shown"] == []
    assert view["focus"] is None


def test_an_automatic_row_is_never_awaiting():
    view = _in_view(sample_row(), [], KNOWN)
    assert view["awaiting"] == []
    assert len(view["shown"]) == 2


def test_a_row_measured_against_nothing_renders():
    view = _in_view(sample_row(recipes=[], cells={}), ["cur"], KNOWN)
    assert view["shown"] == [] and view["focus"] is None
    assert view["cells"] == {}


def test_the_stored_row_is_not_altered():
    """The snapshot must not mutate the cached state it was built from."""
    payload = sample_row()
    cells = payload["cells"]["Server"]
    _in_view(payload, ["cur"], KNOWN)
    assert len(cells["recipes"]) == 2
    assert cells["comparable_any"] is True

# ------------------------------------------------- setting every account at once

# Twenty-four dropdowns is twenty-four clicks to answer one question. The
# estate-wide control is the same choice made once, and it overwrites what
# each account was set to individually - which is what "for all accounts" has
# to mean to be worth having.


def test_one_choice_reaches_every_account(two):
    legacy = recipe_id(two, "Legacy")
    accepted, changed, _state = two.select_all([legacy])
    assert accepted and changed == 2
    wait(two)

    snapshot = two.snapshot()
    for account in ("SAP-one", "SAP-two"):
        row = find(snapshot, account)
        assert row["selected"] == [legacy]
        assert [e["name"] for e in row["shown"]] == ["Legacy"]
    assert snapshot["selected_everywhere"] == [legacy]


def test_it_overwrites_what_an_account_was_set_to(two):
    current, legacy = recipe_id(two, "Current"), recipe_id(two, "Legacy")
    two.select("t1", [current])
    wait(two)
    two.select_all([legacy])
    wait(two)
    assert two.selection("t1") == [legacy]


def test_setting_them_all_back_to_automatic(two):
    two.select_all([recipe_id(two, "Legacy")])
    wait(two)
    accepted, changed, _state = two.select_all([])
    assert accepted and changed == 2
    wait(two)

    snapshot = two.snapshot()
    assert snapshot["selected_everywhere"] == []
    assert two.test_db.load_selections() == {}
    for account in ("SAP-one", "SAP-two"):
        assert find(snapshot, account)["automatic"] is True


def test_a_recipe_that_is_not_in_the_library_changes_nothing(two):
    accepted, changed, state = two.select_all(["never-existed"])
    assert (accepted, changed, state) == (False, 0, "unknown")
    assert two.snapshot()["selected_everywhere"] == []


def test_choosing_what_they_are_already_set_to_changes_nothing(two):
    legacy = recipe_id(two, "Legacy")
    two.select_all([legacy])
    wait(two)
    _accepted, changed, _state = two.select_all([legacy])
    assert changed == 0


def test_it_survives_a_restart(two):
    legacy = recipe_id(two, "Legacy")
    two.select_all([legacy])
    wait(two)

    restarted = OverviewStore(
        two.test_config, FakeJobs(), lambda: object(), lambda client: TENANTS,
        library=two.test_library, db=two.test_db,
    )
    assert restarted.snapshot()["selected_everywhere"] == [legacy]


def test_nothing_is_read_again_for_a_choice_the_figures_cover(two, pipeline):  # noqa: F811
    """Every account is already measured against every recipe."""
    pipeline.read.clear()
    pipeline.compared.clear()
    _accepted, _changed, state = two.select_all([recipe_id(two, "Legacy")])
    assert state == "ready", "it took effect from the stored figures"
    assert pipeline.read == [] and pipeline.compared == []


def test_a_recipe_with_no_figures_yet_is_measured_from_what_was_read(
    tmp_path, pipeline  # noqa: F811
):
    """Chosen for everyone moments after being uploaded.

    The estate is not swept for it: what is installed has not moved because a
    recipe arrived, so the stored readings are measured again instead.
    """
    for tenant in TENANTS:
        for name in ("Current", "Late"):
            pipeline.rows[(tenant["name"], name)] = [
                row(tenant["name"], "Server", "UPDATED", "s1")
            ]
    store = build(tmp_path, recipes=("Current",))
    sweep(store)
    pipeline.read.clear()

    source = tmp_path / "late.csv"
    source.write_text(RECIPE_CSV, encoding="utf-8")
    store.test_library.add(source, "late.csv", "Late", 1)
    late = recipe_id(store, "Late")

    accepted, _changed, state = store.select_all([late])
    assert accepted and state in ("started", "queued", "picked-up")
    wait(store, timeout=20)

    assert pipeline.read == [], "no account was read again for a new recipe"
    snapshot = store.snapshot()
    for account in ("SAP-one", "SAP-two"):
        assert [e["name"] for e in find(snapshot, account)["shown"]] == ["Late"]


# ------------------------------------------- what the estate-wide control shows


def test_accounts_that_disagree_read_as_mixed(two):
    two.select("t1", [recipe_id(two, "Legacy")])
    wait(two)
    assert two.snapshot()["selected_everywhere"] is None, (
        "no single option is the truth, and showing one would misrepresent "
        "the other account"
    )


def test_all_on_automatic_is_an_agreement(two):
    assert two.snapshot()["selected_everywhere"] == []


def test_an_empty_grid_has_nothing_to_disagree_about(tmp_path, pipeline):  # noqa: F811
    store = build(tmp_path)
    assert store.snapshot()["selected_everywhere"] == []

# ------------------------------------------- several recipes for one account

# The question is rarely "which one baseline". It is "how does this account
# read against the one we are on and the one we are moving to" - so an
# account holds a set of recipes, and each keeps its own line, its own
# percentage and its own counts. Blending them into one figure would answer
# neither question.


def test_two_recipes_each_keep_their_own_line(two):
    current, legacy = recipe_id(two, "Current"), recipe_id(two, "Legacy")
    accepted, _state = two.select("t1", [current, legacy])
    assert accepted
    wait(two)

    one = find(two.snapshot(), "SAP-one")
    assert [e["name"] for e in one["shown"]] == ["Current", "Legacy"]
    assert [e["rate"] for e in one["shown"]] == [50.0, 100.0], (
        "each recipe answers for itself; there is no blended figure"
    )
    assert [e["name"] for e in one["cells"]["Server"]["shown"]] == [
        "Current", "Legacy",
    ]


def test_the_order_is_the_library_order_however_it_was_ticked(two):
    current, legacy = recipe_id(two, "Current"), recipe_id(two, "Legacy")
    two.select("t1", [legacy, current])
    wait(two)
    assert two.selection("t1") == [current, legacy], (
        "every row reads the same way whatever order the boxes were ticked in"
    )


def test_choosing_a_subset_leaves_the_rest_out(tmp_path, pipeline):  # noqa: F811
    """Three recipes, two chosen: the third has no line in the row."""
    names = ("Current", "Legacy", "Ancient")
    for tenant in TENANTS:
        for name in names:
            pipeline.rows[(tenant["name"], name)] = [
                row(tenant["name"], "Server", "UPDATED", "s1")
            ]
    store = build(tmp_path, recipes=names)
    sweep(store)

    store.select("t1", [recipe_id(store, "Current"), recipe_id(store, "Ancient")])
    wait(store)
    one = find(store.snapshot(), "SAP-one")
    assert [e["name"] for e in one["shown"]] == ["Current", "Ancient"]
    assert [e["name"] for e in one["recipes"]] == ["Current", "Legacy", "Ancient"], (
        "the figures underneath are untouched; this is a view"
    )


def test_one_recipe_that_is_not_in_the_library_refuses_the_whole_choice(two):
    """Storing the half that is valid would be a choice nobody made."""
    legacy = recipe_id(two, "Legacy")
    accepted, state = two.select("t1", [legacy, "never-existed"])
    assert (accepted, state) == (False, "unknown")
    assert two.selection("t1") == []


def test_a_departed_recipe_is_taken_out_and_the_rest_stands(two):
    current, legacy = recipe_id(two, "Current"), recipe_id(two, "Legacy")
    two.select("t1", [current, legacy])
    wait(two)
    two.test_library.remove(legacy)
    two.forget_recipe(legacy)

    assert two.selection("t1") == [current]
    one = find(two.snapshot(), "SAP-one")
    assert one["automatic"] is False
    assert [e["name"] for e in one["shown"]] == ["Current"]


def test_losing_the_only_chosen_recipe_returns_it_to_automatic(two):
    legacy = recipe_id(two, "Legacy")
    two.select("t1", [legacy])
    wait(two)
    two.test_library.remove(legacy)
    two.forget_recipe(legacy)

    assert two.selection("t1") == []
    assert find(two.snapshot(), "SAP-one")["automatic"] is True


def test_the_pending_count_follows_the_closest_of_the_chosen(two):
    """Among the chosen, the one the account actually follows."""
    current, legacy = recipe_id(two, "Current"), recipe_id(two, "Legacy")
    two.select("t1", [current, legacy])
    wait(two)
    one = find(two.snapshot(), "SAP-one")
    assert one["focus"]["id"] == one["best"]
    assert one["best"] in (current, legacy)


def test_choosing_several_compares_nothing_again(two, pipeline):  # noqa: F811
    pipeline.compared.clear()
    two.select("t1", [recipe_id(two, "Current"), recipe_id(two, "Legacy")])
    wait(two)
    assert pipeline.compared == [], (
        "both recipes were already measured; this only chose which to show"
    )


def test_every_account_can_be_set_to_several_at_once(two):
    current, legacy = recipe_id(two, "Current"), recipe_id(two, "Legacy")
    accepted, changed, _state = two.select_all([current, legacy])
    assert accepted and changed == 2
    wait(two)

    snapshot = two.snapshot()
    assert snapshot["selected_everywhere"] == [current, legacy]
    for account in ("SAP-one", "SAP-two"):
        assert [e["name"] for e in find(snapshot, account)["shown"]] == [
            "Current", "Legacy",
        ]


def test_accounts_holding_different_sets_read_as_mixed(two):
    current, legacy = recipe_id(two, "Current"), recipe_id(two, "Legacy")
    two.select("t1", [current, legacy])
    wait(two)
    two.select("t2", [legacy])
    wait(two)
    assert two.snapshot()["selected_everywhere"] is None


# ------------------------------------------ narrowing a cell to several recipes


def test_a_cell_is_rescored_for_the_chosen_recipes_together():
    """Scored if any of them can score it, and explained from those alone."""
    view = _in_view(
        sample_row(
            cells={
                "Storage": {
                    "assets": 3, "components": 3, "best": "leg",
                    "comparable_any": True, "explain": "", "unscored": "",
                    "recipes": [
                        entry("cur", "Current", None, comparable=0, covered=0,
                              components=3, not_in_recipe=3),
                        entry("leg", "Legacy", 100.0, components=3),
                    ],
                }
            }
        ),
        ["cur", "leg"],
        KNOWN,
    )
    cell = view["cells"]["Storage"]
    assert cell["comparable_any"] is True, "Legacy scores it, and Legacy is chosen"
    assert len(cell["shown"]) == 2


def test_a_cell_none_of_the_chosen_recipes_names_says_so():
    view = _in_view(
        sample_row(
            cells={
                "PDU": {
                    "assets": 1, "components": 4, "best": "leg",
                    "comparable_any": True, "explain": "", "unscored": "",
                    "recipes": [entry("leg", "Legacy", 100.0, components=4)],
                }
            }
        ),
        ["cur"],
        KNOWN,
    )
    cell = view["cells"]["PDU"]
    assert cell["shown"] == []
    assert cell["unscored"] == "no target"
    assert "Current has no target" in cell["explain"]


def test_two_chosen_recipes_are_both_named_when_neither_covers_a_cell():
    view = _in_view(
        sample_row(
            cells={
                "PDU": {
                    "assets": 1, "components": 4, "best": "",
                    "comparable_any": False, "explain": "", "unscored": "",
                    "recipes": [],
                }
            }
        ),
        ["cur", "leg"],
        KNOWN,
    )
    explain = view["cells"]["PDU"]["explain"]
    assert "Current" in explain and "Legacy" in explain
    assert "have no target" in explain


def test_a_partly_measured_choice_names_only_what_is_missing():
    view = _in_view(
        sample_row(recipes=[entry("cur", "Current", 50.0)]), ["cur", "leg"], KNOWN
    )
    assert view["awaiting"] == ["Legacy"]
    assert [e["name"] for e in view["shown"]] == ["Current"], (
        "what has been measured is shown while the rest is checked"
    )
