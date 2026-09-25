"""Dashboard summary cards, filter options and the text report."""
from __future__ import annotations

from firmware.models import (
    ALL_STATUSES,
    STATUS_NEEDS_UPDATE,
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_DETECTED,
    STATUS_NOT_IN_RECIPE,
    STATUS_TONE,
    STATUS_UNABLE,
    STATUS_UPDATED,
)

CARD_ORDER = [
    STATUS_UPDATED,
    STATUS_NEEDS_UPDATE,
    STATUS_NOT_DETECTED,
    STATUS_NOT_IN_RECIPE,
    STATUS_UNABLE,
    STATUS_NOT_APPLICABLE,
]


def summary_cards(result) -> list[dict]:
    counts = result.summary.get("counts", {})
    cards = [
        {"label": "Total Assets", "value": result.summary.get("assets_in_scope", 0),
         "tone": "neutral"},
        {"label": "Components Checked", "value": result.summary.get("components_checked", 0),
         "tone": "neutral"},
    ]
    for status in CARD_ORDER:
        value = counts.get(status, 0)
        if value == 0 and status in (STATUS_NOT_APPLICABLE, STATUS_UNABLE):
            continue
        cards.append(
            {
                "label": status.title() if status != STATUS_UPDATED else "Updated",
                "value": value,
                "tone": STATUS_TONE.get(status, "neutral"),
                "status": status,
            }
        )
    return cards


def compliance_rate(result) -> float:
    counts = result.summary.get("counts", {})
    considered = counts.get(STATUS_UPDATED, 0) + counts.get(STATUS_NEEDS_UPDATE, 0)
    if not considered:
        return 0.0
    return round(100.0 * counts.get(STATUS_UPDATED, 0) / considered, 1)


def filter_options(result) -> dict[str, list[str]]:
    def distinct(attribute: str) -> list[str]:
        values = {getattr(row, attribute, "") or "" for row in result.rows}
        values.discard("")
        return sorted(values, key=str.lower)

    return {
        "tenant": distinct("tenant"),
        "category": distinct("category"),
        "platform": distinct("platform"),
        "model": distinct("model"),
        "component": distinct("component"),
        "result": [s for s in ALL_STATUSES if any(r.result == s for r in result.rows)],
    }


def text_report(result) -> str:
    """The plain-text banner form of the report, useful for e-mail or logs."""
    counts = result.summary.get("counts", {})
    line = "=" * 56
    parts = [
        line,
        "       OPSRAMP FIRMWARE COMPLIANCE REPORT",
        line,
        "",
        f"Tenant:   {result.tenant_name}",
        f"Category: {result.summary.get('category', result.category)}",
        f"Recipe:   {result.recipe_name}",
        "",
        f"Total Assets:             {result.summary.get('assets_in_scope', 0)}",
        f"Components Checked:       {result.summary.get('components_checked', 0)}",
        f"Updated:                  {counts.get(STATUS_UPDATED, 0)}",
        f"Needs Update:             {counts.get(STATUS_NEEDS_UPDATE, 0)}",
        f"Version Not Detected:     {counts.get(STATUS_NOT_DETECTED, 0)}",
        f"Not Found in Recipe:      {counts.get(STATUS_NOT_IN_RECIPE, 0)}",
        f"Unable To Compare:        {counts.get(STATUS_UNABLE, 0)}",
        f"Not Applicable:           {counts.get(STATUS_NOT_APPLICABLE, 0)}",
        line,
    ]
    return "\n".join(parts)


def has_target(row) -> bool:
    """Whether the recipe had something to say about this component.

    Used for coverage, which answers a different question from compliance:
    not "is this up to date" but "does this recipe describe this estate at
    all".  A row that matched a recipe entry carries the approved version even
    when the installed one could not be read, so the presence of a target is
    what separates a gap in the recipe from a gap in the inventory.
    """
    return bool(
        getattr(row, "target_version", "")
        or getattr(row, "target_build", "")
        or row.result == STATUS_NOT_APPLICABLE
    )


