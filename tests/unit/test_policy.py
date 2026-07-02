"""Unit: policy config parsing + the decide() decision table."""
import pytest

from src.models import Ecosystem, Finding, Severity
from src.policy import (
    Action, Isolation, PolicyConfig, Resolver, ValidationConfig, decide,
)
from src.tools.semver_tool import Delta

pytestmark = pytest.mark.unit


def _f(**kw):
    base = dict(finding_id="x", repository="r", ecosystem=Ecosystem.PYPI,
                package="p", declared_version="1.0.0", recommended_version="1.1.0",
                severity=Severity.HIGH)
    base.update(kw)
    return Finding(**base)


# ---- PolicyConfig.from_dict ------------------------------------------------
def test_policyconfig_defaults():
    c = PolicyConfig.from_dict({})
    assert c.auto_deltas == (Delta.PATCH, Delta.MINOR)
    assert c.allow_major_auto is False
    assert c.max_attempts == 3
    assert c.fallback_to_previous is True
    assert c.draft_below_recommended is True


def test_policyconfig_full_roundtrip():
    c = PolicyConfig.from_dict({
        "auto_deltas": ["patch"], "allow_major_auto": True, "max_attempts": 5,
        "max_prs_per_run": 1, "max_llm_calls_per_finding": 2,
        "fallback_to_previous": False, "draft_below_recommended": False,
    })
    assert c.auto_deltas == (Delta.PATCH,)
    assert c.allow_major_auto is True
    assert c.max_attempts == 5
    assert c.max_prs_per_run == 1
    assert c.max_llm_calls_per_finding == 2
    assert c.fallback_to_previous is False
    assert c.draft_below_recommended is False


def test_policyconfig_empty_auto_deltas_keeps_default():
    # falsy auto_deltas ([]) must not wipe the default tuple
    c = PolicyConfig.from_dict({"auto_deltas": []})
    assert c.auto_deltas == (Delta.PATCH, Delta.MINOR)


# ---- ValidationConfig.from_dict --------------------------------------------
def test_validationconfig_defaults():
    c = ValidationConfig.from_dict({})
    assert c.resolver == Resolver.PIP_DRY_RUN
    assert c.isolation == Isolation.VENV
    assert c.test_command[0] == "{python}"
    assert c.reuse_venv is True


def test_validationconfig_explicit():
    c = ValidationConfig.from_dict({
        "resolver": "registry", "isolation": "none",
        "test_command": ["pytest", "-x"], "resolve_timeout": 7,
        "test_timeout": 8, "reuse_venv": False,
    })
    assert c.resolver == Resolver.REGISTRY
    assert c.isolation == Isolation.NONE
    assert c.test_command == ("pytest", "-x")
    assert c.resolve_timeout == 7 and c.test_timeout == 8 and c.reuse_venv is False


def test_validationconfig_empty_test_command_keeps_default():
    c = ValidationConfig.from_dict({"test_command": []})
    assert c.test_command[0] == "{python}"


@pytest.mark.parametrize("bad", [{"resolver": "bogus"}, {"isolation": "bogus"}])
def test_validationconfig_bad_enum_fails_fast(bad):
    with pytest.raises(ValueError):
        ValidationConfig.from_dict(bad)


# ---- decide() decision table -----------------------------------------------
def test_decide_waived_skips():
    d = decide(_f(waived=True), Delta.MINOR, PolicyConfig())
    assert d.action == Action.SKIP


def test_decide_no_recommendation_escalates():
    d = decide(_f(recommended_version=None), Delta.MINOR, PolicyConfig())
    assert d.action == Action.ESCALATE
    assert "no-recommendation" in d.labels


@pytest.mark.parametrize("delta", [Delta.DOWNGRADE, Delta.SAME, Delta.UNKNOWN])
def test_decide_non_upgrade_escalates(delta):
    d = decide(_f(), delta, PolicyConfig())
    assert d.action == Action.ESCALATE
    assert "needs-human" in d.labels


def test_decide_major_without_allow_is_draft():
    d = decide(_f(), Delta.MAJOR, PolicyConfig(allow_major_auto=False))
    assert d.action == Action.DRAFT_HUMAN
    assert "major-bump" in d.labels


def test_decide_major_with_allow_is_auto():
    d = decide(_f(), Delta.MAJOR, PolicyConfig(allow_major_auto=True))
    assert d.action == Action.AUTO


@pytest.mark.parametrize("delta", [Delta.PATCH, Delta.MINOR])
def test_decide_patch_minor_auto(delta):
    d = decide(_f(), delta, PolicyConfig())
    assert d.action == Action.AUTO
    assert "automated" in d.labels


def test_decide_delta_outside_auto_policy_escalates():
    # minor bump but policy only auto-approves patch -> escalate
    d = decide(_f(), Delta.MINOR, PolicyConfig(auto_deltas=(Delta.PATCH,)))
    assert d.action == Action.ESCALATE
    assert "needs-human" in d.labels
