"""The background sweep that fills the compliance matrix.

It exists so the home page is populated on arrival, with no tenant chosen and
no recipe uploaded. Two properties matter enough to pin down here: an account
is read from OpsRamp **once** however many recipes it is measured against, and
what was measured is cached, so signing in shows the grid rather than a spinner.

The OpsRamp read and the comparison itself are stubbed - both are covered by
their own tests - so what is exercised here is the orchestration.
"""
import threading
import time
from types import SimpleNamespace

import pytest

import overview
from firmware.models import Asset, ComparisonRow, InstalledComponent
from opsramp.client import OpsRampError
from overview import OverviewStore
from recipes import RecipeLibrary
from service import Extraction
from store import Database


class FakeConfig:
    OVERVIEW_CATEGORIES = ["all"]
    OVERVIEW_REFRESH_MINUTES = 0
    OVERVIEW_REFRESH_ON_START = False
    OVERVIEW_MAX_TENANTS = 0
    VERSION_COMPARE_POLICY = "at_least"

    def __init__(self, tmp_path, default_recipe=""):
        self.UPLOAD_DIR = tmp_path / "var" / "uploads"
        self.DEFAULT_RECIPE_PATH = default_recipe


class FakeResult:
    def __init__(self, rows):
        self.rows = rows


class FakeJob:
    _next = 0

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        FakeJob._next += 1
        self.id = f"job-{kwargs.get('tenant_id', 'x')}-{kwargs.get('recipe_name', '')}"
        self.result = None
        self.error = ""
        self.finished_at = None


class FakeJobs:
    def create(self, **kwargs):
        return FakeJob(**kwargs)

    def adopt(self, job, result):
        job.result = result
        job.finished_at = time.time()


def row(tenant, category, result, asset="a1", component="BIOS", target="U54 v3.00"):
    return ComparisonRow(
        tenant=tenant,
        category=category,
        result=result,
        asset_id=asset,
        hostname=asset,
        component=component,
        target_version=target,
    )


TENANTS = [
    {"id": "t1", "name": "SAP-one"},
    {"id": "t2", "name": "SAP-two"},
]

# (tenant, recipe) -> the rows that comparison produces.
ROWS = {
    ("SAP-one", "Current"): [
        row("SAP-one", "Server", "UPDATED", "s1"),
        row("SAP-one", "Server", "NEEDS UPDATE", "s2"),
        row("SAP-one", "Switch", "UPDATED", "w1"),
    ],
    ("SAP-two", "Current"): [
        row("SAP-two", "Storage", "NOT FOUND IN RECIPE", "t1", target=""),
    ],
}

RECIPE_CSV = (
    "Platform,Model,Category,Component,Target Version\n"
    "HPE DL,DL380 Gen11,Server,BIOS,U54 v3.00\n"
)


def extraction_for(tenant_id, tenant_name, installed=(("bios", "1.0"),)):
    """A real Extraction, so storing and reloading it is exercised for real."""
    asset = Asset(
        tenant=tenant_name, tenant_id=tenant_id, asset_id="asset-1",
        hostname=f"{tenant_name}-host1", platform_key="hpe_dl",
        model_key="dl380_gen11", category="server",
    )
    return Extraction(
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        category=["all"],
        components=[
            InstalledComponent(
                asset=asset, component_key=key, component=key.upper(),
                installed_version=version,
            )
            for key, version in installed
        ],
        raw_rows=[],
        assets_total=1,
        assets_matched=1,
    )


@pytest.fixture
def pipeline(monkeypatch):
    """Stand in for the OpsRamp read and the comparison."""
    state = SimpleNamespace(
        rows=dict(ROWS), fail=set(), crash=set(), read=[], compared=[],
        installed={},  # tenant name -> the components it reports
        clients=set(), inside=0, peak=0, hold=0.0,
    )
    guard = threading.Lock()

    def extract_tenant(client, *, tenant_id, tenant_name, category, progress=None):
        with guard:
            state.read.append(tenant_name)
            state.clients.add(id(client))
            state.inside += 1
            state.peak = max(state.peak, state.inside)
        try:
            if state.hold:
                time.sleep(state.hold)
            if tenant_name in state.crash:
                raise RuntimeError("something unforeseen")
            if tenant_name in state.fail:
                raise OpsRampError("OpsRamp returned HTTP 500.")
            if progress:
                progress({"message": "Discovering assets"})
            return extraction_for(
                tenant_id, tenant_name,
                state.installed.get(tenant_name, (("bios", "1.0"),)),
            )
        finally:
            with guard:
                state.inside -= 1

    def compare_extraction(extraction, *, entries, recipe_name, policy="at_least"):
        with guard:
            state.compared.append((extraction.tenant_name, recipe_name))
        return FakeResult(state.rows.get((extraction.tenant_name, recipe_name), []))

    monkeypatch.setattr(overview, "extract_tenant", extract_tenant)
    monkeypatch.setattr(overview, "compare_extraction", compare_extraction)
    return state


