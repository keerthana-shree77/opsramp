"""The compliance matrix behind the home page.

The matrix is meant to be there when someone arrives - no tenant to choose, no
recipe to upload, no waiting.  Two things make that possible:

* **The cache.**  A sweep touches every asset of every account, which is far
  too expensive to do while a page loads.  Every account is written to the
  cache database as it finishes, and the page renders from the cache.  That is
  also what lets the grid survive a restart of the portal.

* **One read, many recipes.**  Reading an account is expensive; measuring what
  was read against a recipe is not.  So each account is read once and then
  compared against every recipe in the library, which is what makes it possible
  to say which recipe an account actually follows rather than assuming it.

A refresh runs in the background on a timer, and a sweep that is still running
when the timer fires is left alone rather than being started twice.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from firmware import serialise
from firmware.normalizer import CATEGORY_DISPLAY, SELECTABLE_CATEGORIES
from opsramp.client import OpsRampError
from opsramp.tenants import list_tenants
from recipe.merge import merge_recipe_files
from reports import summary as summary_report
from service import compare_extraction, extract_tenant

logger = logging.getLogger(__name__)

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

META_SWEEP = "sweep"


@dataclass
class TenantState:
    """One row of the matrix: one account, measured against every recipe."""

    tenant_id: str
    name: str
    status: str = "pending"        # pending | running | done | error
    error: str = ""
    finished_at: float | None = None
    assets: int = 0
    components: int = 0
    # One entry per recipe, in library order.  See ``_recipe_stats``.
    recipes: list = field(default_factory=list)
    # The recipe this account's firmware most closely follows, by coverage.
    best: str = ""
    cells: dict = field(default_factory=dict)
    # What was installed when this was measured, and what it was measured
    # against. Together they say whether a re-read needs re-measuring at all.
    fingerprint: str = ""
    recipe_stamp: str = ""
    read_at: float | None = None

    def public(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def restore(cls, payload: dict) -> "TenantState":
        state = cls(
            tenant_id=payload.get("tenant_id", ""),
            name=payload.get("name", ""),
        )
        for key, value in payload.items():
            if hasattr(state, key):
                setattr(state, key, value)
        # A cached row was measured; it is never mid-flight.
        if state.status not in ("done", "error"):
            state.status = "done"
        return state


class _Ready:
    """The accounts a sweep still has to measure, in the order it will take them.

    A queue rather than a list handed out up front, for one reason: the order
    is not settled when the sweep begins.  A sweep over two dozen accounts
    takes minutes, and somebody choosing a recipe for one of them wants *that*
    account's figures, not to wait behind twenty-three others that nobody is
    looking at.  So the choice moves it to the front of what is left.

    ``position`` travels with each account because it is where the account
    belongs in the matrix, which is no longer the order it is measured in.
    """

    def __init__(self, tenants: list[dict]) -> None:
        self._items: list[tuple[int, dict]] = list(enumerate(tenants))
        self.total = len(self._items)
        self._lock = threading.Lock()

    def take(self) -> tuple[int, dict] | None:
        with self._lock:
            return self._items.pop(0) if self._items else None

    def promote(self, tenant_id: str) -> bool:
        """Move one account to the front.  False if it is not still waiting."""
        with self._lock:
            for at, (_position, tenant) in enumerate(self._items):
                if tenant["id"] == tenant_id:
                    if at:
                        self._items.insert(0, self._items.pop(at))
                    return True
        return False

    def waiting(self) -> int:
        with self._lock:
            return len(self._items)


class OverviewStore:
    """The matrix: cached in SQLite, refreshed by a background sweep."""

    def __init__(
        self,
        config,
        jobs,
        client_factory,
        tenant_lister=None,
        library=None,
        db=None,
    ) -> None:
        self.config = config
        self.jobs = jobs
        self.client_factory = client_factory
        # Injected so the sweep can be exercised without OpsRamp.
        self.tenant_lister = tenant_lister or list_tenants
        self.library = library
        self.db = db
        self._lock = threading.RLock()
        self._tenants: dict[str, TenantState] = {}
        self._order: list[str] = []
        self.status = STATUS_IDLE
        self.message = ""
        self.error = ""
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.next_refresh_at: float | None = None
        self.skipped_refreshes = 0
        self.unchanged = 0
        self._done_count = 0
        # A recipe changed while a sweep was running: measure again the moment
        # it finishes, rather than leaving the new recipe out of the grid until
        # somebody presses Refresh.
        self._remeasure_wanted = False
        # The recipes the sweep in flight is measuring against, and the stamp
        # that identifies them. Held here rather than passed down, so a recipe
        # arriving mid-sweep is picked up by the accounts still to be measured
        # instead of waiting for the whole sweep to end.
        self._active: tuple[list, str] = ([], "")
        # How many targets each recipe turned out to hold once parsed. The
        # library records this for an upload but cannot know it for a recipe
        # seeded from DEFAULT_RECIPE_PATH, which is never parsed until a sweep.
        self._targets: dict[str, int] = {}
        # Which recipes each account is judged by, where the operator has
        # said. Every account is still measured against every recipe: this
        # decides what the grid shows and what gets checked first, never what
        # is compared. An empty list means automatic - every recipe, with the
        # closest match marked.
        self._selections: dict[str, list[str]] = {}
        # The accounts a sweep in flight has still to reach. Held here so a
        # choice made while it runs can put that account next in line rather
        # than waiting behind two dozen others.
        self._ready: _Ready | None = None
        self._stop = threading.Event()
        self._load_cache()

    # ------------------------------------------------------------------- cache
    def _load_cache(self) -> None:
        """Populate the matrix from the cache, so a fresh sign-in is instant."""
        if self.db is None:
            return
        try:
            cached = self.db.load_measurements()
            sweep = self.db.get_meta(META_SWEEP, {}) or {}
            chosen = self.db.load_selections()
        except Exception as exc:  # noqa: BLE001 - a bad cache must not stop boot
            logger.warning("Could not read the cached matrix: %s", exc)
            return

        with self._lock:
            self._selections = dict(chosen)
            for payload in cached:
                state = TenantState.restore(payload)
                if not state.tenant_id:
                    continue
                self._tenants[state.tenant_id] = state
                self._order.append(state.tenant_id)
            self.finished_at = sweep.get("finished_at")
            self.started_at = sweep.get("started_at")
            self.error = sweep.get("error", "")
            self.status = STATUS_DONE if cached else STATUS_IDLE
        if cached:
            logger.info("Loaded %d cached account(s) into the matrix", len(cached))

    def _remember(self, state: TenantState, position: int) -> None:
        if self.db is None:
            return
        try:
            self.db.save_measurement(
                state.tenant_id, state.name, position, state.public()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not cache %s: %s", state.name, exc)

    def _remember_sweep(self) -> None:
        if self.db is None:
            return
        try:
            self.db.set_meta(
                META_SWEEP,
                {
                    "started_at": self.started_at,
                    "finished_at": self.finished_at,
                    "error": self.error,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record the sweep: %s", exc)

    # ------------------------------------------------------------------ public
    def snapshot(self) -> dict:
        refs = self.library.all() if self.library is not None else []
        known = {ref.id: ref.name for ref in refs}

        with self._lock:
            for ref in refs:
                if not ref.targets:
                    ref.targets = self._targets.get(ref.id, 0)
            rows = [
                _in_view(
                    _prune(self._tenants[t].public(), known),
                    self._selections.get(t, ()),
                    known,
                )
                for t in self._order
                if t in self._tenants
            ]
            chosen = {t: list(v) for t, v in self._selections.items()}
            status = self.status
            message = self.message
            error = self.error
            started_at = self.started_at
            finished_at = self.finished_at
            next_refresh_at = self.next_refresh_at
            skipped = self.skipped_refreshes
            unchanged = self.unchanged
            queued = self._remeasure_wanted
            read_at = max(
                (s.read_at for s in self._tenants.values() if s.read_at), default=None
            )

        present = {c for row in rows for c in row["cells"]}
        categories = [
            CATEGORY_DISPLAY[key]
            for key, _label in SELECTABLE_CATEGORIES
            if key != "all" and CATEGORY_DISPLAY.get(key) in present
        ]
        categories += sorted(present - set(categories))

        measured = {r["id"] for row in rows for r in row["recipes"]}
        return {
            "status": status,
            "message": message,
            "error": error,
            "started_at": started_at,
            "finished_at": finished_at,
            "next_refresh_at": next_refresh_at,
            "skipped_refreshes": skipped,
            # The last sweep left this many accounts untouched because neither
            # their firmware nor the recipes had moved.
            "unchanged": unchanged,
            # A recipe changed mid-sweep; the re-measure runs when it ends.
            "remeasure_queued": queued,
            "read_at": read_at,
            "readings": sum(1 for r in rows if r["read_at"]),
            "recipes": [ref.public() for ref in refs],
            # Only what is still in the library: a choice naming a recipe that
            # has gone is not a choice any more.
            "selections": {
                tenant: [r for r in picked if r in known]
                for tenant, picked in chosen.items()
            },
            # What the estate-wide dropdown should come up on: the recipe
            # every account is set to, or "" when they are all on automatic.
            # None when they disagree, which the page shows as "mixed" rather
            # than picking one of them and misrepresenting the rest.
            "selected_everywhere": _everywhere(rows),
            "categories": categories,
            "tenants": rows,
            "totals": _totals(rows, refs),
            "configured": bool(refs),
            # A recipe added since the last sweep has no figures yet.
            "unmeasured": [
                ref.public() for ref in refs if rows and ref.id not in measured
            ],
            "done_count": sum(1 for r in rows if r["status"] == "done"),
            "total_count": len(rows),
        }

    def refresh(self) -> bool:
        """Read every account from OpsRamp and measure it.

        Returns False when a sweep is already running.
        """
        return self._start(read=True)

    def recompare(self) -> str:
        """Measure the estate again from what was already read.

        A recipe changing is not a reason to query OpsRamp: what is installed
        has not moved. So adding, replacing or removing a recipe re-measures
        the stored readings, which takes seconds and no API calls, instead of
        sweeping the whole estate from the beginning. If nothing has been read
        yet there is nothing to measure, and this becomes a full sweep.

        Returns ``"started"`` or ``"queued"``. A sweep may already be running -
        with a refresh every few minutes that is likely - and it is measuring
        against the recipes as they were when it began, so the new one would
        be missing from the grid until somebody noticed. Rather than refuse,
        the work is remembered and runs the moment that sweep ends.
        """
        if self._start(read=False):
            return "started"

        # A sweep is running and is measuring against the recipes as they were
        # when it began. Re-reading the library costs almost nothing now that
        # parsed recipes are cached, so the accounts it has not reached yet can
        # use the new list immediately; the ones already finished are caught by
        # the re-measure at the end.
        picked_up = self._adopt_recipes_midsweep()
        with self._lock:
            self._remeasure_wanted = True
        logger.info(
            "Recipes changed mid-sweep; %s, and every account will be measured "
            "again when it ends",
            "the accounts still to come will use the new list" if picked_up
            else "the new list could not be read",
        )
        return "picked-up" if picked_up else "queued"

    # ------------------------------------------------------------- selections
    def select(self, tenant_id: str, recipe_ids) -> tuple[bool, str]:
        """Say which recipes one account is judged by.  ``(accepted, state)``.

        Several at once, because that is the question people actually have:
        an account is commonly read against this month's baseline and the one
        it is moving away from, side by side, and each keeps its own figures.

        This is a choice about *presentation and priority*, not about what is
        compared: every account is measured against every recipe in the
        library whatever is chosen here, which is what makes the choice free
        to make and instant to honour - the figures for the recipes just
        chosen have already been worked out and stored.

        The account is then checked straight away all the same, ahead of the
        routine refresh. Its stored figures are shown in the meantime, and the
        check rewrites them only if something has actually moved: the reading
        is fingerprinted, and an account whose firmware has not changed since
        it was last read is left exactly as it was.

        An empty list returns the account to automatic, where it is shown
        against every recipe with the closest match marked.
        """
        wanted, known = _wanted(recipe_ids, self.library)
        if wanted is None:
            return False, "unknown"

        with self._lock:
            already = self._selections.get(tenant_id, [])
            if wanted:
                self._selections[tenant_id] = list(wanted)
            else:
                self._selections.pop(tenant_id, None)
        if self.db is not None and wanted != already:
            try:
                self.db.save_selection(tenant_id, wanted)
            except Exception as exc:  # noqa: BLE001 - the page still works
                logger.warning("Could not store the recipe choice: %s", exc)
        logger.info(
            "Account %s will be shown against %s",
            tenant_id,
            ", ".join(wanted) or "every recipe, closest match marked",
        )
        return True, self.focus(tenant_id)

    def select_all(self, recipe_ids) -> tuple[bool, int, str]:
        """Judge every account by the same recipes.  ``(accepted, changed, state)``.

        The same decision as choosing per account, made once. It is cheap for
        the same reason: every account has already been measured against
        every recipe, so this only settles which figures the grid shows.

        It does not sweep. Reading the estate is the expensive half and
        nothing about it has changed, so where an account has no figures for
        the chosen recipe yet - one uploaded moments ago - the stored
        readings are measured again instead, which costs seconds and no API
        calls. Where every account already has them, nothing runs at all.
        """
        wanted, _known = _wanted(recipe_ids, self.library)
        if wanted is None:
            return False, 0, "unknown"

        with self._lock:
            tenants = list(self._order)
            changed = sum(
                1 for t in tenants if self._selections.get(t, []) != wanted
            )
            for tenant_id in tenants:
                if wanted:
                    self._selections[tenant_id] = list(wanted)
                else:
                    self._selections.pop(tenant_id, None)
            # Which accounts have no figures for one of them, and so need
            # measuring again from what has already been read.
            missing = any(
                any(
                    not any(entry["id"] == chosen for entry in state.recipes)
                    for chosen in wanted
                )
                for tenant_id, state in self._tenants.items()
                if tenant_id in tenants and state.status == "done"
            )

        if self.db is not None:
            try:
                self.db.save_selections({t: list(wanted) for t in tenants})
            except Exception as exc:  # noqa: BLE001 - the page still works
                logger.warning("Could not store the estate-wide choice: %s", exc)

        logger.info(
            "Every account (%d) will be shown against %s",
            len(tenants),
            ", ".join(wanted) or "every recipe, closest match marked",
        )
        if missing:
            return True, changed, self.recompare()
        return True, changed, "ready"

    def selection(self, tenant_id: str) -> list[str]:
        with self._lock:
            return list(self._selections.get(tenant_id, []))

    def forget_recipe(self, recipe_id: str) -> int:
        """Return to automatic every account pinned to a departed recipe."""
        with self._lock:
            gone = [
                tenant for tenant, picked in self._selections.items()
                if recipe_id in picked
            ]
            for tenant_id in gone:
                left = [r for r in self._selections[tenant_id] if r != recipe_id]
                if left:
                    self._selections[tenant_id] = left
                else:
                    self._selections.pop(tenant_id, None)
        if self.db is not None:
            try:
                self.db.forget_selected_recipe(recipe_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not prune the recipe choices: %s", exc)
        if gone:
            logger.info(
                "%d account(s) were pinned to the removed recipe and are back "
                "on the closest match",
                len(gone),
            )
        return len(gone)

    def focus(self, tenant_id: str) -> str:
        """Check one account now, ahead of the routine background refresh.

        Three things can be true, and the caller says which in the message it
        shows.  ``"promoted"``: a sweep is running and has not reached this
        account yet, so it has been moved to the front of the queue.
        ``"checking"``: nothing was running, so this one account is being read
        and measured on its own - seconds, not the minutes a full sweep takes.
        ``"sweeping"``: a sweep is running and is already past this account,
        and it reads every account, so there is nothing to add.
        """
        ready = self._ready
        if ready is not None and ready.promote(tenant_id):
            logger.info("Account %s moved to the front of the sweep", tenant_id)
            return "promoted"
        if self._start(read=True, only={tenant_id}):
            return "checking"
        return "sweeping"

    def _adopt_recipes_midsweep(self) -> bool:
        """Hand the running sweep the current recipe library."""
        refs = self.library.all() if self.library is not None else []
        if not refs:
            return False
        loaded = []
        for ref in refs:
            entries, trouble = merge_recipe_files(ref.paths, ref.filename)
            if trouble:
                continue
            ref.targets = len(entries)
            loaded.append((ref, entries))
        if not loaded:
            return False
        with self._lock:
            self._active = (loaded, _recipe_stamp(loaded))
            self._targets.update({ref.id: len(e) for ref, e in loaded})
        return True

    def _start(self, read: bool, only: set[str] | None = None) -> bool:
        with self._lock:
            if self.status == STATUS_RUNNING:
                return False
            self.status = STATUS_RUNNING
            self.message = "Starting"
            self.error = ""
            self.started_at = time.time()
            self.finished_at = None
        self._stop.clear()
        threading.Thread(
            target=self._sweep, args=(read, only), daemon=True, name="overview"
        ).start()
        return True

    def rebuild_job(self, job_id: str):
        """Recreate a run the matrix links to, from the stored reading.

        Runs live in memory, so every cell of a cached matrix would link to
        nothing after a restart - the figures would survive but clicking one
        would 404. The reading and the recipe are both on disk, so the run can
        simply be made again.
        """
        if self.db is None or self.library is None:
            return None
        try:
            record = self.db.get_run(job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not look up run %s: %s", job_id, exc)
            return None
        if record is None:
            return None

        ref = next(
            (r for r in self.library.all() if r.id == record["recipe_id"]), None
        )
        if ref is None:
            return None
        entries, trouble = merge_recipe_files(ref.paths, ref.filename)
        if trouble:
            logger.warning("Cannot rebuild run %s: %s", job_id, trouble)
            return None

        with self._lock:
            state = self._tenants.get(record["tenant_id"])
        name = state.name if state else record["tenant_id"]
        extraction = self._stored_reading({"id": record["tenant_id"], "name": name})
        if extraction is None:
            return None

        from service import Tenant

        result = compare_extraction(
            extraction,
            entries=entries,
            recipe_name=ref.name,
            policy=getattr(self.config, "VERSION_COMPARE_POLICY", "at_least"),
        )
        categories = list(self.config.OVERVIEW_CATEGORIES or ["all"])
        job = self.jobs.create(
            tenant_id=record["tenant_id"],
            tenant_name=extraction.tenant_name or name,
            category=categories[0],
            recipe_name=ref.name,
            owner="",
            tenants=[Tenant(record["tenant_id"], extraction.tenant_name or name)],
            categories=categories,
        )
        self.jobs.adopt(job, result)
        logger.info("Rebuilt run %s as %s from the stored reading", job_id, job.id)
        return job

    def stop(self) -> None:
        self._stop.set()

    def start_background(self) -> None:
        """Honour OVERVIEW_REFRESH_ON_START and OVERVIEW_REFRESH_MINUTES."""
        if self.library is not None and not self.library.all():
            return
        if self.config.OVERVIEW_REFRESH_ON_START:
            threading.Timer(5.0, self.refresh).start()
        # The timer below is the silent scan of the X axis: it re-reads every
        # account and leaves the ones that have not moved untouched.

        minutes = self.config.OVERVIEW_REFRESH_MINUTES
        if minutes <= 0:
            return

        def tick() -> None:
            while not self._stop.is_set():
                with self._lock:
                    self.next_refresh_at = time.time() + minutes * 60
                if self._stop.wait(minutes * 60):
                    return
                if not self.refresh():
                    # A sweep over a large estate can outlast the interval.
                    # Starting a second one would double the load on OpsRamp
                    # for no extra freshness, so the tick is dropped instead.
                    with self._lock:
                        self.skipped_refreshes += 1
                    logger.info(
                        "Scheduled refresh skipped: the previous sweep is still "
                        "running (%d skipped so far)",
                        self.skipped_refreshes,
                    )

        threading.Thread(target=tick, daemon=True, name="overview-timer").start()
        logger.info("Compliance matrix will refresh every %d minute(s)", minutes)

    # ----------------------------------------------------------------- internal
    def _fail(self, message: str) -> None:
        # Persist before publishing the outcome. A reader that sees the sweep
        # has finished may immediately act on the cache, so the cache has to
        # be the thing that finished - not a write still on its way.
        with self._lock:
            self.error = message
            self.message = ""
            self.finished_at = time.time()
        self._remember_sweep()
        with self._lock:
            self.status = STATUS_ERROR
        logger.error("Compliance sweep failed: %s", message)

    def _load_recipes(self) -> list[tuple] | None:
        """Read and validate every recipe in the library.  ``[(ref, entries)]``.

        A recipe that no longer loads is reported and left out rather than
        stopping the sweep: the other recipes still have something to say.
        """
        refs = self.library.all() if self.library is not None else []
        if not refs:
            self._fail(
                "No approved recipe has been set. Upload one on this page, or "
                "set DEFAULT_RECIPE_PATH to seed it."
            )
            return None

        loaded: list[tuple] = []
        problems: list[str] = []
        for ref in refs:
            entries, trouble = merge_recipe_files(ref.paths, ref.filename)
            if trouble:
                problems.append(f"{ref.name}: {trouble}")
                continue
            ref.targets = len(entries)
            with self._lock:
                self._targets[ref.id] = ref.targets
            loaded.append((ref, entries))

        if not loaded:
            self._fail("No recipe in the library could be read: " + "; ".join(problems))
            return None
        with self._lock:
            self._active = (loaded, _recipe_stamp(loaded))
        if problems:
            with self._lock:
                self.error = "Some recipes were skipped - " + "; ".join(problems)
            logger.warning("Recipes skipped during the sweep: %s", problems)
        return loaded

    def _sweep(self, read: bool, only: set[str] | None = None) -> None:
        """Measure the estate, or - with ``only`` - a single account of it.

        A single account is the same work over a shorter list: the same read,
        the same fingerprint check, the same comparison against every recipe.
        What it must not do is disturb the rest of the grid, so the order of
        the matrix and the pruning of departed accounts are left alone.
        """
        recipes = self._load_recipes()
        if recipes is None:
            return
        stamp = _recipe_stamp(recipes)

        client = None
        if read:
            try:
                client = self.client_factory()
                tenants = self.tenant_lister(client)
            except OpsRampError as exc:
                self._fail(str(exc))
                self._close(client)
                return
            except Exception as exc:  # noqa: BLE001
                self._fail("Could not list tenants from OpsRamp.")
                logger.exception("Tenant listing crashed during the sweep: %s", exc)
                self._close(client)
                return
        else:
            tenants = self._stored_tenants()
            if not tenants:
                # Nothing has ever been read, so there is nothing to measure
                # against the new recipe. Fall back to reading the estate.
                logger.info("No stored readings; falling back to a full sweep")
                self._sweep(read=True, only=only)
                return

        limit = self.config.OVERVIEW_MAX_TENANTS
        if limit > 0:
            tenants = tenants[:limit]
        if only is not None:
            tenants = [t for t in tenants if t["id"] in only]
            if not tenants:
                # The account has gone since the grid was built. Nothing to do,
                # and nothing to report as a failure either.
                self._close(client)
                logger.info("Nothing to check: OpsRamp does not list that account")
                self._settle(read, only, unchanged=0)
                return

        with self._lock:
            # Figures from the previous sweep stay visible until each account is
            # refreshed, so the page never blanks out while it works.
            if only is None:
                self._order = [t["id"] for t in tenants]
            for t in tenants:
                state = self._tenants.get(t["id"])
                if state is None:
                    state = TenantState(tenant_id=t["id"], name=t["name"])
                    self._tenants[t["id"]] = state
                    if only is not None and t["id"] not in self._order:
                        self._order.append(t["id"])
                state.name = t["name"]
                if state.status != "done":
                    state.status = "pending"
            if only is None:
                for gone in set(self._tenants) - set(self._order):
                    self._tenants.pop(gone, None)
        if self.db is not None and read and only is None:
            try:
                dropped = self.db.forget_tenants(set(self._order))
                self.db.forget_inventory(set(self._order))
                self.db.forget_selections(set(self._order))
                if dropped:
                    logger.info("Dropped %d account(s) OpsRamp no longer lists", dropped)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not prune the cache: %s", exc)
            with self._lock:
                for gone in set(self._selections) - set(self._order):
                    self._selections.pop(gone, None)

        categories = self.config.OVERVIEW_CATEGORIES or ["all"]
        policy = getattr(self.config, "VERSION_COMPARE_POLICY", "at_least")
        logger.info(
            "Compliance %s starting over %d account(s) against %d recipe(s)",
            "sweep" if read else "re-measure",
            len(tenants),
            len(recipes),
        )

        workers = self._worker_count(len(tenants), read)
        self._done_count = 0
        # Published so that an account chosen while this runs can be moved to
        # the front of what is left, instead of waiting behind the others.
        ready = _Ready(tenants)
        self._ready = ready
        try:
            if workers <= 1:
                unchanged = self._measure_queue(
                    lambda: client, ready, recipes, categories, policy, stamp, read
                )
            else:
                unchanged = self._measure_many(
                    ready, recipes, categories, policy, stamp, read, workers
                )
        finally:
            self._ready = None
            self._close(client)

        self._settle(read, only, unchanged)

    def _settle(self, read: bool, only: set[str] | None, unchanged: int) -> None:
        """Publish the outcome of a sweep, and honour anything it displaced."""
        with self._lock:
            self.message = ""
            self.finished_at = time.time()
            if only is None:
                # A single-account check has nothing to say about how much of
                # the estate was quiet, so the estate's own figure stands.
                self.unchanged = unchanged
        self._remember_sweep()
        with self._lock:
            self.status = STATUS_DONE
            follow_on = self._remeasure_wanted
            self._remeasure_wanted = False
            # The recipes just measured against are no longer in force: the
            # next sweep reads the library again.
            self._active = ([], "")
        logger.info(
            "Compliance %s finished; %d account(s) were unchanged",
            "check" if only else ("sweep" if read else "re-measure"),
            unchanged,
        )
        if follow_on and not self._stop.is_set():
            # A recipe arrived while this was running, so the grid does not
            # yet reflect it. Measuring again costs seconds and no API calls.
            logger.info("Measuring again for the recipes that changed mid-sweep")
            self.recompare()

    def _measure_queue(
        self, client_for, ready, recipes, categories, policy, stamp, read
    ) -> int:
        """Take accounts from the queue until it is empty.  Counts the quiet ones.

        Taken one at a time rather than handed out up front, because the order
        is not fixed: an account the operator has just chosen a recipe for is
        moved to the front of this queue while the sweep is running.
        """
        unchanged = 0
        while not self._stop.is_set():
            item = ready.take()
            if item is None:
                return unchanged
            index, tenant = item
            try:
                quiet = self._measure_one(
                    client_for(), tenant, index, ready.total, recipes, categories,
                    policy, stamp, read,
                )
            except Exception as exc:  # noqa: BLE001 - one account must not
                # take the sweep down with it.
                logger.exception("Measuring %s crashed: %s", tenant["name"], exc)
                self._record_error(
                    tenant,
                    "An unexpected error occurred while measuring this account. "
                    "The details are in the application log.",
                    index,
                )
                continue
            if quiet:
                unchanged += 1
        return unchanged

    def _worker_count(self, tenants: int, read: bool) -> int:
        """How many accounts to work on at once.

        Reading is dominated by waiting on OpsRamp, so several accounts at a
        time is most of the win - but each one already fans out over its own
        assets, so the real concurrency is this times MAX_WORKERS and the
        ceiling is what the API will tolerate, not what this machine can run.
        Re-measuring from stored readings is CPU-bound and gains nothing from
        threads, so it stays sequential.
        """
        if not read:
            return 1
        configured = getattr(self.config, "OVERVIEW_TENANT_WORKERS", 4)
        return max(1, min(int(configured or 1), tenants))

    def _measure_many(
        self, ready, recipes, categories, policy, stamp, read, workers
    ) -> int:
        """Measure several accounts at once, each with its own connection.

        A client is not shared between the workers: each holds its own
        connection pool, and sharing one would serialise them on it.

        Every worker draws from the same queue rather than being handed a
        share of the accounts up front, so the queue can still be re-ordered
        while they are working - which is what lets a chosen account jump it.
        """
        logger.info("Reading %d account(s) at a time", workers)
        local = threading.local()
        clients: list = []
        clients_lock = threading.Lock()

        def client_for():
            client = getattr(local, "client", None)
            if client is None:
                client = self.client_factory()
                local.client = client
                with clients_lock:
                    clients.append(client)
            return client

        def drain(_worker):
            return self._measure_queue(
                client_for, ready, recipes, categories, policy, stamp, read
            )

        try:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="sweep"
            ) as pool:
                return sum(pool.map(drain, range(workers)))
        finally:
            for client in clients:
                self._close(client)

    def _stored_tenants(self) -> list[dict]:
        if self.db is None:
            return []
        try:
            return [
                {"id": row["tenant_id"], "name": row["tenant_name"]}
                for row in self.db.inventory_index()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list the stored readings: %s", exc)
            return []

    def _measure_one(
        self, client, tenant, index, total, recipes, categories, policy, stamp,
        read: bool,
    ) -> bool:
        """Measure one account.  Returns True when it needed no new work.

        Reading is the expensive half and the half that rarely changes, so it
        is skipped entirely when the caller only wants the estate measured
        against a different recipe.
        """
        from service import Tenant  # local import: avoids a cycle at module load

        # With several accounts in flight at once "3 of 24" would jump about,
        # so the message names the account and the count is kept separately.
        label = tenant["name"]
        with self._lock:
            # Read when this account starts, not when the sweep did: a recipe
            # may have arrived in between.
            current, stamp = self._active
            recipes = current or recipes
            state = self._tenants[tenant["id"]]
            previous = state.fingerprint
            previous_stamp = state.recipe_stamp
            was_measured = state.status == "done" and bool(state.recipes)
            superseded = [r["job_id"] for r in state.recipes if r.get("job_id")]
            self._done_count += 1
            self.message = f"{label} ({self._done_count} of {total})"
            state.status = "running"

        def progress(update: dict) -> None:
            note = update.get("message")
            if note:
                with self._lock:
                    self.message = f"{label} - {note}"


        if read:
            extraction, error = self._read_tenant(client, tenant, categories, progress)
            if error:
                self._record_error(tenant, error, index)
                return False
            mark = serialise.fingerprint(extraction)
            if mark == previous and stamp == previous_stamp and was_measured:
                # Nothing installed has changed and nothing approved has
                # changed, so the cells would come out identical. Leaving them
                # alone is what keeps a ten-minute scan quiet.
                with self._lock:
                    self._tenants[tenant["id"]].status = "done"
                    self._tenants[tenant["id"]].read_at = time.time()
                return True
            self._store_reading(tenant, extraction, mark)
        else:
            extraction = self._stored_reading(tenant)
            if extraction is None:
                self._record_error(
                    tenant,
                    "No stored reading for this account. Refresh to read it from "
                    "OpsRamp.",
                    index,
                )
                return False
            mark = previous

        measured = []
        for ref, entries in recipes:
            result = compare_extraction(
                extraction, entries=entries, recipe_name=ref.name, policy=policy
            )
            job = self.jobs.create(
                tenant_id=tenant["id"],
                tenant_name=tenant["name"],
                category=categories[0],
                recipe_name=ref.name,
                owner="",  # readable by any signed-in session
                tenants=[Tenant(tenant["id"], tenant["name"])],
                categories=list(categories),
            )
            self.jobs.adopt(job, result)
            self._remember_run(job.id, tenant["id"], ref.id)
            measured.append((ref, job.id, result))

        self._absorb(tenant, measured, index, mark, stamp)
        release = getattr(self.jobs, "release", None)
        if release is not None:
            release(superseded)
        if self.db is not None and superseded:
            try:
                self.db.forget_runs(superseded)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not prune superseded runs: %s", exc)
        return False

    def _read_tenant(self, client, tenant, categories, progress):
        """Read one account from OpsRamp.  Returns ``(extraction, error)``."""
        try:
            return (
                extract_tenant(
                    client,
                    tenant_id=tenant["id"],
                    tenant_name=tenant["name"],
                    category=list(categories),
                    progress=progress,
                ),
                "",
            )
        except OpsRampError as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Reading %s crashed: %s", tenant["name"], exc)
            return None, (
                "An unexpected error occurred while reading this account. The "
                "details are in the application log."
            )

    def _store_reading(self, tenant: dict, extraction, mark: str) -> None:
        if self.db is None:
            return
        try:
            payload = serialise.encode(extraction)
            self.db.save_inventory(
                tenant["id"],
                tenant["name"],
                mark,
                extraction.assets_matched,
                len(extraction.components),
                payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not store the reading for %s: %s", tenant["name"], exc
            )

    def _stored_reading(self, tenant: dict):
        if self.db is None:
            return None
        try:
            payload = self.db.load_inventory(tenant["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the stored reading: %s", exc)
            return None
        if payload is None:
            return None
        try:
            return serialise.decode(payload)
        except ValueError as exc:
            logger.warning(
                "The stored reading for %s is unusable (%s); it will be read again",
                tenant["name"],
                exc,
            )
            return None

    def _remember_run(self, job_id: str, tenant_id: str, recipe_id: str) -> None:
        if self.db is None:
            return
        try:
            self.db.save_run(job_id, tenant_id, recipe_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record run %s: %s", job_id, exc)

    def _record_error(self, tenant: dict, message: str, position: int) -> None:
        with self._lock:
            state = self._tenants.get(tenant["id"])
            if state is None:
                return
            state.status = "error"
            state.error = message
            state.finished_at = time.time()
            snapshot = state.public()
        self._remember(TenantState.restore(snapshot), position)

    def _absorb(
        self, tenant: dict, measured: list, position: int,
        fingerprint: str = "", recipe_stamp: str = "",
    ) -> None:
        """Publish one account's cells as soon as its comparisons finish."""
        with self._lock:
            state = self._tenants.get(tenant["id"])
            if state is None:
                return
            state.status = "done"
            state.error = ""
            state.finished_at = time.time()
            state.read_at = state.read_at or time.time()
            state.fingerprint = fingerprint or state.fingerprint
            state.recipe_stamp = recipe_stamp
            state.recipes = []
            state.cells = {}

            cells: dict[str, dict] = {}
            for ref, job_id, result in measured:
                per_tenant = summary_report.tenant_compliance(result)
                stats = per_tenant[0] if per_tenant else None
                state.recipes.append(_recipe_stats(ref, job_id, stats))
                if stats is not None:
                    state.assets = stats["assets"]
                    state.components = stats["components"]

                grid = summary_report.tenant_category_matrix(result)
                by_category = grid.get(stats["tenant"], {}) if stats else {}
                for category, cell in by_category.items():
                    target = cells.setdefault(
                        category,
                        {
                            "assets": cell["assets"],
                            "components": cell["components"],
                            "recipes": [],
                            "best": "",
                        },
                    )
                    target["recipes"].append(_recipe_stats(ref, job_id, cell))

            for cell in cells.values():
                cell["best"] = _attribute(cell["recipes"])
                cell["comparable_any"] = any(
                    r["comparable"] for r in cell["recipes"]
                )
                cell["explain"] = _explain(cell)
                cell["unscored"] = _unscored_label(cell)
            state.cells = cells
            state.best = _attribute(state.recipes)
            snapshot = state.public()

        self._remember(TenantState.restore(snapshot), position)

    @staticmethod
    def _close(client) -> None:
        if client is None:
            return
        try:
            client.close()
        except Exception:  # pragma: no cover - defensive
            pass


