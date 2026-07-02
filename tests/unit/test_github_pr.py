"""Unit: PR body rendering, branch naming, escalation comment, github guardrail."""
import pytest

from src.models import CheckResult
from src.tools.github_pr import PRManager, _safe_branch, render_pr_body

pytestmark = pytest.mark.unit

from helpers import make_finding  # noqa: E402


def test_render_pr_body_ready_with_cves_and_labels():
    body = render_pr_body(make_finding(cve_ids=["CVE-1", "CVE-2"]), "3.0.0", "3.1.3",
                          "all good", "because security", draft=False,
                          labels=["automated", "dependencies"])
    assert "Ready for review" in body
    assert "CVE-1, CVE-2" in body
    assert "because security" in body
    assert "automated, dependencies" in body
    assert "3.0.0` -> Recommended: `3.1.3" in body


def test_render_pr_body_draft_without_cves_or_labels():
    body = render_pr_body(make_finding(cve_ids=[]), "3.0.0", "3.1.3",
                          "validated", "desc", draft=True, labels=[])
    assert "DRAFT - needs human review" in body
    assert "CVEs: n/a" in body
    assert "Labels: none" in body


@pytest.mark.parametrize("pkg,ver,expected", [
    ("flask", "3.1.3", "autofix/flask-3.1.3"),
    ("org/lib", "1.0", "autofix/org-lib-1.0"),
])
def test_safe_branch(pkg, ver, expected):
    assert _safe_branch(make_finding(package=pkg, recommended_version=ver)) == expected


def test_safe_branch_without_recommendation_uses_fix():
    assert _safe_branch(make_finding(recommended_version=None)) == "autofix/flask-fix"


def test_write_comment_includes_attempts_and_output(tmp_path):
    pr = PRManager(mode="local", artifacts_dir=tmp_path / "prs")
    check = CheckResult(passed=False, checks={"tests_pass": False},
                        detail="tests failed", test_output="TRACEBACK")
    path = pr.write_comment(tmp_path, make_finding(finding_id="F-9"), "3.0.0",
                            ["3.1.3", "3.1.2"], check, ["start", "attempt 1: failed"])
    text = open(path, encoding="utf-8").read()
    assert "ESCALATION" in text
    assert "3.0.0" in text and "`3.1.3`" in text and "`3.1.2`" in text
    assert "tests failed" in text and "TRACEBACK" in text
    assert "attempt 1: failed" in text
    assert path.endswith("ESCALATION_F-9.md")


def test_write_comment_handles_missing_last_check(tmp_path):
    pr = PRManager(mode="local", artifacts_dir=tmp_path / "prs")
    path = pr.write_comment(tmp_path, make_finding(), "3.0.0", [], None, [])
    text = open(path, encoding="utf-8").read()
    assert "no validation detail" in text


def test_prmanager_creates_artifacts_dir(tmp_path):
    target = tmp_path / "nested" / "prs"
    PRManager(mode="local", artifacts_dir=target)
    assert target.exists()


def test_github_mode_requires_token_and_slug(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    pr = PRManager(mode="github", artifacts_dir=tmp_path / "prs", repo_slug=None)
    with pytest.raises(RuntimeError, match="github mode needs"):
        pr.open_pr(tmp_path, "main", make_finding(), "3.0.0", "3.1.3",
                   "body", "msg", draft=False)