def build(tmp_path, tenants=TENANTS, recipes=("Current",), bodies=None, **overrides):
    config = FakeConfig(tmp_path)
    for key, value in overrides.items():
        setattr(config, key, value)

    db = Database(tmp_path / "cache.db")
    library = RecipeLibrary(config, db)
    for name in recipes:
        body = (bodies or {}).get(name, RECIPE_CSV)
        source = tmp_path / f"{name}.csv"
        source.write_text(body, encoding="utf-8")
        library.add(source, f"{name}.csv", name, 1)

    store = OverviewStore(
        config,
        FakeJobs(),
        lambda: object(),
        lambda client: tenants,
        library=library,
        db=db,
    )
    store.test_config = config
    store.test_db = db
    store.test_library = library
    return store


def wait(store, timeout=5.0):
    """Block until whatever the store is doing finishes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = store.snapshot()
        if snapshot["status"] in ("done", "error"):
            return snapshot
        time.sleep(0.01)
    raise AssertionError("the sweep did not finish")


def sweep(store, timeout=5.0):
    assert store.refresh() is True
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = store.snapshot()
        if snapshot["status"] in ("done", "error"):
            return snapshot
        time.sleep(0.01)
    raise AssertionError("the sweep did not finish")


def find(snapshot, name):
    return next(t for t in snapshot["tenants"] if t["name"] == name)


# ----------------------------------------------------------------- happy path


def test_every_account_is_measured_without_being_chosen(tmp_path, pipeline):
    snapshot = sweep(build(tmp_path))
    assert snapshot["status"] == "done"
    assert sorted(pipeline.read) == ["SAP-one", "SAP-two"]
    assert snapshot["done_count"] == 2
    assert [t["name"] for t in snapshot["tenants"]] == ["SAP-one", "SAP-two"]


def test_cells_are_built_per_category(tmp_path, pipeline):
    snapshot = sweep(build(tmp_path))
    one = find(snapshot, "SAP-one")
    assert one["recipes"][0]["rate"] == pytest.approx(66.7)
    assert one["cells"]["Server"]["recipes"][0]["rate"] == pytest.approx(50.0)
    assert one["cells"]["Switch"]["recipes"][0]["rate"] == 100.0
    assert snapshot["categories"] == ["Server", "Switch", "Storage"]


def test_each_cell_links_to_the_run_behind_it(tmp_path, pipeline):
    snapshot = sweep(build(tmp_path))
    cell = find(snapshot, "SAP-one")["cells"]["Server"]
    assert cell["recipes"][0]["job_id"] == "job-t1-Current"


# ------------------------------------------------------- one read, many recipes


def test_an_account_is_read_once_however_many_recipes(tmp_path, pipeline):
    pipeline.rows.update(
        {
            ("SAP-one", "Legacy"): [row("SAP-one", "Server", "NEEDS UPDATE", "s1")],
            ("SAP-two", "Legacy"): [],
        }
    )
    sweep(build(tmp_path, recipes=("Current", "Legacy")))
    assert sorted(pipeline.read) == ["SAP-one", "SAP-two"], (
        "reading an account twice would double the load on OpsRamp for nothing"
    )
    assert sorted(pipeline.compared) == [
        ("SAP-one", "Current"),
        ("SAP-one", "Legacy"),
        ("SAP-two", "Current"),
        ("SAP-two", "Legacy"),
    ]


def test_a_cell_carries_one_line_per_recipe(tmp_path, pipeline):
    pipeline.rows.update(
        {
            ("SAP-one", "Legacy"): [
                row("SAP-one", "Server", "NEEDS UPDATE", "s1"),
                row("SAP-one", "Server", "NEEDS UPDATE", "s2"),
            ],
            ("SAP-two", "Legacy"): [],
        }
    )
    snapshot = sweep(build(tmp_path, recipes=("Current", "Legacy")))
    cell = find(snapshot, "SAP-one")["cells"]["Server"]
    assert [(r["name"], r["rate"]) for r in cell["recipes"]] == [
        ("Current", pytest.approx(50.0)),
        ("Legacy", 0.0),
    ]


def test_attribution_follows_coverage_not_the_rate(tmp_path, pipeline):
    """A recipe that scores 100% on one component describes nothing."""
    pipeline.rows.update(
        {
            ("SAP-one", "Current"): [
                row("SAP-one", "Server", "UPDATED", "s1"),
                row("SAP-one", "Server", "NEEDS UPDATE", "s2"),
                row("SAP-one", "Server", "NEEDS UPDATE", "s3"),
            ],
            ("SAP-one", "Narrow"): [
                row("SAP-one", "Server", "UPDATED", "s1"),
                row("SAP-one", "Server", "NOT FOUND IN RECIPE", "s2", target=""),
                row("SAP-one", "Server", "NOT FOUND IN RECIPE", "s3", target=""),
            ],
            ("SAP-two", "Narrow"): [],
        }
    )
    snapshot = sweep(build(tmp_path, recipes=("Current", "Narrow")))
    one = find(snapshot, "SAP-one")
    narrow = next(r for r in one["recipes"] if r["name"] == "Narrow")
    current = next(r for r in one["recipes"] if r["name"] == "Current")
    assert narrow["rate"] == 100.0 and current["rate"] == pytest.approx(33.3)
    assert one["best"] == current["id"], (
        "the recipe covering three components beats the one covering one"
    )


def test_a_recipe_added_after_the_sweep_is_flagged_unmeasured(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    source = tmp_path / "later.csv"
    source.write_text(RECIPE_CSV, encoding="utf-8")
    store.test_library.add(source, "later.csv", "Later", 1)
    assert [r["name"] for r in store.snapshot()["unmeasured"]] == ["Later"]


# ------------------------------------------------------- cells without a score


def test_a_cell_with_nothing_comparable_says_why(tmp_path, pipeline):
    snapshot = sweep(build(tmp_path))
    cell = find(snapshot, "SAP-two")["cells"]["Storage"]
    assert cell["comparable_any"] is False
    assert "no approved target" in cell["explain"]
    assert "1 component(s)" in cell["explain"]


def test_a_cell_with_no_version_read_says_so(tmp_path, pipeline):
    pipeline.rows[("SAP-two", "Current")] = [
        row("SAP-two", "Storage", "VERSION NOT DETECTED", "t1"),
        row("SAP-two", "Storage", "VERSION NOT DETECTED", "t2"),
    ]
    snapshot = sweep(build(tmp_path))
    cell = find(snapshot, "SAP-two")["cells"]["Storage"]
    assert "2 with no version read" in cell["explain"]


def test_an_account_with_no_assets_in_a_category_has_no_cell(tmp_path, pipeline):
    """The page renders the absence as words; the store simply omits it."""
    snapshot = sweep(build(tmp_path))
    assert "Switch" not in find(snapshot, "SAP-two")["cells"]


# ----------------------------------------------------------------- the cache


def test_the_matrix_survives_a_restart(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)

    # A second store over the same files stands for the portal being restarted.
    restarted = OverviewStore(
        store.test_config,
        FakeJobs(),
        lambda: object(),
        lambda client: TENANTS,
        library=store.test_library,
        db=store.test_db,
    )
    snapshot = restarted.snapshot()
    assert [t["name"] for t in snapshot["tenants"]] == ["SAP-one", "SAP-two"]
    assert find(snapshot, "SAP-one")["recipes"][0]["rate"] == pytest.approx(66.7)
    assert snapshot["finished_at"] is not None


def test_a_cached_account_is_not_re_read_to_be_displayed(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    pipeline.read.clear()
    store.snapshot()
    assert pipeline.read == [], "the page must render from the cache, not OpsRamp"


def test_figures_for_a_removed_recipe_are_not_shown(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    recipe_id = store.snapshot()["recipes"][0]["id"]
    store.test_library.remove(recipe_id)

    snapshot = store.snapshot()
    assert snapshot["recipes"] == []
    assert find(snapshot, "SAP-one")["recipes"] == [], (
        "a percentage under a name nobody can find is worse than none"
    )


def test_an_account_that_disappears_leaves_the_cache(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    store.tenant_lister = lambda client: [TENANTS[0]]
    snapshot = sweep(store)
    assert [t["name"] for t in snapshot["tenants"]] == ["SAP-one"]
    assert [m["name"] for m in store.test_db.load_measurements()] == ["SAP-one"]


# ------------------------------------------------------------------- failures


def test_one_failing_account_does_not_stop_the_sweep(tmp_path, pipeline):
    pipeline.fail.add("SAP-one")
    snapshot = sweep(build(tmp_path))
    assert snapshot["status"] == "done"
    assert find(snapshot, "SAP-one")["status"] == "error"
    assert "500" in find(snapshot, "SAP-one")["error"]
    assert find(snapshot, "SAP-two")["status"] == "done"


def test_no_recipe_set_is_reported_not_crashed(tmp_path, pipeline):
    snapshot = sweep(build(tmp_path, recipes=()))
    assert snapshot["status"] == "error"
    assert "recipe" in snapshot["error"].lower()
    assert snapshot["configured"] is False


def test_an_invalid_recipe_stops_the_sweep_before_any_read(tmp_path, pipeline):
    store = build(
        tmp_path,
        recipes=("Broken",),
        bodies={
            "Broken": "Platform,Model,Category,Component,Target Version\n"
                      "HPE DL,DL380 Gen11,Server,BIOS,\n"
        },
    )
    snapshot = sweep(store)
    assert snapshot["status"] == "error"
    assert pipeline.read == [], "no account should be read against an invalid recipe"


def test_one_unreadable_recipe_does_not_stop_the_others(tmp_path, pipeline):
    pipeline.rows[("SAP-one", "Broken")] = []
    pipeline.rows[("SAP-two", "Broken")] = []
    store = build(
        tmp_path,
        recipes=("Current", "Broken"),
        bodies={"Broken": "nothing that parses as a recipe\n"},
    )
    snapshot = sweep(store)
    assert snapshot["status"] == "done"
    assert sorted(pipeline.compared) == [("SAP-one", "Current"), ("SAP-two", "Current")]
    assert "Broken" in snapshot["error"], "a skipped recipe has to be visible"


# -------------------------------------------------------------------- limits


def test_the_sweep_can_be_capped(tmp_path, pipeline):
    snapshot = sweep(build(tmp_path, OVERVIEW_MAX_TENANTS=1))
    assert pipeline.read == ["SAP-one"]
    assert snapshot["total_count"] == 1


def test_two_sweeps_cannot_run_at_once(tmp_path, pipeline):
    store = build(tmp_path)
    store.status = "running"
    assert store.refresh() is False


def test_the_snapshot_is_readable_before_any_sweep(tmp_path, pipeline):
    snapshot = build(tmp_path).snapshot()
    assert snapshot["status"] == "idle"
    assert snapshot["tenants"] == []
    assert snapshot["totals"]["rate"] is None


# ------------------------------------------------------- keeping the links alive

# Every cell of the matrix links to the run behind it, and the matrix outlives
# any one run. A pruned job turns a cell into a dead link, so the runs the
# matrix points at are exempt from pruning and released by the sweep itself.


def real_jobs(max_jobs=2):
    from jobs import JobStore

    return JobStore(max_jobs=max_jobs, retention_seconds=0)


def build_with(store_jobs, tmp_path, recipes=("Current", "Legacy")):
    store = build(tmp_path, recipes=recipes)
    store.jobs = store_jobs
    return store


def test_matrix_runs_are_not_pruned_away(tmp_path, pipeline):
    pipeline.rows.update(
        {("SAP-one", "Legacy"): [], ("SAP-two", "Legacy"): []}
    )
    jobs = real_jobs(max_jobs=2)
    store = build_with(jobs, tmp_path)
    snapshot = sweep(store)

    linked = {
        entry["job_id"]
        for row in snapshot["tenants"]
        for entry in row["recipes"]
    }
    assert len(linked) == 4, "two accounts against two recipes"
    for job_id in linked:
        assert jobs.get(job_id) is not None, (
            "a cap of 2 must not turn matrix cells into dead links"
        )


def test_a_re_sweep_lets_go_of_the_runs_it_replaces(tmp_path, pipeline):
    pipeline.rows.update(
        {("SAP-one", "Legacy"): [], ("SAP-two", "Legacy"): []}
    )
    jobs = real_jobs()
    store = build_with(jobs, tmp_path)
    first = sweep(store)
    old = {e["job_id"] for row in first["tenants"] for e in row["recipes"]}

    # Firmware moved on both accounts, so both are measured again.
    pipeline.installed["SAP-one"] = (("bios", "2.0"),)
    pipeline.installed["SAP-two"] = (("bios", "2.0"),)
    second = sweep(store)
    new = {e["job_id"] for row in second["tenants"] for e in row["recipes"]}
    assert old.isdisjoint(new)
    assert all(jobs.get(job_id) is None for job_id in old), (
        "holding every sweep's runs for ever would grow without bound"
    )
    assert all(jobs.get(job_id) is not None for job_id in new)


def test_an_unchanged_account_keeps_the_run_its_cells_point_at(tmp_path, pipeline):
    """A quiet re-scan must not churn the links under a reader's cursor."""
    jobs = real_jobs()
    store = build_with(jobs, tmp_path)
    first = sweep(store)
    before = {e["job_id"] for row in first["tenants"] for e in row["recipes"]}

    second = sweep(store)
    after = {e["job_id"] for row in second["tenants"] for e in row["recipes"]}
    assert before == after
    assert all(jobs.get(job_id) is not None for job_id in after)


