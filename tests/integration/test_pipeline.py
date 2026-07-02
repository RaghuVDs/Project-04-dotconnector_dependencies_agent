"""Integration: the real agent + real tools (manifest, validator, registry
facade, local git PR) wired together on a copy of the sample repo. Only the
PyPI network call is stubbed; everything else is the genuine code path."""
import shutil
from pathlib import Path

import pytest

from src.agent import remediate
from src.llm import StubLLM
from src.models import Ecosystem, Outcome, Severity
from src.policy import Isolation, PolicyConfig, Resolver, ValidationConfig
from src.tools import registry as reg
from src.tools.github_pr import PRManager
from src.tools.registry import Registry

pytestmark = pytest.mark.integration

from helpers import (  # noqa: E402
    fake_pypi_payload, git_init, git_porcelain, make_finding,
)

SAMPLE_REPO = Path(__file__).resolve().parents[2] / "sample_repo"
# fully offline + no pip subprocess, but real manifest edit + real pytest run
VCFG = ValidationConfig(resolver=Resolver.REGISTRY, isolation=Isolation.NONE)


@pytest.fixture()
def repo(tmp_path):
    dest = tmp_path / "repo"
    shutil.copytree(SAMPLE_REPO, dest)
    git_init(dest, "main")
    return dest


@pytest.fixture(autouse=True)
def _offline_registry(monkeypatch):
    reg._pypi_payload.cache_clear()
    monkeypatch.setattr(reg, "_pypi_payload", fake_pypi_payload)
    yield


def _remediate(repo, finding, tmp_path, cfg=None):
    registry = Registry()
    pr = PRManager(mode="local", artifacts_dir=tmp_path / "prs")
    return remediate(finding, repo, "main", registry, StubLLM(registry), pr,
                     cfg or PolicyConfig(), VCFG)


def test_minor_bump_produces_pr_and_clean_tree(repo, tmp_path):
    finding = make_finding(finding_id="F-0005", package="flask",
                           declared_version="3.0.0", recommended_version="3.1.3",
                           cve_ids=["CVE-2025-FLASK-11"])
    res = _remediate(repo, finding, tmp_path)

    assert res.outcome == Outcome.FIXED
    assert res.to_version == "3.1.3"
    assert res.branch == "autofix/flask-3.1.3"
    # the loop ran the REAL validator (registry existence + real pytest)
    assert git_porcelain(repo) == ""                      # tree clean afterwards
    assert "flask==3.0.0" in (repo / "requirements.txt").read_text()  # back on base
    pr_body = (tmp_path / "prs" / "PR_F-0005.md").read_text(encoding="utf-8")
    assert "+flask==3.1.3" in pr_body


def test_waived_is_skipped(repo, tmp_path):
    finding = make_finding(finding_id="F-0001", package="pandas",
                           declared_version="2.0.2", recommended_version=None,
                           waived=True)
    res = _remediate(repo, finding, tmp_path)
    assert res.outcome == Outcome.SKIPPED


def test_no_recommendation_escalates(repo, tmp_path):
    finding = make_finding(finding_id="F-0004", package="numpy",
                           declared_version="1.26.2", recommended_version=None)
    res = _remediate(repo, finding, tmp_path)
    assert res.outcome == Outcome.ESCALATED


def test_unsupported_ecosystem_escalates(repo, tmp_path):
    finding = make_finding(finding_id="F-0003", ecosystem=Ecosystem.MAVEN,
                           package="spring-core", declared_version="6.2.18",
                           recommended_version="6.2.19", severity=Severity.HIGH)
    res = _remediate(repo, finding, tmp_path)
    assert res.outcome == Outcome.ESCALATED


def test_major_bump_opens_draft(repo, tmp_path):
    finding = make_finding(finding_id="F-0006", package="pytest",
                           declared_version="8.2.0", recommended_version="9.0.3")
    res = _remediate(repo, finding, tmp_path)
    assert res.outcome == Outcome.ESCALATED          # draft -> escalated
    assert res.to_version == "9.0.3"
    assert git_porcelain(repo) == ""
