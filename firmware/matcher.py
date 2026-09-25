"""Deterministic asset-to-recipe matching.

Three levels, tried in order.  Broad matching is opt-in, never accidental:

  L1 exact       category + platform + model + component
  L2 normalized  category + platform family + model-token subset + component
  L2b model code the model code both sides share (8325, 6300M, SN6600B)
  L2c model alias an equivalence declared in model_aliases.csv
  L3 family      category + platform + component, but only when the recipe row
                 declares a wildcard model or sets "Applies To Family"

If two equally-specific recipe rows disagree about the target version, the
match is reported as ambiguous rather than silently picking one - an incorrect
firmware recommendation is worse than an unanswered one.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Asset, RecipeEntry
from .aliases import aliases_for
from .normalizer import CATEGORY_ALL, model_codes, model_tokens

# A specific platform may be served by a recipe row written for its family.
FAMILY_PARENT = {
    "superdome_flex_280": "superdome_flex",
    "csus_3200": "csus",
}

# Tokens that say which machine of a family this is. Two models sharing a
# code but naming different generations are different hardware.
_GENERATION_TOKENS = ("plus", "v2", "v3", "v4")


def _generation(tokens: set[str]) -> frozenset[str]:
    """The generation markers in a model's tokens: gen10, gen11, plus..."""
    return frozenset(
        token
        for token in tokens
        if token.startswith("gen") and token[3:].isdigit()
        or token in _GENERATION_TOKENS
    )


def _model_variants(model: str, tokens) -> list[tuple[set, frozenset]]:
    """The machines a recipe model names.  Usually one; sometimes two.

    The vendor heads a section "HPE ProLiant DL380 Gen10/Gen10 Plus Details"
    and lists one set of targets under it, meaning both machines. Read as a
    single model it names a generation of "gen10 *and* plus", which is
    neither of them: the Gen10 Plus matched by accident of containment and
    the plain Gen10 matched nothing at all.

    A slash is only read this way when every segment names a generation.
    "DL360/DL380 Gen11" says nothing reliable about which half carries which
    model, so it is left exactly as it was rather than guessed at.
    """
    whole = set(tokens)
    plain = [(whole, _generation(whole))]
    if "/" not in (model or ""):
        return plain

    segments = [part.strip() for part in model.split("/") if part.strip()]
    if len(segments) < 2:
        return plain
    parts = [model_tokens(segment) for segment in segments]
    generations = [_generation(set(part)) for part in parts]
    if not all(generations):
        return plain

    # Everything before the first generation marker is the model itself, and
    # the later segments carry only their own generation: "DL380 Gen10" then
    # "Gen10 Plus" means the DL380 Gen10 and the DL380 Gen10 Plus.
    base = [token for token in parts[0] if token not in generations[0]]
    if not base:
        return plain

    variants = []
    for part, generation in zip(parts, generations):
        named = [token for token in part if token in generation]
        variants.append((set(base) | set(named), generation))
    return variants


LEVEL_EXACT = "L1 exact"
LEVEL_NORMALIZED = "L2 normalized"
LEVEL_CODE = "L2b model code"
LEVEL_ALIAS = "L2c model alias"
LEVEL_FAMILY = "L3 family"


@dataclass
class MatchResult:
    entry: RecipeEntry | None = None
    level: str = ""
    ambiguous: bool = False
    note: str = ""


def _platform_keys(platform_key: str) -> list[str]:
    keys = []
    current = platform_key
    while current and current not in keys:
        keys.append(current)
        current = FAMILY_PARENT.get(current, "")
    return keys


def _category_ok(entry: RecipeEntry, asset: Asset) -> bool:
    if not entry.category or entry.category == CATEGORY_ALL:
        return True
    return entry.category == asset.category


def _targets_agree(entries: list[RecipeEntry]) -> bool:
    signatures = {
        (
            (e.target_version or "").strip().lower(),
            (e.target_build or "").strip().lower(),
            e.not_applicable,
        )
        for e in entries
    }
    return len(signatures) <= 1


