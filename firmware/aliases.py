"""Operator-maintained model equivalences.

OpsRamp reports a part number ("P9R53A", "G620") where the vendor recipe names
a product line ("HPE Metered and Switched PDU", "SN6600B"). Nothing in either
source states that these are the same device, so the equivalence has to be
declared by someone who knows the estate rather than guessed by the tool.

An alias may be scoped to a single component. That is how a platform whose
recipe section omits one component borrows that target from comparable
hardware - the Alletra Storage Server's section states no iLO target, so its
iLO is compared against the Gen11 servers' - while its BIOS and SPS keep the
targets its own section does state. A direct match always wins over an alias,
so a scoped alias can only fill a genuine gap.

The file is plain CSV so it can be edited without touching code, and every
alias that takes effect is named in the report's Reason column.
"""
from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .normalizer import norm_text, normalize_component

logger = logging.getLogger(__name__)

# The declarations are about one estate, not about the tool, so a site that
# would rather not commit its account names can keep the file elsewhere -
# beside the .env, in a config volume - and name it here. Unset, the copy
# shipped with the source is used, which is what a fresh checkout expects.
_SHIPPED_PATH = Path(__file__).resolve().parent.parent / "model_aliases.csv"
DEFAULT_PATH = Path(os.getenv("MODEL_ALIASES_PATH", "").strip() or _SHIPPED_PATH)

_COLUMN_NAMES = {
    "opsramp model": "source",
    "model": "source",
    "recipe model": "recipe",
    "target model": "recipe",
    "component": "component",
    "tenant": "tenant",
    "tenants": "tenant",
    "account": "tenant",
    "accounts": "tenant",
    "notes": "notes",
    "note": "notes",
}

# Several accounts may share one alias: "grr01;grr02". A semicolon rather than
# a comma, so the cell needs no quoting in a CSV.
_SCOPE_SPLIT = re.compile(r"[;|]")


@dataclass(frozen=True)
class Alias:
    recipe_model: str
    component_key: str | None = None  # None means "every component"
    # Accounts this alias is limited to; empty means every account. The same
    # OpsRamp model can stand for different hardware in different accounts -
    # "HP_3PAR" is one estate's label for two distinct arrays - so an
    # equivalence is not always estate-wide.
    tenants: tuple[str, ...] = ()

    def applies_to(self, tenant: str) -> bool:
        if not self.tenants:
            return True
        if not tenant:
            return False
        present = set(norm_text(tenant).split())
        return any(
            set(scope.split()) <= present for scope in self.tenants if scope
        )


_cache: dict[str, list[Alias]] | None = None
_cache_path: Path | None = None


def _map_columns(header: list[str]) -> dict[str, int] | None:
    """Positions of the known columns, or None when there is no header."""
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        field = _COLUMN_NAMES.get(norm_text(cell))
        if field and field not in mapping:
            mapping[field] = index
    if "source" in mapping and "recipe" in mapping:
        return mapping
    return None


def load_aliases(
    path: str | Path | None = None, *, force: bool = False
) -> dict[str, list[Alias]]:
    """Map a normalized OpsRamp model onto the aliases declared for it."""
    global _cache, _cache_path

    # Once a table is loaded it stays loaded: aliases_for() asks without a
    # path, and must not silently fall back to the default file when the
    # caller loaded a different one.
    if path is None:
        if _cache is not None and not force:
            return _cache
        target = _cache_path or DEFAULT_PATH
    else:
        target = Path(path)
        if not force and _cache is not None and _cache_path == target:
            return _cache

    aliases: dict[str, list[Alias]] = {}
    if target.exists():
        try:
            with target.open(newline="", encoding="utf-8-sig") as handle:
                rows = [row for row in csv.reader(handle)]
        except OSError as exc:
            logger.error("Could not read %s: %s", target.name, exc)
            rows = []

        columns = None
        for row in rows:
            if not row:
                continue
            first = (row[0] or "").strip()
            if not first or first.startswith("#"):
                continue
            if columns is None:
                mapped = _map_columns(row)
                if mapped is not None:
                    columns = mapped
                    continue  # that was the header
                columns = {"source": 0, "recipe": 1, "notes": 2}

            def cell(field: str) -> str:
                index = columns.get(field)
                if index is None or index >= len(row):
                    return ""
                return (row[index] or "").strip()

            source = norm_text(cell("source"))
            recipe_model = cell("recipe")
            if not source or not recipe_model:
                continue

            scope_text = cell("component")
            component_key = None
            if scope_text:
                component_key = normalize_component(scope_text, recipe_context=True)
                if component_key is None:
                    logger.error(
                        "%s: component %r is not one this tool compares; the alias "
                        "%s -> %s was ignored",
                        target.name,
                        scope_text,
                        cell("source"),
                        recipe_model,
                    )
                    continue

            scopes = tuple(
                norm_text(part)
                for part in _SCOPE_SPLIT.split(cell("tenant"))
                if norm_text(part)
            )
            aliases.setdefault(source, []).append(
                Alias(recipe_model, component_key, scopes)
            )
        if aliases:
            logger.info("Loaded %d model alias(es) from %s", len(aliases), target.name)
    else:
        logger.info("No model alias file at %s; continuing without aliases", target)

    _cache = aliases
    _cache_path = target
    return aliases


def aliases_for(
    model: str, component_key: str | None = None, tenant: str = ""
) -> list[str]:
    """Recipe model texts the given OpsRamp model is equivalent to.

    Matches the whole model first, then any single token of it, so
    "8325-32C (JL636A)" can be mapped by its bare code. An alias scoped to a
    component is only returned when that component is being matched.

    An alias naming accounts beats one that names none, for those accounts.
    That is what lets a file say "this model means X everywhere, except in
    these four accounts where it means Y" without the two contradicting each
    other - and without the estate-wide row having to list every account it
    does not cover.
    """
    table = load_aliases()
    if not table or not model:
        return []

    normalized = norm_text(model)
    keys = [normalized] + [t for t in normalized.split() if t != normalized]

    scoped: list[str] = []
    general: list[str] = []
    for key in keys:
        for alias in table.get(key, []):
            if alias.component_key is not None and alias.component_key != component_key:
                continue
            if not alias.applies_to(tenant):
                continue
            into = scoped if alias.tenants else general
            if alias.recipe_model not in into:
                into.append(alias.recipe_model)
    return scoped or general
