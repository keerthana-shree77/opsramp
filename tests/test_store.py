"""The cache the matrix is served from.

Nothing in the cache is a source of truth - every row can be rebuilt by
sweeping again - so the behaviour that matters is that a damaged row costs you
that row and not the page.
"""
import json
import threading

import pytest

from store import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "cache.db")


def test_a_measurement_comes_back_as_it_went_in(db):
    payload = {"name": "SAP-one", "status": "done", "recipes": [{"id": "r1", "rate": 42.0}]}
    db.save_measurement("t1", "SAP-one", 0, payload)
    [loaded] = db.load_measurements()
    assert loaded["status"] == "done"
    assert loaded["recipes"] == [{"id": "r1", "rate": 42.0}]
    assert loaded["measured_at"] > 0


def test_measuring_an_account_again_replaces_its_row(db):
    db.save_measurement("t1", "SAP-one", 0, {"status": "error"})
    db.save_measurement("t1", "SAP-one", 0, {"status": "done"})
    loaded = db.load_measurements()
    assert len(loaded) == 1 and loaded[0]["status"] == "done"


def test_accounts_come_back_in_sweep_order(db):
    db.save_measurement("t3", "SAP-three", 2, {})
    db.save_measurement("t1", "SAP-one", 0, {})
    db.save_measurement("t2", "SAP-two", 1, {})
    assert [m["name"] for m in db.load_measurements()] == [
        "SAP-one", "SAP-two", "SAP-three",
    ]


def test_an_unreadable_row_costs_that_row_and_not_the_page(db):
    db.save_measurement("t1", "SAP-one", 0, {"status": "done"})
    db.save_measurement("t2", "SAP-two", 1, {"status": "done"})
    with db.connect() as connection:
        connection.execute(
            "UPDATE measurements SET payload = ? WHERE tenant_id = ?",
            ("{ not json", "t1"),
        )
    loaded = db.load_measurements()
    assert [m["name"] for m in loaded] == ["SAP-two"]


def test_accounts_opsramp_no_longer_lists_are_forgotten(db):
    db.save_measurement("t1", "SAP-one", 0, {})
    db.save_measurement("t2", "SAP-two", 1, {})
    assert db.forget_tenants({"t1"}) == 1
    assert [m["tenant_id"] for m in db.load_measurements()] == ["t1"]


def test_forgetting_everything_empties_the_cache(db):
    db.save_measurement("t1", "SAP-one", 0, {})
    assert db.forget_tenants(set()) == 1
    assert db.load_measurements() == []


def test_meta_values_round_trip(db):
    db.set_meta("sweep", {"finished_at": 12.5, "error": ""})
    assert db.get_meta("sweep") == {"finished_at": 12.5, "error": ""}
    assert db.get_meta("absent", "fallback") == "fallback"


def test_a_corrupt_meta_value_falls_back(db):
    db.set_meta("sweep", {"a": 1})
    with db.connect() as connection:
        connection.execute("UPDATE meta SET value = ? WHERE key = ?", ("{", "sweep"))
    assert db.get_meta("sweep", {}) == {}


# ------------------------------------------------------------------- recipes


def recipe(**overrides):
    fields = {
        "id": "r1", "name": "Current", "filename": "a.csv", "stored": "r1.csv",
        "targets": 10, "uploaded_at": 1.0, "uploaded_by": "operator", "position": 0,
    }
    fields.update(overrides)
    return fields


def test_a_recipe_can_be_found_by_name_whatever_the_case(db):
    db.upsert_recipe(**recipe())
    assert db.find_recipe_by_name("current")["id"] == "r1"
    assert db.find_recipe_by_name("CURRENT")["id"] == "r1"
    assert db.find_recipe_by_name("Previous") is None


def test_upserting_the_same_id_updates_in_place(db):
    db.upsert_recipe(**recipe())
    db.upsert_recipe(**recipe(filename="b.csv", targets=20))
    rows = db.list_recipes()
    assert len(rows) == 1
    assert rows[0]["filename"] == "b.csv" and rows[0]["targets"] == 20


def test_positions_keep_recipes_in_the_order_they_arrived(db):
    db.upsert_recipe(**recipe(id="r1", name="First", position=db.next_recipe_position()))
    db.upsert_recipe(**recipe(id="r2", name="Second", position=db.next_recipe_position()))
    assert [r["name"] for r in db.list_recipes()] == ["First", "Second"]
    assert db.next_recipe_position() == 2


