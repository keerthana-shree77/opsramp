"""Version parsing and comparison.

Firmware versions are never compared as plain strings.  Each side is parsed
into a structured form first::

    "U54 v2.10"              -> prefix=u54  release=(2, 10)
    "8.0 Update 3"           -> release=(8, 0, 3)
    "8.0.3 build-24022510"   -> release=(8, 0, 3)  build=24022510
    "1.2.3-build123"         -> release=(1, 2, 3)  build=123

The comparison is deliberately conservative: anything it cannot interpret with
confidence is reported as UNABLE TO COMPARE rather than guessed at, because a
wrong "UPDATED" is far more damaging than an honest "I could not tell".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import (
    STATUS_NEEDS_UPDATE,
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_DETECTED,
    STATUS_UNABLE,
    STATUS_UPDATED,
)

NA_TOKENS = {
    "",
    "n/a",
    "na",
    "n a",
    "not applicable",
    "none",
    "null",
    "-",
    "--",
    "nil",
    "tbd",
}

_BUILD_PATTERNS = [
    re.compile(r"\bbuilds?[\s\-_:#]*([0-9]{3,})\b"),
    re.compile(r"\bbld[\s\-_:#]*([0-9]{3,})\b"),
    re.compile(r"-b([0-9]{3,})\b"),
]
_UPDATE_RE = re.compile(r"\bupdate[\s\-_]*([0-9]+)\b")
# "8.0 U3k" - a U-form update, but only when it follows the release. A leading
# "U54" is an HPE ROM family prefix and must never be read this way.
_U_UPDATE_RE = re.compile(r"^[\s\-_]*u([0-9]+)([a-z]?)\b")
_VERSION_MARKER_RE = re.compile(r"\bv(?=[0-9])|\bver(?:sion)?\b|\brev(?:ision)?\b")
_PREFIX_RE = re.compile(r"^\s*([a-z]{1,4}[0-9]{1,4}[a-z]?)(?=[\s\-_]|$)")
# Dots only: an underscore in vendor strings separates the version from a
# release date ("3.00_08-20-2026"), so it must not extend the release tuple.
_RELEASE_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_EXTRA_CLEAN_RE = re.compile(r"[^a-z0-9]+")

# Values that are clearly not a firmware version.
_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_MAC_RE = re.compile(r"^([0-9a-f]{2}[:\-]){5}[0-9a-f]{2}$", re.I)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")


def is_not_applicable(value: object) -> bool:
    return str(value or "").strip().lower() in NA_TOKENS


def looks_like_version(value: object, *, trust_name: bool = False) -> bool:
    """Cheap pre-filter used by the extractor to reject junk attributes.

    ``trust_name`` is set when the attribute's own name already identifies the
    component (for example "Server Platform Services (SPS) Firmware_
    SPSFirmwareVersionData"). Four dotted numbers are then read as a version
    rather than discarded as an IP address - SPS firmware really is versioned
    "4.1.5.201", which is indistinguishable in shape from an address.
    """
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return False
    low = text.lower()
    if low in NA_TOKENS:
        return False
    if _MAC_RE.match(text) or _UUID_RE.match(text):
        return False
    if not trust_name and _IPV4_RE.match(text):
        return False
    if _DATE_RE.match(text):
        return False
    if not re.search(r"\d", text):
        return False
    # Accept dotted versions, "U54 v2.12", bare "2.10", build numbers.
    if re.search(r"\d+\.\d+", text):
        return True
    if re.fullmatch(r"[A-Za-z]{0,4}\s?[vV]?\d{1,6}[A-Za-z]?", text.strip()):
        return True
    if re.search(r"\b(?:build|bld)[\s\-_:#]*\d{3,}", low):
        return True
    return False


@dataclass
class ParsedVersion:
    raw: str
    prefix: str | None = None
    release: tuple[int, ...] = ()
    extra: str = ""
    build: str | None = None
    parsed: bool = False
    notes: list[str] = field(default_factory=list)

    def release_text(self) -> str:
        return ".".join(str(part) for part in self.release)


def parse_version(value: object) -> ParsedVersion:
    raw = str(value or "").strip()
    result = ParsedVersion(raw=raw)
    if not raw or raw.lower() in NA_TOKENS:
        return result

    text = raw.lower()

    for pattern in _BUILD_PATTERNS:
        match = pattern.search(text)
        if match:
            result.build = match.group(1)
            text = text[: match.start()] + " " + text[match.end() :]
            break

    update_match = _UPDATE_RE.search(text)
    update_part: int | None = None
    if update_match:
        update_part = int(update_match.group(1))
        text = text[: update_match.start()] + " " + text[update_match.end() :]

    text = _VERSION_MARKER_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()

    prefix_match = _PREFIX_RE.match(text)
    if prefix_match:
        result.prefix = prefix_match.group(1)
        text = text[prefix_match.end() :].strip()

    release_match = _RELEASE_RE.search(text)
    if release_match:
        parts = re.split(r"[._]", release_match.group(0))
        try:
            result.release = tuple(int(p) for p in parts if p != "")
        except ValueError:  # pragma: no cover - regex guarantees digits
            result.release = ()
        tail = text[release_match.end() :]
        head = text[: release_match.start()]
        u_match = _U_UPDATE_RE.match(tail)
        if u_match and update_part is None:
            update_part = int(u_match.group(1))
            # Keep the patch letter ("k") as the revision suffix.
            tail = u_match.group(2) + tail[u_match.end() :]
        remainder = (head + " " + tail).strip()
        result.extra = _EXTRA_CLEAN_RE.sub("", remainder)

    if update_part is not None and result.release:
        result.release = result.release + (update_part,)
    elif update_part is not None and not result.release:
        result.release = (update_part,)

    result.parsed = bool(result.release) or bool(result.build)
    return result


def _cmp_release(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    a_padded = a + (0,) * (width - len(a))
    b_padded = b + (0,) * (width - len(b))
    if a_padded == b_padded:
        return 0
    return -1 if a_padded < b_padded else 1


def _natural_key(text: str) -> list:
    """Order a revision suffix the way a human reads it: c < c1 < c2 < d.

    Digit runs compare as numbers so that "c10" follows "c9", and a suffix
    that is a prefix of another comes first, so "c" precedes "c1".
    """
    parts: list = []
    for run in re.findall(r"\d+|\D+", text):
        if run.isdigit():
            parts.append((0, int(run), ""))
        else:
            parts.append((1, 0, run))
    return parts


def _cmp_extra(installed: str, target: str) -> int:
    """Compare two revision suffixes.  0 equal, 1 installed newer, -1 older."""
    left, right = _natural_key(installed), _natural_key(target)
    return (left > right) - (left < right)


def _policy_result(direction: int, policy: str, what: str, installed: str, target: str):
    """direction: 0 equal, 1 installed newer, -1 installed older."""
    if direction == 0:
        return STATUS_UPDATED, f"Installed {what} matches the approved target ({target})."
    if direction < 0:
        return (
            STATUS_NEEDS_UPDATE,
            f"Installed {what} {installed} is older than the approved target {target}.",
        )
    if policy == "exact":
        return (
            STATUS_NEEDS_UPDATE,
            f"Installed {what} {installed} does not match the approved target "
            f"{target} (installed is newer; policy requires an exact match).",
        )
    return (
        STATUS_UPDATED,
        f"Installed {what} {installed} is newer than the approved target {target}.",
    )


def compare_versions(
    installed_version: object,
    target_version: object,
    installed_build: object = None,
    target_build: object = None,
    *,
    policy: str = "at_least",
) -> dict:
    """Compare one installed value against one approved target value.

    Returns a dict with ``status``, the echoed values and a human ``reason``.
    """
    policy = (policy or "at_least").lower()
    installed_raw = str(installed_version or "").strip()
    target_raw = str(target_version or "").strip()
    installed_build_raw = str(installed_build or "").strip()
    target_build_raw = str(target_build or "").strip()

    out = {
        "status": STATUS_UNABLE,
        "installed_version": installed_raw,
        "target_version": target_raw,
        "installed_build": installed_build_raw,
        "target_build": target_build_raw,
        "reason": "",
    }

    if is_not_applicable(target_raw) and is_not_applicable(target_build_raw):
        out["status"] = STATUS_NOT_APPLICABLE
        out["reason"] = "The recipe marks this component as not applicable."
        return out

    if not installed_raw and not installed_build_raw:
        out["status"] = STATUS_NOT_DETECTED
        out["reason"] = (
            "The asset was found, but no recognised attribute containing this "
            "component's version was detected."
        )
        return out

    installed = parse_version(installed_raw)
    target = parse_version(target_raw)

    # Builds supplied inline ("8.0.3 build-24022510") count as build values.
    inst_build = installed_build_raw or (installed.build or "")
    targ_build = target_build_raw or (target.build or "")
    out["installed_build"] = inst_build
    out["target_build"] = targ_build

    # ------------------------------------------------------ build takes priority
    if targ_build:
        if not inst_build:
            # The recipe pins a build and OpsRamp reported none. The versions
            # are still comparable, and a verdict on them with the gap stated
            # is more use than refusing to answer - which is what ESXi did on
            # every host, because its build is published in the recipe and
            # rarely discoverable from the inventory.
            if installed.parsed and target.parsed:
                direction = _cmp_release(installed.release, target.release)
                what, shown_installed, shown_target = (
                    "version",
                    installed.release_text(),
                    target.release_text(),
                )
                if direction == 0 and installed.extra and target.extra:
                    direction = _cmp_extra(installed.extra, target.extra)
                    what = "revision"
                    shown_installed += installed.extra
                    shown_target += target.extra
                status, reason = _policy_result(
                    direction, policy, what, shown_installed, shown_target
                )
                out["status"] = status
                out["reason"] = (
                    f"{reason} The approved build {targ_build} could not be "
                    "verified because no installed build was detected."
                )
                return out
            out["status"] = STATUS_UNABLE
            out["reason"] = (
                f"The recipe specifies build {targ_build}, but no installed build "
                "number was detected for this asset, and the versions could not "
                "be compared either."
            )
            return out
        try:
            direction = (int(inst_build) > int(targ_build)) - (
                int(inst_build) < int(targ_build)
            )
        except ValueError:
            direction = 0 if inst_build == targ_build else None
            if direction is None:
                out["status"] = STATUS_UNABLE
                out["reason"] = (
                    f"Installed build {inst_build!r} and target build "
                    f"{targ_build!r} are not numerically comparable."
                )
                return out
        status, reason = _policy_result(direction, policy, "build", inst_build, targ_build)
        out["status"] = status
        out["reason"] = reason
        return out

    # ---------------------------------------------------------- version compare
    if not target.parsed:
        out["status"] = STATUS_UNABLE
        out["reason"] = (
            f"The recipe target version {target_raw!r} could not be interpreted "
            "as a version number."
        )
        return out
    if not installed.parsed:
        out["status"] = STATUS_UNABLE
        out["reason"] = (
            f"The installed value {installed_raw!r} could not be interpreted as a "
            "version number."
        )
        return out

    if installed.prefix and target.prefix and installed.prefix != target.prefix:
        out["status"] = STATUS_UNABLE
        out["reason"] = (
            f"Installed firmware family {installed.prefix.upper()} differs from the "
            f"recipe family {target.prefix.upper()}; these are not comparable. "
            "Confirm the recipe row applies to this model."
        )
        return out

    direction = _cmp_release(installed.release, target.release)
    status, reason = _policy_result(
        direction, policy, "version", installed.release_text(), target.release_text()
    )

    if direction == 0:
        if installed.extra and target.extra and installed.extra != target.extra:
            # "9.2.2c" against "9.2.2c1", or ESXi "8.0 U3i" against "8.0 U3k".
            # These are successive revisions of one release and they order
            # perfectly well, so the same three rules apply to them as to the
            # release numbers. Reporting them as incomparable told a reader
            # nothing they could act on.
            suffix_direction = _cmp_extra(installed.extra, target.extra)
            status, reason = _policy_result(
                suffix_direction,
                policy,
                "revision",
                f"{installed.release_text()}{installed.extra}",
                f"{target.release_text()}{target.extra}",
            )
            out["status"] = status
            out["reason"] = reason
            return out
        if installed.extra and not target.extra:
            reason += f" (vendor suffix {installed.extra!r} on the installed value was ignored)"
        if inst_build and not targ_build:
            reason += f" Installed build {inst_build} was not checked; the recipe specifies no build."

    if installed.prefix and not target.prefix:
        reason += (
            f" The installed value carries the prefix {installed.prefix.upper()}, "
            "which the recipe does not specify."
        )

    out["status"] = status
    out["reason"] = reason
    return out