# ---------------------------------------------------------------- scheduling


def test_a_scheduled_refresh_is_dropped_while_one_is_running(tmp_path, pipeline):
    """Sweeps run back to back rather than piling up on top of each other."""
    store = build(tmp_path, OVERVIEW_REFRESH_MINUTES=1)
    store.start_background()
    store.status = "running"

    assert store.refresh() is False
    store.stop()


def test_no_timer_is_started_without_a_recipe(tmp_path, pipeline):
    store = build(tmp_path, recipes=(), OVERVIEW_REFRESH_MINUTES=1,
                  OVERVIEW_REFRESH_ON_START=True)
    store.start_background()
    assert store.next_refresh_at is None
    assert [t.name for t in __import__("threading").enumerate()
            if t.name == "overview-timer"] == []


# ------------------------------------------------- reading once, measuring often

# Reading an account is the expensive half and the half that does not change
# when a recipe does. These pin the consequence: a new recipe is answered from
# what is already on disk, and a quiet re-scan leaves settled accounts alone.


def test_a_new_recipe_does_not_re_read_the_estate(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    pipeline.read.clear()
    pipeline.compared.clear()

    source = tmp_path / "second.csv"
    source.write_text(RECIPE_CSV, encoding="utf-8")
    store.test_library.add(source, "second.csv", "Legacy", 1)
    pipeline.rows[("SAP-one", "Legacy")] = [row("SAP-one", "Server", "UPDATED", "s1")]
    pipeline.rows[("SAP-two", "Legacy")] = []

    assert store.recompare() == "started"
    snapshot = wait(store)
    assert pipeline.read == [], (
        "a recipe arriving does not move what is installed, so OpsRamp must "
        "not be queried again"
    )
    assert ("SAP-one", "Legacy") in pipeline.compared
    assert [r["name"] for r in find(snapshot, "SAP-one")["recipes"]] == [
        "Current", "Legacy",
    ]


def test_a_removed_recipe_loses_its_cells(tmp_path, pipeline):
    pipeline.rows[("SAP-one", "Legacy")] = [row("SAP-one", "Server", "UPDATED", "s1")]
    pipeline.rows[("SAP-two", "Legacy")] = []
    store = build(tmp_path, recipes=("Current", "Legacy"))
    sweep(store)
    legacy = next(r for r in store.snapshot()["recipes"] if r["name"] == "Legacy")

    store.test_library.remove(legacy["id"])
    pipeline.read.clear()
    assert store.recompare() == "started"
    snapshot = wait(store)

    assert [r["name"] for r in snapshot["recipes"]] == ["Current"]
    assert [r["name"] for r in find(snapshot, "SAP-one")["recipes"]] == ["Current"]
    for cell in find(snapshot, "SAP-one")["cells"].values():
        assert [r["name"] for r in cell["recipes"]] == ["Current"]
    assert pipeline.read == [], "removing a recipe is not a reason to re-read"


def test_re_measuring_with_nothing_stored_reads_the_estate(tmp_path, pipeline):
    """A fresh install has nothing to measure, so it has to go and look."""
    store = build(tmp_path)
    assert store.recompare() == "started"
    wait(store)
    assert sorted(pipeline.read) == ["SAP-one", "SAP-two"]


# --------------------------------------------------------- the stored reading


def test_what_was_read_is_stored(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    index = {row["tenant_name"]: row for row in store.test_db.inventory_index()}
    assert set(index) == {"SAP-one", "SAP-two"}
    assert index["SAP-one"]["components"] == 1
    assert index["SAP-one"]["fingerprint"]
    assert index["SAP-one"]["bytes"] > 0


def test_an_unchanged_account_is_not_measured_again(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    pipeline.compared.clear()

    snapshot = sweep(store)
    assert sorted(pipeline.read) == ["SAP-one", "SAP-one", "SAP-two", "SAP-two"], (
        "the scan still looks, which is how a change would be noticed"
    )
    assert pipeline.compared == [], (
        "but nothing moved, so nothing needed measuring again"
    )
    assert snapshot["status"] == "done"


def test_an_account_whose_firmware_moved_is_measured_again(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    pipeline.compared.clear()

    pipeline.installed["SAP-one"] = (("bios", "2.0"),)
    sweep(store)
    assert pipeline.compared == [("SAP-one", "Current")], (
        "only the account that moved is re-measured"
    )


def test_a_stored_reading_that_cannot_be_read_is_reported(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    with store.test_db.connect() as connection:
        connection.execute("UPDATE inventory SET payload = ? WHERE tenant_name = ?",
                           (b"not a document", "SAP-one"))

    assert store.recompare() == "started"
    snapshot = wait(store)
    one = find(snapshot, "SAP-one")
    assert one["status"] == "error"
    assert "Refresh" in one["error"]
    assert find(snapshot, "SAP-two")["status"] == "done", "the rest still measure"


# --------------------------------------------- opening a cell after a restart


def test_a_cell_can_still_be_opened_after_a_restart(tmp_path, pipeline):
    from jobs import JobStore

    store = build_with(real_jobs(max_jobs=50), tmp_path)
    snapshot = sweep(store)
    job_id = find(snapshot, "SAP-one")["recipes"][0]["job_id"]

    # A restart: the cache survives on disk, the runs in memory do not.
    restarted = OverviewStore(
        store.test_config,
        JobStore(max_jobs=50, retention_seconds=3600),
        lambda: object(),
        lambda client: TENANTS,
        library=store.test_library,
        db=store.test_db,
    )
    reloaded = restarted.snapshot()
    assert find(reloaded, "SAP-one")["recipes"][0]["job_id"] == job_id

    rebuilt = restarted.rebuild_job(job_id)
    assert rebuilt is not None, (
        "a cached figure a reader cannot click through to is half a feature"
    )
    assert rebuilt.tenant_name == "SAP-one"
    assert rebuilt.result is not None
    assert sorted(pipeline.read) == ["SAP-one", "SAP-two"], "rebuilt from disk, not OpsRamp"


def test_rebuilding_an_unknown_run_is_refused(tmp_path, pipeline):
    store = build(tmp_path)
    sweep(store)
    assert store.rebuild_job("no-such-job") is None


# ------------------------------------------------- saying why a cell has no score

# The two reasons need different people. A missing target is a gap in the
# recipe; a missing version is a gap in what OpsRamp reports. A cell that only
# said "not scored" sent the reader to neither.


def test_a_cell_with_no_approved_target_says_no_target(tmp_path, pipeline):
    pipeline.rows[("SAP-two", "Current")] = [
        row("SAP-two", "Storage", "NOT FOUND IN RECIPE", "st1", target=""),
        row("SAP-two", "Storage", "NOT FOUND IN RECIPE", "st2", target=""),
    ]
    snapshot = sweep(build(tmp_path))
    cell = find(snapshot, "SAP-two")["cells"]["Storage"]
    assert cell["unscored"] == "no target"
    assert "no approved target" in cell["explain"]


def test_a_cell_with_no_version_read_says_no_version(tmp_path, pipeline):
    pipeline.rows[("SAP-two", "Current")] = [
        row("SAP-two", "Storage", "VERSION NOT DETECTED", "st1"),
    ]
    snapshot = sweep(build(tmp_path))
    cell = find(snapshot, "SAP-two")["cells"]["Storage"]
    assert cell["unscored"] == "no version"
    assert "no version read" in cell["explain"]


def test_a_cell_short_of_both_says_so(tmp_path, pipeline):
    pipeline.rows[("SAP-two", "Current")] = [
        row("SAP-two", "Storage", "VERSION NOT DETECTED", "st1"),
        row("SAP-two", "Storage", "NOT FOUND IN RECIPE", "st2", target=""),
    ]
    snapshot = sweep(build(tmp_path))
    assert find(snapshot, "SAP-two")["cells"]["Storage"]["unscored"] == (
        "no target or version"
    )


def test_a_scored_cell_carries_no_excuse(tmp_path, pipeline):
    snapshot = sweep(build(tmp_path))
    cell = find(snapshot, "SAP-one")["cells"]["Server"]
    assert cell["comparable_any"] is True


# ---------------------------------------------------- reading several at once

# Reading an account is mostly waiting on OpsRamp, so the sweep reads several
# at a time. The estate is the only thing that gets faster; the guarantees
# about what is read, and how failures behave, have to survive it.

MANY = [{"id": f"t{i}", "name": f"SAP-{i:02d}"} for i in range(1, 9)]


def test_accounts_are_read_side_by_side(tmp_path, pipeline):
    for tenant in MANY:
        pipeline.rows[(tenant["name"], "Current")] = [
            row(tenant["name"], "Server", "UPDATED", "s1")
        ]
    pipeline.hold = 0.15  # long enough that sequential reads could not overlap
    store = build(tmp_path, tenants=MANY, OVERVIEW_TENANT_WORKERS=4)
    started = time.time()
    snapshot = sweep(store, timeout=20)
    elapsed = time.time() - started

    assert snapshot["done_count"] == len(MANY)
    assert pipeline.peak > 1, "the accounts were read one after another"
    assert pipeline.peak <= 4, "more were read at once than were asked for"
    assert elapsed < len(MANY) * pipeline.hold, (
        "the sweep took as long as reading them one at a time"
    )


def test_each_account_is_still_read_exactly_once(tmp_path, pipeline):
    for tenant in MANY:
        pipeline.rows[(tenant["name"], "Current")] = []
    sweep(build(tmp_path, tenants=MANY, OVERVIEW_TENANT_WORKERS=4), timeout=20)
    assert sorted(pipeline.read) == sorted(t["name"] for t in MANY)


def test_each_worker_gets_its_own_connection(tmp_path, pipeline):
    """A shared client would serialise the workers on its connection pool."""
    for tenant in MANY:
        pipeline.rows[(tenant["name"], "Current")] = []
    pipeline.hold = 0.1
    sweep(build(tmp_path, tenants=MANY, OVERVIEW_TENANT_WORKERS=4), timeout=20)
    assert len(pipeline.clients) > 1


def test_one_account_crashing_does_not_take_the_sweep_down(tmp_path, pipeline):
    for tenant in MANY:
        pipeline.rows[(tenant["name"], "Current")] = []
    pipeline.crash.add("SAP-03")
    snapshot = sweep(build(tmp_path, tenants=MANY, OVERVIEW_TENANT_WORKERS=4),
                     timeout=20)
    assert snapshot["status"] == "done"
    assert find(snapshot, "SAP-03")["status"] == "error"
    assert sum(1 for t in snapshot["tenants"] if t["status"] == "done") == 7


def test_the_progress_count_reaches_the_total(tmp_path, pipeline):
    """With several in flight the count must still be a count, not a place."""
    for tenant in MANY:
        pipeline.rows[(tenant["name"], "Current")] = []
    store = build(tmp_path, tenants=MANY, OVERVIEW_TENANT_WORKERS=3)
    sweep(store, timeout=20)
    assert store._done_count == len(MANY)


def test_re_measuring_stays_sequential(tmp_path, pipeline):
    """It is CPU-bound, so threads would add contention and no speed."""
    store = build(tmp_path, OVERVIEW_TENANT_WORKERS=8)
    sweep(store)
    assert store._worker_count(2, read=False) == 1
    assert store._worker_count(2, read=True) == 2, "never more workers than accounts"


def test_the_estate_can_be_read_one_at_a_time(tmp_path, pipeline):
    for tenant in MANY[:3]:
        pipeline.rows[(tenant["name"], "Current")] = []
    pipeline.hold = 0.05
    sweep(build(tmp_path, tenants=MANY[:3], OVERVIEW_TENANT_WORKERS=1), timeout=20)
    assert pipeline.peak == 1


def test_a_cell_with_no_target_names_the_equipment(tmp_path, pipeline):
    """"3 with no approved target" does not tell anyone what to add."""
    pipeline.rows[("SAP-two", "Current")] = [
        ComparisonRow(tenant="SAP-two", category="Storage",
                      result="NOT FOUND IN RECIPE", asset_id="s1", hostname="s1",
                      component="OS Version", model="HP_3PAR", target_version=""),
        ComparisonRow(tenant="SAP-two", category="Storage",
                      result="NOT FOUND IN RECIPE", asset_id="s2", hostname="s2",
                      component="OS Version", model="HPE_3PAR A630", target_version=""),
    ]
    snapshot = sweep(build(tmp_path))
    cell = find(snapshot, "SAP-two")["cells"]["Storage"]
    assert cell["unscored"] == "no target"
    assert "HP_3PAR" in cell["explain"]
    assert "HPE_3PAR A630" in cell["explain"]


def test_a_scored_cell_names_nothing(tmp_path, pipeline):
    snapshot = sweep(build(tmp_path))
    entry = find(snapshot, "SAP-one")["cells"]["Server"]["recipes"][0]
    assert entry["no_target_models"] == []


# ------------------------------------------- a recipe that arrives mid-sweep

# With a refresh every few minutes a sweep is running much of the time, and it
# measures against the recipes as they were when it began. A recipe uploaded
# during one used to be refused outright and sat out of the grid until someone
# noticed the banner and pressed Refresh.


def test_a_recipe_arriving_during_a_sweep_is_never_refused(tmp_path, pipeline):
    store = build(tmp_path)
    store.status = "running"          # stand in for a sweep in flight
    assert store.recompare() in ("picked-up", "queued")
    assert store.snapshot()["remeasure_queued"] is True


def test_the_queued_measure_runs_when_the_sweep_ends(tmp_path, pipeline):
    pipeline.hold = 0.2
    store = build(tmp_path, tenants=MANY[:4], OVERVIEW_TENANT_WORKERS=1)
    for tenant in MANY[:4]:
        pipeline.rows[(tenant["name"], "Current")] = [
            row(tenant["name"], "Server", "UPDATED", "s1")
        ]

    assert store.refresh() is True
    # Upload lands while the sweep is still working through the accounts.
    for _ in range(200):
        if store.snapshot()["status"] == "running":
            break
        time.sleep(0.01)
    source = tmp_path / "late.csv"
    source.write_text(RECIPE_CSV, encoding="utf-8")
    store.test_library.add(source, "late.csv", "Late", 1)
    for tenant in MANY[:4]:
        pipeline.rows[(tenant["name"], "Late")] = [
            row(tenant["name"], "Server", "NEEDS UPDATE", "s1")
        ]
    assert store.recompare() in ("picked-up", "queued")

    # Nobody presses anything; it has to land on its own.
    deadline = time.time() + 20
    while time.time() < deadline:
        snapshot = store.snapshot()
        if snapshot["status"] == "done" and not snapshot["unmeasured"]:
            break
        time.sleep(0.05)
    snapshot = store.snapshot()
    assert snapshot["unmeasured"] == [], "the new recipe never reached the grid"
    assert [r["name"] for r in find(snapshot, "SAP-01")["recipes"]] == [
        "Current", "Late",
    ]
    assert snapshot["remeasure_queued"] is False


def test_the_queue_holds_one_measure_however_many_recipes_change(tmp_path, pipeline):
    store = build(tmp_path)
    store.status = "running"
    assert store.recompare() in ("picked-up", "queued")
    assert store.recompare() in ("picked-up", "queued")
    assert store._remeasure_wanted is True
    store.status = "idle"
    assert store.recompare() == "started"
    wait(store)


def test_nothing_is_queued_when_the_measure_simply_runs(tmp_path, pipeline):
    store = build(tmp_path)
    assert store.recompare() == "started"
    wait(store)
    assert store.snapshot()["remeasure_queued"] is False


def test_accounts_still_to_come_use_the_new_recipe_at_once(tmp_path, pipeline):
    """The point of the pick-up: no waiting for the whole sweep.

    A sweep measures against the recipes as they were when it began. Reading
    the library again costs almost nothing now that parsed recipes are cached,
    so an account the sweep has not reached yet is measured against the new
    one straight away rather than in some minutes' time.
    """
    pipeline.hold = 0.25
    store = build(tmp_path, tenants=MANY[:6], OVERVIEW_TENANT_WORKERS=1)
    for tenant in MANY[:6]:
        pipeline.rows[(tenant["name"], "Current")] = [
            row(tenant["name"], "Server", "UPDATED", "s1")
        ]
        pipeline.rows[(tenant["name"], "Late")] = [
            row(tenant["name"], "Server", "NEEDS UPDATE", "s1")
        ]

    assert store.refresh() is True
    for _ in range(400):
        if store.snapshot()["status"] == "running":
            break
        time.sleep(0.01)

    source = tmp_path / "late.csv"
    source.write_text(RECIPE_CSV, encoding="utf-8")
    store.test_library.add(source, "late.csv", "Late", 1)
    assert store.recompare() == "picked-up"

    # An account measured after the upload must already carry both recipes,
    # long before the sweep is over.
    deadline = time.time() + 20
    seen_both = False
    while time.time() < deadline:
        snapshot = store.snapshot()
        for tenant in snapshot["tenants"]:
            if [r["name"] for r in tenant["recipes"]] == ["Current", "Late"]:
                seen_both = True
                break
        if seen_both or snapshot["status"] != "running":
            break
        time.sleep(0.02)
    assert seen_both, "no account picked the new recipe up while the sweep ran"

    snapshot = wait(store, timeout=20)
    assert snapshot["unmeasured"] == []
    for tenant in snapshot["tenants"]:
        assert [r["name"] for r in tenant["recipes"]] == ["Current", "Late"]
