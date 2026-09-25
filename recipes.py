"""The library of approved recipes the matrix is measured against.

There is rarely one right answer to "what should be installed here".  An estate
runs several approved baselines at once - a current one, the one before it, a
stream pinned for a particular customer - so the portal holds a *library* of
recipes under names the operator chooses, and every account is measured against
all of them.  Which recipe an account actually follows is then a reading of the
numbers rather than an assumption made before the sweep starts.

Only a recipe that validates is ever stored, so a sweep can never run against
one that would produce a misleading matrix.  ``DEFAULT_RECIPE_PATH`` remains as
a way to seed a fresh deployment: when the library is empty that file stands in
as a single recipe, so an existing install keeps working untouched.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from recipe.merge import forget_cached_recipes

logger = logging.getLogger(__name__)

CONFIGURED_ID = "configured"
MAX_NAME = 60


class RecipeError(Exception):
    """A recipe could not be stored, with a message meant for the operator."""


@dataclass
class RecipeRef:
    """One approved recipe: the files it is made of, and what it is called.

    A recipe is several files as often as one. The vendor publishes the
    monthly baseline as two PDFs - the CDC matrix and the S4HANA matrix - and
    they describe one baseline between them, so they belong under one name.
    Held as two recipes instead, each would score badly on the equipment the
    other covers and the coverage figures would mean nothing.
    """

    id: str
    name: str
    filename: str
    paths: list[Path] = field(default_factory=list)
    targets: int = 0
    uploaded_at: float | None = None
    uploaded_by: str = ""
    source: str = "upload"  # upload | configured

    @property
    def is_upload(self) -> bool:
        return self.source == "upload"

    @property
    def path(self) -> Path | None:
        """The first file, for callers that only need one."""
        return self.paths[0] if self.paths else None

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "filename": self.filename,
            "files": len(self.paths),
            "targets": self.targets,
            "uploaded_at": self.uploaded_at,
            "uploaded_by": self.uploaded_by,
            "source": self.source,
        }


def clean_name(raw: str, fallback: str = "") -> str:
    """A display name the operator typed, trimmed to something printable."""
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name:
        name = Path(fallback).stem.replace("_", " ").strip()
    return name[:MAX_NAME]


class RecipeLibrary:
    """Every approved recipe, on disk and in the cache database."""

    def __init__(self, config, db) -> None:
        self.config = config
        self.db = db
        self.dir = Path(config.UPLOAD_DIR).parent / "recipes"
        self._lock = threading.RLock()
        self._migrated = False

    # ------------------------------------------------------------------ public
    def all(self) -> list[RecipeRef]:
        """Every recipe to measure against, in the order they were added.

        Falls back to ``DEFAULT_RECIPE_PATH`` when nothing has been uploaded,
        so a deployment that was seeded through the settings file keeps working
        without anyone having to upload the same file again.
        """
        uploads = self.uploads()
        if uploads:
            return uploads
        seed = self._configured()
        return [seed] if seed else []

    def uploads(self) -> list[RecipeRef]:
        """Only the recipes uploaded through the page.  Never the seed."""
        self._migrate_once()
        out: list[RecipeRef] = []
        for row in self.db.list_recipes():
            paths = self._stored_files(row["stored"])
            if not paths:
                logger.warning(
                    "Recipe %r points at %s, which is missing; ignoring it",
                    row["name"],
                    row["stored"],
                )
                continue
            out.append(
                RecipeRef(
                    id=row["id"],
                    name=row["name"],
                    filename=row["filename"],
                    paths=paths,
                    targets=int(row["targets"] or 0),
                    uploaded_at=row["uploaded_at"],
                    uploaded_by=row["uploaded_by"] or "",
                )
            )
        return out

    def _stored_files(self, stored: str) -> list[Path]:
        """The files behind one recipe, in the order they were uploaded.

        A recipe added before the library held more than one file per recipe
        is a single file rather than a directory, so both shapes are read.
        """
        target = self.dir / stored
        if target.is_dir():
            return sorted(item for item in target.iterdir() if item.is_file())
        return [target] if target.exists() else []

    def get(self, recipe_id: str) -> RecipeRef | None:
        for ref in self.all():
            if ref.id == recipe_id:
                return ref
        return None

    def add(
        self,
        sources: list[tuple[Path, str]] | Path,
        filename: str | None = None,
        name: str = "",
        targets: int = 0,
        uploaded_by: str = "",
    ) -> tuple[RecipeRef, bool]:
        """Store an already-validated recipe.  Returns ``(ref, replaced)``.

        ``sources`` is a list of ``(path, original name)`` pairs, because one
        recipe can be several files - the vendor publishes the monthly
        baseline as a CDC matrix and an S4HANA matrix that describe it
        between them. A single path is accepted too, for the ordinary case.

        A name that is already in use replaces that recipe rather than being
        rejected: re-uploading this month's revision of "Current baseline" is
        the ordinary case, and refusing it would leave the operator to delete
        and re-add.  The caller says so in the message it shows, so nothing is
        replaced silently.
        """
        if isinstance(sources, (str, Path)):
            sources = [(Path(sources), filename or Path(sources).name)]
        if not sources:
            raise RecipeError("No recipe file was given.")

        display = ", ".join(original for _path, original in sources)
        name = clean_name(name, sources[0][1])
        if not name:
            raise RecipeError("Give the recipe a name so it can be told apart.")

        with self._lock:
            self._migrate_once()
            existing = self.db.find_recipe_by_name(name)
            replaced = existing is not None
            recipe_id = existing["id"] if replaced else uuid.uuid4().hex
            position = (
                int(existing["position"]) if replaced else self.db.next_recipe_position()
            )

            # Everything for one recipe lives in its own directory, so a
            # revision made of fewer files cannot leave part of the previous
            # one behind to be read alongside it.
            folder = self.dir / recipe_id
            if replaced:
                self._forget_files(existing["stored"])
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True, exist_ok=True)

            for index, (source, original) in enumerate(sources, start=1):
                suffix = Path(original).suffix.lower() or Path(source).suffix.lower()
                shutil.copyfile(source, folder / f"{index:02d}{suffix}")

            uploaded_at = time.time()
            self.db.upsert_recipe(
                id=recipe_id,
                name=name,
                filename=display,
                stored=recipe_id,
                targets=targets,
                uploaded_at=uploaded_at,
                uploaded_by=uploaded_by,
                position=position,
            )
            paths = self._stored_files(recipe_id)
            # Parsed recipes are cached by each file's size and modification
            # time. That already notices a revision, but a library change is a
            # certainty rather than an inference, so the cache is dropped here
            # too - it costs one re-read of files that are about to be read.
            forget_cached_recipes()

        logger.info(
            "Recipe %r %s from %s (%d file(s), %d target(s)) by %s",
            name,
            "replaced" if replaced else "added",
            display,
            len(sources),
            targets,
            uploaded_by or "unknown",
        )
        return (
            RecipeRef(
                id=recipe_id,
                name=name,
                filename=display,
                paths=paths,
                targets=targets,
                uploaded_at=uploaded_at,
                uploaded_by=uploaded_by,
            ),
            replaced,
        )

    def _forget_files(self, stored: str) -> None:
        """Remove whatever a recipe row points at, file or directory."""
        target = self.dir / stored
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)

    def rename(self, recipe_id: str, name: str) -> RecipeRef:
        name = clean_name(name)
        if not name:
            raise RecipeError("A recipe needs a name.")
        with self._lock:
            row = self.db.get_recipe(recipe_id)
            if row is None:
                raise RecipeError("That recipe is no longer in the library.")
            clash = self.db.find_recipe_by_name(name)
            if clash is not None and clash["id"] != recipe_id:
                raise RecipeError(f"Another recipe is already called {name!r}.")
            self.db.rename_recipe(recipe_id, name)
        logger.info("Recipe %s renamed to %r", recipe_id, name)
        return RecipeRef(
            id=recipe_id,
            name=name,
            filename=row["filename"],
            paths=self._stored_files(row["stored"]),
            targets=int(row["targets"] or 0),
            uploaded_at=row["uploaded_at"],
            uploaded_by=row["uploaded_by"] or "",
        )

    def remove(self, recipe_id: str) -> str:
        """Forget one recipe.  Returns its name, or "" if it was already gone."""
        with self._lock:
            row = self.db.get_recipe(recipe_id)
            if row is None:
                return ""
            self._forget_files(row["stored"])
            self.db.delete_recipe(recipe_id)
            forget_cached_recipes()
        logger.info("Recipe %r removed", row["name"])
        return row["name"]

    def describe(self) -> dict:
        """What the page needs in order to talk about the library."""
        refs = self.all()
        return {
            "configured": bool(refs),
            "count": len(refs),
            "seeded": bool(refs) and refs[0].source == "configured",
            "recipes": [ref.public() for ref in refs],
        }

    # ----------------------------------------------------------------- internal
    def _configured(self) -> RecipeRef | None:
        configured = (getattr(self.config, "DEFAULT_RECIPE_PATH", "") or "").strip()
        if not configured:
            return None
        path = Path(configured)
        if not path.exists():
            logger.error(
                "DEFAULT_RECIPE_PATH points at %s, which does not exist", path
            )
            return None
        return RecipeRef(
            id=CONFIGURED_ID,
            name=path.stem.replace("_", " ") or path.name,
            filename=path.name,
            paths=[path],
            source="configured",
        )

    def _migrate_once(self) -> None:
        """Adopt the single recipe kept by the previous release, if any.

        Before the library existed there was one approved recipe, stored as
        ``var/recipe/active.*``.  Bringing it across means an upgrade does not
        silently empty the matrix.
        """
        if self._migrated:
            return
        self._migrated = True

        legacy_dir = Path(self.config.UPLOAD_DIR).parent / "recipe"
        meta_path = legacy_dir / "active.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Could not read the previous approved recipe: %s", exc)
            return
        if not isinstance(meta, dict) or not meta.get("stored"):
            return
        source = legacy_dir / meta["stored"]
        if not source.exists():
            return
        if self.db.list_recipes():
            return  # the library is already populated; leave it alone

        filename = meta.get("filename") or source.name
        try:
            self.add(
                [(source, filename)],
                name=clean_name("", filename) or "Approved recipe",
                targets=int(meta.get("targets") or 0),
                uploaded_by=meta.get("uploaded_by", ""),
            )
        except (RecipeError, OSError) as exc:
            logger.warning("Could not carry the previous recipe over: %s", exc)
            return
        meta_path.unlink(missing_ok=True)
        logger.info("Carried the previously approved recipe %s into the library", filename)
