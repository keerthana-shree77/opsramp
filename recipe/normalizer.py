"""Turn raw recipe rows into canonical :class:`RecipeEntry` objects."""
from __future__ import annotations

from firmware.comparator import is_not_applicable, parse_version
from firmware.models import RecipeEntry
from firmware.normalizer import (
    CATEGORY_ALL,
    detect_platform,
    is_wildcard_model,
    model_tokens,
    norm_text,
    normalize_category,
    normalize_component,
    normalize_model,
    component_display,
    platform_display,
)

_TRUE = {"yes", "y", "true", "1", "mandatory", "required", "enforced"}
_FALSE = {"no", "n", "false", "0", "optional", "informational"}


def parse_bool(value: object, default: bool = False) -> bool:
    text = norm_text(value)
    if not text:
        return default
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def normalize_row(row: dict) -> tuple[RecipeEntry, list[str]]:
    """Normalize one raw row.  Returns the entry plus any field-level problems."""
    problems: list[str] = []

    platform_raw = (row.get("platform") or "").strip()
    model_raw = (row.get("model") or "").strip()
    category_raw = (row.get("category") or "").strip()
    component_raw = (row.get("component") or "").strip()
    target_raw = (row.get("target_version") or "").strip()
    build_raw = (row.get("target_build") or "").strip()

    # The platform is often only fully determined by platform + model together
    # ("Superdome Flex" + "SD Flex 280" -> superdome_flex_280).
    platform_key, platform_name, _hit = detect_platform(
        f"{platform_raw} {model_raw}".strip()
    )
    if not platform_key:
        platform_key, platform_name, _hit = detect_platform(platform_raw)
    if not platform_key:
        problems.append(
            f"Platform {platform_raw!r} is not recognised as a supported platform."
        )

    category = normalize_category(category_raw)
    if category is None and category_raw:
        problems.append(f"Category {category_raw!r} is not a supported category.")
    if category == CATEGORY_ALL:
        category = ""

    component_key = normalize_component(component_raw, recipe_context=True)
    if component_key is None and component_raw:
        problems.append(f"Component {component_raw!r} is not a supported component.")

    not_applicable = is_not_applicable(target_raw) and is_not_applicable(build_raw)
    if not not_applicable and target_raw and not build_raw:
        if not parse_version(target_raw).parsed:
            problems.append(
                f"Target Version {target_raw!r} is not a recognisable version value."
            )
    if build_raw and not build_raw.isdigit():
        problems.append(f"Target Build {build_raw!r} must be numeric.")

    entry = RecipeEntry(
        row_number=int(row.get("_row") or 0),
        platform=platform_raw or platform_display(platform_key),
        platform_key=platform_key or "",
        model=model_raw,
        model_key=normalize_model(model_raw),
        model_tokens=() if is_wildcard_model(model_raw) else model_tokens(model_raw),
        category=category or "",
        component=component_display(component_key) if component_key else component_raw,
        component_key=component_key or "",
        target_version=None if not_applicable else (target_raw or None),
        target_build=build_raw or None,
        mandatory=parse_bool(row.get("mandatory"), default=True),
        not_applicable=not_applicable,
        applies_to_family=parse_bool(row.get("applies_to_family"), default=False)
        or is_wildcard_model(model_raw),
        raw=dict(row.get("_raw") or {}),
    )
    return entry, problems


def signature(entry: RecipeEntry) -> tuple[str, str, str, str]:
    """Identity used for duplicate detection."""
    return (
        entry.category or CATEGORY_ALL,
        entry.platform_key,
        entry.model_key,
        entry.component_key,
    )
