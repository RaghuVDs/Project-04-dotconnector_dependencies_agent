"""Functional: the agent self-healing loop (remediate) across every branch.

The registry, LLM and PR layer are faked; validate() is monkeypatched so we can
script pass/fail per candidate version. git is never touched (FakePR)."""
import pytest

from src import agent
from src.models import Outcome
from src.policy import Action, PolicyConfig, PolicyDecision, ValidationConfig
from src.tools.validator import VenvCreateError

pytestmark = pytest.mark.functional

from helpers import (  # noqa: E402
    FakeLLM, FakePR, FakeRegistry, make_finding, validator_passing_on, write_repo,
)

VCFG = ValidationConfig()
LADDER = ["3.0.0", "3.1.1", "3.1.2", "3.1.3"]


def _patch(monkeypatch, fake):
    monkeypatch.setattr("src.tools.validator.validate", fake)


def _run(tmp_path, finding=None, registry=None, llm=None, cfg=None, pr=None,
         repo_content="flask==3.0.0\n"):
    repo = write_repo(tmp_path, repo_content)
    pr = pr or FakePR(tmp_path / "prs")
    return agent.remediate(
        finding or make_finding(),
        repo, "main",
        registry or FakeRegistry(LADDER),
        llm or FakeLLM(),
        pr,
        cfg or PolicyConfig(),
        VCFG,
    ), pr, repo


# ---- triage exits -----------------------------------------------------------
def test_waived_skips(tmp_path):
    llm = FakeLLM()
    res, _, _ = _run(tmp_path, finding=make_finding(waived=True), llm=llm)
    assert res.outcome == Outcome.SKIPPED and res.reason == "waived"
    assert llm.budget_resets == 1            # budget reset is per-finding


def test_manifest_not_found_escalates(tmp_path):
    res, _, _ = _run(tmp_path, repo_content="django==1.0\n")
    assert res.outcome == Outcome.ESCALATED and "manifest declaring" in res.reason


def test_no_recommendation_escalates(tmp_path):
    res, _, _ = _run(tmp_path, finding=make_finding(recommended_version=None))
    assert res.outcome == Outcome.ESCALATED and "no recommended version" in res.reason


def test_recommended_missing_from_registry_escalates(tmp_path):
    res, _, _ = _run(tmp_path, registry=FakeRegistry(["3.0.0"]))  # 3.1.3 absent
    assert res.outcome == Outcome.ESCALATED and "not found in registry" in res.reason


def test_policy_downgrade_escalates(tmp_path):
    f = make_finding(recommended_version="2.0.0")
    res, _, _ = _run(tmp_path, finding=f, registry=FakeRegistry(["2.0.0", "3.0.0"]))
    assert res.outcome == Outcome.ESCALATED


def test_policy_skip_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "decide",
                        lambda f, d, c: PolicyDecision(Action.SKIP, "forced skip"))
    res, _, _ = _run(tmp_path)
    assert res.outcome == Outcome.SKIPPED and res.reason == "forced skip"


def test_not_auto_fixable_escalates(tmp_path):
    res, _, _ = _run(tmp_path, llm=FakeLLM(auto_fixable=False))
    assert res.outcome == Outcome.ESCALATED and "not auto-fixable" in res.reason


def test_llm_failure_escalates_without_crashing(tmp_path):
    # a dead API key / rate limit / network blip must escalate THIS finding and
    # let the run continue -- never propagate out of remediate().
    class _BoomLLM(FakeLLM):
        def plan_fix(self, *a, **k):
            raise RuntimeError("Error code: 401 - Invalid API Key")

    res, pr, _ = _run(tmp_path, llm=_BoomLLM())
    assert res.outcome == Outcome.ESCALATED
    assert "LLM judgment unavailable" in res.reason
    assert pr.opened == [] and pr.comments == []


# ---- the loop ---------------------------------------------------------------
def test_hallucinated_target_falls_back_to_recommended(tmp_path, monkeypatch):
    _patch(monkeypatch, validator_passing_on({"3.1.3"}))
    # LLM proposes a version that does not exist -> guard falls back to recommended
    res, pr, _ = _run(tmp_path, llm=FakeLLM(target="9.9.9"))
    assert res.outcome == Outcome.FIXED and res.to_version == "3.1.3"
    assert any("falling back to recommended" in line for line in res.audit)


def test_recommended_passes_first_normal_pr(tmp_path, monkeypatch):
    _patch(monkeypatch, validator_passing_on({"3.1.3"}))
    res, pr, _ = _run(tmp_path)
    assert res.outcome == Outcome.FIXED and res.attempts == 1
    assert len(pr.opened) == 1 and pr.opened[0]["draft"] is False
    assert pr.comments == []


