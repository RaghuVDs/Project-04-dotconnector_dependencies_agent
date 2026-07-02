"""Reusable test doubles and factories shared across the suite.

These are deliberately thin: just enough to drive the real code under test
without touching the network, a real LLM, pip, or (where avoidable) git.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from src.llm import Diagnosis, FixPlan
from src.models import CheckResult, Ecosystem, Finding, Severity
from src.tools.registry import RegistryError

# --------------------------------------------------------------------------- #
# Canned PyPI data -- mirrors the sample_repo pins + the findings' targets so
# the registry-existence prefilter passes in integration runs. Keyed by the
# PEP503-ish lowercase name.
# --------------------------------------------------------------------------- #
CANNED_RELEASES = {
    "flask": ["3.0.0", "3.1.0", "3.1.1", "3.1.2", "3.1.3"],
    "requests": ["2.31.0", "2.32.0", "2.33.0", "2.33.1"],
    "werkzeug": ["3.0.6", "3.1.0", "3.1.5", "3.1.6"],
    "pandas": ["2.0.2"],
    "numpy": ["1.26.2"],
    "pytest": ["8.2.0", "9.0.0", "9.0.3"],
    "pytest-runner": ["6.0.1"],
    "setuptools": ["82.0.1"],
}


def fake_pypi_payload(package: str) -> dict:
    """Drop-in for registry._pypi_payload: offline, deterministic."""
    key = package.lower()
    if key not in CANNED_RELEASES:
        raise RegistryError(f"unknown package {package!r}")
    return {"releases": {v: [{"filename": f"{package}-{v}.whl"}]
                         for v in CANNED_RELEASES[key]}}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRegistry:
    """Registry stand-in. `version_exists` checks membership in `versions`;
    `list_versions` returns them (or raises if `raise_list`)."""

    def __init__(self, versions, raise_list=False, exists_missing=None):
        self.versions = list(versions)
        self.raise_list = raise_list
        # versions that list_versions returns but version_exists denies -- models
        # a release yanked between the listing and the existence check.
        self.exists_missing = set(exists_missing or ())
        self.list_calls = 0

    def list_versions(self, ecosystem, package):
        self.list_calls += 1
        if self.raise_list:
            raise RegistryError("boom")
        return list(self.versions)

    def version_exists(self, ecosystem, package, version):
        return version in self.versions and version not in self.exists_missing


class FakeLLM:
    """LLM stand-in with configurable plan; `diagnose` asserts it is never used
    (the loop is deterministic now)."""

    def __init__(self, target=None, auto_fixable=True, pr_desc="why-text",
                 allow_diagnose=False):
        self.target = target
        self.auto_fixable = auto_fixable
        self.pr_desc = pr_desc
        self.allow_diagnose = allow_diagnose
        self.budget_resets = 0
        self.plan_calls = 0
        self.pr_calls = 0

    def reset_budget(self):
        self.budget_resets += 1

    def plan_fix(self, finding, delta, excerpt, versions):
        self.plan_calls += 1
        tgt = self.target if self.target is not None else (finding.recommended_version or "")
        return FixPlan(tgt, self.auto_fixable, "fake reasoning")

    def diagnose(self, *a, **k):
        if not self.allow_diagnose:
            raise AssertionError("diagnose must not be called in the deterministic loop")
        return Diagnosis(True, None, "give up")

    def write_pr_description(self, finding, from_v, to_v, detail):
        self.pr_calls += 1
        return self.pr_desc


class FakePR:
    """PR layer stand-in: records open_pr/write_comment without git."""

    def __init__(self, artifacts_dir):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.opened = []
        self.comments = []

    def open_pr(self, repo_dir, base_branch, finding, from_v, to_v, body,
                commit_msg, draft=False):
        self.opened.append({"to": to_v, "draft": draft, "body": body,
                            "commit_msg": commit_msg})
        return {"branch": f"autofix/{finding.package}-{to_v}",
                "pr_path": str(self.artifacts_dir / f"PR_{finding.finding_id}.md"),
                "pr_url": None}

    def write_comment(self, repo_dir, finding, declared, attempted, last_check, audit):
        self.comments.append({"declared": declared, "attempted": list(attempted)})
        p = self.artifacts_dir / f"ESCALATION_{finding.finding_id}.md"
        p.write_text("escalation", encoding="utf-8")
        return str(p)


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
def make_finding(**overrides) -> Finding:
    base = dict(
        finding_id="F-T", repository="r", ecosystem=Ecosystem.PYPI,
        package="flask", declared_version="3.0.0", recommended_version="3.1.3",
        severity=Severity.MEDIUM, file_path="requirements.txt",
        purl="pkg:pypi/flask@3.0.0",
    )
    base.update(overrides)
    return Finding(**base)


def write_repo(tmp_path, content="flask==3.0.0\n") -> Path:
    p = Path(tmp_path) / "requirements.txt"
    p.write_text(content, encoding="utf-8")
    return Path(tmp_path)


def validator_passing_on(pass_versions):
    """Return a `validate` replacement that passes only when the manifest pins a
    version in `pass_versions` (read straight off disk -- so restore() between
    attempts is exercised for real)."""
    wanted = set(pass_versions)

    def _fake(repo_dir, manifest_path, ecosystem, registry, cfg, venv=None):
        text = Path(manifest_path).read_text(encoding="utf-8")
        ok = any(f"=={v}" in text for v in wanted)
        return CheckResult(
            passed=ok,
            checks={"manifest_parses": True, "version_resolvable": True,
                    "tests_pass": ok},
            detail="ok" if ok else "tests failed",
            test_output="...output tail...",
        )
    return _fake


def git_init(repo: Path, branch="main") -> None:
    def g(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True,
                       text=True, check=True)
    g("init", "-q", "-b", branch)
    g("add", "-A")
    g("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")


def git_porcelain(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def git_branches(repo: Path) -> list[str]:
    out = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    return [b.strip() for b in out.splitlines() if b.strip()]
