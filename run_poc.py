#!/usr/bin/env python3
"""End-to-end POC runner.

    python run_poc.py                 # offline (stub LLM), default sample repo
    LLM_PROVIDER=groq GROQ_API_KEY=... python run_poc.py   # real free LLM

What it does:
  1. loads simulated DotConnector findings
  2. copies the target repo to a clean git workspace
  3. triages (dedup by fingerprint, blast-radius cap)
  4. runs the self-healing agent on each finding
  5. prints a summary and writes PR bodies + a JSON report under ./.run/
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mock_dotconnector.server import load_findings, load_payload  # noqa: E402
from src.agent import remediate  # noqa: E402
from src.llm import make_llm  # noqa: E402
from src.models import Outcome  # noqa: E402
from src.policy import Isolation, PolicyConfig, ValidationConfig  # noqa: E402
from src.tools.github_pr import PRManager  # noqa: E402
from src.tools.registry import Registry  # noqa: E402
from src.tools.validator import VenvCreateError, make_venv_session  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _on_rm_error(func, path, _exc_info) -> None:
    """rmtree onerror: git pack objects are read-only on Windows, which makes
    os.unlink raise PermissionError. Clear the read-only bit and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, onerror=_on_rm_error)


def prepare_workspace(source_repo: Path, workspace: Path, base_branch: str) -> None:
    if workspace.exists():
        _rmtree(workspace)
    shutil.copytree(source_repo, workspace)
    _git(workspace, "init", "-q", "-b", base_branch)
    _git(workspace, "add", "-A")
    _git(workspace, "-c", "user.email=seed@example.com",
         "-c", "user.name=seed", "commit", "-q", "-m", "initial import")


def main() -> int:
    ap = argparse.ArgumentParser(description="DotConnector autofix POC")
    ap.add_argument("--repo", default=str(ROOT / "sample_repo"),
                    help="path to the target repo to remediate")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--mode", choices=["local", "github"], default="local")
    ap.add_argument("--repo-slug", default=None, help="owner/name for github mode")
    ap.add_argument("--in-place", action="store_true",
                    help="operate on --repo directly (already a git checkout); "
                         "used in CI. Default copies to a clean workspace.")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ModuleNotFoundError:
        pass

    cfg_raw = yaml.safe_load(Path(args.config).read_text()) if Path(args.config).exists() else {}
    policy_cfg = PolicyConfig.from_dict(cfg_raw.get("policy", {}))
    validation_cfg = ValidationConfig.from_dict(cfg_raw.get("validation", {}))
    base_branch = cfg_raw.get("base_branch", "main")

    registry = Registry()
    llm, llm_label = make_llm(registry, max_calls=policy_cfg.max_llm_calls_per_finding)

    payload = load_payload()
    findings = load_findings()

    source_repo = Path(args.repo).resolve()
    run_dir = ROOT / ".run"
    artifacts = run_dir / "prs"
    if artifacts.exists():
        _rmtree(artifacts)

    # Throwaway venv for honest resolve + isolated tests. Lives OUTSIDE the
    # target repo tree (so it is never swept into `git add -A`). Clean at start
    # with ignore_errors -- never rmtree a venv whose interpreter may be in use.
    venv = None
    venv_dir = run_dir / "venv"
    shutil.rmtree(venv_dir, ignore_errors=True)
    if validation_cfg.isolation == Isolation.VENV:
        venv = make_venv_session(venv_dir, reuse=validation_cfg.reuse_venv)
        try:
            venv.ensure_created(validation_cfg.resolve_timeout)
        except VenvCreateError as exc:
            print(f"WARNING: could not create venv ({exc}); "
                  f"falling back to in-env tests (isolation=none).")
            venv = None

    if args.in_place:
        workspace = source_repo  # operate on the real checkout (CI / github mode)
    else:
        workspace = run_dir / "workspace"
        prepare_workspace(source_repo, workspace, base_branch)

    pr = PRManager(mode=args.mode, artifacts_dir=artifacts, repo_slug=args.repo_slug)

    print("=" * 74)
    print(f"DotConnector autofix POC  |  app {payload['application_id']} "
          f"({payload['application_name']})")
    print(f"LLM: {llm_label}")
    print(f"Target repo: {source_repo.name}  |  workspace: {workspace}")
    print(f"Policy: auto={', '.join(d.value for d in policy_cfg.auto_deltas)}  "
          f"max_attempts={policy_cfg.max_attempts}  "
          f"max_prs/run={policy_cfg.max_prs_per_run}")
    print(f"Validation: resolver={validation_cfg.resolver.value}  "
          f"isolation={'venv' if venv else 'none'}")
    print("=" * 74)

    results = []
    seen_fingerprints: set[str] = set()
    prs_opened = 0

    for finding in findings:
        # triage: idempotency / dedup
        if finding.fingerprint in seen_fingerprints:
            print(f"\n[{finding.finding_id}] {finding.package}: "
                  f"SKIPPED (duplicate of an earlier finding)")
            continue
        seen_fingerprints.add(finding.fingerprint)

        # triage: blast-radius cap
        if prs_opened >= policy_cfg.max_prs_per_run:
            print(f"\n[{finding.finding_id}] {finding.package}: "
                  f"ESCALATED (blast-radius cap of {policy_cfg.max_prs_per_run} reached)")
            continue

        res = remediate(finding, workspace, base_branch, registry, llm, pr,
                        policy_cfg, validation_cfg, venv)
        results.append(res)

        icon = {Outcome.FIXED: "PR  ", Outcome.ESCALATED: "HUMAN",
                Outcome.SKIPPED: "SKIP", Outcome.ERROR: "ERR "}[res.outcome]
        arrow = f"{res.from_version} -> {res.to_version}" if res.to_version else res.from_version
        print(f"\n[{finding.finding_id}] {icon} {res.package} {arrow}")
        print(f"        {res.reason}")
        if res.branch:
            print(f"        branch: {res.branch}")
        if res.pr_path:
            print(f"        PR body: {Path(res.pr_path).relative_to(ROOT)}")
        if res.pr_url:  # pragma: no cover - only set in github mode (live PR)
            print(f"        PR: {res.pr_url}")
        if res.outcome in (Outcome.FIXED, Outcome.ESCALATED) and res.branch:
            prs_opened += 1

    # summary
    counts: dict[str, int] = {}
    for r in results:
        counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
    print("\n" + "=" * 74)
    print("SUMMARY: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"PRs/branches opened: {prs_opened}")
    print("=" * 74)

    report = run_dir / "report.json"
    report.write_text(json.dumps([r.model_dump() for r in results], indent=2))
    print(f"\nFull report: {report.relative_to(ROOT)}")
    print(f"PR bodies:   {artifacts.relative_to(ROOT)}/")
    print(f"Git branches live in: {workspace}  (try: git -C {workspace} branch)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
