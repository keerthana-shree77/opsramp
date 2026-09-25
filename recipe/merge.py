"""Reading one approved baseline that arrives as several files.

The vendor publishes the monthly baseline as two documents - the CDC matrix
and the S4HANA matrix - which between them describe one set of approved
versions. Uploading them as two separate recipes would mean each scored badly
on the equipment the other covers, so they are read together and merged into
one list of targets.

Where two files claim the same platform/model/component, the first one wins
and the collision is reported. Silently preferring either would let a
duplicated row change a target without anyone seeing it happen.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .loader import RecipeLoadError, load_recipe
from .validator import validate_recipe

logger = logging.getLogger(__name__)

# Parsed recipes, keyed by the exact content of the files they came from.
# Reading a recipe is expensive - a PDF goes through pdfplumber page by page -
# and the same recipe is re-read on every sweep and every re-measure while its
# file has not changed at all.
#
# The key is a hash of the bytes, not the size and timestamp. Those would miss
# a revision written within one tick of the filesystem clock at the same
# length, and measuring an estate against a superseded recipe is worse than
# being slow. Hashing a few megabytes costs a millisecond or two against the
# second or more that parsing costs, so exactness here is nearly free.
_cache: dict[tuple, tuple[list, str]] = {}
_CACHE_LIMIT = 32


def _fingerprint(paths: list[Path]) -> tuple | None:
    """Identifies the files by their content, or None if one cannot be read."""
    marks = []
    for path in paths:
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return None
        marks.append(digest)
    return tuple(marks)


def forget_cached_recipes() -> None:
    """Drop the parsed-recipe cache.  For tests, and after an upload."""
    _cache.clear()


def entry_key(entry) -> tuple:
    return (entry.category, entry.platform_key, entry.model_key, entry.component_key)


def merge_recipe_files(
    paths: list[Path], label: str = ""
) -> tuple[list, str]:
    """Read and merge every file of one recipe.

    Returns ``(entries, problem)``. ``problem`` is empty when the recipe is
    usable; otherwise it explains why, ready to show to an operator, and
    ``entries`` is empty. A recipe made of several files is rejected if *any*
    of them is unreadable or invalid: accepting the rest would quietly narrow
    the baseline, and a target that has silently gone missing is exactly the
    failure this tool exists to catch.
    """
    if not paths:
        return [], "no file is stored for this recipe."

    key = _fingerprint(paths)
    if key is not None and key in _cache:
        held, trouble = _cache[key]
        # A copy of the list, so a caller that adds or removes cannot corrupt
        # what the next caller is handed. The entries themselves are read-only.
        return list(held), trouble

    entries: list = []
    seen: set[tuple] = set()
    duplicates: list[str] = []

    for path in paths:
        name = Path(path).name
        try:
            raw = load_recipe(path, source_name=label or name)
        except (RecipeLoadError, OSError) as exc:
            return _remember(key, ([], f"{name}: {exc}"))

        report = validate_recipe(raw)
        if not report.ok:
            first = "; ".join(issue.render() for issue in report.errors[:2])
            return _remember(
                key, ([], f"{name}: {first or 'no usable targets were found.'}")
            )

        for entry in report.entries:
            signature = entry_key(entry)
            if signature in seen:
                duplicates.append(
                    f"{entry.model or entry.platform} / {entry.component}"
                )
                continue
            seen.add(signature)
            entries.append(entry)

    if not entries:
        return _remember(key, ([], "no usable targets were found."))
    if duplicates:
        logger.info(
            "Recipe %s: %d target(s) appeared in more than one file (%s); the "
            "first was used",
            label or "(unnamed)",
            len(duplicates),
            ", ".join(duplicates[:5]),
        )
    return _remember(key, (entries, ""))


def _remember(key, outcome: tuple[list, str]) -> tuple[list, str]:
    entries, trouble = outcome
    if key is not None:
        if len(_cache) >= _CACHE_LIMIT:
            _cache.clear()
        _cache[key] = (list(entries), trouble)
    return outcome
