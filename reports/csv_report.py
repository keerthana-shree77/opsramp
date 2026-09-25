"""CSV export of the comparison result."""
from __future__ import annotations

import csv
import io

from firmware.models import COMPARISON_COLUMNS, RAW_COLUMNS, RECIPE_COLUMNS


def _write(rows, columns) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([label for _key, label in columns])
    for row in rows:
        data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        writer.writerow([_cell(data.get(key)) for key, _label in columns])
    # utf-8-sig so Excel opens the file with the right encoding on Windows.
    return buffer.getvalue().encode("utf-8-sig")


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def comparison_csv(result) -> bytes:
    return _write(result.rows, COMPARISON_COLUMNS)


def raw_csv(result) -> bytes:
    return _write(result.raw_rows, RAW_COLUMNS)


def recipe_csv(result) -> bytes:
    return _write(result.recipe_entries, RECIPE_COLUMNS)
