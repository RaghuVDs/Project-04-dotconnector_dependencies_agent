"""Manifest location and editing.

This is where the "works across languages" requirement lives. The POC ships a
real requirements.txt editor and a pyproject.toml editor; pom.xml / package.json
/ go.mod are left as clearly-marked extension points so the shape is obvious.

Edits are format-preserving (we rewrite only the version token, not the whole
file) and reversible (`ManifestEdit.restore()`), which the retry loop relies on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Ecosystem

# requirement line: name [extras] op version [; markers] [# comment]
_REQ_RE = re.compile(
    r"^(?P<pre>\s*)"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[^\]]*\])?"
    r"(?P<sp1>\s*)"
    r"(?P<op>==|===|~=|>=|<=|!=|<|>)"
    r"(?P<sp2>\s*)"
    r"(?P<ver>[A-Za-z0-9][A-Za-z0-9._*+!-]*)"
    r"(?P<rest>.*)$"
)

MANIFEST_GLOBS = {
    Ecosystem.PYPI: ["requirements*.txt", "**/requirements*.txt", "pyproject.toml"],
}


def normalize(name: str) -> str:
    """PEP 503 normalization so 'Flask', 'flask', 'pytest_runner' all match."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class ManifestEdit:
    path: Path
    package: str
    old_version: str
    new_version: str
    _original_text: str = field(repr=False, default="")

    def restore(self) -> None:
        """Roll the file back -- used between retry attempts."""
        self.path.write_text(self._original_text, encoding="utf-8")


def find_manifests(repo_dir: Path, ecosystem: Ecosystem) -> list[Path]:
    globs = MANIFEST_GLOBS.get(ecosystem)
    if not globs:
        return []  # ecosystem not supported yet -> caller escalates
    found: list[Path] = []
    for pattern in globs:
        found.extend(p for p in repo_dir.glob(pattern) if p.is_file())
    # de-dupe while preserving order
    seen, unique = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _declared_in_requirements(path: Path, package: str) -> str | None:
    target = normalize(package)
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _REQ_RE.match(line)
        if m and normalize(m.group("name")) == target:
            return m.group("ver")
    return None


def _declared_in_pyproject(path: Path, package: str) -> str | None:
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:  # pragma: no cover
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    deps: list[str] = []
    deps += data.get("project", {}).get("dependencies", [])
    target = normalize(package)
    for spec in deps:
        m = _REQ_RE.match(spec)
        if m and normalize(m.group("name")) == target:
            return m.group("ver")
    return None


def locate(repo_dir: Path, ecosystem: Ecosystem, package: str) -> tuple[Path, str] | None:
    """Return (manifest_path, declared_version) for `package`, or None."""
    for manifest in find_manifests(repo_dir, ecosystem):
        if manifest.name == "pyproject.toml":
            ver = _declared_in_pyproject(manifest, package)
        else:
            ver = _declared_in_requirements(manifest, package)
        if ver is not None:
            return manifest, ver
    return None


def excerpt(path: Path, package: str, context: int = 2) -> str:
    """A few lines around the dependency, for the LLM's context."""
    target = normalize(package)
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        m = _REQ_RE.match(line)
        if m and normalize(m.group("name")) == target:
            lo, hi = max(0, i - context), min(len(lines), i + context + 1)
            return "\n".join(lines[lo:hi])
    return ""


def set_version(path: Path, package: str, new_version: str) -> ManifestEdit:
    """Pin `package` to `new_version`, preserving the line's style. Reversible."""
    original = path.read_text(encoding="utf-8")
    target = normalize(package)
    old_version = "?"
    out_lines = []
    changed = False

    for line in original.splitlines():
        m = _REQ_RE.match(line)
        if m and normalize(m.group("name")) == target and not changed:
            old_version = m.group("ver")
            rebuilt = (
                f"{m.group('pre')}{m.group('name')}{m.group('extras') or ''}"
                f"{m.group('sp1')}=={m.group('sp2')}{new_version}{m.group('rest')}"
            )
            out_lines.append(rebuilt)
            changed = True
        else:
            out_lines.append(line)

    if not changed:
        raise ValueError(f"{package!r} not found in {path}")

    trailing = "\n" if original.endswith("\n") else ""
    path.write_text("\n".join(out_lines) + trailing, encoding="utf-8")
    return ManifestEdit(path, package, old_version, new_version, original)
