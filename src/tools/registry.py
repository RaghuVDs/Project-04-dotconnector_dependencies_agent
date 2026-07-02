"""Package registry client.

Deterministic tool. The agent NEVER guesses whether a version exists or what
the latest release is -- it asks this. PyPI's JSON API is public and free, so
this works with no credentials. Maven Central / npm have equivalent endpoints
(stubbed below) for when you extend the POC to other ecosystems.
"""
from __future__ import annotations

import functools
import json
import urllib.request

from packaging.version import InvalidVersion, Version

from ..models import Ecosystem

PYPI_JSON = "https://pypi.org/pypi/{package}/json"


class RegistryError(RuntimeError):
    pass


@functools.lru_cache(maxsize=256)
def _pypi_payload(package: str) -> dict:
    url = PYPI_JSON.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except Exception as exc:  # noqa: BLE001 - surface as a typed error
        raise RegistryError(f"could not reach PyPI for {package!r}: {exc}") from exc


class Registry:
    """Thin façade so the agent has one place to ask registry questions."""

    def list_versions(self, ecosystem: Ecosystem, package: str) -> list[str]:
        if ecosystem != Ecosystem.PYPI:
            # Extension point: add MavenCentral / npm clients here.
            raise RegistryError(f"registry lookups not implemented for {ecosystem}")
        payload = _pypi_payload(package)
        out: list[str] = []
        for ver, files in payload.get("releases", {}).items():
            if not files:  # yanked / empty release
                continue
            try:
                Version(ver)
            except InvalidVersion:
                continue
            out.append(ver)
        return sorted(out, key=Version)

    def version_exists(self, ecosystem: Ecosystem, package: str, version: str) -> bool:
        try:
            return version in set(self.list_versions(ecosystem, package))
        except RegistryError:
            return False

    def latest_version(self, ecosystem: Ecosystem, package: str) -> str | None:
        versions = self.list_versions(ecosystem, package)
        return versions[-1] if versions else None

    def latest_patch_within_minor(
        self, ecosystem: Ecosystem, package: str, base: str
    ) -> str | None:
        """Highest version that shares major.minor with `base` -- the safest bump."""
        try:
            b = Version(base)
        except InvalidVersion:
            return None
        same_line = [
            v
            for v in self.list_versions(ecosystem, package)
            if Version(v).release[:2] == b.release[:2] and Version(v) >= b
        ]
        return same_line[-1] if same_line else None
