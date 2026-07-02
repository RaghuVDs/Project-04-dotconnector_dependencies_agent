"""Integration: run_poc.main() end-to-end in an ISOLATED root.

We repoint run_poc.ROOT at a tmp dir (so the real ./.run is never touched),
stub the PyPI network call, and force the offline stub LLM. Everything else --
triage/dedup/blast-radius, the agent loop, the real validator (real pytest),
the local git PR layer, and report writing -- runs for real.
"""
import json
import shutil
from pathlib import Path

import pytest
import yaml

import run_poc
from src.tools import registry as reg
from src.tools.validator import VenvCreateError

pytestmark = pytest.mark.integration

from helpers import fake_pypi_payload, git_init  # noqa: E402


class _FakeVenvSession:
    """Stand-in venv so main()'s isolation=venv branch runs without real pip."""

    def __init__(self, raise_create=False):
        self.raise_create = raise_create
        self.python = "/no/such/python"   # makes the test subprocess fail fast

    def ensure_created(self, timeout):
        if self.raise_create:
            raise VenvCreateError("cannot create venv")

    def install(self, manifest_path, timeout):
        return True, "installed (fake)"

ORIG_ROOT = Path(run_poc.__file__).resolve().parent
SAMPLE_REPO = ORIG_ROOT / "sample_repo"

OFFLINE_CONFIG = {
    "base_branch": "main",
    "policy": {"auto_deltas": ["patch", "minor"], "max_attempts": 3,
               "max_prs_per_run": 10},
    "validation": {"resolver": "registry", "isolation": "none",
                   "test_command": ["{python}", "-m", "pytest", "-q"]},
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    reg._pypi_payload.cache_clear()
    monkeypatch.setattr(reg, "_pypi_payload", fake_pypi_payload)
    monkeypatch.setattr(run_poc, "ROOT", tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "stub")          # force deterministic stub
    yield


def _write_config(tmp_path, **policy_overrides):
    cfg = dict(OFFLINE_CONFIG)
    cfg["policy"] = {**OFFLINE_CONFIG["policy"], **policy_overrides}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _report(tmp_path):
    data = json.loads((tmp_path / ".run" / "report.json").read_text(encoding="utf-8"))
    return {d["finding_id"]: d["outcome"] for d in data}


def test_full_run_workspace_mode(monkeypatch, tmp_path):
    sample = tmp_path / "sample_repo"
    shutil.copytree(SAMPLE_REPO, sample)
    cfg = _write_config(tmp_path)
    monkeypatch.setattr("sys.argv",
                        ["run_poc.py", "--repo", str(sample), "--config", str(cfg),
                         "--mode", "local"])

    rc = run_poc.main()
    assert rc == 0

    outcomes = _report(tmp_path)
    assert outcomes.get("F-0001") == "skipped"        # waived
    assert "F-0002" not in outcomes                   # duplicate of F-0001 -> deduped
    assert outcomes.get("F-0004") == "escalated"      # numpy, no recommendation
    assert outcomes.get("F-0005") == "fixed"          # flask minor -> PR
    assert outcomes.get("F-0006") == "escalated"      # pytest major -> draft
    assert outcomes.get("F-0010") == "fixed"          # werkzeug minor -> PR

    prs = tmp_path / ".run" / "prs"
    assert (prs / "PR_F-0005.md").exists()
    # real branches exist in the workspace
    ws = tmp_path / ".run" / "workspace"
    branches = (ws / ".git").exists()
    assert branches


def test_blast_radius_cap_and_in_place(monkeypatch, tmp_path):
    sample = tmp_path / "sample_repo"
    shutil.copytree(SAMPLE_REPO, sample)
    git_init(sample, "main")                          # --in-place needs a checkout
    cfg = _write_config(tmp_path, max_prs_per_run=1)
    # pre-create the artifacts dir to exercise the "rmtree existing" branch
    (tmp_path / ".run" / "prs").mkdir(parents=True)
    monkeypatch.setattr("sys.argv",
                        ["run_poc.py", "--repo", str(sample), "--config", str(cfg),
                         "--mode", "local", "--in-place"])

    rc = run_poc.main()
    assert rc == 0

    outcomes = _report(tmp_path)
    # first fixable finding opens the one allowed PR...
    assert outcomes.get("F-0005") == "fixed"
    # ...after which the blast-radius cap halts processing (no result recorded)
    assert "F-0008" not in outcomes
    assert "F-0010" not in outcomes


def _venv_config(tmp_path):
    cfg = dict(OFFLINE_CONFIG)
    cfg["validation"] = {**OFFLINE_CONFIG["validation"], "isolation": "venv"}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_main_venv_isolation_branch(monkeypatch, tmp_path):
    sample = tmp_path / "sample_repo"
    shutil.copytree(SAMPLE_REPO, sample)
    cfg = _venv_config(tmp_path)
    monkeypatch.setattr(run_poc, "make_venv_session",
                        lambda root, reuse: _FakeVenvSession(raise_create=False))
    monkeypatch.setattr("sys.argv",
                        ["run_poc.py", "--repo", str(sample), "--config", str(cfg),
                         "--mode", "local"])
    assert run_poc.main() == 0          # the venv path is exercised end-to-end


def test_main_venv_creation_failure_falls_back(monkeypatch, tmp_path):
    sample = tmp_path / "sample_repo"
    shutil.copytree(SAMPLE_REPO, sample)
    cfg = _venv_config(tmp_path)
    monkeypatch.setattr(run_poc, "make_venv_session",
                        lambda root, reuse: _FakeVenvSession(raise_create=True))
    monkeypatch.setattr("sys.argv",
                        ["run_poc.py", "--repo", str(sample), "--config", str(cfg),
                         "--mode", "local"])
    # venv creation fails -> warns and falls back to in-env tests; run still completes
    assert run_poc.main() == 0
