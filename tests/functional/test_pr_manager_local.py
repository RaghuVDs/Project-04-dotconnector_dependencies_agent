"""Functional: PRManager local mode against a REAL git repo in a tmp dir."""
import pytest

from src.tools import manifest as m
from src.tools.github_pr import PRManager

pytestmark = pytest.mark.functional

from helpers import git_branches, git_init, git_porcelain, make_finding  # noqa: E402


def test_open_local_creates_branch_commit_and_pr_body(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    git_init(repo, "main")

    # the agent would have edited the working tree before calling open_pr
    m.set_version(repo / "requirements.txt", "flask", "3.1.3")

    pr = PRManager(mode="local", artifacts_dir=tmp_path / "prs")
    info = pr.open_pr(repo, "main", make_finding(finding_id="F-1"), "3.0.0", "3.1.3",
                      body="# PR body\n", commit_msg="fix(deps): bump flask")

    assert info["branch"] == "autofix/flask-3.1.3"
    assert info["pr_url"] is None
    # branch exists, base is checked back out, tree is clean for the next finding
    assert "autofix/flask-3.1.3" in git_branches(repo)
    assert git_porcelain(repo) == ""
    assert (repo / "requirements.txt").read_text().strip() == "flask==3.0.0"  # back on base

    # the PR body file carries the rendered body + the real diff
    pr_text = (tmp_path / "prs" / "PR_F-1.md").read_text(encoding="utf-8")
    assert "# PR body" in pr_text
    assert "## Diff" in pr_text
    assert "+flask==3.1.3" in pr_text and "-flask==3.0.0" in pr_text