def _recipe_stamp(recipes: list[tuple]) -> str:
    """Identifies the set of recipes in force, and their contents.

    Used together with a reading's fingerprint to decide whether an account
    needs measuring again: if neither what is installed nor what is approved
    has moved, the cells would come out identical.
    """
    parts = sorted(
        f"{ref.id}:{ref.uploaded_at or 0}:{len(entries)}" for ref, entries in recipes
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _recipe_stats(ref, job_id: str, stats) -> dict:
    """One recipe's verdict on one account, or on one cell of one account."""
    if stats is None:
        stats = {}
    comparable = stats.get("comparable", 0)
    covered = stats.get("covered", 0)
    components = stats.get("components", 0)
    rate = stats.get("rate")
    return {
        "id": ref.id,
        "name": ref.name,
        "job_id": job_id,
        "rate": rate,
        "tone": summary_report.compliance_tone(rate),
        "updated": stats.get("updated", 0),
        "needs_update": stats.get("needs_update", 0),
        "not_detected": stats.get("not_detected", 0),
        "not_in_recipe": stats.get("not_in_recipe", 0),
        "comparable": comparable,
        "covered": covered,
        "coverage": (
            round(100.0 * covered / components, 1) if components else None
        ),
        "components": components,
        "pending": stats.get("needs_update", 0) + stats.get("not_detected", 0),
        # Which equipment this recipe has nothing to say about, so a cell
        # without a percentage can name it instead of only counting it.
        "no_target_models": list(stats.get("no_target_models") or [])[:6],
    }


def _explain(cell: dict) -> str:
    """Why a cell has no percentage, in words and with the actual numbers.

    A cell with assets in it but nothing to compare used to render as a bare
    dash, which reads as a rendering fault rather than as a finding.  It is a
    finding - the recipe has no target for this equipment, or OpsRamp never
    reported a version - and saying which is what lets someone act on it.
    """
    entries = cell.get("recipes") or []
    if not entries:
        return f"{cell['components']} component(s), no recipe applies"

    best = max(entries, key=lambda r: (r["covered"], r["comparable"]))
    parts = []
    if best["not_detected"]:
        parts.append(f"{best['not_detected']} with no version read")
    if best["not_in_recipe"]:
        named = ", ".join(best.get("no_target_models") or [])
        parts.append(
            f"{best['not_in_recipe']} with no approved target"
            + (f" ({named})" if named else "")
        )
    if not parts:
        parts.append("nothing comparable")
    return f"{cell['components']} component(s): " + ", ".join(parts)


def _unscored_label(cell: dict) -> str:
    """Two words for why a cell has no percentage.

    "Not scored" tells a reader nothing they can act on, and the two reasons
    need entirely different people: a missing target is a gap in the recipe,
    a missing version is a gap in what OpsRamp reports about the asset. So the
    cell says which, without anyone having to hover over it.
    """
    entries = cell.get("recipes") or []
    if not entries:
        return "not measured"
    best = max(entries, key=lambda r: (r["covered"], r["comparable"]))
    if best["not_in_recipe"] and not best["not_detected"]:
        return "no target"
    if best["not_detected"] and not best["not_in_recipe"]:
        return "no version"
    if best["not_detected"] or best["not_in_recipe"]:
        return "no target or version"
    return "not scored"


def _attribute(entries: list[dict]) -> str:
    """Which recipe this firmware belongs to.

    Coverage decides it, not the compliance rate: a recipe holding one target
    that happens to match would score 100% while describing almost nothing.
    The recipe that has an approved target for the most of what is installed is
    the one this estate is actually built to.  A tie is settled by the rate.
    """
    best = ""
    best_key = (-1.0, -1.0)
    for entry in entries:
        key = (entry["covered"], entry["rate"] if entry["rate"] is not None else -1.0)
        if entry["covered"] and key > best_key:
            best_key = key
            best = entry["id"]
    return best


def _prune(row: dict, known: dict[str, str]) -> dict:
    """Drop figures for recipes that have since left the library.

    A cached measurement outlives the recipe it was made against.  Showing a
    percentage under a name nobody can find any more is worse than showing
    nothing, so those entries are dropped until the next sweep.
    """
    if not known:
        row = dict(row)
        row["recipes"] = []
        row["cells"] = {}
        return row

    row = dict(row)
    row["recipes"] = [r for r in row.get("recipes", []) if r["id"] in known]
    cells = {}
    for category, cell in (row.get("cells") or {}).items():
        cell = dict(cell)
        cell["recipes"] = [r for r in cell.get("recipes", []) if r["id"] in known]
        if cell["recipes"] or cell.get("components"):
            cells[category] = cell
    row["cells"] = cells
    return row


def _wanted(recipe_ids, library) -> tuple:
    """Clean a requested set of recipes.  ``(ids_in_library_order, refs)``.

    ``None`` for the ids when one of them is not in the library: a choice
    naming something that is not there is a mistake worth reporting, not a
    silent near-miss.
    """
    refs = library.all() if library is not None else []
    # One id is a reasonable thing to pass, and a string is iterable - left
    # alone it would be read as one recipe id per character.
    if isinstance(recipe_ids, str):
        recipe_ids = [recipe_ids]
    asked = {
        str(recipe_id).strip()
        for recipe_id in (recipe_ids or [])
        if str(recipe_id).strip()
    }
    if asked - {ref.id for ref in refs}:
        return None, refs
    # Library order, so the lines read the same way in every row whatever
    # order the boxes happened to be ticked in.
    return [ref.id for ref in refs if ref.id in asked], refs


def _in_view(row: dict, chosen, known: dict[str, str]) -> dict:
    """Narrow one account's row to the recipes it has been told to be judged by.

    The figures are not recomputed here and nothing is re-measured: an account
    is compared against every recipe in the library, so the line for each
    chosen one has already been worked out.  What this decides is which of
    those lines the grid shows - and each chosen recipe keeps its own line,
    its own percentage and its own counts, because comparing two baselines
    side by side is the reason for choosing more than one.

    A choice belongs to one account only, so it is applied to that account's
    own row and nothing else - which is what keeps one account's choice out of
    another's cells.  A recipe that has left the library is dropped from the
    choice, and an account left with none falls back to automatic rather than
    rendering empty.
    """
    chosen = [recipe_id for recipe_id in (chosen or []) if recipe_id in known]
    picked = set(chosen)
    entries = row.get("recipes") or []
    row["selected"] = list(chosen)
    row["selected_names"] = [known[recipe_id] for recipe_id in chosen]
    row["automatic"] = not chosen

    shown = [e for e in entries if e["id"] in picked] if chosen else list(entries)
    row["shown"] = shown
    # Chosen recipes this account has no figures against yet - uploaded
    # moments ago, or the read failed. Naming them beats an empty row that
    # looks like a rendering fault.
    row["awaiting"] = [
        known[recipe_id]
        for recipe_id in chosen
        if not any(e["id"] == recipe_id for e in entries)
    ]

    # Which line the pending count and the links to the detail open. Among
    # the chosen, the one this account's firmware follows most closely if it
    # is among them, otherwise the first - so a reader always lands on
    # figures the row is actually showing them.
    best = row.get("best") or ""
    row["focus"] = next(
        (e for e in shown if e["id"] == best),
        shown[0] if shown else None,
    )

    cells = {}
    for category, cell in (row.get("cells") or {}).items():
        cell = dict(cell)
        if chosen:
            lines = [e for e in cell.get("recipes") or [] if e["id"] in picked]
            # Recomputed from the chosen recipes alone: a cell that some other
            # recipe could score must not look scored under these.
            view = {"components": cell.get("components", 0), "recipes": lines}
            cell["shown"] = lines
            cell["comparable_any"] = any(e["comparable"] for e in lines)
            if lines:
                cell["explain"] = _explain(view)
                cell["unscored"] = _unscored_label(view)
            else:
                # None of the chosen recipes produced a row here, so none of
                # them names this equipment. That is a gap in the recipes, the
                # same finding as a cell they scored nothing in.
                names = ", ".join(known[recipe_id] for recipe_id in chosen)
                cell["explain"] = (
                    f"{cell.get('components', 0)} component(s): "
                    f"{names} " + ("has" if len(chosen) == 1 else "have")
                    + " no target for any of them"
                )
                cell["unscored"] = "no target"
        else:
            cell["shown"] = list(cell.get("recipes") or [])
        cells[category] = cell
    row["cells"] = cells
    return row


def _everywhere(rows: list[dict]) -> list | None:
    """The recipes every account is set to, or None when they differ.

    An empty list means every account is on automatic, which is a setting
    they agree on as much as any other. None is disagreement, and the page
    says so rather than showing one account's choice as the estate's.
    """
    if not rows:
        return []
    settings = {tuple(row.get("selected") or ()) for row in rows}
    return list(settings.pop()) if len(settings) == 1 else None


def _totals(rows: list[dict], refs: list) -> dict:
    """Estate-wide figures, one set per recipe, plus the headline."""
    done = [r for r in rows if r["status"] == "done"]
    per_recipe = []
    for ref in refs:
        updated = needs = detected = in_recipe = covered = components = 0
        for row in done:
            for entry in row["recipes"]:
                if entry["id"] != ref.id:
                    continue
                updated += entry["updated"]
                needs += entry["needs_update"]
                detected += entry["not_detected"]
                in_recipe += entry["not_in_recipe"]
                covered += entry["covered"]
                components += entry["components"]
        comparable = updated + needs
        rate = round(100.0 * updated / comparable, 1) if comparable else None
        per_recipe.append(
            {
                "id": ref.id,
                "name": ref.name,
                "rate": rate,
                "tone": summary_report.compliance_tone(rate),
                "updated": updated,
                "needs_update": needs,
                "not_detected": detected,
                "not_in_recipe": in_recipe,
                "comparable": comparable,
                "covered": covered,
                "components": components,
                "coverage": round(100.0 * covered / components, 1) if components else None,
            }
        )

    # The headline is the best-matching recipe's rate: measuring an estate
    # against a baseline it does not follow says nothing useful about it.
    leader = max(
        per_recipe,
        key=lambda r: (r["covered"], r["rate"] if r["rate"] is not None else -1.0),
        default=None,
    )
    return {
        "tenants": len(done),
        "assets": sum(r["assets"] for r in done),
        "components": sum(r["components"] for r in done),
        "recipes": per_recipe,
        "leader": leader["name"] if leader and leader["covered"] else "",
        "rate": leader["rate"] if leader and leader["covered"] else None,
        "tone": summary_report.compliance_tone(
            leader["rate"] if leader and leader["covered"] else None
        ),
        "updated": leader["updated"] if leader and leader["covered"] else 0,
        "needs_update": leader["needs_update"] if leader and leader["covered"] else 0,
        "not_detected": leader["not_detected"] if leader and leader["covered"] else 0,
        "not_in_recipe": leader["not_in_recipe"] if leader and leader["covered"] else 0,
    }
