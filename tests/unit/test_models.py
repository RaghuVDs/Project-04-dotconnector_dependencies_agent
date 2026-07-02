"""Unit: canonical data models."""
import pytest

from src.models import (
    CheckResult, Ecosystem, Finding, Outcome, RemediationResult, Severity,
)

pytestmark = pytest.mark.unit


def test_enum_values_are_stable():
    # these strings end up in audit logs / report.json -- pin them
    assert Severity.CRITICAL.value == "critical"
    assert Ecosystem.PYPI.value == "pypi"
    assert Outcome.FIXED.value == "fixed"
    assert {e.value for e in Ecosystem} >= {"pypi", "maven", "npm", "cargo", "go", "unknown"}


def test_finding_defaults():
    f = Finding(finding_id="x", repository="r", ecosystem=Ecosystem.PYPI,
                package="p", declared_version="1.0.0")
    assert f.recommended_version is None
    assert f.severity == Severity.UNKNOWN
    assert f.cve_ids == []
    assert f.waived is False
    assert f.purl is None


def test_fingerprint_uses_purl_and_target():
    f = Finding(finding_id="x", repository="repoA", ecosystem=Ecosystem.PYPI,
                package="flask", declared_version="3.0.0",
                recommended_version="3.1.3", purl="pkg:pypi/flask@3.0.0")
    assert f.fingerprint == "repoA::pkg:pypi/flask@3.0.0::3.1.3"


def test_fingerprint_falls_back_to_package_and_none_target():
    f = Finding(finding_id="x", repository="repoA", ecosystem=Ecosystem.PYPI,
                package="flask", declared_version="3.0.0")  # no purl, no rec
    assert f.fingerprint == "repoA::flask::none"


def test_fingerprint_dedup_identity():
    a = Finding(finding_id="1", repository="r", ecosystem=Ecosystem.PYPI,
                package="pandas", declared_version="2.0.2", purl="pkg:pypi/pandas@2.0.2")
    b = Finding(finding_id="2", repository="r", ecosystem=Ecosystem.PYPI,
                package="pandas", declared_version="2.0.2", purl="pkg:pypi/pandas@2.0.2")
    assert a.fingerprint == b.fingerprint  # different ids, same change -> dedup


def test_checkresult_failure_factory():
    c = CheckResult.failure("tests_pass", "boom", "trace")
    assert c.passed is False
    assert c.checks == {"tests_pass": False}
    assert c.detail == "boom"
    assert c.test_output == "trace"


def test_checkresult_defaults():
    c = CheckResult(passed=True)
    assert c.checks == {}
    assert c.detail == ""
    assert c.test_output == ""


def test_remediationresult_log_appends_in_order():
    r = RemediationResult(finding_id="x", package="p", outcome=Outcome.ERROR,
                          from_version="1.0.0")
    assert r.audit == []
    r.log("first")
    r.log("second")
    assert r.audit == ["first", "second"]
    assert r.to_version is None and r.attempts == 0
