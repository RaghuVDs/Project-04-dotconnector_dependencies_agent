"""Version math. Pure, deterministic, no I/O.

Classifying the size of a bump (patch / minor / major) is what drives the
auto-fix policy, so it must be exact -- never an LLM judgment call.
"""
from __future__ import annotations

from enum import Enum

from packaging.version import InvalidVersion, Version


class Delta(str, Enum):
    SAME = "same"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    DOWNGRADE = "downgrade"
    UNKNOWN = "unknown"


def parse(version: str) -> Version | None:
    try:
        return Version(version)
    except (InvalidVersion, TypeError):
        return None


def _triple(v: Version) -> tuple[int, int, int]:
    r = list(v.release) + [0, 0, 0]
    return r[0], r[1], r[2]


def classify(declared: str, recommended: str) -> Delta:
    a, b = parse(declared), parse(recommended)
    if a is None or b is None:
        return Delta.UNKNOWN
    if b == a:
        return Delta.SAME
    if b < a:
        return Delta.DOWNGRADE
    a_maj, a_min, _ = _triple(a)
    b_maj, b_min, _ = _triple(b)
    if b_maj != a_maj:
        return Delta.MAJOR
    if b_min != a_min:
        return Delta.MINOR
    return Delta.PATCH


def is_upgrade(declared: str, recommended: str) -> bool:
    a, b = parse(declared), parse(recommended)
    return a is not None and b is not None and b > a


def previous_versions(
    versions: list[str],
    current: str,
    floor: str | None = None,
    count: int = 1,
) -> list[str]:
    """Up to `count` registry versions immediately BELOW `current`, highest first.

    Each returned version is strictly greater than `floor` (the declared pin) so
    the step-down ladder never downgrades past where the repo already is -- it
    only ever tries *other upgrades*, just smaller ones than the recommendation.
    Pure version math; the registry supplies `versions` (the list of releases).
    """
    cur = parse(current)
    if cur is None:
        return []
    fl = parse(floor) if floor else None
    below: list[tuple[Version, str]] = []
    for v in versions:
        pv = parse(v)
        if pv is None or pv >= cur:
            continue
        if fl is not None and pv <= fl:
            continue
        below.append((pv, v))
    below.sort(key=lambda t: t[0], reverse=True)  # highest-below first
    return [s for _, s in below[:count]]
