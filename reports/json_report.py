"""Machine-readable export for downstream tooling."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def build_json(result) -> bytes:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": {"id": result.tenant_id, "name": result.tenant_name},
        "category": result.category,
        "recipe": result.recipe_name,
        "summary": result.summary,
        "comparison": [row.to_dict() for row in result.rows],
        "installed_raw": [row.to_dict() for row in result.raw_rows],
        "recipe_entries": [entry.to_dict() for entry in result.recipe_entries],
    }
    return json.dumps(payload, indent=2, default=str).encode("utf-8")