class RecipeIndex:
    """Immutable lookup structure built once per comparison run."""

    def __init__(self, entries: list[RecipeEntry]) -> None:
        self.entries = list(entries)
        self._by_component: dict[str, list[RecipeEntry]] = {}
        for entry in self.entries:
            if not entry.component_key:
                continue
            self._by_component.setdefault(entry.component_key, []).append(entry)

    def __len__(self) -> int:
        return len(self.entries)

    def components(self) -> set[str]:
        return set(self._by_component)

    def match(self, asset: Asset, component_key: str) -> MatchResult:
        pool = self._by_component.get(component_key, [])
        if not pool:
            return MatchResult(note="No recipe row defines this component at all.")

        asset_platforms = _platform_keys(asset.platform_key)
        asset_tokens = set(model_tokens(asset.model or asset.resource_name))

        # An operator-declared alias widens what the asset's model matches
        # without changing what the report shows it as.
        alias_texts = aliases_for(
            asset.model or asset.resource_name, component_key, asset.tenant
        )

        # ---------------------------------------------------------- L1 exact
        exact = [
            e
            for e in pool
            if _category_ok(e, asset)
            and e.platform_key == asset.platform_key
            and e.model_key
            and e.model_key == asset.model_key
        ]
        if exact:
            return self._resolve(exact, LEVEL_EXACT)

        # ----------------------------------------------------- L2 normalized
        asset_generation = _generation(asset_tokens)
        scored: list[tuple[int, RecipeEntry]] = []
        for entry in pool:
            if not _category_ok(entry, asset):
                continue
            if entry.platform_key and entry.platform_key not in asset_platforms:
                continue
            if not entry.model_tokens:
                continue
            # "DL380 Gen10" is a subset of "ProLiant DL380 Gen10 Plus", but a
            # Gen10 Plus is not a Gen10 - different machine, different ROM
            # family. Containment alone would hand it the wrong target
            # whenever its own row is absent from the recipe.
            #
            # Only a row that names a generation is held to this. A row for a
            # bare "DL380" names none, and deliberately covers every
            # generation of it - and a row naming several covers each.
            for recipe_tokens, recipe_generation in _model_variants(
                entry.model, entry.model_tokens
            ):
                if recipe_generation and recipe_generation != asset_generation:
                    continue
                if recipe_tokens and recipe_tokens.issubset(asset_tokens):
                    scored.append((len(recipe_tokens), entry))
                    break
        if scored:
            top = max(score for score, _ in scored)
            best = [entry for score, entry in scored if score == top]
            return self._resolve(best, LEVEL_NORMALIZED)

        # -------------------------------------------------- L2b model codes
        # Vendor prose and OpsRamp rarely share a whole model string, but they
        # do share the code that identifies the model (8325, 6300M, SN6700B).
        asset_codes = model_codes(asset.model or asset.resource_name)
        if asset_codes:
            by_code: list[tuple[int, RecipeEntry]] = []
            for entry in pool:
                if not _category_ok(entry, asset):
                    continue
                if entry.platform_key and entry.platform_key not in asset_platforms:
                    continue
                codes = model_codes(entry.model)
                # The code alone is not enough. "DL380" is common to the
                # DL380 Gen10, the Gen10 Plus and the Gen11, which are three
                # different machines with three different ROM families - and
                # handing one of them another's target reads as a real finding
                # while being entirely wrong. Where both sides name a
                # generation, they have to name the same one. Where neither
                # does the code stands on its own, which is what this level is
                # for: "6300M 48-port 1GbE" and "6300M 48G (JL762A)" share no
                # tokens beyond the code and are the same switch.
                generations = [
                    generation
                    for _tokens, generation in _model_variants(
                        entry.model, entry.model_tokens
                    )
                ]
                named = [g for g in generations if g]
                if named and asset_generation not in named:
                    continue
                if codes and codes <= asset_codes:
                    by_code.append((len(codes), entry))
            if by_code:
                top = max(score for score, _ in by_code)
                best = [entry for score, entry in by_code if score == top]
                return self._resolve(best, LEVEL_CODE)

        # -------------------------------------------------- L2c model alias
        # Each declared alias is evaluated on its own. Pooling their tokens
        # would let a longer alias mask a shorter one that points at a
        # different target, hiding a contradiction in the operator's file.
        if alias_texts:
            picked: list[RecipeEntry] = []
            for text in alias_texts:
                alias_tokens = set(model_tokens(text))
                alias_codes = set(model_codes(text))
                if not alias_tokens and not alias_codes:
                    continue
                scored_alias: list[tuple[int, RecipeEntry]] = []
                for entry in pool:
                    if not _category_ok(entry, asset):
                        continue
                    # No platform gate here, unlike the levels above. An alias
                    # is an explicit statement that this hardware is covered by
                    # that recipe row, and the row may well sit under another
                    # platform - an Alletra Storage Server borrowing the Gen11
                    # servers' iLO target is the case this exists for. The
                    # category still has to agree, and the model text still has
                    # to match.
                    tokens = set(entry.model_tokens)
                    codes = set(model_codes(entry.model))
                    # A shared model code is not enough on its own: "DL380"
                    # is common to DL380 Gen11 and DL380 Gen10, and borrowing
                    # a Gen10 target for a Gen11 machine would be wrong. One
                    # side's tokens must therefore contain the other's, and
                    # the more tokens shared the better the match, so an exact
                    # generation outranks a code-only coincidence.
                    compatible = bool(
                        tokens
                        and alias_tokens
                        and (tokens <= alias_tokens or alias_tokens <= tokens)
                    )
                    if alias_codes and codes and codes <= alias_codes and compatible:
                        scored_alias.append(
                            (100 + len(codes) + len(tokens & alias_tokens), entry)
                        )
                    elif tokens and tokens <= alias_tokens:
                        scored_alias.append((len(tokens), entry))
                if not scored_alias:
                    continue
                top = max(score for score, _ in scored_alias)
                for score, entry in scored_alias:
                    if score == top and entry not in picked:
                        picked.append(entry)
            if picked:
                result = self._resolve(picked, LEVEL_ALIAS)
                result.note = (
                    f"Matched through the model alias "
                    f"{', '.join(repr(t) for t in alias_texts)} declared in "
                    f"model_aliases.csv. {result.note}".strip()
                )
                return result

        # --------------------------------------------------------- L3 family
        family = [
            e
            for e in pool
            if _category_ok(e, asset)
            and (not e.platform_key or e.platform_key in asset_platforms)
            and (e.applies_to_family or not e.model_key)
        ]
        if family:
            return self._resolve(family, LEVEL_FAMILY)

        # Name what the recipe does offer for this component, so the gap is
        # actionable: usually the recipe calls the same hardware something
        # else, which model_aliases.csv exists to reconcile.
        candidates = sorted(
            {e.model for e in pool if e.model and _category_ok(e, asset)}
        )
        note = "No recipe entry exists for this platform/model/component combination."
        if candidates:
            shown = ", ".join(repr(c) for c in candidates[:6])
            if len(candidates) > 6:
                shown += f", and {len(candidates) - 6} more"
            note += (
                f" The recipe lists this component for: {shown}. If one of those "
                f"is the same hardware as {asset.model or 'this asset'!r}, declare "
                "it in model_aliases.csv."
            )
        return MatchResult(note=note)

    @staticmethod
    def _resolve(entries: list[RecipeEntry], level: str) -> MatchResult:
        if len(entries) == 1:
            return MatchResult(entry=entries[0], level=level)
        if _targets_agree(entries):
            return MatchResult(entry=entries[0], level=level)
        rows = ", ".join(str(e.row_number) for e in sorted(entries, key=lambda e: e.row_number))
        return MatchResult(
            entry=None,
            level=level,
            ambiguous=True,
            note=(
                f"Multiple recipe rows ({rows}) match this asset at {level} with "
                "different target versions. Make the recipe more specific."
            ),
        )
