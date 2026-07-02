"""Pull-request creation.

Two modes:

  * local  (default) - no GitHub account needed. Creates a real git branch +
    commit in the working repo and writes the PR body to an artifacts folder.
    This makes the POC fully runnable offline and is what CI tests use.

  * github - pushes the branch and opens a real PR via the GitHub REST API
    (needs GITHUB_TOKEN + a repo slug). Implemented with `requests`; clearly
    isolated so the local path never depends on it.

The agent's authority ends here: it can open (and draft) PRs, never merge.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..models import Finding


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _safe_branch(finding: Finding) -> str:
    pkg = finding.package.replace("/", "-")
    ver = (finding.recommended_version or "fix").replace("/", "-")
    return f"autofix/{pkg}-{ver}"


def render_pr_body(
    finding: Finding,
    from_version: str,
    to_version: str,
    check_detail: str,
    description: str,
    draft: bool,
    labels: list[str],
) -> str:
    cves = ", ".join(finding.cve_ids) if finding.cve_ids else "n/a"
    header = "DRAFT - needs human review" if draft else "Ready for review"
    return (
        f"# [{header}] Bump {finding.package}: {from_version} -> {to_version}\n\n"
        f"> Automated remediation for DotConnector finding `{finding.finding_id}`.\n"
        f"> Labels: {', '.join(labels) if labels else 'none'}\n\n"
        f"## Why\n{description}\n\n"
        f"## Details\n"
        f"- Package: `{finding.package}` ({finding.ecosystem.value})\n"
        f"- Severity: {finding.severity.value}\n"
        f"- CVEs: {cves}\n"
        f"- Declared: `{from_version}` -> Recommended: `{to_version}`\n"
        f"- Repository: {finding.repository}\n\n"
        f"## Validation\n{check_detail}\n\n"
        f"---\n"
        f"*Opened by the DotConnector autofix agent. A human must review and "
        f"merge; the agent never merges.*\n"
    )


class PRManager:
    def __init__(self, mode: str, artifacts_dir: Path, repo_slug: str | None = None):
        self.mode = mode
        self.artifacts_dir = artifacts_dir
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.repo_slug = repo_slug

    def open_pr(
        self,
        repo_dir: Path,
        base_branch: str,
        finding: Finding,
        from_version: str,
        to_version: str,
        body: str,
        commit_msg: str,
        draft: bool = False,
    ) -> dict:
        branch = _safe_branch(finding)
        if self.mode == "github":
            return self._open_github(repo_dir, base_branch, branch, commit_msg, body, draft)
        return self._open_local(repo_dir, base_branch, branch, finding, commit_msg, body)

    def write_comment(
        self,
        repo_dir: Path,
        finding: Finding,
        declared: str,
        attempted: list[str],
        last_check,
        audit: list[str],
    ) -> str:
        """No change was shipped (every candidate failed validation): the manifest
        is back on the declared version. Attach a human-readable comment with the
        attempts and the failing output so a person can decide. In github mode the
        same text would post as an issue/PR comment; for the POC we always write a
        local artifact so the trail is visible offline."""
        detail = getattr(last_check, "detail", "") or "no validation detail"
        output = (getattr(last_check, "test_output", "") or "").strip()
        tried = ", ".join(f"`{v}`" for v in attempted) if attempted else "none"
        log = "\n".join(f"- {line}" for line in audit)
        text = (
            f"# [ESCALATION - needs human] {finding.package}: bump failed validation\n\n"
            f"> Automated remediation for DotConnector finding "
            f"`{finding.finding_id}`.\n"
            f"> The agent tried the recommended version and the latest "
            f"registry/artifactory version; **all candidates failed validation**, "
            f"so the manifest was **left on the declared version "
            f"`{declared}`** (nothing broken, nothing shipped). A human must "
            f"decide how to proceed.\n\n"
            f"## Attempted versions (all failed)\n{tried}\n\n"
            f"## Last failure\n{detail}\n\n"
            + (f"```\n{output}\n```\n\n" if output else "")
            + f"## Decision log\n{log}\n\n"
            f"---\n*Opened by the DotConnector autofix agent. No code change was "
            f"applied; this is a flag for a human reviewer.*\n"
        )
        note_path = self.artifacts_dir / f"ESCALATION_{finding.finding_id}.md"
        note_path.write_text(text, encoding="utf-8")
        return str(note_path)

    # ---- local mode -------------------------------------------------------
    def _open_local(
        self,
        repo_dir: Path,
        base_branch: str,
        branch: str,
        finding: Finding,
        commit_msg: str,
        body: str,
    ) -> dict:
        # carry the working-tree edit onto a fresh branch, commit, return to base
        _git(repo_dir, "checkout", "-B", branch, base_branch)
        _git(repo_dir, "add", "-A")
        _git(repo_dir, "-c", "user.email=autofix@example.com",
             "-c", "user.name=dotconnector-autofix", "commit", "-m", commit_msg)
        diff = _git(repo_dir, "diff", f"{base_branch}..{branch}", "--", ".")
        _git(repo_dir, "checkout", base_branch)
        _git(repo_dir, "checkout", "--", ".")  # ensure clean tree for next finding

        pr_path = self.artifacts_dir / f"PR_{finding.finding_id}.md"
        pr_path.write_text(body + "\n## Diff\n```diff\n" + diff + "\n```\n", encoding="utf-8")
        return {"branch": branch, "pr_path": str(pr_path), "pr_url": None}

    # ---- github mode ------------------------------------------------------
    def _open_github(
        self,
        repo_dir: Path,
        base_branch: str,
        branch: str,
        commit_msg: str,
        body: str,
        draft: bool,
    ) -> dict:  # pragma: no cover - needs network + token
        import requests  # local import so local mode has no hard dep

        token = os.environ.get("GITHUB_TOKEN")
        if not token or not self.repo_slug:
            raise RuntimeError("github mode needs GITHUB_TOKEN and repo_slug")

        _git(repo_dir, "checkout", "-B", branch, base_branch)
        _git(repo_dir, "add", "-A")
        _git(repo_dir, "-c", "user.email=autofix@example.com",
             "-c", "user.name=dotconnector-autofix", "commit", "-m", commit_msg)
        _git(repo_dir, "push", "-u", "origin", branch, "--force")
        _git(repo_dir, "checkout", base_branch)

        resp = requests.post(
            f"https://api.github.com/repos/{self.repo_slug}/pulls",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            json={"title": commit_msg, "head": branch, "base": base_branch,
                  "body": body, "draft": draft},
            timeout=30,
        )
        resp.raise_for_status()
        url = resp.json().get("html_url")
        return {"branch": branch, "pr_path": None, "pr_url": url}