def tenant_compliance(result) -> list[dict]:
    """Per-tenant compliance, computed from the rows of one completed run.

    A run may cover several tenants, so the overview is built from the rows
    rather than from the run's own totals. The rate counts only what could
    actually be compared - UPDATED against NEEDS UPDATE. Components with no
    target, or whose version was not detected, are reported separately rather
    than being folded into a percentage they would quietly distort.
    """
    by_tenant: dict[str, dict] = {}
    for row in result.rows:
        name = row.tenant or "(unnamed tenant)"
        stats = by_tenant.setdefault(
            name,
            {
                "tenant": name,
                "assets": set(),
                "components": 0,
                "covered": 0,
                "counts": {status: 0 for status in ALL_STATUSES},
            },
        )
        stats["assets"].add(row.asset_id or row.hostname)
        stats["components"] += 1
        stats["covered"] += 1 if has_target(row) else 0
        if row.result in stats["counts"]:
            stats["counts"][row.result] += 1

    out: list[dict] = []
    for stats in by_tenant.values():
        counts = stats["counts"]
        updated = counts[STATUS_UPDATED]
        comparable = updated + counts[STATUS_NEEDS_UPDATE]
        components = stats["components"]
        covered = stats["covered"]
        out.append(
            {
                "tenant": stats["tenant"],
                "assets": len(stats["assets"]),
                "components": components,
                "counts": counts,
                "comparable": comparable,
                "updated": updated,
                "needs_update": counts[STATUS_NEEDS_UPDATE],
                "not_detected": counts[STATUS_NOT_DETECTED],
                "not_in_recipe": counts[STATUS_NOT_IN_RECIPE],
                "covered": covered,
                "coverage": round(100.0 * covered / components, 1) if components else None,
                "rate": round(100.0 * updated / comparable, 1) if comparable else None,
            }
        )
    out.sort(key=lambda s: (s["rate"] is not None, -(s["rate"] or 0), s["tenant"]))
    return out


# The bands the operator reads the grid by. Above 90 is green, 80 to 90 is
# amber, below 80 is red. A cell with nothing comparable in it gets no colour
# at all rather than a red one: there is no percentage to judge, and colouring
# it red would report a gap in the recipe as a compliance failure.
TONE_GREEN_AT = 90.0
TONE_AMBER_AT = 80.0


def compliance_tone(rate) -> str:
    """Traffic-light band for a compliance percentage."""
    if rate is None:
        return "muted"
    if rate > TONE_GREEN_AT:
        return "ok"
    if rate >= TONE_AMBER_AT:
        return "warn"
    return "bad"


def tenant_category_matrix(result) -> dict[str, dict[str, dict]]:
    """Compliance per tenant per category, as ``{tenant: {category: stats}}``.

    Keyed on the displayed category text, which is what the results table
    filters on, so a cell can link straight to its own rows.
    """
    grid: dict[str, dict[str, dict]] = {}
    for row in result.rows:
        tenant = row.tenant or "(unnamed tenant)"
        category = row.category or "(uncategorised)"
        cell = grid.setdefault(tenant, {}).setdefault(
            category,
            {
                "assets": set(),
                "components": 0,
                "covered": 0,
                "no_target_models": set(),
                "counts": {status: 0 for status in ALL_STATUSES},
            },
        )
        cell["assets"].add(row.asset_id or row.hostname)
        cell["components"] += 1
        cell["covered"] += 1 if has_target(row) else 0
        if row.result == STATUS_NOT_IN_RECIPE:
            # Named so that a cell with no percentage can say which equipment
            # the recipe is missing, rather than only how much of it there is.
            cell["no_target_models"].add(row.model or row.platform or "(no model)")
        if row.result in cell["counts"]:
            cell["counts"][row.result] += 1

    for categories in grid.values():
        for category, cell in categories.items():
            counts = cell["counts"]
            updated = counts[STATUS_UPDATED]
            comparable = updated + counts[STATUS_NEEDS_UPDATE]
            cell["assets"] = len(cell["assets"])
            cell["updated"] = updated
            cell["needs_update"] = counts[STATUS_NEEDS_UPDATE]
            cell["not_detected"] = counts[STATUS_NOT_DETECTED]
            cell["not_in_recipe"] = counts[STATUS_NOT_IN_RECIPE]
            cell["comparable"] = comparable
            cell["rate"] = (
                round(100.0 * updated / comparable, 1) if comparable else None
            )
            cell["tone"] = compliance_tone(cell["rate"])
            # What a reader of this cell would act on.
            cell["pending"] = cell["needs_update"] + cell["not_detected"]
            cell["coverage"] = (
                round(100.0 * cell["covered"] / cell["components"], 1)
                if cell["components"]
                else None
            )
            cell["no_target_models"] = sorted(cell["no_target_models"])
    return grid
