"""Recipe validation.

Extraction never starts unless the recipe passes: an invalid Y-axis produces a
misleading compliance report, which is worse than no report at all.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from firmware.models import RecipeEntry

from .loader import REQUIRED_FIELDS, RawRecipe
from .normalizer import normalize_row, signature

logger = logging.getLogger(__name__)

_FIELD_LABELS = {
    "platform": "Platform",
    "model": "Model",
    "category": "Category",
    "component": "Component",
    "target_version": "Target Version",
    "target_build": "Target Build",
}


@dataclass
class Issue:
    row: int | None
    message: str

    def render(self) -> str:
        if self.row:
            return f"Row {self.row}: {self.message}"
        return self.message


@dataclass
class ValidationReport:
    entries: list[RecipeEntry] = field(default_factory=list)
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    total_rows: int = 0
    skipped_empty: int = 0
    # Set by the web layer when the uploaded file is kept for adoption.
    stored_path: object = None

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.entries)


def validate_recipe(raw: RawRecipe) -> ValidationReport:
    report = ValidationReport(total_rows=len(raw.rows))

    # Lines the matrix reader could not turn into a target. These are warnings,
    # not errors, but they must be visible: a silently dropped component is an
    # unnoticed gap in the baseline.
    for note in raw.notes[:40]:
        report.warnings.append(Issue(None, note))
    if len(raw.notes) > 40:
        report.warnings.append(
            Issue(None, f"...and {len(raw.notes) - 40} further unused line(s).")
        )

    missing = raw.missing_required
    if missing:
        for name in missing:
            report.errors.append(
                Issue(
                    None,
                    f"Required column {_FIELD_LABELS.get(name, name)!r} is missing "
                    "from the recipe.",
                )
            )
        return report

    seen: dict[tuple, int] = {}

    for row in raw.rows:
        row_number = int(row.get("_row") or 0)
        if row.get("_empty"):
            report.skipped_empty += 1
            continue

        for name in REQUIRED_FIELDS:
            if name == "target_version":
                continue
            if not (row.get(name) or "").strip():
                report.errors.append(
                    Issue(row_number, f"{_FIELD_LABELS[name]} is missing.")
                )

        target = (row.get("target_version") or "").strip()
        build = (row.get("target_build") or "").strip()
        if not target and not build:
            report.errors.append(Issue(row_number, "Target Version is missing."))

        entry, problems = normalize_row(row)
        for problem in problems:
            report.errors.append(Issue(row_number, problem))

        if not entry.model_key and not entry.applies_to_family:
            report.warnings.append(
                Issue(
                    row_number,
                    "Model is empty, so this row will only be used as a "
                    "platform-family fallback.",
                )
            )

        if entry.component_key and entry.platform_key:
            key = signature(entry)
            previous = seen.get(key)
            if previous is not None:
                report.errors.append(
                    Issue(
                        row_number,
                        f"Duplicate entry for {entry.model or entry.platform} / "
                        f"{entry.component} (first seen on row {previous}).",
                    )
                )
            else:
                seen[key] = row_number

        report.entries.append(entry)

    if not report.entries and not report.errors:
        report.errors.append(Issue(None, "The recipe contains no data rows."))

    logger.info(
        "Recipe validation: %d row(s), %d entr(ies), %d error(s), %d warning(s)",
        report.total_rows,
        len(report.entries),
        len(report.errors),
        len(report.warnings),
    )
    return report