def test_step_down_passes_opens_draft(tmp_path, monkeypatch):
    _patch(monkeypatch, validator_passing_on({"3.1.2"}))
    res, pr, _ = _run(tmp_path)
    assert res.outcome == Outcome.ESCALATED and res.to_version == "3.1.2"
    assert res.attempts == 2 and pr.opened[0]["draft"] is True
    assert "below recommended" in res.reason


def test_step_down_auto_ships_when_configured(tmp_path, monkeypatch):
    _patch(monkeypatch, validator_passing_on({"3.1.2"}))
    res, pr, _ = _run(tmp_path, cfg=PolicyConfig(draft_below_recommended=False))
    assert res.outcome == Outcome.FIXED and pr.opened[0]["draft"] is False


def test_all_candidates_fail_restores_and_comments(tmp_path, monkeypatch):
    _patch(monkeypatch, validator_passing_on(set()))
    res, pr, repo = _run(tmp_path)
    assert res.outcome == Outcome.ESCALATED and res.attempts == 3
    assert pr.opened == []
    assert pr.comments[0]["attempted"] == ["3.1.3", "3.1.2", "3.1.1"]
    assert "flask==3.0.0" in (repo / "requirements.txt").read_text()  # restored


def test_fallback_disabled_single_attempt(tmp_path, monkeypatch):
    _patch(monkeypatch, validator_passing_on(set()))
    res, pr, _ = _run(tmp_path, cfg=PolicyConfig(fallback_to_previous=False))
    assert res.attempts == 1 and pr.comments[0]["attempted"] == ["3.1.3"]


def test_yanked_step_down_candidate_is_skipped(tmp_path, monkeypatch):
    # 3.1.1 is listed but version_exists denies it (yanked) -> filtered out of the
    # candidate ladder; the loop only tries 3.1.3 then 3.1.2.
    _patch(monkeypatch, validator_passing_on(set()))
    registry = FakeRegistry(LADDER, exists_missing={"3.1.1"})
    res, pr, _ = _run(tmp_path, registry=registry)
    assert res.outcome == Outcome.ESCALATED
    assert pr.comments[0]["attempted"] == ["3.1.3", "3.1.2"]


def test_max_attempts_zero_yields_no_candidates(tmp_path, monkeypatch):
    # an edge config: max_attempts=0 -> empty ladder -> escalate with no attempt
    _patch(monkeypatch, validator_passing_on({"3.1.3"}))
    res, pr, _ = _run(tmp_path, cfg=PolicyConfig(max_attempts=0))
    assert res.outcome == Outcome.ESCALATED
    assert res.attempts == 0
    assert pr.comments[0]["attempted"] == []
    assert "no valid candidate" in res.reason


def test_venv_create_error_is_infra_error(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise VenvCreateError("no venv")
    _patch(monkeypatch, boom)
    res, pr, repo = _run(tmp_path)
    assert res.outcome == Outcome.ERROR and "environment unavailable" in res.reason
    assert "flask==3.0.0" in (repo / "requirements.txt").read_text()  # tree clean


def test_registry_list_error_still_runs_recommended(tmp_path, monkeypatch):
    _patch(monkeypatch, validator_passing_on({"3.1.3"}))
    # list_versions raises -> versions=[] -> no step-down ladder, recommended only
    res, pr, _ = _run(tmp_path, registry=FakeRegistry(["3.1.3"], raise_list=True))
    assert res.outcome == Outcome.FIXED and res.attempts == 1


# ---- major handling ---------------------------------------------------------
def _pytest_finding():
    return make_finding(package="pytest", declared_version="8.2.0",
                        recommended_version="9.0.3", purl="pkg:pypi/pytest@8.2.0")


def test_major_policy_draft(tmp_path, monkeypatch):
    _patch(monkeypatch, validator_passing_on({"9.0.3"}))
    res, pr, _ = _run(tmp_path, finding=_pytest_finding(),
                      registry=FakeRegistry(["8.2.0", "9.0.0", "9.0.3"]),
                      repo_content="pytest==8.2.0\n")
    assert res.outcome == Outcome.ESCALATED and pr.opened[0]["draft"] is True
    assert "policy draft" in res.reason


def test_forced_major_on_attempt_one_opens_draft(tmp_path, monkeypatch):
    # policy says AUTO (draft=False), but the bump is major and allow_major_auto
    # is False -> the loop itself forces a draft.
    _patch(monkeypatch, validator_passing_on({"9.0.3"}))
    monkeypatch.setattr(agent, "decide",
                        lambda f, d, c: PolicyDecision(Action.AUTO, "forced auto",
                                                       ["automated"]))
    res, pr, _ = _run(tmp_path, finding=_pytest_finding(),
                      registry=FakeRegistry(["8.2.0", "9.0.0", "9.0.3"]),
                      repo_content="pytest==8.2.0\n")
    assert res.outcome == Outcome.ESCALATED and pr.opened[0]["draft"] is True
    assert "major bump" in res.reason
    assert "major-bump" in pr.opened[0]["body"]