def test_deleting_a_recipe_leaves_the_rest(db):
    db.upsert_recipe(**recipe(id="r1", name="First"))
    db.upsert_recipe(**recipe(id="r2", name="Second", position=1))
    db.delete_recipe("r1")
    assert [r["id"] for r in db.list_recipes()] == ["r2"]


# --------------------------------------------------------------- concurrency


def test_the_sweep_can_write_while_the_page_reads(db):
    """The sweep writes from a worker thread while requests read from Flask's."""
    errors = []

    def write(start):
        try:
            for index in range(start, start + 25):
                db.save_measurement(f"t{index}", f"SAP-{index}", index, {"n": index})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def read():
        try:
            for _ in range(25):
                db.load_measurements()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(0,)),
        threading.Thread(target=write, args=(100,)),
        threading.Thread(target=read),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(db.load_measurements()) == 50


def test_the_database_is_created_on_first_use(tmp_path):
    path = tmp_path / "nested" / "deeper" / "cache.db"
    Database(path).set_meta("a", 1)
    assert path.exists()


def test_the_payload_is_json_not_a_pickle(db):
    """Readable with sqlite3 alone, so the cache can be inspected by hand."""
    db.save_measurement("t1", "SAP-one", 0, {"status": "done"})
    with db.connect() as connection:
        raw = connection.execute("SELECT payload FROM measurements").fetchone()[0]
    assert json.loads(raw) == {"status": "done"}


# ---------------------------------------------- which recipes an account uses

# The operator pins an account to one recipe, or to several at once - reading
# it against this month's baseline and the one it is moving away from side by
# side. It is a choice about what the grid shows, so losing it costs nobody a
# measurement, but it is made once and expected to hold, including over a
# restart, which is why it is here and not in the session.


def test_a_choice_is_stored_and_read_back(db):
    db.save_selection("t1", ["r1"])
    db.save_selection("t2", ["r2"])
    assert db.load_selections() == {"t1": ["r1"], "t2": ["r2"]}


def test_several_recipes_are_held_for_one_account(db):
    db.save_selection("t1", ["r1", "r2", "r3"])
    assert db.load_selections() == {"t1": ["r1", "r2", "r3"]}


def test_choosing_again_replaces_the_whole_choice(db):
    """Not merged with what was there: the ticks are the answer, all of it."""
    db.save_selection("t1", ["r1", "r2"])
    db.save_selection("t1", ["r3"])
    assert db.load_selections() == {"t1": ["r3"]}


def test_the_same_recipe_twice_is_stored_once(db):
    db.save_selection("t1", ["r1", "r1"])
    assert db.load_selections() == {"t1": ["r1"]}


def test_clearing_a_choice_removes_it(db):
    db.save_selection("t1", ["r1", "r2"])
    db.clear_selection("t1")
    assert db.load_selections() == {}


def test_an_empty_choice_is_the_same_as_clearing_it(db):
    db.save_selection("t1", ["r1"])
    db.save_selection("t1", [])
    assert db.load_selections() == {}


def test_clearing_a_choice_that_was_never_made_is_not_an_error(db):
    db.clear_selection("nobody")
    assert db.load_selections() == {}


def test_the_choices_of_departed_accounts_are_dropped(db):
    db.save_selection("t1", ["r1"])
    db.save_selection("t2", ["r1", "r2"])
    assert db.forget_selections({"t1"}) == 2
    assert db.load_selections() == {"t1": ["r1"]}


def test_every_choice_goes_when_no_account_is_kept(db):
    db.save_selection("t1", ["r1"])
    db.forget_selections(set())
    assert db.load_selections() == {}


def test_a_departed_recipe_is_taken_out_of_every_choice(db):
    """The rest of an account's choice stands; only the missing one goes."""
    db.save_selection("t1", ["gone", "kept"])
    db.save_selection("t2", ["gone"])
    db.save_selection("t3", ["kept"])
    assert db.forget_selected_recipe("gone") == 2
    assert db.load_selections() == {"t1": ["kept"], "t3": ["kept"]}


def test_a_choice_made_before_an_account_could_hold_several_is_carried_over(db):
    """The previous release stored one recipe per account, in its own table."""
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO selections (tenant_id, recipe_id, chosen_at, chosen_by) "
            "VALUES ('t1', 'r1', 1.0, 'tester')"
        )
    assert db.load_selections() == {"t1": ["r1"]}


def test_the_carry_over_does_not_overwrite_a_newer_choice(db):
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO selections (tenant_id, recipe_id, chosen_at, chosen_by) "
            "VALUES ('t1', 'old', 1.0, '')"
        )
    db.save_selection("t1", ["new"])
    assert db.load_selections() == {"t1": ["new"]}
