"""Unit: the LLM layer -- stub, OpenAI-compatible client (faked), make_llm()."""
import types

import pytest

from src import llm as llm_mod
from src.llm import (
    OpenAICompatLLM, StubLLM, _parse_json, make_llm,
)
from src.models import CheckResult, Ecosystem, Severity
from src.tools.semver_tool import Delta

pytestmark = pytest.mark.unit

from helpers import make_finding  # noqa: E402


# --------------------------------------------------------------------------- #
# _parse_json
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('prefix {"a": 1} suffix', {"a": 1}),
    ('no json here', {}),
    ('{not valid json}', {}),
    ('', {}),
])
def test_parse_json(text, expected):
    assert _parse_json(text) == expected


# --------------------------------------------------------------------------- #
# StubLLM
# --------------------------------------------------------------------------- #
class _StubRegistry:
    def __init__(self, alt):
        self._alt = alt

    def latest_patch_within_minor(self, ecosystem, package, base):
        return self._alt


def test_stub_plan_fix_with_recommendation():
    s = StubLLM()
    plan = s.plan_fix(make_finding(), Delta.MINOR, "", [])
    assert plan.target_version == "3.1.3" and plan.auto_fixable is True


def test_stub_plan_fix_without_recommendation():
    s = StubLLM()
    plan = s.plan_fix(make_finding(recommended_version=None), Delta.UNKNOWN, "", [])
    assert plan.target_version == "" and plan.auto_fixable is False


def test_stub_diagnose_retries_with_alt_patch():
    s = StubLLM(registry=_StubRegistry(alt="3.1.5"))
    d = s.diagnose(make_finding(), "3.1.3", CheckResult(passed=False), [])
    assert d.give_up is False and d.next_version == "3.1.5"


def test_stub_diagnose_gives_up_when_alt_equals_attempted():
    s = StubLLM(registry=_StubRegistry(alt="3.1.3"))
    d = s.diagnose(make_finding(), "3.1.3", CheckResult(passed=False), [])
    assert d.give_up is True and d.next_version is None


def test_stub_diagnose_gives_up_without_registry():
    s = StubLLM(registry=None)
    d = s.diagnose(make_finding(), "3.1.3", CheckResult(passed=False), [])
    assert d.give_up is True


def test_stub_diagnose_gives_up_without_recommendation():
    s = StubLLM(registry=_StubRegistry(alt="3.1.5"))
    d = s.diagnose(make_finding(recommended_version=None), "3.1.3",
                   CheckResult(passed=False), [])
    assert d.give_up is True


def test_stub_write_pr_description_with_and_without_cves():
    s = StubLLM()
    with_cve = s.write_pr_description(make_finding(cve_ids=["CVE-1"]), "3.0.0", "3.1.3", "ok")
    assert "CVE-1" in with_cve
    without = s.write_pr_description(make_finding(cve_ids=[]), "3.0.0", "3.1.3", "ok")
    assert "advisory" in without


def test_stub_reset_budget():
    s = StubLLM()
    s.calls = 5
    s.reset_budget()
    assert s.calls == 0


# --------------------------------------------------------------------------- #
# OpenAICompatLLM with an injected fake client
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, content):
        self.choices = [types.SimpleNamespace(
            message=types.SimpleNamespace(content=content))]


class _FakeCompletions:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResp(self.behavior(kwargs))


def _llm_with(behavior, max_calls=6):
    llm = OpenAICompatLLM("http://x", "key", "model", max_calls=max_calls)
    llm.client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_FakeCompletions(behavior)))
    return llm


def test_compat_plan_fix_parses_json():
    llm = _llm_with(lambda k: '{"target_version":"3.1.3","auto_fixable":true,"reasoning":"r"}')
    plan = llm.plan_fix(make_finding(), Delta.MINOR, "excerpt", ["3.1.3"])
    assert plan.target_version == "3.1.3" and plan.auto_fixable is True and plan.reasoning == "r"


def test_compat_plan_fix_falls_back_to_recommended_when_blank():
    llm = _llm_with(lambda k: "{}")
    plan = llm.plan_fix(make_finding(), Delta.MINOR, "x", [])
    assert plan.target_version == "3.1.3"      # from finding.recommended_version
    assert plan.auto_fixable is False


