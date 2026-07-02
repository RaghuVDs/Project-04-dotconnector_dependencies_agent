"""The validation gate -- i.e. *what counts as a passing check before we open a PR*.

A change is only allowed to become a PR if ALL required checks pass:

  1. manifest_parses     - the edited file is still syntactically valid.
  2. version_resolvable  - the dependency set RESOLVES (not just "each pin
                           exists"). A fast registry-existence pre-filter runs
                           first; then, depending on config, the real resolver:
                             * isolation=venv -> a real `pip install -r` into a
                               throwaway venv IS the resolver (a graph conflict
                               makes the install fail).
                             * isolation=none + resolver=pip_dry_run -> a
                               standalone `pip install --dry-run` in the agent env.
                             * resolver=registry -> only the existence proxy
                               (fast, fully offline).
  3. tests_pass          - the repo's test suite passes after the bump, run in
                           the venv interpreter that has the bumped deps
                           installed (when isolation=venv) so the new version is
                           actually exercised.

Optional / policy checks layered on top by policy.py and the PR gate:
  4. no_severity_regression - re-scan shows the new version isn't itself flagged
                              at >= the original severity. (documented; wire to
                              DotConnector in prod.)
  5. lockfile_consistent    - any lockfile is regenerated and matches.

If a required check fails, the agent does NOT open a normal PR. It either
retries (loop) or escalates as a draft for a human. The resolver-conflict and
test output are routed into CheckResult.detail / .test_output so the LLM's
diagnose step can propose the next version.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..models import CheckResult, Ecosystem
from .manifest import _REQ_RE
from .registry import Registry

# NOTE: `ValidationConfig`, `Isolation`, `Resolver` live in ..policy, which
# imports .tools.semver_tool -- importing them at module load would create a
# cycle (tools/__init__ eagerly imports this module). Annotations are lazy
# strings (`from __future__ import annotations`), and the only runtime use is
# inside validate(), so we import the enums locally there.


def _check_manifest_parses(manifest_path: Path) -> tuple[bool, str]:
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read manifest: {exc}"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        if not _REQ_RE.match(line) and "==" in line:
            return False, f"unparseable requirement line: {line!r}"
    return True, "manifest parses"


def _check_registry_existence(
    manifest_path: Path, ecosystem: Ecosystem, registry: Registry
) -> tuple[bool, str]:
    """Fast pre-filter: every '==' pin must exist in the registry.

    Cheap and offline-friendly; it does NOT resolve the graph (the real
    resolver below does that). Catches obviously-bogus pins before we spend a
    pip resolve on them.
    """
    text = manifest_path.read_text(encoding="utf-8")
    missing: list[str] = []
    for line in text.splitlines():
        m = _REQ_RE.match(line)
        if not m or m.group("op") != "==":
            continue
        name, ver = m.group("name"), m.group("ver")
        if "*" in ver:
            continue
        if not registry.version_exists(ecosystem, name, ver):
            missing.append(f"{name}=={ver}")
    if missing:
        return False, "versions not found in registry: " + ", ".join(missing)
    return True, "all pinned versions resolvable"


def _check_pip_resolves(
    manifest_path: Path, python_exe: str | None, timeout: int
) -> tuple[bool, str, str]:
    """Real resolver: `pip install --dry-run -r <manifest>` must resolve.

    Detects graph conflicts the existence proxy can't (e.g. a bump that requires
    a newer transitive dep than another pin allows). Used when isolation=none;
    in venv mode the real install is the authority instead.
    """
    cmd = [python_exe or sys.executable, "-m", "pip", "install",
           "--dry-run", "--no-input", "-r", str(manifest_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"resolver timed out after {timeout}s", ""
    except FileNotFoundError as exc:
        return False, f"pip not available: {exc}", ""
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, "dependency graph does not resolve: " + _tail(output), output
    return True, "dependency graph resolves (pip --dry-run)", output


def _check_tests(
    repo_dir: Path, test_command: list[str], timeout: int, python_exe: str | None = None
) -> tuple[bool, str, str]:
    if not test_command:
        return True, "no test command configured (skipped)", ""
    interpreter = python_exe or sys.executable
    cmd = [interpreter if part == "{python}" else part for part in test_command]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"tests timed out after {timeout}s", ""
    except FileNotFoundError as exc:
        return False, f"test command not found: {exc}", ""
    output = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0
    return ok, ("tests passed" if ok else f"tests failed (exit {proc.returncode})"), output


def _tail(text: str, n: int = 1500) -> str:
    return text[-n:] if len(text) > n else text


def _interpreter_path(root: Path, windows: bool) -> str:
    """venv interpreter location -- Scripts/ on Windows, bin/ elsewhere."""
    if windows:
        return str(root / "Scripts" / "python.exe")
    return str(root / "bin" / "python")


# --------------------------------------------------------------------------- #
# Isolated environment for honest resolve + test execution.
# --------------------------------------------------------------------------- #
class VenvCreateError(RuntimeError):
    """The venv could not be created -- infrastructure failure, not a bad bump."""


@dataclass
class VenvSession:
    """A reusable throwaway venv. Lives OUTSIDE the target repo tree (so it is
    never swept into `git add -A` when a PR is opened)."""

    root: Path
    reuse: bool = True

    @property
    def python(self) -> str:
        return _interpreter_path(self.root, os.name == "nt")

    def ensure_created(self, timeout: int = 120) -> None:
        if self.reuse and Path(self.python).exists():
            return
        cmd = [sys.executable, "-m", "venv", str(self.root)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise VenvCreateError(f"could not create venv: {exc}") from exc
        if proc.returncode != 0:
            raise VenvCreateError(
                "could not create venv: " + _tail((proc.stdout or "") + (proc.stderr or ""))
            )

    def install(self, manifest_path: Path, timeout: int) -> tuple[bool, str]:
        """Install the (edited) requirements into the venv.

        This IS the resolver in venv mode: pip refuses to install a set that
        does not resolve, so a graph conflict surfaces here as a failure.
        """
        cmd = [self.python, "-m", "pip", "install", "--no-input",
               "-r", str(manifest_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"install timed out after {timeout}s"
        except FileNotFoundError as exc:
            return False, f"pip not available in venv: {exc}"
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output


def make_venv_session(root: Path, reuse: bool = True) -> VenvSession:
    return VenvSession(root=Path(root), reuse=reuse)


def validate(
    repo_dir: Path,
    manifest_path: Path,
    ecosystem: Ecosystem,
    registry: Registry,
    cfg: ValidationConfig,
    venv: VenvSession | None = None,
) -> CheckResult:
    from ..policy import Isolation, Resolver  # local: avoids import cycle

    checks: dict[str, bool] = {}
    details: list[str] = []
    test_output = ""

    # 1. manifest still parses --------------------------------------------------
    ok, msg = _check_manifest_parses(manifest_path)
    checks["manifest_parses"] = ok
    details.append(msg)
    if not ok:
        return CheckResult(passed=False, checks=checks, detail="; ".join(details))

    # 2. version_resolvable -----------------------------------------------------
    #    a. fast registry-existence pre-filter
    ok, msg = _check_registry_existence(manifest_path, ecosystem, registry)
    details.append(msg)
    if not ok:
        checks["version_resolvable"] = False
        return CheckResult(passed=False, checks=checks, detail="; ".join(details))

    #    b. real resolution
    python_exe: str | None = None
    if cfg.isolation == Isolation.VENV and venv is not None:
        # a real install IS the resolver, and prepares the test env in one step
        installed, out = venv.install(manifest_path, cfg.resolve_timeout)
        test_output = out
        checks["version_resolvable"] = installed
        details.append("bumped requirements installed in venv" if installed
                       else "venv install failed: " + _tail(out))
        if not installed:
            return CheckResult(passed=False, checks=checks,
                               detail="; ".join(details), test_output=_tail(out))
        python_exe = venv.python
    elif cfg.resolver == Resolver.PIP_DRY_RUN:
        ok, msg, out = _check_pip_resolves(manifest_path, None, cfg.resolve_timeout)
        test_output = out
        checks["version_resolvable"] = ok
        details.append(msg)
        if not ok:
            return CheckResult(passed=False, checks=checks,
                               detail="; ".join(details), test_output=_tail(out))
    else:  # resolver == REGISTRY, isolation == none -> existence proxy only
        checks["version_resolvable"] = True

    # 3. tests pass -------------------------------------------------------------
    ok, msg, out = _check_tests(
        repo_dir, list(cfg.test_command), cfg.test_timeout, python_exe
    )
    checks["tests_pass"] = ok
    details.append(msg)
    if out:
        test_output = out

    passed = all(checks.values())
    return CheckResult(
        passed=passed, checks=checks, detail="; ".join(details),
        test_output=_tail(test_output),
    )
