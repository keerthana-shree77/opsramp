"""SQLite cache behind the compliance matrix.

The matrix is expensive to build - every asset of every account - so it cannot
be built while someone waits for a page.  It is built by a background sweep and
kept here, which is what lets the home page render the moment a user signs in
and lets the grid survive a restart of the portal.

Nothing in here is a source of truth: every row can be rebuilt by sweeping
again.  So the schema is deliberately shallow - a measurement is stored as the
JSON the page needs - and a row that cannot be read back is discarded with a
warning rather than being allowed to take the page down with it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    stored      TEXT NOT NULL,
    targets     INTEGER NOT NULL DEFAULT 0,
    uploaded_at REAL NOT NULL,
    uploaded_by TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS recipes_name
    ON recipes (name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS measurements (
    tenant_id   TEXT PRIMARY KEY,
    tenant_name TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    measured_at REAL NOT NULL,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- What was read from OpsRamp, kept apart from what it was measured against.
-- A recipe changes far more often than an estate does, so holding the reading
-- means a new recipe is answered from disk instead of by sweeping again.
CREATE TABLE IF NOT EXISTS inventory (
    tenant_id   TEXT PRIMARY KEY,
    tenant_name TEXT NOT NULL,
    read_at     REAL NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    assets      INTEGER NOT NULL DEFAULT 0,
    components  INTEGER NOT NULL DEFAULT 0,
    payload     BLOB NOT NULL
);

-- Which recipes each account is judged by, when the operator has said.
-- Every account is measured against every recipe whatever is chosen here, so
-- this decides what the grid shows and what is checked first - never what is
-- compared. An account with no row here is shown against every recipe, with
-- the closest match marked.
--
-- One row per account per recipe: an account is commonly judged against two
-- or three of them side by side - this month's baseline and the one being
-- moved away from - and a single-valued choice could not say that.
CREATE TABLE IF NOT EXISTS recipe_choices (
    tenant_id TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    chosen_at REAL NOT NULL,
    chosen_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, recipe_id)
);

-- The single-valued table this replaced. Kept so an upgrade carries the
-- choices already made across; see _adopt_old_selections.
CREATE TABLE IF NOT EXISTS selections (
    tenant_id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL,
    chosen_at REAL NOT NULL,
    chosen_by TEXT NOT NULL DEFAULT ''
);

-- Which account and recipe a run belongs to. Runs themselves live in memory,
-- so this is what lets a cell of a cached matrix still be opened after a
-- restart: the run is built again from the stored reading.
CREATE TABLE IF NOT EXISTS runs (
    job_id     TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    recipe_id  TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Database:
    """A small SQLite file.  One connection per operation, WAL journalling.

    A connection per call rather than one shared between threads: the sweep
    writes from a worker thread while requests read from Flask's, and SQLite
    connections are not safe to share across threads.  The volume here is a few
    dozen rows a minute, so there is nothing to gain from pooling.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ready = False

    @contextmanager
    def connect(self):
        """Open, commit and close.  ``sqlite3``'s own context manager commits
        but leaves the handle open, which would leak one per call."""
        self._ensure_schema()
        connection = self._open()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            connection = self._open()
            try:
                with connection:
                    connection.executescript(SCHEMA)
            finally:
                connection.close()
            self._ready = True
            logger.info("Cache database ready at %s", self.path)

    # -------------------------------------------------------------- meta pairs
    def get_meta(self, key: str, default=None):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            logger.warning("Ignoring an unreadable cached value for %r", key)
            return default

    def set_meta(self, key: str, value) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # ------------------------------------------------------------ measurements
    def save_measurement(
        self, tenant_id: str, tenant_name: str, position: int, payload: dict
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO measurements "
                "  (tenant_id, tenant_name, position, measured_at, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET "
                "  tenant_name = excluded.tenant_name, "
                "  position    = excluded.position, "
                "  measured_at = excluded.measured_at, "
                "  payload     = excluded.payload",
                (tenant_id, tenant_name, position, time.time(), json.dumps(payload)),
            )

    def load_measurements(self) -> list[dict]:
        """Every cached tenant, in sweep order.  Unreadable rows are skipped."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT tenant_id, tenant_name, measured_at, payload "
                "FROM measurements ORDER BY position, tenant_name COLLATE NOCASE"
            ).fetchall()

        out: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                logger.warning(
                    "Discarding the cached measurement for %s: unreadable JSON",
                    row["tenant_name"],
                )
                continue
            if not isinstance(payload, dict):
                continue
            payload.setdefault("tenant_id", row["tenant_id"])
            payload.setdefault("name", row["tenant_name"])
            payload["measured_at"] = row["measured_at"]
            out.append(payload)
        return out

    def forget_tenants(self, keep: set[str]) -> int:
        """Drop cached tenants OpsRamp no longer reports.  Returns how many."""
        with self.connect() as connection:
            if keep:
                marks = ",".join("?" for _ in keep)
                cursor = connection.execute(
                    "DELETE FROM measurements WHERE tenant_id NOT IN (" + marks + ")",
                    tuple(keep),
                )
            else:
                cursor = connection.execute("DELETE FROM measurements")
            return cursor.rowcount or 0

    def clear_measurements(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM measurements")

    # --------------------------------------------------------------- inventory
    def save_inventory(
        self,
        tenant_id: str,
        tenant_name: str,
        fingerprint: str,
        assets: int,
        components: int,
        payload: bytes,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO inventory "
                "  (tenant_id, tenant_name, read_at, fingerprint, assets, "
                "   components, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET "
                "  tenant_name = excluded.tenant_name, "
                "  read_at     = excluded.read_at, "
                "  fingerprint = excluded.fingerprint, "
                "  assets      = excluded.assets, "
                "  components  = excluded.components, "
                "  payload     = excluded.payload",
                (
                    tenant_id, tenant_name, time.time(), fingerprint,
                    assets, components, payload,
                ),
            )

    def load_inventory(self, tenant_id: str) -> bytes | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM inventory WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return row["payload"] if row else None

    def inventory_index(self) -> list[dict]:
        """Every stored reading, without its payload.  Cheap to call."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT tenant_id, tenant_name, read_at, fingerprint, assets, "
                "       components, LENGTH(payload) AS bytes "
                "FROM inventory ORDER BY tenant_name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def forget_inventory(self, keep: set[str]) -> int:
        with self.connect() as connection:
            if keep:
                marks = ",".join("?" for _ in keep)
                cursor = connection.execute(
                    "DELETE FROM inventory WHERE tenant_id NOT IN (" + marks + ")",
                    tuple(keep),
                )
            else:
                cursor = connection.execute("DELETE FROM inventory")
            return cursor.rowcount or 0

    # ------------------------------------------------------ recipe choices
    @staticmethod
    def _tidy(recipe_ids) -> list:
        """The ids to store: no blanks, no repeats, order kept.

        A form can post the same box twice if a page is submitted oddly, and
        the table holds one row per account per recipe - so a repeat would
        fail the write and lose the whole choice.
        """
        out: list = []
        for recipe_id in recipe_ids or []:
            recipe_id = str(recipe_id).strip()
            if recipe_id and recipe_id not in out:
                out.append(recipe_id)
        return out

    def save_selection(
        self, tenant_id: str, recipe_ids, chosen_by: str = ""
    ) -> None:
        """Set the recipes one account is judged by, replacing what it had.

        An empty list returns the account to automatic, where it is shown
        against every recipe with the closest match marked.
        """
        wanted = self._tidy(recipe_ids)
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM recipe_choices WHERE tenant_id = ?", (tenant_id,)
            )
            for recipe_id in wanted:
                connection.execute(
                    "INSERT INTO recipe_choices "
                    "  (tenant_id, recipe_id, chosen_at, chosen_by) "
                    "VALUES (?, ?, ?, ?)",
                    (tenant_id, recipe_id, now, chosen_by),
                )

    def clear_selection(self, tenant_id: str) -> None:
        """Back to automatic: the account is shown against every recipe."""
        self.save_selection(tenant_id, [])

    def save_selections(self, choices: dict) -> None:
        """Set many accounts at once, in one transaction.

        Applying a set of recipes across the estate is a single decision, so
        it is written as a single change: either every account moves or none
        does.
        """
        now = time.time()
        with self.connect() as connection:
            for tenant_id, recipe_ids in choices.items():
                connection.execute(
                    "DELETE FROM recipe_choices WHERE tenant_id = ?", (tenant_id,)
                )
                for recipe_id in self._tidy(recipe_ids):
                    connection.execute(
                        "INSERT INTO recipe_choices "
                        "  (tenant_id, recipe_id, chosen_at, chosen_by) "
                        "VALUES (?, ?, ?, '')",
                        (tenant_id, recipe_id, now),
                    )

    def load_selections(self) -> dict:
        """``{tenant_id: [recipe_id, ...]}`` for every account that has been told."""
        self._adopt_old_selections()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT tenant_id, recipe_id FROM recipe_choices "
                "ORDER BY tenant_id, chosen_at, recipe_id"
            ).fetchall()
        out: dict = {}
        for row in rows:
            if row["recipe_id"]:
                out.setdefault(row["tenant_id"], []).append(row["recipe_id"])
        return out

    def _adopt_old_selections(self) -> None:
        """Carry choices made before an account could hold more than one."""
        with self.connect() as connection:
            already = connection.execute(
                "SELECT 1 FROM recipe_choices LIMIT 1"
            ).fetchone()
            if already:
                return
            rows = connection.execute(
                "SELECT tenant_id, recipe_id, chosen_at, chosen_by FROM selections"
            ).fetchall()
            if not rows:
                return
            for row in rows:
                if not row["recipe_id"]:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO recipe_choices "
                    "  (tenant_id, recipe_id, chosen_at, chosen_by) "
                    "VALUES (?, ?, ?, ?)",
                    (row["tenant_id"], row["recipe_id"], row["chosen_at"],
                     row["chosen_by"] or ""),
                )
        logger.info("Carried %d recipe choice(s) over from the previous release",
                    len(rows))

    def forget_selections(self, keep: set) -> int:
        """Drop the choices of accounts OpsRamp no longer reports."""
        with self.connect() as connection:
            if keep:
                marks = ",".join("?" for _ in keep)
                cursor = connection.execute(
                    "DELETE FROM recipe_choices WHERE tenant_id NOT IN ("
                    + marks + ")",
                    tuple(keep),
                )
            else:
                cursor = connection.execute("DELETE FROM recipe_choices")
            return cursor.rowcount or 0

    def forget_selected_recipe(self, recipe_id: str) -> int:
        """Drop every choice naming a recipe that has left the library.

        Left behind, it would pin an account to a name nobody can find any
        more, and the account would look unmeasured until someone noticed the
        dropdown was pointing at nothing.
        """
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM recipe_choices WHERE recipe_id = ?", (recipe_id,)
            )
            return cursor.rowcount or 0

    # -------------------------------------------------------------------- runs
    def save_run(self, job_id: str, tenant_id: str, recipe_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO runs (job_id, tenant_id, recipe_id, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "  tenant_id = excluded.tenant_id, "
                "  recipe_id = excluded.recipe_id",
                (job_id, tenant_id, recipe_id, time.time()),
            )

    def get_run(self, job_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def forget_runs(self, job_ids) -> None:
        job_ids = list(job_ids)
        if not job_ids:
            return
        marks = ",".join("?" for _ in job_ids)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM runs WHERE job_id IN (" + marks + ")", tuple(job_ids)
            )

    # ----------------------------------------------------------------- recipes
    def list_recipes(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM recipes ORDER BY position, uploaded_at"
            ).fetchall()

    def get_recipe(self, recipe_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
            ).fetchone()

    def find_recipe_by_name(self, name: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM recipes WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()

    def upsert_recipe(self, **fields) -> None:
        columns = (
            "id", "name", "filename", "stored", "targets",
            "uploaded_at", "uploaded_by", "position",
        )
        values = tuple(fields[column] for column in columns)
        marks = ",".join("?" for _ in columns)
        updates = ",".join(
            column + " = excluded." + column for column in columns if column != "id"
        )
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO recipes (" + ",".join(columns) + ") "
                "VALUES (" + marks + ") "
                "ON CONFLICT(id) DO UPDATE SET " + updates,
                values,
            )

    def rename_recipe(self, recipe_id: str, name: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE recipes SET name = ? WHERE id = ?", (name, recipe_id)
            )

    def delete_recipe(self, recipe_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))

    def next_recipe_position(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM recipes"
            ).fetchone()
        return int(row["next"])