def test_compat_response_format_unsupported_falls_back_to_plain():
    def behavior(kwargs):
        if "response_format" in kwargs:
            raise RuntimeError("provider rejects response_format")
        return '{"target_version":"3.1.2","auto_fixable":true,"reasoning":"ok"}'
    llm = _llm_with(behavior)
    plan = llm.plan_fix(make_finding(), Delta.MINOR, "x", [])
    assert plan.target_version == "3.1.2"
    # both the response_format attempt and the plain retry were issued
    assert len(llm.client.chat.completions.calls) == 2


def test_compat_diagnose_parses_next_version():
    llm = _llm_with(lambda k: '{"give_up":false,"next_version":"3.1.2","reasoning":"r"}')
    d = llm.diagnose(make_finding(), "3.1.3", CheckResult(passed=False, detail="x"), ["3.1.2"])
    assert d.give_up is False and d.next_version == "3.1.2"


def test_compat_diagnose_null_next_version():
    llm = _llm_with(lambda k: '{"give_up":true,"next_version":null,"reasoning":"done"}')
    d = llm.diagnose(make_finding(), "3.1.3", CheckResult(passed=False), [])
    assert d.give_up is True and d.next_version is None


def test_compat_budget_exhausted_raises():
    llm = _llm_with(lambda k: "{}", max_calls=1)
    llm.plan_fix(make_finding(), Delta.MINOR, "x", [])      # uses the one allowed call
    with pytest.raises(RuntimeError, match="budget"):
        llm.diagnose(make_finding(), "3.1.3", CheckResult(passed=False), [])


def test_compat_write_pr_description_success():
    llm = _llm_with(lambda k: "A concise description.")
    out = llm.write_pr_description(make_finding(), "3.0.0", "3.1.3", "ok")
    assert out == "A concise description."


def test_compat_write_pr_description_exception_is_handled():
    def behavior(kwargs):
        raise RuntimeError("api down")
    llm = _llm_with(behavior)
    out = llm.write_pr_description(make_finding(), "3.0.0", "3.1.3", "ok")
    assert "Bump flask 3.0.0 -> 3.1.3" in out and "api down" in out


def test_compat_reset_budget():
    llm = _llm_with(lambda k: "{}")
    llm.calls = 4
    llm.reset_budget()
    assert llm.calls == 0


# --------------------------------------------------------------------------- #
# make_llm() provider selection
# --------------------------------------------------------------------------- #
_KEYS = ["LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
         "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
         "OLLAMA_API_KEY", "OPENAI_API_KEY"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("provider", ["", "stub", "none"])
def test_make_llm_defaults_to_stub(monkeypatch, provider):
    if provider:
        monkeypatch.setenv("LLM_PROVIDER", provider)
    llm, label = make_llm(None)
    assert isinstance(llm, StubLLM) and "stub" in label


def test_make_llm_unknown_provider_is_stub(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "definitely-not-real")
    llm, label = make_llm(None)
    assert isinstance(llm, StubLLM) and "unknown provider" in label


def test_make_llm_provider_without_key_is_stub(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")  # no GROQ_API_KEY
    llm, label = make_llm(None)
    assert isinstance(llm, StubLLM) and "no API key" in label


def test_make_llm_groq_with_key_builds_client(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "secret")
    llm, label = make_llm(None)
    assert isinstance(llm, OpenAICompatLLM)
    assert label == "groq:llama-3.3-70b-versatile"


def test_make_llm_model_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "llama-3.1-8b-instant")
    _, label = make_llm(None)
    assert label.endswith("llama-3.1-8b-instant")


def test_make_llm_ollama_needs_no_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    llm, label = make_llm(None)
    assert isinstance(llm, OpenAICompatLLM) and label.startswith("ollama:")


def test_make_llm_init_failure_falls_back_to_stub(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "secret")

    def boom(*a, **k):
        raise RuntimeError("openai missing")
    monkeypatch.setattr(llm_mod, "OpenAICompatLLM", boom)
    llm, label = make_llm(None)
    assert isinstance(llm, StubLLM) and "could not init" in label
